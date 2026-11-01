"""Exercise real config retirement/startup, without starting or messaging a bot."""
import copy
import json
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.clean_adopted_shell_config import apply, clean_config, RETIRED
from llm.core import maibot_native_config as mapper


class ConfigRetirement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='adopted-config-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'config').mkdir()
        for name in ('bot_config.toml', 'model_config.toml'):
            shutil.copy2(ROOT / 'config' / name, self.root / 'config' / name)
        self.config = json.loads((ROOT / 'config.json').read_text(encoding='utf8'))
        # A deterministic old deployment fixture regardless of production cleanup.
        native = tomllib.loads((self.root / 'config/bot_config.toml').read_text(encoding='utf8'))
        llm = self.config['llm']
        llm.update(identity={'rules': [native['personality']['personality']]},
                   assistant_nickname=native['bot']['nickname'], memory={'enabled': True},
                   autonomy={'enabled': True}, context_window_tokens=123456,
                   apis=[dict(name='fixture_dormant', model='fixture-model', base_url='https://fixture.invalid/v1',
                              enabled=False, api_key='')])
        llm['auto_reply'].update(density_gate_enabled=True, reply_cooldown_seconds=99)
        self.path = self.root / 'config.json'
        self.path.write_text(json.dumps(self.config), encoding='utf8')

    def sync(self):
        with patch.object(mapper, 'BOT_CONFIG', self.root / 'config/bot_config.toml'), \
             patch.object(mapper, 'MODEL_CONFIG', self.root / 'config/model_config.toml'), \
             patch.object(mapper, 'LEGACY_CONFIG', self.path):
            return mapper.sync_native_config()

    def test_retired_settings_only_and_idempotence(self):
        result, removed = clean_config(self.config)
        self.assertFalse(RETIRED.intersection(result['llm']))
        self.assertTrue(removed)
        self.assertEqual({k: v for k, v in result.items() if k != 'llm'},
                         {k: v for k, v in self.config.items() if k != 'llm'})
        for key in ('enabled', 'intercept_auto_plugins', 'admin_wxids', 'prefix_bypass_wxids', 'direct_queue_size'):
            self.assertEqual(result['llm'][key], self.config['llm'][key])
        for name, profile in result['llm']['group_profiles'].items():
            self.assertEqual(profile['auto_reply']['enabled'], self.config['llm']['group_profiles'][name]['auto_reply']['enabled'])
        self.assertEqual(clean_config(result), (result, []))

    def test_preview_never_writes(self):
        before = self.path.read_bytes()
        report = apply(self.root)
        self.assertGreater(report['removed_count'], 0)
        self.assertFalse(report['applied'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / 'config/gsmps_model_import_pending.json').exists())

    def test_backup_pending_import_and_webui_authority(self):
        bot_before = tomllib.loads((self.root / 'config/bot_config.toml').read_text(encoding='utf8'))
        model_path = self.root / 'config/model_config.toml'
        model_before = tomllib.loads(model_path.read_text(encoding='utf8'))
        old = self.path.read_bytes()
        report = apply(self.root, write=True)
        self.assertEqual((Path(report['backup']) / 'config.json').read_bytes(), old)
        self.assertEqual(report['pending_endpoints'], 1)
        self.assertNotIn('webui', json.loads(self.path.read_text(encoding='utf8')))
        self.assertNotIn('apis', json.loads(self.path.read_text(encoding='utf8'))['llm'])
        self.sync()
        self.assertFalse((self.root / 'config/gsmps_model_import_pending.json').exists())
        native = tomllib.loads(model_path.read_text(encoding='utf8'))
        self.assertEqual(native['model_task_config'], model_before['model_task_config'])
        dormant = next(p for p in native['api_providers'] if p['name'] == 'WechatBot_fixture_dormant')
        self.assertFalse(dormant['enabled'])
        self.assertEqual(tomllib.loads((self.root / 'config/bot_config.toml').read_text(encoding='utf8'))['personality'], bot_before['personality'])
        # Simulate a WebUI deletion: restart must never resurrect the old endpoint.
        import tomlkit
        native['models'] = [m for m in native['models'] if m['api_provider'] != dormant['name']]
        native['api_providers'] = [p for p in native['api_providers'] if p['name'] != dormant['name']]
        model_path.write_text(tomlkit.dumps(native), encoding='utf8')
        self.sync()
        self.assertEqual(tomllib.loads(model_path.read_text(encoding='utf8')), native)
        self.assertFalse(apply(self.root, write=True)['applied'])

    def test_persona_loss_blocks_cleanup(self):
        config = copy.deepcopy(self.config)
        config['llm']['identity']['rules'] = ['MISSING_RULE_SENTINEL']
        self.path.write_text(json.dumps(config), encoding='utf8')
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'rules'):
            apply(self.root, write=True)
        self.assertEqual(self.path.read_bytes(), before)

    def test_native_provider_works_without_legacy_apis(self):
        apply(self.root, write=True)
        from llm import config as loader
        with patch.object(loader, 'CONFIG_PATH', str(self.path)):
            loaded = loader.get_llm_config()
            self.assertEqual(loaded['model_config_source'], 'maibot')
            self.assertEqual(loaded['apis'], [])
            from llm.provider.deepseek_provider import DeepSeekProvider
            from llm.core.native_model_bridge import NativePoolProvider
            self.assertIsInstance(DeepSeekProvider(), NativePoolProvider)

    def test_plugins_do_not_reinitialize_service(self):
        # Run both real shell model paths; no client/core initialization allowed.
        from plugins import jane, emoji
        from types import SimpleNamespace
        with patch('llm.core.maibot_service.MaibotLLMService', side_effect=AssertionError('unexpected service init')), \
             patch('llm.core.native_model_bridge.shell_generate', return_value=SimpleNamespace(content='fixture')) as call:
            self.assertEqual(emoji._call_model('fixture'), 'fixture')
            with patch.object(jane, 'get_llm_config', return_value={'enabled': True}):
                self.assertTrue(jane.handle('fixture', {'group': 'test'})['messages'])
            self.assertEqual(call.call_count, 2)

    def test_health_check_uses_native_pool_and_keeps_task_members(self):
        from scripts.maibot_api_pool_check import native_entries, rank_native_tasks
        doc = {'api_providers': [dict(name='p', base_url='https://fixture.invalid', api_key='', auth_type='none')],
               'models': [dict(name=n, model_identifier=n, model_type='llm', api_provider='p')
                          for n in ['other', 'deepseek', 'gemini']],
               'model_task_config': {'planner': {'model_list': ['other', 'deepseek', 'gemini']},
                                     'embedding': {'model_list': ['vector']}}}
        entries = native_entries(doc)
        self.assertEqual({a['name'] for a in entries}, {'other', 'deepseek', 'gemini'})
        self.assertTrue(all(a['auth_mode'] == 'none' for a in entries))
        rows = [dict(a, passed=True) for a in entries]
        ranked = rank_native_tasks(doc, rows)
        self.assertEqual(ranked['model_task_config']['planner']['model_list'], ['gemini', 'deepseek', 'other'])
        self.assertEqual(ranked['model_task_config']['embedding'], doc['model_task_config']['embedding'])
        rows[-1]['passed'] = False
        ranked = rank_native_tasks(doc, rows)
        self.assertEqual(ranked['model_task_config']['planner']['model_list'], ['deepseek', 'other', 'gemini'])
        self.assertEqual(ranked['api_providers'], doc['api_providers'])

    def test_real_pending_import_in_isolation(self):
        source = ROOT / 'config/gsmps_model_import_pending.json'
        if not source.exists():
            self.skipTest('One-time production import already consumed')
        self.path.write_bytes((ROOT / 'config.json').read_bytes())
        pending = json.loads(source.read_text(encoding='utf8'))
        shutil.copy2(source, self.root / 'config' / source.name)
        model_path = self.root / 'config/model_config.toml'
        before = tomllib.loads(model_path.read_text(encoding='utf8'))
        self.sync()
        after = tomllib.loads(model_path.read_text(encoding='utf8'))
        from scripts.adopt_maibot_model_pool import slug
        names = {p['name'] for p in after['api_providers']}
        self.assertTrue(all('WechatBot_' + slug(a['name']) in names for a in pending['apis']))
        self.assertEqual(before['model_task_config'], after['model_task_config'])
        from src.config.config import ModelConfig
        ModelConfig.model_validate({k:v for k,v in after.items() if k != 'inner'})
        self.assertFalse((self.root / 'config' / source.name).exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
