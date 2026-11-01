"""Real scoped storage/relay/prompt/role contracts; isolated instance, no external sends."""
import asyncio
from datetime import datetime
import importlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
INSTANCE=Path(tempfile.mkdtemp(prefix='identity-relay-',dir=ROOT/'temp'))
os.environ['MAIBOT_INSTANCE_DIR']=str(INSTANCE)
(INSTANCE/'config').mkdir()
for name in ('bot_config.toml','model_config.toml'):
    shutil.copy2(ROOT/'config'/name,INSTANCE/'config'/name)
from src.common.database.database import initialize_database,get_db_session,engine
from src.common.database.database_model import Messages,ChatSession
initialize_database()
from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
from src.maisaka.context.message_id_alias import to_display_message_id
from core.cross_platform_relay import relay
from utils import sqlite_store as store


class Contracts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        store.DB_PATH=str(INSTANCE/(self._testMethodName+'.sqlite3'))
        self.sid=self._testMethodName
        self.runtime=NS(session_id=self.sid,chat_stream=NS(platform='qq'),
                        expand_message_id_aliases=lambda x:x,find_source_message_by_id=lambda x:None)
        self.ctx=BuiltinToolRuntimeContext(None,self.runtime)
        self.context=dict(platform='qq',account_id='111',scope='local',wxid='qq:222',user_id='222',
                          user='oldicecube老冰块',serverId=self.sid,sessionId='qq-session')

    def save_message(self,mid,session=None,platform='qq',user='222'):
        with get_db_session() as db:
            db.add(Messages(message_id=mid,timestamp=datetime.now(),platform=platform,user_id=user,
                            user_nickname='renamed',session_id=session or self.sid,raw_content=b''))

    async def test_at_display_alias_falls_back_to_same_session_database(self):
        self.save_message('actual-long-message-id')
        at=self.ctx._resolve_at_attachment(to_display_message_id('actual-long-message-id'))
        self.assertEqual(at.target_user_id,'222')

    async def test_at_does_not_resolve_other_group_or_platform(self):
        self.save_message('other-group',session='foreign')
        self.save_message('other-platform',platform='wechat',user='wxid_other')
        for mid in ('other-group','other-platform'):
            with self.assertRaises(ValueError):self.ctx._resolve_at_attachment(mid)

    async def test_at_history_entry_checked_against_actual_conversation(self):
        self.runtime.find_source_message_by_id=lambda mid:NS(session_id='foreign',platform='qq')
        with self.assertRaises(ValueError):self.ctx._resolve_at_attachment('foreign')

    async def test_missing_at_keeps_reply_body(self):
        from src.config.config import global_config
        with patch.object(global_config.chat,'enable_reply_at',True):
            items=await self.ctx.post_process_reply_message_items_async('正文不会丢失',{'attach_at':['missing']},skip_post_process=True)
        self.assertEqual(items[0].sequence.components[0].text,'正文不会丢失')

    async def test_direct_at_ids_are_platform_specific(self):
        self.assertEqual(self.ctx._resolve_at_attachment({'user_id':'222'}).target_user_id,'222')
        with self.assertRaises(ValueError):self.ctx._resolve_at_attachment({'user_id':'wxid_other'})
        self.runtime.chat_stream.platform='wechat'
        self.assertEqual(self.ctx._resolve_at_attachment({'user_id':'wxid_other'}).target_user_id,'wxid_other')
        with self.assertRaises(ValueError):self.ctx._resolve_at_attachment({'user_id':'222'})

    async def relay_case(self,source='qq',dest='wechat',targets=None,send_result=True,text='你好'):
        from src.common.message_provenance import plugin_execution
        self.calls=[]
        async def send(sequence,sid,**kwargs):
            self.calls.append((sequence,sid,dict(plugin_execution.get()),kwargs))
            if isinstance(send_result,Exception):raise send_result
            return send_result
        self.context['platform']=source
        with patch('core.community_streams.active_groups',AsyncMock(return_value=targets if targets is not None else [NS(session_id='wx-session')])), \
             patch('src.services.send_service.custom_reply_set_to_stream_with_message',send), \
             patch('core.shell_plugin_config.settings',return_value={}):
            return await relay(text,self.context,dest)

    async def test_qq_relay_exact_text_trusted_origin_and_plugin_mark(self):
        result=await self.relay_case(text='你好 @all /ban')
        sequence,sid,origin,kwargs=self.calls[0]
        self.assertEqual(sequence.components[0].text,'[QQ | oldicecube老冰块]你好 @all /ban')
        self.assertEqual(len(sequence.components),1)
        self.assertEqual(origin['relay_origin']['platform'],'qq')
        self.assertEqual(origin['relay_origin']['user_id'],'222')
        self.assertEqual(origin['plugin_id'],'shell.wx')
        self.assertEqual(kwargs['maisaka_source_kind'],'plugin_output')
        self.assertIn('成功 1',result)

    async def test_wechat_relay_label_and_opposite_session(self):
        await self.relay_case('wechat','qq',[NS(session_id='qq-session')])
        self.assertEqual(self.calls[0][0].components[0].text,'[微信 | oldicecube老冰块]你好')
        self.assertEqual(self.calls[0][1],'qq-session')

    async def test_relay_dedupe_persists_after_call(self):
        await self.relay_case()
        result=await self.relay_case()
        self.assertFalse(self.calls)
        self.assertIn('跳过 1',result)

    async def test_relay_uncertain_failure_is_not_replayed(self):
        result=await self.relay_case(send_result=RuntimeError('uncertain'))
        self.assertIn('失败 1',result)
        await self.relay_case()
        self.assertFalse(self.calls)

    async def test_relay_no_active_targets_no_id_wrong_platform_and_size(self):
        await self.relay_case(targets=[]);self.assertFalse(self.calls)
        self.context.pop('serverId')
        await self.relay_case();self.assertFalse(self.calls)
        await self.relay_case('qq','qq');self.assertFalse(self.calls)
        await self.relay_case(text='x'*2001);self.assertFalse(self.calls)

    async def test_active_groups_native_policy_and_proactive_gates(self):
        from core.community_streams import active_groups
        from src.chat.message_receive.chat_manager import chat_manager
        sessions={self.sid+suffix:NS(session_id=self.sid+suffix,platform=p,group_id=g,group_name='same name',account_id='111',scope='local')
                  for suffix,p,g in [('a','qq','100'),('b','wechat','200'),('c','qq','300'),('d','qq','400')]}
        with get_db_session() as db:
            for sid,s in sessions.items():db.add(ChatSession(session_id=sid,platform=s.platform,group_id=s.group_id,group_name=s.group_name,account_id=s.account_id))
        manager=NS(is_target_allowed=AsyncMock(side_effect=lambda route,**kw:kw['target_id']!='300'))
        dispatcher=NS(config={'group_features':{'same name':{}}},_group_feature_enabled=Mock(return_value=True))
        from src.common.utils.utils_config import ChatConfigUtils
        with patch.object(chat_manager,'get_existing_session_by_session_id',side_effect=lambda sid:sessions.get(sid)), \
             patch.object(ChatConfigUtils,'get_talk_value',side_effect=lambda sid,**kw:0 if sid.endswith('d') else 1), \
             patch('src.platform_io.get_platform_io_manager',return_value=manager), \
             patch('core.maibot_shell_registry.get_dispatcher',return_value=dispatcher):
            result=await active_groups()
        self.assertEqual({s.group_id for s in result},{'100','200'})
        dispatcher._group_feature_enabled.assert_called_once_with('same name','llm_enabled')

    async def test_linked_memory_merges_hits_and_retains_source(self):
        from src.services.memory_service import MemoryService
        from src.chat.message_receive.chat_manager import chat_manager
        service=MemoryService()
        service._invoke=AsyncMock(side_effect=[
            {'success':True,'hits':[dict(content='shared',hash='same',score=.4,metadata={'chat_id':'wx'})]},
            {'success':True,'hits':[dict(content='shared',hash='same',score=.9,metadata={'chat_id':'qq'}),dict(content='second',hash='other',score=.6)]}])
        with patch('utils.account_identity.linked_person_ids',return_value=['wx-person','qq-person']), \
             patch.object(chat_manager,'get_existing_session_by_session_id',side_effect=lambda sid:NS(platform=sid,group_name='same name',group_id='100')):
            result=await service.search('query',chat_id='current',person_id='wx-person')
        self.assertEqual(len(result.hits),2)
        self.assertEqual(result.hits[0].score,.9)
        self.assertIn('qq | same name | session=qq',result.to_text())
        self.assertIn('来源未标注',result.to_text())
        self.assertEqual({c.args[1]['chat_id'] for c in service._invoke.call_args_list},{'current'})
        self.assertEqual({c.args[1]['person_id'] for c in service._invoke.call_args_list},{'wx-person','qq-person'})

    async def test_linked_memory_partial_failure_clears_stale_error(self):
        from src.services.memory_service import MemoryService
        service=MemoryService();service._invoke=AsyncMock(side_effect=[{'success':False,'error':'old'}, {'success':True,'hits':[]}])
        with patch('utils.account_identity.linked_person_ids',return_value=['a','b']):result=await service.search('x',person_id='a')
        self.assertTrue(result.success);self.assertEqual(result.error,'')

    async def test_linked_person_profile_includes_other_platform_with_source_label(self):
        from src.maisaka.memory import person_profile as profiles
        candidate=profiles.PersonProfileCandidate(person_id='wx-person',person_name='player',user_id='wxid_owner')
        anchor=NS(platform='wechat',processed_plain_text='current conversation')
        query=AsyncMock(side_effect=[{'success':True,'profile_text':'喜欢红石'}, {'success':True,'profile_text':'做过小程序'}])
        with patch.object(profiles.global_config.a_memorix.integration,'enable_person_profile_injection',True), \
             patch.object(profiles,'collect_person_profile_candidates',return_value=[candidate]), \
             patch('utils.account_identity.memory_aliases',return_value=[('wechat','wxid_owner','wx-person'),('qq','222','qq-person')]), \
             patch.object(profiles.memory_service,'profile_admin',query):
            result=await profiles.build_person_profile_injection_messages(anchor_message=anchor)
        self.assertEqual(query.await_count,2)
        self.assertIn('关联账号 qq:222','\n'.join(result))
        self.assertIn('非本群即时对话','\n'.join(result))

    async def test_shell_persona_never_overrides_native_even_legacy_model_mode(self):
        from llm.core import maibot_native_config as mapper
        import tomlkit
        original=tomlkit.parse(mapper.BOT_CONFIG.read_text(encoding='utf8')).unwrap()
        hostile={'model_config_source':'legacy_import','assistant_nickname':'SENTINEL','identity':{'name':'SENTINEL','rules':['SENTINEL']},
                 'behavior_style':'SENTINEL','reply_style':'SENTINEL',
                 'apis':[dict(name='fixture',model='fixture',base_url='https://fixture.invalid',api_key='')]}
        with patch.object(mapper,'_read_legacy',return_value={'llm':hostile}):mapper.sync_native_config()
        mapped=tomlkit.parse(mapper.BOT_CONFIG.read_text(encoding='utf8')).unwrap()
        self.assertEqual(mapped['personality'],original['personality'])
        self.assertEqual(mapped['bot']['nickname'],original['bot']['nickname'])
        self.assertEqual(mapped['chat']['reply_style'],original['chat']['reply_style'])

    async def test_native_auxiliary_prompt_ignores_stale_shell_persona(self):
        from llm.core.maibot_service import MaibotLLMService
        from src.config.config import global_config
        service=MaibotLLMService.__new__(MaibotLLMService)
        service.config={'identity':{'name':'SENTINEL'},'reply_style':'SENTINEL','private_prompt':'SENTINEL'}
        for group in ('','group'):
            prompt=service._build_prompt('user','message',group,None)
            self.assertNotIn('SENTINEL',prompt)
            self.assertIn(global_config.personality.personality,prompt)
            self.assertIn(global_config.bot.nickname,prompt)


