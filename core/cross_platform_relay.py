"""Explicit user-requested text relay. It cannot forward control segments or commands."""
import asyncio
from contextlib import closing
import hashlib
import re
import time

_RELAY_COOLDOWN: dict[tuple[str, str, str], float] = {}


def _usable_name(value: object) -> str:
    value = str(value or "").strip()
    if not value or value.startswith(("qq:", "wxid_", "wx:", "qq_")) or value.isdigit():
        return ""
    return value[:80]


def _member_name(context: dict, user_id: object, provided: object = "") -> str:
    """Resolve a stable platform ID to a display name without leaking QQ IDs."""
    candidate = _usable_name(provided)
    if candidate:
        return candidate
    clean_id = str(user_id or "").strip()
    if clean_id.startswith("qq:"):
        clean_id = clean_id[3:]
    for item in context.get("mentions") or []:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("user_id") or item.get("wxid") or "").strip()
        if item_id in {clean_id, "qq:" + clean_id}:
            candidate = _usable_name(item.get("name") or item.get("nickname"))
            if candidate:
                return candidate
    try:
        from src.person_info.person_info import Person
        person = Person(platform="qq", user_id=clean_id)
        group_id = str(context.get("group_id") or "").strip()
        for row in getattr(person, "group_cardname_list", []) or []:
            if isinstance(row, dict) and str(row.get("group_id") or "") == group_id:
                candidate = _usable_name(row.get("group_cardname"))
                if candidate:
                    return candidate
        if getattr(person, "is_known", False):
            for value in (getattr(person, "nickname", ""), getattr(person, "person_name", "")):
                candidate = _usable_name(value)
                if candidate:
                    return candidate
    except Exception:
        pass
    return "群友"


def _format_relay_text(text: str, context: dict) -> str:
    """Translate QQ mention markers and add quoted-message identity/content."""
    text = str(text or "").strip()

    def replace_qq_mention(match):
        return "@" + _member_name(context, match.group(1))

    text = re.sub(r"@<qq:([^>]+)>", replace_qq_mention, text)
    quoted_id = context.get("quoted_sender_wxid") or context.get("quoted_sender_id")
    quoted_sender = context.get("quoted_sender") or context.get("quoted_sender_nickname")
    quoted_content = " ".join(str(context.get("quoted_content") or "").split())
    if quoted_id or quoted_sender or quoted_content:
        if len(quoted_content) > 500:
            quoted_content = quoted_content[:500].rstrip() + "..."
        name = _member_name(context, quoted_id, quoted_sender)
        quote = f"[回复 {name}"
        if quoted_content:
            quote += f"：{quoted_content}"
        quote += "]"
        text = f"{quote} {text}".strip()
    return text


async def relay(text, context, destination):
    from core.community_streams import active_groups
    from src.services.send_service import custom_reply_set_to_stream_with_message
    from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
    from src.common.message_provenance import plugin_scope
    from core.shell_plugin_config import settings
    from utils import sqlite_store as store
    source = context.get('platform') or 'wechat'
    if (source,destination) not in {('wechat','qq'),('qq','wechat')}:
        return '该命令不支持当前消息平台'
    text = ' '.join(map(str,text)) if isinstance(text,list) else str(text or '').strip()
    text = _format_relay_text(text, context)
    config = settings('gsmps.command.' + ('qq' if destination=='qq' else 'wx'))
    cooldown = max(0, int(config.get('cooldown_seconds', 5) or 0))
    cooldown_key = (source, destination, str(context.get('group_id') or context.get('sessionId') or context.get('group') or ''))
    if cooldown and time.monotonic() - _RELAY_COOLDOWN.get(cooldown_key, -1e9) < cooldown:
        return f'转发冷却中，请等待 {cooldown} 秒'
    limit = int(config.get('max_chars',2000))
    if not text or len(text)>limit:
        return f'请输入 1–{limit} 字的转发正文'
    raw = context.get('raw') or {}
    name = _usable_name(context.get('user') or raw.get('user')) or 'group member'
    name = re.sub(r'[\[\]\r\n\x00-\x1f]', ' ', name)[:80]
    body = '[' + ('QQ' if source=='qq' else '微信') + ' | ' + name + ']' + text
    targets = await active_groups(destination)
    if not targets:
        return '对端没有已放行且开启 LLM 主动回复的群聊'
    if len(targets)>50:
        return '对端目标超过单次 50 群限制，本次未发送'
    source_id = str(context.get('serverId') or context.get('messageKey') or raw.get('serverId') or '')
    if not source_id:
        return '缺少原始消息标识，本次未转发'
    sent=0; failed=0; skipped=0
    _RELAY_COOLDOWN[cooldown_key] = time.monotonic()
    for target in targets:
        key=hashlib.sha256(repr((source,context.get('account_id'),context.get('scope'),source_id,target.session_id)).encode()).hexdigest()
        with store._LOCK, closing(store._connect()) as database, database:
            database.execute('CREATE TABLE IF NOT EXISTS cross_platform_relay '
                             '(id TEXT PRIMARY KEY, source_platform TEXT, target_session TEXT, status TEXT)')
            claimed=database.execute('INSERT OR IGNORE INTO cross_platform_relay VALUES(?,?,?,?)',
                                     (key,source,target.session_id,'pending')).rowcount
        if not claimed:
            skipped+=1
            continue
        delivered=None
        try:
            origin=dict(platform=source,account_id=str(context.get('account_id') or ''),scope=str(context.get('scope') or ''),
                        user_id=str(context.get('user_id') or context.get('wxid') or raw.get('wxid') or ''),
                        nickname=name,message_id=source_id,source_session=str(context.get('native_session_id') or context.get('sessionId') or ''))
            with plugin_scope('shell.'+('qq' if destination=='qq' else 'wx'),trigger_message_id=source_id,relay_origin=origin):
                delivered=await custom_reply_set_to_stream_with_message(MessageSequence([TextComponent(body)]),
                    target.session_id, typing=False, sync_to_maisaka_history=True, maisaka_source_kind='plugin_output')
        except Exception:
            # Delivery may have partially completed; do not replay automatically.
            delivered=None
        with store._LOCK, closing(store._connect()) as database, database:
            database.execute('UPDATE cross_platform_relay SET status=? WHERE id=?',('sent' if delivered else 'failed',key))
        sent+=bool(delivered); failed+=not bool(delivered)
    return f'转发完成：成功 {sent} 群，失败 {failed} 群，已处理跳过 {skipped} 群'


def relay_sync(text, context, destination):
    from llm.core.maibot_runtime import native_runtime
    loop=context.get('_native_loop') or native_runtime._loop
    if loop is None or not loop.is_running():
        return 'MaiBot 原生消息循环尚未就绪'
    try:
        if asyncio.get_running_loop() is loop:
            return '转发需要在插件工作线程中执行'
    except RuntimeError:
        pass
    future=asyncio.run_coroutine_threadsafe(relay(text,context,destination),loop)
    try:
        return future.result(timeout=120)
    except Exception:
        future.cancel()
        return '转发未得到完整发送确认，请检查记录，避免重复发送'
