"""Explicit @-only mode; permissions apply to a new batch, never old history."""


def _proactive_entry(runtime):
    """Return the configured entry that whitelists this chat, if any.

    Entries are ``platform:group_id`` (preferred, stable across renames) or
    ``platform:group_name``. Private chats never match.
    """

    from src.config.config import global_config

    entries = getattr(global_config.chat.reply_timing, 'proactive_chat_groups', None) or []
    stream = getattr(runtime, 'chat_stream', None)
    if not entries or stream is None:
        return ''
    platform = str(getattr(stream, 'platform', '') or '').strip().lower()
    group_id = str(getattr(stream, 'group_id', '') or '').strip()
    if not group_id:
        return ''
    group_name = str(getattr(stream, 'group_name', '') or '').strip()
    for raw in entries:
        entry = str(raw or '').strip()
        if not entry:
            continue
        if ':' in entry:
            entry_platform, _, target = entry.partition(':')
            if entry_platform.strip().lower() != platform:
                continue
            target = target.strip()
        else:
            target = entry
        if target and target in {group_id, group_name}:
            return entry
    return ''


def enabled(runtime):
    """@-only mode; whitelisted groups keep normal talking and proactive tasks."""

    from src.config.config import global_config
    return bool(getattr(getattr(runtime, 'chat_stream', None), 'platform', '') in {'qq', 'wechat'}
                and getattr(global_config.chat.reply_timing, 'reply_only_when_at', False)
                and not _proactive_entry(runtime))


def addressed(messages):
    from src.maisaka.context.freshness import is_stale_message
    return any(getattr(message, 'is_at', False)
               and not getattr(message, 'is_notify', False)
               and not is_stale_message(message) for message in messages)


def reply_allowed(runtime):
    return not enabled(runtime) or getattr(runtime, '_at_reply_authorized', False) is True
