"""Local OneBot WS + real native adapter codecs and platform routes; no QQ login/sends."""
from __future__ import annotations
import asyncio
import importlib
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CHECKS = []


def check(name, passed):
    CHECKS.append(dict(name=name, passed=bool(passed)))
    print(name, bool(passed), flush=True)


async def run(instance):
    pkg = ModuleType('napcat_acceptance')
    pkg.__path__ = [str(ROOT / 'plugins/MaiBot-SnowLuma-Adapter')]
    sys.modules[pkg.__name__] = pkg
    def load(name): return importlib.import_module(pkg.__name__ + '.' + name)
    config = load('config'); profile = load('profile')
    from src.plugin_runtime.runner.manifest_validator import ManifestValidator
    validator = ManifestValidator(project_root=ROOT, log_errors=False, log_compat_warnings=False)
    manifest = json.loads((ROOT / 'plugins/MaiBot-SnowLuma-Adapter/_manifest.json').read_text(encoding='utf8'))
    check('native_manifest_sdk_dependency_compatible', validator.validate(manifest) and not validator.errors)
    plugin = load('plugin').create_plugin()
    check('native_plugin_components_import', any(c['type'] == 'MESSAGE_GATEWAY' for c in plugin.get_components()))
    logger = logging.getLogger('qq-acceptance')
    from aiohttp import web
    actions = []; received = []; connections = []; opened = asyncio.Event(); incoming = asyncio.Event()
    own_cards={'345678':'まひるん','999999':'different-group-card'}
    token = 'isolated-fixture-not-production'
    event = dict(post_type='message', message_type='group', self_id=123456, user_id=234567,
        group_id=345678, group_name='QQ fixture', message_id=-17, time=1793278800,
        sender=dict(user_id=234567, nickname='fixture-user', card='群名片'),
        message=[dict(type='reply', data=dict(id='-9')), dict(type='at', data=dict(qq='123456')),
                 dict(type='text', data=dict(text='中文\nemoji 😀')),
                 dict(type='forward', data=dict(id='forward-fixture'))])
    async def endpoint(request):
        check('websocket_auth_header_and_query', request.headers.get('Authorization') == 'Bearer ' + token
              and request.query.get('access_token') == token)
        ws = web.WebSocketResponse(); await ws.prepare(request); connections.append(ws)
        await ws.send_json(event)
        async for message in ws:
            payload = json.loads(message.data); actions.append(payload)
            name = payload['action']
            data = {'get_login_info': dict(user_id=123456, nickname='fixture-bot'),
                    'get_version_info': dict(app_name='NapCat.Onebot', app_version='4.18.41'),
                    'get_msg': dict(message_id=-9, user_id=456789, sender=dict(user_id=456789, nickname='被引用者'),
                        message=[dict(type='text', data=dict(text='引用原文'))]),
                    'get_forward_msg': dict(messages=[dict(message_id=18, sender=dict(user_id=456789, nickname='记录作者'),
                        time=1793278790, content=[dict(type='text', data=dict(text='聊天记录正文'))])]),
                    'send_group_msg': dict(message_id=-100), 'send_private_msg': dict(message_id=-101)}.get(name, {})
            if name=='get_group_member_info':
                data=dict(user_id=payload['params']['user_id'],nickname='fixture-bot',
                          card=own_cards.get(str(payload['params']['group_id']),''))
            await ws.send_json(dict(status='ok', retcode=0, data=data, echo=payload['echo']))
        return ws
    app = web.Application(); app.router.add_get('/', endpoint)
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
    port = site._server.sockets[0].getsockname()[1]
    async def on_open(): opened.set()
    async def on_close(): pass
    async def on_payload(payload): received.append(payload); incoming.set()
    transport = load('transport').QQTransportClient(logger, on_open, on_close, on_payload)
    state = profile.ProfileState(profile.NAPCAT_PROFILE)
    transport.configure(config.QQServerConfig(server='127.0.0.1', port=port, token=token, connection_id='fixture'), state)
    try:
        await transport.start(); await asyncio.wait_for(opened.wait(), 5); await asyncio.wait_for(incoming.wait(), 5)
        version = await transport.call_action('get_version_info', {})
        check('real_onebot_request_echo_response', profile.detect_client_type_from_app_name(version['data']['app_name']) == 'napcat')
        action_service = load('services').QQActionService(logger, transport, state)
        query = load('services').QQQueryService(action_service, logger)
        settings = config.QQPluginSettings()
        inbound = load('codecs.inbound').QQInboundCodec(logger, query, state, lambda: settings)
        native = await inbound.build_message_dict(received[0], '123456', '234567', event['sender'])
        segments = native['raw_message']; by_type = {s['type']: s for s in segments}
        check('qq_identity_not_flattened', native['platform'] == 'qq' and native['account_id'] == '123456'
              and native['message_info']['group_info']['group_id'] == '345678' and native['message_id'] == '-17')
        check('qq_at_parsed_structurally', native['is_at'] and by_type['at']['data']['target_user_id'] == '123456')
        identity=native['message_info']['additional_config']['platform_bot_identity']
        check('self_group_card_automatically_resolved',identity==dict(user_id='123456',group_id='345678',nickname='fixture-bot',cardname='まひるん'))
        plain={**event,'message':[dict(type='text',data=dict(text='ordinary message'))]}
        before=sum(a['action']=='get_group_member_info' for a in actions)
        cached=await inbound.build_message_dict(plain,'123456','234567',event['sender'])
        check('self_identity_cached_without_extra_request',sum(a['action']=='get_group_member_info' for a in actions)==before
              and cached['message_info']['additional_config']['platform_bot_identity']==identity)
        inbound.clear_cache()
        fresh=await inbound.build_message_dict(plain,'123456','234567',event['sender'])
        check('identity_known_before_any_self_at',fresh['message_info']['additional_config']['platform_bot_identity']==identity and not fresh['is_at'])
        another=await inbound.build_message_dict({**plain,'group_id':999999},'123456','234567',event['sender'])
        check('self_group_card_never_leaks_between_groups',another['message_info']['additional_config']['platform_bot_identity']['cardname']=='different-group-card')
        own_cards['345678']='renamed-group-card'
        updated=await inbound.build_message_dict(event,'123456','234567',event['sender'])
        check('real_self_at_refreshes_changed_card',updated['message_info']['additional_config']['platform_bot_identity']['cardname']=='renamed-group-card')
        with patch.object(inbound,'_resolve_at_target_info',AsyncMock(side_effect=RuntimeError('profile lookup unavailable'))):
            failure=await inbound.build_message_dict({**plain,'group_id':888888,'message':[dict(type='at',data=dict(qq='123456'))]},'123456','234567',event['sender'])
        check('self_at_survives_profile_lookup_failure',failure['is_at'] and failure['raw_message'][0]['data']['target_user_id']=='123456')
        expired_key=('345678','123456');inbound._self_identity_cache[expired_key]=(0,identity)
        expired=await inbound.build_message_dict(plain,'123456','234567',event['sender'])
        check('identity_ttl_reloads_current_card',expired['message_info']['additional_config']['platform_bot_identity']['cardname']=='renamed-group-card')
        for index in range(520):
            await inbound._self_identity(str(index),'123456',[dict(type='at',data=dict(target_user_id='123456',target_user_nickname='bot'))])
        check('self_identity_cache_bounded',len(inbound._self_identity_cache)<=512)
        inbound.clear_cache()
        check('disconnect_clears_all_identity_aliases',not inbound._self_identity_cache)
        check('qq_unicode_newline_preserved', by_type['text']['data'] == '中文\nemoji 😀')
        quote = by_type['reply']['data']
        check('qq_quote_negative_id_and_content', quote['target_message_id'] == '-9'
              and quote['target_message_content'] == '引用原文' and quote['target_message_sender_id'] == '456789')
        forward = by_type['forward']['data'][0]
        check('qq_forward_author_and_content', forward['user_id'] == '456789' and '聊天记录正文' in str(forward))
        codec = load('codecs.outbound').QQOutboundCodec(state)
        outbound = dict(platform='qq', account_id='123456', message_info=dict(group_info=dict(group_id='345678')),
            raw_message=[dict(type='reply', data=dict(target_message_id='-9')), dict(type='at', data=dict(target_user_id='234567')),
                         dict(type='text', data='测试回复\n😀')])
        encoded = await codec.prepare_outbound_actions(outbound, {})
        name, params = encoded[0]
        check('qq_reply_and_at_survive_egress', name == 'send_group_msg' and str(params['group_id']) == '345678'
              and params['message'][0] == dict(type='reply', data=dict(id='-9'))
              and params['message'][1] == dict(type='at', data=dict(qq='234567')))
        receipt = await transport.call_action(name, params)
        check('qq_send_receipt_negative_id', receipt['data']['message_id'] == -100)
        private = dict(message_info=dict(additional_config=dict(platform_io_target_user_id='234567')),
                       raw_message=[dict(type='text', data='private fixture')])
        check('qq_private_separate_action', codec.build_outbound_actions(private, {})[0][0] == 'send_private_msg')
    finally:
        await transport.stop()
        for ws in connections: await ws.close()
        await runner.cleanup()
    check('transport_clean_shutdown', not transport._pending_actions and transport._connection_task is None)
    from src.platform_io.manager import PlatformIOManager
    from src.platform_io.drivers.base import PlatformIODriver
    from src.platform_io.types import DriverDescriptor, DriverKind, RouteKey, RouteBinding
    from llm.core.wechat_driver import WechatShellDriver
    class QQFixtureDriver(PlatformIODriver):
        async def send_message(self, *args, **kwargs): raise AssertionError('route-only test must not send')
    broker = PlatformIOManager(); wechat = WechatShellDriver('123456')
    qq = QQFixtureDriver(DriverDescriptor('fixture-qq', DriverKind.PLUGIN, 'qq', account_id='123456', scope='fixture'))
    await broker.add_driver(wechat); await broker.add_driver(qq)
    for driver in [wechat, qq]: broker.bind_send_route(RouteBinding(driver.descriptor.route_key, driver.driver_id, driver.descriptor.kind))
    check('dual_egress_qq_never_routes_to_wechat', broker.resolve_drivers(RouteKey('qq', '123456', 'fixture')) == [qq])
    check('dual_egress_wechat_never_routes_to_qq', broker.resolve_drivers(RouteKey('wechat', '123456')) == [wechat])
    check('unknown_qq_account_no_wechat_fallback', not broker.resolve_drivers(RouteKey('qq', '999999', 'fixture')))
    from src.common.utils.utils_session import SessionUtils
    check('same_group_number_different_platform_isolated', SessionUtils.calculate_session_id('qq', group_id='345678') !=
          SessionUtils.calculate_session_id('wechat', group_id='345678'))
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    from src.platform_io.types import InboundMessageEnvelope
    from src.chat.message_receive.message import SessionMessage
    from src.maisaka.context.planner_messages import build_planner_user_prefix_from_session_message
    native['scope']='fixture'
    message=PluginMessageUtils._build_session_message_from_dict(native)
    captured=[]
    async def receive(envelope): captured.append(envelope.session_message)
    broker.set_inbound_dispatcher(receive)
    broker.bind_receive_route(RouteBinding(qq.descriptor.route_key, qq.driver_id, qq.descriptor.kind))
    env=InboundMessageEnvelope(qq.descriptor.route_key, qq.driver_id, qq.descriptor.kind, session_message=message)
    check('qq_native_ingress_origin_accepted',await broker.accept_inbound(env))
    await asyncio.sleep(.01)
    restored=SessionMessage.from_db_instance(message.to_db_instance())
    origin=restored.message_info.additional_config['message_origin']
    prefix=build_planner_user_prefix_from_session_message(restored)
    check('qq_source_survives_persistence_and_model_prefix',len(captured)==1 and origin['platform']=='qq'
          and origin['account_id']=='123456' and origin['scope']=='fixture'
          and 'platform="qq"' in prefix and 'scope="fixture"' in prefix)
    message.platform='wechat'
    check('qq_spoofed_route_rejected',not await broker.accept_inbound(env))
    try:
        await broker.send_message(message,qq.descriptor.route_key)
        mismatch=False
    except ValueError: mismatch=True
    check('outbound_wrong_platform_rejected_before_driver',mismatch)
    from src.plugin_runtime.integration import PluginRuntimeManager
    message.platform='qq'
    native['message_info']['additional_config']['message_origin']={'platform':'forged'}
    env.session_message=None; env.payload=native
    with patch('src.chat.message_receive.bot.chat_bot.receive_message',new_callable=AsyncMock) as receiver:
        await PluginRuntimeManager._dispatch_platform_inbound(object.__new__(PluginRuntimeManager),env)
    check('qq_payload_only_ingress_gets_trusted_source',receiver.call_args.args[0].message_info.additional_config[
          'message_origin']['platform']=='qq')
    from scripts.configure_napcat import prepare, PLUGIN_ID
    setup = instance / 'setup'; (setup / 'config').mkdir(parents=True)
    shutil.copy2(ROOT / 'config/bot_config.toml', setup / 'config/bot_config.toml')
    source_config = (ROOT / 'plugins/MaiBot-SnowLuma-Adapter/config.toml').read_bytes()
    prepare(setup, source=ROOT / 'plugins/MaiBot-SnowLuma-Adapter', account='123456', groups=['345678'], owner='456789', enable=True)
    check('setup_does_not_mutate_production_source', source_config == (ROOT / 'plugins/MaiBot-SnowLuma-Adapter/config.toml').read_bytes())
    from src.platform_io.adapter_policy import AdapterIdentity, AdapterPolicyManager
    policy = AdapterPolicyManager(setup / 'config/adapter_policy.toml'); identity = AdapterIdentity(plugin_id=PLUGIN_ID, platform='qq')
    check('qq_whitelist_target_only', policy.evaluate(identity, chat_type='group', target_id='345678').allowed and
          not policy.evaluate(identity, chat_type='group', target_id='999999').allowed)
    check('qq_private_owner_only', policy.evaluate(identity, chat_type='private', target_id='456789').allowed and
          not policy.evaluate(identity, chat_type='private', target_id='234567').allowed)
    check('wechat_policy_unaffected', policy.evaluate(AdapterIdentity(platform='wechat'), chat_type='group', target_id='old@chatroom').allowed)
    import tomllib
    setup_config = tomllib.loads((setup / 'plugins/MaiBot-SnowLuma-Adapter/config.toml').read_text(encoding='utf8'))
    ob = json.loads((setup / 'depends-data/napcat/config/onebot11_123456.json').read_text(encoding='utf8'))
    check('generated_token_and_ws_config_match', setup_config['client']['token'] == ob['network']['websocketServers'][0]['token']
          and ob['network']['websocketServers'][0]['messagePostFormat'] == 'array')
    # Stub only the sandboxed reader: reject irrelevant calendar evidence and retry once.
    life = ModuleType('qq_life_web_test'); life.__path__ = [str(ROOT / 'plugins/autonomous_life')]; sys.modules[life.__name__] = life
    research = importlib.import_module(life.__name__ + '.web')
    samples = [dict(text='<rss><channel><item><title>2026 calendar</title><link>https://example.com/calendar</link></item></channel></rss>'),
               dict(text='<rss><channel><item><title>PaperMC Minecraft server</title><link>https://papermc.io</link></item></channel></rss>')]
    with patch.object(research, 'read', side_effect=samples) as reader:
        result = research.explore('2026年10月 Minecraft PaperMC')
    check('research_ignores_calendar_and_bounded_retry', reader.call_count == 2 and len(result['results']) == 1
          and result['search_query'] == 'Minecraft PaperMC')
    from scripts.maibot_api_pool_check import apply_results
    pool = dict(llm=dict(apis=[dict(name=n, model=m, priority=i) for i, (n, m) in enumerate(
        [('ds','deepseek-test'), ('other','other-test'), ('gem','gemini-test'), ('bad','gemini-failed')])]))
    apply_results(pool, [dict(name=n, passed=n!='bad', state='available' if n!='bad' else 'upstream_unavailable')
                         for n in ['ds','other','gem','bad']])
    check('api_availability_then_gemini_before_deepseek', [a['name'] for a in pool['llm']['apis']] == ['gem','ds','other','bad'])
    from scripts.configure_nvidia_deepseek import prepare_config, NAME
    prepare_config(pool, key='synthetic-key-not-real', verified=True)
    priorities = {a['name']: a['priority'] for a in pool['llm']['apis']}
    check('legacy_nvidia_setup_cannot_overtake_enabled_gemini', priorities['gem'] < priorities[NAME] < priorities['other'])


def main():
    with tempfile.TemporaryDirectory(prefix='qq-adapter-', dir=ROOT / 'temp') as temp:
        instance = Path(temp); (instance / 'config').mkdir()
        for name in ['bot_config.toml', 'model_config.toml']: shutil.copy2(ROOT / 'config' / name, instance / 'config' / name)
        os.environ['MAIBOT_INSTANCE_DIR'] = str(instance)
        try: asyncio.run(run(instance))
        except Exception:
            import traceback
            traceback.print_exc(); check('unhandled_exception', False)
        finally:
            database=sys.modules.get('src.common.database.database')
            if database is not None:
                database.engine.dispose()
    report = ROOT / 'docs/maibot-wechat-acceptance/qq-napcat-20261029.json'
    report.write_text(json.dumps(dict(checks=CHECKS, passed=all(x['passed'] for x in CHECKS),
        real_qq_login=False, physical_messages=False, restarted=False), ensure_ascii=False, indent=2), encoding='utf8')
    return 0 if all(x['passed'] for x in CHECKS) else 1


if __name__ == '__main__': raise SystemExit(main())
