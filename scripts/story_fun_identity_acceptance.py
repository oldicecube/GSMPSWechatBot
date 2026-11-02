"""Offline Story/tag/auth, native leisure persistence, strict identity and timeout tests."""
import asyncio
from contextlib import asynccontextmanager, closing
from datetime import datetime, timedelta
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
INSTANCE = tempfile.TemporaryDirectory(prefix='story-fun-', dir=ROOT/'temp', ignore_cleanup_errors=True)
os.environ['MAIBOT_INSTANCE_DIR'] = INSTANCE.name
os.environ['MAIBOT_INTERCEPT_SEND'] = '1'
base = Path(INSTANCE.name)
(base/'config').mkdir()
for name in ('bot_config.toml', 'model_config.toml'):
    shutil.copy2(ROOT/'config'/name, base/'config'/name)


def load(folder):
    name = 'new_accept_'+folder.replace('-', '_')
    pkg = ModuleType(name); pkg.__path__ = [str(ROOT/'plugins'/folder)]; sys.modules[name] = pkg
    return importlib.import_module(name+'.plugin')


repost = load('mai-repost-shit')
story = importlib.import_module(repost.__package__+'.core.story')
life = load('autonomous_life')
from utils.bili_interest import TagStore, parse_report
from utils.fun_journal import FunJournal


def candidate(index, tags, duration=60):
    return story.Candidate(bvid='BV'+str(index), aid=index, cid=index, title=str(index), author='fixture',
                           author_mid=123, play=1, like=1, favorite=1, duration=duration, typename='fixture', tags=tags)


class Tags(unittest.TestCase):
    def test_user_preset_and_strict_report_parser(self):
        values = json.loads((ROOT/'plugins/mai-repost-shit/core/story_tags.json').read_text(encoding='utf8'))
        self.assertEqual(len(values), 333)
        self.assertEqual(values['搞笑'], 12)
        self.assertEqual(values['抽象'], 11)
        self.assertEqual(parse_report('\ufeff标签报告\n排名\t标签\t出现视频数\n1\t东方PROJECT\t3\n2\t东方project\t2'),
                         {'东方PROJECT': 3, '东方project': 2})
        with self.assertRaises(ValueError): parse_report('invalid report')

    def test_four_import_modes_seed_once_and_parse_events(self):
        expected = {'replace': {'b': 5, 'c': 7}, 'update': {'a': 2, 'b': 5, 'c': 7},
                    'add': {'a': 2, 'b': 3, 'c': 7}, 'merge': {'a': 2, 'b': 8, 'c': 7}}
        for mode, values in expected.items():
            with tempfile.TemporaryDirectory() as tmp:
                store = TagStore(Path(tmp)/'tags.db'); store.seed({'a': 2, 'b': 3})
                store.import_tags({'b': 5, 'c': 7}, mode); self.assertEqual(store.tags(), values)
                TagStore(store.path).seed({'a': 999}); self.assertEqual(store.tags(), values)
        with tempfile.TemporaryDirectory() as tmp:
            store = TagStore(Path(tmp)/'tags.db')
            self.assertTrue(store.record_parse('BV1', ['a', 'a', 'b'], 'wechat:g:m1'))
            self.assertFalse(store.record_parse('BV1', ['a'], 'wechat:g:m1'))
            self.assertTrue(store.record_parse('BV1', ['a'], 'qq:g:m2'))
            self.assertEqual(store.tags(), {'a': 2, 'b': 1})
            store.edit('a', 9); store.edit('b', 0, True); self.assertEqual(store.tags(), {'a': 9})
            with self.assertRaises(ValueError): store.edit('a', float('nan'))

    def test_weighted_score_tie_duration_and_exact_tag(self):
        weights = {'a': 10, 'b': 2, 'c': 1, 'd': 10}
        first, second = candidate(1, ['a', 'b', 'c']), candidate(2, ['a', 'b', 'd'])
        self.assertIs(story.pick_batch([first, second], weights, 3), second)
        self.assertIs(story.pick_batch([second, candidate(3, second.tags)], weights, 3), second)
        self.assertIsNone(story.pick_batch([candidate(4, second.tags, 121)], weights, 3))
        self.assertIsNone(story.pick_batch([candidate(4, ['A', 'b', 'd'])], weights, 3))


