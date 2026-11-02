"""GSMPS command: one explicit repost, without involving Planner."""
from contextvars import copy_context
import threading

COMMAND = '/shit'
_slots = threading.BoundedSemaphore(2)
WEBUI_CONFIG = {'version': 1, 'fields': {}}


async def _invoke(source, context):
    from src.chat.message_receive.chat_manager import chat_manager
    from src.plugin_runtime.integration import get_plugin_runtime_manager
    from utils.platform_target import parse_target
    platform = str(context.get('platform') or 'wechat')
    sid = str(context.get('native_session_id') or '')
    target = str(context.get('sessionId') or context.get('group_id') or context.get('group') or '')
    parsed = parse_target(target)
    group_id = str(context.get('group_id') or parsed['target_id'])
    account = str(context.get('account_id') or parsed.get('account_id') or '')
    scope = context.get('scope', parsed.get('scope'))
    sessions = [session for session in chat_manager.sessions.values()
                if session.platform == platform and session.group_id and session.account_id
                and (session.session_id == sid if sid else str(session.group_id) == group_id or session.group_name == target)
                and (not account or str(session.account_id) == account)
                and (scope is None or str(session.scope or '') == str(scope))]
    if len(sessions) > 1 and not sid:
        from src.common.utils.utils_session import SessionUtils
        sessions = [session for session in sessions if session.session_id == SessionUtils.calculate_session_id(
            session.platform, group_id=session.group_id, account_id=session.account_id, scope=session.scope)]
    if len(sessions) != 1:
        return {'success': False, 'sent': False, 'reason': '无法唯一匹配本群的原生会话，请先检查适配器连接'}
    name = 'repost.manual_one' if source == 'bv' else 'repost.tieba_manual'
    result = await get_plugin_runtime_manager()._cap_api_call('gsmps.command.shit', 'api.call',
        {'api_name': name, 'args': {'stream_id': sessions[0].session_id}})
    while isinstance(result, dict) and isinstance(result.get('result'), dict):
        result = result['result']
    return result


def _work(source, context):
    try:
        from llm.core.maibot_runtime import native_runtime
        from core.sender import send
        result = native_runtime.submit(_invoke(source, context), timeout=360)
        if not isinstance(result, dict) or result.get('sent') is not True:
            reason = str(result.get('reason') or result.get('error') or '未找到符合条件的内容') if isinstance(result, dict) else '回执无效'
            send(target=context.get('group') or context.get('sessionId'), mode='wechat_text', content='本次搬屎未确认送达：'+reason[:180])
    except Exception as exc:
        print(f'[SHIT] invocation failed: {type(exc).__name__}', flush=True)
        from core.sender import send
        send(target=context.get('group') or context.get('sessionId'), mode='wechat_text',
             content='本次搬屎请求异常，尚未确认送达；未自动重试。请检查插件或适配器状态。')
    finally:
        _slots.release()


def handle(content, context):
    source = str(content or '').strip().lower() or 'bv'
    if source not in {'bv', 'tb'}:
        return '用法：/shit [bv|tb]，默认 bv；只触发一次，绕过Planner。'
    if not context.get('group') and not str(context.get('sessionId') or '').endswith('@chatroom'):
        return '请在已启用插件的群聊中使用。'
    if not _slots.acquire(blocking=False):
        return '正在执行搬屎请求，请稍后再试。'
    threading.Thread(target=copy_context().run, args=(_work, source, dict(context)), daemon=True, name='gsmps-manual-repost').start()
    return '已提交一次'+('B站' if source == 'bv' else '贴吧')+'搬屎请求。'
