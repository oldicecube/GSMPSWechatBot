"""Real, admitted group sessions whose native proactive reply frequency is positive."""
async def active_groups(platform=None):
    from sqlmodel import select
    from src.chat.message_receive.chat_manager import chat_manager
    from src.common.database.database import get_db_session
    from src.common.database.database_model import ChatSession
    from src.common.utils.utils_config import ChatConfigUtils
    from src.platform_io import get_platform_io_manager, RouteKey
    from core.maibot_shell_registry import get_dispatcher
    manager = get_platform_io_manager()
    with get_db_session(auto_commit=False) as database:
        ids = database.exec(select(ChatSession.session_id).where(ChatSession.group_id != None)).all()
    result = []
    dispatcher = get_dispatcher()
    for sid in ids:
        session = chat_manager.get_existing_session_by_session_id(sid)
        if not session or not session.group_id or session.platform not in {'qq','wechat'} or not session.account_id:
            continue
        if platform and session.platform != platform:
            continue
        if ChatConfigUtils.get_talk_value(sid, is_group_chat=True) <= 0:
            continue
        if session.platform == 'wechat' and dispatcher:
            features=dispatcher.config.get('group_features') or {}
            key=session.group_id if session.group_id in features else session.group_name
            if not dispatcher._context_feature_enabled(dict(platform=session.platform,account_id=session.account_id,
                    scope=session.scope,group_id=session.group_id,group=key,sessionId=session.group_id),'llm_enabled'):
                continue
        route = RouteKey(platform=session.platform, account_id=session.account_id, scope=session.scope)
        from src.platform_io.chat_features import route_override
        if any(route_override(route,session.group_id,feature) is False for feature in ('llm_enabled','plugins_enabled')):
            continue
        if not await manager.is_target_allowed(route, chat_type='group', target_id=session.group_id):
            continue
        result.append(session)
    return result
