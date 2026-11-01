"""跨群转发运行时装配。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import PLUGIN_VERSION, ForwardMessagesAutoConfig, GroupIdList
from .debug_stats import (
    DEBUG_STATS_FILENAME,
    DebugForwardStatsStore,
)
from .core.state import ForwardStateStore
from .core.streams import GroupStreamRegistry
from .request import ForwardRequestService
from .source.authorization import ForwardInvocationGate
from .source.history import PlannerHistoryParser
from .source.models import ViewEligibilityStatus
from .source.trigger import SourcePlannerTriggerService
from .source.output_items import serialize_output_item_tool_calls
from .source.view_flow import ViewBeforeForwardCoordinator
from .source.view_state import ViewEligibilityStore
from .target.delivery import ForwardDeliveryService


class ForwardingRuntime:
    """装配插件服务并协调生命周期和 Planner Hook。"""

    def __init__(
        self,
        context: Any,
        config_provider: Callable[[], ForwardMessagesAutoConfig],
    ) -> None:
        """创建一次插件生命周期内共享的服务对象。

        该构造方法只完成依赖装配，不执行磁盘或网络 I/O。状态加载和聊天流
        索引刷新由 ``start`` 负责。

        Args:
            context: MaiBot 注入的 ``PluginContext``，供各服务访问 capability、
                插件数据目录和日志。
            config_provider: 返回当前强类型插件配置的无参数回调。使用回调而非
                固定快照，使热更新后各服务可以立即读取新配置。
        """

        self._config_provider = config_provider
        self.view_eligibility = ViewEligibilityStore()
        self._pending_view_judgments: dict[str, dict[str, bool]] = {}
        self.streams = GroupStreamRegistry(context)
        self.state = ForwardStateStore(
            context.paths.data_dir / "forward_state.json",
            context.logger,
        )
        self.debug_stats = DebugForwardStatsStore(
            Path(context.paths.data_dir) / DEBUG_STATS_FILENAME,
            Path(__file__).resolve().parents[1],
            PLUGIN_VERSION,
            context.logger,
        )
        self.source_trigger = SourcePlannerTriggerService(
            context,
            config_provider,
            self.state,
        )
        self.delivery = ForwardDeliveryService(
            context,
            config_provider,
            self.streams,
            self.state,
        )
        self.invocation_gate = ForwardInvocationGate()
        self.requests = ForwardRequestService(
            context,
            config_provider,
            self.view_eligibility,
            self.state,
            self.delivery,
            self.streams.resolve_group,
        )
        self.view_before_forward = ViewBeforeForwardCoordinator(
            config_provider,
            self.view_eligibility,
            self.invocation_gate,
            self.requests.can_auto_view,
            context.logger,
        )

    @property
    def config(self) -> ForwardMessagesAutoConfig:
        """读取当前生效的强类型插件配置。

        Returns:
            ``config_provider`` 当前返回的 ``ForwardMessagesAutoConfig``。
        """

        return self._config_provider()

    @property
    def background_tasks(self) -> set[Any]:
        """暴露尚未回收的后台投递任务集合。

        该属性主要用于生命周期清理和测试等待，不应由调用方直接增删。

        Returns:
            投递与 source 触发服务维护的短生命周期 ``asyncio.Task`` 集合。
            调试统计的常驻 writer 不包含在内，避免测试等待永不结束。
        """

        return self.delivery.background_tasks | self.source_trigger.background_tasks

    def source_groups(self) -> list[str]:
        """返回规范化后的 source QQ 群白名单。

        Returns:
            去空、去重并保持配置顺序的 source 群号字符串列表。
        """

        return GroupIdList.normalize(self.config.routing.source_groups)

    def target_groups(self) -> list[str]:
        """返回规范化后的 target QQ 群白名单。

        Returns:
            去空、去重并保持配置顺序的 target 群号字符串列表；该顺序也是
            后台投递顺序。
        """

        return GroupIdList.normalize(self.config.routing.target_groups)

    async def start(self) -> None:
        """启动运行时并准备处理 Planner 请求。

        启动顺序为恢复投递服务、加载永久防重状态和 source Planner 触发状态，
        最后按开发开关初始化本地调试统计。这样后续启动步骤失败时不会遗留统计
        writer。目标聊天流只在实际投递时按群号解析，避免插件加载与 Host 聊天
        管理器初始化之间产生时序依赖。
        """

        self.delivery.resume()
        await self.state.load()
        await self.source_trigger.start()
        await self.debug_stats.start()

    async def stop(self) -> None:
        """停止后台投递并保存最终状态。

        方法先取消并等待 source 与投递任务，再持久化阶段快照，最后等待
        调试统计完成已接受事件的原子保存，保证卸载后没有遗留任务继续调用
        Host capability 或写入统计。
        """

        self.cancel_pending_direct_forwards()
        try:
            await self.source_trigger.stop()
            await self.delivery.stop()
            await self.state.save()
        finally:
            await self.debug_stats.stop()

    def observe_source_message(self, message: Any) -> bool:
        """统计合格 source 消息，再交给 Planner 触发服务独立筛选。

        调试统计只在环境变量显式开启且插件启用时登记 source 白名单中的
        唯一外部合并转发，并排除已经持久化的插件转发回声；该过程不依赖
        ``trigger_source_planner``。随后无论统计结果如何，都按原路径调用
        source 触发服务。

        Args:
            message: ``chat.receive.after_process`` 提供的序列化消息载荷。

        Returns:
            source 触发服务新建后台 Planner 任务时返回 ``True``；统计是否
            新增不改变该返回值。
        """

        config = self.config
        if config.plugin.enabled:
            self.debug_stats.record_source_message(
                message,
                GroupIdList.normalize(config.routing.source_groups),
                self.state.is_forwarded_target_message,
            )
        return self.source_trigger.observe(message)

    def capture_view_results(self, session_id: str, history_items: Any) -> list[str]:
        """按 Planner 当前历史同步查看资格并登记新的成功结果。

        每轮都用当前消息或 Context Item 历史替换该聊天流的旧快照，因此被
        上下文裁剪的查看结果立即失效。仍可见的成功结果持续合法且不按时间
        过期；失败结果按可重试、空内容、可修正、终止和未知类型重新计算。
        每个成功 ``tool_call_id`` 只会首次登记为等待 Planner 立即判断的消息。

        Args:
            session_id: source Planner 当前聊天流 ID。
            history_items: Planner 请求携带的旧版 OpenAI 消息历史，或最新
                ``before_request`` Hook 的 Context Item 快照列表。

        Returns:
            本次首次捕获成功的 ``msg_id`` 列表。调用方可据此判断是否需要
            向紧接着的 Planner 请求追加一次性续轮提醒。
        """

        observations = PlannerHistoryParser.extract_view_observations(history_items)
        fresh_message_ids = self.view_eligibility.sync_context(
            session_id,
            observations,
        )
        auto_view_message_ids = self.view_before_forward.capture_context(
            session_id,
            history_items,
            observations,
        )
        manual_message_ids = [message_id for message_id in fresh_message_ids if message_id not in auto_view_message_ids]
        pending_judgments = self._pending_view_judgments.get(session_id)
        if pending_judgments:
            for message_id in pending_judgments:
                lookup = self.view_eligibility.lookup(session_id, message_id)
                if lookup.status is ViewEligibilityStatus.READY:
                    pending_judgments[message_id] = lookup.content_complete
        if manual_message_ids:
            pending_judgments = self._pending_view_judgments.setdefault(session_id, {})
            for message_id in manual_message_ids:
                pending_judgments[message_id] = self.view_eligibility.lookup(
                    session_id,
                    message_id,
                ).content_complete
        return fresh_message_ids

    async def transform_after_response(
        self,
        session_id: str,
        response: Any,
        tool_calls: Any,
        authorization_round: str,
    ) -> tuple[str, Any]:
        """编排直接请求的自动查看，并确认普通查看后的判断提醒。

        Args:
            session_id: 当前 source Planner 聊天流 ID。
            response: 模型原始文本响应。
            tool_calls: Host 序列化后的模型工具调用列表。
            authorization_round: EARLY Hook 在同一分发链生成的随机标记。

        Returns:
            协调器处理后的响应文本与工具调用。路径 A 和未接管响应保持原样；
            路径 B 会只返回一个系统查看或恢复后的转发调用。
        """

        transformed = await self.view_before_forward.transform_after_response(
            session_id,
            response,
            tool_calls,
            authorization_round,
        )
        self.acknowledge_view_judgment(session_id)
        return transformed

    async def transform_after_output_items(
        self,
        session_id: str,
        output_items: Any,
        authorization_round: str,
    ) -> Any:
        """编排最新 Planner 输出 Item，并确认普通查看后的判断提醒。

        协调器成功处理一次正常 Planner 响应后，会清除该 session 尚待消费的
        主动查看判断提醒；若处理被异常中断，则保留提醒供下一次请求恢复。

        Args:
            session_id: 当前 Planner Hook 提供的真实聊天流 ID。
            output_items: ``maisaka.planner.after_response`` 提供的 Context Item
                快照列表。
            authorization_round: EARLY Hook 生成的当前分发链随机标记。

        Returns:
            已清洗或按路径 B 替换后的 Context Item 快照列表。
        """

        transformed = await self.view_before_forward.transform_after_output_items(
            session_id,
            output_items,
            authorization_round,
        )
        self.acknowledge_view_judgment(session_id)
        return transformed

    def authorize_forward_calls(self, session_id: str, tool_calls: Any) -> tuple[Any, str]:
        """清洗转发参数并绑定真实 ``after_response`` 会话。

        该步骤不执行 capability 或文件 I/O，供 EARLY Hook 在自动查看编排前
        独立完成。即使后续 Hook 超时，正式 Tool handler 也只接受这里签发的
        一次性凭据；缺少凭据的原始模型调用会被拒绝。显式开启调试统计时，
        方法还会把已观察消息的原始 Planner 请求加入内存去重集合；磁盘保存
        由常驻 worker 在 Hook 让出控制后执行。

        Args:
            session_id: Host Hook 提供的真实 Planner 会话 ID。
            tool_calls: Host 序列化后的模型工具调用列表。

        Returns:
            二元组包含清除未声明字段并加入一次性凭据的工具调用，以及供
            LATE Hook 验证本次分发链的随机标记。
        """

        sanitized_calls, authorization_round = self.invocation_gate.sanitize_and_authorize(
            session_id,
            tool_calls,
        )
        self.debug_stats.record_requested_calls(session_id, sanitized_calls)
        return sanitized_calls, authorization_round

    def authorize_forward_output_items(self, session_id: str, output_items: Any) -> tuple[Any, str]:
        """清洗最新 Planner 输出 Item 并绑定真实会话授权。

        Args:
            session_id: Host Hook 提供的真实 Planner 会话 ID。
            output_items: Host 序列化的 ``FunctionCallItem`` 快照列表。

        Returns:
            二元组包含清洗后的 Context Item 快照和当前 Hook 分发链随机标记。
            调试统计使用旧版调用字典作为内部兼容投影，但不会改变返回协议。
        """

        sanitized_items, authorization_round = self.invocation_gate.sanitize_and_authorize_output_items(
            session_id,
            output_items,
        )
        self.debug_stats.record_requested_calls(
            session_id,
            serialize_output_item_tool_calls(sanitized_items),
        )
        return sanitized_items, authorization_round

    def cancel_pending_direct_forwards(self) -> None:
        """取消尚未发送的路径 B 续接请求。

        配置热更新和插件卸载都会调用该方法，避免旧分享授权跨越配置边界后
        在未来普通 Planner 响应中复活。已经开始的真实投递不受影响。
        """

        self.view_before_forward.clear()
        self.invocation_gate.clear()

    def build_view_judgment_reminder(self, session_id: str) -> str:
        """按嵌套查看完整性构造等待 Planner 消费的续轮判断提醒。

        提醒在对应 Planner 请求成功返回前保持待处理，因此请求若被新消息
        打断，下一次重试仍会收到相同提醒。已发现路径尚未全部查看时提供
        可选继续查看说明，全部覆盖后才声明完整；方法只读取状态，不清除
        待处理项。

        Args:
            session_id: 当前 source Planner 聊天流 ID。

        Returns:
            有待判断消息时返回一个 ``system-reminder`` 文本，否则返回空串。
        """

        pending_judgments = self._pending_view_judgments.get(session_id, {})
        if not pending_judgments:
            return ""
        complete_message_ids = "、".join(
            sorted(message_id for message_id, is_complete in pending_judgments.items() if is_complete)
        )
        nested_message_ids = "、".join(
            sorted(message_id for message_id, is_complete in pending_judgments.items() if not is_complete)
        )
        if complete_message_ids and nested_message_ids:
            return (
                "<system-reminder>\n"
                f"刚刚已经成功查看合并转发消息 msg_id={complete_message_ids} 的完整内容；"
                f"也已经查看合并转发消息 msg_id={nested_message_ids} 的当前层内容，但后者仍有尚未展开的嵌套转发消息。"
                "对于仍有嵌套的消息，如果继续查看有助于判断，建议使用刚看到的内容中给出的 path 再次调用 "
                "view_forward_message；你也可以直接依据当前已看到的内容，分别判断它们是否有意思、符合你的人设并值得"
                "分享到其他群聊。值得时通过 tool_search 发现并调用 request_cross_group_forward，不值得时不要转发。"
                "不要等待下一条聊天消息后再作判断。\n"
                "</system-reminder>"
            )
        if nested_message_ids:
            return (
                "<system-reminder>\n"
                f"刚刚已经查看合并转发消息 msg_id={nested_message_ids} 的当前层内容，但其中仍有尚未展开的嵌套转发消息。"
                "如果继续查看有助于判断，建议使用刚看到的内容中给出的 path 再次调用 view_forward_message；"
                "你也可以直接依据当前已看到的内容，判断它是否有意思、符合你的人设并值得分享到其他群聊。"
                "值得时通过 tool_search 发现并调用 request_cross_group_forward，不值得时不要转发。"
                "不要等待下一条聊天消息后再作判断。\n"
                "</system-reminder>"
            )
        return (
            "<system-reminder>\n"
            f"刚刚已经成功查看合并转发消息 msg_id={complete_message_ids} 的完整内容。"
            "请在本次续轮中优先依据刚看到的完整内容，判断它是否有意思、符合你的人设并值得分享到其他群聊；"
            "值得时通过 tool_search 发现并调用 request_cross_group_forward，不值得时不要转发。"
            "不要等待下一条聊天消息后再作判断。\n"
            "</system-reminder>"
        )

    def acknowledge_view_judgment(self, session_id: str) -> None:
        """确认 Planner 已完成一次带续轮提醒的模型响应。

        Args:
            session_id: 已成功返回 Planner 响应的聊天流 ID。该方法清除该流
                当前全部待判断消息，使后续普通轮次不再重复注入提醒。
        """

        self._pending_view_judgments.pop(session_id, None)

    async def request_forward(
        self,
        msg_id: str,
        sharing_reason: str,
        content_summary: str,
        invocation_context: dict[str, Any],
    ) -> dict[str, Any]:
        """将 Tool 调用委托给请求服务完成校验和真实投递。

        Args:
            msg_id: source Planner 请求分享的合并转发消息 ID。
            sharing_reason: Planner 给出的分享理由，可为空。
            content_summary: 连续可重试故障达到配置阈值或连续两次返回
                空内容时使用的忠实摘要，可为空。
            invocation_context: Runner 传入的完整调用参数。运行时只消费
                EARLY Hook 签发的一次性凭据，不信任其中模型可写的
                ``platform``、``group_id``、``stream_id`` 或 ``chat_id``。

        Returns:
            可供 Planner 阅读的结果字典。完整投递成功时包含
            ``success=True``、``completed=True``、``status="succeeded"``、
            ``job_id`` 和目标统计；重复任务会返回 ``accepted=False``；
            校验或投递失败返回 ``success=False`` 和中文 ``content``。
        """

        source_stream_id = self.invocation_gate.consume(
            msg_id,
            invocation_context,
        )
        if not source_stream_id:
            return self.requests.failure(
                "转发请求缺少可信的 Planner 会话授权，已拒绝执行；请在当前 Planner 轮次重新发起请求。"
            )
        return await self.requests.create(
            msg_id,
            sharing_reason,
            content_summary,
            source_stream_id,
        )
