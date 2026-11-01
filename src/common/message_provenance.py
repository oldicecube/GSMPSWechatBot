"""Host-owned transport and plugin provenance, persisted in additional_config."""
from contextlib import contextmanager
from contextvars import ContextVar
from uuid import uuid4


plugin_execution = ContextVar('maibot_plugin_execution', default=None)
_active_invocations = {}


@contextmanager
def track_plugin_invocation(plugin_id, stream_id, *, trigger_message_id='', invocation_id=''):
    """Correlate separate Runner->Host RPC tasks without accepting model IDs."""
    key = uuid4().hex
    _active_invocations[key] = dict(plugin_id=str(plugin_id), stream_id=str(stream_id),
        trigger_message_id=str(trigger_message_id or ''), invocation_id=str(invocation_id or key))
    try:
        yield _active_invocations[key]
    finally:
        _active_invocations.pop(key, None)


def matching_invocation(plugin_id, stream_id):
    matches = [item for item in _active_invocations.values()
               if item['plugin_id'] == plugin_id and item['stream_id'] == str(stream_id or '')]
    # Ambiguous overlapping/background sends retain plugin identity, never guess a user.
    return matches[0] if len(matches) == 1 else {}


@contextmanager
def plugin_scope(plugin_id, *, trigger_message_id='', invocation_id='', **extra):
    token = plugin_execution.set(dict(plugin_id=str(plugin_id),
        trigger_message_id=str(trigger_message_id or ''),
        invocation_id=str(invocation_id or uuid4().hex), **extra))
    try:
        yield
    finally:
        plugin_execution.reset(token)


def stamp_message(message, *, direction, transport=''):
    """Derive source from actual route fields, never from message text/LLM args."""
    config = message.message_info.additional_config
    origin = dict(platform=str(message.platform), account_id=str(message.account_id or ''),
                  scope=str(message.scope or ''), direction=direction,
                  message_id=str(message.message_id),
                  platform_message_id=str(getattr(message, 'platform_message_id', '') or ''),
                  transport=str(transport))
    config['message_origin'] = origin
    from src.platform_io.message_capabilities import message_capabilities
    capabilities = message_capabilities(str(message.platform), is_group=message.message_info.group_info is not None)
    if capabilities:
        config['adapter_capabilities'] = capabilities
    else:
        config.pop('adapter_capabilities', None)
    execution = plugin_execution.get()
    if direction == 'outbound' and execution:
        config['plugin_activity'] = {k: str(execution.get(k) or '') for k in
            ('plugin_id', 'trigger_message_id', 'invocation_id')}
        config['plugin_activity']['kind'] = 'plugin_output'
        config['learnable'] = False
        if isinstance(execution.get('relay_origin'),dict):
            config['relay_origin'] = dict(execution['relay_origin'])
    return origin


def is_plugin_message(message):
    return bool(message.message_info.additional_config.get('plugin_activity', {}).get('plugin_id'))
