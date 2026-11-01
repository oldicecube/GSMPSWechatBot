"""当前 Planner 上下文中的 view_forward_message 查看资格。"""

from __future__ import annotations

from .models import (
    ViewEligibilityEntry,
    ViewEligibilityLookup,
    ViewEligibilityStatus,
    ViewFailureState,
    ViewObservationKind,
)
from .history import ViewToolObservation


class ViewEligibilityStore:
    """按聊天流同步当前 Planner 上下文中的查看资格。"""

    def __init__(self) -> None:
        """创建空的进程内上下文资格索引。

        索引不持久化，也不记录时间。每次 Planner ``before_request`` 都会
        用该聊天流当前可见的完整历史替换旧快照，因此索引不能让已经被
        上下文裁剪的查看结果继续有效。
        """

        self._entries: dict[tuple[str, str], ViewEligibilityEntry] = {}
        self._failure_states: dict[tuple[str, str], ViewFailureState] = {}
        self._processed_calls_by_stream: dict[str, set[str]] = {}

    def sync_context(
        self,
        stream_id: str,
        observations: list[ViewToolObservation],
    ) -> list[str]:
        """用当前 Planner 历史完整替换一个聊天流的查看资格。

        任一仍可见的成功结果都会使对应 ``msg_id`` 合法，并使用最后一次
        成功结果的文本；同时根据根层和已发现嵌套路径的成功结果计算完整性。
        仅在没有成功结果时才根据最后一段连续失败计算降级状态。旧快照中
        存在但本次上下文已不可见的消息会立即移除。

        Args:
            stream_id: 当前 source Planner 的聊天流 ID。
            observations: 从本轮 Planner 消息历史按顺序解析出的查看结果。

        Returns:
            本轮首次见到成功 ``tool_call_id`` 的 ``msg_id`` 列表，供运行时
            生成一次性立即判断提醒。
        """

        self._remove_stream_snapshot(stream_id)
        previous_context_calls = self._processed_calls_by_stream.get(
            stream_id,
            set(),
        )
        grouped: dict[str, list[ViewToolObservation]] = {}
        fresh_success_ids: list[str] = []
        seen_fresh_message_ids: set[str] = set()
        seen_context_calls: set[str] = set()

        for observation in observations:
            if observation.call_id in seen_context_calls:
                continue
            seen_context_calls.add(observation.call_id)
            grouped.setdefault(observation.message_id, []).append(observation)
            is_fresh = observation.call_id not in previous_context_calls
            if (
                is_fresh
                and observation.kind is ViewObservationKind.SUCCESS
                and observation.message_id not in seen_fresh_message_ids
            ):
                fresh_success_ids.append(observation.message_id)
                seen_fresh_message_ids.add(observation.message_id)

        if seen_context_calls:
            self._processed_calls_by_stream[stream_id] = seen_context_calls
        else:
            self._processed_calls_by_stream.pop(stream_id, None)

        for message_id, message_observations in grouped.items():
            key = (stream_id, message_id)
            successful = [
                observation for observation in message_observations if observation.kind is ViewObservationKind.SUCCESS
            ]
            if successful:
                latest_success = successful[-1]
                self._entries[key] = ViewEligibilityEntry(
                    call_id=latest_success.call_id,
                    content=latest_success.content,
                    content_complete=self._content_is_complete(successful),
                )
                continue
            self._failure_states[key] = self._build_failure_state(message_observations)

        return fresh_success_ids

    def lookup(self, stream_id: str, message_id: str) -> ViewEligibilityLookup:
        """查询指定消息在当前 Planner 上下文中的查看资格。

        Args:
            stream_id: source Planner 当前聊天流 ID。
            message_id: 要查询的合并转发消息 ID。

        Returns:
            ``ViewEligibilityLookup``。当前上下文仍含成功查看结果时返回
            ``READY``、最后一次结果文本和完整性状态，否则返回 ``MISSING``
            及可见失败状态。
        """

        key = (stream_id, message_id)
        entry = self._entries.get(key)
        failure_state = self._failure_states.get(key, ViewFailureState())
        return ViewEligibilityLookup(
            status=ViewEligibilityStatus.READY if entry is not None else ViewEligibilityStatus.MISSING,
            content=entry.content if entry is not None else "",
            content_complete=entry.content_complete if entry is not None else False,
            retryable_failure_count=failure_state.retryable_count,
            empty_content_failure_count=failure_state.empty_content_count,
            last_observation_kind=failure_state.last_kind,
            last_failure_content=failure_state.last_content,
        )

    @staticmethod
    def _content_is_complete(observations: list[ViewToolObservation]) -> bool:
        """判断当前可见成功结果是否覆盖全部已发现嵌套路径。

        完整性要求根层成功结果仍在当前 Planner 上下文中，并且每个成功
        结果暴露的嵌套 ``path`` 都存在同一消息的成功查看结果。路径结果还
        可以继续暴露更深路径，因此对当前观察集合求闭包即可覆盖任意深度。

        Args:
            observations: 同一 ``msg_id`` 的成功查看观察，按上下文顺序排列。

        Returns:
            根层和全部已发现路径都有成功结果时返回 ``True``；缺少根层、
            存在未查看路径或调用路径无效时返回 ``False``。
        """

        if any(observation.path is None for observation in observations):
            return False
        successful_paths = {observation.path for observation in observations}
        if () not in successful_paths:
            return False
        required_paths = {nested_path for observation in observations for nested_path in observation.nested_paths}
        return required_paths.issubset(successful_paths)

    def _remove_stream_snapshot(self, stream_id: str) -> None:
        """移除一个聊天流上一轮派生出的全部资格和失败状态。

        Args:
            stream_id: 即将由本轮 Planner 上下文重新同步的聊天流 ID。
        """

        self._entries = {key: entry for key, entry in self._entries.items() if key[0] != stream_id}
        self._failure_states = {key: state for key, state in self._failure_states.items() if key[0] != stream_id}

    @staticmethod
    def _build_failure_state(
        observations: list[ViewToolObservation],
    ) -> ViewFailureState:
        """从同一消息的可见失败结果计算最后一段连续状态。

        Args:
            observations: 同一 ``msg_id`` 按上下文顺序排列的失败观察。

        Returns:
            最近失败的分类和文本。最近分类为可重试故障或空内容时，还会
            携带从列表末尾向前计算的同类连续次数。
        """

        latest = observations[-1]
        state = ViewFailureState(
            last_kind=latest.kind,
            last_content=latest.content,
        )
        if latest.kind is ViewObservationKind.RETRYABLE_FAILURE:
            state.retryable_count = ViewEligibilityStore._count_trailing_kind(
                observations,
                latest.kind,
            )
        elif latest.kind is ViewObservationKind.EMPTY_CONTENT_FAILURE:
            state.empty_content_count = ViewEligibilityStore._count_trailing_kind(
                observations,
                latest.kind,
            )
        return state

    @staticmethod
    def _count_trailing_kind(
        observations: list[ViewToolObservation],
        kind: ViewObservationKind,
    ) -> int:
        """统计观察列表末尾连续出现指定分类的次数。

        Args:
            observations: 同一消息按上下文顺序排列的失败观察。
            kind: 要从列表末尾连续匹配的失败分类。

        Returns:
            末尾连续匹配数量；调用方传入末项分类时至少为 ``1``。
        """

        count = 0
        for observation in reversed(observations):
            if observation.kind is not kind:
                break
            count += 1
        return count
