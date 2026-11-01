"""Host-only proxy: capability callers never choose a user, route or admin bit."""
import asyncio
from collections import OrderedDict
import copy
import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor

PLUGIN_ID = 'wechatbot.owned-plugin-tools'
_grants = OrderedDict()
_counts = OrderedDict()
_busy = set()
_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='owned-plugin-tools')
RETIRED = {'core.clear', 'builtin.plugin-management.management'}


def issue_grant(context):
    anchor = context.metadata.get('anchor_message') if context else None
    if not anchor or not context.stream_id or context.platform not in {'wechat', 'qq'}:
        return ''
    if str(anchor.platform) != context.platform or str(anchor.session_id) != context.stream_id:
        return ''
    account = str(anchor.account_id or '')
    from src.services.bot_account_service import get_bot_accounts
    if not account or account not in get_bot_accounts(context.platform):
        return ''
    message = copy.copy(anchor)
    message.message_info = copy.copy(anchor.message_info)
    message.message_info.user_info = copy.copy(anchor.message_info.user_info)
    from utils.wechat_identity import canonical_wechat_user_id
    user_id = canonical_wechat_user_id(account) if context.platform == 'wechat' else account
    message.message_info.user_info.user_id = user_id
    message.message_info.user_info.user_nickname = 'MaiBot'
    message.message_info.user_info.user_cardname = None
    original_config = anchor.message_info.additional_config
    message.message_info.additional_config = {'agent_invocation': True, 'learnable': False,
        'wechat': copy.deepcopy(original_config.get('wechat', {})),
        'platform_io_target_user_id': original_config.get('platform_io_target_user_id', '')}
    message.is_notify = False
    trusted = copy.copy(context)
    trusted.user_id = user_id
    trusted.metadata = {'anchor_message': message, 'owned_plugin_proxy': True}
    token = secrets.token_urlsafe(32)
    now = time.monotonic()
    with _lock:
        while _grants and (next(iter(_grants.values()))[0] < now - 60 or len(_grants) >= 1024):
            _grants.popitem(last=False)
        _grants[token] = (now, trusted)
    return token


def _bounded(result):
    # Tool results are data, not assistant utterances. Actual plugin sends are
    # independently recorded by the existing Host provenance delivery scope.
    encoded = json.dumps(result, ensure_ascii=False, default=str)
    if len(encoded) > 12000:
        return {'truncated': True, 'preview': encoded[:12000]}
    return result


def _shell_context(context):
    anchor = context.metadata['anchor_message']
    if context.platform == 'qq':
        from core.shell_platform_bridge import native_router_message
        result = native_router_message(anchor)
        result.update(_agent_invocation=True, user='MaiBot', wxid='qq:'+context.user_id)
        return result
    group = anchor.message_info.group_info
    result = dict(platform=context.platform, source_platform=context.platform,
        account_id=str(anchor.account_id), native_session_id=context.stream_id,
        sessionId=str(group.group_id if group else anchor.message_info.additional_config.get('platform_io_target_user_id') or context.user_id),
        group=str(group.group_name or group.group_id) if group else '', user='MaiBot', wxid=context.user_id,
        content=str(anchor.processed_plain_text or ''), _agent_invocation=True,
        serverId=str(anchor.message_id), is_private=not bool(group), prefix_used=False)
    result['raw'] = {**anchor.message_info.additional_config.get('wechat', {}), **result, '_ts': time.time()}
    return result


