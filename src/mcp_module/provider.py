"""MCP 工具 Provider。"""

from __future__ import annotations

from typing import Optional

from src.core.tooling import (
    ToolAvailabilityContext,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolInvocation,
    ToolProvider,
    ToolSpec,
)

from .service import MCPService


class MCPToolProvider(ToolProvider):
    """基于进程级 MCPService 的轻量工具 Provider。"""

    provider_name = "mcp"
    provider_type = "mcp"

    def __init__(self, service: MCPService) -> None:
        """初始化 MCP 工具 Provider。

        Args:
            service: 进程级 MCP 服务。
        """

        self._service = service

    async def list_tools(
        self,
        context: Optional[ToolAvailabilityContext] = None,
    ) -> list[ToolSpec]:
        """列出全部 MCP 工具。"""

        del context
        tools = await self._service.list_tools()
        # The selected MCP search service is exposed through the native web
        # provider, with source labels, bounded previews and result persistence.
        # Keep the underlying connection callable by that provider without
        # presenting a second unbounded entry point to the model.
        from src.config.config import config_manager
        model_config = config_manager.get_model_config()
        selected = model_config.model_task_config.web_search.model_list
        service = model_config.web.services.get(selected[0]) if selected else None
        if service and service.adapter in {'stepsearch_mcp', 'parallel_mcp'}:
            tools = [tool for tool in tools if not (
                tool.metadata.get('server_name') == service.server_ref
                and tool.name in {'web_search', 'web_fetch'})]
        return tools

    async def invoke(
        self,
        invocation: ToolInvocation,
        context: Optional[ToolExecutionContext] = None,
    ) -> ToolExecutionResult:
        """执行指定 MCP 工具。

        Args:
            invocation: 工具调用请求。
            context: 执行上下文。

        Returns:
            ToolExecutionResult: 工具执行结果。
        """

        return await self._service.call_tool_invocation(invocation, context)

    async def close(self) -> None:
        """Runtime 不拥有共享连接，因此关闭 Provider 时无需释放服务。"""
