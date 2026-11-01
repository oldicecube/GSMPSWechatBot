"""WebUI facade for externally executed GSMPS modules, never native migration."""
import copy
import json

from fastapi import HTTPException
import tomlkit

from core import maibot_shell_registry as shell
from core import shell_plugin_config as config_api

RETIRED_COMMANDS = {'core.clear', 'builtin.plugin-management.management'}
SECRET_MASK = '********'


def _field_schema(name, field, order):
    typ = field['type']
    ui_type = ('password' if field.get('secret') else 'select' if field.get('choices') else
               {'bool': 'switch', 'int': 'number', 'float': 'number', 'list': 'list', 'json': 'textarea'}.get(typ, 'text'))
    timing = field.get('reload', 'restart' if field.get('config_path') else 'hot')
    hint = ('下次重启 bot 后生效' if timing == 'restart' else '保存后下次触发时生效')
    if field.get('secret'):
        hint += '；******** 表示保留现有值，留空表示清除'
    if field.get('description'):
        hint += '。' + field['description']
    default = field.get('default')
    if typ == 'json':
        default = json.dumps(default, ensure_ascii=False, indent=2)
    result = dict(name=name, type={'int': 'integer', 'float': 'number', 'str': 'string', 'bool': 'boolean',
                                 'list': 'array', 'json': 'string'}[typ],
                  ui_type=ui_type, default=SECRET_MASK if field.get('secret') else default,
                  label=field.get('label', name), description=hint, hint=hint, required=False,
                  hidden=False, disabled=False, order=order, reload=timing)
    for key in ('min', 'max', 'choices', 'max_length'):
        if key in field:
            result[key] = field[key]
    if typ == 'list':
        result.update(item_type='string', max_items=200)
    return result


def shell_bundle(plugin_id):
    row = shell.find(plugin_id)
    if row is None:
        return None
    declared = config_api.fields(row) if row['kind'] != 'adapter' else {}
    values = config_api.settings(plugin_id) if declared else {}
    for name, field in declared.items():
        if field.get('secret'):
            values[name] = SECRET_MASK
        elif field['type'] == 'json':
            values[name] = json.dumps(values[name], ensure_ascii=False, indent=2)
    config = {'plugin': {'enabled': row['enabled']}, 'settings': values}
    sections = {
        'plugin': dict(name='plugin', title='GSMPS Wechatbot 管理', description=row['path'], collapsed=False, order=0,
                       fields={'enabled': _field_schema('enabled', dict(type='bool', default=True, label='启用原壳子模块'), 0)})}
    if declared:
        sections['settings'] = dict(name='settings', title='功能配置', collapsed=False, order=1,
            description='执行仍由原 GSMPS 模块负责。启动生效的参数不会自动重启 bot；相同配置路径由各条目共享。',
            fields={name: _field_schema(name, field, i) for i, (name, field) in enumerate(declared.items())})
    schema = dict(plugin_id=plugin_id, plugin_info=dict(name=row['manifest']['name'], version='shell',
        description='来源：GSMPS Wechatbot。原模块运行位置不变，命令权限在麦麦设置中管理。', author=shell.SOURCE),
        sections=sections, layout=dict(type='auto', tabs=[]))
    return dict(success=True, schema=schema, config=config, raw_config=tomlkit.dumps(config), source=shell.SOURCE)


def save_shell_config(plugin_id, config):
    row = shell.find(plugin_id)
    if row is None:
        raise HTTPException(404, '未找到壳子插件')
    if not isinstance(config, dict) or not config or set(config) - {'plugin', 'settings'}:
        raise HTTPException(400, '只接受 plugin.enabled 和已声明的 settings 字段')
    general, patch = config.get('plugin', {}), config.get('settings', {})
    if not isinstance(general, dict) or set(general) - {'enabled'} or not isinstance(patch, dict):
        raise HTTPException(400, '配置节或启停字段无效')
    if 'enabled' in general and type(general['enabled']) is not bool:
        raise HTTPException(400, 'plugin.enabled 必须为布尔值')
    declared = config_api.fields(row) if row['kind'] != 'adapter' else {}
    if set(patch) - set(declared):
        raise HTTPException(400, '包含未声明的配置项')
    try:
        with shell._lock:
            current = config_api.settings(plugin_id) if declared else {}
            updated = copy.deepcopy(current)
            for name, value in patch.items():
                field = declared[name]
                if field.get('secret') and value == SECRET_MASK:
                    continue
                updated[name] = config_api.validate(name, value, field)
            config_api.validate_combined(plugin_id, updated)
            changes = {name: value for name, value in updated.items() if value != current[name]}
            bindings = {declared[name]['config_path']: value for name, value in changes.items() if declared[name].get('config_path')}
            local = {name: value for name, value in changes.items() if not declared[name].get('config_path')}
            shell.save_controls(plugin_id, general.get('enabled'), local, bindings)
    except (ValueError, TypeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    restart = any(declared[name].get('reload', 'restart' if declared[name].get('config_path') else 'hot') == 'restart' for name in changes)
    note = '配置已保存；标注为启动生效的修改需要下次手动重启 bot' if restart else '配置已保存；原壳子模块在下次触发时读取'
    return dict(success=True, enabled=shell.enabled(plugin_id), source=shell.SOURCE,
                runtime_status='restart_required' if restart else 'success' if shell.get_dispatcher() else 'stopped',
                message=note, note=note, restart_required=restart)


def reset_shell_config(plugin_id):
    row = shell.find(plugin_id)
    if row is None:
        raise HTTPException(404, '未找到壳子插件')
    paths = [field['config_path'] for field in config_api.fields(row).values() if field.get('config_path')]
    backup = shell.reset_controls(plugin_id, paths)
    return dict(success=True, message='已移除 WebUI 覆盖，恢复原 config.json 的值；启动参数在下次重启生效，共享字段同步恢复', backup=backup)
