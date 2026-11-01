"""source Planner 查看资格领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(slots=True)
class ViewEligibilityEntry:
    """当前 Planner 上下文中最后一次成功查看的文本及完整性。"""

    call_id: str
    content: str
    content_complete: bool


class ViewEligibilityStatus(str, Enum):
    """指定消息在当前 Planner 上下文中的查看资格状态。"""

    READY = "ready"
    MISSING = "missing"


class ViewObservationKind(str, Enum):
    """一次 view_forward_message 结果的语义分类。"""

    SUCCESS = "success"
    RETRYABLE_FAILURE = "retryable_failure"
    EMPTY_CONTENT_FAILURE = "empty_content_failure"
    CORRECTABLE_FAILURE = "correctable_failure"
    TERMINAL_FAILURE = "terminal_failure"
    UNKNOWN_FAILURE = "unknown_failure"


@dataclass(slots=True)
class ViewFailureState:
    """同一消息最近一段连续查看失败的分类状态。"""

    retryable_count: int = 0
    empty_content_count: int = 0
    last_kind: ViewObservationKind | None = None
    last_content: str = ""


@dataclass(frozen=True, slots=True)
class ViewEligibilityLookup:
    """一次当前上下文查看资格查询结果。"""

    status: ViewEligibilityStatus
    content: str
    content_complete: bool
    retryable_failure_count: int
    empty_content_failure_count: int
    last_observation_kind: ViewObservationKind | None
    last_failure_content: str
