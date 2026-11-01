"""跨群转发共享任务与投递领域对象。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any


class TargetStage(IntEnum):
    """单个目标群已经完成的处理阶段。"""

    PENDING = 0
    SENT = 1
    # 兼容 v0.1.15 及更早版本的持久化状态；新投递不再写入合成上下文。
    CONTEXT_APPENDED = 2
    PLANNER_QUEUED = 3

    @classmethod
    def from_value(cls, value: str) -> TargetStage:
        """将持久化阶段字符串转换为枚举值。

        Args:
            value: 阶段名称，不区分大小写，例如 ``"sent"``。未知值、
                空值或非字符串兼容值会降级为 ``PENDING``。

        Returns:
            对应的 ``TargetStage``；无法识别时返回 ``TargetStage.PENDING``。
        """

        try:
            return cls[value.upper()]
        except (KeyError, AttributeError):
            return cls.PENDING

    @property
    def storage_value(self) -> str:
        """返回适合写入 JSON 状态文件的小写阶段名称。

        Returns:
            当前枚举成员的小写名称，例如 ``TargetStage.SENT`` 返回
            ``"sent"``。
        """

        return self.name.lower()


@dataclass(slots=True)
class ForwardJob:
    """后台转发任务快照。"""

    job_id: str
    state_key: str
    source_stream_id: str
    source_group_id: str
    source_message_id: str
    target_group_ids: list[str]
    forward_messages: list[dict[str, Any]]
    sharing_reason: str


@dataclass(frozen=True, slots=True)
class TargetDeliveryResult:
    """单个目标群完成顺序处理后的真实结果。"""

    target_group_id: str
    stage: TargetStage
    success: bool
    failure_stage: str = ""
    error: str = ""


@dataclass(frozen=True, slots=True)
class ForwardDeliveryReport:
    """一次跨群转发任务覆盖全部目标群的聚合结果。"""

    job_id: str
    target_count: int
    target_results: tuple[TargetDeliveryResult, ...]

    @property
    def success(self) -> bool:
        """判断全部目标群是否都完成了当前要求的处理阶段。

        Returns:
            结果数量与目标数量一致且每个结果均成功时返回 ``True``。
        """

        return len(self.target_results) == self.target_count and all(result.success for result in self.target_results)

    @property
    def completed_target_count(self) -> int:
        """统计完成当前完整处理要求的目标群数量。

        Returns:
            ``success=True`` 的目标结果数量。
        """

        return sum(result.success for result in self.target_results)

    @property
    def sent_target_count(self) -> int:
        """统计已经确认完成物理发送的目标群数量。

        Returns:
            阶段不低于 ``TargetStage.SENT`` 的目标结果数量。
        """

        return sum(result.stage >= TargetStage.SENT for result in self.target_results)

    @property
    def failed_results(self) -> tuple[TargetDeliveryResult, ...]:
        """返回未完成当前完整处理要求的目标结果。

        Returns:
            保持配置顺序的失败目标结果元组。
        """

        return tuple(result for result in self.target_results if not result.success)
