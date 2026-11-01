"""MaiBot Platform IO driver backed by the Wechat Bot sender."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import os
from pathlib import Path

from src.platform_io.drivers.base import PlatformIODriver
from src.platform_io.types import DeliveryReceipt, DeliveryStatus, DriverDescriptor, DriverKind


class WechatShellDriver(PlatformIODriver):
    def __init__(self, account_id: str, on_delivery=None):
        super().__init__(DriverDescriptor(driver_id="wechatbot-shell", kind=DriverKind.LOCAL,
                                         platform="wechat", account_id=account_id, plugin_id='gsmps.adapter.weflow',
                                         metadata={'name': 'GSMPS Wechat Weflow', 'source': 'GSMPS Wechatbot', 'protocol': 'weflow', 'plugin_type': 'adapter'}))
        self.on_delivery = on_delivery

    async def send_message(self, message, route_key, metadata=None):
        from core.maibot_shell_registry import enabled, ADAPTER_ID
        if not enabled(ADAPTER_ID):
            raise RuntimeError('GSMPS Wechat Weflow adapter is disabled')
        from core.sender import send
        from src.chat.message_receive.chat_manager import chat_manager
        from src.common.data_models.message_component_data_model import (
            AtComponent, DictComponent, EmojiComponent, FileComponent, ImageComponent, ReplyComponent, TextComponent, VoiceComponent,
        )
        group = message.message_info.group_info
        session = chat_manager.get_session_by_session_id(message.session_id)
        recipient_id = group.group_id if group else message.message_info.additional_config.get("platform_io_target_user_id") or (session.user_id if session else None)
        # wxSend is a UI transport and searches display names. Keep native IDs
        # separate from that final UI selector (especially in private chats).
        target = (group.group_name or group.group_id) if group else ((session.user_nickname or session.user_id) if session else recipient_id)
        operations = []
        text_parts = []
        native_mentions = []
        text_segments = []
        quote_mode = None
        dropped_quote_count = 0
        dropped_controls = 0

        def flush_text():
            if text_parts:
                operations.append({"mode": "wechat_text", "content": "".join(text_parts), "preserve_text": True})
                if native_mentions:
                    operations[-1].update(resolve_mentions=True, group_id=group.group_id, native_segments=list(text_segments))
                text_parts.clear()
                native_mentions.clear()
                text_segments.clear()

        try:
            from src.platform_io.manager import PlatformIOManager
            if (PlatformIOManager.build_route_key_from_message(message) != route_key
                    or not PlatformIOManager._driver_matches_route(self, route_key)):
                raise ValueError('Message origin does not match WeChat adapter identity')
            if not target:
                raise ValueError("Missing WeChat recipient ID")
            # Validate the whole component sequence before performing any physical send.
            for component in message.raw_message.components:
                from src.platform_io.message_capabilities import dropped_admin_control
                if dropped_admin_control(component):
                    dropped_controls += 1
                    continue
                if isinstance(component, DictComponent) and component.data.get('type') in {'video', 'videourl'}:
                    payload = component.data.get('data', component.data)
                    if isinstance(payload, str):
                        payload = {'url': payload}
                    if not isinstance(payload, dict):
                        raise ValueError('WeChat video has no usable file payload')
                    component = FileComponent(name=payload.get('name') or 'video.mp4',
                        url=payload.get('url') or payload.get('file') or payload.get('path') or '',
                        base64_data=component.data.get('binary_data_base64') or payload.get('base64') or payload.get('binary_data_base64') or '')
                if isinstance(component, TextComponent):
                    text_parts.append(component.text)
                    text_segments.append({"type": "text", "text": component.text})
                elif isinstance(component, AtComponent):
                    if not group:
                        raise ValueError("WeChat @ mentions require a group chat")
                    if component.target_user_id in {"notify@all", "all", "@all"}:
                        raise ValueError("WeChat shell does not support @all")
                    text_parts.append(f"[[WX_AT:{component.target_user_id}]]")
                    native_mentions.append(component.target_user_id)
                    text_segments.append({"type": "mention", "wxid": component.target_user_id})
                elif isinstance(component, ReplyComponent):
                    # wxSend cannot represent references. Do not turn quoted text
                    # (including its @ labels) into outbound text or mention segments.
                    quote_mode = "dropped_unsupported"
                    dropped_quote_count += 1
                elif isinstance(component, (ImageComponent, EmojiComponent, VoiceComponent)):
                    flush_text()
                    if isinstance(component, EmojiComponent):
                        from .maibot_emoji import native_emoji_file
                        path = await native_emoji_file(component)
                        operations.append({"mode": "wechat_file", "file_path": str(path)})
                        continue
                    data = component.binary_data
                    if not data:
                        loader = component.load_image_binary if isinstance(component, ImageComponent) else component.load_emoji_binary if isinstance(component, EmojiComponent) else component.load_voice_binary
                        await loader()
                        data = component.binary_data
                    suffix = ".wav" if isinstance(component, VoiceComponent) else self._image_suffix(data)
                    path = await asyncio.to_thread(self._cache_bytes, data, suffix)
                    operations.append({"mode": "wechat_voice" if isinstance(component, VoiceComponent) else "wechat_file", "file_path": str(path)})
                elif isinstance(component, FileComponent):
                    flush_text()
                    if component.base64_data:
                        data = base64.b64decode(component.base64_data, validate=True)
                        path = await asyncio.to_thread(self._cache_bytes, data, Path(component.name).suffix or ".bin")
                    elif component.url and Path(component.url).is_file():
                        path = Path(component.url)
                    elif component.url.startswith(("http://", "https://")):
                        from core.weflow_media import download_media
                        data = await asyncio.to_thread(download_media, component.url)
                        path = await asyncio.to_thread(self._cache_bytes, data, Path(component.name).suffix or ".bin")
                    else:
                        raise ValueError(f"File has no downloadable content: {component.name}")
                    operations.append({"mode": "wechat_file", "file_path": str(path)})
                else:
                    raise ValueError(f"Unsupported WeChat outbound component: {type(component).__name__}")
            flush_text()
            if not operations and not dropped_quote_count and not dropped_controls:
                raise ValueError("Empty WeChat outbound message")
            sent_operations = []
            for operation in operations:
                ok, error = await asyncio.to_thread(send, target=target, source_platform=message.platform, **operation)
                if not ok:
                    raise RuntimeError(f"WeChat delivery failed after {len(sent_operations)}/{len(operations)} segments: {error}")
                sent_operations.append(operation)
            receipt = DeliveryReceipt(message.message_id, route_key, DeliveryStatus.SENT if operations else DeliveryStatus.DROPPED,
                                      driver_id=self.driver_id, driver_kind=self.descriptor.kind,
                                      metadata={"operations": operations, "quote_mode": quote_mode, "dropped_quote_count": dropped_quote_count,
                                                "dropped_admin_controls": dropped_controls,
                                                "message_origin": dict(message.message_info.additional_config.get('message_origin') or {}),
                                                "recipient_id": recipient_id, "transport_target": target,
                                                "intercepted": os.environ.get("MAIBOT_INTERCEPT_SEND") == "1"})
        except Exception as exc:
            receipt = DeliveryReceipt(message.message_id, route_key, DeliveryStatus.FAILED,
                                      driver_id=self.driver_id, driver_kind=self.descriptor.kind, error=str(exc))
        if self.on_delivery:
            self.on_delivery(message, receipt)
        return receipt

    @staticmethod
    def _image_suffix(data):
        from io import BytesIO
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            return {"JPEG": ".jpg", "GIF": ".gif", "WEBP": ".webp"}.get(image.format, ".png")

    @staticmethod
    def _cache_bytes(data, suffix):
        from src.common.paths import get_instance_root
        if not data:
            raise ValueError("Empty outbound media")
        path = get_instance_root() / "temp" / "wechat-outbound" / (hashlib.sha256(data).hexdigest() + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path
