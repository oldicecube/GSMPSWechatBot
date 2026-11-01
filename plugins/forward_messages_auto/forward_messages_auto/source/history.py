"""source Planner 查看调用与结果历史解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import ViewObservationKind

VIEW_FORWARD_TOOL_NAME = "view_forward_message"
VIEW_FORWARD_CORRECTABLE_FAILURE_PREFIXES = (
    "查看转发消息工具需要提供有效的 `msg_id` 参数。",
    "未找到目标转发消息，msg_id=",
)
VIEW_FORWARD_TERMINAL_FAILURE_PREFIXES = ("目标消息不是可展开查看的转发消息，msg_id=",)
VIEW_FORWARD_RETRYABLE_FAILURE_PREFIXES = (
    "查看转发消息完整内容时发生异常。",
    "工具 view_forward_message 调用失败：",
    "未找到工具：view_forward_message",
    "未找到内置工具处理器：view_forward_message",
    "统一工具注册表尚未初始化。",
)
VIEW_FORWARD_EMPTY_CONTENT_FAILURE_PREFIXES = ("转发消息内容为空，msg_id=",)
VIEW_FORWARD_UNKNOWN_FAILURE_PREFIXES = ("工具 view_forward_message 执行失败。",)
_NESTED_FORWARD_PATH_PATTERN = re.compile(
    r"\[嵌套转发消息，path=\[(?P<indices>\d+(?:,\s*\d+)*)\]，"
    r"可再次调用 view_forward_message 展开\]"
)


@dataclass(frozen=True, slots=True)
class _ViewToolCall:
    """一条已验证工具名、调用 ID 和消息 ID 的查看调用。"""

    message_id: str
    path: tuple[int, ...] | None


@dataclass(frozen=True, slots=True)
class ViewToolObservation:
    """一组已经配对的查看工具调用、浏览路径及文本结果。"""

    call_id: str
    message_id: str
    content: str
    kind: ViewObservationKind
    path: tuple[int, ...] | None = ()
    nested_paths: tuple[tuple[int, ...], ...] = ()


class PlannerHistoryParser:
    """从 Planner 历史中配对查看工具调用及结果。"""

    @staticmethod
    def extract_view_results(messages: Any) -> list[tuple[str, str]]:
        """提取 ``view_forward_message`` 调用对应的成功文本结果。

        方法按消息出现顺序扫描 Planner 历史，先记录 assistant 工具调用中
        的调用 ID、消息 ID 和浏览路径，再用 tool 消息中的 ``tool_call_id``
        配对。该兼容接口不区分结果是否仍暴露待查看的嵌套路径；调用方需要
        完整性时应使用 ``extract_view_observations`` 和资格存储。
        无关工具、无 ID 调用和格式异常消息都会被忽略；空结果会被记录为
        空内容失败，因此不会出现在本方法返回的成功结果中。

        Args:
            messages: Planner 请求中的消息历史。期望为 OpenAI 兼容字典列表；
                传入其他类型时视为空历史。

        Returns:
            ``(msg_id, content)`` 二元组列表，顺序与匹配到的 tool 结果顺序
            一致。没有匹配项时返回空列表。

        Examples:
            assistant 调用 ID 为 ``call-1``、参数 ``msg_id="m1"``，随后 tool
            结果引用 ``call-1`` 时，返回 ``[("m1", "完整内容")]``。
        """

        return [
            (observation.message_id, observation.content)
            for observation in PlannerHistoryParser.extract_view_observations(messages)
            if observation.kind is ViewObservationKind.SUCCESS
        ]

    @staticmethod
    def extract_view_observations(messages: Any) -> list[ViewToolObservation]:
        """提取查看调用、结果文本和插件可识别的结果分类。

        方法同时支持旧版 OpenAI 消息历史和最新 Context Item 快照。旧版
        历史通过 ``assistant.tool_calls`` 与 ``tool`` 消息配对；最新载荷通过
        ``FunctionCallItem`` 与 ``FunctionCallOutputItem`` 的调用 ID 配对。
        最新载荷优先使用结构化 ``success`` 字段；显式失败再按内置工具稳定
        错误前缀细分，无法识别时归为未知失败。旧版载荷仍通过稳定前缀兼容
        分类，避免把普通成功文本误判为可重试失败。

        Args:
            messages: Planner 请求中的 OpenAI 消息历史，或最新
                ``before_request`` Hook 的 Context Item 快照列表。

        Returns:
            按 tool 消息出现顺序排列的 ``ViewToolObservation`` 列表。无法
            配对或结构异常的消息会被忽略；已配对的空字符串或纯空白结果
            会分类为 ``EMPTY_CONTENT_FAILURE``。
        """

        if not isinstance(messages, list):
            return []

        if PlannerHistoryParser._is_context_item_history(messages):
            return PlannerHistoryParser._extract_context_item_observations(messages)

        call_by_id: dict[str, _ViewToolCall] = {}
        observations: list[ViewToolObservation] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "").strip().lower()
            if role == "assistant":
                PlannerHistoryParser._record_calls(message, call_by_id)
            elif role == "tool":
                PlannerHistoryParser._record_result(message, call_by_id, observations)
        return observations

    @staticmethod
    def has_tool_result(messages: Any, call_id: str) -> bool:
        """判断当前 Planner 历史是否包含指定工具调用的结果。

        Args:
            messages: Planner 请求中的 OpenAI 消息历史，或最新 Context Item
                快照列表。
            call_id: 要查找的非空工具调用 ID。

        Returns:
            存在角色为 ``tool`` 且 ``tool_call_id`` 精确匹配的消息时返回
            ``True``；输入格式异常或结果不存在时返回 ``False``。
        """

        normalized_call_id = str(call_id or "").strip()
        if not normalized_call_id or not isinstance(messages, list):
            return False
        if PlannerHistoryParser._is_context_item_history(messages):
            return any(
                isinstance(item, dict)
                and item.get("item_type") == "FunctionCallOutputItem"
                and str(item.get("call_id") or "").strip() == normalized_call_id
                for item in messages
            )
        return any(
            isinstance(message, dict)
            and str(message.get("role") or "").strip().lower() == "tool"
            and str(message.get("tool_call_id") or "").strip() == normalized_call_id
            for message in messages
        )

    @staticmethod
    def _is_context_item_history(messages: list[Any]) -> bool:
        """判断列表是否使用最新 Context Item 快照格式。

        Args:
            messages: 待判断的历史列表。

        Returns:
            列表中至少存在带 ``item_type`` 字段的字典时返回 ``True``，否则
            按旧版 OpenAI 消息历史处理。
        """

        return any(isinstance(item, dict) and "item_type" in item for item in messages)

    @staticmethod
    def _extract_context_item_observations(items: list[Any]) -> list[ViewToolObservation]:
        """从最新 Context Item 快照中配对查看调用与结果。

        Args:
            items: ``before_request`` Hook 提供的 Context Item 快照列表。

        Returns:
            按 ``FunctionCallOutputItem`` 出现顺序排列的查看结果观察列表；
            无法精确配对或结构不完整的 Item 会被忽略。
        """

        call_by_id: dict[str, _ViewToolCall] = {}
        observations: list[ViewToolObservation] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("item_type")
            if item_type == "FunctionCallItem":
                PlannerHistoryParser._record_context_item_call(item, call_by_id)
            elif item_type == "FunctionCallOutputItem":
                PlannerHistoryParser._record_context_item_result(item, call_by_id, observations)
        return observations

    @staticmethod
    def _record_context_item_call(
        item: dict[str, Any],
        call_by_id: dict[str, _ViewToolCall],
    ) -> None:
        """记录最新 Context Item 中的查看函数调用。

        Args:
            item: 一个 ``FunctionCallItem`` 快照。
            call_by_id: 可变的调用 ID 到查看调用映射。
        """

        raw_call = item.get("tool_call")
        if not isinstance(raw_call, dict):
            return
        tool_name = str(raw_call.get("func_name") or "").strip()
        arguments = raw_call.get("args")
        call_id = str(raw_call.get("call_id") or "").strip()
        message_id = str(arguments.get("msg_id") or "").strip() if isinstance(arguments, dict) else ""
        if tool_name == VIEW_FORWARD_TOOL_NAME and call_id and message_id:
            call_by_id[call_id] = _ViewToolCall(
                message_id=message_id,
                path=PlannerHistoryParser._parse_view_path(arguments),
            )

    @staticmethod
    def _record_context_item_result(
        item: dict[str, Any],
        call_by_id: dict[str, _ViewToolCall],
        observations: list[ViewToolObservation],
    ) -> None:
        """将最新 Context Item 中的工具结果追加到观察列表。

        Args:
            item: 一个 ``FunctionCallOutputItem`` 快照。
            call_by_id: 已记录的调用 ID 到查看调用映射。
            observations: 接收已配对观察结果的可变列表。结构化状态为显式
                失败但文本无法分类时，结果会记录为 ``UNKNOWN_FAILURE``。
        """

        call_id = str(item.get("call_id") or "").strip()
        view_call = call_by_id.get(call_id)
        content = item.get("output")
        if view_call is None or not isinstance(content, str):
            return
        normalized_content = content.strip()
        observations.append(
            ViewToolObservation(
                call_id=call_id,
                message_id=view_call.message_id,
                content=normalized_content,
                kind=PlannerHistoryParser._classify_context_item_result(
                    normalized_content,
                    item.get("success"),
                ),
                path=view_call.path,
                nested_paths=PlannerHistoryParser._extract_nested_paths(normalized_content),
            )
        )

    @staticmethod
    def _classify_context_item_result(
        content: str,
        success: Any,
    ) -> ViewObservationKind:
        """优先根据 Context Item 的结构化状态分类查看结果。

        Args:
            content: 已去除首尾空白的 ToolResult 文本，允许为空。
            success: ``FunctionCallOutputItem`` 的结构化成功标志。布尔值以外
                的输入按缺少字段处理，以兼容非标准或更旧的快照。

        Returns:
            显式成功且内容非空时返回 ``SUCCESS``；显式失败会继续按稳定错误
            前缀细分，未匹配前缀时返回 ``UNKNOWN_FAILURE``。空内容始终返回
            ``EMPTY_CONTENT_FAILURE``；缺少结构化状态时沿用旧版文本分类。
        """

        if success is True:
            return ViewObservationKind.SUCCESS if content else ViewObservationKind.EMPTY_CONTENT_FAILURE
        if success is False:
            if not content:
                return ViewObservationKind.EMPTY_CONTENT_FAILURE
            classified = PlannerHistoryParser._classify_view_result(content)
            if classified is ViewObservationKind.SUCCESS:
                return ViewObservationKind.UNKNOWN_FAILURE
            return classified
        if not content:
            return ViewObservationKind.EMPTY_CONTENT_FAILURE
        return PlannerHistoryParser._classify_view_result(content)

    @staticmethod
    def _record_calls(message: dict[str, Any], call_by_id: dict[str, _ViewToolCall]) -> None:
        """记录一条 assistant 消息中的查看工具调用。

        兼容 ``function`` 嵌套定义和扁平工具定义。该方法会原地更新调用
        映射，不返回新字典。

        Args:
            message: 角色为 assistant 的 Planner 历史消息。
            call_by_id: 可变映射，键为工具调用 ID，值为已规范化查看调用。
        """

        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            return
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if isinstance(function, dict):
                tool_name = str(function.get("name") or "").strip()
                arguments = function.get("arguments")
            else:
                tool_name = str(tool_call.get("name") or "").strip()
                arguments = tool_call.get("arguments")
            if tool_name != VIEW_FORWARD_TOOL_NAME or not isinstance(arguments, dict):
                continue
            call_id = str(tool_call.get("id") or tool_call.get("call_id") or "").strip()
            message_id = str(arguments.get("msg_id") or "").strip()
            if call_id and message_id:
                call_by_id[call_id] = _ViewToolCall(
                    message_id=message_id,
                    path=PlannerHistoryParser._parse_view_path(arguments),
                )

    @staticmethod
    def _record_result(
        message: dict[str, Any],
        call_by_id: dict[str, _ViewToolCall],
        observations: list[ViewToolObservation],
    ) -> None:
        """将一条可配对的 tool 结果追加到结果列表。

        Args:
            message: 角色为 tool 的 Planner 历史消息。
            call_by_id: 已收集的调用 ID 到查看调用映射。
            observations: 接收已配对查看结果的可变列表。调用 ID 已知且
                ``content`` 为字符串时追加；空白文本按空内容失败记录。
        """

        call_id = str(message.get("tool_call_id") or "").strip()
        view_call = call_by_id.get(call_id)
        content = message.get("content")
        if view_call is None or not isinstance(content, str):
            return

        normalized_content = content.strip()
        observations.append(
            ViewToolObservation(
                call_id=call_id,
                message_id=view_call.message_id,
                content=normalized_content,
                kind=(
                    PlannerHistoryParser._classify_view_result(normalized_content)
                    if normalized_content
                    else ViewObservationKind.EMPTY_CONTENT_FAILURE
                ),
                path=view_call.path,
                nested_paths=PlannerHistoryParser._extract_nested_paths(normalized_content),
            )
        )

    @staticmethod
    def _parse_view_path(arguments: dict[str, Any]) -> tuple[int, ...] | None:
        """从查看工具参数中规范化可选嵌套路径。

        Args:
            arguments: ``view_forward_message`` 的调用参数。省略 ``path`` 时
                表示根层查看；格式无效时保留为不可用于完整性证明的 ``None``。

        Returns:
            根层调用返回空元组；有效非负整数数组返回对应元组；其他格式
            返回 ``None``，但不会影响失败 ToolResult 的配对和分类。
        """

        raw_path = arguments.get("path", [])
        if not isinstance(raw_path, list) or any(
            not isinstance(index, int) or isinstance(index, bool) or index < 0 for index in raw_path
        ):
            return None
        return tuple(raw_path)

    @staticmethod
    def _extract_nested_paths(content: str) -> tuple[tuple[int, ...], ...]:
        """提取 Host 浏览结果中尚可继续展开的嵌套路径。

        Args:
            content: ``view_forward_message`` 返回的已规范化文本。方法只识别
                当前 Host 生成的完整嵌套占位格式，其他 ``path`` 字样会忽略。

        Returns:
            按结果文本出现顺序去重后的路径元组；没有嵌套占位时返回空元组。
        """

        nested_paths: list[tuple[int, ...]] = []
        seen_paths: set[tuple[int, ...]] = set()
        for match in _NESTED_FORWARD_PATH_PATTERN.finditer(content):
            path = tuple(int(index.strip()) for index in match.group("indices").split(","))
            if path in seen_paths:
                continue
            seen_paths.add(path)
            nested_paths.append(path)
        return tuple(nested_paths)

    @staticmethod
    def _classify_view_result(content: str) -> ViewObservationKind:
        """根据当前 Host 的稳定文本前缀分类查看结果。

        Args:
            content: 已去除首尾空白的非空 ToolResult 文本。

        Returns:
            对应的 ``ViewObservationKind``。未匹配任何已知失败前缀时返回
            ``SUCCESS``，供缺少结构化状态的旧版消息历史兼容使用；显式失败
            的 Context Item 会由调用方把该默认值收紧为 ``UNKNOWN_FAILURE``。
        """

        if content.startswith(VIEW_FORWARD_CORRECTABLE_FAILURE_PREFIXES):
            return ViewObservationKind.CORRECTABLE_FAILURE
        if content.startswith(VIEW_FORWARD_TERMINAL_FAILURE_PREFIXES):
            return ViewObservationKind.TERMINAL_FAILURE
        if content.startswith(VIEW_FORWARD_RETRYABLE_FAILURE_PREFIXES):
            return ViewObservationKind.RETRYABLE_FAILURE
        if content.startswith(VIEW_FORWARD_EMPTY_CONTENT_FAILURE_PREFIXES):
            return ViewObservationKind.EMPTY_CONTENT_FAILURE
        if content.startswith(VIEW_FORWARD_UNKNOWN_FAILURE_PREFIXES):
            return ViewObservationKind.UNKNOWN_FAILURE
        return ViewObservationKind.SUCCESS
