"""转发 Tool 的可信 Planner 会话授权。"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from secrets import token_urlsafe
from typing import Any

from .output_items import (
    extract_output_item_tool_calls,
    replace_output_item_arguments,
)

FORWARD_TOOL_NAME = "request_cross_group_forward"
FORWARD_CONTEXT_TOKEN_ARGUMENT = "_forward_context_token"
FORWARD_AUTHORIZATION_ROUND_KWARG = "_forward_authorization_round"
PUBLIC_FORWARD_ARGUMENTS = ("msg_id", "sharing_reason", "content_summary")
MAX_PENDING_AUTHORIZATIONS = 1024


class ForwardInvocationGate:
    """清洗模型参数，并以一次性凭据绑定真实 Planner 会话。"""

    def __init__(self) -> None:
        """创建空的一次性授权表。

        授权只覆盖尚未进入正式 Tool handler 的调用，并限制最大数量。插件
        重载或配置更新会清空全部凭据；已经进入投递服务的任务不受影响。
        """

        self._authorizations: OrderedDict[str, tuple[str, str, str]] = OrderedDict()

    def sanitize_and_authorize(self, session_id: str, tool_calls: Any) -> tuple[Any, str]:
        """清洗全部转发调用，并为本轮有效调用签发新会话凭据。

        模型提供的 ``platform``、``group_id``、``stream_id`` 等未声明字段
        会被删除。非转发工具和供应商附加字段保持不变。每个转发调用都会撤销
        其输入参数里携带的旧凭据，再签发本次新凭据；同 session 其他并发请求
        的凭据不受影响，模型回放历史参数也不能复用旧授权。非列表输入原样返回。

        Args:
            session_id: ``after_response`` Hook 提供的真实 Planner 会话 ID；
                为空时只清洗参数，不签发凭据。
            tool_calls: Host 序列化后的工具调用列表。

        Returns:
            二元组包含清洗后的工具调用和不可预测的本轮标记；非列表工具
            输入在第一项保持原值。LATE Hook 必须同时验证标记与调用凭据。
        """

        round_id = token_urlsafe(32)
        if not isinstance(tool_calls, list):
            return tool_calls, round_id

        normalized_session_id = str(session_id or "").strip()
        sanitized_calls = deepcopy(tool_calls)
        for tool_call in sanitized_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            if str(function.get("name") or "").strip() != FORWARD_TOOL_NAME:
                continue
            arguments = function.get("arguments")
            if not isinstance(arguments, dict):
                continue

            stale_token = str(arguments.get(FORWARD_CONTEXT_TOKEN_ARGUMENT) or "").strip()
            if stale_token:
                self._authorizations.pop(stale_token, None)

            sanitized_arguments = {
                key: deepcopy(arguments[key]) for key in PUBLIC_FORWARD_ARGUMENTS if key in arguments
            }
            message_id = str(sanitized_arguments.get("msg_id") or "").strip()
            if message_id:
                sanitized_arguments["msg_id"] = message_id

            if normalized_session_id and message_id:
                sanitized_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT] = self._issue(
                    normalized_session_id,
                    message_id,
                    round_id,
                )
            function["arguments"] = sanitized_arguments
        return sanitized_calls, round_id

    def sanitize_and_authorize_output_items(
        self,
        session_id: str,
        output_items: Any,
    ) -> tuple[Any, str]:
        """清洗最新 Context Item 载荷并为转发调用签发一次性凭据。

        Args:
            session_id: ``maisaka.planner.after_response`` 提供的真实 Planner
                会话 ID；为空时只清洗公开参数，不签发凭据。
            output_items: 最新 Hook 契约中的 ``FunctionCallItem`` 快照列表。
                非列表输入原样返回，结构异常的 Item 不会被强行重建。

        Returns:
            二元组包含清洗后的 ``output_items`` 深拷贝和本轮随机标记。每个
            转发调用只保留公开参数，并在 session 与 ``msg_id`` 均有效时附加
            内部一次性凭据。
        """

        round_id = token_urlsafe(32)
        if not isinstance(output_items, list):
            return output_items, round_id

        normalized_session_id = str(session_id or "").strip()
        sanitized_items = output_items
        for call in extract_output_item_tool_calls(output_items):
            if call.name != FORWARD_TOOL_NAME:
                continue
            stale_token = str(call.arguments.get(FORWARD_CONTEXT_TOKEN_ARGUMENT) or "").strip()
            if stale_token:
                self._authorizations.pop(stale_token, None)

            sanitized_arguments = self._public_arguments(call.arguments)
            message_id = str(sanitized_arguments.get("msg_id") or "").strip()
            if message_id:
                sanitized_arguments["msg_id"] = message_id
            if normalized_session_id and message_id:
                sanitized_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT] = self._issue(
                    normalized_session_id,
                    message_id,
                    round_id,
                )
            sanitized_items = replace_output_item_arguments(
                sanitized_items,
                call.index,
                sanitized_arguments,
            )
        return sanitized_items, round_id

    def sanitize_for_orchestration(
        self,
        session_id: str,
        tool_calls: Any,
        round_id: str,
    ) -> Any:
        """清洗 LATE Hook 参数，并只保留 EARLY 已签发的有效凭据。

        本方法绝不签发或改绑凭据。两个 Hook 之间若配置更新清空授权、其他
        Hook 改写 ``session_id``、本轮标记丢失，或调用携带未知凭据，内部
        字段会被删除，自动查看协调器不会接管，正式 handler 最终按缺少授权
        拒绝。

        Args:
            session_id: LATE ``after_response`` Hook 当前收到的 Planner 会话 ID。
            tool_calls: 上游 Hook 已处理的序列化工具调用列表。
            round_id: EARLY Hook 经同一 Hook 链传递的本轮随机标记；为空或
                不匹配时不接受任何存量凭据。

        Returns:
            清洗后的工具调用深拷贝。只有凭据仍精确绑定本轮标记、当前
            session 与 ``msg_id`` 时才保留内部字段；非列表输入保持原值。
        """

        if not isinstance(tool_calls, list):
            return tool_calls

        normalized_session_id = str(session_id or "").strip()
        normalized_round_id = str(round_id or "").strip()
        sanitized_calls = deepcopy(tool_calls)
        for tool_call in sanitized_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            if str(function.get("name") or "").strip() != FORWARD_TOOL_NAME:
                continue
            arguments = function.get("arguments")
            if not isinstance(arguments, dict):
                continue

            sanitized_arguments = {
                key: deepcopy(arguments[key]) for key in PUBLIC_FORWARD_ARGUMENTS if key in arguments
            }
            message_id = str(sanitized_arguments.get("msg_id") or "").strip()
            if message_id:
                sanitized_arguments["msg_id"] = message_id

            token = str(arguments.get(FORWARD_CONTEXT_TOKEN_ARGUMENT) or "").strip()
            if (
                normalized_round_id
                and token
                and self._authorizations.get(token)
                == (
                    normalized_session_id,
                    message_id,
                    normalized_round_id,
                )
            ):
                sanitized_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT] = token
            elif token:
                self._authorizations.pop(token, None)
            function["arguments"] = sanitized_arguments
        return sanitized_calls

    def sanitize_output_items_for_orchestration(
        self,
        session_id: str,
        output_items: Any,
        round_id: str,
    ) -> Any:
        """清洗 LATE Hook 的最新 Context Item 载荷并验证调用绑定。

        Args:
            session_id: LATE Hook 当前收到的 Planner 会话 ID。
            output_items: 上游 Hook 已处理的 Context Item 快照列表。
            round_id: EARLY Hook 在同一分发链生成的随机标记；为空或不匹配
                时不会保留任何内部凭据。

        Returns:
            深拷贝后的 Context Item 列表。只有凭据精确绑定当前 round、session
            和同一 ``msg_id`` 时才会保留；其他转发调用仍保留公开参数，最终
            由正式 Tool handler 拒绝。
        """

        if not isinstance(output_items, list):
            return output_items

        normalized_session_id = str(session_id or "").strip()
        normalized_round_id = str(round_id or "").strip()
        sanitized_items = output_items
        for call in extract_output_item_tool_calls(output_items):
            if call.name != FORWARD_TOOL_NAME:
                continue
            sanitized_arguments = self._public_arguments(call.arguments)
            message_id = str(sanitized_arguments.get("msg_id") or "").strip()
            if message_id:
                sanitized_arguments["msg_id"] = message_id

            token = str(call.arguments.get(FORWARD_CONTEXT_TOKEN_ARGUMENT) or "").strip()
            if (
                normalized_round_id
                and token
                and self._authorizations.get(token)
                == (
                    normalized_session_id,
                    message_id,
                    normalized_round_id,
                )
            ):
                sanitized_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT] = token
            elif token:
                self._authorizations.pop(token, None)
            sanitized_items = replace_output_item_arguments(
                sanitized_items,
                call.index,
                sanitized_arguments,
            )
        return sanitized_items

    def authorize_arguments(
        self,
        session_id: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """为插件恢复的转发参数签发一次性会话凭据。

        Args:
            session_id: 原始直接请求所属的真实 Planner 会话 ID。
            arguments: 仅包含公开 Tool 字段的原始请求参数。

        Returns:
            清洗后的独立参数字典。会话和 ``msg_id`` 均有效时额外包含内部
            一次性凭据；格式无效时不签发，正式处理器将安全拒绝。
        """

        normalized_session_id = str(session_id or "").strip()
        sanitized_arguments = {key: deepcopy(arguments[key]) for key in PUBLIC_FORWARD_ARGUMENTS if key in arguments}
        message_id = str(sanitized_arguments.get("msg_id") or "").strip()
        if message_id:
            sanitized_arguments["msg_id"] = message_id
        if normalized_session_id and message_id:
            sanitized_arguments[FORWARD_CONTEXT_TOKEN_ARGUMENT] = self._issue(
                normalized_session_id,
                message_id,
                "",
            )
        return sanitized_arguments

    def consume(self, message_id: str, invocation_context: dict[str, Any]) -> str:
        """消费一次性凭据并返回其绑定的真实 Planner 会话。

        正式 Tool handler 不信任模型可写的 Host 上下文字段，只使用本方法
        返回的会话 ID。凭据无论匹配与否都会在首次消费时删除，避免猜测或
        错配调用反复试用。

        Args:
            message_id: 本次 Tool 调用声明的源消息 ID。
            invocation_context: Runner 传给 Tool handler 的完整参数字典；
                方法只读取内部凭据字段。

        Returns:
            凭据与规范化 ``message_id`` 匹配时返回真实 ``session_id``；
            缺失、未知或错配时返回空字符串。
        """

        token = str(invocation_context.get(FORWARD_CONTEXT_TOKEN_ARGUMENT) or "").strip()
        if not token:
            return ""
        authorization = self._authorizations.pop(token, None)
        if authorization is None:
            return ""
        session_id, authorized_message_id, _ = authorization
        if authorized_message_id != str(message_id or "").strip():
            return ""
        return session_id

    def revoke_arguments(self, arguments: dict[str, Any]) -> None:
        """撤销一组被自动查看流程替换的未消费参数凭据。

        Args:
            arguments: 含内部凭据的清洗后转发参数。字段缺失或未知时忽略。
        """

        token = str(arguments.get(FORWARD_CONTEXT_TOKEN_ARGUMENT) or "").strip()
        if token:
            self._authorizations.pop(token, None)

    def revoke_calls(self, tool_calls: Any) -> None:
        """撤销一批即将被编排器丢弃的转发调用凭据。

        非转发工具、结构异常调用和不含内部凭据的参数都会被忽略；方法不修改
        输入列表。该操作用于路径 B 用系统查看或恢复调用替换模型本轮输出前，
        避免被覆盖调用的凭据继续留在授权表中。

        Args:
            tool_calls: LATE Hook 已清洗、但即将被替换的工具调用列表。
        """

        if not isinstance(tool_calls, list):
            return
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict):
                continue
            if str(function.get("name") or "").strip() != FORWARD_TOOL_NAME:
                continue
            arguments = function.get("arguments")
            if isinstance(arguments, dict):
                self.revoke_arguments(arguments)

    def revoke_output_items(self, output_items: Any) -> None:
        """撤销最新 Context Item 载荷中所有未消费的转发凭据。

        Args:
            output_items: LATE Hook 当前收到的 Context Item 快照列表。该方法
                只读取调用参数，不修改输入列表。
        """

        for call in extract_output_item_tool_calls(output_items):
            if call.name == FORWARD_TOOL_NAME:
                self.revoke_arguments(call.arguments)

    def clear(self) -> None:
        """清除全部尚未消费的一次性 Planner 会话凭据。"""

        self._authorizations.clear()

    def _issue(self, session_id: str, message_id: str, round_id: str) -> str:
        """生成并登记一条有容量上限的一次性授权。

        Args:
            session_id: 授权绑定的真实 Planner 会话 ID。
            message_id: 授权绑定的源消息 ID。
            round_id: EARLY Hook 的本轮标记；由可信路径 B 恢复的 handler
                凭据使用空字符串，不再经过 LATE 验证。

        Returns:
            不透明且不可由模型预测的随机凭据字符串。
        """

        token = token_urlsafe(32)
        self._authorizations[token] = (session_id, message_id, round_id)
        while len(self._authorizations) > MAX_PENDING_AUTHORIZATIONS:
            self._authorizations.popitem(last=False)
        return token

    @staticmethod
    def _public_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
        """复制转发 Tool 的公开参数并删除所有内部字段。

        Args:
            arguments: 模型或历史载荷提供的原始参数字典。

        Returns:
            仅包含 ``msg_id``、``sharing_reason`` 和 ``content_summary`` 的
            独立参数字典。
        """

        return {key: deepcopy(arguments[key]) for key in PUBLIC_FORWARD_ARGUMENTS if key in arguments}
