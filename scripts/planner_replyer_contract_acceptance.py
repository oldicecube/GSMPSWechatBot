"""Offline native Planner -> Replyer -> ordered platform delivery regression.

Uses an isolated instance and intercepts every send. No production bot restart.
"""
import asyncio
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
INSTANCE = Path(tempfile.mkdtemp(prefix='planner-replyer-', dir=ROOT / 'temp'))
(INSTANCE / 'config').mkdir()
for name in ('bot_config.toml', 'model_config.toml'):
    shutil.copy2(ROOT / 'config' / name, INSTANCE / 'config' / name)
os.environ['MAIBOT_INSTANCE_DIR'] = str(INSTANCE)

from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
from src.common.data_models.reply_generation_data_models import ReplyGenerationResult, LLMCompletionResult
from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext, PostProcessedReplyMessage
from src.maisaka.builtin_tool import reply as module
from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator


class NativeReplyContract(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.target = NS(message_id='target', session_id='stream', is_at=True, is_mentioned=True,
                         message_info=NS(user_info=NS(user_id='12345', user_cardname='member',
                                                     user_nickname='member'), additional_config={}))
        self.runtime = NS(session_id='stream', log_prefix='test',
                          chat_stream=NS(platform='qq', is_group_session=True, group_id='group',
                                         user_id='12345', account_id='bot'),
                          _chat_loop_service=NS(_active_tool_names=('reply', 'web_search', 'peer_group_context')),
                          deferred_tool_specs_by_name={'tieba_share': None}, _chat_history=[],
                          _update_stage_status=Mock(), record_planner_reply=Mock(),
                          find_source_message_by_id=Mock(return_value=self.target))
        self.ctx = BuiltinToolRuntimeContext(NS(), self.runtime)
        self.result = ReplyGenerationResult(success=True,
            completion=LLMCompletionResult(response_text='Replyer-generated final text'))
        self.replyer = NS(generate_reply_with_context=AsyncMock(return_value=(True, self.result)))

    async def run_reply(self, *, fail_second=False, generation_success=True):
        extension = NS(prepare=AsyncMock(), targets=[], reply_id='test-reply', parameters={})
        items = [PostProcessedReplyMessage(MessageSequence([TextComponent(text)]))
                 for text in ('generated part one', 'generated part two')]
        self.replyer.generate_reply_with_context.return_value = (generation_success, self.result)
        self.sender = AsyncMock(side_effect=[NS(message_id='sent-one'),
                                           None if fail_second else NS(message_id='sent-two')])
        hook = AsyncMock(return_value=('Replyer-generated final text',
                                        dict(skip_post_process=False, enable_splitter=True,
                                             enable_chinese_typo=False)))
        with patch.object(module, 'ReplyExtensionExecution', return_value=extension), \
             patch.object(module.replyer_manager, 'get_replyer', return_value=self.replyer), \
             patch.object(module, '_invoke_before_post_process_hook', hook), \
             patch.object(module, '_resolve_reply_quote', AsyncMock(return_value=(False, 0))), \
             patch.object(self.ctx, 'post_process_reply_message_items_async',
                          AsyncMock(return_value=items)) as postprocess, \
             patch.object(module.send_service, '_send_to_target_with_message', self.sender), \
             patch.object(module.config_module.global_config.debug, 'replyer_retro_prompt', False):
            result = await module.execute_reply(self.ctx, {
                'msg_id': 'target', 'reply_reference': 'grounded reply guidance',
                # An old model may retain this removed argument in history.
                'messages': ['PLANNER BODY MUST NEVER BE SENT'], 'attach_emoji': 0,
            }, call_id='call-test')
        return result, postprocess

    def test_native_schemas_do_not_offer_direct_planner_body(self):
        self.assertNotIn('messages', module.get_tool_spec().parameters_schema['properties'])
        self.assertNotIn('messages', module.get_reply_options_spec().parameters_schema['properties'])

    async def test_replyer_is_always_called_and_fragments_have_separate_receipts(self):
        result, postprocess = await self.run_reply()
        self.assertTrue(result.success)
        self.replyer.generate_reply_with_context.assert_awaited_once()
        arguments = self.replyer.generate_reply_with_context.call_args.kwargs['reply_tool_args']
        self.assertNotIn('messages', arguments)
        self.assertIn('web_search', arguments['_planner_context']['available_tools'])
        postprocess.assert_awaited_once()
        self.assertEqual(postprocess.call_args.args[0], 'Replyer-generated final text')
        self.assertEqual(self.sender.await_count, 2)
        self.assertEqual(result.metadata['sent_message_ids'], ['sent-one', 'sent-two'])
        self.assertEqual([c.kwargs['processed_plain_text'] for c in self.sender.call_args_list],
                         ['generated part one', 'generated part two'])

    async def test_replyer_failure_never_falls_back_to_planner_body(self):
        result, _ = await self.run_reply(generation_success=False)
        self.assertFalse(result.success)
        self.sender.assert_not_awaited()

    async def test_partial_delivery_retains_successful_receipt(self):
        result, _ = await self.run_reply(fail_second=True)
        self.assertFalse(result.success)
        self.assertEqual(result.metadata['sent_message_ids'], ['sent-one'])
        self.assertEqual([r['success'] for r in result.metadata['send_results']], [True, False])

    def test_handoff_does_not_replace_guidance_or_expose_tool_schema(self):
        text = BaseMaisakaReplyGenerator._build_reply_reference_message('', 'grounded facts', {
            'platform': 'qq', 'available_tools': ['web_search', 'peer_group_context'],
            'deferred_tools': ['tieba_share']})
        self.assertIn('grounded facts', text)
        self.assertIn('peer_group_context', text)
        self.assertNotIn('parameters_schema', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