class StoryProtocol(unittest.IsolatedAsyncioTestCase):
    async def selector(self, tags, page_size=10):
        client = object.__new__(story.StoryClient)
        client.lock = asyncio.Lock(); client.store = NS(tags=lambda: {'a': 2, 'b': 3, 'c': 4})
        async def feed(config, page):
            return [{'aid': (page-1)*page_size+i+1} for i in range(page_size)]
        client.feed = AsyncMock(side_effect=feed)
        client.candidate = AsyncMock(side_effect=lambda row: candidate(row['aid'], tags))
        return client, await client.pick(NS())

    async def test_batches_ten_even_when_server_returns_five(self):
        client, result = await self.selector(['a', 'b', 'c'], page_size=5)
        self.assertEqual(result.aid, 1)
        self.assertEqual(client.feed.await_count, 2)
        self.assertEqual(client.candidate.await_count, 10)

    async def test_twenty_and_fifty_video_relaxation(self):
        client, result = await self.selector(['a', 'b'])
        self.assertEqual(client.candidate.await_count, 20); self.assertEqual(result.aid, 1)
        client, result = await self.selector(['a'])
        self.assertEqual(client.candidate.await_count, 50); self.assertEqual(result.aid, 1)
        client, result = await self.selector(['unmatched'])
        self.assertIsNone(result); self.assertEqual(client.candidate.await_count, 60)

    async def test_story_endpoint_is_not_homepage_and_account_is_not_uid_only(self):
        client = object.__new__(story.StoryClient)
        client.request = AsyncMock(return_value={'code': 0, 'data': {'items': [{'aid': 1}]}})
        client.access = AsyncMock(return_value='secret-fixture')
        cfg = NS(use_account=False, expected_uid='456', seed_aid=7)
        await client.feed(cfg, 2)
        self.assertEqual(client.request.call_args.args[1], '/x/v2/feed/index/story')
        client.access.assert_not_awaited()
        cfg.use_account = True; await client.feed(cfg, 3)
        client.access.assert_awaited_once_with('456')
        self.assertEqual(client.request.call_args.args[2]['access_key'], 'secret-fixture')
        client.request.return_value = {'code': 0, 'data': {'mid': 123}}
        with self.assertRaises(story.BiliError): await client.verify({'accessToken': 'fixture'}, '456')
        with self.assertRaises(story.BiliError): await client.verify({'appKey': 'wrong', 'accessToken': 'fixture'})
        with self.assertRaises(story.BiliError): await client.verify({'mid': 123})

    async def test_auth_refresh_and_signature(self):
        from urllib.parse import urlencode
        params = story.signed({'z': '空 格', 'ts': 1, 'none': None})
        unsigned = {k: v for k, v in params.items() if k != 'sign'}
        self.assertEqual(params['sign'], hashlib.md5((urlencode(sorted(unsigned.items()))+story.APPSEC).encode()).hexdigest())
        with tempfile.TemporaryDirectory() as tmp:
            store = TagStore(Path(tmp)/'auth.db')
            store.set('auth', {'accessToken': 'expired', 'refreshToken': 'refresh', 'accessTokenExpiresAt': 1})
            client = object.__new__(story.StoryClient); client.store = store; client.auth_lock = asyncio.Lock()
            client.request = AsyncMock(return_value={'code': 0, 'data': {'token_info': {'access_token': 'renewed', 'expires_in': 7200}}})
            client.verify = AsyncMock(side_effect=lambda token, uid: {**token, 'mid': uid, 'verified_at': __import__('time').time()})
            self.assertEqual(await client.access('123'), 'renewed')
            self.assertEqual(store.get('auth')['mid'], '123')

    async def test_private_webui_and_native_config_declarations(self):
        from src.plugin_runtime.webui_schema import load_webui_extension
        extension = load_webui_extension(str(ROOT/'plugins/mai-repost-shit'))
        self.assertEqual(extension.pages[0].id, 'story')
        self.assertTrue(extension.pages[0].actions['importTags'].confirmation)
        components = repost.create_plugin().get_components()
        self.assertTrue({'repost.story.import', 'repost.story.auth', 'repost.manual_one', 'browse_story'} <= {row['name'] for row in components})
        self.assertIn('story', repost.ShitpostConfig.model_fields)


