"""Embedded native MaiBot/MaiSaka runtime for the WeChat adapter.

The runtime is hosted inside the Wechat Bot process.  It owns chat history,
memory, learning, turn scheduling, planner/replyer policy and native tools;
the adapter only converts WeChat records at ingress and egress.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import uuid
from datetime import datetime, timezone
from typing import Any


class NativeMaiBotRuntime:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._runtimes: dict[str, Any] = {}
        self._outputs: dict[str, list[str]] = {}
        self._lock = threading.RLock()
        self._send_installed = False

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._ready.clear()
            self._startup_error = None
            self._thread = threading.Thread(target=self._thread_main, name="maibot-native-core", daemon=True)
            self._thread.start()
        if not self._ready.wait(90):
            raise TimeoutError("MaiBot native runtime startup timed out")
        if self._startup_error:
            raise RuntimeError("MaiBot native runtime startup failed") from self._startup_error

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._bootstrap())
            self._ready.set()
            loop.run_forever()
        except BaseException as exc:  # pragma: no cover - startup diagnostics
            self._startup_error = exc
            self._ready.set()
        finally:
            loop.close()

    async def _bootstrap(self) -> None:
        from src.common.database.database import initialize_database
        from src.chat.message_receive.chat_manager import chat_manager
        from src.services.memory_flow_service import memory_automation_service

        initialize_database()
        await chat_manager.initialize()
        await memory_automation_service.start()
        self._install_send_bridge()

    def _install_send_bridge(self) -> None:
        if self._send_installed:
            return
        from src.services import send_service

        original = send_service.send_session_message_with_message
        owner = self

        async def send_session_message_with_message(message, *args, **kwargs):
            platform = str(getattr(message, "platform", "") or "")
            if platform != "wechat":
                return await original(message, *args, **kwargs)
            group = getattr(getattr(message, "message_info", None), "group_info", None)
            user = getattr(getattr(message, "message_info", None), "user_info", None)
            target = getattr(group, "group_id", None) or getattr(user, "user_id", None) or ""
            text = str(getattr(message, "processed_plain_text", None) or "").strip()
            if not text:
                text = "".join(str(getattr(item, "text", "") or "") for item in getattr(getattr(message, "raw_message", None), "components", ()) or ()).strip()
            session_id = str(getattr(message, "session_id", "") or "")
            owner._outputs.setdefault(session_id, []).append(text)
            if str(__import__("os").environ.get("MAIBOT_INTERCEPT_SEND", "")).lower() not in {"1", "true", "yes", "on"}:
                from core.sender import send

                sent, error = send(target, text, mode="wechat_text")
                if not sent:
                    return None
            runtime = owner._runtimes.get(session_id)
            if runtime is not None:
                try:
                    runtime.append_sent_message_to_chat_history(message)
                except Exception:
                    pass
            return message

        send_service.send_session_message_with_message = send_session_message_with_message
        self._send_installed = True

    def submit(self, coro):
        self._ensure_thread()
        assert self._loop is not None
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=180)

    async def _handle_message_async(self, group_id: str, nickname: str, content: str, wxid: str, session_id: str | None) -> dict[str, Any]:
        from src.chat.message_receive.chat_manager import chat_manager
        from src.chat.message_receive.message import SessionMessage
        from src.common.data_models.mai_message_data_model import GroupInfo, MessageInfo, UserInfo
        from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
        from src.maisaka.runtime import MaisakaHeartFlowChatting

        group = str(group_id or "")
        user = str(wxid or nickname or "wechat-user")
        session = await chat_manager.get_or_create_session(
            platform="wechat",
            user_id=user,
            group_id=group or None,
            account_id="wechatbot",
        )
        if group:
            session.group_name = group
        native_session_id = str(session_id or session.session_id)
        if native_session_id != session.session_id:
            native_session_id = session.session_id
        message_id = f"wechat:{uuid.uuid4().hex}"
        message = SessionMessage(
            message_id=message_id,
            timestamp=datetime.now(timezone.utc).replace(tzinfo=None),
            received_at=datetime.now(timezone.utc).replace(tzinfo=None),
            platform="wechat",
        )
        message.session_id = native_session_id
        message.account_id = "wechatbot"
        message.scope = None
        message.message_info = MessageInfo(
            user_info=UserInfo(user_id=user, user_nickname=str(nickname or user)),
            group_info=GroupInfo(group_id=group, group_name=group) if group else None,
        )
        message.raw_message = MessageSequence(components=[TextComponent(text=str(content or ""))])
        await message.process(enable_heavy_media_analysis=False, enable_voice_transcription=False)
        chat_manager.register_message(message)
        runtime = self._runtimes.get(native_session_id)
        if runtime is None:
            runtime = MaisakaHeartFlowChatting(native_session_id)
            self._runtimes[native_session_id] = runtime
            self._outputs[native_session_id] = []
            await runtime.start()
        before = len(self._outputs.get(native_session_id, []))
        await runtime.register_message(message)
        for _ in range(1800):
            await asyncio.sleep(0.1)
            values = self._outputs.get(native_session_id, [])
            if len(values) > before:
                text = values[-1]
                return {"messages": [{"type": "text", "content": text}], "should_reply": bool(text), "animation": None, "maibot_native": True}
        return {"messages": [], "should_reply": False, "animation": None, "error": "MaiBot native runtime timed out"}

    def handle_message(self, group_id: str, nickname: str, content: str, wxid: str = "", session_id: str | None = None) -> dict[str, Any]:
        return self.submit(self._handle_message_async(group_id, nickname, content, wxid, session_id))


native_runtime = NativeMaiBotRuntime()
