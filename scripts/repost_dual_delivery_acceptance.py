"""Isolated SDK receipt, per-stream scheduling and dual-adapter broadcast tests.

No production lifecycle hooks, network requests, group sends or production data writes.
"""
import ast
import asyncio
from contextlib import nullcontext
from datetime import datetime
import importlib
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
INSTANCE = tempfile.TemporaryDirectory(prefix='dual-repost-', ignore_cleanup_errors=True)
os.environ['MAIBOT_INSTANCE_DIR'] = INSTANCE.name
os.environ['MAIBOT_INTERCEPT_SEND'] = '1'


def load(folder):
    name = 'dual_repost_test_' + folder.replace('-', '_')
    pkg = ModuleType(name)
    pkg.__path__ = [str(ROOT / 'plugins' / folder)]
    sys.modules[name] = pkg
    return importlib.import_module(name + '.plugin')


repost = load('mai-repost-shit')
life = load('autonomous_life')
tieba = load('tieba_share')
cateye = load('cateye_mai_forward')
sources = importlib.import_module(repost.__package__ + '.core.sources')
delivery = importlib.import_module(repost.__package__ + '.core.deliver')
state = importlib.import_module(repost.__package__ + '.core.state')
WX = 'test@chatroom'
QQ = 'qq:123:napcat-local:group:456'
STREAMS = [dict(session_id='wx', platform='wechat', account_id='wxid_bot', scope='', group_id=WX),
           dict(session_id='qq', platform='qq', account_id='123', scope='napcat-local', group_id='456')]


def configure(plugin, data):
    data.setdefault('plugin', {})['config_version'] = plugin.config_model().plugin.config_version
    plugin.set_plugin_config(data)


def context():
    return NS(logger=logging.getLogger('dual-repost'),
              chat=NS(get_all_streams=AsyncMock(return_value={'streams': STREAMS})),
              config=NS(get=AsyncMock(return_value='native persona')),
              send=NS(forward=AsyncMock(return_value={'sent': True}),
                      hybrid=AsyncMock(return_value={'sent': True}),
                      image=AsyncMock(return_value=True), custom=AsyncMock(return_value=True)))


