"""Embedded MaiBot host. WeChat is a transport driver, not a second chat engine."""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import threading
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

from utils.runtime_trace import trace_runtime
from .wechat_translation import WechatMessageTranslator, wire_fields

logger = logging.getLogger("maibot_core_bridge")


class NativeMaiBotRuntime:
    def __init__(self) -> None:
        self._thread = None
        self._loop = None
        self._ready = threading.Event()
        self._startup_error = None
        self._lock = threading.RLock()
        self._runtimes = {}
        self._outputs = {}
        self._delivery_seq = {}
        self._seen = OrderedDict()
        self._ingest_lock = None
        self._watchers = set()
        self._background = []
        self._translator = None
        self._delivery_failures = {}
        self._webui_server = None

    def _ensure_thread(self) -> None:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                self._ready.clear()
                self._startup_error = None
                self._thread = threading.Thread(target=self._thread_main, name="maibot-native-core", daemon=True)
                self._thread.start()
        if not self._ready.wait(120):
            raise TimeoutError("MaiBot native runtime startup timed out")
        if self._startup_error:
            raise RuntimeError("MaiBot native runtime startup failed") from self._startup_error

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._bootstrap())
            self._ready.set()
            loop.run_forever()
        except BaseException as exc:
            self._startup_error = exc
            logger.exception("MaiBot host failed")
            self._ready.set()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    async def _bootstrap(self):
        from src.common.runtime_loop import set_main_loop
        from src.common.database.database import initialize_database
        from src.chat.message_receive.chat_manager import chat_manager
        from src.chat.heart_flow.heartflow_manager import heartflow_manager
        from src.services.memory_flow_service import memory_automation_service
        from src.platform_io.manager import get_platform_io_manager
        from src.platform_io.types import RouteBinding, RouteKey
        from src.config.config import config_manager
        from src.prompt.prompt_manager import prompt_manager
        from src.emoji_system.emoji_manager import emoji_manager
        from src.A_memorix.host_service import a_memorix_host_service
        from src.services.expression_vector_service import expression_vector_service
        from src.plugin_runtime.integration import get_plugin_runtime_manager
        from src.agent.service import agent_service
        from src.mcp_module.service import get_mcp_service
        from .wechat_driver import WechatShellDriver

        from src.common.paths import get_instance_root
        # Native QQ voice codecs discover ffmpeg through PATH. Reuse WeFlow's
        # existing binary in this process and its plugin children only.
        import os
        import shutil
        ffmpeg_dir = get_instance_root() / 'weflow-core' / 'node_modules' / 'ffmpeg-static'
        if not shutil.which('ffmpeg') and (ffmpeg_dir / 'ffmpeg.exe').is_file():
            os.environ['PATH'] = str(ffmpeg_dir) + os.pathsep + os.environ.get('PATH', '')
        config = json.loads((get_instance_root() / "config.json").read_text(encoding="utf8"))
        self._translator = WechatMessageTranslator(config)
        self._ingest_lock = asyncio.Lock()
        set_main_loop(asyncio.get_running_loop())
        initialize_database()
        await chat_manager.initialize()
        self._runtimes = heartflow_manager.heartflow_chat_list
        manager = get_platform_io_manager()
        self._driver = WechatShellDriver(self._translator.account_id, self._on_delivery)
        await manager.add_driver(self._driver)
        binding = RouteBinding(RouteKey("wechat", self._translator.account_id), self._driver.driver_id, self._driver.descriptor.kind)
        manager.bind_send_route(binding)
        manager.bind_receive_route(binding)
        await manager.ensure_send_pipeline_ready()
        await asyncio.to_thread(prompt_manager.load_prompts)
        await asyncio.to_thread(emoji_manager.load_emojis_from_db)
        a_memorix_host_service.register_config_reload_callback()
        await a_memorix_host_service.start()
        expression_vector_service.start()
        await memory_automation_service.start()
        plugin_runtime = get_plugin_runtime_manager()
        plugin_runtime.ensure_builtin_hook_specs_registered()
        await plugin_runtime.start()
        from src.config.config import global_config
        if global_config.plugin_runtime.enabled and not plugin_runtime._started:
            raise RuntimeError("MaiBot plugin runtime failed to start")
        get_mcp_service().register_config_reload_callback()
        await agent_service.start()
        await config_manager.start_file_watcher()
        if global_config.webui.enabled:
            from src.webui.webui_server import get_threaded_webui_server
            self._webui_server = get_threaded_webui_server()
            self._webui_server.start()
        # Record values read by this running core, rather than only on-disk config.
        # Hash persona text so the operational trace need not expose its contents.
        from src.config.config import model_config
        trace_runtime("maibot_native_config_loaded",
            nickname=global_config.bot.nickname,
            persona_sha256=hashlib.sha256(global_config.personality.personality.encode("utf-8")).hexdigest(),
            behavior_sha256=hashlib.sha256(global_config.personality.behavior_style.encode("utf-8")).hexdigest(),
            reply_style_sha256=hashlib.sha256(global_config.personality.reply_style.encode("utf-8")).hexdigest(),
            planner_models=",".join(model_config.model_task_config.planner.model_list),
            utils_models=",".join(model_config.model_task_config.utils.model_list),
            plugin_runtime_enabled=global_config.plugin_runtime.enabled,
            learning_exclusions=len(global_config.learning.excluded_users))
        from src.core.event_bus import event_bus
        from src.core.types import EventType
        await event_bus.emit(event_type=EventType.ON_START)
        self._background.append(asyncio.create_task(chat_manager.regularly_save_sessions(), name="wechat-session-save"))
        self._background.append(asyncio.create_task(emoji_manager.periodic_emoji_maintenance(), name="wechat-emoji-maintenance"))
        trace_runtime("maibot_native_ready", account=self._translator.account_id, driver=self._driver.driver_id)

    def _on_delivery(self, message, receipt):
        from src.platform_io.types import DeliveryStatus
        if receipt.status == DeliveryStatus.SENT:
            self._delivery_seq[message.session_id] = self._delivery_seq.get(message.session_id, 0) + 1
            self._outputs.setdefault(message.session_id, deque(maxlen=256)).append({
                "sequence": self._delivery_seq[message.session_id],
                "message_id": message.message_id, "text": message.processed_plain_text,
                "operations": receipt.metadata.get("operations", []), "quote_mode": receipt.metadata.get("quote_mode"),
            })
        else:
            self._delivery_failures[message.session_id] = receipt.error
        trace_runtime("maibot_native_delivery", session=message.session_id, status=receipt.status.value, error=receipt.error)

    def submit(self, coro, timeout=240):
        try:
            self._ensure_thread()
        except BaseException:
            coro.close()
            raise
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"MaiBot host operation exceeded {timeout}s") from None

    async def _ingest(self, contexts, *, wait_for_completion=False, timeout=180, observe_only=False):
        from src.chat.message_receive.bot import chat_bot
        from src.chat.message_receive.chat_manager import chat_manager
        from src.common.utils.utils_message import MessageUtils
        from src.services.memory_flow_service import memory_automation_service
        from src.services.bot_account_service import get_bot_accounts
        from src.platform_io.manager import get_platform_io_manager
        accepted, skipped, session_ids = [], [], set()
        dispositions = {}
        before = {}
        async with self._ingest_lock:
            for context in contexts:
                f = wire_fields(context)
                source = f.get("source_kind")
                if f.get("is_bot") or f.get("isSend") in (1, "1") or source in {"bot_echo_external", "bot_echo", "bot_outgoing"} or (f.get("wxid") or f.get("senderWxid")) in get_bot_accounts("wechat"):
                    skipped.append("bot_echo")
                    continue
                message = await self._translator.translate(context)
                activity = context.get('_host_plugin_activity')
                if isinstance(activity, dict):
                    message.message_info.additional_config['plugin_activity'] = dict(activity)
                    message.message_info.additional_config['learnable'] = False
                manager = get_platform_io_manager()
                route = manager.build_route_key_from_message(message)
                info = message.message_info
                chat_type = "group" if info.group_info else "private"
                target_id = info.group_info.group_id if info.group_info else info.user_info.user_id
                dedupe_key = (route, target_id, message.message_id)
                if dedupe_key in self._seen or not await manager.is_target_allowed(route, chat_type=chat_type, target_id=target_id):
                    skipped.append("duplicate_or_policy")
                    continue
                sid = getattr(message, "session_id", None)
                if not sid:
                    from src.common.utils.utils_session import SessionUtils
                    sid = SessionUtils.calculate_session_id("wechat", user_id=message.message_info.user_info.user_id,
                        group_id=message.message_info.group_info.group_id if message.message_info.group_info else None,
                        account_id=message.account_id, scope=message.scope)
                    message.session_id = sid
                before.setdefault(sid, self._delivery_seq.get(sid, 0))
                self._delivery_failures.pop(sid, None)
                if context.get("force_reply") or context.get("prefix_used") or context.get("directed_to_bot") or context.get("reply_to_bot"):
                    # A shell prefix is a native forced trigger, not a fabricated @ component.
                    message.message_info.additional_config["force_reply"] = True
                self._apply_chat_profile(message)
                if observe_only or message.is_notify or f.get("prompt_visible") is False:
                    message.is_notify = bool(message.is_notify)
                    await message.process(enable_heavy_media_analysis=False, enable_voice_transcription=False)
                    chat_manager.register_message(message)
                    info = message.message_info
                    await chat_manager.get_or_create_session("wechat", info.user_info.user_id,
                        info.group_info.group_id if info.group_info else None, account_id=message.account_id)
                    await MessageUtils.store_message_to_db_async(message)
                    runtime = self._runtimes.get(message.session_id)
                    if activity and runtime is not None and not any(
                            getattr(item, 'message_id', None) == message.message_id for item in runtime._chat_history):
                        observed = await runtime._reasoning_engine._build_history_message(
                            message, source_kind='plugin_trigger')
                        if observed is not None:
                            runtime._reasoning_engine._insert_chat_history_message(observed)
                    if f.get("learnable") is not False and not message.is_notify:
                        await memory_automation_service.on_incoming_message(message)
                    disposition = "observed"
                else:
                    disposition = await chat_bot.receive_message(message, raise_errors=True)
                self._seen[dedupe_key] = True
                while len(self._seen) > 10000:
                    self._seen.popitem(last=False)
                accepted.append(message.message_id)
                dispositions[message.message_id] = disposition
                if disposition == "ingested":
                    session_ids.add(message.session_id)
                trace_runtime("maibot_native_ingested", session=message.session_id, message_id=message.message_id,
                              components=",".join(type(c).__name__ for c in message.raw_message.components))
        result = {"messages": [], "should_reply": False, "animation": None, "maibot_native": True,
                  "native_delivery_owned": True, "accepted": accepted, "skipped": skipped, "dispositions": dispositions, "_llm_ok": True}
        if not session_ids:
            return result
        if wait_for_completion:
            result.update(await self._wait_for_turn(session_ids, before, timeout))
        else:
            task = asyncio.create_task(self._watch_turn(session_ids, before, timeout), name="wechat-turn-result")
            self._watchers.add(task)
            task.add_done_callback(self._watchers.discard)
        return result

    def _apply_chat_profile(self, message):
        """Retire old bridge prompts; native WebUI alone owns conversational policy."""
        from src.config.config import global_config
        items = global_config.chat.reply_style.chat_prompts
        items[:] = [p for p in items if not p.prompt.startswith('[WechatBot chat profile]\n')]

    async def _wait_for_turn(self, session_ids, before, timeout):
        import time
        from src.config.config import config_manager
        from src.llm_models.quota_cooldown import task_retry_after
        started = time.monotonic()
        while time.monotonic() - started < timeout:
            await asyncio.sleep(0.05)
            deliveries = [item for sid in session_ids for item in self._outputs.get(sid, []) if item["sequence"] > before.get(sid, 0)]
            for sid in session_ids:
                if self._delivery_failures.get(sid):
                    return {"_llm_ok": False, "error": self._delivery_failures[sid], "native_deliveries": deliveries}
                runtime = self._runtimes.get(sid)
                cooldown = getattr(getattr(runtime, '_reasoning_engine', None), '_model_cooldown_error', None)
                if cooldown is not None:
                    retry_after = task_retry_after(config_manager.get_model_config(), cooldown.task_name)
                    if retry_after > 0:
                        return {"_llm_ok": False, "error": cooldown.message, "retry_after": retry_after,
                                "native_deliveries": deliveries, "native_sent": bool(deliveries)}
                task = runtime._internal_loop_task if runtime else None
                if task and task.done() and not task.cancelled() and task.exception():
                    exc = task.exception()
                    return {"_llm_ok": False, "error": f"{type(exc).__name__}: {exc}", "native_deliveries": deliveries}
            if deliveries:
                return {"native_deliveries": deliveries, "native_sent": True}
            runtimes = [self._runtimes[s] for s in session_ids if s in self._runtimes]
            if runtimes and all(r._agent_state in {"stop", "wait"} and r._get_pending_message_count() == 0
                                 and not r._message_turn_scheduled
                                 and not getattr(getattr(r, '_reasoning_engine', None), '_model_cooldown_error', None)
                                 for r in runtimes):
                return {"native_deliveries": [], "native_sent": False, "completed_without_reply": True}
        return {"_llm_ok": False, "error": f"MaiBot turn exceeded {timeout}s", "native_deliveries": []}

    async def _watch_turn(self, session_ids, before, timeout):
        result = await self._wait_for_turn(session_ids, before, timeout)
        trace_runtime("maibot_native_turn_finished", sessions=",".join(session_ids), ok=result.get("_llm_ok", True),
                      error=result.get("error"), sent=result.get("native_sent", False))

    def handle_contexts(self, contexts, **kwargs):
        return self.submit(self._ingest(contexts, **kwargs), timeout=float(kwargs.get("timeout", 180)) + 30)

    async def record_plugin_delivery(self, execution, delivery):
        # UI sends are serialized, and their context commits must preserve the
        # same order even when several Worker threads enqueue confirmations.
        async with self._ingest_lock:
            return await self._record_plugin_delivery(execution, delivery)

    async def _record_plugin_delivery(self, execution, delivery):
        """Persist plugin output as an observation, never a Planner decision."""
        import uuid
        from pathlib import Path
        from src.chat.message_receive.chat_manager import chat_manager
        from src.common.utils.utils_message import MessageUtils
        from src.common.message_provenance import stamp_message
        context = execution.get('context') or {}
        if delivery.get('target') not in {context.get('group'), context.get('user'), context.get('sessionId')}:
            trace_runtime('plugin_delivery_context_skipped', reason='different_target', plugin=execution['plugin_id'])
            return
        text = str(delivery.get('content') or '')
        if not text:
            text = '[插件发送媒体：' + Path(str(delivery.get('file_path') or '')).name + ']'
        clean = dict(group=context.get('group'), sessionId=context.get('sessionId'),
            is_private=context.get('is_private', False), wxid=self._translator.bot_user_id,
            user='MaiBot 插件', content=text, serverId='shell-plugin-' + uuid.uuid4().hex,
            source_kind='plugin_outbound', learnable=False, prompt_visible=True, platform='wechat')
        message = await self._translator.translate(clean)
        message.platform_message_id = None  # UI sender has no authoritative platform ID receipt.
        message.message_info.additional_config['plugin_activity'] = {
            **{k: str(execution.get(k) or '') for k in ('plugin_id', 'trigger_message_id', 'invocation_id')},
            'kind': 'plugin_output'}
        message.message_info.additional_config['learnable'] = False
        stamp_message(message, direction='outbound', transport='wechatbot-shell')
        await message.process(enable_heavy_media_analysis=False, enable_voice_transcription=False)
        chat_manager.register_message(message)
        await MessageUtils.store_message_to_db_async(message)
        runtime = self._runtimes.get(message.session_id)
        if runtime is not None:
            runtime.append_sent_message_to_chat_history(message, source_kind='plugin_output')
        trace_runtime('plugin_delivery_context_recorded', message_id=message.message_id,
                      plugin=execution['plugin_id'], platform=message.platform)

    def handle_message(self, group_id, nickname, content, wxid="", session_id=None, message_context=None,
                       force_reply=False, wait_for_completion=True):
        context = dict(message_context or {})
        context.update(group=group_id, user=nickname, content=content, wxid=wxid or context.get("wxid"),
                       sessionId=session_id or context.get("sessionId"), force_reply=force_reply)
        return self.handle_contexts([context], wait_for_completion=wait_for_completion)

    async def _shutdown(self):
        if self._webui_server is not None:
            await self._webui_server.shutdown()
            self._webui_server = None
        from src.chat.heart_flow.heartflow_manager import heartflow_manager
        from src.services.memory_flow_service import memory_automation_service
        from src.A_memorix.host_service import a_memorix_host_service
        from src.plugin_runtime.integration import get_plugin_runtime_manager
        from src.agent.service import agent_service
        from src.platform_io.manager import get_platform_io_manager
        from src.config.config import config_manager
        from src.services.expression_vector_service import expression_vector_service
        from src.mcp_module.service import get_mcp_service
        for task in [*self._background, *self._watchers]:
            task.cancel()
        await asyncio.gather(*self._background, *self._watchers, return_exceptions=True)
        await heartflow_manager.shutdown()
        await agent_service.stop()
        await memory_automation_service.shutdown()
        await expression_vector_service.stop()
        await get_mcp_service().close()
        await a_memorix_host_service.stop()
        from src.emoji_system.emoji_manager import emoji_manager
        from src.chat.image_system.image_manager import image_manager
        emoji_manager.shutdown()
        await image_manager.shutdown()
        await get_plugin_runtime_manager().bridge_event("on_stop")
        await get_plugin_runtime_manager().stop()
        await get_platform_io_manager().stop()
        await config_manager.stop_file_watcher()

    def close(self):
        if self._loop and self._thread and self._thread.is_alive():
            self.submit(self._shutdown(), timeout=45)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)


native_runtime = NativeMaiBotRuntime()
