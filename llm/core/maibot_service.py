"""MaiBot-backed LLM service for the legacy WeChat dispatcher.

Only the dispatcher-facing request/response shape is translated here. Message
transport remains in the existing WeChat sender path. MaiBot owns the actual
model client, request scheduling, provider selection and response parsing.
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional


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
        self.config = self._load_legacy_llm_config()
        self.proactive_reply = _ProactiveShim()
        self.ai_plugin_manager = SimpleNamespace(set_reload_callback=lambda *_args, **_kwargs: None)
        self._proactive_callback = None
        self._scheduled_callback = None
        self._client = None

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
            maibot_root = os.environ.get("MAIBOT_ROOT", r"D:\maibot")
            if maibot_root not in sys.path:
                sys.path.insert(0, maibot_root)
            from src.services.llm_service import LLMServiceClient

            self._client = LLMServiceClient(task_name="planner", request_type="wechat.dispatcher")
        return self._client

    def _build_prompt(self, nickname: str, content: str, group_id: str, message_context: Optional[Dict[str, Any]]) -> str:
        identity = self.config.get("identity") or {}
        persona = "。".join(str(identity.get(key) or "").strip() for key in ("name", "role", "style") if identity.get(key))
        style = str(self.config.get("reply_style") or "").strip()
        group_prompt = str(self.config.get("group_prompt") or "").strip()
        private_prompt = str(self.config.get("private_prompt") or "").strip()
        context = message_context or {}
        lines = [
            f"你是 MaiBot 驱动的 WeChat Bot。人格：{persona}",
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
        del wxid, force_reply
        if not self.config.get("enabled", True):
            return {"messages": [], "should_reply": False, "error": "LLM disabled"}
        try:
            result = asyncio.run(self._get_client().generate_response(self._build_prompt(nickname, content, str(group_id or ""), message_context), session_id=str(session_id or group_id or "")))
            text = str(getattr(result, "content", None) or getattr(result, "text", None) or self._extract_result_text(result) or "").strip()
            return {"messages": [{"type": "text", "content": text}] if text else [], "should_reply": bool(text), "animation": None, "maibot_model": getattr(result, "model_name", "")}
        except Exception as exc:
            return {"messages": [], "should_reply": False, "error": f"MaiBot LLM failed: {type(exc).__name__}: {exc}"}

    def handle_batch_message(self, group_id, nickname, content, **kwargs):
        return self.handle_message(group_id, nickname, content, **kwargs)

    def handle_proactive_message(self, *_args, **_kwargs):
        return {"messages": [], "should_reply": False, "animation": None}

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
