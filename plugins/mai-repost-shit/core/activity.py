"""Summarize native messages without counting quotes, echoes or duplicate deliveries."""
from __future__ import annotations

from datetime import datetime
import re
from typing import Any

_BILI = re.compile(r"(?:https?://)?(?:[\w-]+\.)?(?:bilibili\.com|b23\.tv)/|(?<![A-Za-z0-9])BV[0-9A-Za-z]{10}(?![A-Za-z0-9])", re.I)
_MEME = re.compile(r"搬[屎史]|鬼畜|抽象视频|[BbＢｂ]站解析|解析[BbＢｂ]站")


def timestamp(value: Any) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(str(value)).timestamp()
        except (ValueError, TypeError, OverflowError):
            return 0.0


def summarize(messages: list, *, now: float, window: int, group_id: str, ignored_users: list[str]) -> dict:
    users: set[str] = set()
    seen: set[str] = set()
    total = shares = bili = 0
    newest = newest_share = 0.0
    from utils.platform_target import matches_target
    for message in messages:
        if not isinstance(message, dict) or not matches_target(message, group_id) or message.get('is_notify'):
            continue
        info = message.get('message_info') or {}
        user = str((info.get('user_info') or {}).get('user_id') or '')
        extra = info.get('additional_config') or {}
        wechat = extra.get('wechat') or {}
        bot_policy = extra.get('bot_interaction') or {}
        if (not user or user in ignored_users or message.get('is_bot') or extra.get('is_bot')
                or bot_policy.get('sender_is_bot') or wechat.get('is_other_bot') or wechat.get('is_bot')
                or wechat.get('is_bot_self') or wechat.get('isSend') in (1, '1')
                or wechat.get('source_kind') in {'bot_echo', 'bot_echo_external', 'bot_outgoing'}):
            continue
        stamp = timestamp(message.get('timestamp'))
        mid = str(message.get('message_id') or '')
        if not mid or mid in seen or not now - window <= stamp <= now + 5:
            continue
        seen.add(mid)
        users.add(user)
        total += 1
        newest = max(newest, stamp)
        parts = message.get('raw_message') or []
        texts = []
        video = False
        for part in parts:
            if not isinstance(part, dict):
                continue
            kind, data = part.get('type'), part.get('data')
            if kind == 'text' and isinstance(data, str):
                if not data.startswith('[引用预览：'):
                    texts.append(data)
            elif kind == 'file' and isinstance(data, dict):
                video |= str(data.get('mime_type') or '').startswith('video/') or str(data.get('name') or data.get('filename') or '').lower().endswith('.mp4')
            elif kind in {'video', 'image', 'emoji'}:
                video = True
            # Reply/forward nodes are deliberately not traversed: old quoted links are not new shares.
        text = '\n'.join(texts) if parts else str(message.get('processed_plain_text') or '')
        is_bili = bool(_BILI.search(text))
        if video or is_bili or _MEME.search(text):
            shares += 1
            bili += int(is_bili)
            newest_share = max(newest_share, stamp)
    return dict(messages=total, users=len(users), shares=shares, bili=bili,
                newest=newest, newest_share=newest_share, observed_at=now)


def eligible(stats: dict, cfg: Any, now: float) -> bool:
    return (stats.get('messages', 0) >= cfg.min_messages and stats.get('users', 0) >= cfg.min_users
            and stats.get('shares', 0) >= cfg.min_shares and now - stats.get('newest_share', 0) <= cfg.fresh_seconds)


def is_hot(stats: dict, cfg: Any) -> bool:
    return stats.get('messages', 0) >= cfg.hot_messages or stats.get('shares', 0) >= cfg.hot_shares


def low_activity_eligible(stats: dict, cfg: Any, now: float) -> bool:
    """Allow quiet-room opportunities after a few human messages.

    This deliberately excludes a completely empty room and a currently busy
    burst; it lets the planner fill a stale quiet window rather than waiting
    for two fresh Bilibili shares that may never arrive.
    """
    messages = int(stats.get('messages', 0) or 0)
    newest = float(stats.get('newest', 0) or 0)
    # 允许“没有新的分享”而继续补位；仍要求聊天流至少有一条真实历史，
    # 避免刚发现一个从未有过消息的空目标就凭空广播。
    quiet_since = now - newest if newest > 0 else now
    return (getattr(cfg, 'low_activity_enabled', True)
            and messages >= getattr(cfg, 'low_activity_min_messages', 1)
            and stats.get('shares', 0) < cfg.min_shares
            and quiet_since >= getattr(cfg, 'low_activity_idle_seconds', 180))
