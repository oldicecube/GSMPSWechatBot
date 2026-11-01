"""Strict marker parsing and group-identity validation for native WeChat @ mentions."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import requests
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[2]
GROUPS_DIR = BASE_DIR / "data" / "groups"
DB_PATH = BASE_DIR / "data" / "bot.sqlite3"
MARKER_RE = re.compile(r"\[\[WX_AT:([A-Za-z0-9_.@-]{1,160})\]\]")
MARKER_PREFIX = "[[WX_AT:"
MAX_MENTIONS_PER_MESSAGE = 8
MAX_GROUP_HISTORY_BYTES = 32 * 1024 * 1024


class MentionMarkupError(ValueError):
    pass


def _safe_group_history_path(group_id: str) -> Path:
    group_id = str(group_id or "").strip()
    if not group_id:
        raise MentionMarkupError("mention tags are only supported in a known chat")
    root = GROUPS_DIR.resolve()
    path = (root / group_id / "group_messages.json").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise MentionMarkupError("invalid group history path") from exc
    return path


def _validate_nickname(name: str, wxid: str) -> str:
    # Do not feed controls/newlines or account IDs into the @ autocomplete box.
    if not isinstance(name, str) or not name or len(name) > 120:
        raise MentionMarkupError("invalid or missing group nickname")
    if name != name.strip() or name.startswith("@") or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise MentionMarkupError("group nickname contains unsupported characters")
    if name == wxid or name.startswith("wxid_") or name.endswith("@chatroom"):
        raise MentionMarkupError("an account ID is not a confirmed group nickname")
    if name in {"unknown", "member", "\u672a\u77e5\u7528\u6237", "\u672a\u77e5\u53d1\u9001\u8005", "\u6211", "\u6240\u6709\u4eba"}:
        raise MentionMarkupError("placeholder or all-member nickname is not allowed")
    return name


def _group_members(group_id: str) -> dict[str, str]:
    """Load the latest observed name per wxid once for the entire message."""
    members = _native_group_members(group_id)
    # Observed history can contain a previous nickname after a group member
    # renames themselves. Prefer WeFlow's current group membership snapshot.
    if str(group_id).endswith("@chatroom"):
        try:
            from core import weflow_media
            if weflow_media.TOKEN:
                response = requests.get(
                    f"{weflow_media.BASE_URL}/api/v1/group-members",
                    params={"chatroomId": group_id}, headers=weflow_media._headers(), timeout=3,
                )
                response.raise_for_status()
                payload = response.json()
                if payload.get("success") is True and isinstance(payload.get("members"), list):
                    current = {}
                    for row in payload["members"]:
                        wxid = str(row.get("wxid") or "").strip()
                        name = str(row.get("groupNickname") or row.get("displayName") or row.get("nickname") or "")
                        if wxid:
                            current[wxid] = name
                    # A confirmed membership snapshot also rejects departed users.
                    return current
        except (OSError, ValueError, TypeError, requests.RequestException):
            pass
    path = _safe_group_history_path(group_id)
    if path.is_file() and path.stat().st_size <= MAX_GROUP_HISTORY_BYTES:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = []
        if isinstance(payload, list):
            for row in reversed(payload):
                if not isinstance(row, dict) or row.get("role") == "context_summary":
                    continue
                wxid = str(row.get("wxid") or row.get("sender_wxid") or row.get("subject_id") or "").strip()
                name = str(row.get("group_nickname") or row.get("groupNickname") or row.get("nickname") or row.get("sender_nickname") or row.get("user") or "")
                if wxid and wxid not in members:
                    # Include bots for duplicate-name detection, not as invented identities.
                    members[wxid] = name
    # Compatibility fallback: echo records retain group-scoped sender identity
    # even when the compact group transcript has been rotated.
    echo_path = BASE_DIR / "data" / "echo.json"
    if echo_path.is_file():
        try:
            rows = json.loads(echo_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            rows = []
        if isinstance(rows, list):
            for row in reversed(rows):
                if not isinstance(row, dict) or str(row.get("group") or "").strip() != str(group_id).strip():
                    continue
                wxid = str(row.get("wxid") or "").strip()
                name = str(row.get("user") or row.get("nickname") or "").strip()
                if wxid and name and wxid not in members:
                    members[wxid] = name
    return members


def _native_group_members(group_id: str) -> dict[str, str]:
    """Read identities actually observed by MaiBot after native ingestion."""
    database = Path(os.environ.get("MAIBOT_INSTANCE_DIR") or BASE_DIR) / "data" / "MaiBot.db"
    if not database.is_file():
        return {}
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as conn:
            field = "group_id" if str(group_id).endswith("@chatroom") else "group_name"
            # Display names can collide. Require a single canonical group.
            groups = conn.execute(f"SELECT DISTINCT group_id FROM mai_messages WHERE platform='wechat' AND {field}=?", (group_id,)).fetchall()
            if len(groups) != 1 or not groups[0][0]:
                return {}
            rows = conn.execute("SELECT user_id, user_cardname, user_nickname FROM mai_messages WHERE platform='wechat' AND group_id=? AND is_notify=0 ORDER BY id DESC LIMIT 5000", (groups[0][0],)).fetchall()
        members = {}
        for wxid, card, nickname in rows:
            if wxid and wxid not in members:
                members[wxid] = card or nickname or ""
        return members
    except (sqlite3.Error, OSError, ValueError):
        return {}


def _reminder_name(group_id: str, wxid: str) -> str:
    # A leased, dispatching reminder can preserve its requester's identity after
    # history compaction. This is still an observed name, not a live member lookup.
    if not DB_PATH.is_file():
        return ""
    conn = None
    try:
        conn = sqlite3.connect(f"file:{DB_PATH.resolve().as_posix()}?mode=ro", uri=True, timeout=2)
        conn.execute("PRAGMA busy_timeout=2000")
        row = conn.execute(
            "SELECT owner_name FROM llm_scheduled_events "
            "WHERE group_id=? AND owner_wxid=? AND status='dispatching' "
            "AND owner_name<>'' ORDER BY id DESC LIMIT 1",
            (str(group_id).strip(), wxid),
        ).fetchone()
        return str(row[0]) if row else ""
    except sqlite3.Error:
        return ""
    finally:
        if conn is not None:
            conn.close()


def resolve_group_member(group_id: str, identifier: str):
    """Resolve a known group member by wxid or observed group nickname.

    This is intentionally limited to identities already observed in the local
    group transcript; it never guesses or queries arbitrary account IDs.
    """
    key = str(identifier or "").strip()
    if not key:
        return {"ok": False, "error": "member identifier is required"}
    members = _group_members(group_id)
    if key in members:
        return {"ok": True, "wxid": key, "nickname": members[key], "source": "group_history"}
    folded = key.casefold().lstrip("@")
    matches = [(wxid, name) for wxid, name in members.items() if str(name or "").casefold() == folded]
    if len(matches) == 1:
        wxid, name = matches[0]
        return {"ok": True, "wxid": wxid, "nickname": name, "source": "group_history"}
    if len(matches) > 1:
        return {"ok": False, "error": "nickname is ambiguous", "matches": [{"wxid": w, "nickname": n} for w, n in matches]}
    return {"ok": False, "error": "member not observed in this group"}


def normalize_plain_mentions(message: str, group_id: str) -> str:
    """Convert exact, unique observed @names in LLM output to native markers.

    Never use fuzzy matching, substrings, wxid guesses, or names from another
    group. Emails, links and duplicate member names stay unchanged.
    """
    text = str(message or "")
    if "@" not in text:
        return text
    members = _group_members(group_id)
    names = {}
    for wxid, name in members.items():
        try:
            _validate_nickname(name, wxid)
        except MentionMarkupError:
            continue
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{1,160}", wxid):
            continue
        names.setdefault(name, []).append(wxid)
    counts = {}
    for name, ids in names.items():
        counts[name.casefold()] = counts.get(name.casefold(), 0) + len(ids)
    unique = {name: ids[0] for name, ids in names.items() if counts[name.casefold()] == 1}
    if not unique:
        return text
    alternatives = "|".join(re.escape(name) for name in sorted(unique, key=len, reverse=True))
    pattern = re.compile(r"(?<![\w@/])@(" + alternatives + r")(?=$|[\s,.;:!?" + "\u3001\u3002\uff0c\uff1b\uff1a\uff01\uff1f" + r"])")
    return pattern.sub(lambda match: "[[WX_AT:" + unique[match.group(1)] + "]]", text)


def parse_and_resolve_mentions(message: str, group_id: str):
    """Return ``None`` for plain text, else (segments, display_text).

    Segment types are ``text`` and ``mention``. Mention markers are never sent
    literally; each target is resolved to a nickname observed in this group.
    """
    text = str(message or "")
    matches = list(MARKER_RE.finditer(text))
    if not matches:
        if "[[WX_AT" in text:
            raise MentionMarkupError("malformed WX_AT marker")
        return None
    if len(matches) > MAX_MENTIONS_PER_MESSAGE:
        raise MentionMarkupError(f"at most {MAX_MENTIONS_PER_MESSAGE} mentions are allowed per message")

    # Reject mixed valid/malformed markers rather than pasting internal syntax.
    if "[[WX_AT" in MARKER_RE.sub("", text):
        raise MentionMarkupError("malformed WX_AT marker")
    members = _group_members(group_id)
    for match in matches:
        wxid = match.group(1)
        if wxid not in members:
            members[wxid] = _reminder_name(group_id, wxid)
    resolved = {}
    for match in matches:
        wxid = match.group(1)
        nickname = _validate_nickname(members.get(wxid, ""), wxid)
        if any(other != wxid and name.casefold() == nickname.casefold() for other, name in members.items()):
            raise MentionMarkupError("multiple observed members share this nickname; native @ is ambiguous")
        resolved[wxid] = nickname

    segments = []
    display = []
    cursor = 0
    for match in matches:
        literal = text[cursor:match.start()]
        if literal:
            segments.append({"type": "text", "text": literal})
            display.append(literal)
        wxid = match.group(1)
        nickname = resolved[wxid]
        segments.append({"type": "mention", "wxid": wxid, "nickname": nickname})
        display.append("@" + nickname)
        cursor = match.end()
    tail = text[cursor:]
    if tail:
        segments.append({"type": "text", "text": tail})
        display.append(tail)
    return segments, "".join(display)


def resolve_native_segments(values, group_id: str, content: str):
    """Resolve only explicit native At components, preserving literal marker text."""
    if not isinstance(values, list) or not values or len(values) > 1000:
        raise MentionMarkupError("invalid native message segments")
    segments, display, wire = [], [], []
    count = 0
    for item in values:
        if not isinstance(item, dict):
            raise MentionMarkupError("invalid native message segment")
        if item.get("type") == "text" and isinstance(item.get("text"), str):
            text = item["text"]
            segments.append({"type": "text", "text": text}); display.append(text); wire.append(text)
        elif item.get("type") == "mention" and isinstance(item.get("wxid"), str):
            count += 1
            marker = "[[WX_AT:" + item["wxid"] + "]]"
            if count > MAX_MENTIONS_PER_MESSAGE or not MARKER_RE.fullmatch(marker):
                raise MentionMarkupError("invalid native mention")
            resolved, label = parse_and_resolve_mentions(marker, group_id)
            segments.extend(resolved); display.append(label); wire.append(marker)
        else:
            raise MentionMarkupError("unsupported native message segment")
    if "".join(wire) != content:
        raise MentionMarkupError("native message segments do not match content")
    return segments, "".join(display)


__all__ = ["MentionMarkupError", "MARKER_RE", "parse_and_resolve_mentions", "resolve_native_segments", "resolve_group_member", "normalize_plain_mentions"]
