"""Shared exact-match group policy used by optional auto modules."""


def normalize_target_groups(value):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item).strip() for item in value if str(item).strip()}


def message_group_name(context):
    context = context if isinstance(context, dict) else {}
    for key in ("groupName", "group_name", "group"):
        value = context.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def is_allowed_group(context, target_groups):
    context = context if isinstance(context, dict) else {}
    if not context.get("is_group"):
        return True
    name = message_group_name(context)
    return bool(name) and name in normalize_target_groups(target_groups)
