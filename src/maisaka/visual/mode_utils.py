from src.common.logger import get_logger
from src.config.config import config_manager, global_config
from src.llm_models.payload_content.context_item import SUPPORTED_IMAGE_FORMATS

logger = get_logger("maisaka_visual_mode")


def _normalize_model_names(model_names: list[str]) -> list[str]:
    """过滤空模型名，保持与模型任务实际回退逻辑一致。"""

    return [model_name.strip() for model_name in model_names if model_name.strip()]


def _resolve_enable_visual_task(
    task_name: str,
    visual_mode: str,
    mode_field_name: str,
    fallback_task_name: str = "",
) -> bool:
    """根据指定任务配置与视觉模式解析当前是否应启用视觉消息。

    Args:
        task_name: 目标模型任务名。
        visual_mode: 生效的视觉模式，取值 `text` / `multimodal` / `auto`。
        mode_field_name: 视觉模式对应的配置字段名，仅用于报错信息。
        fallback_task_name: 目标任务未配置模型时继用的任务名。

    Returns:
        bool: 是否应把图片直接交给该任务的模型。
    """

    model_config = config_manager.get_model_config()
    model_task_config = model_config.model_task_config
    task_config = getattr(model_task_config, task_name)
    models_by_name = {model.name: model for model in model_config.models}

    if visual_mode == "text":
        return False

    task_models = _normalize_model_names(list(task_config.model_list))
    resolved_task_name = task_name
    if not task_models and fallback_task_name:
        fallback_task_config = getattr(model_task_config, fallback_task_name)
        task_models = _normalize_model_names(list(fallback_task_config.model_list))
        resolved_task_name = fallback_task_name

    task_label = f"{task_name} 任务"
    if resolved_task_name != task_name:
        task_label = f"{task_name} 任务继用的 {resolved_task_name} 任务"

    missing_models = [model_name for model_name in task_models if model_name not in models_by_name]
    non_visual_models = [
        model_name for model_name in task_models if model_name in models_by_name and not models_by_name[model_name].visual
    ]

    if visual_mode == "multimodal":
        if missing_models:
            raise ValueError(
                f"{mode_field_name}=multimodal，但 {task_label}存在未定义的模型：{', '.join(missing_models)}"
            )
        if non_visual_models:
            raise ValueError(
                f"{mode_field_name}=multimodal，但 {task_label}存在未开启 visual 的模型："
                f"{', '.join(non_visual_models)}"
            )
        return True

    if missing_models:
        logger.warning(
            f"{mode_field_name}=auto 时发现 {task_label}存在未定义模型："
            f"{', '.join(missing_models)}，将退化为纯文本"
        )
        return False

    return bool(task_models) and not non_visual_models


def resolve_enable_visual_planner() -> bool:
    """根据 planner 配置解析当前是否应启用视觉消息。"""

    return _resolve_enable_visual_task("planner", global_config.visual.planner_mode, "planner_mode")


def resolve_enable_visual_replyer() -> bool:
    """根据 replyer 配置解析当前是否应启用视觉消息。"""

    return _resolve_enable_visual_task("replyer", global_config.visual.replyer_mode, "replyer_mode")


def is_image_description_consumed() -> bool:
    """判断当前规划与回复链路是否存在会读取图片描述的文本消费者。

    规划与回复都把图片本体直接交给模型时，描述文本不会被任何一方读取。

    Returns:
        bool: 是否存在需要图片描述的消费者。
    """

    return not (resolve_enable_visual_planner() and resolve_enable_visual_replyer())


def is_image_description_required(image_format: str = "") -> bool:
    """判断当前配置下是否需要为指定格式的图片生成文字描述。

    在消费者判断之上再叠加格式判断：图片格式无法直接送达模型时会降级为文本，
    此时即使链路本身是多模态也仍然需要描述。

    Args:
        image_format: 图片实际格式，例如 `png`、`jpeg`；未知时传空字符串。

    Returns:
        bool: 是否需要为该图片生成描述文本。
    """

    if is_image_description_consumed():
        return True

    normalized_format = str(image_format).strip().lower()
    if not normalized_format:
        # 格式未知时无法确认图片能直接送达模型，按需要描述处理
        return True

    return normalized_format not in SUPPORTED_IMAGE_FORMATS
