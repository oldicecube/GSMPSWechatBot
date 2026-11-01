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

from src.common.data_models.message_component_data_model import MessageSequence, TextComponent, AtComponent, EmojiComponent
from src.common.data_models.reply_generation_data_models import ReplyGenerationResult, LLMCompletionResult
from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext, PostProcessedReplyMessage
from src.maisaka.builtin_tool import reply as module
from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator
from src.maisaka.context.reply_segments import split_reply_messages, message_layout_instruction
from src.maisaka.reply_protocol import planner_text_action
from src.maisaka.reasoning_engine import MaisakaReasoningEngine, CycleRuntimeState
from src.core.tooling import ToolExecutionResult


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

    async def run_reply(self, *, fail_second=False, generation_success=True, native_postprocess=False):
        extension = NS(prepare=AsyncMock(), targets=[], reply_id='test-reply', parameters={})
        items = [PostProcessedReplyMessage(MessageSequence([TextComponent(text)]))
                 for text in ('generated part one', 'generated part two')]
        self.replyer.generate_reply_with_context.return_value = (generation_success, self.result)
        self.sender = AsyncMock(side_effect=[NS(message_id='sent-one'),
                                           None if fail_second else NS(message_id='sent-two')])
        hook = AsyncMock(return_value=(self.result.completion.response_text,
                                        dict(skip_post_process=False, enable_splitter=True,
                                             enable_chinese_typo=False)))
        with patch.object(module, 'ReplyExtensionExecution', return_value=extension), \
             patch.object(module.replyer_manager, 'get_replyer', return_value=self.replyer), \
             patch.object(module, '_invoke_before_post_process_hook', hook), \
             patch.object(module, '_resolve_reply_quote', AsyncMock(return_value=(False, 0))), \
             patch.object(self.ctx, 'post_process_reply_message_items_async',
                          AsyncMock(wraps=self.ctx.post_process_reply_message_items_async)
                          if native_postprocess else AsyncMock(return_value=items)) as postprocess, \
             patch.object(module.send_service, '_send_to_target_with_message', self.sender), \
             patch.object(module.config_module.global_config.debug, 'replyer_retro_prompt', False):
            result = await module.execute_reply(self.ctx, {
                'msg_id': 'target', 'reply_reference': 'grounded reply guidance',
                # An old model may retain this removed argument in history.
                'messages': ['PLANNER BODY MUST NEVER BE SENT'], 'attach_emoji': 0,
            }, call_id='call-test')
        return result, postprocess

    async def test_replyer_blank_lines_reach_two_actual_sequential_sends(self):
        self.result.completion.response_text = '完整回答一句\n\n再接一个自然反应'
        config = module.config_module.global_config
        with patch.object(config.response_splitter, 'enable', True), \
             patch.object(config.response_splitter, 'max_split_num', 3), \
             patch.object(config.response_post_process, 'enable_response_post_process', False):
            result, _ = await self.run_reply(native_postprocess=True)
        self.assertTrue(result.success)
        self.assertEqual([call.kwargs['processed_plain_text'] for call in self.sender.call_args_list],
                         ['完整回答一句', '再接一个自然反应'])
        self.assertEqual(result.structured_content['reply_text'], self.result.completion.response_text)
        self.assertTrue(all(call.kwargs['sync_to_maisaka_history'] for call in self.sender.call_args_list))

    async def test_explicit_boundaries_work_with_rule_postprocessing(self):
        config = module.config_module.global_config
        with patch.object(config.response_splitter, 'enable', True), \
             patch.object(config.response_splitter, 'max_split_num', 3), \
             patch.object(config.response_splitter, 'mode', 'rule'), \
             patch.object(config.response_post_process, 'enable_response_post_process', True):
            items = await self.ctx.post_process_reply_message_items_async(
                'Answer.\n\nAnother complete reaction.', enable_chinese_typo=False)
        self.assertEqual(len(items), 2)
        self.assertEqual([item.sequence.components[0].text for item in items],
                         ['Answer.', 'Another complete reaction.'])

    async def test_mention_and_emoji_are_attached_once_to_correct_messages(self):
        config = module.config_module.global_config
        emoji = EmojiComponent(binary_hash='fixture', binary_data=b'fixture')
        with patch.object(config.response_splitter, 'enable', True), \
             patch.object(config.response_splitter, 'max_split_num', 3), \
             patch.object(config.emoji, 'use_new_send_logic', True), \
             patch.object(config.chat, 'enable_reply_at', True), \
             patch.object(self.ctx, '_resolve_at_attachment', return_value=AtComponent('verified-user')), \
             patch.object(self.ctx, '_resolve_emoji_attachment', AsyncMock(return_value=emoji)):
            items = await self.ctx.post_process_reply_message_items_async(
                'First complete message\n\nSecond complete message',
                {'attach_at': ['target'], 'attach_emoji': 1}, enable_chinese_typo=False)
        self.assertEqual(len(items), 3)
        self.assertIsInstance(items[0].sequence.components[0], AtComponent)
        self.assertTrue(all(not isinstance(c, AtComponent) for c in items[1].sequence.components))
        self.assertEqual(items[2].sequence.components, [emoji])

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


