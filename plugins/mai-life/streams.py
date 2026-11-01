"""聊天流白名单匹配。"""

from __future__ import annotations

from typing import Any


def normalize_text(value: Any) -> str:
    """去掉首尾空白。"""

    return str(value or "").strip()


def normalize_allowlist(entries: list[str] | None) -> list[str]:
    """规范化白名单，去掉空项并保持顺序。"""

    result: list[str] = []
    seen: set[str] = set()
    for item in entries or []:
        text = normalize_text(item)
        if not text or text in seen:
            continue
        result.append(text)
        seen.add(text)
    return result


def stream_session_id(stream: dict[str, Any]) -> str:
    """读取聊天流 ID。"""

    return normalize_text(stream.get("session_id") or stream.get("stream_id"))


def stream_match_keys(stream: dict[str, Any]) -> set[str]:
    """一个聊天流可被白名单命中的全部键。"""

    platform = normalize_text(stream.get("platform") or "qq")
    session_id = stream_session_id(stream)
    group_id = normalize_text(stream.get("group_id"))
    user_id = normalize_text(stream.get("user_id"))
    chat_type = normalize_text(stream.get("chat_type"))
    if not chat_type:
        if stream.get("is_group_session") is True or group_id:
            chat_type = "group"
        elif user_id:
            chat_type = "private"
    keys: set[str] = set()
    if platform == 'qq' and stream.get('account_id') and (group_id or user_id):
        from utils.platform_target import target_key
        keys.add(target_key('qq', stream['account_id'], stream.get('scope'),
                            'group' if group_id else 'private', group_id or user_id))
    if session_id:
        keys.add(session_id)
        keys.add(f"session:{session_id}")
    if group_id:
        keys.add(group_id)
        keys.add(f"{platform}:group:{group_id}")
        keys.add(f"group:{group_id}")
        keys.add(f"{platform}:{group_id}")
    if user_id:
        keys.add(user_id)
        keys.add(f"{platform}:private:{user_id}")
        keys.add(f"private:{user_id}")
        keys.add(f"{platform}:{user_id}")
    if platform and chat_type and user_id:
        keys.add(f"{platform}_{chat_type}_{user_id}")
    if platform and chat_type and group_id:
        keys.add(f"{platform}_{chat_type}_{group_id}")
    return keys


def allowlist_includes_all(allowlist: list[str]) -> bool:
    """白名单是否为 all。"""

    return any(item.lower() == "all" for item in allowlist)


def filter_streams(streams: list[dict[str, Any]], allowlist: list[str] | None) -> list[dict[str, Any]]:
    """按白名单过滤聊天流。空白名单返回空列表。"""

    entries = normalize_allowlist(allowlist)
    if not entries:
        return []
    known = [item for item in streams if isinstance(item, dict) and stream_session_id(item)]
    if allowlist_includes_all(entries):
        return known

    matched: dict[str, dict[str, Any]] = {}
    for stream in known:
        session_id = stream_session_id(stream)
        keys = stream_match_keys(stream)
        if any(entry in keys for entry in entries):
            matched[session_id] = stream
    return list(matched.values())


def session_allowed(
    session_id: str,
    allowlist: list[str] | None,
    streams: list[dict[str, Any]] | None = None,
) -> bool:
    """判断某个 session 是否在白名单中。"""

    session_id = normalize_text(session_id)
    entries = normalize_allowlist(allowlist)
    if not session_id or not entries:
        return False
    if allowlist_includes_all(entries):
        return True
    if session_id in entries or f"session:{session_id}" in entries:
        return True
    for stream in streams or []:
        if stream_session_id(stream) != session_id:
            continue
        keys = stream_match_keys(stream)
        if any(entry in keys for entry in entries):
            return True
    return False


def stream_matches_entry(stream: dict[str, Any], entry: str) -> bool:
    """判断聊天流是否命中某一条配置标识。"""

    text = normalize_text(entry)
    if not text:
        return False
    session_id = stream_session_id(stream)
    if text == session_id or text == f"session:{session_id}":
        return True
    return text in stream_match_keys(stream)


def format_stream_info(stream: dict[str, Any]) -> str:
    """把聊天流信息写成给模型看的短文本。"""

    session_id = stream_session_id(stream)
    platform = normalize_text(stream.get("platform") or "")
    chat_type = normalize_text(stream.get("chat_type") or "")
    group_id = normalize_text(stream.get("group_id"))
    user_id = normalize_text(stream.get("user_id"))
    lines = [f"聊天流 ID: {session_id}"]
    if platform:
        lines.append(f"平台: {platform}")
    if chat_type:
        lines.append(f"类型: {chat_type}")
    if group_id:
        lines.append(f"群: {group_id}")
    if user_id:
        lines.append(f"用户: {user_id}")
    return "\n".join(lines)