class Scheduling(unittest.IsolatedAsyncioTestCase):
    async def test_host_discovery_without_importing_host_registry(self):
        ctx = NS(call_capability=AsyncMock(return_value={'tools': [
            {'name': 'tieba_share'}, {'name': 'share_message_pack'}]}))
        status = await sources.discover(ctx)
        self.assertTrue(all(status[source]['available'] for source in ('tieba', 'bilibili', 'qq_forward')))
        ctx.call_capability.assert_awaited_once_with('tool.get_definitions')
        from maibot_sdk.context import PluginContext
        ctx.call_capability.return_value = PluginContext._normalize_capability_result('tool.get_definitions',
            {'success': True, 'tools': [{'name': 'tieba_share'}, {'name': 'share_message_pack'}]})
        self.assertTrue((await sources.discover(ctx))['tieba']['available'])

    async def test_rpc_timeout_and_real_failure_receipt(self):
        ctx = NS(call_capability=AsyncMock(return_value={'success': True, 'result': {'success': False, 'sent': False}}))
        self.assertFalse((await sources.call_api(ctx, 'repost.tieba_one', stream_id='qq'))['sent'])
        self.assertEqual(ctx.call_capability.call_args.kwargs['timeout_ms'], 360000)
        ctx.call_capability.return_value = {'success': False, 'error': '[E_TIMEOUT] RPC expired'}
        self.assertTrue((await sources.call_api(ctx, 'repost.tieba_one'))['unknown'])

    async def test_apis_and_required_capabilities_are_registered(self):
        for module, name in ((repost, 'repost.autonomous_one'), (tieba, 'repost.tieba_one'),
                             (cateye, 'repost.qq_pack_one')):
            components = module.create_plugin().get_components()
            self.assertTrue(any(row['name'] == name and row['type'] == 'API' for row in components))
        for folder, capability in (('mai-repost-shit', 'tool.get_definitions'), ('tieba_share', 'send.hybrid')):
            manifest = json.loads((ROOT / 'plugins' / folder / '_manifest.json').read_text(encoding='utf8'))
            self.assertIn(capability, manifest['capabilities'])

    async def test_quiet_targets_both_queued_without_waiting_for_wechat(self):
        plugin = repost.create_plugin()
        configure(plugin, {'plugin': {'enabled': True}, 'target': {'groups': [WX, QQ],
            'active_hours': [0, 24], 'mode': 'planner', 'source_channels': ['bilibili', 'tieba']},
            'planner': {'coordinator': True}})
        plugin._store = state.StateStore(Path(INSTANCE.name) / 'scheduler.json', logging.getLogger('test'))
        plugin._pipeline = NS(_resolve_stream=AsyncMock(side_effect=lambda target: ('wx' if target == WX else 'qq', '群')))
        gate = asyncio.Event()
        calls = []

        async def capability(name, **args):
            if name == 'tool.get_definitions':
                return {'tools': [{'name': 'tieba_share'}]}
            if name == 'message.get_recent':
                return {'messages': []}
            calls.append(args)
            await gate.wait()
            return {'success': True, 'result': {'sent': True}}

        plugin._set_context(NS(logger=logging.getLogger('test'), call_capability=capability))
        await plugin._planner_tick()
        await asyncio.sleep(0)
        self.assertEqual({row['args']['group_id'] for row in calls}, {WX, QQ})
        self.assertTrue(all(row['args']['source'] == 'tieba' for row in calls))
        self.assertTrue(all(row['timeout_ms'] == 360000 for row in calls))
        await plugin._planner_tick()
        self.assertEqual(len(calls), 2)
        gate.set()
        await asyncio.gather(*plugin._repost_tasks.values())

    async def test_priority_fallback_and_unknown_delivery(self):
        plugin = repost.create_plugin()
        configure(plugin, {'plugin': {'enabled': True}, 'planner': {'coordinator': True},
                                  'target': {'groups': [QQ]}})
        plugin._source_status = {source: {'available': True} for source in ('tieba', 'bilibili', 'qq_forward')}
        plugin._pipeline = NS(_resolve_stream=AsyncMock(return_value=('qq', '群')))
        plugin._set_context(NS(call_capability=AsyncMock(return_value={'success': True})))
        plugin._run_external_source = AsyncMock(return_value={'sent': True})
        plugin._run_and_report = AsyncMock(return_value={'sent': True})
        receipt = await plugin.autonomous_one(group_id=QQ, grant='fake')
        self.assertEqual(receipt['source'], 'tieba')
        plugin._run_and_report.assert_not_awaited()
        plugin._run_external_source.return_value = {'sent': False}
        receipt = await plugin.autonomous_one(group_id=QQ, grant='fake')
        self.assertEqual(receipt['source'], 'bilibili')
        plugin._run_and_report.reset_mock()
        plugin._run_external_source.return_value = {'sent': False, 'unknown': True}
        receipt = await plugin.autonomous_one(group_id=QQ, grant='fake')
        self.assertTrue(receipt['unknown'])
        plugin._run_and_report.assert_not_awaited()

    async def test_coordinator_independent_of_life_backoff_and_global_slot(self):
        plugin = life.create_plugin()
        configure(plugin, {'plugin': {'enabled': True}, 'life': {'groups': [WX, QQ]}})
        with tempfile.TemporaryDirectory() as tmp:
            plugin.store = life.Store(Path(tmp) / 'journal.db')
            plugin.store.set('backoff_until', 99999999999)
            plugin.context = AsyncMock()
            async def history(target):
                return ('wx' if target == WX else 'qq', [])
            plugin.context.side_effect = history
            gate = asyncio.Event()
            entered = []
            async def capability(name, **args):
                entered.append(args['args']['group_id'])
                await gate.wait()
                return {'success': True, 'result': {'success': False, 'sent': False}}
            ctx = context()
            ctx.call_capability = capability
            plugin._set_context(ctx)
            await plugin.slot.acquire()
            try:
                tasks = [asyncio.create_task(plugin.repost_opportunity(group_id=target, force=True,
                         low_activity=True, daily_limit=96, gap_seconds=900, action_limit=96)) for target in (WX, QQ)]
                for _ in range(10):
                    await asyncio.sleep(0)
                    if len(entered) == 2:
                        break
                self.assertEqual(set(entered), {WX, QQ})
                gate.set()
                receipts = await asyncio.gather(*tasks)
                self.assertTrue(all(row['sent'] is False for row in receipts))
                self.assertEqual(plugin.store.count(plugin.now().date().isoformat(), 'action'), 0)
                with plugin.store.db() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM repost_shares WHERE state='failed'").fetchone()[0], 2)
                    self.assertEqual(db.execute('SELECT count(*) FROM shares').fetchone()[0], 0)
            finally:
                gate.set()
                plugin.slot.release()


