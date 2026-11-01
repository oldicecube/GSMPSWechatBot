"""麦麦自主跨群转发插件入口。"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

if __package__:
    from .forward_messages_auto import (
        CONFIG_VERSION,
        PLUGIN_VERSION,
        ForwardMessagesAutoConfig,
        ForwardingRuntime,
    )
    from .forward_messages_auto.config_recovery import LastKnownGoodConfig
    from .forward_messages_auto.source.authorization import (
        FORWARD_AUTHORIZATION_ROUND_KWARG,
        FORWARD_TOOL_NAME,
    )
    from .forward_messages_auto.source.output_items import append_user_reminder
else:
    from forward_messages_auto import (
        CONFIG_VERSION,
        PLUGIN_VERSION,
        ForwardMessagesAutoConfig,
        ForwardingRuntime,
    )
    from forward_messages_auto.config_recovery import LastKnownGoodConfig
    from forward_messages_auto.source.authorization import (
        FORWARD_AUTHORIZATION_ROUND_KWARG,
        FORWARD_TOOL_NAME,
    )
    from forward_messages_auto.source.output_items import append_user_reminder


class ForwardMessagesAutoPlugin(MaiBotPlugin):
    """声明 SDK 组件并装配跨群转发运行时。"""

    config_model = ForwardMessagesAutoConfig

    def __init__(self) -> None:
        """创建尚未启动运行时的插件实例。

        SDK 上下文和配置会在 Runner 加载阶段注入。构造方法仅初始化运行时
        引用；磁盘状态、聊天流和后台服务由 ``on_load`` 创建。
        """

        super().__init__()
        self._runtime: ForwardingRuntime | None = None
        self._config_recovery = LastKnownGoodConfig(Path(__file__).resolve().with_name("config.toml"))

    def normalize_plugin_config(
        self,
        config_data: Mapping[str, Any] | None,
    ) -> tuple[dict[str, Any], bool]:
        """校验新配置，并在 TOML 临时不完整时保留最近有效配置。

        Runner 的文件监听可能在编辑器尚未写完数组时读取失败，并在调用本
        方法前重建出默认配置。此时方法仅在磁盘文件确实不可解析时恢复最近
        一次有效快照，避免默认关闭状态被误判为用户主动禁用。合法配置仍交
        给 SDK 合并和校验；配置文件被删除时也继续使用 SDK 默认配置。

        Args:
            config_data: Runner 完成版本预处理后的插件配置；解析失败时通常
                是配置模型生成的完整默认值。

        Returns:
            二元组包含可注入的完整配置字典，以及 SDK 是否需要把归一化结果
            视为发生变化。恢复旧快照时第二项固定为 ``False``。
        """

        recovered_config = self._config_recovery.recover_for_invalid_file(
            config_data,
            type(self).build_default_config(),
        )
        if recovered_config is not None:
            try:
                self.ctx.logger.warning("config.toml 当前不可解析，继续使用最近一次有效配置并等待下次保存")
            except (AttributeError, RuntimeError):
                pass
            return recovered_config, False

        normalized_config, changed = super().normalize_plugin_config(config_data)
        self._config_recovery.remember(normalized_config)
        return normalized_config, changed

    @property
    def runtime(self) -> ForwardingRuntime:
        """返回已经由插件加载生命周期初始化的运行时。

        Returns:
            当前插件实例共享的 ``ForwardingRuntime``。

        Raises:
            RuntimeError: 在 ``on_load`` 完成前或运行时尚未创建时访问。
        """

        if self._runtime is None:
            raise RuntimeError("插件运行时尚未初始化")
        return self._runtime

    def get_components(self) -> list[dict[str, Any]]:
        """读取 SDK 组件定义，并强制转发 Tool 只在群聊生效。

        方法保留 SDK 生成的全部组件，仅对 ``request_cross_group_forward``
        设置顶层 ``chat_scope="group"``，并移除 metadata 中可能重复的同名
        字段，以符合 Host 的组件元数据格式。

        Returns:
            可注册到 Host 的组件定义列表。
        """

        components = super().get_components()
        for component in components:
            if component.get("name") != FORWARD_TOOL_NAME:
                continue
            component["chat_scope"] = "group"
            metadata = component.get("metadata")
            if isinstance(metadata, dict):
                metadata.pop("chat_scope", None)
        return components

    async def on_load(self) -> None:
        """创建运行时并完成插件加载准备。

        方法装配使用动态配置提供器的 ``ForwardingRuntime``，加载永久防重
        状态，随后检查版本字段并记录当前启用状态及白名单数量。
        """

        self._runtime = ForwardingRuntime(self.ctx, lambda: self.config)
        await self.runtime.start()
        self._warn_for_version_mismatch()
        self.ctx.logger.info(
            "麦麦自主跨群转发插件 v%s 已加载，enabled=%s source=%d target=%d",
            PLUGIN_VERSION,
            self.config.plugin.enabled,
            len(self.runtime.source_groups()),
            len(self.runtime.target_groups()),
        )

    async def on_unload(self) -> None:
        """停止后台任务、保存状态并完成插件卸载。

        即使运行时尚未创建也允许安全调用。已创建时会取消并等待所有转发
        任务，再持久化最后阶段，避免卸载后继续向目标群发送。
        """

        if self._runtime is not None:
            await self._runtime.stop()
        self.ctx.logger.info("麦麦自主跨群转发插件已卸载")

    async def on_config_update(
        self,
        scope: str,
        config_data: dict[str, Any],
        version: str,
    ) -> None:
        """确认 Runner 已写入的新配置并记录热更新。

        运行时服务通过动态配置提供器读取白名单和行为字段，不清理查看资格
        或永久防重状态；尚未发送的路径 B 续接请求会被取消，避免旧授权跨越
        启用状态或路由变更后恢复。方法随后检查版本并记录更新信息。

        Args:
            scope: 配置更新范围，由 MaiBot Runner 提供并写入日志。
            config_data: 更新后的原始配置字典。本插件通过 ``self.config``
                读取强类型配置，因此不直接消费该参数。
            version: 本次热更新事件携带的配置版本标识，用于日志追踪。
        """

        del config_data
        if self._runtime is not None:
            self.runtime.cancel_pending_direct_forwards()
        self._warn_for_version_mismatch()
        self.ctx.logger.info("自主跨群转发配置已更新: scope=%s version=%s", scope, version)

    @HookHandler(
        "chat.receive.after_process",
        name="trigger_source_planner_for_forward_message",
        description="source 白名单群收到合并转发消息时按配置强制触发本群 Planner。",
        mode=HookMode.OBSERVE,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def trigger_source_planner_for_forward_message(
        self,
        message: Any = None,
        **kwargs: Any,
    ) -> None:
        """观察入站合并转发并异步安排一次 source Planner 触发。

        Hook 只把消息交给运行时筛选和排队，不修改入站消息，也不等待真实
        消息落库或 Planner 入队，因此不会阻塞 Host 消息主链。是否查看、
        转发或回复仍完全由后续 Planner 自主决定。

        Args:
            message: ``chat.receive.after_process`` 提供的序列化
                ``SessionMessage``；缺失或格式异常时忽略。
            **kwargs: Hook 未来可能增加的其他参数，本处理器不读取。

        Raises:
            RuntimeError: Hook 在 ``on_load`` 初始化运行时前被调用。
        """

        del kwargs
        self.runtime.observe_source_message(message)

    def _warn_for_version_mismatch(self) -> None:
        """检查配置声明的版本字段并记录不一致警告。

        该方法不修改用户配置，也不阻止插件加载。插件版本和配置版本分别与
        源码常量比较，以提示旧配置或手工编辑造成的偏差。
        """

        if self.config.plugin.version != PLUGIN_VERSION:
            self.ctx.logger.warning(
                "配置中的 plugin.version=%s 与插件版本 %s 不一致",
                self.config.plugin.version,
                PLUGIN_VERSION,
            )
        if self.config.plugin.config_version != CONFIG_VERSION:
            self.ctx.logger.warning(
                "配置版本 %s 与插件要求的 %s 不一致",
                self.config.plugin.config_version,
                CONFIG_VERSION,
            )

    @HookHandler(
        "maisaka.planner.before_request",
        name="capture_view_forward_result",
        description="按当前 Planner 上下文同步已经展开的合并转发查看资格。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def capture_view_forward_result(self, **kwargs: Any) -> dict[str, Any]:
        """同步上下文查看资格，并在成功查看后的续轮末尾追加判断提醒。

        方法从 Planner 历史中提取 ``view_forward_message`` 结果，并以当前
        ``session_id`` 隔离同步。当前历史中仍可见的成功结果持续有效，
        被上下文裁剪的结果立即失效；新成功结果会触发一个位于请求末尾的
        ``system-reminder``，要求 Planner 立即判断是否值得分享。它不修改
        工具定义；自主转发 Tool 仍由 deferred tool 机制负责发现。

        Args:
            **kwargs: ``maisaka.planner.before_request`` Hook 参数。使用
                ``session_id`` 隔离状态，并读取及按需追加最新 ``items``；同时
                兼容旧版 ``messages`` 载荷。

        Returns:
            阻塞 Hook 的继续结果。没有待判断消息时参数保持原样；成功查看
            后会在 ``modified_kwargs.items`` 或旧版 ``messages`` 末尾追加一次
            判断提醒。

        Raises:
            RuntimeError: Hook 在 ``on_load`` 初始化运行时前被调用。
        """

        session_id = str(kwargs.get("session_id") or "").strip()
        if session_id:
            history_items = kwargs.get("items") if "items" in kwargs else kwargs.get("messages")
            self.runtime.capture_view_results(session_id, history_items)
            reminder = self.runtime.build_view_judgment_reminder(session_id)
            if reminder:
                if "items" in kwargs and isinstance(kwargs.get("items"), list):
                    kwargs["items"] = append_user_reminder(kwargs["items"], reminder)
                elif isinstance(kwargs.get("messages"), list):
                    kwargs["messages"].append({"role": "user", "content": reminder})
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.planner.after_response",
        name="authorize_forward_request_context",
        description="清除模型伪造上下文字段，并为转发调用绑定真实 Planner 会话。",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def authorize_forward_request_context(self, **kwargs: Any) -> dict[str, Any]:
        """在任何异步预检前清洗参数并签发一次性会话凭据。

        方法不调用 Host capability。每个转发调用若携带历史内部凭据，只撤销
        该凭据并为本次调用签发新凭据，不影响同 session 的其他并发调用。
        方法同时生成只沿当前 Hook 链传递的随机轮次标记。即使后续自动查看
        Hook 超时或失败，本次修改仍由 Host Hook 链保留；如果本 Hook 自身
        未执行成功，LATE 与正式 Tool handler 会因缺少可信凭据而拒绝原始
        模型调用，不会信任模型提供的 ``platform``、``group_id`` 或
        ``stream_id``。

        Args:
            **kwargs: ``maisaka.planner.after_response`` Hook 参数。读取真实
                ``session_id`` 和最新 ``output_items``；同时兼容旧版序列化
                ``tool_calls``，并写入只供同一 Hook 链 LATE 阶段使用的本轮
                随机标记。

        Returns:
            阻塞 Hook 的继续结果，其中最新 ``output_items`` 或旧版转发调用
            只保留公开参数并附带内部一次性凭据，同时包含本轮随机标记；其他
            工具调用与响应字段保持不变。

        Raises:
            RuntimeError: Hook 在 ``on_load`` 初始化运行时前被调用。
        """

        session_id = str(kwargs.get("session_id") or "").strip()
        if "output_items" in kwargs:
            output_items, authorization_round = self.runtime.authorize_forward_output_items(
                session_id,
                kwargs.get("output_items"),
            )
            kwargs["output_items"] = output_items
        else:
            tool_calls, authorization_round = self.runtime.authorize_forward_calls(
                session_id,
                kwargs.get("tool_calls"),
            )
            kwargs["tool_calls"] = tool_calls
        kwargs[FORWARD_AUTHORIZATION_ROUND_KWARG] = authorization_round
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.planner.after_response",
        name="orchestrate_view_before_forward",
        description="未查看时先执行真实合并转发查看，再恢复原转发请求。",
        mode=HookMode.BLOCKING,
        order=HookOrder.LATE,
        timeout_ms=6000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def orchestrate_view_before_forward(self, **kwargs: Any) -> dict[str, Any]:
        """在工具执行前编排自动查看并清除已消费的判断提醒。

        对单独出现的直接转发请求，当前上下文没有成功查看或允许降级资格时，
        方法将其替换成真实 ``view_forward_message``。下一 Planner 续轮捕获
        精确结果后，再恢复最初的转发参数；同一批不会同时查看和发送。路径 A
        的调用保持原样。此 Hook 只验证 EARLY 本轮标记及其签发的凭据，绝不
        重新签发或改绑。被新消息中断的 Planner 请求不会触发本 Hook，因此
        尚未消费的普通查看判断提醒会继续保留。

        Args:
            **kwargs: ``maisaka.planner.after_response`` Hook 参数。读取
                ``session_id``、最新 ``output_items`` 或旧版 ``response``、
                ``tool_calls``，以及 EARLY 本轮标记；消费标记后保留其他统计
                及选择字段。

        Returns:
            阻塞 Hook 的继续结果。未接管时最新 Item 或旧版响应保持原样；路径
            B 会在对应载荷中写回唯一的查看或恢复转发调用。

        Raises:
            RuntimeError: Hook 在 ``on_load`` 初始化运行时前被调用。
        """

        session_id = str(kwargs.get("session_id") or "").strip()
        authorization_round = str(kwargs.pop(FORWARD_AUTHORIZATION_ROUND_KWARG, "") or "").strip()
        if session_id:
            if "output_items" in kwargs:
                kwargs["output_items"] = await self.runtime.transform_after_output_items(
                    session_id,
                    kwargs.get("output_items"),
                    authorization_round,
                )
            else:
                response, tool_calls = await self.runtime.transform_after_response(
                    session_id,
                    kwargs.get("response"),
                    kwargs.get("tool_calls"),
                    authorization_round,
                )
                kwargs["response"] = response
                kwargs["tool_calls"] = tool_calls
        return {"action": "continue", "modified_kwargs": kwargs}

    @Tool(
        FORWARD_TOOL_NAME,
        brief_description=(
            "根据 msg_id，将已经完整查看，且你觉得有意思、符合人设、值得分享的合并转发消息，转发到其他群聊。"
        ),
        detailed_description=(
            "调用本工具前，先查看该消息的全部内容，"
            "并仅在看完后判断它有意思、符合人设且值得分享时调用；不要仅根据消息预览请求转发。"
            "目标群由插件配置决定，禁止自行指定目标群。"
            "content_summary 只在连续重试达到阈值或返回空内容时降级使用。"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description="要分享的源合并转发消息 ID",
                required=True,
            ),
            ToolParameterInfo(
                name="sharing_reason",
                param_type=ToolParamType.STRING,
                description="认为这则内容值得分享的简短原因",
                required=True,
                default="",
            ),
            ToolParameterInfo(
                name="content_summary",
                param_type=ToolParamType.STRING,
                description=("对当前已知内容的摘要，仅在连续重试达到阈值或返回空内容时降级使用"),
                required=False,
                default="",
            ),
        ],
        visibility="deferred",
        chat_scope="group",
        timeout_ms=120000,
    )
    async def request_cross_group_forward(
        self,
        msg_id: str,
        sharing_reason: str = "",
        content_summary: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        """请求把一则 source 合并转发分享至白名单群。

        该 Tool 不接收 target 参数；目标集合和顺序只能来自插件配置。方法
        将 SDK 调用上下文和 Planner 参数交给运行时。Planner 未提前查看时，
        ``after_response`` Hook 会先执行真实查看并在后续内部轮恢复相同参数；
        处理器本身继续校验查看资格，并等待全部 target 的真实顺序结果。

        Args:
            msg_id: 要分享的源合并转发消息 ID。
            sharing_reason: Planner 判断内容值得分享的简短理由。
            content_summary: 连续可重试故障达到配置阈值或连续两次返回
                空内容时使用的忠实摘要。
            **kwargs: Runner 传入的完整 Tool 参数。正式入口只消费 EARLY Hook
                生成的内部一次性凭据；模型提供的 Host 上下文字段不参与授权。

        Returns:
            Planner 可读的真实任务结果。新任务包含 ``accepted=True``、
            ``completed=True``、聚合状态、任务 ID 和目标数量；重复任务包含
            ``accepted=False``；拒绝或投递失败时包含 ``success=False`` 和
            中文原因。

        Raises:
            RuntimeError: Tool 在 ``on_load`` 初始化运行时前被调用。
        """

        return await self.runtime.request_forward(
            msg_id,
            sharing_reason,
            content_summary,
            kwargs,
        )


def create_plugin() -> ForwardMessagesAutoPlugin:
    """创建供 MaiBot Runner 加载的插件实例。

    Returns:
        尚未注入上下文或启动运行时的 ``ForwardMessagesAutoPlugin``。Runner
        随后负责设置配置与上下文并调用 ``on_load``。
    """

    return ForwardMessagesAutoPlugin()
