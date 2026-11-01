"""Trusted legacy plugin execution scopes; only successful sends are recorded."""
from contextlib import contextmanager
from src.common.message_provenance import plugin_scope, plugin_execution


@contextmanager
def legacy_plugin_scope(service, plugin_id, context):
    from llm.core.wechat_translation import external_id, wire_fields
    fields = wire_fields(context)
    trigger = external_id(fields, str(fields.get('sessionId') or fields.get('group') or ''))
    platform = str(context.get('platform') or fields.get('platform') or fields.get('source_platform') or 'wechat')
    recorder = getattr(service, 'record_plugin_delivery', None) if platform == 'wechat' else None
    with plugin_scope('shell.' + str(plugin_id), trigger_message_id=trigger,
                      context=dict(context), on_delivery=recorder,
                      platform=platform):
        yield plugin_execution.get()


def tag_plugin_result(result, plugin_id, context, service):
    if not callable(getattr(service, 'record_plugin_delivery', None)):
        return result
    if isinstance(result, str):
        result = dict(messages=[result], mode='wechat_text')
    if isinstance(result, dict) and not result.get('native_delivery_owned'):
        result = {**result, '_shell_plugin_id': str(plugin_id)}
    return result
