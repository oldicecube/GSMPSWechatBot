"""Management metadata for shell modules. No native plugin imports or migration.

The IDs are namespaced by executor, so plugins/bv.py and auto/bv.py never share
a switch. State is read at execution time by the original shell Dispatcher.
"""
import ast
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import threading
import weakref
import shutil
from datetime import datetime

SOURCE = 'GSMPS Wechatbot'
ADAPTER_ID = 'gsmps.adapter.weflow'
_lock = threading.RLock()
_dispatcher = None


def root():
    return Path(os.environ.get('MAIBOT_INSTANCE_DIR') or Path(__file__).resolve().parents[1])


def bind_dispatcher(dispatcher):
    global _dispatcher
    _dispatcher = weakref.ref(dispatcher)


def get_dispatcher():
    return _dispatcher() if _dispatcher else None


def state_path():
    return root() / 'config/gsmps_management.json'


def state():
    path = state_path()
    if not path.exists():
        return {}
    # Invalid management state fails closed instead of enabling every module.
    value = json.loads(path.read_text(encoding='utf8'))
    if not isinstance(value, dict):
        raise ValueError('Invalid GSMPS management state')
    return value


def enabled(plugin_id):
    try:
        return state().get(plugin_id, {}).get('enabled', True) is True
    except (OSError, ValueError, TypeError):
        return False


def set_enabled(plugin_id, value):
    if type(value) is not bool or not find(plugin_id):
        raise ValueError('Unknown shell plugin or invalid enabled value')
    with _lock:
        values = state()
        values.setdefault(plugin_id, {})['enabled'] = value
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf8')
        os.replace(temporary, path)
    return value


def save_controls(plugin_id, enabled_value=None, settings_patch=None, bindings_patch=None):
    """Persist one validated form atomically, including shared config bindings."""
    if not find(plugin_id):
        raise ValueError('Unknown shell plugin')
    with _lock:
        values = state()
        entry = values.setdefault(plugin_id, {})
        if enabled_value is not None:
            if type(enabled_value) is not bool:
                raise ValueError('enabled must be a boolean')
            entry['enabled'] = enabled_value
        entry.setdefault('settings', {}).update(settings_patch or {})
        values.setdefault('_bindings', {}).update(bindings_patch or {})
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')
        os.replace(temporary, path)


def reset_controls(plugin_id, binding_paths):
    """Drop overrides only; restore deployment values rather than empty secrets."""
    with _lock:
        values = state()
        path = state_path()
        backup = None
        if path.exists():
            backup = path.with_name(path.name + '.reset-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.bak')
            shutil.copy2(path, backup)
        values.pop(plugin_id, None)
        for key in binding_paths:
            values.get('_bindings', {}).pop(key, None)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf8')
        os.replace(temporary, path)
        return str(backup) if backup else None


