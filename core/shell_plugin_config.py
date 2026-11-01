"""Declarative, validated WebUI fields for original shell module executors."""
import copy
import json
import time
import threading
import math

COMMON_FIELDS = {
    'cooldown_seconds': dict(type='int', default=0, min=0, max=3600, label='同用户调用冷却（秒）', description='0 保留原行为；按插件、群聊和用户分别限流'),
    'allow_groups': dict(type='list', item_type='str', default=[], label='允许的群', description='留空继承来源适配器白名单；微信填写核实的群名或 ID，QQ 填 qq:账号:连接:group:群号'),
    'allow_users': dict(type='list', item_type='str', default=[], label='允许的用户 ID', description='留空不额外限制；微信填 wxid，QQ 填 qq:用户号；不能赋予管理员权限'),
    'agent_enabled': dict(type='bool', default=True, label='允许麦麦调用', description='以 bot 自己的普通用户身份调用，仍受原插件权限约束'),
}
_times = {}
_lock = threading.Lock()


def fields(plugin):
    return {**copy.deepcopy(COMMON_FIELDS), **copy.deepcopy(plugin.get('config_fields') or {})}


def settings(plugin_id, *, controls=None, base=None):
    from core import maibot_shell_registry as registry
    plugin = registry.find(plugin_id)
    if not plugin:
        return {}
    if base is None:
        try:
            base = json.loads((registry.root() / 'config.json').read_text(encoding='utf8'))
        except (OSError, ValueError):
            base = {}
    controls = registry.state() if controls is None else controls
    overrides = controls.get(plugin_id, {}).get('settings', {})
    values = {}
    for name, field in fields(plugin).items():
        path = field.get('config_path')
        if path:
            value = base
            for part in path.split('.'):
                value = value.get(part) if isinstance(value, dict) and part else None
            value = controls.get('_bindings', {}).get(path, value)
        else:
            # Shell-only fields live in WebUI management state; they must not
            # accidentally receive the entire config.json object as a value.
            value = overrides.get(name)
        value = value if value is not None else field.get('default')
        values[name] = validate(name, value, field)
    return values


def apply_overrides(config):
    """Overlay declared startup bindings without rewriting the original config."""
    from core import maibot_shell_registry as registry
    result = copy.deepcopy(config or {})
    controls = registry.state()
    allowed_paths = {f['config_path']: f for p in registry.catalog() for f in fields(p).values() if f.get('config_path')}
    for path, value in controls.get('_bindings', {}).items():
        if path not in allowed_paths:
            raise ValueError(f'Unknown config binding: {path}')
        value = validate(path, value, allowed_paths[path])
        parts = path.split('.')
        target = result
        for part in parts[:-1]:
            target = target.setdefault(part, {})
        target[parts[-1]] = value
    return result


def validate(name, value, field):
    typ = field['type']
    if typ == 'json' and isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValueError(f'{name}: JSON 格式错误') from exc
    valid = ((typ == 'bool' and type(value) is bool) or (typ == 'int' and type(value) is int)
             or (typ == 'float' and type(value) in (int, float)) or (typ == 'str' and isinstance(value, str))
             or (typ == 'json' and isinstance(value, (dict, list)))
             or (typ == 'list' and isinstance(value, list) and len(value) <= 200 and all(isinstance(x, str) and len(x) <= 300 for x in value)))
    if not valid:
        raise ValueError(f'{name}: 类型必须为 {typ}')
    if typ in {'int', 'float'} and (not math.isfinite(value) or value < field.get('min', -float('inf')) or value > field.get('max', float('inf'))):
        raise ValueError(f'{name}: 超出允许范围')
    if typ == 'str' and len(value) > field.get('max_length', 2000):
        raise ValueError(f'{name}: 文本过长')
    if 'choices' in field and value not in field['choices']:
        raise ValueError(f'{name}: 不在允许的选项中')
    if typ == 'json':
        try:
            encoded = json.dumps(value, allow_nan=False)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValueError(f'{name}: 非法 JSON 内容') from exc
        if len(encoded) > 20000:
            raise ValueError(f'{name}: JSON 内容过长')
    return copy.deepcopy(value)


def validate_combined(plugin_id, values):
    """Cross-field constraints must hold before any management state is written."""
    if plugin_id == 'gsmps.command.lottery':
        if not values['min_stake'] <= values['max_stake_per_option'] <= values['daily_total_limit']:
            raise ValueError('最小投注 ≤ 单项上限 ≤ 每日总额上限')


def can_invoke(plugin_id, context):
    cfg = settings(plugin_id)
    if not cfg:
        return False
    group = str(context.get('group') or '')
    group_id = str(context.get('sessionId') or '')
    user = str(context.get('wxid') or '')
    if cfg['allow_groups'] and not ({group, group_id} & set(cfg['allow_groups'])):
        return False
    if context.get('_background'):
        # Background services have no requesting user; destination restrictions
        # still apply when their output is delivered.
        return True
    if cfg['allow_users'] and user not in cfg['allow_users']:
        return False
    if context.get('_agent_invocation') and not cfg['agent_enabled']:
        return False
    with _lock:
        return time.monotonic() - _times.get((plugin_id, group_id or group, user), -float('inf')) >= cfg['cooldown_seconds']


def mark_invoked(plugin_id, context):
    with _lock:
        now = time.monotonic()
        if len(_times) > 10000:
            for key, stamp in list(_times.items()):
                if now - stamp > 3600:
                    _times.pop(key, None)
        _times[(plugin_id, str(context.get('sessionId') or context.get('group') or ''), str(context.get('wxid') or ''))] = now
