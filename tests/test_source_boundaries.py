"""Source provenance boundaries for memory, learning, prompt and mentions."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.wechat_sender.mention_markup import MentionMarkupError, normalize_plain_mentions, parse_and_resolve_mentions
from llm.identity import annotate_records
from llm.memory.memory_manager import MemoryManager
from llm.learning.learner import StyleLearner
from llm.prompt.prompt_builder import _group_context_line, _target_message_block
import importlib.util
_parser_spec = importlib.util.spec_from_file_location("wechatbot_response_parser", Path(__file__).resolve().parents[1] / "llm/core/response_parser.py")
_parser = importlib.util.module_from_spec(_parser_spec); _parser_spec.loader.exec_module(_parser)
build_error_response = _parser.build_error_response

class SourceBoundaryTests(unittest.TestCase):
    def test_memory_records_carry_provenance_and_visibility(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(MemoryManager,"_ensure_group_dir",return_value=directory):
            manager=MemoryManager.__new__(MemoryManager);manager.group_message_max_records=20
            # Patch file path construction indirectly by using a real temporary group.
            group=Path(directory)/"g";group.mkdir()
            with patch.object(manager,"_ensure_group_dir",return_value=str(group)):
                self.assertTrue(manager.add_group_message('g','plugin','system output',is_bot=True,role='bot',source_kind='plugin_outbound',source_origin='plugin:test',learnable=False,prompt_visible=False))
                rows=manager.get_group_messages('g')
            self.assertEqual(rows[0]['source_kind'],'plugin_outbound');self.assertFalse(rows[0]['learnable']);self.assertFalse(rows[0]['prompt_visible'])

    def test_llm_delivery_is_not_learning_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            learner=StyleLearner({'learning':{'enabled':True,'db_path':str(Path(directory)/'learning.db')}},start_worker=False)
            self.assertIsNone(learner.record_message({'group':'g','wxid':'bot','user':'bot','content':'Bot fixed phrase','is_bot':True,'source_kind':'llm_assistant','learnable':False}))
            self.assertFalse(learner._eligible({'content':'Bot fixed phrase','speaker':'bot','is_bot':False,'source_kind':'llm_assistant','learnable':False}))
            self.assertTrue(learner._eligible({'content':'user phrase','speaker':'wxid-user','is_bot':False,'source_kind':'user_incoming','learnable':True}))

    def test_error_response_is_internal_silent_not_assistant_text(self):
        result=build_error_response('provider quota error')
        self.assertEqual(result['messages'],[]);self.assertFalse(result['_llm_ok']);self.assertTrue(result['_llm_error']);self.assertIn('quota',result['_internal_error'])

    def test_identity_does_not_turn_prefix_text_into_self_author(self):
        config={'bot_wxids':['wxid_self'],'assistant_nickname':'Configured Bot','prefixes':['@Configured Bot'],'identity':{'name':'Configured Bot'}}
        rows=annotate_records([{'wxid':'wxid_user','nickname':'Member','content':'@Configured Bot 我不是机器人','role':'user'}],config)
        self.assertFalse(rows[0]['is_bot_self']);self.assertEqual(rows[0]['bot_identity'],'other')
        rows=annotate_records([{'wxid':'wxid_self','nickname':'Member','content':'prefix echo','role':'user'}],config)
        self.assertTrue(rows[0]['is_bot_self'])

    def test_quote_is_rendered_as_authoritative_separate_context(self):
        item={'message_id':'m2','nickname':'Replying','sender_wxid':'wxid-reply','content':'我不同意','quoted_message_id':'m1','quoted_sender':'Original','quoted_sender_wxid':'wxid-original','quoted_content':'原始上下文里的问题','quoted_role':'user','quoted_is_bot':False}
        line=_group_context_line(item)
        block=_target_message_block({'current_message':item,'batch_messages':[item],'identity':{'name':'Bot'},'prefixes':['@Bot'],'bot_identity':{'name':'Bot','prefixes':['@Bot'],'bot_wxids':['wxid-self']}})
        self.assertIn('引用消息',line);self.assertIn('原始上下文里的问题',line);self.assertIn('source_name=Original',block);self.assertIn('quoted_sender=Original',block);self.assertIn('原始上下文里的问题',block)
        self.assertLess(block.index('原始上下文里的问题'),len(block))

    def test_verified_mention_only_and_ambiguous_name_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch('core.wechat_sender.mention_markup.GROUPS_DIR',Path(directory)):
            group=Path(directory)/'g';group.mkdir()
            (group/'group_messages.json').write_text(json.dumps([{'wxid':'wxid-a','nickname':'Alice','content':'x'},{'wxid':'wxid-b','nickname':'Bob','content':'y'}]),encoding='utf-8')
            self.assertIn('[[WX_AT:wxid-a]]',normalize_plain_mentions('请 @Alice 看看', 'g'))
            with self.assertRaises(MentionMarkupError):parse_and_resolve_mentions('[[WX_AT:wxid-unknown]]','g')
            self.assertEqual(parse_and_resolve_mentions('普通文本 @Alice','g'),None)

if __name__=='__main__':unittest.main()
