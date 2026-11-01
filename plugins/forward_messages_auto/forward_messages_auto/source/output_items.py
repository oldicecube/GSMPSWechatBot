"""MaiBot Context Item Hook 载荷的轻量处理辅助。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4


FUNCTION_CALL_ITEM_TYPE = "FunctionCallItem"


@dataclass(frozen=True, slots=True)
class OutputItemToolCall:
    """描述一个 Context Item 中的模型函数调用。"""

    index: int
    call_id: str
    name: str
    arguments: dict[str, Any]


def extract_output_item_tool_calls(output_items: Any) -> list[OutputItemToolCall]:
    """从最新 ``output_items`` 载荷中按原顺序提取函数调用。

    Args:
        output_items: MaiBot ``maisaka.planner.after_response`` 提供的 Context
            Item 快照列表。非列表、非 ``FunctionCallItem`` 或结构不完整的
            Item 会被忽略，交由正式 Tool handler 继续执行最终拒绝。

    Returns:
        按载荷顺序排列的函数调用描述列表。每个描述包含原列表下标、调用 ID、
        工具名称和独立的参数字典。
    """

    if not isinstance(output_items, list):
        return []

    calls: list[OutputItemToolCall] = []
    for index, item in enumerate(output_items):
        if not isinstance(item, dict) or item.get("item_type") != FUNCTION_CALL_ITEM_TYPE:
            continue
        raw_call = item.get("tool_call")
        if not isinstance(raw_call, dict):
            continue
        call_id = str(raw_call.get("call_id") or "").strip()
        name = str(raw_call.get("func_name") or "").strip()
        arguments = raw_call.get("args")
        if not call_id or not name or not isinstance(arguments, dict):
            continue
        calls.append(
            OutputItemToolCall(
                index=index,
                call_id=call_id,
                name=name,
                arguments=deepcopy(arguments),
            )
        )
    return calls


def serialize_output_item_tool_calls(output_items: Any) -> list[dict[str, Any]]:
    """把最新 Context Item 中的调用投影为旧版统计所需的字典形状。

    Args:
        output_items: MaiBot Context Item 快照列表。

    Returns:
        与旧版 ``tool_calls`` 相同的独立字典列表，仅用于插件内部调试统计，
        不会作为最新 Hook 的返回载荷。
    """

    return [
        {
            "id": call.call_id,
            "function": {
                "name": call.name,
                "arguments": deepcopy(call.arguments),
            },
        }
        for call in extract_output_item_tool_calls(output_items)
    ]


def replace_output_item_arguments(
    output_items: Any,
    index: int,
    arguments: dict[str, Any],
) -> Any:
    """只替换一个 ``FunctionCallItem`` 的参数并保留其余 Context Item。

    Args:
        output_items: MaiBot Context Item 快照列表。
        index: 要替换的函数调用 Item 在列表中的下标。
        arguments: 新的工具参数字典；调用方应先完成可信字段清洗。

    Returns:
        深拷贝后的 Item 列表。下标或 Item 结构无效时返回输入的深拷贝，
        不会为了修复异常载荷而插入新 Item。
    """

    copied_items = deepcopy(output_items)
    if not isinstance(copied_items, list) or not 0 <= index < len(copied_items):
        return copied_items
    item = copied_items[index]
    if not isinstance(item, dict) or item.get("item_type") != FUNCTION_CALL_ITEM_TYPE:
        return copied_items
    raw_call = item.get("tool_call")
    if not isinstance(raw_call, dict):
        return copied_items
    raw_call["args"] = deepcopy(arguments)
    return copied_items


def replace_output_item_call(
    output_items: Any,
    index: int,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    call_id: str,
) -> Any:
    """用新的函数调用替换一个 Context Item，并生成新的 Item ID。

    Args:
        output_items: MaiBot Context Item 快照列表。
        index: 要替换的函数调用 Item 在列表中的下标。
        tool_name: 新函数调用名称，例如 ``view_forward_message``。
        arguments: 新函数调用参数字典。
        call_id: 新函数调用 ID，必须为非空字符串。

    Returns:
        深拷贝后的 Item 列表。替换成功时只改变目标函数调用；目标结构无效
        时返回未修改的深拷贝。
    """

    copied_items = deepcopy(output_items)
    if not isinstance(copied_items, list) or not 0 <= index < len(copied_items):
        return copied_items
    item = copied_items[index]
    if not isinstance(item, dict) or item.get("item_type") != FUNCTION_CALL_ITEM_TYPE:
        return copied_items
    raw_call = item.get("tool_call")
    meta = item.get("meta")
    if not isinstance(raw_call, dict) or not isinstance(meta, dict):
        return copied_items
    normalized_tool_name = str(tool_name or "").strip()
    normalized_call_id = str(call_id or "").strip()
    if not normalized_tool_name or not normalized_call_id:
        return copied_items
    meta["item_id"] = uuid4().hex
    item["tool_call"] = {
        "call_id": normalized_call_id,
        "func_name": normalized_tool_name,
        "args": deepcopy(arguments),
        "extra_content": None,
    }
    return copied_items


def append_output_item_call(
    output_items: Any,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    call_id: str,
    template_item: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """在没有可替换调用时追加一个协议完整的函数调用 Item。

    Args:
        output_items: MaiBot Context Item 快照列表；非列表按空列表处理。
        tool_name: 新函数调用名称。
        arguments: 新函数调用参数字典。
        call_id: 新函数调用 ID。
        template_item: 可选的既有 Context Item，用于复用当前 Planner 轮次的
            ``logical_turn_id`` 和时间戳；缺失时会生成新的协议元数据。

    Returns:
        包含新函数调用的独立 Item 列表。新 Item 追加在原列表末尾，以保持
        当前非工具 Item 的相对顺序。
    """

    copied_items = deepcopy(output_items) if isinstance(output_items, list) else []
    template_meta = template_item.get("meta") if isinstance(template_item, dict) else None
    meta = deepcopy(template_meta) if isinstance(template_meta, dict) else {}
    meta["item_id"] = uuid4().hex
    meta.setdefault("logical_turn_id", uuid4().hex)
    meta.setdefault("timestamp", datetime.now().isoformat())
    copied_items.append(
        {
            "item_type": FUNCTION_CALL_ITEM_TYPE,
            "meta": meta,
            "tool_call": {
                "call_id": str(call_id or "").strip(),
                "func_name": str(tool_name or "").strip(),
                "args": deepcopy(arguments),
                "extra_content": None,
            },
        }
    )
    return copied_items


def replace_first_assistant_text(output_items: Any, text: str) -> Any:
    """将当前输出中的首个 assistant 正文替换为已有流程提示文本。

    Args:
        output_items: MaiBot Context Item 快照列表。
        text: 要写入的简体中文提示文本。

    Returns:
        深拷贝后的 Item 列表。没有 assistant 正文时不追加新 Item，避免改变
        原模型输出的 Item 数量和工具顺序。
    """

    copied_items = deepcopy(output_items)
    if not isinstance(copied_items, list):
        return copied_items
    for item in copied_items:
        if not isinstance(item, dict) or item.get("item_type") != "AssistantMessageItem":
            continue
        item["parts"] = [{"type": "text", "text": str(text or "")}]
        return copied_items
    return copied_items


def append_user_reminder(output_items: Any, text: str) -> list[dict[str, Any]]:
    """在最新 ``before_request`` Items 末尾追加一次判断提醒。

    Args:
        output_items: MaiBot ``before_request`` 提供的 Context Item 快照列表。
        text: 已由运行时构造的完整 ``system-reminder`` 文本。

    Returns:
        独立的 Context Item 列表，末尾包含一个新的 ``UserMessageItem``。该
        Item 使用空的 ``logical_turn_id``，不会被当作模型工具轮次。
    """

    copied_items = deepcopy(output_items) if isinstance(output_items, list) else []
    copied_items.append(
        {
            "item_type": "UserMessageItem",
            "meta": {
                "item_id": uuid4().hex,
                "logical_turn_id": None,
                "timestamp": datetime.now().isoformat(),
            },
            "parts": [{"type": "text", "text": str(text or "")}],
        }
    )
    return copied_items
