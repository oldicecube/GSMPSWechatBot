"""Hook 参数模型构造辅助。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Sequence


def build_object_schema(
    properties: Dict[str, Dict[str, Any]],
    *,
    required: Sequence[str] | None = None,
) -> Dict[str, Any]:
    """构造对象级 JSON Schema。

    Args:
        properties: 字段定义映射。
        required: 必填字段名列表。

    Returns:
        Dict[str, Any]: 标准化后的对象级 Schema。
    """

    schema: Dict[str, Any] = {
        "type": "object",
        "properties": deepcopy(properties),
    }
    normalized_required = [str(item).strip() for item in (required or []) if str(item).strip()]
    if normalized_required:
        schema["required"] = normalized_required
    return schema
