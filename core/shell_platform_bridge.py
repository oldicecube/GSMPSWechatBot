"""Native QQ -> existing Router/Dispatcher; shell outputs -> native Platform IO.

No second QQ listener or LLM admission. Targets retain platform IDs; only
command prefixes may use the bot's verified, conversation-local display name.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import uuid

from utils.platform_target import parse_target, target_key

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='dual-shell-plugin')
_routers = {}


def native_router_message(message):
    from src.common.data_models.message_component_data_model import AtComponent, ReplyComponent, ImageComponent, EmojiComponent, VoiceComponent, TextComponent
    from src.common.utils.utils_session import SessionUtils
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    group, user = message.message_info.group_info, message.message_info.user_info
    sid = SessionUtils.calculate_session_id(message.platform, user_id=user.user_id,
        group_id=group.group_id if group else None, account_id=message.account_id, scope=message.scope)
    message.session_id = sid
    destination = target_key(message.platform, message.account_id, message.scope,
                             'group' if group else 'private', group.group_id if group else user.user_id)
    # Legacy modules key identity/state by wxid/sessionId. Keep their names, use
    # platform-qualified values for QQ; user_id/group_id retain actual wire IDs.
    result = dict(platform=message.platform, source_platform=message.platform,
        account_id=str(message.account_id or ''), scope=str(message.scope or ''),
        native_session_id=sid, sessionId=destination, group=destination if group else '',
        group_id=group.group_id if group else '', group_name=group.group_name if group else '',
        user=user.user_cardname or user.user_nickname or user.user_id, user_id=user.user_id,
        wxid='qq:'+user.user_id, serverId=message.message_id, messageKey=message.message_id,
        type='group' if group else 'private', is_group=bool(group), is_private=not bool(group),
        is_at=bool(message.is_at), is_mentioned=bool(message.is_mentioned), is_notify=bool(message.is_notify),
        directed_to_bot=bool(message.is_at or message.is_mentioned),
        content='', timestamp=message.timestamp.timestamp(),
        native_message=PluginMessageUtils._session_message_to_dict(message),
        _native_loop=None, _shell_plugins_only=True)
    try:
        result['_native_loop'] = asyncio.get_running_loop()
    except RuntimeError:
        from llm.core.maibot_runtime import native_runtime
        result['_native_loop'] = native_runtime._loop
    texts, mentions = [], []
    identity = message.message_info.additional_config.get('platform_bot_identity') or {}
    if (not isinstance(identity,dict) or str(identity.get('user_id') or '') != str(message.account_id or '')
            or str(identity.get('group_id') or '') != str(group.group_id if group else '')):
        identity = {}
    nickname, cardname = identity.get('nickname') or '', identity.get('cardname') or ''
    for c in message.raw_message.components:
        if isinstance(c, TextComponent): texts.append(c.text)
        elif isinstance(c, AtComponent):
            mentions.append({'user_id': c.target_user_id, 'name': c.target_user_nickname or ''})
            # Preserve segment order and ID, including @other. Never infer the
            # target from its name (two members may use the same group card).
            texts.append(f'@<qq:{c.target_user_id}> ')
            if str(c.target_user_id) == str(message.account_id or ''):
                result.update(is_at=True, is_mentioned=True, directed_to_bot=True)
                message.is_at = message.is_mentioned = True
                nickname = c.target_user_nickname or nickname
                cardname = c.target_user_cardname or cardname
        elif isinstance(c, ReplyComponent):
            result.update(quoted_message_id=c.target_message_id, quoted_sender_wxid=c.target_message_sender_id,
                          quoted_sender=c.target_message_sender_cardname or c.target_message_sender_nickname or '',
                          quoted_content=c.target_message_content, reply_to_bot=c.target_message_sender_id==message.account_id)
        elif isinstance(c, (ImageComponent, EmojiComponent)):
            result.update(is_picture=True, is_emoji=isinstance(c, EmojiComponent))
            texts.append('[图片]')
            if c.binary_data: result['_image_binary'] = bytes(c.binary_data)
        elif isinstance(c, VoiceComponent):
            result['is_voice'] = True
            texts.append('[语音]')
        else:
            texts.append('[消息附件]')
    result['content'] = ''.join(texts) or ('[图片]' if result.get('is_picture') else message.processed_plain_text or '')
    result['mentions'] = mentions
    result.update(bot_self_id=str(message.account_id or ''), bot_nickname=nickname,
                  bot_group_cardname=cardname)
    if message.is_notify:
        result['qq_notice'] = message.message_info.additional_config.get('napcat_notice_payload') or {}
        result['source_kind'] = 'system_event'
        result['learnable'] = False
    result['raw'] = {k:v for k,v in result.items() if k not in {'raw', '_native_loop'}}
    return result


def context_for_target(target):
    parsed = parse_target(target)
    if parsed['platform'] != 'qq':
        return {'group': target, 'platform': 'wechat', '_background': True}
    from src.common.utils.utils_session import SessionUtils
    from llm.core.maibot_runtime import native_runtime
    sid = SessionUtils.calculate_session_id('qq', group_id=parsed['target_id'] if parsed['kind']=='group' else None,
        user_id=parsed['target_id'] if parsed['kind']=='private' else None,
        account_id=parsed['account_id'], scope=parsed['scope'])
    return dict(platform='qq', source_platform='qq', account_id=parsed['account_id'], scope=parsed['scope'],
        native_session_id=sid, sessionId=target, group=target if parsed['kind']=='group' else '',
        group_id=parsed['target_id'] if parsed['kind']=='group' else '', user_id=parsed['target_id'] if parsed['kind']=='private' else '',
        _native_loop=native_runtime._loop, _background=True)


async def send_output(context, *, target, content=None, file_path=None, mode='wechat_text', native_segments=None, **kwargs):
    from src.chat.message_receive.chat_manager import chat_manager
    from src.services.send_service import custom_reply_set_to_stream_with_message
    from src.common.data_models.message_component_data_model import MessageSequence, TextComponent, AtComponent, ImageComponent, EmojiComponent, VoiceComponent, FileComponent, DictComponent
    from src.common.message_provenance import plugin_execution, plugin_scope
    if context.get('platform') != 'qq':
        return False, 'Native shell output requires a trusted QQ context'
    session = chat_manager.get_session_by_session_id(context.get('native_session_id'))
    if (not session or session.platform != 'qq' or str(session.account_id or '') != context.get('account_id')
            or str(session.scope or '') != str(context.get('scope') or '')):
        return False, 'QQ destination session is missing or its identity changed'
    aliases = {str(context.get(k) or '') for k in ('group','sessionId','group_id','user','user_id')}
    if str(target or '') not in aliases or not target:
        return False, 'Shell output target differs from its source conversation'
    if mode not in {'wechat_text','wechat_file','wechat_voice'}:
        return False, 'Unsupported native shell output mode'
    if mode == 'wechat_text':
        if content is None or not str(content).strip(): return False, 'Empty plugin text'
        segments = []
        if native_segments:
            for item in native_segments:
                if item.get('type') == 'text': segments.append(TextComponent(text=str(item.get('text') or '')))
                elif item.get('type') == 'mention': segments.append(AtComponent(target_user_id=str(item.get('user_id') or item.get('wxid') or '')))
                else: return False, 'Unsupported shell native text segment'
        else:
            segments = [TextComponent(text=str(content))]
    else:
        path = Path(file_path or '')
        if not path.is_file(): return False, 'Plugin media file does not exist'
        if path.stat().st_size > 80*1024*1024: return False, 'Plugin media exceeds 80 MiB transfer budget'
        if mode == 'wechat_voice' or path.suffix.lower() in {'.png','.jpg','.jpeg','.webp','.gif'}:
            data = await asyncio.to_thread(path.read_bytes)
            import hashlib
            component = VoiceComponent if mode == 'wechat_voice' else EmojiComponent if path.suffix.lower()=='.gif' else ImageComponent
            segments = [component(binary_data=data, binary_hash=hashlib.sha256(data).hexdigest())]
        elif path.suffix.lower() in {'.mp4', '.webm', '.mov', '.mkv'}:
            segments = [DictComponent(data={'type':'video','data':{'url':str(path.resolve())}})]
        else:
            segments = [FileComponent.from_payload({'name':path.name, 'url':str(path.resolve())})]
    execution = plugin_execution.get()
    if not execution:
        with plugin_scope('shell.background', platform='qq', context=context):
            return await send_output(context, target=target, content=content, file_path=file_path, mode=mode, native_segments=native_segments)
    sent = await custom_reply_set_to_stream_with_message(MessageSequence(components=segments), session.session_id, typing=False, show_log=False)
    if sent is None: return False, 'Native QQ adapter rejected or failed delivery'
    execution['delivered'] = True
    from src.chat.heart_flow.heartflow_manager import heartflow_manager
    runtime = heartflow_manager.heartflow_chat_list.get(session.session_id)
    if runtime is not None:
        runtime.append_sent_message_to_chat_history(sent, source_kind='plugin_output')
    return True, ''


def send_output_sync(context, **kwargs):
    loop = context.get('_native_loop')
    if loop is None or not loop.is_running(): return False, 'QQ native core loop is unavailable'
    try:
        if asyncio.get_running_loop() is loop:
            return False, 'Synchronous plugin send must run outside the native core event loop'
    except RuntimeError:
        pass
    future = asyncio.run_coroutine_threadsafe(send_output(context, **kwargs), loop)
    try:
        return future.result(timeout=120)
    except Exception as exc:
        future.cancel()
        return False, 'QQ plugin delivery failed: '+type(exc).__name__


async def observe_message(message):
    from src.chat.message_receive.chat_manager import chat_manager
    from src.common.utils.utils_message import MessageUtils
    from src.chat.heart_flow.heartflow_manager import heartflow_manager
    await message.process(enable_heavy_media_analysis=False, enable_voice_transcription=False)
    chat_manager.register_message(message)
    await MessageUtils.store_message_to_db_async(message)
    runtime = heartflow_manager.heartflow_chat_list.get(message.session_id)
    if runtime is not None and not any(getattr(m,'message_id',None)==message.message_id for m in runtime._chat_history):
        observed = await runtime._reasoning_engine._build_history_message(message, source_kind='plugin_trigger')
        if observed is not None: runtime._reasoning_engine._insert_chat_history_message(observed)


async def dispatch_native_plugins(message):
    """Return True only when shell plugins own this event; caller admits other events once."""
    from core.maibot_shell_registry import get_dispatcher
    dispatcher = get_dispatcher()
    if message.platform != 'qq' or not dispatcher or not dispatcher.inited:
        return False
    from core.router import Router
    from src.platform_io.manager import get_platform_io_manager
    group = message.message_info.group_info
    # Recheck policy even when this entry is invoked directly rather than via SDK.
    if not await get_platform_io_manager().is_target_allowed(
            get_platform_io_manager().build_route_key_from_message(message),
            chat_type='group' if group else 'private',
            target_id=group.group_id if group else message.message_info.user_info.user_id):
        return True
    msg = native_router_message(message)
    from src.chat.message_receive.chat_manager import chat_manager
    chat_manager.register_message(message)
    await chat_manager.get_or_create_session('qq', message.message_info.user_info.user_id,
        group.group_id if group else None, account_id=message.account_id, scope=message.scope)
    key = (message.account_id, message.scope, msg['sessionId'])
    if key not in _routers:
        config = dispatcher.config
        # Explicit slash commands follow the shared Router; QQ group admission
        # belongs to the native adapter policy rather than WeChat display names.
        _routers[key] = Router(config.get('prefix', []), [msg['group'] or msg['sessionId']],
            time_slots=config.get('time_slots'), rate_limit_cfg=config.get('rate_limit'), prefix_mode='mixed')
        while len(_routers)>512: _routers.pop(next(iter(_routers)))
    # Names change per group and over time. Refresh rather than accumulating
    # global aliases or leaving the previous card in a long-lived Router.
    configured = dispatcher.config.get('prefix', [])
    configured = [configured] if isinstance(configured,str) else list(configured or [])
    configured = [str(value).strip() for value in configured if str(value).strip()]
    aliases = [f'@<qq:{message.account_id}>']
    aliases.extend('@'+str(msg[k]).strip() for k in ('bot_group_cardname','bot_nickname') if str(msg.get(k) or '').strip())
    _routers[key].prefixes = sorted(set(configured+aliases), key=len, reverse=True)
    event = _routers[key].parse(msg)
    if not event: return True
    cleaned = _routers[key]._clean(msg['content'])
    if event.get('prefix_used') and any(cleaned == alias or cleaned.startswith(alias+' ') for alias in aliases):
        # A typed local @name is a mention, while only the structured target ID
        # is an actual @. Preserve native mentioned/inevitable-at policy choices.
        message.is_mentioned = True
        msg.update(is_mentioned=True, directed_to_bot=True)
    context = {**msg, **event, 'raw':msg, '_shell_plugins_only':True}
    context['type'] = msg['type']
    command = context.get('command')
    if command:
        from src.common.bot_interaction_policy import find_bot_rule, select_plugin_refusal
        user_id = message.message_info.user_info.user_id
        group_id = group.group_id if group else ''
        known = command in dispatcher.plugins or any(command in (getattr(m,'AUTO_COMMANDS',()) or ()) for m in dispatcher.modules.values())
        if known and find_bot_rule('qq', user_id, group_id) and select_plugin_refusal('qq',user_id,group_id,message.message_id):
            message.message_info.additional_config.update(force_reply=True, bot_plugin_request=True)
            return False
    from contextvars import copy_context
    copied = copy_context()
    result = await asyncio.get_running_loop().run_in_executor(_executor,
        lambda: copied.run(dispatcher.dispatch, command, context.get('args'), context))
    activity = context.get('_host_plugin_activity')
    consumed = bool(activity or context.get('_shell_command_matched'))
    if result is not None and not (isinstance(result,dict) and (result.get('forward_to_llm') or result.get('forward_batch_to_llm'))):
        consumed = True
        from core.plugin_activity import legacy_plugin_scope
        plugin_id = result.get('_shell_plugin_id') if isinstance(result,dict) else None
        with legacy_plugin_scope(dispatcher.llm_service, plugin_id or context.get('_shell_command_matched') or 'automation', context):
            if isinstance(result, str):
                await send_output(context,target=msg['group'] or msg['user'],content=result)
            elif isinstance(result,dict) and not result.get('native_delivery_owned'):
                target = result.get('target') or msg['group'] or msg['user']
                for text in result.get('messages',[]) or ([result['content']] if result.get('content') else []):
                    await send_output(context,target=target,content=str(text),mode=result.get('mode','wechat_text'))
                if result.get('file_path'):
                    await send_output(context,target=target,file_path=result['file_path'],mode=result.get('mode','wechat_file'))
                if result.get('animation'):
                    from llm.core.maibot_emoji import get_native_emoji_path
                    path = get_native_emoji_path(result['animation'])
                    if path: await send_output(context,target=target,file_path=path,mode='wechat_file')
    if consumed:
        message.message_info.additional_config.update(learnable=False, plugin_activity=activity or
            dict(plugin_id='shell.'+str(context.get('_shell_command_matched') or 'automation'),kind='plugin_trigger',
                 trigger_message_id=message.message_id,invocation_id=uuid.uuid4().hex))
        await observe_message(message)
    return consumed
