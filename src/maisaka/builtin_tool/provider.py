"""Maisaka 内置工具 Provider。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Dict, Optional

from src.core.tooling import (
    ToolAvailabilityContext,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolProvider,
    ToolSpec,
)

from . import get_all_builtin_tool_specs

BuiltinToolHandler = Callable[[ToolInvocation, Optional[ToolExecutionContext]], Awaitable[ToolExecutionResult]]


class MaisakaBuiltinToolProvider(ToolProvider):
    """Maisaka 内置工具提供者。"""

    provider_name = "maisaka_builtin"
    provider_type = "builtin"

    def __init__(self, handlers: Optional[Dict[str, BuiltinToolHandler]] = None) -> None:
        """初始化内置工具 Provider。

        Args:
            handlers: 工具名到异步处理器的映射。
        """

        self._handlers = dict(handlers or {})

    async def list_tools(
        self,
        context: Optional[ToolAvailabilityContext] = None,
    ) -> list[ToolSpec]:
        """列出全部内置工具。"""

        return list(get_all_builtin_tool_specs(context))

    async def invoke(
        self,
        invocation: ToolInvocation,
        context: Optional[ToolExecutionContext] = None,
    ) -> ToolExecutionResult:
        """执行指定内置工具。

        Args:
            invocation: 工具调用请求。
            context: 执行上下文。

        Returns:
            ToolExecutionResult: 工具执行结果。
        """

        handler = self._handlers.get(invocation.tool_name)
        if handler is None:
            return ToolExecutionResult(
                tool_name=invocation.tool_name,
                success=False,
                error_message=f"未找到内置工具处理器：{invocation.tool_name}",
            )
        return await handler(invocation, context)

    async def close(self) -> None:
        """关闭 Provider。

        内置 Provider 无需释放额外资源。
        """

