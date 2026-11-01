"""后台顺序投递服务。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..config import ForwardMessagesAutoConfig
from ..core.capabilities import CapabilityResult
from ..core.models import (
    ForwardDeliveryReport,
    ForwardJob,
    TargetDeliveryResult,
    TargetStage,
)
from ..core.state import ForwardStateStore
from ..core.streams import GroupStreamRegistry


class ForwardDeliveryService:
    """按目标群顺序推进发送和 Planner 阶段。"""

    def __init__(
        self,
        context: Any,
        config_provider: Callable[[], ForwardMessagesAutoConfig],
        streams: GroupStreamRegistry,
        state: ForwardStateStore,
    ) -> None:
        """创建共享后台任务和阶段状态的顺序投递服务。

        Args:
            context: MaiBot ``PluginContext``，用于发送消息、触发 Planner
                和记录日志。
            config_provider: 返回最新插件配置的回调，用于读取是否触发目标群
                Planner。
            streams: 负责将 target QQ 群号解析为聊天流的注册表。
            state: 记录每个 target 已完成阶段的持久化存储。
        """

        self._ctx = context
        self._config_provider = config_provider
        self._streams = streams
        self._state = state
        self.background_tasks: set[asyncio.Task[Any]] = set()
        self._active_job_keys: set[str] = set()
        self._is_unloading = False

    def is_active(self, state_key: str) -> bool:
        """判断幂等键对应的任务是否仍在当前进程中执行。

        Args:
            state_key: 转发任务的稳定幂等键。

        Returns:
            任务已安排且尚未从 ``_run_job`` 退出时返回 ``True``。
        """

        return state_key in self._active_job_keys

    def schedule(self, job: ForwardJob) -> asyncio.Task[ForwardDeliveryReport]:
        """将一个已验证任务安排到当前事件循环后台执行。

        方法立即登记活动键并创建具名 ``asyncio.Task``。调用方可以等待返回
        的任务取得全部 target 的真实处理报告；任务结束时回调负责回收引用
        和记录未处理异常。

        Args:
            job: 已完成权限、消息类型和幂等校验的转发任务快照。

        Returns:
            正在执行顺序投递并最终返回 ``ForwardDeliveryReport`` 的任务。

        Raises:
            RuntimeError: 当调用时没有正在运行的 asyncio 事件循环。
        """

        self._active_job_keys.add(job.state_key)
        task = asyncio.create_task(
            self._run_job(job),
            name=f"cross-group-forward:{job.job_id}",
        )
        self.background_tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return task

    async def stop(self) -> None:
        """进入卸载状态，取消并等待全部后台任务。

        取消结果通过 ``return_exceptions=True`` 收集，因此单个任务的取消或
        清理异常不会阻止其他任务结束。完成后任务集合为空。
        """

        self._is_unloading = True
        tasks = list(self.background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.background_tasks.clear()

    def resume(self) -> None:
        """退出卸载状态，允许新任务继续逐目标处理。

        该方法不恢复已经被取消的 ``asyncio.Task``；未完成阶段依靠后续同一
        请求结合持久化状态重新创建任务。
        """

        self._is_unloading = False

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        """回收完成任务，并记录未被任务内部处理的异常。

        Args:
            task: ``schedule`` 创建且已经进入完成状态的后台任务。

        Note:
            正常完成和取消不会写错误日志；只有 ``task.exception()`` 返回
            非空异常时记录堆栈。
        """

        self.background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return
        if exception is not None:
            self._ctx.logger.error(
                "跨群转发后台任务异常结束: %s",
                exception,
                exc_info=exception,
            )

    async def _run_job(self, job: ForwardJob) -> ForwardDeliveryReport:
        """按照任务快照中的 target 顺序执行完整投递。

        开始时确保任务状态已持久化。单个 target 的普通异常会记录后继续
        下一个 target，并进入聚合报告；任务取消会向上传播；无论如何退出
        都会移除活动键。

        Args:
            job: 包含 source、目标顺序、原始节点和展开内容的任务快照。

        Returns:
            保持 target 配置顺序的真实投递聚合报告。

        Raises:
            asyncio.CancelledError: 插件卸载取消任务时向上保留取消语义。
        """

        target_results: list[TargetDeliveryResult] = []
        try:
            await self._state.ensure_job(job)
            for target_group_id in job.target_group_ids:
                if self._is_unloading:
                    break
                try:
                    target_results.append(await self._process_target(job, target_group_id))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._ctx.logger.error(
                        "处理目标群失败: job=%s target=%s error=%s",
                        job.job_id,
                        target_group_id,
                        exc,
                        exc_info=True,
                    )
                    target_results.append(
                        TargetDeliveryResult(
                            target_group_id=target_group_id,
                            stage=self._state.get_target_stage(job.state_key, target_group_id),
                            success=False,
                            failure_stage="处理目标群",
                            error=str(exc),
                        )
                    )
            return ForwardDeliveryReport(
                job_id=job.job_id,
                target_count=len(job.target_group_ids),
                target_results=tuple(target_results),
            )
        finally:
            self._active_job_keys.discard(job.state_key)

    async def _process_target(
        self,
        job: ForwardJob,
        target_group_id: str,
    ) -> TargetDeliveryResult:
        """从已持久化阶段继续处理单个目标群。

        方法依次解析聊天流、发送合并转发，并按配置触发目标群 Planner。
        任一步骤返回失败时停止当前 target，不推进后续阶段；外层任务仍会
        继续下一个 target。

        Args:
            job: 当前转发任务快照。
            target_group_id: 当前处理的 target QQ 群号。

        Returns:
            包含最终阶段和失败位置的目标群真实处理结果。
        """

        target_stream_id = await self._streams.resolve(target_group_id)
        if not target_stream_id:
            self._ctx.logger.warning(
                "无法解析 target 群聊流: job=%s target=%s",
                job.job_id,
                target_group_id,
            )
            return TargetDeliveryResult(
                target_group_id=target_group_id,
                stage=TargetStage.PENDING,
                success=False,
                failure_stage="解析目标群",
                error="无法解析 target 群聊流",
            )

        stage = self._state.get_target_stage(job.state_key, target_group_id)
        target_message_id = self._state.get_target_message_id(job.state_key, target_group_id)
        if stage < TargetStage.SENT:
            error, target_message_id = await self._send_forward(job, target_group_id, target_stream_id)
            if error is not None:
                return TargetDeliveryResult(
                    target_group_id=target_group_id,
                    stage=stage,
                    success=False,
                    failure_stage="发送",
                    error=error,
                )
            stage = TargetStage.SENT

        if not self._config_provider().behavior.trigger_target_planner:
            return TargetDeliveryResult(
                target_group_id=target_group_id,
                stage=stage,
                success=True,
            )
        if stage < TargetStage.PLANNER_QUEUED:
            error = await self._trigger_planner(
                job,
                target_group_id,
                target_stream_id,
                target_message_id,
            )
            if error is not None:
                return TargetDeliveryResult(
                    target_group_id=target_group_id,
                    stage=stage,
                    success=False,
                    failure_stage="触发目标群 Planner",
                    error=error,
                )
            stage = TargetStage.PLANNER_QUEUED
        return TargetDeliveryResult(
            target_group_id=target_group_id,
            stage=stage,
            success=True,
        )

    async def _send_forward(
        self,
        job: ForwardJob,
        target_group_id: str,
        target_stream_id: str,
    ) -> tuple[str | None, str | None]:
        """将原始合并转发节点发送到目标聊天流。

        发送时请求 Host 返回平台最终目标消息 ID，并开启 Maisaka 历史同步。
        新版 Host 返回的 ID 会和 ``SENT`` 阶段原子持久化；旧版 Host 仍可按
        布尔结果完成发送，但不会产生回复锚点。发送成功却缺少 ID 时会记录
        warning，明确表示后续启用安全 fallback。插件不重复注入源群展开内容。

        Args:
            job: 提供发送节点、任务 ID 和来源元数据的任务快照。
            target_group_id: 用于日志和阶段状态的目标 QQ 群号。
            target_stream_id: 已解析的目标 MaiBot 聊天流 ID。

        Returns:
            二元组第一项为失败原因，第二项为平台最终目标消息 ID。发送成功
            时第一项为 ``None``；旧 Host 成功但未返回 ID 时第二项为
            ``None``。
        """

        result = await self._call_forward_with_details(job, target_stream_id)
        if not CapabilityResult.succeeded(result):
            error = CapabilityResult.error(result)
            self._ctx.logger.warning(
                "合并转发发送失败: job=%s target=%s error=%s",
                job.job_id,
                target_group_id,
                error,
            )
            return error, None
        target_message_id = CapabilityResult.message_id(result)
        if target_message_id is None:
            self._ctx.logger.warning(
                "send.forward 未返回目标消息 ID，启用安全 fallback: job=%s target=%s result_type=%s",
                job.job_id,
                target_group_id,
                type(result).__name__,
            )
        await self._state.record_target_sent(job, target_group_id, target_message_id)
        return None, target_message_id

    async def _call_forward_with_details(
        self,
        job: ForwardJob,
        target_stream_id: str,
    ) -> Any:
        """调用 ``send.forward`` 并尽可能保留 Host 返回的详细发送结果。

        当前 MaiBot Host 在 ``return_details=True`` 时会返回 ``sent`` 和最终
        ``message_id``，但部分 SDK 版本会因顶层 ``success`` 字段把该结果归一
        化为布尔值。SDK 已允许插件通过受限的 ``cap.call`` 原始通道调用已声明
        capability；此处优先使用该通道，以免在 SDK 归一化前丢失最终 ID。旧 SDK
        未提供该方法时才使用普通 ``send.forward`` 代理。原始调用一旦抛出异常
        不会再回退重发，避免投递状态未知时产生重复消息。

        Args:
            job: 提供原始转发节点、处理后纯文本和任务来源标识的转发任务快照。
            target_stream_id: 已解析的目标 MaiBot 聊天流 ID。

        Returns:
            Host 未经 SDK 布尔归一化的详细结果，或旧 SDK 普通发送代理的兼容
            返回值。
        """

        send_args = {
            "messages": job.forward_messages,
            "stream_id": target_stream_id,
            "processed_plain_text": "[跨群分享的合并转发消息]",
            "storage_message": True,
            "sync_to_maisaka_history": True,
            "maisaka_source_kind": f"cross_group_forward:{job.job_id}",
            "return_details": True,
        }
        raw_host_call = getattr(self._ctx, "call_host_method", None)
        if callable(raw_host_call):
            return await raw_host_call(
                "cap.call",
                payload={
                    "capability": "send.forward",
                    "args": send_args,
                },
            )
        return await self._ctx.send.forward(
            job.forward_messages,
            target_stream_id,
            processed_plain_text="[跨群分享的合并转发消息]",
            storage_message=True,
            sync_to_maisaka_history=True,
            maisaka_source_kind=f"cross_group_forward:{job.job_id}",
            return_details=True,
        )

    async def _trigger_planner(
        self,
        job: ForwardJob,
        target_group_id: str,
        target_stream_id: str,
        target_message_id: str | None,
    ) -> str | None:
        """强制触发目标群 Planner 自主决定评论或沉默。

        意图要求 Planner 结合本群语境自主判断，而非机械复述。新版 Host
        返回目标消息 ID 时，插件把该 ID 作为唯一 ``reply`` 锚点；旧 Host
        没有返回 ID 时保留按真实历史定位并在不确定时沉默的兼容语义。主动
        任务不暴露 source 消息 ID，避免跨聊天流误用。

        Args:
            job: 提供分享理由和任务 ID 的任务快照。
            target_group_id: 用于日志和阶段状态的目标 QQ 群号。
            target_stream_id: 要触发主动任务的目标聊天流 ID。
            target_message_id: 平台最终目标消息 ID；旧 Host 未提供时为
                ``None``。

        Returns:
            Planner 入队并记录阶段成功时返回 ``None``；capability 失败时
            返回当前兼容解析器生成的错误文本。
        """

        if target_message_id:
            reply_instruction = f"这则真实消息的目标消息 ID (msg_id) 是 {target_message_id}；如果决定发表看法，调用 reply 工具进行回复时只能使用这个 ID。"
        else:
            reply_instruction = (
                "如果当前上下文中能可靠定位由你刚刚实际发送的那则合并转发消息，并且你决定发表看法，"
                "调用 reply 工具进行回复时只能选择该真实消息的 msg_id。无法可靠定位时请保持沉默。"
            )
        intent = (
            "你刚刚把一则来自其他群聊的合并转发分享到了本群。"
            "请结合本群近期聊天、群友关系、记忆和你的表达习惯，自主决定是否发表一句自然的整体看法；"
            f"{reply_instruction}"
            "如果没有合适或有价值的话可说，就保持沉默。不要机械复述消息，也不要暴露插件内部流程。"
        )
        metadata = {"job_id": job.job_id}
        if target_message_id:
            metadata["target_message_id"] = target_message_id
        result = await self._ctx.maisaka.proactive.trigger(
            target_stream_id,
            intent=intent,
            reason=job.sharing_reason or "源群 Planner 判断这则内容值得分享",
            priority="normal",
            metadata=metadata,
        )
        if not CapabilityResult.succeeded(result):
            error = CapabilityResult.error(result)
            self._ctx.logger.warning(
                "目标群 Planner 触发失败: job=%s target=%s error=%s",
                job.job_id,
                target_group_id,
                error,
            )
            return error
        await self._state.advance_target(
            job,
            target_group_id,
            TargetStage.PLANNER_QUEUED,
        )
        self._ctx.logger.info(
            "目标群处理完成: job=%s target=%s",
            job.job_id,
            target_group_id,
        )
        return None
