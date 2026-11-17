"""A corrupt chat database must not turn an authorized shell command into a plugin error."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
INSTANCE = tempfile.TemporaryDirectory(prefix='plugin-db-', dir=ROOT / 'temp', ignore_cleanup_errors=True)
os.environ['MAIBOT_INSTANCE_DIR'] = INSTANCE.name
os.environ['MAIBOT_INTERCEPT_SEND'] = '1'
(Path(INSTANCE.name) / 'config').mkdir(exist_ok=True)
for name in ('bot_config.toml', 'model_config.toml'):
    source = ROOT / 'config' / name
    if source.exists():
        (Path(INSTANCE.name) / 'config' / name).write_bytes(source.read_bytes())

CORRUPT = sqlite3.DatabaseError('database disk image is malformed')


def failing_service():
    # ``record_plugin_delivery`` stays unset so plugin results keep their
    # original shell shape instead of being rewritten into a delivery dict.
    return Mock(native_ingress=True, record_plugin_delivery=None,
                ingest_context=Mock(side_effect=CORRUPT))


def build_dispatcher(service, modules=None):
    from core.dispatcher import Dispatcher
    dispatcher = Dispatcher()
    dispatcher.debug = False
    dispatcher.llm_service = service
    dispatcher._context_feature_enabled = Mock(return_value=True)
    dispatcher._module_enabled_for_group = Mock(return_value=True)
    dispatcher._dispatch_ai_plugin = Mock(return_value=None)
    dispatcher.modules = dict(modules or {})
    return dispatcher


def wechat_context(content, command=''):
    return {'platform': 'wechat', 'group': 'fixture@chatroom', 'sessionId': 'fixture@chatroom',
            'user': 'member', 'wxid': 'wxid_member', 'messageKey': 'fixture-message',
            'serverId': 'fixture-server', 'content': content, 'command': command}


class PluginExecutionDbResilience(unittest.TestCase):
    def setUp(self):
        self.logger = Mock()
        self.enterContext(patch('src.common.logger.get_logger', return_value=self.logger))
        self.enterContext(patch('core.shell_plugin_config.mark_invoked'))

    def test_command_plugin_still_runs_when_observation_write_fails(self):
        service = failing_service()
        handler = Mock(return_value='signed')
        dispatcher = build_dispatcher(service)
        dispatcher.plugins = {'/probe': NS(__name__='plugins.probe', handle=handler)}
        with patch('utils.response_window.response_allowed', return_value=False):
            result = dispatcher.dispatch('/probe', '', wechat_context('/probe', '/probe'))
        self.assertEqual(result, 'signed')
        self.assertEqual(handler.call_count, 1)
        self.assertEqual(service.ingest_context.call_count, 1)
        self.assertIs(service.ingest_context.call_args.kwargs.get('observe_only'), True)
        self.logger.error.assert_called_once()

    def test_consumed_auto_command_is_not_retried_or_replaced_by_error_text(self):
        service = failing_service()
        consumed = {'messages': ['sleeping'], 'mode': 'wechat_text', 'forward_batch_to_llm': False}
        auto = NS(AUTO_COMMANDS=('/sleep',), AUTO_COMMAND_CONSUMES=True,
                  handle_auto=Mock(return_value=consumed))
        dispatcher = build_dispatcher(service, {'auto:sleep': auto})
        with patch('utils.response_window.response_allowed', return_value=False):
            result = dispatcher.dispatch('/sleep', '', wechat_context('/sleep', '/sleep'))
        self.assertEqual(result, consumed)
        self.assertEqual(auto.handle_auto.call_count, 1)
        self.assertEqual(service.ingest_context.call_count, 1)
        self.assertIs(service.ingest_context.call_args.kwargs.get('observe_only'), True)

    def test_observation_failure_never_executes_a_plugin_for_plain_messages(self):
        service = failing_service()
        handler = Mock(return_value='should not run')
        dispatcher = build_dispatcher(service)
        dispatcher.plugins = {'/probe': NS(__name__='plugins.probe', handle=handler)}
        with patch('utils.response_window.response_allowed', return_value=False):
            result = dispatcher.dispatch('', '', wechat_context('just chatting'))
        self.assertEqual(handler.call_count, 0)
        self.assertEqual(result['messages'], [])
        self.assertFalse(result['should_reply'])
        self.assertTrue(result['observation_failed'])

    def test_non_observed_admission_still_surfaces_the_database_error(self):
        # Ordinary chat in a non-strict prefix mode has no observation wrapper:
        # a broken database must fail loudly instead of pretending to answer.
        service = failing_service()
        dispatcher = build_dispatcher(service)
        dispatcher.prefix_mode = 'mixed'
        context = wechat_context('just chatting')
        context['proactive_candidate'] = False
        with patch('utils.response_window.response_allowed', return_value=False), \
             patch.object(dispatcher, '_can_forward_to_llm', return_value=True):
            with self.assertRaises(sqlite3.DatabaseError):
                dispatcher.dispatch('', '', context)

    def test_fallback_result_keeps_native_delivery_ownership(self):
        service = failing_service()
        dispatcher = build_dispatcher(service)
        result = dispatcher._observe_plugin_context(wechat_context('hello'))
        self.assertEqual(result['messages'], [])
        self.assertFalse(result['should_reply'])
        self.assertTrue(result['native_delivery_owned'])
        self.assertTrue(result['observation_failed'])


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(PluginExecutionDbResilience))
    report = dict(passed=result.wasSuccessful(), tests=result.testsRun,
                  failures=len(result.failures), errors=len(result.errors),
                  physical_messages=False, database_replaced=False, main_process_restarted=False)
    destination = ROOT / 'docs/maibot-wechat-acceptance/plugin-execution-db-resilience-20261118.json'
    destination.write_text(json.dumps(report, indent=2), encoding='utf8')
    raise SystemExit(0 if result.wasSuccessful() else 1)