class ReplyBoundaryTests(unittest.TestCase):
    def test_short_single_message_is_unchanged(self):
        self.assertEqual(split_reply_messages('就这样，一句完整回复', 3), ['就这样，一句完整回复'])

    def test_order_overflow_and_crlf(self):
        self.assertEqual(split_reply_messages('a\r\n\r\nb\r\n\r\nc\r\n\r\nd', 3),
                         ['a', 'b', 'c\n\nd'])

    def test_formatted_content_stays_whole(self):
        for text in ('说明\n\n```python\nprint(1)\n\nprint(2)\n```',
                     '清单\n\n- 第一项\n\n- 第二项', '标题\n\n# 章节\n\n正文'):
            with self.subTest(text=text):
                self.assertEqual(split_reply_messages(text, 3), [text])

    def test_layout_respects_disabled_or_single_limit(self):
        self.assertEqual(message_layout_instruction(1), '')
        self.assertEqual(split_reply_messages('a\n\nb', 1), ['a\n\nb'])
        self.assertNotIn('JSON', message_layout_instruction(3))


class PlannerRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = NS(log_prefix='fixture', _chat_history=[], _update_stage_status=Mock(),
                          _end_planner_continuation=Mock(), _reset_consecutive_wait_count=Mock(),
                          _enter_stop_state=Mock())
        self.engine = MaisakaReasoningEngine(self.runtime)
        self.engine._turn_passive_replyer = False
        self.engine._emit_planner_progress = AsyncMock()
        self.engine._should_replace_reasoning = Mock(return_value=False)
        self.engine._build_action_tool_definitions = AsyncMock(return_value=([{'name': 'reply'}], ''))
        self.engine._reply_from_planner_content = AsyncMock()
        self.engine._handle_tool_calls = AsyncMock(return_value=(True, 'stop', [], []))
        self.state = CycleRuntimeState(reply_target_message=NS(message_id='real-target'))
        self.cycle = NS(time_records={})

    @staticmethod
    def response(text, tools=None):
        return NS(content=text, tool_calls=tools or [], raw_messages=[], output_items=())

    async def handle(self, first, corrected):
        self.engine._run_interruptible_planner = AsyncMock(return_value=corrected)
        return await self.engine._handle_planner_response_actions(
            response=first, cycle_detail=self.cycle, state=self.state, planner_extra_lines=[])

    def test_live_decisions_and_negations(self):
        samples = {
            '结论：调用 `reply` 进行回复，附带表情包。不调用其他工具。': 'reply',
            '不需要调用其他工具，直接回复即可。': 'reply',
            '我应该直接回复他，表明我正在运行。': 'reply',
            '我先用 minecraft_rcon_action 查询，然后用 reply 发送。先调用 minecraft_rcon_action 查询。': 'action',
            '先调用 web_search 查询，然后直接回复即可。': 'action',
            '先用 minecraft_rcon_action 查询，然后直接回复。': 'action',
            '可以调用 reply。不过最终决定：暂不发言，等待新消息。': 'none',
            '不要调用 reply。': 'none',
            '不需要回复。': 'none',
            '群友正在聊天，没有我的事。': 'none',
        }
        for text, action in samples.items():
            with self.subTest(text=text):
                self.assertEqual(planner_text_action(text), action)

    async def test_corrected_tool_call_uses_normal_execution_once(self):
        tool = NS(call_id='real-call', func_name='reply')
        await self.handle(self.response('结论：调用 reply 进行回复。'), self.response('', [tool]))
        self.engine._run_interruptible_planner.assert_awaited_once()
        self.engine._handle_tool_calls.assert_awaited_once()
        self.engine._reply_from_planner_content.assert_not_awaited()

    async def test_second_explicit_reply_recovers_through_replyer_once(self):
        response = self.response('不需要调用其他工具，直接回复即可。')
        await self.handle(response, response)
        self.engine._run_interruptible_planner.assert_awaited_once()
        self.engine._reply_from_planner_content.assert_awaited_once()
        self.assertEqual(self.state.reply_target_message.message_id, 'real-target')

    async def test_recovery_uses_real_reply_service_and_frozen_target(self):
        self.engine._reply_from_planner_content = MaisakaReasoningEngine._reply_from_planner_content.__get__(self.engine)
        response = self.response('不需要调用其他工具，直接回复即可。')
        with patch('src.maisaka.reasoning_engine.execute_reply',
                   AsyncMock(return_value=ToolExecutionResult(tool_name='reply', success=True,
                                                             content='fixture receipt'))) as executor:
            await self.handle(response, response)
        executor.assert_awaited_once()
        self.assertIs(executor.call_args.kwargs['target_message'], self.state.reply_target_message)
        self.assertEqual(executor.call_args.args[1]['msg_id'], 'real-target')
        self.assertEqual(self.state.cycle_end.reason, 'planner_reply_end')

    async def test_failed_recovery_is_reported_without_second_send(self):
        self.engine._reply_from_planner_content = MaisakaReasoningEngine._reply_from_planner_content.__get__(self.engine)
        response = self.response('应该直接回复。')
        with patch('src.maisaka.reasoning_engine.execute_reply',
                   AsyncMock(return_value=ToolExecutionResult(tool_name='reply', success=False,
                                                             error_message='fixture failure'))) as executor:
            await self.handle(response, response)
        executor.assert_awaited_once()
        self.assertEqual(self.state.cycle_end.reason, 'planner_reply_failed')

    async def test_correction_may_choose_silence_without_forced_speech(self):
        await self.handle(self.response('应该直接回复。'), self.response('决定暂不发言，等待新消息。'))
        self.engine._reply_from_planner_content.assert_not_awaited()

    async def test_pending_query_is_not_faked_or_replaced_by_reply(self):
        response = self.response('先调用 minecraft_rcon_action 查询服务器，然后回复。')
        await self.handle(response, response)
        self.engine._run_interruptible_planner.assert_awaited_once()
        self.engine._reply_from_planner_content.assert_not_awaited()

    async def test_silent_decision_makes_no_extra_model_request(self):
        await self.handle(self.response('不需要回复，等待新消息。'), self.response(''))
        self.engine._run_interruptible_planner.assert_not_awaited()

    async def test_passive_mode_keeps_existing_single_reply_entry(self):
        self.engine._turn_passive_replyer = True
        await self.handle(self.response('简短回应群友'), self.response(''))
        self.engine._run_interruptible_planner.assert_not_awaited()
        self.engine._reply_from_planner_content.assert_awaited_once()


if __name__ == '__main__':
    unittest.main(verbosity=2)
