"""/ban: block bot access by verified WeChat/QQ ID (not QQ group muting)."""
WEBUI_CONFIG = {'version': 1, 'fields': {}}
COMMAND = '/ban'
ADMIN_WXIDS = []


def init(config=None):
    global ADMIN_WXIDS
    ADMIN_WXIDS = ((config or {}).get('llm') or {}).get('admin_wxids') or []


def is_admin(context):
    from src.config.config import global_config
    from src.core.local_operator import has_command_permission
    platform = str(context.get('platform') or 'wechat')
    actor = str(context.get('user_id') or context.get('wxid') or (context.get('raw') or {}).get('wxid') or '')
    if platform == 'qq':
        actor = actor.removeprefix('qq:')
    scoped = f'{platform}:{actor}'
    legacy = actor if platform == 'wechat' else 'qq:' + actor
    return (legacy in ADMIN_WXIDS or scoped in ADMIN_WXIDS
            or has_command_permission('gsmps.command.ban.invoke', platform, actor,
                str(context.get('native_session_id') or ''), global_config.plugin.permission,
                global_config.plugin.command_permissions, local_operator=False))


def handle(content, context):
    if not is_admin(context):
        return '你没有权限使用此命令。'
    from utils.user_blacklist import user_key
    target = str(content or '').strip()
    platform = str(context.get('platform') or 'wechat')
    if target.startswith('qq:'):
        platform = 'qq'
    elif target.startswith('wechat:'):
        platform, target = 'wechat', target.removeprefix('wechat:')
    elif target.startswith('wxid_'):
        platform = 'wechat'
    key = user_key(platform, target)
    if not key:
        return '用法：/ban <wxid 或 QQ号>；跨端可用 qq:QQ号、wechat:wxid。此命令禁止使用 bot，不是群禁言。'
    return ban_user(key)


def ban_user(target_wxid):
    from utils.user_blacklist import add
    return (f'已将 {target_wxid} 加入 bot 黑名单' if add(target_wxid)
            else f'用户 {target_wxid} 已在黑名单中')


def load_blacklist():
    from utils.user_blacklist import load
    return load()


def save_blacklist(data):
    from utils.sqlite_store import save_document
    from utils.user_blacklist import PATH
    save_document(PATH, data)
