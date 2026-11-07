"""Isolated @-only reply checks; no provider calls, group sends or live config writes."""
import asyncio
from datetime import datetime,timedelta
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock,Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
INSTANCE=tempfile.TemporaryDirectory(prefix='at-only-',ignore_cleanup_errors=True)
os.environ['MAIBOT_INSTANCE_DIR']=INSTANCE.name
os.environ['MAIBOT_INTERCEPT_SEND']='1'
(Path(INSTANCE.name)/'config').mkdir()
for name in ('bot_config.toml','model_config.toml'):
    shutil.copy2(ROOT/'config'/name,Path(INSTANCE.name)/'config'/name)

from src.maisaka import at_reply_policy as policy
from src.maisaka.runtime import MaisakaHeartFlowChatting as Runtime
from src.config.config import global_config

def message(at=False,mention=False,notice=False,old=False):
    return NS(is_at=at,is_mentioned=mention,is_notify=notice,
              timestamp=datetime.now()-timedelta(hours=2 if old else 0),
              message_info=NS(additional_config={'force_reply':True}))

def runtime(platform='qq'):
    return NS(chat_stream=NS(platform=platform,is_group_session=True),
              message_cache=[],_last_processed_index=0,_at_reply_authorized=False)

class AtOnly(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flag=patch.object(global_config.chat.reply_timing,'reply_only_when_at',True)
        self.flag.start();self.addCleanup(self.flag.stop)

    def test_only_fresh_actual_at_authorizes_a_batch(self):
        for msg in (message(),message(mention=True),message(at=True,notice=True),message(at=True,old=True)):
            self.assertFalse(policy.addressed([msg]))
        self.assertTrue(policy.addressed([message(),message(at=True)]))

    def test_both_platforms_ignore_focus_frequency_and_old_at(self):
        with patch('utils.response_window.response_allowed',return_value=True):
            for platform in ('qq','wechat'):
                obj=runtime(platform)
                obj.message_cache=[message(at=True),message()];obj._last_processed_index=1
                self.assertEqual(Runtime._get_effective_reply_frequency(obj),0)
                obj.message_cache.append(message(at=True))
                self.assertEqual(Runtime._get_effective_reply_frequency(obj),1)

    def test_mention_and_plugin_force_cannot_arm_but_at_can(self):
        with patch('src.maisaka.runtime.is_mentioned_bot_in_message',return_value=(True,False,1.0)):
            obj=runtime();obj._arm_forced_turn=Mock()
            Runtime._update_message_trigger_state(obj,message(mention=True))
            obj._arm_forced_turn.assert_not_called()
            Runtime._update_message_trigger_state(obj,message(at=True))
            obj._arm_forced_turn.assert_called_once()

    async def test_proactive_plugin_and_subagent_wakeup_rejected(self):
        obj=runtime()
        result=await Runtime.enqueue_proactive_task(obj,plugin_id='fixture',intent='say hello')
        self.assertFalse(result['success'])
        # Has no queue/focus state: an allowed wake-up would fail this test.
        Runtime._queue_proactive_turn(obj,message(notice=True))

    def test_reply_authorization_is_per_turn_and_mode_is_reversible(self):
        obj=runtime();self.assertFalse(policy.reply_allowed(obj))
        obj._at_reply_authorized=True;self.assertTrue(policy.reply_allowed(obj))
        obj._at_reply_authorized=False
        with patch.object(global_config.chat.reply_timing,'reply_only_when_at',False):
            self.assertTrue(policy.reply_allowed(obj))

    async def test_reply_entry_blocks_before_replyer_and_send(self):
        from src.maisaka.builtin_tool.reply import execute_reply
        from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
        ctx=NS(runtime=runtime(),build_failure_result=BuiltinToolRuntimeContext.build_failure_result)
        with patch('utils.response_window.response_allowed',return_value=True):
            result=await execute_reply(ctx,{'reply_reference':'fixture'},call_id='fixture')
        self.assertFalse(result.success)

    def test_shared_focus_cannot_borrow_another_groups_at(self):
        from src.maisaka.focus.manager import FocusModeManager
        with patch.object(global_config.experimental,'focus_mode',True):
            self.assertFalse(FocusModeManager().is_enabled_for_chat(is_group_chat=True))

    async def test_private_proactive_tasks_are_also_rejected(self):
        obj=runtime();obj.chat_stream.is_group_session=False
        result=await Runtime.enqueue_proactive_task(obj,plugin_id='fixture',intent='hello')
        self.assertFalse(result['success'])

    async def test_engine_silences_proactive_and_timeout_without_new_at(self):
        from src.maisaka.reasoning_engine import MaisakaReasoningEngine,TurnStartContext
        for cached,proactive,timeout in (([message()],False,False),([],True,False),([],False,True)):
            obj=runtime();obj._running=True;obj._internal_turn_queue=asyncio.Queue()
            obj._internal_turn_queue.put_nowait('message')
            engine=MaisakaReasoningEngine.__new__(MaisakaReasoningEngine);engine._runtime=obj
            engine._prepare_turn_start_context=AsyncMock(return_value=TurnStartContext(
                cached,message(notice=proactive),timeout,proactive,False))
            async def silent(**kwargs):obj._running=False
            engine._handle_silent_turn=AsyncMock(side_effect=silent)
            await engine._run_loop()
            engine._handle_silent_turn.assert_awaited_once()

    async def test_engine_accepts_new_at_and_clears_authorization_after_turn(self):
        from src.maisaka.reasoning_engine import MaisakaReasoningEngine,TurnStartContext
        obj=runtime();obj._running=True;obj._internal_turn_queue=asyncio.Queue()
        obj._internal_turn_queue.put_nowait('message')
        obj._chat_loop_service=NS();obj._consume_forced_turn_reason=Mock(return_value=None)
        obj._max_internal_rounds=0;obj._STATE_RUNNING='running';obj._agent_state='stopped'
        engine=MaisakaReasoningEngine.__new__(MaisakaReasoningEngine);engine._runtime=obj;engine._reply_options={}
        msg=message(at=True)
        async def prepare(trigger):
            obj._running=False
            return TurnStartContext([msg],msg,False,False,False)
        engine._prepare_turn_start_context=prepare;engine._handle_silent_turn=AsyncMock()
        await engine._run_loop()
        engine._handle_silent_turn.assert_not_awaited()
        self.assertFalse(obj._at_reply_authorized)

    def test_model_field_defaults_and_live_pause_settings(self):
        from src.config.official_configs import ChatReplyTimingConfig
        self.assertFalse(ChatReplyTimingConfig().reply_only_when_at)
        import tomllib
        timing=tomllib.loads((ROOT/'config/bot_config.toml').read_text(encoding='utf8'))['chat']['reply_timing']
        self.assertTrue(timing['reply_only_when_at']);self.assertTrue(timing['inevitable_at_reply'])
        self.assertTrue(timing['private_reply_paused']);self.assertFalse(timing['mentioned_bot_reply'])

if __name__=='__main__':unittest.main(verbosity=2)
