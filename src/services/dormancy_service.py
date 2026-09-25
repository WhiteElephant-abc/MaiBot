"""作息（休眠）服务。

按配置在固定时段让整个麦麦进入休眠：休眠期间拦截一切模型与嵌入调用，
并让常驻后台任务挂起，直到醒来。

休眠标志落在本地存储里而不是按时间实时推算，是为了给以后的动态作息
（比如白天睡觉、临时改作息）留出空间——调度只负责翻转标志，
读取方永远只认标志本身。
"""

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, Optional

import asyncio
import contextvars

from src.common.logger import get_logger
from src.config.config import global_config
from src.manager.local_store_manager import local_storage

logger = get_logger("dormancy")

_STATE_KEY = "dormancy_state"
_TICK_SECONDS = 30.0

_bypass_dormancy = contextvars.ContextVar("dormancy_bypass", default=False)


class DormancyError(RuntimeError):
    """休眠期间发起的模型调用。

    休眠的正常路径是调用方先查 `is_dormant()` 并让路，这个异常只作为兜底，
    防止新加的调用点漏掉判断而悄悄消耗额度。
    """


@contextmanager
def bypass_dormancy() -> Iterator[None]:
    """临时放行模型调用，仅供 WebUI、CLI 等由人主动发起的操作使用。"""

    token = _bypass_dormancy.set(True)
    try:
        yield
    finally:
        _bypass_dormancy.reset(token)


def _parse_hhmm(value: str) -> int:
    """将 HH:MM 解析为当日分钟数。"""

    hour_text, minute_text = value.split(":")
    return int(hour_text) * 60 + int(minute_text)


