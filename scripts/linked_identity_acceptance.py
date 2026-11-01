"""Real bindings + native person DB + final prompt/profile/memory contracts, no live LLM."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
INSTANCE=Path(tempfile.mkdtemp(prefix='linked-person-',dir=ROOT/'temp'))
os.environ['MAIBOT_INSTANCE_DIR']=str(INSTANCE)
(INSTANCE/'config').mkdir()
for name in ('bot_config.toml','model_config.toml'):shutil.copy2(ROOT/'config'/name,INSTANCE/'config'/name)
from src.common.database.database import initialize_database,get_db_session,engine
from src.common.database.database_model import PersonInfo
from src.person_info.person_info import Person,get_person_id
from src.services.memory_service import MemoryService
from src.maisaka.memory import person_profile as profiles
from src.maisaka.builtin_tool import query_person_profile as query_tool
from src.maisaka.context.planner_messages import build_planner_user_prefix_from_session_message
from utils import account_identity as identity,sqlite_store as store
from sqlmodel import select
initialize_database()


class LinkedIdentities(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        store.DB_PATH=str(INSTANCE/(self._testMethodName+'.sqlite3'))
        self.wx='wxid_'+self._testMethodName
        self.qq=str(100000+list(unittest.defaultTestLoader.getTestCaseNames(type(self))).index(self._testMethodName))
        self.wp=get_person_id('wechat',self.wx);self.qp=get_person_id('qq',self.qq)
        store.player_stats_ensure('SamePlayer')
        identity.bind_game(self.wx,'SamePlayer')
        identity.bind_game('qq:'+self.qq,'SamePlayer')
        self.texts={self.wp:'# 人物画像\n## 稳定了解\n- 在微信群讨论红石\n## 近期互动\n- 研究计时器',
                    self.qp:'# 人物画像\n## 稳定了解\n- 在QQ群写过小程序\n## 近期互动\n- 展示过实验'}

    async def backend(self, name, **kwargs):
        pid=kwargs.get('person_id')
        return dict(success=pid in self.texts,person_id=pid,profile_text=self.texts.get(pid,''))

    async def test_rename_both_platforms_preserves_native_ids_profiles_and_binding(self):
        for platform,uid in [('wechat',self.wx),('qq',self.qq)]:
            old=Person.register_person(platform,uid,'同一个旧昵称',self._testMethodName,'旧名片')
            old.memory_points=['稳定的真实经历'];old.sync_to_database()
            new=Person.register_person(platform,uid,'全新昵称',self._testMethodName,'新群名片')
            self.assertEqual(old.person_id,new.person_id)
            self.assertEqual(Person(platform=platform,user_id=uid).memory_points,['稳定的真实经历'])
            self.assertEqual(set(identity.linked_person_ids(new.person_id)),{self.wp,self.qp})
            with get_db_session() as db:
                self.assertEqual(len(db.exec(select(PersonInfo).where(PersonInfo.platform==platform,PersonInfo.user_id==uid)).all()),1)
        self.assertNotEqual(self.wp,self.qp) # Preserve two original IDs and their provenance.

    async def test_query_profile_both_directions_retains_separate_structured_sections(self):
        service=MemoryService();service._invoke_admin=AsyncMock(side_effect=self.backend)
        for primary,other,platform in [(self.wp,self.qp,'qq'),(self.qp,self.wp,'wechat')]:
            result=await service.profile_admin(action='query',person_id=primary)
            self.assertEqual(result['person_id'],primary)
            self.assertEqual(result['profile_text'],self.texts[primary])
            self.assertEqual(result['linked_profiles'][0]['person_id'],other)
            text=query_tool._extract_profile_text(result)
            self.assertIn('微信群讨论红石',text);self.assertIn('QQ群写过小程序',text)
            self.assertIn('同一人的关联账号 '+platform+':',text)
            self.assertIn('非本群即时对话',text)

    async def test_profile_injection_structured_profiles_keep_both_sources(self):
        service=MemoryService();service._invoke_admin=AsyncMock(side_effect=self.backend)
        candidate=profiles.PersonProfileCandidate(person_id=self.wp,person_name='全新昵称',user_id=self.wx)
        with patch.object(profiles.global_config.a_memorix.integration,'enable_person_profile_injection',True), \
             patch.object(profiles,'collect_person_profile_candidates',return_value=[candidate]), \
             patch.object(profiles,'memory_service',service):
            prompt='\n'.join(await profiles.build_person_profile_injection_messages(anchor_message=NS(platform='wechat',processed_plain_text='现在聊一下实验')))
        for token in ['微信群讨论红石','QQ群写过小程序','同一人','qq:'+self.qq,'非本群即时对话','全新昵称']:
            self.assertIn(token,prompt)
        self.assertEqual(service._invoke_admin.await_count,2)

    async def test_explicit_same_person_hint_in_actual_planner_prefix(self):
        from llm.core.wechat_translation import WechatMessageTranslator
        message=await WechatMessageTranslator({'weflow':{'myWxid':'wxid_test_bot'}}).translate(dict(
            wxid=self.wx,user='改名后的微信用户',content='hello',group='测试群',sessionId='test@chatroom',serverId='identity-probe'))
        prefix=build_planner_user_prefix_from_session_message(message)
        for token in ('这些账号属于同一人','SamePlayer','qq:'+self.qq,'当前平台会话仍独立','改名后的微信用户'):
            self.assertIn(token,prefix)
        message.platform='qq';message.message_info.user_info.user_id=self.qq
        message.message_info.user_info.user_nickname='改名后的QQ用户'
        qq_prefix=build_planner_user_prefix_from_session_message(message)
        for token in ('这些账号属于同一人','SamePlayer',self.wx,'当前平台会话仍独立','改名后的QQ用户'):
            self.assertIn(token,qq_prefix)

    async def test_automatic_profile_failure_one_side_retains_other_side(self):
        candidate=profiles.PersonProfileCandidate(person_id=self.wp,person_name='当前微信昵称',user_id=self.wx)
        for failing, expected in [(self.wp,'QQ群写过小程序'),(self.qp,'微信群讨论红石')]:
            async def backend(**kwargs):
                if kwargs.get('person_id')==failing:raise RuntimeError('one backend unavailable')
                pid=kwargs['person_id']
                return dict(success=True,profile_text=self.texts[pid])
            with patch.object(profiles.global_config.a_memorix.integration,'enable_person_profile_injection',True), \
                 patch.object(profiles,'collect_person_profile_candidates',return_value=[candidate]), \
                 patch.object(profiles.memory_service,'profile_admin',side_effect=backend):
                prompt='\n'.join(await profiles.build_person_profile_injection_messages(
                    anchor_message=NS(platform='wechat',processed_plain_text='聊一下实验')))
            self.assertIn(expected,prompt)
            self.assertIn('当前微信昵称',prompt)

    async def test_long_local_profile_does_not_hide_linked_identity(self):
        self.texts[self.wp]+='\n- '+'本地经历'*600
        await self.test_profile_injection_structured_profiles_keep_both_sources()

    async def test_memory_queries_both_stable_ids_after_rename_without_merging_chat_ids(self):
        service=MemoryService()
        async def memory(name,args):
            return {'success':True,'hits':[dict(content='经历:'+args['person_id'],hash=args['person_id'],score=.8)]}
        service._invoke=AsyncMock(side_effect=memory)
        result=await service.search('实验',person_id=self.qp,chat_id='qq-current-session')
        self.assertEqual(len(result.hits),2)
        args=[c.args[1] for c in service._invoke.call_args_list]
        self.assertEqual({x['person_id'] for x in args},{self.wp,self.qp})
        self.assertEqual({x['chat_id'] for x in args},{'qq-current-session'})

    async def test_exact_peer_memory_scope_survives_native_capability_and_service(self):
        from src.plugin_runtime.capabilities.data import RuntimeDataCapabilityMixin
        service=MemoryService();service._invoke=AsyncMock(return_value={'success':True,'hits':[]})
        await service.search('红石',chat_id='peer-session',include_shared=False)
        self.assertIs(service._invoke.call_args.args[1]['include_shared'],False)
        reply=NS(success=True,to_text=lambda **kw:'source-labelled memory')
        with patch('src.services.memory_service.memory_service.search',new=AsyncMock(return_value=reply)) as search:
            result=await RuntimeDataCapabilityMixin()._cap_knowledge_search('peer','knowledge.search',
                {'query':'红石','chat_id':'peer-session','include_shared':False})
        self.assertTrue(result['success'])
        self.assertEqual(result['search_scope'],{'chat_id':'peer-session','include_shared':False})
        self.assertEqual(search.call_args.kwargs['chat_id'],'peer-session')
        self.assertIs(search.call_args.kwargs['include_shared'],False)

    async def test_exact_peer_scope_never_expands_even_with_global_memory_sharing(self):
        from src.A_memorix.host_service import AMemorixHostService
        host=AMemorixHostService();host._runtime_state='ready'
        kernel=NS(search_memory=AsyncMock(return_value={'hits':[]}));host._kernel=kernel
        with patch.object(host,'is_enabled',return_value=True), \
             patch.object(host,'_read_config',return_value={'global_memory_sharing_enabled':True}), \
             patch('core.community_streams.active_groups',new=AsyncMock()) as active:
            await host.invoke('search_memory',{'query':'红石','chat_id':'peer-session','include_shared':False})
        request=kernel.search_memory.call_args.args[0]
        self.assertEqual(request.chat_id,'peer-session')
        self.assertEqual(tuple(request.shared_chat_ids),())
        self.assertTrue(request.respect_filter)
        active.assert_not_awaited()

    async def test_default_memory_search_keeps_existing_admitted_sharing(self):
        from src.A_memorix.host_service import AMemorixHostService
        host=AMemorixHostService();host._runtime_state='ready'
        kernel=NS(search_memory=AsyncMock(return_value={'hits':[]}));host._kernel=kernel
        with patch.object(host,'is_enabled',return_value=True), \
             patch.object(host,'_read_config',return_value={'global_memory_sharing_enabled':False}), \
             patch('src.A_memorix.host_service.AMemorixConfigUtils.get_shared_memory_session_ids',return_value={'local'}), \
             patch('core.community_streams.active_groups',new=AsyncMock(return_value=[NS(session_id='local'),NS(session_id='peer')])):
            await host.invoke('search_memory',{'query':'红石','chat_id':'local'})
        request=kernel.search_memory.call_args.args[0]
        self.assertEqual(request.chat_id,'local')
        self.assertEqual(set(request.shared_chat_ids),{'local','peer'})

    async def test_same_nickname_on_unbound_account_never_links(self):
        unbound='9999999'
        Person.register_person('qq',unbound,'全新昵称')
        self.assertEqual(identity.linked_person_ids(get_person_id('qq',unbound)),[get_person_id('qq',unbound)])
        self.assertEqual(identity.identity_hint('qq',unbound),'')

    async def test_profile_mutation_does_not_propagate_to_other_account(self):
        service=MemoryService();service._invoke_admin=AsyncMock(return_value={'success':True})
        await service.profile_admin(action='set_override',person_id=self.wp,override_text='native profile edit')
        service._invoke_admin.assert_awaited_once()
        self.assertEqual(service._invoke_admin.await_args.kwargs['person_id'],self.wp)

    async def test_legacy_profile_service_also_links_two_stable_ids(self):
        service=MemoryService()
        service._invoke=AsyncMock(side_effect=[{'summary':'微信经历','traits':['红石'],'evidence':[{'source':'wx'}]},
            {'summary':'QQ经历','traits':['Python'],'evidence':[{'source':'qq'}]}])
        result=await service.get_person_profile(self.wp,chat_id='current')
        for token in ('微信经历','QQ经历','wechat:'+self.wx,'qq:'+self.qq,'非本群即时对话'):
            self.assertIn(token,result.summary)
        self.assertEqual({c.args[1]['person_id'] for c in service._invoke.call_args_list},{self.wp,self.qp})
        self.assertEqual({c.args[1]['chat_id'] for c in service._invoke.call_args_list},{'current'})

    async def test_memory_one_backend_exception_keeps_other_platform_results(self):
        service=MemoryService()
        service._invoke=AsyncMock(side_effect=[RuntimeError('one side offline'),{'success':True,'hits':[dict(content='可用的另一端经历',score=.8)]}])
        result=await service.search('实验',person_id=self.wp)
        self.assertTrue(result.success);self.assertEqual(len(result.hits),1)


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(LinkedIdentities))
    engine.dispose()
    report=dict(passed=result.wasSuccessful(),tests=result.testsRun,instance=str(INSTANCE),physical_messages=False,
        failures=[(str(t),e) for t,e in result.failures+result.errors])
    (ROOT/'docs/maibot-wechat-acceptance/linked-identity-20261030.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    raise SystemExit(not result.wasSuccessful())
