from typing import Final


PROMPT_EXAMPLE_EXPRESSION_STYLES: Final[frozenset[str]] = frozenset(
    {
        "我嘞个xxxx",
        "对对对",
        "这么强！",
    }
)


def strip_expression_style_usage_prefix(style: str) -> str:
    """移除表达学习 prompt 示例带出的“使用”前缀。"""

    normalized_style = str(style or "").strip()
    if normalized_style.startswith("使用"):
        return normalized_style[len("使用") :].strip()
    return normalized_style


def normalize_expression_style_for_learning(style: str) -> str:
    """规范化准备学习或使用的表达风格文本。"""

    return strip_expression_style_usage_prefix(style)


def is_prompt_example_expression_style(style: str) -> bool:
    """判断表达风格是否是 prompt 示例内容泄漏。"""

    normalized_style = normalize_expression_style_for_learning(style)
    return normalized_style in PROMPT_EXAMPLE_EXPRESSION_STYLES
