"""Isolated peer references, real SDK hooks, legacy budget migration and binding receipts."""
import asyncio
import importlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
INSTANCE = tempfile.TemporaryDirectory(prefix='peer-context-',dir=ROOT/'temp',ignore_cleanup_errors=True)
os.environ['MAIBOT_INSTANCE_DIR'] = INSTANCE.name


def load(folder):
    name = 'context_test_'+folder
    pkg = ModuleType(name); pkg.__path__=[str(ROOT/'plugins'/folder)]; sys.modules[name]=pkg
    return importlib.import_module(name+'.plugin')


mod = load('community_context')
life = load('autonomous_life')


class PeerContext(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = mod.create_plugin()
        self.plugin.set_plugin_config({'plugin':{'enabled':True,'config_version':'1.0.0'},'bridge':{'group_name':'同名群',
            'targets':['test@chatroom','qq:123:scope:group:456']}})
        self.wx = dict(session_id='wx',platform='wechat',group_id='test@chatroom',group_name='同名群')
        self.qq = dict(session_id='qq',platform='qq',account_id='123',scope='scope',group_id='456',group_name='同名群')
        self.streams = [self.wx,self.qq]
        self.message = dict(message_id='remote-control-id',platform='qq',account_id='123',scope='scope',
            timestamp=time.time(),message_info=dict(user_info=dict(user_id='private-uid',nickname='小明'),group_info=dict(group_id='456')),
            raw_message=[dict(type='text',data='隔壁在做红石计时实验')])
        self.ctx = NS(chat=NS(get_all_streams=AsyncMock(return_value=self.streams)),logger=logging.getLogger('peer-test'),
            call_capability=AsyncMock(side_effect=self.capability))
        self.plugin._set_context(self.ctx)

    async def capability(self, name, **kwargs):
        if name=='frequency.get_current_talk_value':return {'success':True,'value':1}
        if name=='knowledge.search':raise RuntimeError('embedding offline')
        return [self.message]

    async def test_native_components_schema(self):
        names = {c['name'] for c in self.plugin.get_components()}
        self.assertTrue({'peer_group_context','peer_group_planner_context','peer_group_reply_context','community_context.reference'} <= names)
        self.assertIn('refresh_seconds',str(mod.Config.model_json_schema()))

    async def test_bidirectional_route_and_same_name(self):
        self.assertEqual(mod.peer_for(self.streams,'wx',self.plugin.config.bridge),self.qq)
        self.assertEqual(mod.peer_for(self.streams,'qq',self.plugin.config.bridge),self.wx)

    async def test_wrong_account_scope_same_name_private_and_ambiguity(self):
        cfg=self.plugin.config.bridge
        for changes in ({'account_id':'999'},{'scope':'other'},{'group_name':'他群'},{'group_id':None}):
            self.assertIsNone(mod.peer_for([self.wx,{**self.qq,**changes}],'wx',cfg))
        self.assertIsNone(mod.peer_for([*self.streams,{**self.qq,'session_id':'duplicate'}],'wx',cfg))
        self.assertIsNone(mod.peer_for(self.streams,'not-authorized',cfg))

    async def test_history_no_control_ids_no_binary_and_filters(self):
        invalid = [dict(self.message,account_id='999'),dict(self.message,scope='other'),dict(self.message,is_bot=True),
            dict(self.message,is_notify=True),dict(self.message,timestamp=time.time()-49*3600),dict(self.message,timestamp=time.time()+90),
            dict(self.message,message_info={'group_info':{'group_id':'999'}})]
        rows=mod.excerpts([self.message,*invalid],self.qq,self.plugin.config.bridge,time.time())
        self.assertEqual(len(rows),1)
        encoded=json.dumps(rows)
        self.assertNotIn('remote-control-id',encoded); self.assertNotIn('private-uid',encoded)

    async def test_real_planner_and_replyer_payloads_separate_context(self):
        from src.plugin_runtime.hook_payloads import deserialize_prompt_items
        # Validate with the same native deserializer used by the planner.
        result=await self.plugin.planner(session_id='wx',items=[])
        items=result['modified_kwargs']['items']
        self.assertEqual(items[0]['item_type'],'SystemMessageItem')
        self.assertIn('不是当前会话',items[0]['parts'][0]['text'])
        self.assertIn('"source_platform": "qq"',items[0]['parts'][0]['text'])
        deserialize_prompt_items(items)
        reply=await self.plugin.replyer(session_id='wx',extra_prompt='existing-native-hint')
        self.assertTrue(reply['modified_kwargs']['extra_prompt'].startswith('existing-native-hint'))
        reads=[c for c in self.ctx.call_capability.await_args_list if c.args[0]=='message.get_recent']
        self.assertEqual(len(reads),1)
        self.assertFalse(reads[0].kwargs['include_binary_data'])

    async def test_context_disable_and_peer_revoke_invalidate_cache(self):
        self.assertTrue(await self.plugin.context('wx'))
        self.ctx.chat.get_all_streams.return_value=[self.wx]
        self.assertIsNone(await self.plugin.context('wx'))
        self.plugin.config.plugin.enabled=False
        self.assertIsNone(await self.plugin.context('wx'))

    async def test_optional_memory_failure_keeps_labeled_context(self):
        await self.plugin.context('wx')
        result=await self.plugin.query(query='红石',stream_id='wx')
        self.assertTrue(result['success']); self.assertIn('memory_status',result)

    async def test_peer_memory_query_uses_other_chat_in_both_directions(self):
        async def memory_capability(name, **kwargs):
            if name=='knowledge.search':
                return {'success':True,'content':'已保存的旁群红石讨论',
                    'search_scope':{'chat_id':kwargs['chat_id'],'include_shared':False}}
            return await self.capability(name, **kwargs)
        self.ctx.call_capability.side_effect=memory_capability
        self.plugin.config.bridge.automatic_memory=False
        for sid, peer_sid, platform in [('wx','qq','qq'),('qq','wx','wechat')]:
            self.message.update(platform=platform,account_id='123' if platform=='qq' else '',scope='scope' if platform=='qq' else '')
            self.message['message_info']['group_info']['group_id']='456' if platform=='qq' else 'test@chatroom'
            response=await self.plugin.query(query='红石',session_id=sid)
            self.assertTrue(response['success'])
            memory=response['related_memory_with_source']
            self.assertEqual(memory['source_platform'],platform)
            self.assertEqual(memory['context_kind'],'reference_not_current_conversation')
            call=[c for c in self.ctx.call_capability.await_args_list if c.args[0]=='knowledge.search'][-1]
            self.assertEqual(call.kwargs['chat_id'],peer_sid)
            self.assertTrue(call.kwargs['respect_filter'])
            self.assertFalse(call.kwargs['include_shared'])

    async def test_automatic_peer_memory_is_labeled_bounded_and_cached(self):
        async def memory_capability(name, **kwargs):
            if name=='knowledge.search':return {'success':True,'content':'QQ群之前也讨论过红石方案'*100,
                'search_scope':{'chat_id':kwargs['chat_id'],'include_shared':False}}
            return await self.capability(name, **kwargs)
        self.ctx.call_capability.side_effect=memory_capability
        text=await self.plugin.context('wx')
        payload=json.loads(text[len(mod.POLICY):])
        self.assertEqual(payload['source_platform'],'qq')
        self.assertIn('related_peer_memory',payload)
        self.assertLessEqual(len(text),self.plugin.config.bridge.context_chars)
        self.assertEqual(await self.plugin.context('wx'),text)
        calls=[c for c in self.ctx.call_capability.await_args_list if c.args[0]=='knowledge.search']
        self.assertEqual(len(calls),1);self.assertEqual(calls[0].kwargs['chat_id'],'qq')

    async def test_slow_peer_memory_does_not_hide_recent_context(self):
        async def slow(name, **kwargs):
            if name=='knowledge.search':await asyncio.sleep(10)
            return await self.capability(name, **kwargs)
        self.ctx.call_capability.side_effect=slow
        self.plugin.config.bridge.memory_timeout_seconds=.2
        text=await asyncio.wait_for(self.plugin.guarded_context('wx'),2)
        self.assertIn('隔壁在做红石计时实验',text)

    async def test_old_host_without_scope_ack_never_labels_shared_memory_as_peer_only(self):
        async def old_host(name, **kwargs):
            if name=='knowledge.search':return {'success':True,'content':'可能来自其他群的资料'}
            return await self.capability(name, **kwargs)
        self.ctx.call_capability.side_effect=old_host
        result=await self.plugin.query(query='红石',stream_id='wx')
        self.assertTrue(result['success'])
        self.assertIn('memory_status',result)
        self.assertNotIn('related_memory_with_source',result)
        self.assertNotIn('可能来自其他群的资料',result['peer_reference'])

    async def test_reference_retains_valid_json_and_latest_rows(self):
        rows=[dict(time='now',speaker='human',text=str(n)+'x'*400) for n in range(8)]
        text=mod.render_reference(self.qq,rows,self.plugin.config.bridge,'saved memory'*100)
        payload=json.loads(text[len(mod.POLICY):])
        self.assertTrue(payload['messages'][-1]['text'].startswith('7'))
        self.assertLessEqual(len(text),self.plugin.config.bridge.context_chars)

    async def test_observed_sharing_and_thought_counts_are_truthful(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=life.Store(Path(tmp)/'life.sqlite3')
            for n in range(3):
                event=store.reserve('today',n,'sid','thought',72)
                store.finish(event,{'action':'think'})
            budget=store.activity_budget('today','sid',1000,12,8,4)
            self.assertEqual(budget['consecutive_private_thoughts'],3)
            self.assertEqual(budget['actions_done'],0)
            self.assertEqual(budget['shares_sent_today'],0)
            self.assertIsNone(budget['seconds_since_last_share'])
            self.assertFalse(budget['public_reply_required'])
            store.reserve_share('today',900,'other','fingerprint','',4,0,0)
            store.share_result('fingerprint','sent')
            budget=store.activity_budget('today','sid',1000,12,8,4)
            self.assertEqual(budget['shares_sent_today'],1)
            self.assertEqual(budget['shares_remaining'],3)
            self.assertEqual(budget['seconds_since_last_share'],100)

    async def test_native_disabled_peer_does_not_serve_cached_context(self):
        self.assertTrue(await self.plugin.context('wx'))
        self.ctx.call_capability.side_effect=None
        self.ctx.call_capability.return_value={'success':True,'value':0}
        self.assertIsNone(await self.plugin.context('wx'))

    async def test_legacy_thoughts_do_not_starve_real_action_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            store=life.Store(Path(tmp)/'life.sqlite3')
            for n in range(11):
                event=store.reserve('today',n,'s','action',12)
                store.finish(event,{'action':'think','thought':'private'})
            failure=store.reserve('today',12,'s','action',12)
            store.finish(failure,{'error':'unknown'},'failed')
            self.assertEqual(store.count('today','action'),1)
            for n in range(11):
                self.assertIsNotNone(store.reserve('today',n,'s','action',12))
            self.assertIsNone(store.reserve('today',20,'s','action',12))
            self.assertEqual(store.count('today','action'),12)
            with store.db() as db:
                self.assertEqual(db.execute('select count(*) from events').fetchone()[0],23)

    async def test_model_declines_repost_without_consuming_share_or_action_budget(self):
        plugin=life.create_plugin()
        plugin.set_plugin_config({'plugin':{'enabled':True,'config_version':'1.0.0'},'life':{'groups':['test@chatroom']}})
        with tempfile.TemporaryDirectory() as tmp:
            plugin.store=life.Store(Path(tmp)/'life.sqlite3')
            plugin._set_context(NS(config=NS(get=AsyncMock(return_value='native persona'))))
            plugin.awake=lambda now: True
            now=plugin.now().timestamp()
            rows=[dict(id=str(n),person='human'+str(n%2),stamp=now,text='https://b23.tv/interesting'+str(n)) for n in range(6)]
            plugin.context=AsyncMock(return_value=('sid',rows))
            plugin.model=AsyncMock(return_value={'repost':False})
            result=await plugin.repost_opportunity(group_id='test@chatroom')
            self.assertTrue(result['skipped'])
            self.assertEqual(plugin.store.count(plugin.now().date().isoformat(),'action'),0)
            with plugin.store.db() as db:self.assertEqual(db.execute('select count(*) from shares').fetchone()[0],0)
            await plugin.repost_opportunity(group_id='test@chatroom')
            self.assertEqual(plugin.model.await_count,1)


class BindingReceipt(unittest.TestCase):
    def test_other_platform_name_and_native_cached_impression(self):
        from utils import account_identity as identity,sqlite_store as store
        from plugins import bind
        base=Path(INSTANCE.name)/'binding'
        override=patch.object(store,'BASE_DIR',str(base)); override.start(); self.addCleanup(override.stop)
        store.DB_PATH=str(base/'data'/'bind-test.sqlite3')
        Path(store.DB_PATH).parent.mkdir(parents=True,exist_ok=True)
        store.player_stats_ensure('Player')
        self.assertNotIn('另一侧',bind.handle('Player',dict(platform='wechat',wxid='wxid_test',user='微信新昵称')))
        data=base/'data'
        with sqlite3.connect(data/'MaiBot.db') as db:
            db.execute('create table if not exists person_info(platform,user_id,user_nickname,person_name,memory_points)')
            db.execute('insert into person_info values(?,?,?,?,?)',('wechat','wxid_test','更新后的微信昵称','',json.dumps(['喜欢红石实验'],ensure_ascii=False)))
        receipt=bind.handle('Player',dict(platform='qq',user_id='123',user='QQ昵称'))
        self.assertIn('微信：更新后的微信昵称',receipt);self.assertIn('喜欢红石实验',receipt)
        receipt=bind.handle('Player',dict(platform='wechat',wxid='wxid_test',user='又改昵称'))
        self.assertIn('QQ：QQ昵称',receipt); self.assertIn('暂无足够印象',receipt)
        self.assertEqual(len(identity.details('qq:123')['members']),2)


if __name__=='__main__':
    unittest.main(verbosity=2)