class Leisure(unittest.TestCase):
    def test_global_share_budget_is_preserved_for_native_fun_replies(self):
        import sqlite3
        from utils.fun_share_budget import reserve_for_turn,record_result
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'MAIBOT_INSTANCE_DIR':tmp}):
            def context(number,stream='wx'):
                return NS(engine=NS(_tool_request_message=NS(message_id='fun-'+str(number),
                    message_info=NS(additional_config={'proactive_target':True}))),runtime=NS(session_id=stream))
            first=context(0)
            self.assertTrue(reserve_for_turn(first));self.assertTrue(reserve_for_turn(first))
            self.assertFalse(reserve_for_turn(context(1,'qq')))
            record_result(first,'sent')
            path=Path(tmp)/'data/plugins/wechatbot.autonomous-life/life.sqlite3'
            for i in range(1,4):
                with closing(sqlite3.connect(path)) as db, db:db.execute('UPDATE shares SET stamp=stamp-8000')
                self.assertTrue(reserve_for_turn(context(i,'qq')))
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('UPDATE shares SET stamp=stamp-8000')
                self.assertEqual(db.execute("SELECT count(*) FROM shares WHERE state='sent'").fetchone()[0],1)
            self.assertFalse(reserve_for_turn(context(5)))
            self.assertFalse(reserve_for_turn(context(0)))
            # Ordinary chat responses can still refer to discoveries without
            # being mistaken for a fresh autonomous broadcast.
            self.assertTrue(reserve_for_turn(NS(engine=NS(),runtime=NS(session_id='wx'))))

    def test_continuity_pause_abandon_and_report_consumption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/'fun.db'; journal = FunJournal(path)
            first = journal.reserve('wx', '游戏实验', 'code', '先验证算法')
            with self.assertRaises(ValueError): journal.reserve('wx', '另一个', 'hot', '查资料')
            self.assertTrue(journal.finish(first['id'], 'completed', '真实实验得到42，下一步可视化'))
            journal = FunJournal(path)
            self.assertEqual(len(journal.snapshot('wx')['reports']), 1)
            resumed = journal.reserve('wx', 'ignored', 'code', '继续画图', first['direction'])
            self.assertIn('得到42', resumed['task'])
            self.assertEqual(journal.snapshot('qq')['directions'], [])
            self.assertEqual(journal.transition('wx', first['direction'], 'pause'), [resumed['id']])
            self.assertFalse(journal.finish(resumed['id'], 'completed', 'late callback'))
            self.assertFalse(journal.consume('qq', first['id']))
            self.assertTrue(journal.consume('wx', first['id']))
            self.assertEqual(journal.snapshot('wx')['reports'], [])
            journal.transition('wx', first['direction'], 'abandon')
            with self.assertRaises(ValueError): journal.reserve('wx', 'ignored', 'code', 'continue', first['direction'])

    def test_native_fun_components_and_daily_budget(self):
        names = {row['name'] for row in life.create_plugin().get_components()}
        self.assertTrue({'fun_direction', 'persistent_fun_planner'} <= names)
        with tempfile.TemporaryDirectory() as tmp:
            journal = FunJournal(Path(tmp)/'fun.db')
            for i in range(12):
                run = journal.reserve('s'+str(i), '题目', 'hot', '任务'); journal.finish(run['id'], 'failed', '失败')
            with self.assertRaises(ValueError): journal.reserve('extra', '题目', 'hot', '任务')

    def test_old_projects_are_imported_once_without_claiming_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal=FunJournal(Path(tmp)/'fun.db')
            old=[{'title':'旧实验','status':'done','code':'print(42)','note':'尚待验证','updated':1}]
            journal.import_legacy_projects('wx',old);journal.import_legacy_projects('wx',old)
            rows=journal.snapshot('wx')['directions'];self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['state'],'paused');self.assertIn('核验',rows[0]['objective'])

    def test_sandbox_code_and_real_artifacts_survive_restart(self):
        from src.core.tooling import ToolExecutionResult
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'fun.db';journal=FunJournal(path)
            run=journal.reserve('wx','实验','code','写程序')
            journal.record_step(run['id'],'sandbox_program',{'code':'print(42)'},
                ToolExecutionResult(tool_name='sandbox_program',success=True,
                    structured_content={'stdout':'42','artifacts':[{'name':'result.txt','text':'42'}]}))
            material=FunJournal(path).continuation_material(run['direction'])
            self.assertIn('print(42)',material);self.assertIn('result.txt',material);self.assertIn('42',material)


