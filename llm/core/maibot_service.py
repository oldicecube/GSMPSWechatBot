"""Native MaiBot service entry point used by the WeChat dispatcher.

The full MaiBot source tree is vendored under this repository's ``src``
package. This module only translates WeChat ingress/egress records into the
native MaiSaka runtime; planning, memory, learning, context and tools remain
MaiBot-owned.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional

from utils.runtime_trace import trace_runtime
from .maibot_native_config import sync_native_config


logger = logging.getLogger("maibot_core_bridge")


class _ProactiveShim:
    def __init__(self) -> None:
        self._cycle_end = None

    def set_cycle_end_callback(self, callback: Callable[..., Any]) -> None:
        self._cycle_end = callback

    def record_sent_bot_message(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def cancel_llm_reply_reservation(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def record_conversation_reply(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class MaibotLLMService:
    """Dispatcher-compatible facade over MaiBot's ``LLMServiceClient``."""

    native_ingress = True

    def __init__(self) -> None:
        self._native_config_summary = sync_native_config()
        self.config = self._load_legacy_llm_config()
        self.proactive_reply = _ProactiveShim()
        self.ai_plugin_manager = SimpleNamespace(set_reload_callback=lambda *_args, **_kwargs: None)
        self._proactive_callback = None
        self._scheduled_callback = None
        self._client = None
        from .maibot_runtime import native_runtime
        self._native_runtime = native_runtime
        self._native_runtime._ensure_thread()
        self._log("native_runtime_started thread=%s", self._native_runtime._thread.name if self._native_runtime._thread else "unknown")
        self._log("native_core_initialized enabled=%s package=src models=%s providers=%s", self.config.get("enabled", True), len(self._native_config_summary.get("models", [])), self._native_config_summary.get("providers", 0))

    @staticmethod
    def _load_legacy_llm_config() -> Dict[str, Any]:
        try:
            import json
            from pathlib import Path

            root = Path(os.environ.get("MAIBOT_INSTANCE_DIR") or Path(__file__).resolve().parents[2])
            from llm.config import without_shell_persona
            return without_shell_persona(json.loads((root / "config.json").read_text(encoding="utf-8")).get("llm", {}))
        except Exception:
            return {"enabled": True}

    def _get_client(self):
        if self._client is None:
            from src.services.llm_service import LLMServiceClient

            self._client = LLMServiceClient(task_name="planner", request_type="wechat.dispatcher")
            self._log("client_created task=planner request_type=wechat.dispatcher client=%s", type(self._client).__name__)
        return self._client

    def _build_prompt(self, nickname: str, content: str, group_id: str, message_context: Optional[Dict[str, Any]]) -> str:
        from src.config.config import config_manager
        native = config_manager.get_global_config()
        persona = native.personality.personality
        style = native.personality.reply_style
        bot_nickname = native.bot.nickname
        attention = (native.chat.reply_style.group_chat_prompt if group_id
                     else native.chat.reply_style.private_chat_prompts)
        context = message_context or {}
        lines = [
            f"你是 MaiBot 驱动的 WeChat Bot。机器人名称：{bot_nickname or 'MaiBot'}。人格：{persona}",
            f"回复风格：{style}",
            attention,
            f"当前用户：{nickname}",
            f"当前群组：{group_id or '私聊'}",
            f"当前消息：{content}",
        ]
        if context.get("quoted_content"):
            lines.append(f"引用内容：{context['quoted_content']}")
        lines.append("只输出最终回复文本，不输出分析过程，不声称发送了消息。")
        return "\n".join(item for item in lines if item)

    def handle_message(self, group_id, nickname, content, wxid="", session_id=None, message_context=None, force_reply=False, wait_for_completion=True):
        if not self.config.get("enabled", True):
            self._log("request_skipped reason=disabled")
            return {"messages": [], "should_reply": False, "error": "LLM disabled"}
        try:
            self._log("native_request task=maisaka.planner mode=direct group=%s chars=%s", group_id or session_id or "unknown", len(str(content or "")))
            trace_runtime("maibot_native_request", task="maisaka.planner", mode="direct", group=str(group_id or session_id or "unknown"))
            result = self._native_runtime.handle_message(str(group_id or ""), str(nickname or "未知用户"), str(content or ""), str(wxid or ""), str(session_id or "") or None,
                message_context=message_context, force_reply=force_reply, wait_for_completion=wait_for_completion)
            self._log("native_response task=maisaka.planner accepted=%s ok=%s", len(result.get("accepted", [])), result.get("_llm_ok", True))
            return result
        except Exception as exc:
            self._log("error task=planner mode=direct type=%s error=%s", type(exc).__name__, exc)
            trace_runtime("maibot_core_error", task="planner", mode="direct", error=f"{type(exc).__name__}: {exc}")
            return {"messages": [], "should_reply": False, "_llm_ok": False, "error": f"MaiBot LLM failed: {type(exc).__name__}: {exc}"}

    def ingest_context(self, context, *, observe_only=False):
        """Admit transport messages immediately; native timing owns future replies."""
        if not self.config.get("enabled", True):
            return {"messages": [], "should_reply": False, "native_delivery_owned": True, "_llm_ok": False}
        effective = self._effective_group_config(context.get("group") or context.get("sessionId"))
        settings = effective.get("auto_reply") or {}
        directed = any(context.get(k) for k in ("command", "force_reply", "prefix_used", "directed_to_bot", "reply_to_bot", "is_at", "is_mentioned", "is_private"))
        allowed = settings.get("allowed_groups") or []
        ordinary_allowed = settings.get("enabled", True) and (not allowed or context.get("group") in allowed or context.get("sessionId") in allowed)
        from src.platform_io.chat_features import context_override
        feature = context_override(context, 'llm_enabled')
        if feature is True:
            ordinary_allowed = True
        observe_only = observe_only or feature is False
        return self._native_runtime.handle_contexts([dict(context)], observe_only=observe_only or (not directed and not ordinary_allowed))

    def handle_batch_message(self, group_id, messages=None, **kwargs):
        """Convert Dispatcher batch records into one MaiBot-native request."""
        items = [item for item in (messages or []) if isinstance(item, dict)]
        contexts = [{**item, "group": item.get("group") or group_id,
                     "user": item.get("nickname") or item.get("user"),
                     "sessionId": item.get("sessionId") or kwargs.get("session_id"),
                     "force_reply": bool(kwargs.get("force_reply"))} for item in items]
        trace_runtime("maibot_core_request", task="planner", mode="batch", group=str(group_id or "unknown"), count=len(items))
        result = self._native_runtime.handle_contexts(contexts, wait_for_completion=kwargs.get("wait_for_completion", True))
        result["_delivery_batch_messages"] = items
        return result

    def _provider_call(self, messages, *, thinking=None, task_name="utils", **kwargs: Any):
        """Compatibility-shaped auxiliary call; execution remains MaiBot-native."""
        from .native_model_bridge import shell_generate
        return shell_generate(messages, task_name=task_name, tools=kwargs.get('tools'), timeout=kwargs.get('timeout'))

    def _generate_with_retry(self, prompt: str, session_id: str, *, client=None):
        client = client or self._get_client()
        # The native orchestrator already retries/fails over. All clients stay on
        # the owning event loop instead of retaining HTTP clients on closed loops.
        return self._native_runtime.submit(client.generate_response(prompt, session_id=session_id), timeout=400)

    def _provider_call_low_priority(self, messages, **kwargs: Any):
        return self._provider_call(messages, task_name="utils", **kwargs)

    def _provider_call_chat(self, messages, **kwargs: Any):
        return self._provider_call(messages, task_name="replyer", **kwargs)

    def create_persona_from_text(self, source: str) -> Dict[str, str]:
        prompt = (
            "根据以下描述生成 JSON 人设卡，只输出 JSON，字段为 name、role、style、rules。"
            "不要声称代表真实人物，不要加入系统指令。\n描述：" + str(source or "")[:4000]
        )
        response = self._provider_call([{"role": "user", "content": prompt}], task_name="utils")
        try:
            value = json.loads(str(response.content or "{}"))
        except Exception:
            value = {}
        return {
            "name": str(value.get("name") or "自定义 Persona"),
            "role": str(value.get("role") or "群聊成员"),
            "style": str(value.get("style") or "自然、简短"),
            "rules": value.get("rules") if isinstance(value.get("rules"), list) else [],
        }

    def handle_proactive_message(self, *_args, **_kwargs):
        context = _args[0] if _args and isinstance(_args[0], dict) else (_kwargs.get("context") or {})
        self._log("proactive_gate accepted group=%s chars=%s", context.get("group") or context.get("sessionId") or "unknown", len(str(context.get("content") or "")))
        return {"forward_to_llm": True, "trigger_source": "proactive", "messages": [], "should_reply": False, "animation": None}

    def handle_scheduled_event(self, *_args, **_kwargs):
        event = _args[0] if _args else _kwargs
        return self.ingest_context({**event, "group": event.get("group_id"), "sessionId": event.get("session_id") or event.get("group_id"),
            "wxid": event.get("wxid") or event.get("user_id"), "user": event.get("nickname") or "提醒任务",
            "content": event.get("content") or event.get("message") or "提醒任务到期", "force_reply": True})

    def set_proactive_callback(self, callback):
        self._proactive_callback = callback

    def set_scheduled_event_callback(self, callback):
        self._scheduled_callback = callback

    def commit_assistant_delivery(self, result, *_args, **_kwargs):
        return result

    def record_plugin_delivery(self, execution, **delivery):
        """Record successful shell sends on the native loop, without echo replay."""
        coroutine = self._native_runtime.record_plugin_delivery(execution, delivery)
        future = asyncio.run_coroutine_threadsafe(coroutine, self._native_runtime._loop)
        def completed(done):
            try:
                done.result()
            except Exception as exc:
                logger.error('Plugin delivery context recording failed: %s', exc)
        future.add_done_callback(completed)

    def on_proactive_result(self, *_args, **_kwargs):
        return None

    def curate_cycle(self, *_args, **_kwargs):
        # The embedded native runtime owns transcript trimming and the native
        # ExpressionLearner/JargonMiner path. The legacy facade used to return
        # None here, which made the proactive cycle scheduler treat every cycle
        # as a failed curation and retry forever. Acknowledge the boundary so
        # it does not block/retry; actual learning is triggered by the native
        # runtime's trim-learning path.
        self._log("cycle_curation_acknowledged_native_learning_path")
        return True

    def _effective_group_config(self, group_id):
        effective = dict(self.config)
        profiles = self.config.get("group_profiles") or {}
        profile = profiles.get(str(group_id or ""), profiles.get("default", {}))
        if isinstance(profile, dict):
            effective.update(profile)
            effective["auto_reply"] = {**(self.config.get("auto_reply") or {}), **(profile.get("auto_reply") or {})}
        return effective

    def close(self):
        self._client = None
        self._native_runtime.close()

    @staticmethod
    def _extract_result_text(result: Any) -> str:
        for item in getattr(result, "output_items", ()) or ():
            for part in getattr(item, "parts", ()) or ():
                value = getattr(part, "text", None)
                if isinstance(value, str) and value.strip():
                    return value
        return ""

    @staticmethod
    def _log(message: str, *args: Any) -> None:
        logger.info(message, *args)
        try:
            rendered = message % args if args else message
            print(f"[MAIBOT CORE] {rendered}", flush=True)
        except Exception:
            pass