class QQRoles(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pkg=ModuleType('qq_roles_fixture');pkg.__path__=[str(ROOT/'plugins/MaiBot-SnowLuma-Adapter')];sys.modules[pkg.__name__]=pkg
        module=importlib.import_module(pkg.__name__+'.plugin')
        self.plugin=module.create_plugin()
        self.plugin._set_context(NS(logger=Mock(),message=NS(get_by_id=AsyncMock(return_value={'message_info':{'user_info':{'user_id':'222','user_nickname':'target'}}}))))
        self.query=NS(get_login_info=AsyncMock(return_value={'user_id':'111'}),
                      get_group_member_info=AsyncMock(side_effect=lambda **kw:{'role':'admin' if kw['user_id']=='111' else 'member'}),set_group_ban=AsyncMock())
        self.plugin._require_query_service=lambda:self.query
        self.plugin._load_settings=lambda:NS(mute=NS(allowed_groups=['100'],admin_users=[],min_duration=60,max_duration=3600))

    async def test_qq_group_admin_can_use_mute_without_bot_admin(self):
        result=await self.plugin.tool_mute(msg_id='-1',duration='60',group_id='100',stream_id='qq')
        self.assertTrue(result['success'])
        self.query.set_group_ban.assert_awaited_once_with(group_id=100,user_id=222,duration=60)

    async def test_ordinary_qq_bot_role_cannot_mute(self):
        self.query.get_group_member_info=AsyncMock(return_value={'role':'member'})
        result=await self.plugin.tool_mute(msg_id='-1',duration='60',group_id='100',stream_id='qq')
        self.assertFalse(result['success']);self.query.set_group_ban.assert_not_awaited()

    async def test_qq_group_admin_target_is_protected(self):
        self.query.get_group_member_info=AsyncMock(return_value={'role':'admin'})
        result=await self.plugin.tool_mute(msg_id='-1',duration='60',group_id='100',stream_id='qq')
        self.assertFalse(result['success']);self.query.set_group_ban.assert_not_awaited()

    async def test_model_chosen_mute_duration_is_clamped(self):
        for duration,expected in [('1',60),('900',900),('999999',3600)]:
            self.query.set_group_ban.reset_mock()
            result=await self.plugin.tool_mute(msg_id='-1',duration=duration,group_id='100',stream_id='qq')
            self.assertTrue(result['success'])
            self.query.set_group_ban.assert_awaited_once_with(group_id=100,user_id=222,duration=expected)

    async def test_mute_rejects_outside_group_and_nonpositive_duration(self):
        for group,duration in [('200','60'),('100','0'),('100','-1')]:
            result=await self.plugin.tool_mute(msg_id='-1',duration=duration,group_id=group,stream_id='qq')
            self.assertFalse(result['success'])
        self.query.set_group_ban.assert_not_awaited()

    async def test_recall_uses_actual_message_id_and_propagates_platform_rejection(self):
        self.query.delete_message=AsyncMock()
        for message_id in ('-1001','-1002'):
            result=await self.plugin.tool_recall_message(message_id=message_id)
            self.assertTrue(result['success'])
            self.query.delete_message.assert_awaited_with(message_id=int(message_id))
        self.query.delete_message.side_effect=PermissionError('fixture QQ recall permission/window rejected')
        result=await self.plugin.tool_recall_message(message_id='-1003')
        self.assertFalse(result['success'])
        self.assertIn('permission/window rejected',result['error'])


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__]))
    engine.dispose()
    report={'passed':result.wasSuccessful(),'tests':result.testsRun,'failures':[(str(t),e) for t,e in result.failures+result.errors],
            'instance':str(INSTANCE),'physical_messages':False,'production_restart':False}
    (ROOT/'docs/maibot-wechat-acceptance/identity-relay-20261030.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    raise SystemExit(not result.wasSuccessful())