class NativeLeisure(unittest.IsolatedAsyncioTestCase):
    async def test_real_native_manager_launch_report_and_consumption(self):
        from src.plugin_runtime.fun_system import control
        from src.maisaka.subagent_tasks import SubAgentTaskManager,SubAgentOutcome
        from src.chat.heart_flow.heartflow_manager import heartflow_manager
        from src.core.tooling import ToolSpec
        manager=SubAgentTaskManager()
        specs=[ToolSpec(name=name,description=name,parameters_schema={}) for name in
               ('web_search','sandbox_program','send_message','rcon')]
        runtime=NS(chat_stream=NS(platform='qq',account_id='123',scope='napcat-local',group_id='456',is_group_session=True),
            _subagent_tasks=manager,_tool_registry=NS(list_tools=AsyncMock(return_value=specs)),
            _reasoning_engine=NS(_build_tool_availability_context=Mock(return_value=None)),deliver_subagent_result=Mock())
        async def rounds(*args,**kwargs):return SubAgentOutcome('实际执行结果'+('x'*2000),{'sandbox_program':1})
        with patch.object(heartflow_manager,'get_or_create_heartflow_chat',AsyncMock(return_value=runtime)), \
             patch('src.platform_io.get_platform_io_manager',return_value=NS(is_target_allowed=AsyncMock(return_value=True))), \
             patch('src.platform_io.chat_features.route_override',return_value=None), \
             patch('src.maisaka.builtin_tool.subagent._run_sub_agent_rounds',side_effect=rounds) as worker:
            args={'stream_id':'native-fun','action':'start','mode':'code','title':'实验','task':'验证一个算法'}
            result=await control('wechatbot.autonomous-life',args)
            self.assertTrue(result['success'])
            self.assertEqual(set(result['granted_tools']),{'web_search','sandbox_program'})
            await manager.get(result['task_id']).asyncio_task
            runtime.deliver_subagent_result.assert_called_once()
            status=await control('wechatbot.autonomous-life',{'stream_id':'native-fun','action':'status'})
            self.assertEqual(len(status['reports']),1);self.assertLessEqual(len(status['reports'][0]['report']),2200)
            self.assertIn('sandbox_program',status['reports'][0]['report'])
            self.assertTrue((await control('wechatbot.autonomous-life',{'stream_id':'native-fun','action':'consume',
                            'report_id':result['task_id']}))['success'])
            self.assertFalse((await control('other',args))['success'])

    async def test_planner_receives_persistent_reports_without_visible_send(self):
        plugin=life.create_plugin()
        plugin.set_plugin_config({'plugin':{'enabled':True,'config_version':'1.0.0'},'fun':{'enabled':True},'life':{'groups':['wx@chatroom']}})
        plugin._set_context(NS(paths=NS(data_dir=base/'fun-plugin')))
        plugin.store=None;plugin.fun_group=AsyncMock(return_value='wx@chatroom')
        journal=FunJournal(base/'fun-plugin/fun.sqlite3')
        run=journal.reserve('wx-sid','实际实验','code','任务');journal.finish(run['id'],'completed','实际结果42')
        result=await plugin.fun_planner(session_id='wx-sid',items=[])
        text=result['modified_kwargs']['items'][0]['parts'][0]['text']
        self.assertIn('实际结果42',text);self.assertIn('可以暂存',text)
        self.assertEqual(len(journal.snapshot('wx-sid')['reports']),1)
        journal.consume('wx-sid',run['id'])
        result=await plugin.fun_planner(session_id='wx-sid',items=[])
        self.assertNotIn('"reports": [{',result['modified_kwargs']['items'][0]['parts'][0]['text'])