class Budget(unittest.TestCase):
    def test_per_stream_limits_unknown_restart_and_ordinary_share_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'journal.db'
            journal = life.Store(path)
            self.assertTrue(journal.reserve_share('day', 10000, 'wx', 'life', '', 4, 7200, 7200))
            self.assertTrue(journal.reserve_repost('day', 10000, 'wx', 'wx-1', 2, 900))
            self.assertTrue(journal.reserve_repost('day', 10000, 'qq', 'qq-1', 2, 900))
            self.assertFalse(journal.reserve_repost('day', 10100, 'qq', 'qq-2', 2, 900))
            journal = life.Store(path)
            with journal.db() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM repost_shares WHERE state='unknown'").fetchone()[0], 2)
            self.assertTrue(journal.reserve_repost('day', 11000, 'qq', 'qq-2', 2, 900))
            self.assertFalse(journal.reserve_repost('day', 12000, 'qq', 'qq-3', 2, 900))


class Media(unittest.IsolatedAsyncioTestCase):
    async def test_bilibili_platform_and_account(self):
        ctx = context()
        candidate = NS(header_text=lambda: 'synthetic video')
        path = Path(INSTANCE.name) / 'clip.mp4'
        path.write_bytes(b'synthetic')
        sender = delivery.Deliverer(ctx, ctx.logger)
        cfg = NS(include_header_text=True)
        self.assertTrue((await sender.send('qq', candidate, path, cfg))['sent'])
        nodes = ctx.send.forward.call_args.args[0]
        self.assertEqual(nodes[-1]['user_id'], '123')
        self.assertEqual(nodes[-1]['segments'][0]['file'], path.resolve().as_posix())
        self.assertNotIn('binary_data_base64', nodes[-1]['segments'][0])
        ctx.send.hybrid.assert_not_awaited()
        self.assertTrue((await sender.send('wx', candidate, path, cfg))['sent'])
        self.assertEqual(ctx.send.hybrid.call_args.args[0][-1]['type'], 'file')
        self.assertFalse((await sender.send('missing', candidate, path, cfg))['sent'])

    async def test_tieba_forward_qq_cards_wechat_and_real_failure(self):
        plugin = tieba.create_plugin()
        configure(plugin, {'plugin': {'enabled': True}, 'send': {'forward': True}})
        ctx = context()
        ctx.send.forward.return_value = True
        plugin._set_context(ctx)
        plugin._forward_nodes = AsyncMock(return_value=([{'user_id': '', 'segments': []}], []))
        plugin._send_cards = AsyncMock(return_value=True)
        plugin._send_videos = AsyncMock()
        plugin._gate_of = lambda: tieba._SendGate(retries=0, rate=100000)
        items = [(NS(tid='1'), NS())]
        self.assertEqual(await plugin._deliver('qq', items, {}), [])
        self.assertEqual(ctx.send.forward.call_args.args[0][0]['user_id'], '123')
        plugin._send_cards.assert_not_awaited()
        self.assertEqual(await plugin._deliver('wx', items, {}), [])
        plugin._send_cards.assert_awaited_once()
        plugin._send_cards.return_value = False
        self.assertEqual(await plugin._deliver('wx', items, {}), items)

    async def test_wechat_long_image_uses_native_file_not_unknown_imageurl(self):
        plugin = tieba.create_plugin()
        ctx = context()
        ctx.send.hybrid.return_value = False
        plugin._set_context(ctx)
        plugin._gate_of = lambda: tieba._SendGate(retries=0)
        self.assertFalse(await plugin._send_page_image('wx', 'file:///D:/synthetic/card.png', ''))
        ctx.send.custom.assert_not_awaited()
        plugin._card_images = AsyncMock(return_value=(['fake'], ['file:///D:/synthetic/card.png']))
        plugin._send_page_image = AsyncMock(return_value=False)
        self.assertFalse(await plugin._send_cards('wx', NS(), NS(), {}))

    async def test_tieba_api_waits_for_delivery_and_enforces_destination(self):
        plugin = tieba.create_plugin()
        configure(plugin, {'plugin': {'enabled': True}, 'tool': {'enabled': True}, 'admin': {'allow_groups': [WX, '456']},
                                  'stone': {'preset_forums': ['搬石']}})
        plugin._set_context(context())
        plugin._run_round = AsyncMock(return_value=tieba._RoundSummary('failed', error=True))
        result = await plugin.repost_one(stream_id='qq')
        self.assertFalse(result['sent'])
        self.assertEqual(plugin._run_round.call_args.kwargs['lock_timeout_seconds'], 120)
        plugin._run_round.return_value = tieba._RoundSummary('delivered', landed_posts=1)
        self.assertTrue((await plugin.repost_one(stream_id='wx'))['sent'])
        self.assertFalse((await plugin.repost_one(stream_id='missing'))['sent'])

    async def test_tieba_send_exception_never_retries(self):
        send = AsyncMock(side_effect=TimeoutError('synthetic'))
        with self.assertRaises(tieba._DeliveryUnknown):
            await tieba._SendGate(retries=3).run(send)
        self.assertEqual(send.await_count, 1)

    async def test_tieba_round_waits_for_shared_renderer_instead_of_dropping_qq(self):
        plugin = tieba.create_plugin()
        configure(plugin, {'plugin': {'enabled': True}, 'stone': {'preset_forums': ['搬石']}})
        plugin._lock = asyncio.Lock()
        plugin._client = object()
        plugin._store = object()
        plugin._run_locked = AsyncMock(return_value=tieba._RoundSummary('sent', landed_posts=1))
        await plugin._lock.acquire()
        task = asyncio.create_task(plugin._run_round('test', ['qq'], lock_timeout_seconds=1))
        await asyncio.sleep(0.02)
        self.assertFalse(task.done())
        plugin._lock.release()
        self.assertTrue((await task).sent)

    async def test_saved_packs_skip_private_and_current_group(self):
        plugin = cateye.create_plugin()
        configure(plugin, {'plugin': {'enabled': True}})
        plugin._set_context(context())
        plugin._pack_load_all = AsyncMock(return_value=[
            {'pack_id': 'private', 'source': {'kind': 'private', 'chat_id': '9'}},
            {'pack_id': 'same', 'source': {'kind': 'group', 'chat_id': '456'}},
            {'pack_id': 'saved', 'source': {'kind': 'group', 'chat_id': '789'}}])
        plugin._permission_error = Mock(return_value='')
        plugin._do_share_pack = AsyncMock(return_value={'sent': True})
        self.assertTrue((await plugin.repost_one(stream_id='qq'))['sent'])
        plugin._do_share_pack.assert_awaited_once_with('saved', {'stream_id': 'qq'})
        self.assertFalse((await plugin.repost_one(stream_id='wx'))['sent'])


