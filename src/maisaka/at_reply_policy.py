"""Explicit @-only mode; permissions apply to a new batch, never old history."""


def enabled(runtime):
    from src.config.config import global_config
    return bool(getattr(getattr(runtime, 'chat_stream', None), 'platform', '') in {'qq', 'wechat'}
                and getattr(global_config.chat.reply_timing, 'reply_only_when_at', False))


def addressed(messages):
    from src.maisaka.context.freshness import is_stale_message
    return any(getattr(message, 'is_at', False)
               and not getattr(message, 'is_notify', False)
               and not is_stale_message(message) for message in messages)


def reply_allowed(runtime):
    return not enabled(runtime) or getattr(runtime, '_at_reply_authorized', False) is True