class ParseIntegration(unittest.TestCase):
    def test_successful_bv_jobs_add_weights_once_per_request(self):
        from plugins import bv
        with tempfile.TemporaryDirectory() as tmp:
            store=TagStore(Path(tmp)/'tags.db');store.set('parse_linked_enabled',True)
            plan={'info':{'bvid':'BVfixture','title':'fixture'},'stream':{}}
            with patch.object(bv,'TEMP_DIR',tmp),patch.object(bv.bilibili,'resolve',return_value=plan), \
                 patch.object(bv.bilibili,'download'),patch.object(bv.bilibili,'fetch_video_tags',return_value=['a','a','b']), \
                 patch.object(bv,'send',return_value=(True,'')),patch.object(bv,'_send_text',return_value=True), \
                 patch('utils.bili_interest.default_path',return_value=store.path):
                bv._run_job('wx','text','BVfixture','wechat:g:message')
                bv._run_job('qq','text','BVfixture','qq:g:message')
                bv._run_job('wx','text','BVfixture','wechat:g:message')
                self.assertEqual(store.tags(),{'a':2,'b':2})
                store.set('parse_linked_enabled',False)
                bv._run_job('another','text','BVfixture','wechat:another:new')
                self.assertEqual(store.tags(),{'a':2,'b':2})


class ManualRepost(unittest.IsolatedAsyncioTestCase):
    async def test_shit_routes_both_platforms_without_planner(self):
        from plugins import shit
        from src.chat.message_receive.chat_manager import chat_manager
        manager=NS(_cap_api_call=AsyncMock(return_value={'success':True,'result':{'sent':True}}))
        sessions={'wx':NS(platform='wechat',account_id='wxid_self',scope='',group_id='g@chatroom',group_name='Group',session_id='wx'),
                  'qq':NS(platform='qq',account_id='123',scope='napcat',group_id='456',group_name='Group',session_id='qq')}
        with patch.dict(chat_manager.sessions,sessions,clear=True),patch('src.plugin_runtime.integration.get_plugin_runtime_manager',return_value=manager):
            wx={'platform':'wechat','native_session_id':'wx','sessionId':'g@chatroom','account_id':'wxid_self','scope':''}
            qq={'platform':'qq','native_session_id':'qq','sessionId':'qq:123:napcat:group:456','account_id':'123','scope':'napcat'}
            self.assertTrue((await shit._invoke('bv',wx))['sent'])
            self.assertEqual(manager._cap_api_call.call_args.args[2],{'api_name':'repost.manual_one','args':{'stream_id':'wx'}})
            self.assertTrue((await shit._invoke('tb',qq))['sent'])
            self.assertEqual(manager._cap_api_call.call_args.args[2],{'api_name':'repost.tieba_manual','args':{'stream_id':'qq'}})

    async def test_paused_tieba_allows_only_explicit_manual_api(self):
        tieba=load('tieba_share');plugin=tieba.create_plugin()
        plugin.set_plugin_config({'plugin':{'enabled':True,'config_version':'1.1.8'},'tool':{'enabled':False},
                                  'admin':{'allow_groups':['456']},'stone':{'preset_forums':['搬石']}})
        plugin._set_context(NS(chat=NS(get_all_streams=AsyncMock(return_value=[
            {'session_id':'qq','platform':'qq','account_id':'123','scope':'napcat','group_id':'456'}]))))
        plugin._run_round=AsyncMock(return_value=tieba._RoundSummary('sent',landed_posts=1))
        self.assertFalse((await plugin.repost_one(stream_id='qq'))['sent'])
        self.assertTrue((await plugin.manual_one(stream_id='qq'))['sent'])


