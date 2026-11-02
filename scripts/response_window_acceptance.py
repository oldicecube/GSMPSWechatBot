"""Quiet hours mute only LLM/autonomous reposts, never user plugin requests."""
import asyncio
from contextlib import nullcontext
from datetime import datetime
import importlib
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType,SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
INSTANCE=tempfile.TemporaryDirectory(prefix='quiet-hours-',dir=ROOT/'temp',ignore_cleanup_errors=True)
os.environ['MAIBOT_INSTANCE_DIR']=INSTANCE.name;os.environ['MAIBOT_INTERCEPT_SEND']='1'
(Path(INSTANCE.name)/'config').mkdir()
for name in ('bot_config.toml','model_config.toml'):
    shutil.copy2(ROOT/'config'/name,Path(INSTANCE.name)/'config'/name)
from utils.response_window import response_allowed


def load(folder):
    name='quiet_test_'+folder.replace('-','_');pkg=ModuleType(name);pkg.__path__=[str(ROOT/'plugins'/folder)];sys.modules[name]=pkg
    return importlib.import_module(name+'.plugin')


class QuietHours(unittest.TestCase):
    def test_cross_midnight_and_local_timezone(self):
        slots=[{'start':'08:00','end':'03:00'}]
        for hour,minute,allowed in [(2,59,True),(3,0,True),(3,1,False),(7,59,False),(8,0,True),(23,50,True)]:
            self.assertEqual(response_allowed(slots,now=datetime(2026,11,2,hour,minute,tzinfo=ZoneInfo('Asia/Shanghai'))),allowed)
        self.assertFalse(response_allowed(slots,now=datetime(2026,11,1,20,0,tzinfo=ZoneInfo('UTC'))))
        self.assertTrue(response_allowed([],now=datetime(2026,11,2,4)))

    def test_router_keeps_slash_mentions_and_raw_automation_for_both_platforms(self):
        from core.router import Router
        for platform,target in [('wechat','g@chatroom'),('qq','qq:123:napcat:group:456')]:
            router=Router(['@bot'],[target],time_slots=[{'start':'08:00','end':'03:00'}],prefix_mode='mixed')
            router._is_in_time_slot=Mock(return_value=False)
            router._is_blacklisted=Mock(return_value=False)
            with patch('core.auto_registry.get_raw_message_targets',return_value=['patpat','sleep']):
                for content in ('/shit bv','/shit tb','/sleep','/ping','@bot','普通消息'):
                    event=router.parse({'platform':platform,'account_id':'123','scope':'napcat','group':target,
                        'sessionId':target,'group_id':'456' if platform=='qq' else target,'content':content,'wxid':'wxid_a','user':'member'})
                    self.assertIsNotNone(event,(platform,content))
                    if content.startswith('/'):self.assertEqual(event['command'],content.split()[0])

    def test_dispatcher_plugin_and_patpat_still_run_but_llm_is_muted(self):
        from core.dispatcher import Dispatcher
        dispatcher=Dispatcher();dispatcher.debug=False;dispatcher.llm_service=None
        plugin=NS(__name__='plugins.ping',handle=Mock(return_value='pong'))
        auto=NS(handle_auto=Mock(return_value='patpat-response'))
        dispatcher.plugins={'/ping':plugin};dispatcher.modules={'auto:patpat':auto}
        dispatcher._context_feature_enabled=Mock(return_value=True)
        dispatcher._module_enabled_for_group=Mock(return_value=True)
        with patch('utils.response_window.response_allowed',return_value=False), \
             patch('core.plugin_activity.legacy_plugin_scope',return_value=nullcontext({'delivered':False,'trigger_message_id':'m','invocation_id':'i'})), \
             patch('core.plugin_activity.tag_plugin_result',side_effect=lambda value,*args:value), \
             patch('core.shell_plugin_config.mark_invoked'):
            for platform in ('wechat','qq'):
                context={'platform':platform,'group':'fixture','user':'member','content':'/ping','messageKey':'m'}
                self.assertEqual(dispatcher.dispatch('/ping','',context),'pong')
                self.assertIsNone(dispatcher._dispatch_llm(context,force_reply=True))
            self.assertEqual(plugin.handle.call_count,2);self.assertEqual(auto.handle_auto.call_count,2)


