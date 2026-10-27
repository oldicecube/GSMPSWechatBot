"""wait 内置工具。"""

from typing import Optional

from src.config.config import global_config
from src.core.tooling import ToolExecutionContext, ToolExecutionResult, ToolInvocation, ToolSpec

from .context import BuiltinToolRuntimeContext


def get_tool_spec() -> ToolSpec:
    """获取 wait 工具声明。"""

    return ToolSpec(
        name="wait",
        description="等待一段时间；期间不查看聊天，用于一段时间后再次查看聊天内容。不适用于正在进行的聊天。",
        parameters_schema={
            "type": "object",
            "properties": {
                "seconds": {
                    "type": "integer",
                    "description": "等待秒数。",
                },
            },
            "required": ["seconds"],
        },
        provider_name="maisaka_builtin",
        provider_type="builtin",
    )


async def handle_tool(
    tool_ctx: BuiltinToolRuntimeContext,
    invocation: ToolInvocation,
    context: Optional[ToolExecutionContext] = None,
) -> ToolExecutionResult:
    """执行 wait 内置工具。"""

    del context
    seconds = invocation.arguments.get("seconds", 30)
    try:
        wait_seconds = int(seconds)
    except (TypeError, ValueError):
        wait_seconds = 30
    wait_seconds = max(0, wait_seconds)
    current_count = tool_ctx.runtime._enter_counted_wait_state(
        seconds=wait_seconds,
        tool_call_id=invocation.call_id,
        logical_turn_id=tool_ctx.engine.active_logical_turn_id,
    )
    wait_detail = (
        f"当前对话循环进入等待状态，最多等待 {wait_seconds} 秒；新消息可提前结束等待并继续思考。"
        if global_config.chat.reply_timing.planner_message_steering
        else f"当前对话循环进入等待状态，将固定等待 {wait_seconds} 秒；期间收到的新消息不会提前打断本次等待。"
    )
    return tool_ctx.build_success_result(
        invocation.tool_name,
        f"{wait_detail}连续 wait 次数：{current_count}。",
        metadata={
            "pause_execution": True,
            "consecutive_wait_count": current_count,
        },
    )
