"""Translate transport fields, without replacing MaiBot's conversation policy."""
from __future__ import annotations

import base64
import hashlib
import html
import re
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any


def wire_fields(context: dict) -> dict:
    raw = context.get("raw") if isinstance(context.get("raw"), dict) else {}
    original = raw.get("_raw") if isinstance(raw.get("_raw"), dict) else {}
    return {**original, **raw, **{k: v for k, v in context.items() if v is not None}}


def local_datetime(value: Any, default: datetime) -> datetime:
    """Native timestamps are local naive datetimes, not UTC stripped of its zone."""
    if isinstance(value, datetime):
        return value.astimezone().replace(tzinfo=None) if value.tzinfo else value
    try:
        stamp = float(value)
        if stamp > 100_000_000_000:
            stamp /= 1000
        return datetime.fromtimestamp(stamp) if stamp > 0 else default
    except (TypeError, ValueError, OverflowError, OSError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return local_datetime(parsed, default)
        except ValueError:
            return default


def external_id(fields: dict, session: str) -> str:
    server = fields.get("serverId") or fields.get("server_id") or fields.get("svrid") or fields.get("rawid")
    if server and str(server) != "0":
        return str(server)
    key = fields.get("messageKey") or fields.get("message_id")
    if key:
        return str(key)
    local = fields.get("localId") or fields.get("local_id")
    return f"wechat:{session}:local:{local}" if local else f"wechat:{uuid.uuid4().hex}"


def xml_message(text: str):
    start = text.find("<")
    if start < 0 or len(text) > 2_000_000:
        return None
    try:
        return ET.fromstring(text[start:])
    except ET.ParseError:
        return None


def app_message(root):
    """只读取当前消息的 appmsg，不能把转发节点里的引用当作外层引用。"""
    if root is None:
        return None
    return root if root.tag == "appmsg" else root.find("appmsg")


def embedded_xml(element):
    """兼容 recorditem/recordxml 的子元素、CDATA 和多层实体编码。"""
    if element is None:
        return None
    if len(element):
        return element[0]
    text = element.text or ""
    for _ in range(4):
        root = xml_message(text)
        if root is not None:
            return root
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return None


def forward_record_component(record, message_id: str, *, depth=0, budget=None):
    """保持合并记录层级；附件无明文字节时明确标注，不伪造可见图片。"""
    from src.common.data_models.message_component_data_model import ForwardComponent, ForwardNodeComponent, TextComponent
    if record is None or depth >= 8:
        return None
    budget = [1000] if budget is None else budget
    items = record.findall("./datalist/dataitem")
    if not items:
        items = record.findall("./dataitem")
    if record.tag == "dataitem":
        items = [record]
    nodes = []
    for index, node in enumerate(items):
        if budget[0] <= 0:
            break
        budget[0] -= 1
        node_id = node.get("dataid") or node.findtext("messageuuid") or f"{message_id}:forward:{index}"
        kind = str(node.get("datatype") or node.findtext("datatype") or "1")
        parts = []
        source_time = node.findtext("sourcetime")
        if source_time:
            parts.append(TextComponent(f"[原消息时间：{source_time}]\n"))
        title = node.findtext("datatitle") or ""
        body = node.findtext("datadesc") or node.findtext("content") or ""
        nested_element = node.find("recordxml")
        if nested_element is None:
            nested_element = node.find("recorditem")
        if kind == "17" or nested_element is not None:
            if title or body:
                parts.append(TextComponent("\n".join(x for x in (title, body) if x)))
            nested = forward_record_component(embedded_xml(nested_element), node_id, depth=depth+1, budget=budget)
            parts.append(nested if nested is not None else TextComponent("[嵌套聊天记录正文不可用或达到解析上限]"))
        elif kind in {"2", "3", "4", "8"}:
            label = {"2": "图片", "3": "语音", "4": "视频", "8": "文件"}[kind]
            description = "\n".join(x for x in (title, body) if x)
            parts.append(TextComponent(f"[{label}附件，未取得可解析的原始内容]" + ("\n"+description if description else "")))
        else:
            url = node.findtext("dataurl") if kind == "5" else ""
            parts.append(TextComponent("\n".join(x for x in (title, body, url) if x) or "[转发内容不可用]"))
        sender_id = node.findtext("dataitemsource/realchatname") or node.findtext("dataitemsource/fromusr")
        if sender_id and sender_id.endswith("@chatroom"):
            sender_id = None
        nodes.append(ForwardComponent(user_nickname=node.findtext("sourcename") or sender_id or "未知用户",
            user_id=sender_id, message_id=node_id, content=parts))
    if nodes and len(items) > len(nodes):
        nodes[-1].content.append(TextComponent("[剩余聊天记录超过解析数量上限，未展开]"))
    return ForwardNodeComponent(nodes) if nodes else None


def readable_quote_content(value: Any, message_type="") -> str:
    text = str(value or "")
    if str(message_type) == "1":
        return text
    root = xml_message(text)
    app = app_message(root)
    if app is not None:
        if app.findtext("type") == "19":
            component = forward_record_component(embedded_xml(app.find("recorditem")), "quoted-record")
            if component is not None:
                from src.common.data_models.message_component_data_model import ForwardNodeComponent, TextComponent
                def render(group):
                    lines = ["[引用聊天记录]"]
                    for node in group.forward_components:
                        lines.append(f"{node.user_nickname}：")
                        for part in node.content:
                            if isinstance(part, ForwardNodeComponent):
                                lines.append(render(part))
                            elif isinstance(part, TextComponent):
                                lines.append(part.text)
                    return "\n".join(lines)
                return render(component)
            return "[引用聊天记录正文不可用]"
        return "\n".join(x for x in (app.findtext("title"), app.findtext("des"), app.findtext("url")) if x) or "[引用应用消息]"
    if root is not None and root.tag == "msg":
        for tag, label in (("img", "图片"), ("voicemsg", "语音"), ("videomsg", "视频"), ("emoji", "表情")):
            if root.find(tag) is not None:
                return f"[引用{label}，原始内容不可用]"
    return text


class WechatMessageTranslator:
    def __init__(self, config: dict):
        self.config = config
        self.account_id = str((config.get("weflow") or {}).get("myWxid") or "wechatbot")
        from utils.wechat_identity import canonical_wechat_user_id
        self.bot_user_id = canonical_wechat_user_id(self.account_id)

    async def translate(self, context: dict):
        from src.chat.message_receive.message import SessionMessage
        from src.common.data_models.mai_message_data_model import GroupInfo, MessageInfo, UserInfo
        from src.common.data_models.message_component_data_model import (
            AtComponent, DictComponent, FileComponent, MessageSequence, ReplyComponent, TextComponent,
        )
        f = wire_fields(context)
        if str(f.get('platform') or f.get('source_platform') or 'wechat').lower() != 'wechat':
            raise ValueError('Non-WeChat messages must use their native platform adapter')
        try:
            local_type = int(f.get("localType") or f.get("local_type") or 1)
        except (ValueError, TypeError):
            local_type = 1
        # WeChat 4 stores appmsg subtype in the upper 32 bits (e.g. 57<<32|49).
        local_type &= 0xffffffff
        # Push content is a display label for rich messages. Hydrate the exact
        # original when the transport has not supplied its functional payload.
        if local_type in {42, 48, 49} and xml_message(str(f.get("rawContent") or "")) is None and xml_message(str(f.get("content") or "")) is None:
            original = await self._fetch_original(f)
            f.update({k: v for k, v in original.items() if k not in {"messageKey", "localId", "serverId", "sessionId"} and v is not None})
        private = bool(f.get("is_private") or f.get("type") == "private" or f.get("sessionType") == "private")
        session = str(f.get("sessionId") or "")
        group_id = "" if private else str((session if session.endswith("@chatroom") else "") or f.get("group_id") or f.get("group") or "")
        user_id = str(f.get("wxid") or f.get("senderWxid") or f.get("senderUsername") or "")
        if not user_id:
            raise ValueError("WeChat inbound message has no sender wxid")
        now = datetime.now()
        msg = SessionMessage(
            message_id=external_id(f, session or group_id or user_id),
            timestamp=local_datetime(f.get("timestamp") or f.get("createTime"), now),
            received_at=local_datetime(f.get("received_at") or f.get("_ts"), now), platform="wechat",
        )
        msg.account_id = self.account_id
        platform_id = str(f.get("serverId") or f.get("server_id") or f.get("svrid") or "")
        msg.platform_message_id = platform_id if platform_id not in {"", "0"} else None
        metadata = {k: f[k] for k in (
            "messageKey", "localId", "serverId", "sessionId", "localType", "timestamp", "source_kind",
            "source_origin", "learnable", "prompt_visible", "prefix_used", "directed_to_bot", "directed_to_other",
            "reply_to_bot", "reply_to_other", "quoted_message_type", "quoted_timestamp", "mentions",
            "rawContent", "parsedContent", "msgSource", "rawLocalType", "localSubtype", "isSend", "quoted_available", "quoted_role", "quoted_is_bot",
        ) if k in f}
        msg.message_info = MessageInfo(
            user_info=UserInfo(user_id, str(f.get("user") or f.get("sourceName") or user_id),
                               f.get("user_cardname") or f.get("senderCardname")),
            group_info=GroupInfo(group_id, str(f.get("group") or f.get("groupName") or group_id)) if group_id else None,
            additional_config={"wechat": metadata},
        )
        from src.common.message_provenance import stamp_message
        stamp_message(msg, direction='inbound', transport='wechatbot-shell')
        from src.common.utils.utils_session import SessionUtils
        msg.session_id = SessionUtils.calculate_session_id(
            "wechat", user_id=user_id, group_id=group_id or None, account_id=self.account_id, scope=msg.scope,
        )
        msg.is_at = bool(f.get("is_at") or f.get("isAt") or f.get("atBot"))
        msg.is_mentioned = bool(f.get("is_mentioned") or f.get("isMentioned"))
        msg.is_command = bool(f.get("command"))
        msg.is_notify = local_type in {10000, 10002} or f.get("source_kind") == "system_event"
        from src.common.bot_interaction_policy import annotate_message
        annotate_message(msg, plugin_request=context.get("bot_plugin_request") is True)
        text = str(context.get("content") if context.get("content") is not None else f.get("text") or f.get("content") or "")
        raw_text = str(f.get("rawContent") or "")
        root = xml_message(raw_text)
        if root is None:
            root = xml_message(text)
        if local_type == 1:
            root = None
        if f.get("command"):
            text = str(f["command"]) + (" " + str(f.get("args")) if f.get("args") else "")
            root = None
        elif raw_text and root is None and local_type == 1:
            text = re.sub(r"^" + re.escape(user_id) + r":\r?\n", "", raw_text, count=1)
        components = []
        quote = f.get("quoted_message") if isinstance(f.get("quoted_message"), dict) else {}
        quote_id = next((x for x in (quote.get("server_id"), f.get("quoted_server_id"), f.get("quoted_message_id"), quote.get("message_id")) if x and str(x) != "0"), None)
        if quote.get("local_id") and (not quote_id or str(quote_id) == str(quote["local_id"])):
            quote_id = f"wechat:{session or group_id or user_id}:local:{quote['local_id']}"
        quote_body = f.get("quoted_content") or quote.get("content")
        quote_user = f.get("quoted_sender_wxid") or quote.get("sender_wxid")
        quote_name = f.get("quoted_sender") or quote.get("sender")
        quote_type = f.get("quoted_message_type") or quote.get("message_type") or ""
        app = app_message(root)
        if app is not None and app.findtext("type") == "57":
            ref = app.find("refermsg")
            if ref is not None:
                server_ref = ref.findtext("svrid")
                quote_id = server_ref if server_ref not in {None, "", "0"} else quote_id
                quote_body = ref.findtext("content") or quote_body
                quote_user = ref.findtext("chatusr") or quote_user
                quote_name = ref.findtext("displayname") or quote_name
                quote_type = ref.findtext("type") or quote_type
        quote_body = readable_quote_content(quote_body, quote_type)
        if quote_id or quote_body:
            # 原生 ReplyComponent 的数据库序列化仅保留 ID；适配器扩展元数据
            # 保存微信提供的引用快照，让窗口外引用在重启恢复后仍然可读。
            metadata["quoted_snapshot"] = dict(message_id=str(quote_id or ""),content=quote_body,
                sender_id=quote_user,sender_name=quote_name,message_type=quote_type)
            # Do not invent a platform reference when only a preview is available.
            if quote_id:
                quote_id = str(quote_id)
                components.append(ReplyComponent(quote_id, str(quote_body or ""), quote_user, quote_name))
                msg.reply_to = quote_id
            else:
                components.append(TextComponent(f"[引用预览：{quote_name or ''}：{quote_body}]"))
        mention_values = f.get("atUsers") or f.get("atUserList") or f.get("mentions") or []
        if isinstance(mention_values, str):
            mention_values = mention_values.split(",")
        for item in mention_values:
            target = (item.get("wxid") or item.get("user_id") or item.get("id")) if isinstance(item, dict) else str(item)
            # Router also supplies display names here: they are not user IDs.
            if target and (isinstance(item, dict) or target.startswith("wxid_") or target == "notify@all"):
                components.append(AtComponent(str(target), item.get("nickname") if isinstance(item, dict) else None))
        source_xml = str(f.get("msgSource") or f.get("msg_source") or "")
        if source_xml:
            match = re.search(r"<atuserlist>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</atuserlist>", source_xml, re.S)
            if match:
                existing = {c.target_user_id for c in components if isinstance(c, AtComponent)}
                for target in match.group(1).split(","):
                    target = target.strip()
                    if target and target not in existing:
                        components.append(AtComponent(target)); existing.add(target)
        media_type = str(f.get("mediaType") or f.get("media_type") or "").lower()
        kind = {3: "image", 34: "voice", 43: "video", 47: "emoji"}.get(local_type, media_type)
        if kind in {"image", "emoji", "voice", "video"}:
            try:
                component = await self._media_component(kind, f)
            except Exception as exc:
                metadata["media_error"] = f"{type(exc).__name__}: {exc}"
                component = DictComponent(data={"type": kind, "data": {"available": False, "error": str(exc)}})
            components.append(component)
            msg.is_picture = kind == "image"
            msg.is_emoji = kind == "emoji"
            # Retain captions, but never expose encrypted/XML transport bodies as user prose.
            if root is None and text and not (text.startswith("[") and text.endswith("]")):
                components.append(TextComponent(text))
        elif root is not None and local_type == 42:
            card = {"user_id": root.get("username") or "", "nickname": root.get("nickname") or "",
                    "alias": root.get("alias") or "", "avatar": root.get("bigheadimgurl") or root.get("smallheadimgurl") or ""}
            metadata["contact_card"] = card
            components.append(TextComponent(f"[名片] {card['nickname']} ({card['user_id']})"))
        elif root is not None and local_type == 48:
            location = root.find(".//location")
            data = dict(location.attrib) if location is not None else {}
            metadata["location"] = data
            components.append(TextComponent("[位置] " + " ".join(str(data.get(k) or "") for k in ("poiname", "label", "x", "y")).strip()))
        elif root is not None:
            app = app_message(root)
            if app is not None and app.findtext("type") == "6":
                components.append(FileComponent(name=app.findtext("title") or "", size=app.findtext("appattach/totallen") or "",
                                                file_id=app.findtext("appattach/attachid") or "", url=f.get("mediaUrl") or "",
                                                base64_data=f.get("base64") or ""))
            elif app is not None:
                title = app.findtext("title") or ""
                url = app.findtext("url") or ""
                components.append(TextComponent("\n".join(x for x in (title, app.findtext("des"), url) if x)))
                metadata["appmsg_type"] = app.findtext("type")
                if app.findtext("type") == "19":
                    forward = forward_record_component(embedded_xml(app.find("recorditem")), msg.message_id)
                    if forward is not None:
                        components.append(forward)
                    else:
                        components.append(TextComponent("[聊天记录正文不可用，未取得有效的转发节点]"))
            else:
                components.append(DictComponent(data={"type": "wechat_event" if msg.is_notify else "wechat_xml", "data": {"xml": text}}))
        elif kind == "file":
            components.append(FileComponent.from_payload(f))
        else:
            components.append(TextComponent(text))
        msg.raw_message = MessageSequence(components=components)
        if any(isinstance(c, AtComponent) and c.target_user_id in {self.bot_user_id, self.account_id, "notify@all"} for c in components):
            msg.is_at = True
        return msg

    async def _fetch_original(self, fields: dict, *, media=False):
        import asyncio
        from core.weflow_client import WeFlowClient
        client = WeFlowClient(self.config.get("token"), None, None)
        wf = self.config.get("weflow") or {}
        client.api_base = str(wf.get("apiBase") or f"http://{wf.get('apiHost') or '127.0.0.1'}:{wf.get('apiPort') or 5031}")
        result = await asyncio.to_thread(client.get_original_message, fields.get("sessionId"), fields.get("localId"),
            fields.get("serverId") or fields.get("svrid"), timeout=25 if media else 8, media=media)
        message = (result.get("message") or {}) if result.get("ok") else {}
        expected = fields.get("serverId") or fields.get("svrid")
        actual = message.get("serverIdRaw") or message.get("serverId")
        if expected and actual and str(expected) != "0" and str(actual) != "0" and str(expected) != str(actual):
            raise ValueError("Original WeChat message server ID does not match requested ID")
        return message

    async def _media_component(self, kind: str, fields: dict):
        import asyncio
        from src.common.data_models.message_component_data_model import DictComponent, EmojiComponent, FileComponent, ImageComponent, VoiceComponent
        f = dict(fields)
        # Resolve by exact transport identifiers; never select a nearby image by time.
        if not (f.get("mediaUrl") or f.get("media_url") or f.get("binary_data") or f.get("base64")):
            f.update(await self._fetch_original(f, media=True))
        data = f.get("binary_data") or b""
        if not data and f.get("base64"):
            data = base64.b64decode(f["base64"], validate=True)
        url = str(f.get("mediaUrl") or f.get("media_url") or "")
        if not data and url:
            from core.weflow_media import download_media
            data = await asyncio.to_thread(download_media, url)
        if kind == "video":
            return FileComponent(name=f.get("fileName") or "video.mp4", mime_type="video/mp4", url=url,
                                 file_id=str(f.get("serverId") or f.get("localId") or ""),
                                 base64_data=base64.b64encode(data).decode() if data else "")
        cls = {"image": ImageComponent, "emoji": EmojiComponent, "voice": VoiceComponent}[kind]
        if data:
            return cls(binary_hash=hashlib.sha256(data).hexdigest(), binary_data=data)
        # Explicit unavailability retains type + lookup identifiers without fabricating bytes.
        return DictComponent(data={"type": kind, "data": {"available": False, "sessionId": f.get("sessionId"),
                             "localId": f.get("localId"), "serverId": f.get("serverId"), "url": url}})