class NativeQuietHours(unittest.IsolatedAsyncioTestCase):
    async def test_manual_repost_runs_while_both_scheduler_modes_are_muted(self):
        mod=load('mai-repost-shit');plugin=mod.create_plugin()
        plugin.set_plugin_config({'plugin':{'enabled':True,'config_version':'1.3.0'},'target':{'mode':'planner','groups':['g@chatroom']},
                                  'planner':{'coordinator':True}})
        ctx=NS(chat=NS(get_all_streams=AsyncMock(return_value=[{'platform':'wechat','account_id':'wxid_self','group_id':'g@chatroom','session_id':'sid'}])),
               logger=logging.getLogger('quiet'))
        plugin._set_context(ctx);plugin._pipeline=NS();plugin._store=NS();plugin._run_and_report=AsyncMock(return_value={'sent':True})
        with patch('utils.response_window.response_allowed',return_value=False):
            await plugin._tick()
            plugin.config.target.mode='scheduled';await plugin._tick()
            self.assertFalse((await plugin.autonomous_one(group_id='g@chatroom',grant='fixture'))['sent'])
            plugin._run_and_report.assert_not_awaited()
            self.assertTrue((await plugin.manual_one(stream_id='sid'))['sent'])
            plugin._run_and_report.assert_awaited_once_with(plugin._run_config(),override_stream='sid')

    async def test_pipeline_rechecks_at_send_boundary_but_manual_send_is_allowed(self):
        mod=load('mai-repost-shit');pipeline_mod=importlib.import_module(mod.__package__+'.core.pipeline')
        candidate=NS(bvid='BVfixture',aid=1,cid=2,title='fixture',author='fixture',play=1,like=1,favorite=0,duration=10,
                     channel='story',tags=['a'],at_ratio=0,at_count=0,comment_count=0,favorite_ratio=0,favorite_metric='fav_like')
        pipeline=object.__new__(pipeline_mod.Pipeline);pipeline._download=AsyncMock(return_value=Path('fixture.mp4'))
        pipeline._deliverer=NS(send=AsyncMock(return_value={'sent':False}));pipeline._logger=logging.getLogger('quiet')
        pipeline._candidate_brief=lambda item:{'bvid':item.bvid}
        cfg=NS(retry_rounds=1,retry_gap_seconds=0)
        with patch('utils.response_window.response_allowed',return_value=False):
            result=await pipeline.run_once(cfg,override_stream='sid',candidate_override=candidate,automatic=True)
            self.assertFalse(result['sent']);pipeline._deliverer.send.assert_not_awaited()
            await pipeline.run_once(cfg,override_stream='sid',candidate_override=candidate,automatic=False)
            pipeline._deliverer.send.assert_awaited_once()

    async def test_native_frequency_and_reply_are_muted_without_affecting_other_plugins(self):
        from src.maisaka.runtime import MaisakaHeartFlowChatting
        from src.maisaka.builtin_tool.reply import execute_reply
        from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
        for platform in ('wechat','qq'):
            runtime=NS(chat_stream=NS(platform=platform,is_group_session=True),session_id='sid')
            with patch('utils.response_window.response_allowed',return_value=False):
                self.assertEqual(MaisakaHeartFlowChatting._get_effective_reply_frequency(runtime),0)
                result=await execute_reply(BuiltinToolRuntimeContext(NS(),runtime),{'msg_id':'m','reply_reference':'test'},call_id='test')
                self.assertFalse(result.success);self.assertIn('不响应时段',result.content or result.error_message)


if __name__=='__main__':unittest.main(verbosity=2)
