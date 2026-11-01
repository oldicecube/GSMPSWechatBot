"""麦麦生活插件入口。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import asyncio
import json
import logging

from maibot_sdk import (
    CONFIG_RELOAD_SCOPE_SELF,
    Command,
    HookHandler,
    MaiBotPlugin,
    ON_BOT_CONFIG_RELOAD,
    Tool,
)
from maibot_sdk.types import (
    ErrorPolicy,
    HookMode,
    HookOrder,
    ToolParameterInfo,
    ToolParamType,
)

from .config_model import MaiLifeConfig, share_allowlist
from .generator import LifeGenerator, format_activities, format_shares, format_shares_by_stream
from .injector import build_inject_text, build_today_schedule_text, inject_into_items, inject_into_messages
from .prompts import DEFAULT_WAKE_INTENT_TEMPLATE, choose_template, render_template
from .scheduler import DEFAULT_MISS_TOLERANCE_MINUTES, LifeScheduler, share_due_status
from .store import LifeDocument, LifeStore, ShareItem, shares_for_stream, task_due_at, task_state
from .tasks import TaskManager, format_task_records
from .tool_descriptions import DEFAULT_MODIFY_TOOL_DESCRIPTION, DEFAULT_QUERY_TOOL_DESCRIPTION
from .streams import (
    filter_streams,
    format_stream_info,
    normalize_text,
    session_allowed,
    stream_session_id,
)
from .times import parse_date, wall_clock_now, weekday_cn

logger = logging.getLogger("mailife.mai-life")

_QUERY_DESCRIPTION = DEFAULT_QUERY_TOOL_DESCRIPTION
_MODIFY_DESCRIPTION = DEFAULT_MODIFY_TOOL_DESCRIPTION


class MaiLifePlugin(MaiBotPlugin):
    """麦麦生活：日程生成、分享提醒、规划器注入与工具。"""

    config_model = MaiLifeConfig
    config_reload_subscriptions = {ON_BOT_CONFIG_RELOAD}

    def __init__(self) -> None:
        super().__init__()
        self._store: LifeStore | None = None
        self._generator: LifeGenerator | None = None
        self._scheduler: LifeScheduler | None = None
        self._generate_lock = asyncio.Lock()
        self._cached_persona = ""
        self._cached_nickname = "麦麦"
        self._stream_cache: dict[str, tuple[datetime, list[dict[str, Any]]]] = {}
        self._registered_tool_descriptions: tuple[str, str] | None = None
        self._manual_generation_tasks: set[asyncio.Task[None]] = set()

    def _tool_descriptions(self) -> tuple[str, str]:
        # Runner 先注入配置再收集组件；未注入时仅用于离线组件探查。
        config = self.config if self._plugin_config_instance is not None else MaiLifeConfig()
        return (
            choose_template(config.tools.query_description, DEFAULT_QUERY_TOOL_DESCRIPTION),
            choose_template(config.tools.modify_description, DEFAULT_MODIFY_TOOL_DESCRIPTION),
        )

    def get_components(self) -> list[dict[str, Any]]:
        components = super().get_components()
        query, modify = self._tool_descriptions()
        descriptions = {"mai_life_query": query, "mai_life_modify": modify}
        for component in components:
            if component["name"] in descriptions:
                metadata = dict(component["metadata"])
                metadata["description"] = descriptions[component["name"]]
                metadata["brief_description"] = descriptions[component["name"]]
                # Host 的 action 调用入口强制绑定当前流；普通 tool 入口仅补缺失值。
                # 组件仍是 TOOL，Runner 使用同一 handler/结果协议，不读取 action_data 的目标流。
                metadata["invoke_method"] = "plugin.invoke_action"
                component["metadata"] = metadata
        self._registered_tool_descriptions = (query, modify)
        return components

    async def on_load(self) -> None:
        data_dir = self.ctx.paths.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self._store = LifeStore(data_dir, now=self._now)
        self._generator = LifeGenerator(self.ctx, self._store)
        self._scheduler = LifeScheduler(
            get_config=lambda: self.config,
            generate_today=self._generate_today,
            load_today=self._load_today,
            fire_share=self._fire_share,
            mark_share_fired=self._mark_share_fired,
            store=self._store,
        )
        await self._refresh_bot_profile()
        if self.config.plugin.enabled:
            await self._scheduler.start(
                generate_if_missing=self.config.generation.generate_on_load_if_missing
            )
            logger.info("麦麦生活已加载，调度器已启动")
        else:
            logger.info("麦麦生活已加载，但 plugin.enabled=false，调度器未启动")

    async def _stop_manual_generation(self) -> None:
        tasks = list(self._manual_generation_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._manual_generation_tasks.clear()

    async def on_unload(self) -> None:
        if self._scheduler is not None:
            await self._scheduler.stop()
        await self._stop_manual_generation()
        logger.info("麦麦生活已卸载")

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        del config_data, version
        if scope == ON_BOT_CONFIG_RELOAD:
            await self._refresh_bot_profile()
            return
        if scope != CONFIG_RELOAD_SCOPE_SELF:
            return
        if self._registered_tool_descriptions != self._tool_descriptions():
            logger.info("工具说明配置已保存；当前 SDK 不热更新组件元数据，需手动重载插件后规划器才会看到新说明")
        if not self.config.plugin.enabled:
            await self._stop_manual_generation()
        if self._scheduler is None:
            return
        if self.config.plugin.enabled:
            await self._scheduler.start(
                generate_if_missing=self.config.generation.generate_on_load_if_missing
            )
            logger.info("麦麦生活配置已更新，调度器已重启")
        else:
            await self._scheduler.stop()
            logger.info("麦麦生活已关闭，调度器已停止")

    @HookHandler(
        "maisaka.planner.before_request",
        name="mai_life_inject_schedule",
        description="在规划器请求前注入最近几条日程",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,
    )
    async def inject_schedule_handler(self, **kwargs: Any) -> dict[str, Any]:
        config = self.config
        if not config.plugin.enabled or not config.schedule.enabled:
            return {"action": "continue"}
        session_id = normalize_text(kwargs.get("session_id"))
        hook_log = self.ctx.logger
        if not session_id:
            hook_log.info("日程注入跳过：规划器未提供 session_id")
            return {"action": "continue"}
        streams = await self._cached_streams(config.schedule.stream_discovery_platform)
        if not session_allowed(session_id, config.schedule.allowed_streams, streams):
            hook_log.info(
                "日程注入跳过：聊天流未匹配白名单 session=%s allowlist=%s",
                session_id,
                config.schedule.allowed_streams,
            )
            return {"action": "continue"}
        now = self._now()
        document = self._load_today(now)
        text = build_inject_text(
            document,
            now,
            count=config.schedule.recent_inject_count,
            window_minutes=config.schedule.inject_window_minutes,
            template=config.prompts.inject_template,
            yesterday=self._load_yesterday(now),
            extra_values={
                "timezone": config.generation.timezone,
                "bot_nickname": self._cached_nickname,
            },
        )
        if not text:
            hook_log.info(
                "日程注入跳过：没有可注入的日程 session=%s date=%s activities=%s",
                session_id,
                now.strftime("%Y-%m-%d"),
                0 if document is None else len(document.activities),
            )
            return {"action": "continue"}
        result = dict(kwargs)
        items = kwargs.get("items")
        messages = kwargs.get("messages")
        if isinstance(items, list):
            result["items"] = inject_into_items(items, text, now=now)
            hook_log.info("已向规划器注入日程 session=%s chars=%s payload=items", session_id, len(text))
            return {"action": "continue", "modified_kwargs": result}
        if isinstance(messages, list):
            result["messages"] = inject_into_messages(messages, text)
            hook_log.info("已向规划器注入日程 session=%s chars=%s payload=messages", session_id, len(text))
            return {"action": "continue", "modified_kwargs": result}
        hook_log.warning(
            "日程注入跳过：规划器未提供 items 或 messages session=%s keys=%s",
            session_id,
            sorted(str(key) for key in kwargs),
        )
        return {"action": "continue"}

    @Tool(
        "mai_life_query",
        brief_description=_QUERY_DESCRIPTION,
        detailed_description=_QUERY_DESCRIPTION,
        core_tool=True,
        parameters=[
            ToolParameterInfo(
                name="date",
                param_type=ToolParamType.STRING,
                description="YYYY-MM-DD；留空查今天日程/所有待处理任务；任务按到期日筛选",
                required=False,
            ),
            ToolParameterInfo(
                name="target",
                param_type=ToolParamType.STRING,
                description="schedule=全局日程；share=当前聊天流统一任务；both=两者，默认 both",
                required=False,
                enum_values=["schedule", "share", "both"],
            ),
        ],
    )
    async def handle_query(self, date: str = "", target: str = "both", **kwargs: Any) -> dict[str, Any]:
        try:
            return await self._query_life(date=date, target=target, kwargs=kwargs)
        except Exception as exc:
            logger.exception("查询生活记录失败")
            return {"success": False, "content": f"查询失败：{exc}"}

    @Tool(
        "mai_life_modify",
        brief_description=_MODIFY_DESCRIPTION,
        detailed_description=_MODIFY_DESCRIPTION,
        core_tool=True,
        parameters=[
            ToolParameterInfo(
                name="target", param_type=ToolParamType.STRING,
                description="schedule=生活日程；share=当前对话的自动分享与单次任务",
                required=True, enum_values=["schedule", "share"],
            ),
            ToolParameterInfo(
                name="action", param_type=ToolParamType.STRING,
                description="share 支持 list/add/update/delete；schedule 支持 add/update/delete/replace_day",
                required=True, enum_values=["list", "add", "update", "delete", "replace_day"],
            ),
            ToolParameterInfo(
                name="request", param_type=ToolParamType.STRING,
                description="仅 schedule 必填：用自然语言描述日程变更",
                required=False,
            ),
            ToolParameterInfo(
                name="date", param_type=ToolParamType.STRING,
                description="YYYY-MM-DD；日程默认今天；任务 list 按到期日筛选，update/delete 可填 owner_date，add 留空",
                required=False,
            ),
            ToolParameterInfo(
                name="task_id", param_type=ToolParamType.STRING,
                description="任务 update/delete 必填：查询返回的 task_id",
                required=False,
            ),
            ToolParameterInfo(
                name="title", param_type=ToolParamType.STRING,
                description="任务 add 必填：简短标题；update 可修改",
                required=False,
            ),
            ToolParameterInfo(
                name="intent", param_type=ToolParamType.STRING,
                description="任务 add 必填：到期要做什么；提醒写明约定，分享写明话题",
                required=False,
            ),
            ToolParameterInfo(
                name="run_at", param_type=ToolParamType.STRING,
                description="配置时区下的未来 YYYY-MM-DD HH:MM[:SS]；与 delay_minutes 二选一",
                required=False,
            ),
            ToolParameterInfo(
                name="delay_minutes", param_type=ToolParamType.NUMBER,
                description="从现在延后多少分钟，须大于 0；与 run_at 二选一",
                required=False,
            ),
            ToolParameterInfo(
                name="request_id", param_type=ToolParamType.STRING,
                description="可选，仅任务 add：同一请求重试沿用此键，避免重复安排",
                required=False,
            ),
        ],
    )
    async def handle_modify(
        self,
        target: str = "schedule",
        action: str = "update",
        request: str = "",
        date: str = "",
        task_id: str = "",
        title: str = "",
        intent: str = "",
        run_at: str = "",
        delay_minutes: float | None = None,
        request_id: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        try:
            unsupported = {"repeat", "recurrence", "cron", "interval", "frequency", "every", "repeat_interval", "target_stream_id", "timezone"}.intersection(kwargs)
            if unsupported:
                return {"success": False, "content": "不支持周期、跨流或每任务时区参数，请使用当前流单次任务"}
            return await self._modify_life(
                target=target, action=action, request=request, date=date, kwargs=kwargs,
                task_id=task_id, title=title, intent=intent, run_at=run_at,
                delay_minutes=delay_minutes, request_id=request_id,
            )
        except ValueError as exc:
            return {"success": False, "content": f"参数无效：{exc}"}
        except Exception as exc:
            logger.exception("修改生活记录失败")
            return {"success": False, "content": f"修改失败：{exc}"}

    @Command("mai_life_help", description="查看麦麦生活命令", pattern=r"^/mai_life_help$")
    async def handle_help(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        denied = await self._require_command_admin(stream_id, kwargs)
        if denied is not None:
            return denied
        text = (
            "麦麦生活命令（仅管理员）：\n"
            "/mai_life_help 查看帮助\n"
            "/mai_life_status 查看开关、条数和下次生成时间\n"
            "/mai_life_generate 立即生成今日日程和分享任务\n"
            "/mai_life_show 查看今日日程和所有聊天流的分享任务\n"
            "/mai_life_wake 立即对当前聊天流唤醒规划器（测试用）"
        )
        await self._reply(stream_id, text)
        return True, "已发送帮助", 2

    @Command("mai_life_status", description="查看麦麦生活状态", pattern=r"^/mai_life_status$")
    async def handle_status(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        denied = await self._require_command_admin(stream_id, kwargs)
        if denied is not None:
            return denied
        now = self._now()
        document = self._load_today(now)
        activity_count = len(document.activities) if document else 0
        share_count = len(document.shares) if document else 0
        pending = sum(1 for item in document.shares if task_state(item) == "pending") if document else 0
        stream_count = len({item.stream_id for item in document.shares if item.stream_id}) if document else 0
        text = (
            f"插件启用：{'是' if self.config.plugin.enabled else '否'}\n"
            f"管理员：{len(self.config.plugin.admin_user_ids)} 人\n"
            f"日程功能：{'是' if self.config.schedule.enabled else '否'}，"
            f"白名单 {len(self.config.schedule.allowed_streams)} 项\n"
            f"分享功能：{'是' if self.config.share.enabled else '否'}，"
            f"白名单 {len(share_allowlist(self.config.share))} 项\n"
            f"今日日期：{now.strftime('%Y-%m-%d')} {now.strftime('%H:%M')}\n"
            f"今日日程：{activity_count} 条\n"
            f"分享任务：{share_count} 条，未提醒 {pending} 条，"
            f"涉及 {stream_count} 个聊天流\n"
            f"每日生成时刻：{self.config.generation.time}"
        )
        await self._reply(stream_id, text)
        return True, "已发送状态", 2

    @Command("mai_life_generate", description="立即生成今日生活记录", pattern=r"^/mai_life_generate$")
    async def handle_generate(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        denied = await self._require_command_admin(stream_id, kwargs)
        if denied is not None:
            return denied
        if not self.config.plugin.enabled:
            await self._reply(stream_id, "麦麦生活未启用")
            return True, "插件未启用", 2
        task = asyncio.create_task(self._generate_and_notify(stream_id), name="mai-life-manual-generate")
        self._manual_generation_tasks.add(task)
        task.add_done_callback(self._manual_generation_tasks.discard)
        await self._reply(stream_id, "正在生成今日日程和分享任务…")
        return True, "已开始生成", 2

    @Command("mai_life_show", description="查看今日日程和所有聊天流的分享任务", pattern=r"^/mai_life_show$")
    async def handle_show(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        denied = await self._require_command_admin(stream_id, kwargs)
        if denied is not None:
            return denied
        now = self._now()
        document = self._load_today(now)
        if document is None:
            await self._reply(stream_id, "今天还没有生活记录。可用 /mai_life_generate 生成。")
            return True, "今日无记录", 2
        streams = await self._cached_streams("all_platforms")
        text = (
            f"{document.date} 日程：\n{format_activities(document.activities)}\n\n"
            f"{document.date} 全聊天流分享任务：\n{format_shares_by_stream(document.shares, streams)}"
        )
        await self._reply(stream_id, text)
        return True, "已发送今日记录", 2

    @Command("mai_life_wake", description="立即唤醒当前聊天流的规划器", pattern=r"^/mai_life_wake$")
    async def handle_wake(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        denied = await self._require_command_admin(stream_id, kwargs)
        if denied is not None:
            return denied
        if not stream_id:
            return True, "无法获取当前聊天流", 2
        if not self.config.plugin.enabled:
            await self._reply(stream_id, "麦麦生活未启用")
            return True, "插件未启用", 2
        now = self._now()
        intent = render_template(
            choose_template(self.config.prompts.wake_intent_template, DEFAULT_WAKE_INTENT_TEMPLATE),
            self._wake_values(now, share_item="手动测试唤醒"),
        )
        result = await self.ctx.maisaka.proactive.trigger(
            stream_id=stream_id,
            intent=intent,
            reason="mai_life_manual_wake",
            metadata={"source": "mai-life", "manual": True},
        )
        await self._reply(stream_id, f"已请求唤醒规划器：{result}")
        return True, "已唤醒规划器", 2

    async def _query_life(self, date: str, target: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": True, "content": "麦麦生活未启用"}
        session_id = self._tool_session_id(kwargs)
        now = self._now()
        requested_date = normalize_text(date)
        query_date = requested_date or now.strftime("%Y-%m-%d")
        parse_date(query_date)
        scope = normalize_text(target).lower() or "both"
        if scope not in {"schedule", "share", "both"}:
            return {"success": False, "content": "target 只能是 schedule、share 或 both"}

        want_schedule = scope in {"schedule", "both"}
        want_share = scope in {"share", "both"}
        if want_schedule:
            streams = await self._cached_streams(self.config.schedule.stream_discovery_platform)
            if not session_allowed(session_id, self.config.schedule.allowed_streams, streams):
                want_schedule = False
        if want_share:
            share_streams = await self._cached_streams("all_platforms")
            if (
                not session_allowed(session_id, share_allowlist(self.config.share), share_streams)
                or not any(stream_session_id(stream) == session_id for stream in share_streams)
            ):
                want_share = False
        if scope == "schedule" and not want_schedule:
            return {"success": True, "content": "当前聊天流未启用日程功能"}
        if scope == "share" and not want_share:
            return {"success": True, "content": "当前聊天流未启用分享提醒"}
        if not want_schedule and not want_share:
            return {"success": True, "content": "当前聊天流未启用麦麦生活"}

        if self._store is None:
            return {"success": False, "content": "存储未初始化"}
        parts: list[str] = []
        if want_schedule:
            if not self.config.schedule.enabled:
                parts.append("日程功能未启用")
            else:
                document = self._store.load(query_date)
                if query_date == now.strftime("%Y-%m-%d"):
                    text = build_today_schedule_text(document, self._load_yesterday(now), now)
                else:
                    text = format_activities(document.activities) if document else "（该日期暂无日程）"
                parts.append(f"{query_date} 日程：\n{text}")
        records: list[dict[str, Any]] = []
        if want_share:
            if not self.config.share.enabled:
                parts.append("分享/定时任务功能未启用")
            else:
                records = TaskManager(self._store).list(session_id, self.config.generation.timezone, requested_date)
                label = f"{requested_date} 到期" if requested_date else "跨日期待处理"
                parts.append(f"当前聊天流{label}任务：\n{format_task_records(records)}")
        return {
            "success": True, "content": "\n\n".join(parts), "tasks": records,
            "timezone": self.config.generation.timezone,
        }

    async def _modify_life(
        self, target: str, action: str, request: str, date: str, kwargs: dict[str, Any], *,
        task_id: str = "", title: str = "", intent: str = "", run_at: str = "",
        delay_minutes: float | None = None, request_id: str = "",
    ) -> dict[str, Any]:
        if not self.config.plugin.enabled:
            return {"success": False, "content": "麦麦生活未启用"}
        if self._store is None:
            return {"success": False, "content": "存储未初始化"}
        session_id = self._tool_session_id(kwargs)
        now = self._now()
        target_name = normalize_text(target).lower() or "schedule"
        action_name = normalize_text(action).lower() or "update"
        if target_name not in {"schedule", "share"}:
            return {"success": False, "content": "target 只能是 schedule 或 share"}
        if target_name == "share":
            streams = await self._cached_streams("all_platforms")
            if not self.config.share.enabled or not session_allowed(
                session_id, share_allowlist(self.config.share), streams
            ):
                return {"success": False, "content": "当前聊天流未启用分享/定时任务"}
            if not any(stream_session_id(stream) == session_id for stream in streams):
                return {"success": False, "content": "当前流尚未在 Host 注册，不能自行构造目标聊天流"}
            return TaskManager(self._store).manage(
                action=action_name, stream_id=session_id, now=now,
                timezone=self.config.generation.timezone, date=date, task_id=task_id,
                title=title, intent=intent, run_at=run_at, delay_minutes=delay_minutes,
                request_id=request_id, request=request, wake_planner=self.config.share.wake_planner,
            )
        if action_name not in {"add", "update", "delete", "replace_day"}:
            return {"success": False, "content": "schedule 只支持 add/update/delete/replace_day"}
        if task_id or title or intent or run_at or delay_minutes is not None or request_id:
            return {"success": False, "content": "日程修改请使用 request；任务结构化字段只用于 target=share"}
        request_text = normalize_text(request)
        if not request_text:
            return {"success": False, "content": "日程修改 request 不能为空"}
        schedule_date = normalize_text(date) or now.strftime("%Y-%m-%d")
        parse_date(schedule_date)
        streams = await self._cached_streams(self.config.schedule.stream_discovery_platform)
        if not self.config.schedule.enabled or not session_allowed(
            session_id, self.config.schedule.allowed_streams, streams
        ):
            return {"success": False, "content": "当前聊天流未启用日程功能"}
        if self._generator is None:
            return {"success": False, "content": "生成器未初始化"}
        document = await self._generator.modify(
            self.config, now, date=schedule_date, target="schedule", action=action_name,
            request=request_text, persona=self._cached_persona, bot_nickname=self._cached_nickname,
        )
        return {"success": True, "content": f"已更新日程：\n{format_activities(document.activities)}"}

    async def _generate_today(self, force: bool) -> LifeDocument | None:
        if self._generator is None:
            return None
        async with self._generate_lock:
            now = self._now()
            history = await self._resolve_history()
            knowledge = await self._resolve_knowledge()
            share_streams = await self._list_share_streams()
            history_by_stream = await self._resolve_history_by_streams(share_streams)
            document = await self._generator.generate_today(
                self.config,
                now,
                force=force,
                persona=self._cached_persona,
                history=history,
                knowledge=knowledge,
                bot_nickname=self._cached_nickname,
                share_streams=share_streams,
                history_by_stream=history_by_stream,
            )
            if document is not None:
                logger.info(
                    "已保存生活记录 date=%s activities=%s shares=%s streams=%s",
                    document.date,
                    len(document.activities),
                    len(document.shares),
                    len({item.stream_id for item in document.shares if item.stream_id}),
                )
            return document

    async def _generate_and_notify(self, stream_id: str) -> None:
        try:
            document = await self._generate_today(True)
        except Exception:
            logger.exception("手动生成失败")
            await self._reply(stream_id, "生成失败，请查看日志")
            return
        if document is None:
            await self._reply(stream_id, "生成失败：没有得到可用的日程")
            return
        current_shares = shares_for_stream(document, stream_id) if stream_id else document.shares
        stream_count = len({item.stream_id for item in document.shares if item.stream_id})
        warning = ""
        if any(item.source == "legacy" for item in document.shares):
            warning = "\n含来源未知的旧任务：相应聊天流的自动分享重生成已跳过，原任务完整保留。可逐项接管，次日新记录不受影响。"
        await self._reply(
            stream_id,
            f"已生成 {document.date}：日程 {len(document.activities)} 条，"
            f"当前聊天流统一任务 {len(current_shares)} 条，共 {stream_count} 个聊天流。{warning}",
        )

    def _load_today(self, now: datetime) -> LifeDocument | None:
        if self._store is None:
            return None
        return self._store.load(now.strftime("%Y-%m-%d"))

    def _load_yesterday(self, now: datetime) -> LifeDocument | None:
        if self._store is None:
            return None
        return self._store.load((now - timedelta(days=1)).strftime("%Y-%m-%d"))

    def _mark_share_fired(self, date: str, share_id: str, now: datetime) -> None:
        if self._store is not None:
            self._store.mark_share_fired(date, share_id, now)

    async def _fire_share(self, item: ShareItem, document: LifeDocument, now: datetime) -> None:
        if self._store is None or not self.config.plugin.enabled or not self.config.share.enabled or not self.config.share.wake_planner:
            return
        # 未绑定的旧记录不广播到所有白名单流，也不猜测它的原目标。
        target_stream_id = normalize_text(item.stream_id)
        if not target_stream_id:
            self._store.mark_task_state(document.date, "", item.id, "skipped", "legacy_stream_unknown", now, expected=item)
            return
        streams = [
            stream for stream in await self._list_share_streams()
            if stream_session_id(stream) == target_stream_id
        ]
        if not streams:
            # Host 流暂不可发现或不在白名单时保持 pending，宽限后由巡检记录跳过。
            logger.info("任务到期但当前未匹配白名单真实聊天流: task=%s", item.id)
            return
        if not self.config.plugin.enabled or not self.config.share.enabled or not self.config.share.wake_planner:
            return
        now = self._now()
        status = share_due_status(item, now, owner_date=document.date, miss_tolerance_minutes=DEFAULT_MISS_TOLERANCE_MINUTES)
        if status == "missed":
            self._store.mark_task_state(document.date, target_stream_id, item.id, "skipped", "missed_tolerance", now, expected=item)
            return
        if status != "due":
            return
        if self._scheduler is not None and not self._scheduler.can_wake_stream(target_stream_id, now, self.config.share.cooldown_seconds):
            return
        claimed = self._store.claim_task(document.date, item)
        if claimed is None:
            return
        intent = claimed.intent.strip()
        if not intent and claimed.source == "planner":
            self._store.mark_task_state(document.date, target_stream_id, item.id, "skipped", "missing_planner_intent", now, expected_state="submitting")
            return
        if not intent:
            share_item = f"{claimed.time} {claimed.title}"
            if claimed.hint:
                share_item += f"（{claimed.hint}）"
            intent = render_template(
                choose_template(self.config.prompts.wake_intent_template, DEFAULT_WAKE_INTENT_TEMPLATE),
                self._wake_values(now, share_item=share_item),
            )
        try:
            result = await self.ctx.maisaka.proactive.trigger(
                stream_id=target_stream_id, intent=intent,
                reason="mai_life_task" if claimed.source == "planner" else "mai_life_share",
                metadata={
                    "source": "mai-life", "task_id": claimed.id, "share_id": claimed.id,
                    "owner_date": document.date, "date": document.date,
                    "task_source": claimed.source, "run_at": task_due_at(claimed, document.date).isoformat(timespec="seconds"),
                },
            )
        except (Exception, asyncio.CancelledError):
            # RPC 断开/超时可能发生在 Host 已接受之后，不冒充失败并自动重复提交。
            finished_at = self._now()
            if self._scheduler is not None:
                self._scheduler.remember_wake(target_stream_id, finished_at)
            self._store.mark_task_state(document.date, target_stream_id, item.id, "consumed", "submission_result_unknown", finished_at, expected_state="submitting")
            raise
        finished_at = self._now()
        if isinstance(result, dict) and result.get("success") is True:
            if self._scheduler is not None:
                self._scheduler.remember_wake(target_stream_id, finished_at)
            self._store.mark_task_state(document.date, target_stream_id, item.id, "submitted", "host_accepted", finished_at, expected_state="submitting")
            logger.info("任务已提交规划器（不等于消息已发出）: task=%s", item.id)
        elif isinstance(result, dict) and result.get("success") is False:
            self._store.mark_task_state(document.date, target_stream_id, item.id, "pending", "host_rejected", finished_at, expected_state="submitting")
            logger.warning("Host 未接受任务，保留宽限内重试: task=%s", item.id)
        else:
            if self._scheduler is not None:
                self._scheduler.remember_wake(target_stream_id, finished_at)
            self._store.mark_task_state(document.date, target_stream_id, item.id, "consumed", "submission_result_unknown", finished_at, expected_state="submitting")
            logger.warning("Host 返回形状未知，未宣称提交成功: task=%s", item.id)

    def _wake_values(self, now: datetime, share_item: str) -> dict[str, str]:
        return {
            "date": now.strftime("%Y-%m-%d"),
            "weekday": weekday_cn(now),
            "now": now.strftime("%Y-%m-%d %H:%M"),
            "timezone": self.config.generation.timezone,
            "bot_nickname": self._cached_nickname,
            "share_item": share_item,
            "recent_schedule": "",
            "persona": self._cached_persona,
        }

    async def _list_share_streams(self) -> list[dict[str, Any]]:
        streams = await self._cached_streams("all_platforms")
        return filter_streams(streams, share_allowlist(self.config.share))

    async def _cached_streams(self, platform: str) -> list[dict[str, Any]]:
        platform = platform or "all_platforms"
        now = datetime.now()
        cached = self._stream_cache.get(platform)
        if cached is not None and 0 <= (now - cached[0]).total_seconds() < 60:
            return cached[1]
        fallback = cached[1] if cached is not None else []
        try:
            streams = await self.ctx.chat.get_all_streams(platform)
        except Exception:
            logger.exception("读取聊天流列表失败")
            return fallback
        if isinstance(streams, dict) and streams.get("success") is False:
            logger.warning("读取聊天流列表失败: %s", streams.get("error"))
            return fallback
        if not isinstance(streams, list):
            return fallback
        result = [item for item in streams if isinstance(item, dict)]
        self._stream_cache[platform] = (now, result)
        return result

    async def _resolve_history_by_streams(self, streams: list[dict[str, Any]]) -> dict[str, str]:
        result: dict[str, str] = {}
        for stream in streams:
            session_id = stream_session_id(stream)
            if not session_id:
                continue
            text = await self._resolve_history(stream_ids=[session_id])
            if text:
                result[session_id] = text
        return result

    @staticmethod
    def _learning_message_allowed(message: dict, rules: list) -> bool:
        if not isinstance(message, dict) or message.get("is_notify"):
            return False
        info = message.get("message_info") or {}
        metadata = info.get("additional_config") or {}
        if metadata.get("learnable") is False or (metadata.get("wechat") or {}).get("learnable") is False:
            return False
        platform = str(message.get("platform") or "")
        user = str((info.get("user_info") or {}).get("user_id") or "")
        group = str((info.get("group_info") or {}).get("group_id") or "")
        for rule in rules:
            if not isinstance(rule, dict):
                raise TypeError("Invalid learning policy rule")
            if rule.get("platform") == platform and str(rule.get("user_id")) == user and (not rule.get("group_id") or str(rule["group_id"]) == group):
                return False
        return True

    async def _resolve_history(self, stream_ids: list[str] | None = None) -> str:
        config = self.config.generation
        if not config.include_history or config.history_message_limit <= 0:
            return ""
        resolved_ids = [normalize_text(item) for item in (stream_ids or []) if normalize_text(item)]
        if not resolved_ids:
            resolved_ids = [normalize_text(item) for item in config.history_stream_ids if normalize_text(item)]
        if not resolved_ids:
            streams = await self._cached_streams(self.config.schedule.stream_discovery_platform)
            resolved_ids = [
                stream_session_id(item)
                for item in filter_streams(streams, self.config.schedule.allowed_streams)
            ]
        stream_ids = resolved_ids
        if not stream_ids:
            return ""
        now = self._now()
        start = now.timestamp() - max(1, int(config.history_window_hours)) * 3600
        parts: list[str] = []
        for stream_id in stream_ids[:8]:
            try:
                messages = await self.ctx.message.get_by_time_in_chat(
                    stream_id,
                    str(start),
                    str(now.timestamp()),
                    limit=config.history_message_limit,
                    limit_mode="latest",
                    filter_mai=False,
                    filter_command=True,
                )
            except Exception:
                logger.exception("读取聊天历史失败: %s", stream_id)
                continue
            if isinstance(messages, dict) and messages.get("success") is False:
                continue
            if not messages:
                continue
            # Persistent life generation is learning: honor the host learning policy.
            try:
                rules = await self.ctx.config.get("learning.excluded_users", [])
                if not isinstance(rules, list):
                    raise TypeError("learning.excluded_users must be a list")
                if isinstance(messages, dict):
                    messages = messages.get("messages", [])
                messages = [m for m in messages if self._learning_message_allowed(m, rules)]
            except Exception:
                logger.exception("Cannot verify learning policy; skipping history: %s", stream_id)
                continue
            if not messages:
                continue
            try:
                readable = await self.ctx.message.build_readable(messages)
            except Exception:
                readable = json.dumps(messages, ensure_ascii=False)[:2000]
            text = normalize_text(readable)
            if text:
                parts.append(f"聊天流 {stream_id}：\n{text}")
        return "\n\n".join(parts)

    async def _resolve_knowledge(self) -> str:
        config = self.config.generation
        if not config.include_knowledge or config.knowledge_search_limit <= 0:
            return ""
        try:
            payload = knowledge_search_args(
                self._now(),
                limit=config.knowledge_search_limit,
                window_hours=config.knowledge_window_hours,
            )
            result = await self.ctx.call_capability("knowledge.search", **payload)
        except Exception:
            logger.exception("知识库检索失败")
            return ""
        if isinstance(result, dict) and result.get("success") is False:
            return ""
        return normalize_text(result)

    async def _refresh_bot_profile(self) -> None:
        extra = self.config.generation.extra_persona.strip()
        system_persona = ""
        if self.config.generation.include_persona:
            try:
                system_persona = normalize_text(
                    await self.ctx.config.get("personality.personality", "")
                )
            except Exception:
                logger.exception("读取主程序人设失败")
        self._cached_persona = compose_persona(
            include_system=self.config.generation.include_persona,
            system_persona=system_persona,
            extra_persona=extra,
        )
        try:
            nickname = normalize_text(await self.ctx.config.get("bot.nickname", ""))
        except Exception:
            nickname = ""
        self._cached_nickname = nickname or "麦麦"

    def _now(self) -> datetime:
        timezone_name = self.config.generation.timezone
        if timezone_name not in {"", "local", "system"} and not self._timezone_ok(timezone_name):
            raise ValueError(f"未知时区: {timezone_name}")
        return wall_clock_now(timezone_name)

    @staticmethod
    def _timezone_ok(name: str) -> bool:
        try:
            wall_clock_now(name)
            return True
        except ValueError:
            return False

    @staticmethod
    def _tool_session_id(kwargs: dict[str, Any]) -> str:
        return (
            normalize_text(kwargs.get("stream_id"))
            or normalize_text(kwargs.get("chat_id"))
        )

    async def _require_command_admin(
        self, stream_id: str, kwargs: dict[str, Any]
    ) -> tuple[bool, str, int] | None:
        platform, user_id, is_local = command_caller_identity(kwargs)
        if is_admin_user(
            platform,
            user_id,
            self.config.plugin.admin_user_ids,
            is_local_operator=is_local,
        ):
            return None
        if not self.config.plugin.admin_user_ids:
            text = "尚未配置管理员，无法使用麦麦生活命令。请在插件设置中填写管理员。"
        else:
            text = "只有管理员可以使用麦麦生活命令"
        await self._reply(stream_id, text)
        return True, "没有权限", 2

    async def _reply(self, stream_id: str, text: str) -> None:
        if not stream_id:
            return
        await self.ctx.send.text(text, stream_id)


KNOWLEDGE_SEARCH_QUERY = "角色最近的习惯、约定和日程相关记忆"


def command_caller_identity(kwargs: dict[str, Any]) -> tuple[str, str, bool]:
    """从命令调用上下文取出 platform、user_id，以及是否为本地控制台操作员。"""

    message = kwargs.get("message")
    message_dict = message if isinstance(message, dict) else {}
    message_info = message_dict.get("message_info") if isinstance(message_dict.get("message_info"), dict) else {}
    user_info = message_info.get("user_info") if isinstance(message_info.get("user_info"), dict) else {}
    platform = (
        normalize_text(kwargs.get("platform"))
        or normalize_text(message_dict.get("platform"))
        or normalize_text(message_info.get("platform"))
    )
    user_id = (
        normalize_text(kwargs.get("user_id"))
        or normalize_text(user_info.get("user_id"))
        or normalize_text(message_dict.get("user_id"))
    )
    return platform, user_id, kwargs.get("is_local_operator") is True


def is_admin_user(
    platform: str,
    user_id: str,
    admin_user_ids: list[str] | None,
    *,
    is_local_operator: bool = False,
) -> bool:
    """判断命令调用者是否为管理员。本地控制台操作员始终通过。"""

    if is_local_operator:
        return True
    user_id = normalize_text(user_id)
    if not user_id:
        return False
    platform = normalize_text(platform)
    for item in admin_user_ids or []:
        configured = normalize_text(item)
        if not configured:
            continue
        if ":" not in configured:
            if not platform and configured == user_id:
                return True
            continue
        configured_platform, configured_user_id = configured.split(":", 1)
        configured_platform = configured_platform.strip()
        configured_user_id = configured_user_id.strip()
        if configured_user_id.lower().startswith("private:"):
            configured_user_id = configured_user_id.split(":", 1)[1].strip()
        if (
            platform
            and configured_platform.casefold() == platform.casefold()
            and configured_user_id == user_id
        ):
            return True
    return False


def knowledge_search_args(
    now: datetime,
    *,
    limit: int,
    window_hours: int,
) -> dict[str, Any]:
    """组装记忆检索参数。窗口大于 0 时走 hybrid 并带上时间范围。"""

    payload: dict[str, Any] = {
        "query": KNOWLEDGE_SEARCH_QUERY,
        "limit": max(1, int(limit)),
    }
    hours = int(window_hours)
    if hours <= 0:
        return payload
    end = now.timestamp()
    payload["mode"] = "hybrid"
    payload["time_start"] = end - hours * 3600
    payload["time_end"] = end
    return payload


def compose_persona(*, include_system: bool, system_persona: str, extra_persona: str) -> str:
    """读入人设开启时拼接主程序人设与补充人设，关闭时只用补充人设。"""

    extra = str(extra_persona or "").strip()
    if not include_system:
        return extra
    system = str(system_persona or "").strip()
    if system and extra:
        return f"{system}\n\n补充设定：{extra}"
    return system or extra


def create_plugin() -> MaiLifePlugin:
    """创建插件实例。"""

    return MaiLifePlugin()
