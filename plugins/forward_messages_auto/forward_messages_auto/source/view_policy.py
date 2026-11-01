"""source 查看重试与 fallback 判定策略。"""

from __future__ import annotations

from .models import ViewEligibilityLookup, ViewEligibilityStatus, ViewObservationKind

EMPTY_CONTENT_FALLBACK_THRESHOLD = 2


def needs_additional_view(
    lookup: ViewEligibilityLookup,
    retryable_failure_threshold: int,
) -> bool:
    """判断当前查看状态是否需要系统继续调用查看工具。

    Args:
        lookup: 当前 Planner 上下文中的查看资格及连续失败统计。
        retryable_failure_threshold: 可重试故障允许 fallback 前的连续次数。

    Returns:
        已成功查看、达到对应 fallback 阈值或出现不可自动重试失败时返回
        ``False``；完全缺失、可重试故障未达阈值或首次空内容返回 ``True``。
    """

    if lookup.status is ViewEligibilityStatus.READY:
        return False

    observation_kind = lookup.last_observation_kind
    if observation_kind is None:
        return True
    if observation_kind is ViewObservationKind.RETRYABLE_FAILURE:
        return lookup.retryable_failure_count < retryable_failure_threshold
    if observation_kind is ViewObservationKind.EMPTY_CONTENT_FAILURE:
        return lookup.empty_content_failure_count < EMPTY_CONTENT_FALLBACK_THRESHOLD
    return False
