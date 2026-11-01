"""通过同一个游戏用户名关联微信和 QQ，积分与人物记忆共享。"""
WEBUI_CONFIG = {'version': 1, 'fields': {'agent_enabled': {'type': 'bool', 'default': False, 'label': '允许 agent 调用', 'reload': 'hot'}}}
COMMAND = '/bind'


def handle(content, context):
    # Optional display enrichment must never prevent the identity transaction.
    # Importing it by name also broke requests in a process that still held an
    # older account_identity module during a rolling source update.
    from utils import account_identity as identity
    import logging
    import sqlite3
    context = context if isinstance(context, dict) else {}
    if context.get('_agent_invocation'):
        return '账号绑定必须由账号本人直接操作'
    text = ' '.join(map(str, content)) if isinstance(content, list) else str(content or '').strip()
    uid = identity.actor(context)
    if not uid:
        return '无法识别当前平台账号'
    try:
        if not text or text == 'status':
            info = identity.details(uid)
            return ('游戏账号：' + info['player'] + '\n已绑定平台：' + '、'.join(sorted({x.split(':',1)[0] if x.startswith('qq:') else 'wechat' for x in info['members']}))) if info else '用法：/bind <游戏玩家名>；在 QQ、微信两端绑定相同游戏名即可关联'
        info = identity.bind_game(uid, text)
    except ValueError as exc:
        return str(exc)
    except sqlite3.Error:
        logging.getLogger(__name__).exception('Binding storage failed on %s', context.get('platform','wechat'))
        return '绑定存储暂不可用，请稍后重试；不会覆盖已有的绑定。'
    note = ''
    try:
        enrich = getattr(identity, 'binding_peer_note', None)
        if callable(enrich):
            note = enrich(uid, context.get('user') or '')
    except Exception:
        logging.getLogger(__name__).exception('Binding committed; optional peer impression unavailable')
        note = '\n关联身份已保存，另一侧昵称/印象暂不可用。'
    return '绑定成功：' + str(context.get('user') or uid) + ' → ' + info['player'] + note