@lru_cache(maxsize=256)
def _metadata(path_text, mtime):
    path = Path(path_text)
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    functions = {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    values = {}
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for target in n.targets:
                if isinstance(target, ast.Name) and target.id in {'COMMAND', 'ALIASES', 'AUTO_COMMANDS', 'MATCH_RAW_MESSAGE', 'WEBUI_CONFIG', 'SUPPORTED_PLATFORMS'}:
                    try:
                        values[target.id] = ast.literal_eval(n.value)
                    except (ValueError, TypeError):
                        pass
    return functions, values, (ast.get_docstring(tree) or '').split('\n')[0][:500]


def catalog():
    rows = []
    dispatcher = get_dispatcher()
    try:
        controls = state()
    except (OSError, ValueError):
        controls = None
    for kind, folder in [('command', 'plugins'), ('automation', 'auto')]:
        for path in sorted((root() / folder).glob('*.py')):
            if path.name.startswith('_'):
                continue
            try:
                functions, values, description = _metadata(str(path), path.stat().st_mtime_ns)
            except (SyntaxError, UnicodeError):
                continue
            handler = 'handle' if kind == 'command' else 'handle_auto'
            if not ({handler, 'start'} & functions):
                continue
            plugin_id = f'gsmps.{kind}.{path.stem}'
            commands = [values.get('COMMAND') or '/' + path.stem] if kind == 'command' else sorted(values.get('AUTO_COMMANDS') or [])
            aliases = sorted(values.get('ALIASES') or []) if kind == 'command' else []
            is_enabled = controls is not None and controls.get(plugin_id, {}).get('enabled', True) is True
            module_key = path.stem if kind == 'command' else 'auto:' + path.stem
            loaded = bool(dispatcher and module_key in dispatcher.modules and is_enabled)
            rows.append(dict(id=plugin_id, source=SOURCE, external=True, kind=kind, module=path.stem,
                module_key=module_key, commands=commands, aliases=aliases, handler=handler if handler in functions else '',
                enabled=is_enabled, disabled=not is_enabled, loaded=loaded,
                load_status=('success' if loaded else 'not_loaded') if is_enabled else 'disabled',
                path=str(path), load_error='', usage_seconds=0, circuit_status=None, changelog=None, release=None,
                manifest=dict(id=plugin_id, name=path.stem + ('（自动化）' if kind == 'automation' else ''),
                    version='shell', description=description or f'由原 GSMPS Wechatbot {folder}/{path.name} 执行',
                    author=dict(name=SOURCE), plugin_type='plugin', source=SOURCE, tags=[SOURCE, kind]),
                managed_operations=['toggle', 'commands'], invocable=bool(handler in functions)))
            rows[-1]['config_fields'] = (values.get('WEBUI_CONFIG') or {}).get('fields', {})
            rows[-1]['platforms'] = values.get('SUPPORTED_PLATFORMS') or ['wechat','qq']
    rows.append(dict(id=ADAPTER_ID, source=SOURCE, external=True, kind='adapter', module='weflow',
        module_key='weflow', commands=[], aliases=[], handler='', enabled=enabled(ADAPTER_ID), disabled=not enabled(ADAPTER_ID),
        loaded=bool(dispatcher and dispatcher.inited and enabled(ADAPTER_ID)),
        load_status='success' if dispatcher and dispatcher.inited and enabled(ADAPTER_ID) else 'not_loaded',
        path='core/weflow_client.py + llm/core/wechat_driver.py', usage_seconds=0, load_error='',
        circuit_status=None, changelog=None, release=None, invocable=False,
        manifest=dict(id=ADAPTER_ID, name='GSMPS Wechat Weflow', version='shell', author=dict(name=SOURCE),
                      description='微信 Weflow 转译层；来源信息为 wechat，保留原发送与接收实现。',
                      plugin_type='adapter', source=SOURCE, tags=[SOURCE]), managed_operations=['toggle']))
    return rows


def find(plugin_id):
    if not isinstance(plugin_id, str) or not re.fullmatch(r'gsmps\.(command|automation|adapter)\.[A-Za-z0-9_-]+', plugin_id):
        return None
    return next((p for p in catalog() if p['id'] == plugin_id), None)


def command_id(plugin_id):
    return plugin_id + '.invoke'


def commands(plugin_id=None):
    rows = []
    for plugin in catalog():
        if plugin['kind'] == 'adapter' or (plugin_id and plugin_id != plugin['id']):
            continue
        # Event-only automations are visible and manageable in command management
        # without pretending they have a user-typed slash command.
        literals = plugin['commands'] + plugin['aliases']
        rows.append(dict(id=command_id(plugin['id']), name=plugin['module'], description=plugin['manifest']['description'],
            plugin_name=plugin['id'], source=SOURCE, external=True, component_type='command',
            pattern='^(?:' + '|'.join(re.escape(c) for c in literals) + ')(?:\\s|$)' if literals else '(自动事件)',
            aliases=literals, permission='operator' if plugin['module'] in {'ban','rcon','prompt'} else 'public',
            enabled=plugin['enabled'], trigger_type='command' if literals else 'event'))
    return rows


def allowed(plugin_id, context=None):
    if context:
        from src.platform_io.chat_features import context_override
        if context_override(context, 'plugins_enabled') is False:
            return False
    platform = str((context or {}).get('platform') or 'wechat')
    row = find(plugin_id)
    if row and platform not in row.get('platforms', ['wechat','qq']):
        return False
    if platform not in {'wechat', 'qq'} or not enabled(plugin_id) or (platform == 'wechat' and not enabled(ADAPTER_ID)):
        return False
    try:
        from src.config.config import global_config
        from src.core.local_operator import has_command_permission
        cid = command_id(plugin_id)
        if cid in global_config.plugin.disabled_commands:
            return False
        from src.core.announcement_manager import global_announcement_manager
        sid = str((context or {}).get('native_session_id') or '')
        if sid and cid in global_announcement_manager.get_disabled_chat_commands(sid):
            return False
        rule = global_config.plugin.command_permissions.get(cid)
        if context:
            from core.shell_plugin_config import can_invoke
            if not can_invoke(plugin_id, context):
                return False
        if rule is not None:
            c = context or {}
            session_id = str(c.get('native_session_id') or '')
            if not session_id and c.get('sessionId'):
                from src.common.utils.utils_session import SessionUtils
                session_id = SessionUtils.calculate_session_id(str(c.get('platform') or 'wechat'),
                    group_id=str(c['sessionId']), account_id=str(c.get('account_id') or (get_dispatcher().config.get('weflow') or {}).get('myWxid') or '') if get_dispatcher() else None)
            return has_command_permission(cid, platform, str(c.get('user_id') or c.get('wxid') or ''),
                session_id, [],
                global_config.plugin.command_permissions, local_operator=False)
        return True
    except (ImportError, AttributeError, OSError, ValueError, TypeError):
        return False
