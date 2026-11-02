"""Historical evidence can inform a turn without becoming a new reply target."""
from datetime import datetime

MAX_REPLY_AGE_SECONDS = 3600


def is_stale_message(message, *, now=None):
    stamp = getattr(message, 'timestamp', None)
    if not isinstance(stamp, datetime):
        return False
    current = now or datetime.now(stamp.tzinfo)
    return current.timestamp() - stamp.timestamp() >= MAX_REPLY_AGE_SECONDS


def expired_target_hint(history):
    from src.maisaka.context.message_id_alias import to_display_message_id
    ids = []
    for entry in history:
        original = getattr(entry, 'original_message', None)
        if original is not None and getattr(original, 'message_id', '') and is_stale_message(original):
            ids.append(to_display_message_id(original.message_id))
    if not ids:
        return ''
    return ('[历史有效期边界·内部数据] 下列消息已超过1小时，仅作背景，不能作为本轮回复目标或待回答的问题：'
            + ', '.join(dict.fromkeys(ids)) + '。请围绕本轮新消息或当前主动入口决策；没有新问题时可以结束，不要反复回到旧问题。')
