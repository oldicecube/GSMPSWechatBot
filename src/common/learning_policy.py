"""Shared source policy for every automatic learning path.

Reception, conversation history and replies deliberately remain unaffected.
"""
from typing import Any


def is_learning_allowed(message: Any) -> bool:
    from src.config.config import global_config

    message = getattr(message, "original_message", message)
    if getattr(message, "is_notify", False):
        return False
    info = getattr(message, "message_info", None)
    metadata = getattr(info, "additional_config", {}) or {}
    if metadata.get("learnable") is False or metadata.get("wechat", {}).get("learnable") is False:
        return False
    user = getattr(info, "user_info", None)
    group = getattr(info, "group_info", None)
    platform = str(getattr(message, "platform", "") or "")
    user_id = str(getattr(user, "user_id", "") or getattr(message, "user_id", "") or "")
    group_id = str(getattr(group, "group_id", "") or getattr(message, "group_id", "") or "")
    for rule in global_config.learning.excluded_users:
        if rule.platform == platform and rule.user_id == user_id and (not rule.group_id or rule.group_id == group_id):
            return False
    return True
