"""Alert source regression through real dual-platform Router contexts; no sends."""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.router import Router
from plugins import alert


class AlertSourceAcceptance(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(alert, 'CONFIG', {
            'rcon': {'host': 'fixture.invalid', 'port': 25575, 'password': 'fixture'}}))
        self.sender = self.enterContext(patch.object(alert, 'send', return_value=(True, '')))

    def test_real_router_same_named_qq_and_wechat_groups(self):
        for platform, target, uid, label in [
            ('qq', 'qq:123456:fixture:group:345678', 'qq:234567', 'QQ群'),
            ('wechat', 'fixture@chatroom', 'wxid_fixture', '微信群'),
        ]:
            with self.subTest(platform=platform):
                message = dict(platform=platform, source_platform=platform, group=target,
                    group_name='同名群聊', sessionId=target, wxid=uid, user='同名群友',
                    account_id='123456' if platform=='qq' else 'wxid_bot', scope='fixture',
                    content='/alert 服务器消息\n第二行', serverId='synthetic-alert')
                router = Router([], [target], prefix_mode='mixed')
                event = router.parse(message)
                self.assertIsNotNone(event)
                self.assertEqual(event['platform'], platform)
                self.assertEqual(event['command'], '/alert')
                result = alert.handle(event['args'], {**message, **event, 'raw': message})
                self.assertEqual(result, '发送成功')
                sent = self.sender.call_args.kwargs
                self.assertEqual(sent['mode'], 'rcon')
                self.assertEqual(sent['target'], '')
                self.assertEqual(sent['content'], f'[{label}] <同名群友>\n服务器消息\n第二行')

    def test_source_metadata_fallbacks_and_explicit_platform_precedence(self):
        for fields in [dict(platform='qq'), dict(source_platform='qq'),
                       dict(raw={'platform':'qq'}), dict(raw={'source_platform':'qq'}),
                       dict(platform=' QQ '), dict(platform='qq',raw={'platform':'wechat'})]:
            with self.subTest(fields=fields):
                alert.handle('正文', {'user':'群友', **fields})
                self.assertEqual(self.sender.call_args.kwargs['content'], '[QQ群] <群友>\n正文')
        alert.handle('正文', dict(user='群友',platform='wechat',source_platform='qq'))
        self.assertEqual(self.sender.call_args.kwargs['content'], '[微信群] <群友>\n正文')

    def test_legacy_wechat_and_explicit_unknown_are_distinct(self):
        alert.handle('正文', dict(user='群友',raw=None))
        self.assertEqual(self.sender.call_args.kwargs['content'], '[微信群] <群友>\n正文')
        alert.handle('正文', dict(user='群友',platform='other'))
        self.assertEqual(self.sender.call_args.kwargs['content'], '[未知来源] <群友>\n正文')

    def test_content_and_send_error_preserved(self):
        self.sender.return_value = False, 'fixture rejected'
        result = alert.handle('fallback', dict(platform='qq',user='群友',content=['第一行','第二行']))
        self.assertEqual(result, '发送失败: fixture rejected')
        self.assertEqual(self.sender.call_args.kwargs['content'], '[QQ群] <群友>\n第一行\n第二行')


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AlertSourceAcceptance))
    report=dict(passed=result.wasSuccessful(),tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        physical_messages=False,rcon_requests=False,main_process_restarted=False)
    destination=ROOT/'docs/maibot-wechat-acceptance/alert-source-20261031.json'
    destination.write_text(json.dumps(report,indent=2),encoding='utf8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
