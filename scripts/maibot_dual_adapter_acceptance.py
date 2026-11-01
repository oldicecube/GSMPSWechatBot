"""Isolated real broker/driver/codec contract tests; never log in or physically send."""
from __future__ import annotations

import asyncio
import copy
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
    if not passed:
        raise AssertionError(name)


async def run(instance):
    pkg = ModuleType('dual_adapter_fixture')
    pkg.__path__ = [str(ROOT / 'plugins/MaiBot-SnowLuma-Adapter')]
    sys.modules[pkg.__name__] = pkg
    def load(name): return importlib.import_module(pkg.__name__ + '.' + name)
    profile = load('profile')
    codec = load('codecs.outbound').QQOutboundCodec(profile.ProfileState(profile.NAPCAT_PROFILE))
    plugin = load('plugin').create_plugin()
    calls = []
    async def action(name, params):
        calls.append((name, params))
        return dict(status='ok', retcode=0, data=dict(message_id=-100))
    runtime = NS(runtime_state=NS(account_id='123456', scope='fixture'), outbound_codec=codec,
                 action_service=NS(call_action=action))
    plugin._require_runtime_bundle = lambda: runtime
    plugin._debug_logger = None
    class Supervisor:
        async def invoke_message_gateway(self, **kw):
            return NS(payload={'success': True, 'result': await plugin.handle_snowluma_gateway(**kw['args'])})

    from src.platform_io.manager import PlatformIOManager
    from src.platform_io.drivers.plugin_driver import PluginPlatformDriver
    from src.platform_io.types import RouteKey, RouteBinding, InboundMessageEnvelope, DeliveryStatus
    from src.plugin_runtime.host.message_utils import PluginMessageUtils as U
    from llm.core.wechat_driver import WechatShellDriver
    from src.common.message_provenance import stamp_message
    from src.platform_io.message_capabilities import planner_transport_hint
    from src.chat.message_receive.message import SessionMessage
    from src.maisaka.context.planner_messages import build_planner_user_prefix_from_session_message
    from src.common.utils.utils_session import SessionUtils

    qq = PluginPlatformDriver('fixture-qq', 'qq', Supervisor(), 'snowluma_gateway', supports_send=True,
                              account_id='123456', scope='fixture', plugin_id='fixture.qq')
    wx = WechatShellDriver('wxid_fixture')
    broker = PlatformIOManager()
    for driver in (qq, wx):
        await broker.add_driver(driver)
        # Deliberately broad registration must not erase declared driver identity.
        broker.bind_send_route(RouteBinding(RouteKey(driver.descriptor.platform), driver.driver_id, driver.descriptor.kind))
        broker.bind_receive_route(RouteBinding(RouteKey(driver.descriptor.platform), driver.driver_id, driver.descriptor.kind))
    check('broad_binding_preserves_account_and_connection', broker.resolve_drivers(qq.descriptor.route_key) == [qq]
          and not broker.resolve_drivers(RouteKey('qq', '999999', 'fixture'))
          and not broker.resolve_drivers(RouteKey('qq', '123456', 'other'))
          and not broker.resolve_drivers(RouteKey('qq')))
    check('platform_routes_do_not_cross', broker.resolve_drivers(wx.descriptor.route_key) == [wx]
          and not broker.resolve_drivers(RouteKey('wechat', '123456', 'fixture')))

    def payload(platform='qq', raw=None, group=True):
        return dict(platform=platform, account_id='123456' if platform == 'qq' else 'wxid_fixture',
                    scope='fixture' if platform == 'qq' else None, message_id='fixture-message',
                    processed_plain_text='fixture', message_info=dict(
                        user_info=dict(user_id='123456', user_nickname='fixture-bot', platform=platform),
                        group_info=dict(group_id='345678', group_name='fixture-group', platform=platform) if group else None,
                        additional_config=dict(platform_io_target_user_id='234567')),
                    raw_message=raw if raw is not None else [dict(type='text', data='中文\n😀')])

    async def gateway(raw, **changes):
        message = payload(raw=raw)
        message.update(changes)
        calls.clear()
        return await plugin.handle_snowluma_gateway(message=message, route={})

    # Native SessionMessage -> plugin RPC serializer -> actual gateway/codec -> OneBot wire.
    message = U._build_session_message_from_dict(payload(raw=[
        dict(type='reply', data=dict(target_message_id='-9', target_message_content='不得泄漏的引用')),
        dict(type='at', data=dict(target_user_id='234567')), dict(type='text', data='中文\n😀')]))
    receipt = await qq.send_message(message, qq.descriptor.route_key)
    check('native_driver_to_qq_wire_quote_at_unicode', receipt.status == DeliveryStatus.SENT
          and calls == [('send_group_msg', dict(group_id='345678', message=[
              dict(type='reply', data=dict(id='-9')), dict(type='at', data=dict(qq='234567')),
              dict(type='text', data=dict(text='中文\n😀'))]))])
    calls.clear()
    receipt = await qq.send_message(message, RouteKey('qq', '123456', 'wrong'))
    check('direct_driver_wrong_connection_rejected_before_rpc', receipt.status == DeliveryStatus.FAILED and not calls)
    for field, value in [('platform', 'wechat'), ('account_id', '999999'), ('scope', 'wrong')]:
        result = await gateway([dict(type='text', data='x')], **{field: value})
        check('gateway_rejects_wrong_' + field, result['success'] is False and not calls)

    raw = [dict(type='reply', data=dict(target_message_id='internal-id', target_message_content='@234567 secret'))]
    result = await gateway(raw + [dict(type='text', data='body')])
    check('invalid_quote_dropped_without_body_or_at_leak', result['success'] and calls[0][1]['message'] ==
          [dict(type='text', data=dict(text='body'))])
    calls.clear()
    receipt = await qq.send_message(U._build_session_message_from_dict(payload(raw=raw)), qq.descriptor.route_key)
    check('pure_invalid_quote_is_dropped_not_sent', receipt.status == DeliveryStatus.DROPPED and not calls)
    result = await gateway([dict(type='at', data=dict(target_user_id='notify@all'))])
    check('qq_all_mention_normalized', result['success'] and calls[0][1]['message'][0]['data']['qq'] == 'all')
    for label, raw in [('wechat_id', [dict(type='at', data=dict(target_user_id='wxid_other'))]),
                       ('missing_image', [dict(type='image', data={})]),
                       ('missing_emoji', [dict(type='emoji', data={})]),
                       ('missing_voice', [dict(type='voice', data={})]),
                       ('unknown_extension', [dict(type='dict', data=dict(type='unsupported', data={}))]),
                       ('malformed_segment', ['invalid']),
                       ('empty_forward', [dict(type='forward', data=[dict(user_id='234567', content=[])])])]:
        result = await gateway([dict(type='text', data='must not send partial text')] + raw)
        check('preflight_rejects_' + label, result['success'] is False and not calls)

    private = payload(group=False, raw=[dict(type='text', data='private')])
    result = await plugin.handle_snowluma_gateway(message=private, route={})
    check('qq_private_uses_user_endpoint', result['success'] and calls[-1][0] == 'send_private_msg'
          and calls[-1][1]['user_id'] == '234567' and 'group_id' not in calls[-1][1])
    private['raw_message'] = [dict(type='at', data=dict(target_user_id='234567'))]
    calls.clear()
    result = await plugin.handle_snowluma_gateway(message=private, route={})
    check('private_mentions_rejected', not result['success'] and not calls)
    result = await plugin.handle_snowluma_gateway(message=payload(), route=dict(group_id='999999'))
    check('conflicting_group_target_rejected', not result['success'] and not calls)
    wrong_target = payload()
    wrong_target['message_info']['group_info']['group_id'] = '123@chatroom'
    result = await plugin.handle_snowluma_gateway(message=wrong_target, route={})
    check('wechat_target_cannot_enter_qq_wire', not result['success'] and not calls)

    # Payloads generated by native File/Image/Emoji/Voice serializers, not invented APIs.
    for media_type, wire_type in [('image', 'image'), ('emoji', 'image'), ('voice', 'record')]:
        result = await gateway([dict(type=media_type, binary_data_base64='YWJj')])
        wire = calls[0][1]['message'][0] if calls else {}
        check('qq_' + media_type + '_binary_to_wire', result['success'] and wire.get('type') == wire_type
              and wire['data']['file'] == 'base64://YWJj'
              and (media_type == 'voice' or wire['data']['sub_type'] == (1 if media_type == 'emoji' else 0)))
    result = await gateway([dict(type='file', data=dict(name='a.bin', base64='YWJj', size=3))])
    check('native_file_base64_contract', result['success'] and calls[0][1]['message'][0] ==
          dict(type='file', data=dict(file='base64://YWJj', name='a.bin')))
    result = await gateway([dict(type='dict', data=dict(type='video', data=dict(url='https://example.org/a.mp4')))])
    check('qq_video_extension_native', result['success'] and calls[0][1]['message'][0]['type'] == 'video')
    nodes = [dict(user_id='234567', user_nickname='记录作者', content=[dict(type='text', data='记录正文')])]
    forward = payload(raw=[dict(type='forward', data=nodes)])
    nap = codec.build_outbound_actions(forward, {})[0]
    snow_codec = load('codecs.outbound').QQOutboundCodec(profile.ProfileState(profile.SNOWLUMA_PROFILE))
    snow = snow_codec.build_outbound_actions(forward, {})[0]
    check('forward_profile_specific_parameter_and_authorship', 'message' in nap[1] and 'messages' in snow[1]
          and nap[1]['message'][0]['data']['uin'] == '234567'
          and nap[1]['message'][0]['data']['name'] == '记录作者')
    runtime.outbound_codec = snow_codec
    n = 0
    async def partial_action(name, params):
        nonlocal n
        n += 1
        return dict(status='ok' if n == 1 else 'failed', retcode=0 if n == 1 else 1400)
    runtime.action_service.call_action = partial_action
    result = await gateway([dict(type='text', data='first'), dict(type='file', data=dict(url='https://example.org/a', name='a')),
                            dict(type='text', data='last')])
    check('partial_delivery_reported_and_later_actions_stopped', not result['success'] and n == 2
          and result['metadata']['partial_delivery'] and result['metadata']['completed_actions'] == 1
          and result['metadata']['automatic_retry_safe'] is False)
    runtime.outbound_codec = codec
    runtime.action_service.call_action = action

    received = []
    async def receive(env): received.append(env)
    broker.set_inbound_dispatcher(receive)
    native = payload()
    env = InboundMessageEnvelope(qq.descriptor.route_key, qq.driver_id, qq.descriptor.kind,
                                 external_message_id='same-id', payload=copy.deepcopy(native))
    env.payload['platform'] = 'wechat'
    check('forged_payload_rejected_before_dedupe', not await broker.accept_inbound(env))
    env.payload = native
    check('valid_payload_after_forgery_not_lost', await broker.accept_inbound(env))
    check('repeat_payload_is_deduped', not await broker.accept_inbound(env))
    await asyncio.sleep(0)
    check('only_valid_payload_dispatched', len(received) == 1)
    other_env = copy.copy(env)
    other_env.route_key = RouteKey('qq', '123456', 'other')
    check('dedupe_identity_scoped_to_connection', broker._build_inbound_dedupe_key(env) != broker._build_inbound_dedupe_key(other_env))
    msg = U._build_session_message_from_dict(native)
    msg.message_info.additional_config['adapter_capabilities'] = {'adapter': 'forged'}
    stamp_message(msg, direction='inbound', transport=qq.driver_id)
    restored = SessionMessage.from_db_instance(msg.to_db_instance())
    prefix = build_planner_user_prefix_from_session_message(restored)
    check('trusted_source_and_capability_survive_storage', 'platform="qq"' in prefix and 'scope="fixture"' in prefix
          and restored.message_info.additional_config['adapter_capabilities']['reply'] == 'native_platform_id')
    check('platform_account_scope_session_isolation', len({SessionUtils.calculate_session_id(p, group_id='345678', account_id=a, scope=s)
          for p, a, s in [('wechat','123456','fixture'), ('qq','123456','fixture'), ('qq','999999','fixture'), ('qq','123456','other')]}) == 4)
    check('planner_hint_qq_capabilities', '来源平台=qq' in planner_transport_hint(restored)
          and '群权限' in planner_transport_hint(restored))

    # Real WeChat driver; final external sender is intercepted, everything before it runs.
    wx_calls = []
    def wx_send(**kw): wx_calls.append(kw); return True, ''
    async def wx_run(raw, group=True):
        wx_calls.clear()
        return await wx.send_message(U._build_session_message_from_dict(payload('wechat', raw, group)), wx.descriptor.route_key)
    with patch('core.sender.send', wx_send), patch('core.maibot_shell_registry.enabled', return_value=True):
        quote = dict(type='reply', data=dict(target_message_id='wx-1', target_message_content='@wxid_other secret'))
        receipt = await wx_run([quote, dict(type='text', data='body')])
        check('wechat_quote_dropped_without_text_or_mention_leak', receipt.status == DeliveryStatus.SENT
              and len(wx_calls) == 1 and wx_calls[0]['content'] == 'body' and not wx_calls[0].get('native_segments'))
        receipt = await wx_run([quote])
        check('wechat_pure_quote_dropped', receipt.status == DeliveryStatus.DROPPED and not wx_calls)
        mute = dict(type='dict', data=dict(type='set_group_ban', data=dict(group_id='345678',user_id='234567',duration=60)))
        receipt = await wx_run([mute, dict(type='text', data='body'), dict(type='at',data=dict(target_user_id='wxid_other'))])
        check('wechat_mute_dropped_valid_text_and_at_retained', receipt.status == DeliveryStatus.SENT
              and len(wx_calls)==1 and wx_calls[0]['content']=='body[[WX_AT:wxid_other]]'
              and wx_calls[0]['native_segments'][1]['wxid']=='wxid_other'
              and receipt.metadata['dropped_admin_controls']==1)
        receipt = await wx_run([mute])
        check('wechat_pure_mute_dropped_without_send', receipt.status == DeliveryStatus.DROPPED and not wx_calls)
        for all_id in ('all', '@all', 'notify@all'):
            receipt = await wx_run([dict(type='text', data='x'), dict(type='at', data=dict(target_user_id=all_id))])
            check('wechat_all_mention_rejected_' + all_id, receipt.status == DeliveryStatus.FAILED and not wx_calls)
        receipt = await wx_run([dict(type='text', data='x'), dict(type='forward', data=nodes)])
        check('wechat_forward_rejected_before_partial_text', receipt.status == DeliveryStatus.FAILED and not wx_calls)
        receipt = await wx_run([dict(type='dict', data=dict(type='video', data=dict(base64='YWJj', name='a.mp4')))])
        check('wechat_video_translated_to_file', receipt.status == DeliveryStatus.SENT and len(wx_calls) == 1
              and wx_calls[0]['mode'] == 'wechat_file' and Path(wx_calls[0]['file_path']).read_bytes() == b'abc')
        receipt = await wx_run([dict(type='at', data=dict(target_user_id='wxid_other'))], group=False)
        check('wechat_private_at_rejected', receipt.status == DeliveryStatus.FAILED and not wx_calls)
        wxmsg = U._build_session_message_from_dict(payload('wechat'))
        check('planner_hint_wechat_quote_and_all_limits', '来源平台=wechat' in planner_transport_hint(wxmsg)
              and '引用控制段会直接丢弃' in planner_transport_hint(wxmsg))
        wxmsg.platform = 'qq'
        receipt = await wx.send_message(wxmsg, wx.descriptor.route_key)
        check('wechat_driver_refuses_qq_origin', receipt.status == DeliveryStatus.FAILED and not wx_calls)

    # Native QQ inbound file/video keep fields, instead of flattening everything to text.
    inbound = load('codecs.inbound').QQInboundCodec(logging.getLogger('dual-fixture'), NS(),
                profile.ProfileState(profile.NAPCAT_PROFILE), lambda: load('config').QQPluginSettings())
    event = dict(message_type='group', group_id=345678, message_id=-17, time=1793278800,
                 message=[dict(type='file', data=dict(file_id='fid', name='a.txt', file_size=3, url='https://example.org/a')),
                          dict(type='video', data=dict(file='vid', url='https://example.org/a.mp4', file_size=3))])
    native = await inbound.build_message_dict(event, '123456', '234567', dict(nickname='fixture'))
    file_seg = next(x for x in native['raw_message'] if x['type'] == 'file')
    video_seg = next(x for x in native['raw_message'] if x['type'] == 'dict')
    check('inbound_file_metadata_preserved', file_seg['data']['name'] == 'a.txt' and file_seg['data']['file_id'] == 'fid'
          and file_seg['data']['url'] == 'https://example.org/a')
    check('inbound_video_preserved_without_fake_analysis', video_seg['data']['type'] == 'video'
          and video_seg['data']['analysis_available'] is False and video_seg['data']['data']['url'].endswith('.mp4'))
    event['message'] = [dict(type='image', data=dict(base64='YWJj', sub_type=1)),
                        dict(type='image', data=dict(base64='ZGVm', sub_type=0))]
    native = await inbound.build_message_dict(event, '123456', '234567', dict(nickname='fixture'))
    check('inbound_emoji_and_picture_binary_distinguished', native['is_emoji'] and native['is_picture']
          and [(s['type'], s['binary_data_base64']) for s in native['raw_message']] == [('emoji','YWJj'),('image','ZGVm')])
    inbound._query_service = NS(get_record_detail=AsyncMock(return_value=dict(base64='YWJj')))
    event['message'] = [dict(type='record', data=dict(file='voice.wav'))]
    native = await inbound.build_message_dict(event, '123456', '234567', dict(nickname='fixture'))
    check('inbound_voice_uses_profile_format_and_binary', native['raw_message'][0]['type'] == 'voice'
          and native['raw_message'][0]['binary_data_base64'] == 'YWJj'
          and inbound._query_service.get_record_detail.call_args.kwargs['out_format'] == 'wav')
    event.update(message_type='private', sub_type='group', message=[dict(type='text', data=dict(text='temporary private'))])
    native = await inbound.build_message_dict(event, '123456', '234567', dict(nickname='fixture'))
    check('temporary_private_origin_not_mistaken_for_group_destination', not native['message_info'].get('group_info')
          and native['message_info']['additional_config']['platform_io_temp_group_id'] == '345678'
          and native['message_info']['additional_config']['platform_io_target_user_id'] == '234567')
    # Notices use the same identity barrier, including internally scheduled notices.
    router = load('runtime.router').QQEventRouter(gateway_capability=NS(route_message=AsyncMock()),
        logger=logging.getLogger('dual-fixture'), gateway_name='fixture', load_settings=lambda: load('config').QQPluginSettings())
    rt = NS(runtime_state=NS(account_id='123456', scope='fixture'), ban_tracker=NS(record_notice=AsyncMock()))
    router.bind_runtime(rt)
    await router.handle_inbound_message(dict(self_id=999999))
    rt.runtime_state.account_id = None
    await router.handle_inbound_message(dict(self_id=123456))
    rt.runtime_state.account_id = '123456'
    await router.handle_notice_event(dict(self_id=999999, notice_type='notify', sub_type='poke'))
    await router.route_notice_payload(dict(self_id=123456), '123456', 'wrong')
    check('notices_cannot_cross_account_or_connection', not rt.ban_tracker.record_notice.called
          and not router._gateway_capability.route_message.called)


def main():
    with tempfile.TemporaryDirectory(prefix='dual-adapter-', dir=ROOT / 'temp') as temp:
        instance = Path(temp)
        (instance / 'config').mkdir()
        for name in ('bot_config.toml', 'model_config.toml'):
            shutil.copy2(ROOT / 'config' / name, instance / 'config' / name)
        os.environ['MAIBOT_INSTANCE_DIR'] = str(instance)
        os.environ['MAIBOT_INTERCEPT_SEND'] = '1'
        try:
            asyncio.run(run(instance))
        except Exception:
            import traceback
            traceback.print_exc()
            CHECKS.append(dict(name='unhandled_exception', passed=False))
        finally:
            db = sys.modules.get('src.common.database.database')
            if db is not None: db.engine.dispose()
    report = ROOT / 'docs/maibot-wechat-acceptance/dual-adapter-20261030.json'
    report.write_text(json.dumps(dict(checks=CHECKS, passed=bool(CHECKS) and all(x['passed'] for x in CHECKS),
        real_qq_login=False, physical_messages=False, restarted=False), ensure_ascii=False, indent=2), encoding='utf8')
    return 0 if CHECKS and all(x['passed'] for x in CHECKS) else 1


if __name__ == '__main__': raise SystemExit(main())