class DormancyService:
    """作息调度与休眠闸门。"""

    def __init__(self) -> None:
        """初始化服务并从本地存储恢复休眠标志。"""

        self._dormant = False
        self._sleep_started_at = ""
        self._sleep_started_display = ""
        self._wake_notice = ""
        self._announced_on = ""
        self._task: Optional[asyncio.Task[None]] = None
        self._load_state()

    # ------------------------------------------------------------------
    # 状态落盘
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """从本地存储恢复休眠标志，进程重启后仍保持睡着状态。"""

        raw_state = local_storage[_STATE_KEY]
        if isinstance(raw_state, dict):
            self._dormant = bool(raw_state.get("dormant", False))
            self._sleep_started_at = str(raw_state.get("sleep_started_at", ""))
            self._sleep_started_display = str(raw_state.get("sleep_started_display", ""))
            self._wake_notice = str(raw_state.get("wake_notice", ""))
            self._announced_on = str(raw_state.get("announced_on", ""))
        else:
            self._dormant = False
            self._sleep_started_at = ""
            self._sleep_started_display = ""
            self._wake_notice = ""
            self._announced_on = ""

        if not global_config.dormancy.enabled:
            # 作息关闭时不应沿用上次落盘的任何作息状态：休眠标志会让麦麦一直不开口，
            # 上一轮的苏醒提示也可能在几天后才被新建的运行时注入进去
            if self._dormant:
                logger.info("作息已关闭，清除上次遗留的休眠标志")
            self._dormant = False
            self._sleep_started_at = ""
            self._sleep_started_display = ""
            self._wake_notice = ""

        if self._dormant:
            logger.info(f"从本地存储恢复了休眠状态，开始时间: {self._sleep_started_at or '未知'}")

    def _save_state(self) -> None:
        """把休眠标志写入本地存储。"""

        state: Dict[str, Any] = {
            "dormant": self._dormant,
            "sleep_started_at": self._sleep_started_at,
            "sleep_started_display": self._sleep_started_display,
            "wake_notice": self._wake_notice,
            "announced_on": self._announced_on,
        }
        local_storage[_STATE_KEY] = state

    # ------------------------------------------------------------------
    # 闸门
    # ------------------------------------------------------------------

    def is_dormant(self) -> bool:
        """当前是否处于休眠，供各调用点让路使用。"""

        return self._dormant

    async def wait_until_awake(self, *, poll_seconds: float = 30.0) -> None:
        """等到醒来为止，未休眠时立即返回。

        供已经持有任务、需要原地等待的后台消费者使用。内部按固定间隔轮询而不是
        用事件集中唤醒：挂起的消费者如果同时被放行，醒来瞬间会一起发起请求。
        定时轮询型的后台循环不需要调用它，各自判断 `is_dormant()` 跳过本轮即可，
        那样能天然保持各自的相位。
        """

        while self._dormant:
            await asyncio.sleep(poll_seconds)

    def should_block_model_call(self) -> bool:
        """判断本次模型调用是否应被休眠拦截。"""

        return self._dormant and not _bypass_dormancy.get()

    def wake_notice(self) -> str:
        """返回刚结束的这次睡眠的苏醒提示，没有则返回空字符串。

        会话运行时在睡眠期间已全部停止，因此醒来后新建的运行时意味着这是该会话
        醒来后的第一次活动。提示里带明确起止时刻，模型可以对照请求末尾的当前
        时间自行判断距醒来过了多久，因此不做过期清理。
        """

        return self._wake_notice

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """启动作息调度任务。"""

        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="dormancy_scheduler")
        logger.info(f"作息调度已启动: enabled={global_config.dormancy.enabled}")

    async def stop(self) -> None:
        """停止作息调度任务。"""

        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        """周期性对齐作息状态。

        单轮对齐失败只记录并进入下一轮，不终止调度：调度一旦退出就再也没有东西
        会重启它，作息会永久停止翻转而进程仍活着，表现为「作息莫名其妙不工作了」，
        这比反复出现的错误日志更难排查。
        """

        while True:
            try:
                await self._evaluate()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("作息状态对齐失败，将在下一轮重试")
            await asyncio.sleep(_TICK_SECONDS)

    async def _evaluate(self) -> None:
        """按当前时间与配置翻转休眠状态。"""

        if not global_config.dormancy.enabled:
            if self._dormant:
                await self._wake(reason="作息已关闭")
            return

        now = datetime.now()
        now_minutes = now.hour * 60 + now.minute
        sleep_minutes = _parse_hhmm(global_config.dormancy.sleep_time)
        wake_minutes = _parse_hhmm(global_config.dormancy.wake_time)

        if self._is_within_sleep_window(now_minutes, sleep_minutes, wake_minutes):
            await self._fall_asleep(now)
            return

        if self._dormant:
            await self._wake(reason="到达起床时间")

        await self._maybe_announce(now, now_minutes, sleep_minutes)

    @staticmethod
    def _is_within_sleep_window(now_minutes: int, sleep_minutes: int, wake_minutes: int) -> bool:
        """判断当前分钟数是否落在就寝时段内，支持跨夜。"""

        if sleep_minutes == wake_minutes:
            # 起止时间相同视为不做息，避免误判成全天休眠
            return False
        if sleep_minutes < wake_minutes:
            return sleep_minutes <= now_minutes < wake_minutes
        return now_minutes >= sleep_minutes or now_minutes < wake_minutes

    async def _fall_asleep(self, now: datetime) -> None:
        """进入休眠并停掉所有会话运行时。"""

        if not self._dormant:
            self._dormant = True
            self._sleep_started_at = now.isoformat(timespec="seconds")
            self._sleep_started_display = now.strftime("%m-%d %H:%M")
            # 新一轮睡眠开始，上一轮的苏醒提示不再有意义
            self._wake_notice = ""
            self._save_state()
            logger.info(f"进入休眠: sleep_started_at={self._sleep_started_at}")

        await self._stop_all_chat_runtimes()

    async def _wake(self, *, reason: str) -> None:
        """结束休眠。

        只翻转标志，不主动恢复任何任务：休眠期间消息照常入库，常驻后台循环
        各自保持原有节奏、只跳过休眠中的轮次，因此醒来没有积压需要集中释放。
        """

        # 在本刻把苏醒提示定稿，运行时重建时直接注入，读取路径不做任何时间解析
        self._wake_notice = self._build_wake_notice()
        self._dormant = False
        self._sleep_started_at = ""
        self._sleep_started_display = ""
        self._announced_on = ""
        self._save_state()
        logger.info(f"结束休眠: reason={reason}")

    def _build_wake_notice(self) -> str:
        """生成苏醒提示文本，就寝跨度缺失时返回空字符串。"""

        if not self._sleep_started_display:
            return ""
        wake_display = datetime.now().strftime("%m-%d %H:%M")
        return (
            "这是作息系统的作息提示，不代表当前用户刚刚发来新消息。\n"
            f"你按自己的作息休息了一段时间：{self._sleep_started_display} 就寝，{wake_display} 起床。\n"
            "现在你已经醒了，这是你醒来后在本会话里第一次看到消息，"
            "休息期间的消息都记录在上面的历史中。\n"
            "回复时请结合实际间隔判断：刚醒来不久可以自然表现出刚睡醒的状态。"
        )

    async def _stop_all_chat_runtimes(self) -> None:
        """停掉所有仍在运行的会话运行时。"""

        from src.chat.heart_flow.heartflow_manager import heartflow_manager

        stopped_count = await heartflow_manager.stop_all_chats(reason="dormancy")
        if stopped_count > 0:
            logger.info(f"休眠已停止 {stopped_count} 个会话运行时")

    # ------------------------------------------------------------------
    # 睡前告别
    # ------------------------------------------------------------------

    async def _maybe_announce(self, now: datetime, now_minutes: int, sleep_minutes: int) -> None:
        """就寝前向仍活跃的会话注入睡前意图。"""

        announce_lead_minutes = int(global_config.dormancy.announce_lead_minutes)
        announce_minutes = sleep_minutes - announce_lead_minutes
        if announce_minutes < 0:
            # 就寝时间早于提前量（例如就寝 00:05、提前 15 分钟），按跨夜折算
            announce_minutes += 24 * 60

        today_key = now.strftime("%Y-%m-%d")
        if self._announced_on == today_key:
            return

        if not self._is_within_announce_window(now_minutes, announce_minutes, sleep_minutes):
            return

        self._announced_on = today_key
        self._save_state()
        await self._announce_to_relevant_chats()

    @staticmethod
    def _is_within_announce_window(now_minutes: int, announce_minutes: int, sleep_minutes: int) -> bool:
        """判断当前分钟数是否落在睡前提醒窗口内。"""

        if announce_minutes < sleep_minutes:
            return announce_minutes <= now_minutes < sleep_minutes
        return now_minutes >= announce_minutes or now_minutes < sleep_minutes

    async def _announce_to_relevant_chats(self) -> None:
        """对满足「最近有消息」或「麦麦最近发过言」的会话说晚安。

        两个条件各自独立触发，任一成立即算需要告别；各自的窗口设为 0 表示
        不按该条件判断，可以只用其中一个。
        """

        # 这条导入必须留在函数内：reference_type 定义在 maisaka.context.messages，
        # 而它的依赖链会绕回 chat.message_receive.bot，后者在模块级导入本模块
        from src.chat.heart_flow.heartflow_manager import heartflow_manager
        from src.maisaka.context.messages import ReferenceMessageType

        message_window_seconds = int(global_config.dormancy.announce_recent_message_minutes) * 60
        spoke_window_seconds = int(global_config.dormancy.announce_bot_spoke_minutes) * 60
        now_timestamp = datetime.now().timestamp()
        sleep_time = global_config.dormancy.sleep_time

        announced_count = 0
        for runtime in list(heartflow_manager.heartflow_chat_list.values()):
            has_recent_message = (
                message_window_seconds > 0
                and now_timestamp - runtime.last_message_received_at() <= message_window_seconds
            )
            has_recent_speech = (
                spoke_window_seconds > 0 and now_timestamp - runtime.last_bot_spoke_at() <= spoke_window_seconds
            )
            if not (has_recent_message or has_recent_speech):
                continue
            try:
                runtime.enqueue_state_notice(
                    content=(
                        "这是作息系统的就寝提醒，不代表当前用户刚刚发来新消息。\n"
                        f"现在已经到了你的就寝时间（{sleep_time}），你准备去睡觉了。\n"
                        "请让正在聊天的人知道你要去睡了。"
                    ),
                    reference_type=ReferenceMessageType.DORMANCY_SLEEP,
                    display_prefix="[作息提醒]",
                    reason="作息系统就寝提醒",
                    trigger_turn=True,
                )
                announced_count += 1
            except Exception as exc:
                logger.warning(f"{runtime.log_prefix} 注入就寝提醒失败: {exc}")

        logger.info(f"睡前告别已提交给 {announced_count} 个相关会话")


dormancy_service = DormancyService()
"""全局作息服务单例。"""