def _invoke_shell(row, arguments, context):
    from core import maibot_shell_registry as registry
    from core.plugin_activity import legacy_plugin_scope
    from core.shell_plugin_config import mark_invoked
    dispatcher = registry.get_dispatcher()
    if not dispatcher or not dispatcher.inited or context.platform not in {'wechat', 'qq'}:
        return {'success': False, 'error': 'Original GSMPS executor is not connected on this platform'}
    request = _shell_context(context)
    if not registry.allowed(row['id'], request) or not dispatcher._module_enabled_for_group(row['module_key'], request['group'], request):
        return {'success': False, 'error': 'Plugin disabled, restricted or cooling down'}
    feature = 'auto_plugins_enabled' if row['kind'] == 'automation' else 'plugins_enabled'
    if not dispatcher._context_feature_enabled(request, feature):
        return {'success': False, 'error': 'Plugin feature disabled for this group'}
    command = next((c for c in registry.commands(row['id'])), {})
    if command.get('permission') == 'operator':
        return {'success': False, 'error': 'Bot uses an ordinary user identity; operator commands are unavailable'}
    module = dispatcher.modules.get(row['module_key'])
    handler = getattr(module, row['handler'], None) if row['handler'] else None
    if not callable(handler):
        return {'success': False, 'error': 'Background monitor has no user-callable handler; inspect its status instead'}
    with legacy_plugin_scope(dispatcher.llm_service, row['module_key'], request):
        if row['kind'] == 'command':
            if set(arguments) - {'text'} or not isinstance(arguments.get('text', ''), str):
                return {'success': False, 'error': 'Shell command accepts only text arguments'}
            result = handler(arguments.get('text', ''), request)
        else:
            if arguments:
                return {'success': False, 'error': 'Automation observes the actual message; fabricated events are not accepted'}
            result = handler(request)
    if result is not None:
        mark_invoked(row['id'], request)
    return {'success': True, 'source': registry.SOURCE, 'plugin_id': row['id'], 'kind': 'plugin_tool_result', 'result': _bounded(result)}


def catalog(context, query='', offset=0):
    from core import maibot_shell_registry as registry
    from src.plugin_runtime.component_query import component_query_service as components
    from src.core.tooling import ToolAvailabilityContext
    from src.core.types import ComponentType
    availability = ToolAvailabilityContext(session_id=context.session_id, stream_id=context.stream_id,
        is_group_chat=context.is_group_chat, group_id=context.group_id, user_id=context.user_id, platform=context.platform)
    rows = []
    # GSMPS shell commands/automations are deliberately excluded.  This tool
    # is the ordinary-user bridge for MaiBot-native plugins only.
    for spec in components.get_llm_available_tool_specs(availability).values():
        if spec.provider_name == PLUGIN_ID:
            continue
        rows.append(dict(target='tool:' + spec.name, kind='tool', source='maibot', enabled=spec.enabled,
            invocable=spec.enabled, description=spec.description, parameters=spec.parameters_schema))
    for _supervisor, entry in components._iter_component_entries(ComponentType.COMMAND, context=availability):
        info = components._build_command_info(entry)
        cid = info.plugin_name + '.' + info.name
        if cid in RETIRED:
            continue
        rows.append(dict(target='command:' + cid, kind='command', source='maibot', enabled=info.enabled,
            invocable=info.enabled and info.permission != 'operator', description=info.description,
            pattern=str(entry.metadata.get('command_pattern', '')), arguments={'text': '包含 /前缀的完整命令'}))
    needle = str(query).casefold()[:100]
    rows = [r for r in rows if not needle or needle in json.dumps(r, ensure_ascii=False).casefold()]
    offset = max(0, int(offset))
    return dict(success=True, identity='ordinary_bot_user', platform=context.platform, total=len(rows),
                items=rows[offset:offset + 15], next_offset=offset + 15 if offset + 15 < len(rows) else None)


async def _invoke_native(target, arguments, context):
    from src.plugin_runtime.component_query import component_query_service as components
    from src.core.tooling import ToolAvailabilityContext, ToolInvocation
    if target.startswith('tool:'):
        name = target[5:]
        availability = ToolAvailabilityContext(session_id=context.session_id, stream_id=context.stream_id,
            is_group_chat=context.is_group_chat, group_id=context.group_id, user_id=context.user_id, platform=context.platform)
        spec = next((s for s in components.get_llm_available_tool_specs(availability).values() if s.name == name and s.enabled), None)
        if not spec or spec.provider_name == PLUGIN_ID:
            return {'success': False, 'error': 'Tool unavailable or recursive proxy invocation'}
        result = await components.invoke_tool_as_tool(ToolInvocation(name, arguments=arguments,
            session_id=context.session_id, stream_id=context.stream_id), context)
        return dict(success=result.success, source='maibot', kind='plugin_tool_result',
                    content=result.content[:12000], error=result.error_message[:2000], result=_bounded(result.structured_content))
    if target.startswith('command:'):
        text = arguments.get('text', '')
        if set(arguments) != {'text'} or not isinstance(text, str):
            return {'success': False, 'error': 'Native command requires full command text'}
        found = components.find_command_by_text(text)
        if not found:
            return {'success': False, 'error': 'Command not found'}
        executor, groups, info = found
        cid = info.plugin_name + '.' + info.name
        from src.core.types import ComponentType
        availability = ToolAvailabilityContext(session_id=context.session_id, stream_id=context.stream_id,
            is_group_chat=context.is_group_chat, group_id=context.group_id, user_id=context.user_id, platform=context.platform)
        available = {entry.full_name for _, entry in components._iter_component_entries(ComponentType.COMMAND, context=availability)}
        from src.core.announcement_manager import global_announcement_manager
        if cid not in available or info.name in global_announcement_manager.get_disabled_chat_commands(context.stream_id):
            return {'success': False, 'error': 'Command unavailable in this chat'}
        from src.config.config import global_config
        from src.core.local_operator import has_command_permission
        if target[8:] != cid or cid in RETIRED or cid in global_config.plugin.disabled_commands or info.permission == 'operator':
            return {'success': False, 'error': 'Command unavailable to an ordinary bot user'}
        if cid in global_config.plugin.command_permissions and not has_command_permission(cid, context.platform,
            context.user_id, context.stream_id, [], global_config.plugin.command_permissions, local_operator=False):
            return {'success': False, 'error': 'Command ACL denied'}
        message = context.metadata['anchor_message']
        message.processed_plain_text = text
        message.is_command = True
        success, result, _ = await executor(message=message, matched_groups=groups,
            plugin_config=components.get_plugin_config(info.plugin_name))
        return dict(success=success, source='maibot', kind='plugin_tool_result', result=_bounded(result))
    return {'success': False, 'error': 'Unknown target type'}


