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

            return json.loads((Path(__file__).resolve().parents[2] / "config.json").read_text(encoding="utf-8")).get("llm", {})
        except Exception:
            return {"enabled": True, "assistant_nickname": "LLM", "identity": {}}

    def _get_client(self):
        if self._client is None:
            from src.services.llm_service import LLMServiceClient

            self._client = LLMServiceClient(task_name="planner", request_type="wechat.dispatcher")
            self._log("client_created task=planner request_type=wechat.dispatcher client=%s", type(self._client).__name__)
        return self._client

    def _build_prompt(self, nickname: str, content: str, group_id: str, message_context: Optional[Dict[str, Any]]) -> str:
        identity = self.config.get("identity") or {}
        persona = "。".join(str(identity.get(key) or "").strip() for key in ("name", "role", "style") if identity.get(key))
        style = str(self.config.get("reply_style") or "").strip()
        group_prompt = str(self.config.get("group_prompt") or "").strip()
        private_prompt = str(self.config.get("private_prompt") or "").strip()
        bot_nickname = ""
        try:
            from src.config.config import config_manager

            native = config_manager.get_global_config()
            persona_cfg = getattr(native, "personality", None)
            bot_cfg = getattr(native, "bot", None)
            chat_cfg = getattr(native, "chat", None)
            persona = str(getattr(persona_cfg, "personality", persona) or persona)
            style = str(getattr(persona_cfg, "reply_style", style) or style)
            bot_nickname = str(getattr(bot_cfg, "nickname", "") or "")
            if group_id:
                group_prompt = str(getattr(getattr(chat_cfg, "reply_style", None), "group_chat_prompt", group_prompt) or group_prompt)
            else:
                group_prompt = str(getattr(getattr(chat_cfg, "reply_style", None), "private_chat_prompts", private_prompt) or private_prompt)
        except Exception:
            pass
        context = message_context or {}
        lines = [
            f"你是 MaiBot 驱动的 WeChat Bot。机器人名称：{bot_nickname or 'MaiBot'}。人格：{persona}",
            f"回复风格：{style}",
            group_prompt if group_id else private_prompt,
            f"当前用户：{nickname}",
            f"当前群组：{group_id or '私聊'}",
            f"当前消息：{content}",
        ]
        if context.get("quoted_content"):
            lines.append(f"引用内容：{context['quoted_content']}")
        lines.append("只输出最终回复文本，不输出分析过程，不声称发送了消息。")
        return "\n".join(item for item in lines if item)

    def handle_message(self, group_id, nickname, content, wxid="", session_id=None, message_context=None, force_reply=False):
        del message_context, force_reply
        if not self.config.get("enabled", True):
            self._log("request_skipped reason=disabled")
            return {"messages": [], "should_reply": False, "error": "LLM disabled"}
        try:
            self._log("native_request task=maisaka.planner mode=direct group=%s chars=%s", group_id or session_id or "unknown", len(str(content or "")))
            trace_runtime("maibot_native_request", task="maisaka.planner", mode="direct", group=str(group_id or session_id or "unknown"))
            result = self._native_runtime.handle_message(str(group_id or ""), str(nickname or "未知用户"), str(content or ""), str(wxid or ""), str(session_id or "") or None)
            self._log("native_response task=maisaka.planner mode=direct chars=%s", len(str((result.get("messages") or [{}])[0].get("content") or "")))
            return result
        except Exception as exc:
            self._log("error task=planner mode=direct type=%s error=%s", type(exc).__name__, exc)
            trace_runtime("maibot_core_error", task="planner", mode="direct", error=f"{type(exc).__name__}: {exc}")
            return {"messages": [], "should_reply": False, "error": f"MaiBot LLM failed: {type(exc).__name__}: {exc}"}

    def handle_batch_message(self, group_id, messages=None, **kwargs):
        """Convert Dispatcher batch records into one MaiBot-native request."""
        items = [item for item in (messages or []) if isinstance(item, dict)]
        nickname = str((items[-1] if items else {}).get("nickname") or (items[-1] if items else {}).get("user") or "未知用户")
        content = "\n".join(
            f"{item.get('nickname') or item.get('user') or '用户'}: {item.get('content') or ''}" for item in items
        ).strip()
        context = dict(items[-1] if items else {})
        context["batch_messages"] = items
        self._log("request task=planner mode=batch group=%s count=%s chars=%s", group_id or "unknown", len(items), len(content))
        trace_runtime("maibot_core_request", task="planner", mode="batch", group=str(group_id or "unknown"), count=len(items))
        result = self.handle_message(
            group_id,
            nickname,
            content,
            session_id=kwargs.get("session_id") or group_id,
            message_context=context,
            force_reply=bool(kwargs.get("force_reply")),
        )
        result["_llm_ok"] = True
        result["_delivery_batch_messages"] = items
        return result

    def _provider_call(self, messages, *, thinking=None, task_name="utils", **kwargs: Any):
        """Compatibility-shaped auxiliary call; execution remains MaiBot-native."""
        del thinking, kwargs
        prompt = "\n".join(f"{item.get('role', 'user')}: {item.get('content', '')}" for item in messages or [])
        from src.services.llm_service import LLMServiceClient

        client = LLMServiceClient(task_name=task_name, request_type="wechat.auxiliary")
        self._log("request task=%s mode=auxiliary chars=%s", task_name, len(prompt))
        trace_runtime("maibot_core_request", task=task_name, mode="auxiliary")
        result = self._generate_with_retry(prompt, "wechat-auxiliary", client=client)
        text = self._extract_result_text(result)
        self._log("response task=%s mode=auxiliary model=%s chars=%s", task_name, getattr(result, "model_name", "") or "unknown", len(text))
        trace_runtime("maibot_core_response", task=task_name, mode="auxiliary", model=str(getattr(result, "model_name", "") or ""), chars=len(text))
        return SimpleNamespace(content=text)

    def _generate_with_retry(self, prompt: str, session_id: str, *, client=None):
        client = client or self._get_client()
        last_error = None
        for attempt in range(3):
            try:
                return asyncio.run(client.generate_response(prompt, session_id=session_id))
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (attempt + 1))
        raise last_error

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
        return {"messages": [], "should_reply": False, "animation": None}

    def set_proactive_callback(self, callback):
        self._proactive_callback = callback

    def set_scheduled_event_callback(self, callback):
        self._scheduled_callback = callback

    def commit_assistant_delivery(self, result, *_args, **_kwargs):
        return result

    def on_proactive_result(self, *_args, **_kwargs):
        return None

    def curate_cycle(self, *_args, **_kwargs):
        return None

    def _effective_group_config(self, *_args, **_kwargs):
        return {}

    def close(self):
        self._client = None

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
