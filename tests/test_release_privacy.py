"""Local-only publication boundary and deployment-independent default checks."""
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services import mc_api
from plugins import jane
from llm.learning.learner import StyleLearner

ROOT=Path(__file__).resolve().parents[1]

class ReleasePrivacyTests(unittest.TestCase):
    def test_runtime_material_ignored_and_installation_notes_publishable(self):
        for path in ['config.json','data/blacklist.json','data/bot.sqlite3','data/groups/example/group_messages.json','plugins/jane_prompt.txt','music-source/provider.js','assets/otto/tokens.json','webui/server.py','docs/LLM_CACHE_LIVE_VALIDATION_20261010.md']:
            result=subprocess.run(['git','check-ignore','-q',path],cwd=ROOT)
            self.assertEqual(result.returncode,0,path)
        for path in ['sample_config.json','music-source/README.md','assets/otto/README.md','data/fortune_words.json']:
            result=subprocess.run(['git','check-ignore','-q','--no-index',path],cwd=ROOT)
            self.assertEqual(result.returncode,1,path)

    def test_unconfigured_mc_does_not_connect_to_any_server(self):
        with patch.object(mc_api,'_game_host',''),patch.object(mc_api.JavaServer,'lookup') as lookup:
            with self.assertRaisesRegex(ValueError,'not configured'):mc_api._create_server()
            lookup.assert_not_called()

    def test_public_persona_works_without_local_private_text(self):
        with patch('pathlib.Path.is_file',return_value=False):
            prompt=jane._system_prompt()
        self.assertEqual(prompt,jane.SYSTEM_PROMPT)
        self.assertIn('虚构',prompt)
        self.assertNotIn('真实说过',prompt)

    def test_learning_self_identity_is_configured_not_hardcoded(self):
        with tempfile.TemporaryDirectory() as directory:
            learner=StyleLearner({'identity':{'name':'Configured Bot'},'prefixes':['@Other Alias'],'learning':{'enabled':False,'db_path':str(Path(directory)/'profile.db')}},start_worker=False)
            self.assertIn('configured bot',learner.bot_names)
            self.assertIn('other alias',learner.bot_names)
            self.assertNotIn('服务器状态@我',learner.bot_names)
            self.assertNotIn('gsmps bot',learner.bot_names)

if __name__=='__main__':unittest.main()