class MonitorBroadcast(unittest.TestCase):
    def test_live_native_targets_acl_deduplication_and_wechat_failure(self):
        tree = ast.parse((ROOT / 'core/dispatcher.py').read_text(encoding='utf8'))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Dispatcher')
        func = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_broadcast_text')
        runtime = ModuleType('llm.core.maibot_runtime')
        runtime.native_runtime = NS(_loop=NS(is_running=lambda: True))
        community = ModuleType('core.community_streams')
        community.active_groups = AsyncMock()
        bridge = ModuleType('core.shell_platform_bridge')
        bridge.context_for_target = lambda target: dict(target=target)
        activity = ModuleType('core.plugin_activity')
        activity.legacy_plugin_scope = lambda *args: nullcontext()
        future = NS(result=lambda **kwargs: [NS(**STREAMS[1])], cancel=Mock())
        def submit(coro, loop):
            coro.close()
            return future
        sent = Mock(side_effect=lambda **args: (args['target'] == QQ, 'synthetic WX failure'))
        ns = {'send': sent}
        exec(compile(ast.Module(body=[func], type_ignores=[]), '<actual broadcast method>', 'exec'), ns)
        dispatcher = NS(llm_service=None, _context_feature_enabled=Mock(return_value=True),
                        _module_enabled_for_group=Mock(return_value=True))
        with patch.dict(sys.modules, {'llm.core.maibot_runtime': runtime, 'core.community_streams': community,
                        'core.shell_platform_bridge': bridge, 'core.plugin_activity': activity}), \
                patch('asyncio.run_coroutine_threadsafe', side_effect=submit):
            self.assertTrue(ns['_broadcast_text'](dispatcher, [WX, WX], 'synthetic', module_name='auto:player_monitor'))
            self.assertEqual([call.kwargs['target'] for call in sent.call_args_list], [WX, QQ])
            sent.reset_mock()
            dispatcher._module_enabled_for_group.return_value = False
            self.assertFalse(ns['_broadcast_text'](dispatcher, [WX], 'synthetic', module_name='auto:player_monitor'))
            sent.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