class IdentityAndFreshness(unittest.TestCase):
    def test_observed_nickname_updates_even_when_group_card_is_unchanged(self):
        from src.person_info.person_info import Person, get_person_id
        from src.common.database.database import get_db_session
        from src.common.database.database_model import PersonInfo
        from sqlmodel import select
        pid=get_person_id('wechat','wxid_rename')
        with get_db_session() as db:
            db.add(PersonInfo(person_id=pid,platform='wechat',user_id='wxid_rename',user_nickname='旧名',person_name='旧名',
                is_known=True,memory_points='[]',group_cardname=json.dumps([{'group_id':'g','group_cardname':'群名片'}])))
        Person.register_person('wechat','wxid_rename','新名','g','群名片')
        with get_db_session() as db:
            row=db.exec(select(PersonInfo).where(PersonInfo.person_id==pid)).first()
            self.assertEqual(row.user_nickname,'新名');self.assertEqual(row.person_name,'旧名')

    def test_proactive_actor_is_bot_not_last_group_member(self):
        from src.maisaka.runtime import MaisakaHeartFlowChatting
        from src.common.data_models.mai_message_data_model import GroupInfo
        from src.maisaka.context.planner_messages import is_current_account
        runtime=NS(chat_stream=NS(platform='wechat',account_id='wxid_self_abcd',scope='',user_id='wxid_old_member'),
                   session_id='wx-sid',_build_group_info=lambda:GroupInfo(group_id='g@chatroom',group_name='Group'))
        message=MaisakaHeartFlowChatting._build_proactive_trigger_message(runtime,'fresh-task','可选新话题')
        self.assertEqual(message.message_info.user_info.user_id,'wxid_self')
        self.assertTrue(message.is_notify)
        self.assertTrue(is_current_account('wechat','wxid_self_abcd','wxid_self'))
        self.assertFalse(is_current_account('wechat','wxid_self_abcd','wxid_self_zzzz'))

    def test_exact_identity_ambiguity_platform_and_rename(self):
        from src.common.database.database import get_db_session
        from src.common.database.database_model import PersonInfo
        from src.person_info.person_info import get_person_id_by_person_name, get_person_id
        def person(uid, nickname, platform='wechat'):
            return PersonInfo(person_id=get_person_id(platform, uid), platform=platform, user_id=uid,
                user_nickname=nickname, person_name=nickname,
                group_cardname=json.dumps([{'group_id':'g','group_cardname':nickname}], ensure_ascii=False))
        with get_db_session() as db:
            db.add(person('wxid_a','冰块'));db.add(person('wxid_b','老冰块'));db.add(person('123','冰块','qq'))
        self.assertEqual(get_person_id_by_person_name('冰块', 'wechat'), get_person_id('wechat','wxid_a'))
        self.assertEqual(get_person_id_by_person_name('冰'), '')
        self.assertEqual(get_person_id_by_person_name('冰块'), '')
        self.assertEqual(get_person_id_by_person_name('wxid_b', 'wechat'), get_person_id('wechat','wxid_b'))
        from src.maisaka.builtin_tool.query_memory import _resolve_person_id
        self.assertEqual(_resolve_person_id(person_name='冰块', platform='wechat', user_id='wxid_b', group_id='g')[0], get_person_id('wechat','wxid_b'))
        with get_db_session() as db:
            from sqlmodel import select
            row=db.exec(select(PersonInfo).where(PersonInfo.person_id==get_person_id('wechat','wxid_a'))).first()
            row.user_nickname='新昵称';row.person_name='新昵称';row.group_cardname='[]';db.add(row)
        self.assertEqual(get_person_id_by_person_name('新昵称','wechat'),get_person_id('wechat','wxid_a'))
        self.assertEqual(get_person_id_by_person_name('冰块','wechat'),'')

    def test_manual_aliases_are_exact_and_ambiguous_alias_is_not_guessed(self):
        from src.A_memorix.core.utils.person_profile_service import PersonProfileService
        obj=object.__new__(PersonProfileService)
        obj.resolve_person_id=Mock(return_value='')
        obj.metadata_store=NS(find_exact_profile_alias_ids=Mock(return_value=['person-a']))
        self.assertEqual(obj.resolve_profile_identity('明确别称'),'person-a')
        obj.metadata_store.find_exact_profile_alias_ids.return_value=['person-a','person-b']
        self.assertEqual(obj.resolve_profile_identity('同名别称'),'')

    def test_external_bot_cannot_become_self_from_plugin_metadata(self):
        from src.maisaka.context.planner_messages import build_planner_user_prefix_from_session_message
        from src.common.data_models.message_component_data_model import MessageSequence, TextComponent
        message=NS(platform='wechat', account_id='wxid_self', scope='', session_id='test', message_id='external',
            timestamp=datetime.now(), is_notify=False, raw_message=MessageSequence([TextComponent('I am the bot')]),
            message_info=NS(user_info=NS(user_id='wxid_other',user_nickname='相似名字',user_cardname='相似名字'),
                additional_config={'plugin_activity':{'kind':'plugin_output','plugin_id':'other'}}))
        with patch('src.common.bot_interaction_policy.message_bot_rule',return_value={'is_bot':True}), \
             patch('utils.account_identity.identity_hint',return_value=''):
            prefix=build_planner_user_prefix_from_session_message(message,is_self_message=True)
        self.assertIn('user_id="wxid_other"',prefix)
        self.assertIn('is_self_message="false"',prefix)
        self.assertNotIn('is_self_message="true"',prefix)
        self.assertIn('不采用其语气',prefix)

    def test_freshness_boundary_and_cache_consumption(self):
        from src.maisaka.context.freshness import is_stale_message,expired_target_hint
        from src.maisaka.runtime import MaisakaHeartFlowChatting
        now=datetime.now();old=NS(timestamp=now-timedelta(hours=1,seconds=1));fresh=NS(timestamp=now)
        self.assertTrue(is_stale_message(old,now=now));self.assertFalse(is_stale_message(fresh,now=now))
        runtime=NS(message_cache=[old,fresh],_last_processed_index=0,_fixed_message_debouncer=NS(consume=Mock()))
        self.assertEqual(MaisakaHeartFlowChatting._collect_pending_messages(runtime),[fresh])
        self.assertEqual(runtime._last_processed_index,2)
        old.message_id='old-message';fresh.message_id='fresh-message'
        hint=expired_target_hint([NS(original_message=old),NS(original_message=fresh)])
        from src.maisaka.context.message_id_alias import to_display_message_id
        self.assertIn(to_display_message_id(old.message_id),hint)
        self.assertNotIn(to_display_message_id(fresh.message_id),hint)