async def execute(plugin_id, args):
    if plugin_id != PLUGIN_ID:
        return {'success': False, 'error': 'Capability reserved for owned-plugin-tools'}
    with _lock:
        grant = _grants.pop(args.get('grant', ''), None)
    if not grant or grant[0] < time.monotonic() - 60:
        return {'success': False, 'error': 'Missing, expired or consumed Host grant'}
    context = grant[1]
    if args.get('operation', 'list') == 'list':
        offset = args.get('offset', 0)
        if type(offset) is not int or offset < 0:
            return {'success': False, 'error': 'offset must be a nonnegative integer'}
        return catalog(context, args.get('query', ''), offset)
    if args.get('operation') != 'invoke':
        return {'success': False, 'error': 'Unknown operation'}
    target = args.get('target', '')
    arguments = args.get('arguments') or {}
    if not isinstance(target, str) or not isinstance(arguments, dict) or len(json.dumps(arguments, default=str)) > 8000:
        return {'success': False, 'error': 'Invalid or oversized arguments'}
    reserved = {'user_id', 'platform', 'group_id', 'stream_id', 'chat_id', 'account_id',
                'is_local_operator', '_host_grant', '_request_context', 'message'}
    if reserved & arguments.keys():
        return {'success': False, 'error': 'Identity and route arguments are owned by Host'}
    key = (context.stream_id, str(context.metadata['anchor_message'].message_id))
    now = time.monotonic()
    with _lock:
        while _counts and (next(iter(_counts.values()))[0] < now - 300 or len(_counts) >= 1024):
            _counts.popitem(last=False)
        created, count = _counts.get(key, (now, 0))
        # One directory lookup plus one actual native-plugin invocation per
        # reasoning turn.  This prevents tool loops while still allowing the
        # model to discover a target before it invokes it.
        if args.get('operation') == 'invoke' and count >= 2:
            return {'success': False, 'error': 'Only one native plugin behavior may be invoked per message turn'}
        if len(_busy) >= 2 or (context.stream_id, target) in _busy:
            return {'success': False, 'error': 'Target still running'}
        _counts[key] = (created, count + 1)
        _busy.add((context.stream_id, target))
    from core import maibot_shell_registry as registry
    row = registry.find(target)
    if row:
        return {'success': False, 'error': 'GSMPS shell plugins are excluded from MaiBot autonomous plugin triggering'}
    async def invoke():
        return await _invoke_native(target, arguments, context)
    task = asyncio.create_task(invoke())
    def finished(done):
        with _lock:
            _busy.discard((context.stream_id, target))
        if not done.cancelled():
            done.exception()  # consume a late exception after the caller timeout
    task.add_done_callback(finished)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=20)
    except asyncio.TimeoutError:
        return {'success': False, 'error': 'Invocation still running; do not repeat it', 'running': True}
    except Exception as exc:
        return {'success': False, 'error': type(exc).__name__ + ': invocation failed'}
