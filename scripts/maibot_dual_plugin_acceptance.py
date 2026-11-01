"""Isolated dual-platform shell plugin contract; no real login or recipients."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
checks = []


def check(name, ok):
    checks.append({'name': name, 'passed': bool(ok)})
    print(name, bool(ok), flush=True)
    assert ok, name


async def run(instance):
    from core.dispatcher import Dispatcher
    from core.router import Router
    from core import maibot_shell_registry as registry
    from core import shell_platform_bridge as bridge
    from utils.platform_target import target_key, matches_target, resolve_configured_stream
    from src.plugin_runtime.host.message_utils import PluginMessageUtils as U
    from src.chat.message_receive.chat_manager import chat_manager
    from src.common.message_provenance import plugin_execution
    from src.common.data_models.message_component_data_model import ReplyComponent, TextComponent
    d = Dispatcher(); d.debug = False; d.load_plugins(); d.inited = True
    d.config = {'prefix': ['@test'], 'group_features': {}}
    service = NS(native_ingress=True, ingest_context=Mock(side_effect=AssertionError('QQ entered WeChat LLM')),
                 record_plugin_delivery=Mock())
    d.llm_service = service
    qq_group = target_key('qq', '123456', 'fixture', 'group', '345678')
    def payload(text='/ping', group=True, mid='fixture', extra=None):
        return dict(platform='qq', account_id='123456', scope='fixture', message_id=mid,
            message_info=dict(user_info=dict(user_id='234567', user_nickname='same-name'),
                group_info=dict(group_id='345678', group_name='same-name') if group else None,
                additional_config=extra or {}), raw_message=[dict(type='text', data=text)])
    # Every real module's Router/Dispatcher binding is exercised on both inputs;
    # business side effects are replaced by a recording handler for this matrix.
    command_modules = {id(m): m for m in d.plugins.values()}
    for module in command_modules.values():
        command = next(k for k,v in d.plugins.items() if v is module)
        for platform in ('wechat', 'qq'):
            if platform not in getattr(module,'SUPPORTED_PLATFORMS',['wechat','qq']):
                check(module.__name__+'.'+platform+'.excluded',not registry.allowed('gsmps.command.'+module.__name__.rsplit('.',1)[-1],{'platform':platform}))
                continue
            msg = (bridge.native_router_message(U._build_session_message_from_dict(payload(command))) if platform=='qq'
                   else dict(platform='wechat', account_id='wxid_test', scope='', group='fixture@chatroom',
                             sessionId='fixture@chatroom', wxid='wxid_user', user='same-name', content=command))
            router = Router([], [msg['group']], prefix_mode='mixed')
            event = router.parse(msg); context = {**msg, **event, 'raw':msg}
            recorder = Mock(return_value='fixture result')
            with patch.object(module, 'handle', recorder), patch.object(d, 'modules', {}), patch.object(d, 'llm_service', None):
                result = d.dispatch(event['command'], event['args'], context)
            check(module.__name__+'.'+platform, recorder.call_count==1 and result=='fixture result'
                  and recorder.call_args.args[1]['platform']==platform)
    for name,module in d.auto_modules.items():
        if not hasattr(module, 'handle_auto'): continue
        for platform in ('wechat','qq'):
            context = dict(platform=platform, group=qq_group if platform=='qq' else 'fixture@chatroom',
                           sessionId=qq_group if platform=='qq' else 'fixture@chatroom', wxid='qq:234567' if platform=='qq' else 'wxid_u',
                           content='fixture', raw={})
            recorder = Mock(return_value='auto result')
            with patch.object(module, 'handle_auto', recorder), patch.object(d, 'modules', {'auto:'+name:module}), patch.object(d,'llm_service',None):
                result = d.dispatch(None,None,context)
            check('auto.'+name+'.'+platform, result=='auto result' and recorder.call_count==1)
    # Restore actual handlers for native QQ -> Router -> Dispatcher -> real send
    # service -> real driver -> real codec -> intercepted OneBot API acceptance.
    pkg = ModuleType('dual_plugin_qq'); pkg.__path__=[str(ROOT/'plugins/MaiBot-SnowLuma-Adapter')]; sys.modules[pkg.__name__]=pkg
    load=lambda name:importlib.import_module(pkg.__name__+'.'+name)
    profile=load('profile'); codec=load('codecs.outbound').QQOutboundCodec(profile.ProfileState(profile.NAPCAT_PROFILE))
    plugin=load('plugin').create_plugin(); calls=[]
    async def action(name,params):
        calls.append((name,params)); return dict(status='ok',retcode=0,data={'message_id':-len(calls)})
    plugin._require_runtime_bundle=lambda:NS(runtime_state=NS(account_id='123456',scope='fixture'),outbound_codec=codec,action_service=NS(call_action=action))
    plugin._debug_logger=None
    class Supervisor:
        async def invoke_message_gateway(self,**kw):
            return NS(payload={'success':True,'result':await plugin.handle_snowluma_gateway(**kw['args'])})
    from src.platform_io.manager import PlatformIOManager
    from src.platform_io.drivers.plugin_driver import PluginPlatformDriver
    from src.platform_io.types import RouteBinding
    broker=PlatformIOManager()
    driver=PluginPlatformDriver('fixture-qq','qq',Supervisor(),'snowluma_gateway',supports_send=True,
                               account_id='123456',scope='fixture',plugin_id='fixture.qq')
    await broker.add_driver(driver)
    broker.bind_send_route(RouteBinding(driver.descriptor.route_key,driver.driver_id,driver.descriptor.kind))
    # Policy tested separately with an explicit rejection below; never touches
    # the production policy file or a live transport.
    broker.is_target_allowed=AsyncMock(return_value=True)
    bridge._routers.clear()
    d.config['group_features']={'default':{'plugins_enabled':False,'auto_plugins_enabled':False,'llm_enabled':False}}
    with patch('src.platform_io.manager.get_platform_io_manager',return_value=broker), patch('src.services.send_service.get_platform_io_manager',return_value=broker):
        # Keep only relevant real automations; no external game API/downloads.
        with patch.object(d,'modules',{'ping':importlib.import_module('plugins.ping')}):
            message=U._build_session_message_from_dict(payload(mid='qq-command'))
            check('native_qq_command_consumed',await bridge.dispatch_native_plugins(message))
            check('native_qq_one_wire_reply',len(calls)==1 and calls[0][0]=='send_group_msg' and str(calls[0][1]['group_id'])=='345678')
            check('qq_no_wechat_translator',not service.ingest_context.called and not service.record_plugin_delivery.called)
            check('command_marked_plugin_trigger',message.message_info.additional_config['plugin_activity']['kind']=='plugin_trigger')
            private=U._build_session_message_from_dict(payload(group=False,mid='qq-private'))
            check('native_private_command_consumed',await bridge.dispatch_native_plugins(private))
            check('private_returns_actual_qq_user',calls[-1][0]=='send_private_msg' and str(calls[-1][1]['user_id'])=='234567')
            check('normal_chat_admitted_once',not await bridge.dispatch_native_plugins(U._build_session_message_from_dict(payload('ordinary chat',mid='ordinary'))))
            check('unknown_native_command_passes_core',not await bridge.dispatch_native_plugins(U._build_session_message_from_dict(payload('/native_fixture',mid='native-command'))))
        # Actual shell Router/Dispatcher/status handler/native send path. Only
        # the game-server query and transport are replaced; no real group send.
        from plugins import default_status
        def mention_payload(text='', *, target='123456', name='まひるん', identity=None, mid='self-at'):
            raw=payload(text, mid=mid, extra={'platform_bot_identity':identity} if identity else None)
            raw['raw_message']=[dict(type='at',data=dict(target_user_id=target,
                target_user_nickname='QQ bot',target_user_cardname=name)),dict(type='text',data=text)]
            return U._build_session_message_from_dict(raw)
        with patch.object(d,'modules',{'default_status':default_status}), patch.object(default_status,'status',return_value={
                'online':True,'latency_ms':2,'online_players':3,'max_players':20}) as status:
            before=len(calls)
            at=mention_payload('\u2005',mid='self-at-bare')
            check('bare_self_at_runs_default_status',await bridge.dispatch_native_plugins(at) and status.call_count==1)
            check('bare_self_at_returns_one_native_status',len(calls)==before+1 and '3/20' in str(calls[-1]))
            check('bare_self_at_retains_self_flags',at.is_at and at.is_mentioned)
            check('bare_self_at_is_plugin_trigger',at.message_info.additional_config['plugin_activity']['kind']=='plugin_trigger')
            addressed=mention_payload(' 你好',mid='self-at-chat')
            check('self_at_chat_reaches_native_once',not await bridge.dispatch_native_plugins(addressed) and addressed.is_at and status.call_count==1)
            command=mention_payload(' /ping',mid='self-at-command')
            with patch.object(d,'modules',{'ping':importlib.import_module('plugins.ping')}):
                check('self_at_command_matches_after_cleaning',await bridge.dispatch_native_plugins(command) and '砰!' in str(calls[-1]))
            other=mention_payload('',target='777777',name='まひるん',mid='other-at-same-name')
            check('same_name_other_member_not_self_prefix',not await bridge.dispatch_native_plugins(other) and not other.is_at and status.call_count==1)
            media=mention_payload('',mid='self-at-image');media.raw_message.components.append(
                __import__('src.common.data_models.message_component_data_model',fromlist=['ImageComponent']).ImageComponent(binary_hash='fixture-image'))
            check('self_at_image_not_empty_status_request',not await bridge.dispatch_native_plugins(media) and status.call_count==1)
            identity=dict(user_id='123456',group_id='345678',nickname='QQ bot',cardname='まひるん')
            typed=U._build_session_message_from_dict(payload('@まひるん\u2005',mid='typed-name',extra={'platform_bot_identity':identity}))
            check('current_group_card_is_dynamic_prefix',await bridge.dispatch_native_plugins(typed) and status.call_count==2)
            typed_chat=U._build_session_message_from_dict(payload('@まひるん 你好',mid='typed-card-chat',extra={'platform_bot_identity':identity}))
            check('typed_group_card_chat_preserves_native_mention',not await bridge.dispatch_native_plugins(typed_chat)
                  and typed_chat.is_mentioned and not typed_chat.is_at)
            renamed={**identity,'cardname':'new-card'}
            old=U._build_session_message_from_dict(payload('@まひるん',mid='old-card',extra={'platform_bot_identity':renamed}))
            check('old_card_not_kept_as_stale_prefix',not await bridge.dispatch_native_plugins(old) and status.call_count==2)
            # Native force-reply policy is exercised on the non-plugin path.
            from src.maisaka.runtime import MaisakaHeartFlowChatting
            arm=Mock(); runtime=NS(_arm_forced_turn=arm)
            with patch('src.maisaka.runtime.global_config',NS(chat=NS(reply_timing=NS(inevitable_at_reply=True,mentioned_bot_reply=False)))):
                MaisakaHeartFlowChatting._update_message_trigger_state(runtime,addressed)
            check('self_at_chat_arms_native_inevitable_reply',arm.call_count==1 and arm.call_args.kwargs['is_at'])
            # The native system prompt can learn our actual card before @.
            from src.maisaka.chat_loop_service import MaisakaChatLoopService
            from src.maisaka.context.messages import SessionBackedMessage
            original=U._build_session_message_from_dict(payload('ordinary',mid='identity-hint',extra={'platform_bot_identity':identity}))
            bridge.native_router_message(original)
            entry=SessionBackedMessage.from_session_message(original,raw_message=original.raw_message,visible_text='ordinary')
            loop=NS(_session_id=original.session_id,_is_group_chat=True,_bot_platform='',_bot_platform_nickname='',
                _bot_group_cardname='',_localized_text=lambda values:values['zh-CN'])
            notice=MaisakaChatLoopService._build_bot_nickname_notice(loop,[entry])
            check('native_prompt_knows_verified_group_card_without_at','まひるん' in notice and '你在本群' in notice)
            no_flags=mention_payload('hi',mid='native-at-no-flags')
            no_flags.is_at=no_flags.is_mentioned=False
            from src.chat.utils.utils import is_mentioned_bot_in_message
            check('native_self_id_detects_at_without_adapter_flags',is_mentioned_bot_in_message(no_flags)[:2]==(True,True))
            with patch('src.chat.utils.utils.get_bot_accounts',return_value=['123456','777777']):
                check('other_configured_bot_at_is_not_this_account',not is_mentioned_bot_in_message(other)[1])
            invalid=U._build_session_message_from_dict(payload('ordinary',mid='wrong-group',extra={
                'platform_bot_identity':{**identity,'group_id':'different-group'}}))
            bridge.native_router_message(invalid)
            bad_entry=SessionBackedMessage.from_session_message(invalid,raw_message=invalid.raw_message,visible_text='ordinary')
            clean_loop=NS(**{**vars(loop),'_bot_platform':'','_bot_platform_nickname':'','_bot_group_cardname':''})
            check('foreign_group_identity_not_our_prompt',not MaisakaChatLoopService._build_bot_nickname_notice(clean_loop,[bad_entry]))
            with patch.dict(d.enabled_modules,{'default_status':False}):
                disabled=mention_payload('',mid='disabled-status')
                check('disabled_status_preserves_native_at_fallback',not await bridge.dispatch_native_plugins(disabled) and disabled.is_at)
        context=bridge.native_router_message(message)
        from core.plugin_activity import legacy_plugin_scope
        from core.sender import send
        with legacy_plugin_scope(service,'ping',context):
            result=await asyncio.to_thread(send,qq_group,'thread send')
        check('sync_plugin_send_returns_native_receipt',result[0] and calls[-1][0]=='send_group_msg')
        before=len(calls)
        check('cross_chat_send_rejected',not (await bridge.send_output(context,target='999999',content='blocked'))[0] and len(calls)==before)
        with legacy_plugin_scope(service,'ping',context):
            ok=await bridge.send_output(context,target=qq_group,content='hello',native_segments=[{'type':'mention','user_id':'234567'},{'type':'text','text':' hello'}])
        check('native_at_fields_preserved',ok[0] and calls[-1][1]['message'][0]=={'type':'at','data':{'qq':'234567'}})
        from PIL import Image
        image=instance/'test.png';Image.new('RGB',(4,4),'red').save(image)
        with legacy_plugin_scope(service,'picture',context):
            ok=await bridge.send_output(context,target=qq_group,file_path=image,mode='wechat_file')
        check('image_sent_as_qq_image',ok[0] and calls[-1][1]['message'][0]['type']=='image')
        media=instance/'test.mp4';media.write_bytes(b'fixture')
        with legacy_plugin_scope(service,'bv',context):
            ok=await bridge.send_output(context,target=qq_group,file_path=media,mode='wechat_file')
        check('video_sent_as_native_video',ok[0] and calls[-1][1]['message'][0]['type']=='video')
        voice=instance/'test.silk';voice.write_bytes(b'fixture')
        with legacy_plugin_scope(service,'otto',context):
            ok=await bridge.send_output(context,target=qq_group,file_path=voice,mode='wechat_voice')
        check('voice_sent_as_native_record',ok[0] and calls[-1][1]['message'][0]['type']=='record')
        # Real two-message symmetry workflow, including the original binary
        # and sender-scoped follow-up; no WeFlow image lookup on QQ.
        import auto.picture as picture
        from src.common.data_models.message_component_data_model import ImageComponent
        with patch.object(d,'modules',{'auto:picture':picture}), patch.object(picture,'SAVE_DIR',str(instance)):
            begin=U._build_session_message_from_dict(payload('/对称 左右',mid='symmetry-begin'))
            check('qq_picture_followup_command_owned',await bridge.dispatch_native_plugins(begin))
            incoming=U._build_session_message_from_dict(payload('',mid='symmetry-image'))
            incoming.raw_message.components=[ImageComponent(binary_data=image.read_bytes(),binary_hash='fixture-image')]
            check('qq_picture_followup_processed',await bridge.dispatch_native_plugins(incoming))
            check('qq_picture_workflow_sent_image',calls[-1][1]['message'][0]['type']=='image')
        # Explicit persisted destination routes only to its existing session.
        from llm.core.maibot_runtime import native_runtime
        with patch.object(native_runtime,'_loop',asyncio.get_running_loop()):
            check('background_explicit_qq_target', (await asyncio.to_thread(send,qq_group,'background'))[0])
        broker.is_target_allowed.return_value=False
        before=len(calls)
        check('blocked_adapter_policy_no_plugin_send',await bridge.dispatch_native_plugins(U._build_session_message_from_dict(payload(mid='blocked'))) and len(calls)==before)
    # Actual patpat only consumes structured pokes addressed to this account.
    import auto.patpat as pat
    poke=U._build_session_message_from_dict(payload('notice',mid='poke',extra={'napcat_notice_payload':{'notice_type':'notify','sub_type':'poke','self_id':123456,'user_id':234567,'target_id':123456}}));poke.is_notify=True
    c=bridge.native_router_message(poke)
    check('qq_actual_poke_matches_patpat',pat._is_bot_pat(c))
    check('qq_text_cannot_spoof_poke',not pat._is_bot_pat({**c,'is_notify':False}))
    check('qq_other_poke_not_bot',not pat._is_bot_pat({**c,'qq_notice':{**c['qq_notice'],'target_id':999999}}))
    quoted=U._build_session_message_from_dict(payload('/ping',mid='quote'))
    quoted.raw_message.components.insert(0,ReplyComponent('-9','/rcon dangerous','123456'))
    q=bridge.native_router_message(quoted)
    event=Router([], [qq_group],prefix_mode='mixed').parse(q)
    check('quote_body_never_command_input',event['command']=='/ping' and q['quoted_content']=='/rcon dangerous')
    # Identity/display changes do not rename the plugin conversation or wallet.
    renamed=U._build_session_message_from_dict(payload(mid='renamed'));renamed.message_info.user_info.user_nickname='new name'
    r=bridge.native_router_message(renamed)
    check('nickname_change_keeps_qq_identity',r['wxid']==context['wxid'] and r['sessionId']==qq_group)
    check('account_scope_distinct_targets',len({target_key('qq',a,s,'group','345678') for a,s in [('123456','fixture'),('123456','other'),('999999','fixture')]})==3)
    # Agent proxy uses the same message structure and ordinary bot identity.
    from src.plugin_runtime.owned_plugin_tools import _shell_context, catalog, _invoke_shell
    owned=NS(platform='qq',user_id='123456',stream_id=message.session_id,session_id=message.session_id,
             is_group_chat=True,group_id='345678',metadata={'anchor_message':message})
    c=_shell_context(owned)
    check('agent_context_source_preserved',c['account_id']=='123456' and c['scope']=='fixture' and c['_agent_invocation'])
    answer=await asyncio.to_thread(_invoke_shell,registry.find('gsmps.command.ping'),{'text':''},owned)
    check('agent_can_call_qq_shell_plugin',answer.get('success') and answer['kind']=='plugin_tool_result')
    answer=await asyncio.to_thread(_invoke_shell,registry.find('gsmps.command.rcon'),{'text':'list'},owned)
    check('agent_operator_command_stays_denied',not answer.get('success'))
    for name,module in d.auto_modules.items():
        if hasattr(module,'handle_auto'):continue
        captured=[]
        def capture(*args,**kw):
            captured.append(dict(plugin_execution.get() or {}));return True,''
        with patch('core.dispatcher.send',side_effect=capture):
            check('background_monitor.'+name,d._broadcast_text([qq_group],'fixture',feature='auto_plugins_enabled',module_name='auto:'+name)
                  and captured[0]['platform']=='qq')
    # Autonomous plugins resolve exact account+scope, never a same-name match.
    row={'platform':'qq','account_id':'123456','scope':'fixture','group_id':'345678','session_id':message.session_id}
    chat=NS(get_all_streams=AsyncMock(return_value=[row,{**row,'account_id':'999999'}]))
    check('autonomy_exact_qq_stream',await resolve_configured_stream(chat,qq_group)==row)
    chat.get_all_streams.return_value=[row,row]
    check('autonomy_ambiguous_session_rejected',await resolve_configured_stream(chat,qq_group) is None)
    # QQ and WeChat adapter switches are independent.
    registry.set_enabled(registry.ADAPTER_ID,False)
    check('wechat_switch_does_not_disable_qq_plugins',registry.allowed('gsmps.command.ping',context)
          and not registry.allowed('gsmps.command.ping',{'platform':'wechat'}))
    registry.set_enabled(registry.ADAPTER_ID,True)
    from src.core.announcement_manager import global_announcement_manager as controls
    cid=registry.command_id('gsmps.command.ping')
    controls.disable_specific_chat_command(context['native_session_id'],cid)
    check('native_per_chat_command_control_applies_to_qq_shell',not registry.allowed('gsmps.command.ping',context))
    controls.enable_specific_chat_command(context['native_session_id'],cid)
    check('native_per_chat_command_can_be_reenabled',registry.allowed('gsmps.command.ping',context))
    from src.webui.routers.chat.routes import _chat_session_to_response, _write_chat_talk_frequency_rule, TalkFrequencyUpdateRequest
    from src.common.database.database import get_db_session
    from src.common.database.database_model import ChatSession
    from sqlmodel import select
    await chat_manager.get_or_create_session('wechat','wxid_user','345678@chatroom',account_id='wxid_fixture')
    with get_db_session() as database:
        rows=database.exec(select(ChatSession)).all()
        qq_row=next(r for r in rows if r.platform=='qq' and r.group_id=='345678')
        wx_row=next(r for r in rows if r.platform=='wechat')
        qq_row.group_name=wx_row.group_name='GDUT SIE Minecraft Public Server'
        entries=[_chat_session_to_response(r,None,0,0,0) for r in (qq_row,wx_row)]
        check('webui_same_group_name_separate_adapter_entries',entries[0]['group_name']==entries[1]['group_name']
              and entries[0]['session_id']!=entries[1]['session_id'] and {r['platform'] for r in entries}=={'qq','wechat'})
        _write_chat_talk_frequency_rule(qq_row,TalkFrequencyUpdateRequest(value=0))
        import tomllib
        cfg=tomllib.loads((instance/'config/bot_config.toml').read_text(encoding='utf8'))
        check('native_webui_can_disable_qq_llm_without_wechat_rule',any(r.get('platform')=='qq' and r.get('item_id')=='345678'
              and r.get('value')==0 for r in cfg['chat']['reply_timing']['talk_value_rules']))
    from scripts.configure_napcat import prepare
    shutil.copytree(ROOT/'plugins/MaiBot-SnowLuma-Adapter',instance/'plugins/MaiBot-SnowLuma-Adapter',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    prepared=prepare(instance,account='123456',groups=['345678'],owner='234567',enable=True)
    check('setup_saves_quick_login_account',prepared['enabled'] and json.loads((instance/'depends-data/napcat/config/maibot-launch.json').read_text())['account']=='123456')
    # Name lookup uses the real local WS protocol and rejects duplicates before
    # publishing adapter/policy changes.
    from aiohttp import web
    from scripts.configure_napcat import resolve_group_names
    queries=[]; group_rows=[{'group_id':345678,'group_name':'GDUT SIE Minecraft Public Server'}]
    async def ws_handler(request):
        ws=web.WebSocketResponse();await ws.prepare(request)
        async for item in ws:
            value=json.loads(item.data);queries.append(value['action'])
            data={'user_id':123456} if value['action']=='get_login_info' else group_rows
            await ws.send_json({'echo':value['echo'],'retcode':0,'data':data})
        return ws
    app=web.Application();app.router.add_get('/',ws_handler)
    runner=web.AppRunner(app);await runner.setup();site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    config_path=instance/'plugins/MaiBot-SnowLuma-Adapter/config.toml';original=config_path.read_text(encoding='utf8')
    import tomlkit
    cfg=tomlkit.parse(original);cfg['client']['port']=site._server.sockets[0].getsockname()[1]
    config_path.write_text(tomlkit.dumps(cfg),encoding='utf8')
    try:
        found=await resolve_group_names(['GDUT SIE Minecraft Public Server'],root=instance)
        check('group_name_resolves_actual_logged_in_ids',found==('123456',['345678']))
        group_rows.append({'group_id':999999,'group_name':'GDUT SIE Minecraft Public Server'})
        try:
            await resolve_group_names(['GDUT SIE Minecraft Public Server'],root=instance)
            ambiguous=False
        except ValueError:ambiguous=True
        check('duplicate_group_names_fail_closed',ambiguous)
        check('group_lookup_never_sends_chat',set(queries)=={'get_login_info','get_group_list'})
    finally:
        await runner.cleanup();config_path.write_text(original,encoding='utf8')
    print('ONEBOT_FIXTURE_CALLS',len(calls),flush=True)


def main():
    with tempfile.TemporaryDirectory(prefix='dual-plugin-',dir=ROOT/'temp') as temp:
        instance=Path(temp)
        for folder in ('config','plugins','auto'):
            (instance/folder).mkdir()
        for name in ('bot_config.toml','model_config.toml'):
            shutil.copy2(ROOT/'config'/name,instance/'config'/name)
        import tomlkit
        config_path=instance/'config/bot_config.toml'
        config=tomlkit.parse(config_path.read_text(encoding='utf8'))
        config['a_memorix']['plugin']['enabled']=False
        config_path.write_text(tomlkit.dumps(config),encoding='utf8')
        for folder in ('plugins','auto'):
            for source in (ROOT/folder).glob('*.py'):shutil.copy2(source,instance/folder/source.name)
        os.environ['MAIBOT_INSTANCE_DIR']=str(instance)
        async def execute():
            try:
                await run(instance)
            finally:
                from src.services.memory_flow_service import memory_automation_service
                await memory_automation_service.shutdown()
                from src.A_memorix.host_service import a_memorix_host_service
                await a_memorix_host_service.stop()
        try:asyncio.run(execute())
        except Exception:
            import traceback
            traceback.print_exc();checks.append({'name':'unhandled_exception','passed':False})
        finally:
            db=sys.modules.get('src.common.database.database')
            if db:db.engine.dispose()
    report=ROOT/'docs/maibot-wechat-acceptance/dual-plugin-20261030.json'
    report.write_text(json.dumps({'checks':checks,'passed':all(c['passed'] for c in checks),'real_qq_login':False,'physical_messages':False,'restarted':False},ensure_ascii=False,indent=2),encoding='utf8')
    print('REPORT',report)
    return 0 if checks and all(c['passed'] for c in checks) else 1


if __name__=='__main__':raise SystemExit(main())
