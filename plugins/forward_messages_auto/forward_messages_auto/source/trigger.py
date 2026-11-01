"""source 合并转发消息的 Planner 强制触发服务。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from ..config import ForwardMessagesAutoConfig, GroupIdList
from ..core.capabilities import CapabilityResult
from ..core.messages import ForwardMessageParser
from ..core.state import ForwardStateStore
from .message import SourceForwardMessageParser, SourceForwardObservation
from .trigger_state import SourcePlannerTriggerStore

MESSAGE_READY_MAX_ATTEMPTS = 20
MESSAGE_READY_RETRY_SECONDS = 0.05


class SourcePlannerTriggerService:
    """在入站消息落库后为合并转发安排一次 source Planner 主动任务。"""

    def __init__(
        self,
        context: Any,
        config_provider: Callable[[], ForwardMessagesAutoConfig],
        forward_state: ForwardStateStore,
    ) -> None:
        """装配消息就绪检查、回声识别和永久防重依赖。

        Args:
            context: MaiBot ``PluginContext``，用于查询消息、触发 Planner、
                访问插件数据目录并记录日志。
            config_provider: 返回最新强类型配置的回调，使热更新可立即控制
                source 触发开关和白名单。
            forward_state: 已加载的转发状态，用于识别带最终目标消息 ID 的
                历史转发回声。
        """

        self._ctx = context
        self._config_provider = config_provider
        self._forward_state = forward_state
        self._store = SourcePlannerTriggerStore(
            context.paths.data_dir / "source_planner_trigger_state.json",
            context.logger,
        )
        self.background_tasks: set[asyncio.Task[Any]] = set()
        self._active_keys: set[str] = set()
        self._is_unloading = False

    async def start(self) -> None:
        """加载永久触发状态并允许新入站消息安排任务。"""

        await self._store.load()
        self._is_unloading = False

    async def stop(self) -> None:
        """停止接受新消息，取消待触发任务并保存最终防重状态。"""

        self._is_unloading = True
        tasks = list(self.background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.background_tasks.clear()
        self._active_keys.clear()
        await self._store.save()

    def observe(self, message: Any) -> bool:
        """筛选一条入站消息，并在满足条件时异步安排 Planner 触发。

        方法不会等待消息落库或 Planner 入队，因此观察型 Hook 不会延长
        入站主链。开关关闭、插件关闭、source 不匹配、消息格式不符、已知
        转发回声、正在处理或已经成功触发过的消息都会被忽略。

        Args:
            message: ``chat.receive.after_process`` Hook 提供的消息载荷。

        Returns:
            本次新建后台触发任务时返回 ``True``，否则返回 ``False``。

        Raises:
            RuntimeError: 当前没有运行中的 asyncio 事件循环时抛出。
        """

        config = self._config_provider()
        if self._is_unloading or not config.plugin.enabled or not config.behavior.trigger_source_planner:
            return False
        observation = SourceForwardMessageParser.parse(message)
        if observation is None:
            return False
        source_groups = set(GroupIdList.normalize(config.routing.source_groups))
        if observation.group_id not in source_groups:
            return False
        if self._forward_state.is_forwarded_target_message(observation.group_id, observation.message_id):
            self._ctx.logger.info(
                "忽略插件已发送的合并转发回声: source=%s msg_id=%s",
                observation.stream_id,
                observation.message_id,
            )
            return False
        trigger_key = self._store.build_key(observation.stream_id, observation.message_id)
        if trigger_key in self._active_keys or self._store.contains(trigger_key):
            return False
        self._active_keys.add(trigger_key)
        task = asyncio.create_task(
            self._trigger_when_ready(observation, trigger_key),
            name=f"source-forward-planner:{trigger_key.rsplit(':', 1)[-1]}",
        )
        self.background_tasks.add(task)
        task.add_done_callback(self._on_task_done)
        return True

    def _on_task_done(self, task: asyncio.Task[Any]) -> None:
        """回收完成任务，并记录未被任务主体处理的异常。

        Args:
            task: ``observe`` 创建且已经完成或取消的后台任务。
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
                "source Planner 触发任务异常结束: %s",
                exception,
                exc_info=exception,
            )

    async def _trigger_when_ready(
        self,
        observation: SourceForwardObservation,
        trigger_key: str,
    ) -> None:
        """等待真实消息可查询后触发 Planner，并在成功后永久防重。

        Hook 发生在 Host 入站消息落库和 Maisaka 注册之前。本方法通过受限
        重试等待同一 ``stream_id + msg_id`` 可查询，避免主动任务抢在真实
        消息进入聊天流之前运行。任何失败只记录日志，不会查看、转发或回复。

        Args:
            observation: 已通过入站环境、类型和来源校验的消息标识。
            trigger_key: 本次 source 消息的稳定永久防重键。
        """

        try:
            if not await self._wait_until_message_ready(observation):
                self._ctx.logger.warning(
                    "source 合并转发消息未在等待窗口内就绪，跳过 Planner 触发: source=%s msg_id=%s",
                    observation.stream_id,
                    observation.message_id,
                )
                return
            config = self._config_provider()
            if self._is_unloading or not config.plugin.enabled or not config.behavior.trigger_source_planner:
                return
            if observation.group_id not in set(GroupIdList.normalize(config.routing.source_groups)):
                return
            result = await self._ctx.maisaka.proactive.trigger(
                observation.stream_id,
                intent=(
                    f"本群刚收到一则真实合并转发消息，消息 ID 是 {observation.message_id}。"
                    "请结合本群当前上下文和你的人设，自主决定是否查看、判断或采取后续行动，如转发分享。"
                ),
                reason="source 白名单群收到合并转发消息",
                priority="normal",
                metadata={
                    "trigger_kind": "source_forward_message",
                    "source_message_id": observation.message_id,
                },
            )
            if not CapabilityResult.succeeded(result):
                self._ctx.logger.warning(
                    "source Planner 触发失败: source=%s msg_id=%s error=%s",
                    observation.stream_id,
                    observation.message_id,
                    CapabilityResult.error(result),
                )
                return
            await self._store.mark_triggered(trigger_key)
            self._ctx.logger.info(
                "source Planner 已触发: source=%s msg_id=%s",
                observation.stream_id,
                observation.message_id,
            )
        finally:
            self._active_keys.discard(trigger_key)

    async def _wait_until_message_ready(self, observation: SourceForwardObservation) -> bool:
        """轮询确认真实 source 消息已落库且仍属于同一聊天流。

        Args:
            observation: Hook 阶段识别出的 source 消息标识。

        Returns:
            在固定等待窗口内查询到同一聊天流的有效合并转发时返回 ``True``；
            插件卸载、开关关闭、消息始终不可见或结构变化时返回 ``False``。
        """

        for attempt in range(MESSAGE_READY_MAX_ATTEMPTS):
            if self._is_unloading or not self._config_provider().behavior.trigger_source_planner:
                return False
            try:
                result = await self._ctx.message.get_by_id(
                    observation.message_id,
                    stream_id=observation.stream_id,
                    include_binary_data=False,
                )
            except Exception as exc:
                self._ctx.logger.debug(
                    "等待 source 合并转发消息就绪时查询失败: source=%s msg_id=%s error=%s",
                    observation.stream_id,
                    observation.message_id,
                    exc,
                )
                result = None
            message = self._unwrap_message_result(result)
            if (
                isinstance(message, dict)
                and str(message.get("session_id") or "").strip() == observation.stream_id
                and ForwardMessageParser.extract(message) is not None
            ):
                return True
            if attempt + 1 < MESSAGE_READY_MAX_ATTEMPTS:
                await asyncio.sleep(MESSAGE_READY_RETRY_SECONDS)
        return False

    @staticmethod
    def _unwrap_message_result(result: Any) -> Any:
        """兼容当前 SDK 解包结果和旧版 Host 成功包装。

        Args:
            result: ``ctx.message.get_by_id`` 的任意返回值。

        Returns:
            旧包装含 ``message`` 时返回其值；否则原样返回当前 SDK 结果。
        """

        if isinstance(result, dict) and "message" in result and "success" in result:
            return result.get("message")
        return result
