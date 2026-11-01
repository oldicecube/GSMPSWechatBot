"""Conversation-local feature gates, independent of adapter admission/identity.

Only explicit switches override old behaviour. Reading a page never migrates
settings, and display names never identify a group. Cache invalidates on file
replacement as well as edits; no production process restart is needed for edits.
"""
from functools import lru_cache

from .adapter_policy import AdapterIdentity, AdapterPolicyManager, get_adapter_policy_manager
from .types import RouteKey


@lru_cache(maxsize=2048)
def _read(path, fingerprint, identity, group_id):
    return AdapterPolicyManager(path).get_group_features(identity, group_id)


def route_override(route, group_id, feature):
    if not group_id or route.platform not in {'qq', 'wechat'}:
        return None
    from .manager import get_platform_io_manager
    policy = get_adapter_policy_manager()
    path = policy.policy_path
    try:
        stat = path.stat()
        fingerprint = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
    except FileNotFoundError:
        return None
    choices = []
    for driver in get_platform_io_manager().resolve_drivers(route):
        descriptor = driver.descriptor
        identity = AdapterIdentity(adapter_id=driver.driver_id, plugin_id=descriptor.plugin_id or '',
            gateway_name=str(descriptor.metadata.get('gateway_name', '')), platform=route.platform,
            account_id=route.account_id or descriptor.account_id, scope=route.scope or descriptor.scope)
        value = _read(path, fingerprint, identity, str(group_id)).get(feature)
        if value is not None:
            choices.append(value)
    return all(choices) if choices else None


def message_override(message, feature):
    group = message.message_info.group_info
    return route_override(RouteKey(message.platform, message.account_id, message.scope),
                          group.group_id if group else '', feature)


def session_override(session_id, feature):
    if not session_id:
        return None
    from src.chat.message_receive.chat_manager import chat_manager
    session = chat_manager.get_existing_session_by_session_id(session_id)
    if not session:
        return None
    return route_override(RouteKey(session.platform, session.account_id, session.scope), session.group_id, feature)


def context_override(context, feature):
    platform = str(context.get('platform') or context.get('source_platform') or 'wechat')
    account = context.get('account_id') or context.get('bot_wxid')
    if platform=='wechat' and not account:
        from core.maibot_shell_registry import get_dispatcher
        dispatcher = get_dispatcher()
        account = ((dispatcher.config.get('weflow') or {}).get('myWxid') if dispatcher else None)
    return route_override(RouteKey(platform, account or None, context.get('scope') or None),
                          context.get('group_id') or (context.get('sessionId') if platform=='wechat' else ''), feature)


def legacy_defaults(identity, targets):
    """Show existing shell settings without silently broadening them on save."""
    from core.maibot_shell_registry import get_dispatcher
    dispatcher = get_dispatcher()
    result = {}
    for target in targets:
        flags = dict(llm_enabled=True, plugins_enabled=True)
        if identity.platform == 'wechat' and dispatcher:
            from src.chat.message_receive.chat_manager import chat_manager
            sessions = chat_manager.sessions.values()
            name = next((s.group_name for s in sessions if s.platform=='wechat' and s.group_id==target
                         and s.account_id==identity.account_id), '')
            config = dispatcher.config.get('group_features') or {}
            key = target if target in config else name
            flags = dict(llm_enabled=dispatcher._group_feature_enabled(key,'llm_enabled'),
                plugins_enabled=dispatcher._group_feature_enabled(key,'plugins_enabled') or
                                dispatcher._group_feature_enabled(key,'auto_plugins_enabled'))
        result[target] = flags
    return result
