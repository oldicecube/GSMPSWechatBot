"""B23 trigger/redirect/download/send regression; no real chat deliveries.

--live additionally resolves the public example short link, without downloading.
"""
import argparse
from contextvars import copy_context
import json
from pathlib import Path
import queue
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from services import bilibili
from plugins import bv
from auto import bv as auto_bv

BV = 'BV1GJ411x7h7'
SHORT = 'https://b23.tv/5x4wy5f'
LONG = 'https://www.bilibili.com/video/' + BV + '/'


def response(status=302, location=LONG):
    return Mock(status_code=status, headers={'Location': location} if location else {})


class B23Acceptance(unittest.TestCase):
    def setUp(self):
        self.queue = queue.Queue(maxsize=5)
        self.enterContext(patch.object(bv, '_QUEUE', self.queue))
        self.enterContext(patch.object(bv, '_INFLIGHT', set()))
        self.enterContext(patch.object(bv, 'QUEUE_LIMIT', 5))
        self.enterContext(patch.object(bv, '_CONFIG', {}))
        self.enterContext(patch.object(bilibili, '_config', {}))
        self.context = dict(group='fixture', user='fixture-user', wxid='wxid_fixture',
                            sessionId='fixture@chatroom', messageKey='fixture-message')

    def test_share_text_formats(self):
        for text, expected in [
            ('看看' + SHORT + '，很好笑', SHORT),
            ('【标题】 ' + SHORT + ' 复制这段去B站', SHORT),
            ('[' + SHORT + '](https://example.test)', SHORT),
            ('b23.tv/5x4wy5f', SHORT),
            ('HTTPS://B23.TV/5x4wy5f', SHORT),
            ('http://b23.tv/5x4wy5f', SHORT.replace('https:', 'http:')),
            (SHORT + '?p=2&amp;share_source=copy，看看', SHORT + '?p=2&share_source=copy'),
        ]:
            with self.subTest(text=text):
                self.assertEqual(bilibili.extract_short_link(text), expected)

    def test_unrelated_or_spoofed_domains_not_triggered(self):
        for text in ['普通聊天', 'https://b23.tv.evil.test/code', 'https://notb23.tv/code',
                     'https://evil.test/b23.tv/code', 'https://b23.tv@evil.test/code',
                     'https://user@b23.tv/code', 'ftp://b23.tv/code', 'https://b23.tv/',
                     'https://b23.tv/code_more', 'https://b23.tv/code/other']:
            with self.subTest(text=text):
                self.assertEqual(bilibili.extract_short_link(text), '')
                self.assertIsNone(auto_bv.handle_auto(dict(self.context, content=text)))

    def test_auto_trigger_is_local_and_queue_shared_with_command(self):
        with patch.object(bilibili, '_resolve_short_link', side_effect=AssertionError('network in handler')):
            result = auto_bv.handle_auto(dict(self.context, content='分享一下 ' + SHORT))
            self.assertIn('已加入', result)
            self.assertEqual(self.queue.qsize(), 1)
            self.assertIn('已在队列', bv.handle(SHORT, self.context))
        job = self.queue.get_nowait()
        self.assertEqual(job[:3], ('fixture', '分享一下 ' + SHORT, SHORT))

    def test_command_and_raw_fallback(self):
        for context in [dict(self.context, command='/bv', args=SHORT),
                        dict(self.context, raw={'text': SHORT})]:
            with self.subTest(context=context):
                bv._INFLIGHT.clear()
                self.assertIn('已加入', auto_bv.handle_auto(context))

    def test_bv_tokens_still_supported(self):
        with patch.object(bilibili, '_resolve_short_link', side_effect=AssertionError('unnecessary network')):
            self.assertIn('已加入', auto_bv.handle_auto(dict(self.context, content='看看 ' + BV)))
            self.assertEqual(bilibili.extract_identifier(LONG)['bvid'], BV)
        self.assertIsNone(auto_bv.handle_auto(dict(self.context, content='x' + BV + 'extra')))

    def test_queue_limit_and_missing_destination(self):
        with patch.object(bv, 'QUEUE_LIMIT', 1):
            self.assertIn('已加入', bv.handle(SHORT, self.context))
            self.assertIn('队列已满', bv.handle('https://b23.tv/other', self.context))
        self.assertIn('找不到发送目标', bv.handle(SHORT, {}))

    def test_redirect_uses_location_without_fetching_final_html(self):
        reply = response(location=LONG + '?p=2&share_source=copy')
        with patch.object(bilibili.requests, 'get', return_value=reply) as get:
            identifier = bilibili.extract_identifier('分享 ' + SHORT)
        self.assertEqual(identifier, dict(bvid=BV, aid=None, page=2))
        self.assertEqual(get.call_count, 1)
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        self.assertTrue(get.call_args.kwargs['stream'])
        reply.close.assert_called_once()

    def test_relative_hops_and_page_fallback(self):
        with patch.object(bilibili.requests, 'get', side_effect=[response(location='/second'), response()]) as get:
            result = bilibili.extract_identifier(SHORT + '?p=3')
        self.assertEqual(result['page'], 3)
        self.assertEqual(get.call_args_list[1].args[0], 'https://b23.tv/second')

    def test_av_destination(self):
        with patch.object(bilibili.requests, 'get', return_value=response(location='https://www.bilibili.com/video/av80433022/?p=2')):
            self.assertEqual(bilibili.extract_identifier(SHORT), dict(bvid=None, aid=80433022, page=2))

    def test_opaque_code_is_not_mistaken_for_embedded_av_id(self):
        with patch.object(bilibili.requests, 'get', return_value=response()) as get:
            self.assertEqual(bilibili.extract_identifier('https://b23.tv/xav1234')['bvid'], BV)
            get.assert_called_once()

    def test_tracking_query_cannot_supply_video_identity(self):
        for query in ['?tag=av1234', '?tag=BV17x411w7KC']:
            with self.subTest(query=query), patch.object(bilibili.requests, 'get', return_value=response()) as get:
                url = SHORT + query
                self.assertEqual(bilibili.extract_identifier(url)['bvid'], BV)
                get.assert_called_once()
                self.assertIn('已加入', bv.handle(url, self.context))
                self.assertEqual(self.queue.get_nowait()[2], url)
                bv._INFLIGHT.clear()
        with patch.object(bilibili.requests, 'get', return_value=response(location=LONG + '?next=https://b23.tv/other&p=2')) as get:
            self.assertEqual(bilibili.extract_identifier(SHORT)['page'], 2)
            self.assertEqual(get.call_count, 1)

    def test_non_video_destinations_rejected(self):
        for url in ['https://live.bilibili.com/123', 'https://space.bilibili.com/123',
                    'https://www.bilibili.com/read/cv123?next=' + BV]:
            with self.subTest(url=url), patch.object(bilibili.requests, 'get', return_value=response(location=url)):
                with self.assertRaisesRegex(bilibili.BilibiliError, '不支持的短链类型'):
                    bilibili.extract_identifier(SHORT)

    def test_untrusted_redirects_rejected_before_following(self):
        for url in ['http://127.0.0.1/private', 'https://bilibili.com.evil.test/video/' + BV,
                    'https://b23.tv:1234/next', 'file:///etc/passwd',
                    'https://user:pass@www.bilibili.com/video/' + BV]:
            reply = response(location=url)
            with self.subTest(url=url), patch.object(bilibili.requests, 'get', return_value=reply) as get:
                with self.assertRaises(bilibili.BilibiliError):
                    bilibili.extract_identifier(SHORT)
                self.assertEqual(get.call_count, 1)
                reply.close.assert_called_once()

    def test_loop_and_hop_limit(self):
        with patch.object(bilibili.requests, 'get', return_value=response(location=SHORT)) as get:
            with self.assertRaisesRegex(bilibili.BilibiliError, '循环'):
                bilibili.extract_identifier(SHORT)
            self.assertEqual(get.call_count, 1)
        with patch.object(bilibili.requests, 'get', side_effect=[response(location='/hop' + str(n)) for n in range(5)]) as get:
            with self.assertRaisesRegex(bilibili.BilibiliError, '次数过多'):
                bilibili.extract_identifier(SHORT)
            self.assertEqual(get.call_count, 5)

    def test_bad_status_missing_location_and_network_error(self):
        for status, location in [(404, None), (429, None), (200, None), (302, None)]:
            reply = response(status, location)
            with self.subTest(status=status), patch.object(bilibili.requests, 'get', return_value=reply):
                with self.assertRaises(bilibili.BilibiliError):
                    bilibili.extract_identifier(SHORT)
                reply.close.assert_called_once()
        with patch.object(bilibili.requests, 'get', side_effect=requests.Timeout):
            with self.assertRaisesRegex(bilibili.BilibiliError, 'Timeout'):
                bilibili.extract_identifier(SHORT)

    def test_redirect_time_budget(self):
        with patch.object(bilibili.time, 'monotonic', side_effect=[0, 21]), patch.object(bilibili.requests, 'get') as get:
            with self.assertRaisesRegex(bilibili.BilibiliError, '超时'):
                bilibili.extract_identifier(SHORT)
            get.assert_not_called()

    def test_real_resolve_download_send_and_cleanup_with_http_fixture(self):
        stream = dict(urls=['https://cdn.fixture.test/video.mp4'], size=5, quality=32)
        info = dict(bvid=BV, title='合成验收视频', cid=123, duration=1)
        http_stream = Mock(status_code=200)
        http_stream.iter_content.return_value = [b'video']
        with tempfile.TemporaryDirectory(prefix='bv-') as directory, \
             patch.object(bv, 'TEMP_DIR', directory), \
             patch.object(bilibili.requests, 'get', side_effect=[response(), http_stream]) as get, \
             patch.object(bilibili, '_api_get', return_value=info) as api, \
             patch.object(bilibili, 'pick_stream', return_value=stream), \
             patch.object(bv, 'send', return_value=(True, '')) as send:
            bv._run_job('fixture', SHORT, SHORT)
            self.assertEqual(api.call_args.args[1], {'bvid': BV})
            self.assertEqual(get.call_count, 2)
            sent = send.call_args_list[0].kwargs
            self.assertEqual(sent['mode'], 'wechat_file')
            self.assertEqual(sent['target'], 'fixture')
            self.assertEqual(Path(sent['file_path']).parent, Path(directory))
            self.assertIn('解析成功', send.call_args_list[1].kwargs['content'])
            self.assertFalse(list(Path(directory).iterdir()))

    def test_failed_job_reports_and_releases_capacity(self):
        bv._INFLIGHT.add(SHORT)
        job = ('fixture', SHORT, SHORT, copy_context())
        worker_queue = Mock()
        worker_queue.get.side_effect = [job, StopIteration]
        with tempfile.TemporaryDirectory(prefix='bv-') as directory, \
             patch.object(bv, 'TEMP_DIR', directory), \
             patch.object(bv, '_QUEUE', worker_queue), \
             patch.object(bilibili, 'resolve', side_effect=bilibili.BilibiliError('fixture failure')), \
             patch.object(bv, 'send', return_value=(True, '')) as send:
            with self.assertRaises(StopIteration):
                bv._worker_loop()
            self.assertIn('fixture failure', send.call_args.kwargs['content'])
            self.assertFalse(list(Path(directory).iterdir()))
        self.assertNotIn(SHORT, bv._INFLIGHT)
        worker_queue.task_done.assert_called_once()

    def test_router_dispatcher_automatic_trigger_and_group_switch(self):
        from core.router import Router
        from core.dispatcher import Dispatcher
        from core import auto_registry
        with patch.object(auto_registry, 'RAW_MESSAGE_TARGETS', {'bv'}), \
             patch('core.maibot_shell_registry.allowed', return_value=True), \
             patch('core.shell_plugin_config.mark_invoked'):
            router = Router(prefix=['@fixturebot'], target_group=['fixture'], prefix_mode='strict')
            dispatcher = Dispatcher()
            dispatcher.debug = False
            dispatcher.modules = {'auto:bv': auto_bv}
            message = dict(self.context, content='看看' + SHORT)
            event = router.parse(message)
            self.assertEqual(event['auto_target'], ['bv'])
            self.assertIn('已加入', dispatcher.dispatch(event['command'], event['args'], event))
            self.assertEqual(self.queue.qsize(), 1)
            dispatcher.config = {'group_features': {'fixture': {'auto_plugins_enabled': False}}}
            event = router.parse(dict(message, content='https://b23.tv/other'))
            self.assertIsNone(dispatcher.dispatch(event['command'], event['args'], event))
            self.assertEqual(self.queue.qsize(), 1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--live-url', default='https://b23.tv/auxImJw',
                        help='Public Rick Astley example short link; may expire')
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(B23Acceptance))
    report = dict(tests=result.testsRun, passed=result.wasSuccessful(), production_restart=False,
                  physical_messages=False, failures=[str(t) for t, _ in result.failures + result.errors])
    if args.live:
        try:
            report['live'] = dict(url=args.live_url, identifier=bilibili.extract_identifier(args.live_url))
            report['passed'] = report['passed'] and report['live']['identifier']['bvid'] == BV
        except Exception as exc:
            report['live'] = dict(error=type(exc).__name__ + ': ' + str(exc))
            report['passed'] = False
    path = ROOT / 'temp/bv-short-link-acceptance.json'
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)