class TimeoutAccounting(unittest.IsolatedAsyncioTestCase):
    async def test_cached_profile_never_overrides_current_identity(self):
        from src.A_memorix.core.utils.person_profile_service import PersonProfileService
        obj=object.__new__(PersonProfileService)
        obj.metadata_store=NS(get_latest_person_profile_snapshot=Mock(return_value={
            'person_id':'wrong-old-id','person_name':'旧昵称','aliases':['旧别称'],'profile_text':'facts'}))
        obj._is_snapshot_stale=Mock(return_value=False)
        obj.get_person_aliases=Mock(return_value=(['新名片'],'新名片',[]))
        obj._apply_manual_override=lambda pid,payload:payload
        result=await obj.query_person_profile(person_id='correct-id')
        self.assertEqual(result['person_id'],'correct-id');self.assertEqual(result['person_name'],'新名片')
        self.assertEqual(result['aliases'],['新名片'])

    async def test_reply_tool_rejects_old_target_before_replyer(self):
        from src.maisaka.builtin_tool.reply import execute_reply, _resolve_reply_quote
        from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
        old=NS(message_id='old',timestamp=datetime.now()-timedelta(hours=2))
        runtime=NS(session_id='wx',chat_stream=NS(platform='wechat'),_chat_loop_service=NS(_active_tool_names=[]))
        result=await execute_reply(BuiltinToolRuntimeContext(NS(),runtime),{'msg_id':'old','reply_reference':'旧问题'},
                                   call_id='test',target_message=old)
        self.assertFalse(result.success);self.assertIn('超过1小时',result.content or result.error_message)
        proactive=NS(message_info=NS(additional_config={'proactive_target':True}))
        self.assertEqual(await _resolve_reply_quote(BuiltinToolRuntimeContext(NS(),runtime),'新发现',proactive),(False,0.0))

    async def test_pacing_is_outside_network_timeout(self):
        import src.llm_models.utils_model as mod
        obj=object.__new__(mod.LLMOrchestrator)
        obj.model_for_task=NS(hard_timeout=0.01);obj.request_type='test';obj.task_name='utils'
        obj._record_success_generation_attempt=Mock()
        provider=NS(max_retry=1,name='fixture',retry_interval=0)
        request=NS(model_info=NS(name='fixture',model_identifier='fixture'),trace_context=None)
        response=NS()
        client=NS(get_audio_transcriptions=AsyncMock(return_value=response))
        @asynccontextmanager
        async def usage(*args):yield
        async def pace(*args):await asyncio.sleep(0.04)
        with patch.object(mod,'wait_for_request_slot',side_effect=pace),patch.object(mod,'blocked_reason',return_value=''), \
             patch.object(mod,'track_model_task_usage',usage),patch.object(mod,'mark_request_succeeded',Mock()):
            self.assertIs(await obj._attempt_request_on_model_with_timeout(provider,client,request,'fixture'),response)
            client.get_audio_transcriptions=AsyncMock(side_effect=lambda req: asyncio.sleep(0.05))
            async def slow(req):await asyncio.sleep(0.05);return response
            client.get_audio_transcriptions.side_effect=slow
            with self.assertRaises(asyncio.TimeoutError):await obj._attempt_request_on_model(provider,client,request)


if __name__=='__main__':
    unittest.main(verbosity=2)
