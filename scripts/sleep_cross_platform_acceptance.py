"""Shared sleep state, any enabled group/media event, atomic settlement; no live sends."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
INSTANCE=Path(tempfile.mkdtemp(prefix='sleep-dual-',dir=ROOT/'temp'))
os.environ['MAIBOT_INSTANCE_DIR']=str(INSTANCE)
(INSTANCE/'config').mkdir()
for file in ('bot_config.toml','model_config.toml'):shutil.copy2(ROOT/'config'/file,INSTANCE/'config'/file)
for folder in ('auto','plugins'):
    (INSTANCE/folder).mkdir()
    for source in (ROOT/folder).glob('*.py'):shutil.copy2(source,INSTANCE/folder/source.name)
from auto import sleep
from utils import sqlite_store as store,account_identity as identity,points_manager as points
from core.router import Router
from core.dispatcher import Dispatcher
from core.auto_registry import register_raw_message_target


class SleepAcrossGroups(unittest.TestCase):
    def setUp(self):
        store.DB_PATH=str(INSTANCE/(self._testMethodName+'.sqlite3'))
        store.player_stats_ensure('Player');identity.bind_game('wxid_owner','Player');identity.bind_game('qq:123','Player')
        points.set_points('wxid_owner',100)
        self.wx=dict(platform='wechat',wxid='wxid_owner',user_id='wxid_owner',user='微信新昵称',group='另一个群',sessionId='other@chatroom',type='group',content='普通发言')
        self.qq=dict(platform='qq',user_id='123',user='QQ新昵称',account_id='789',scope='scope',group='qq:789:scope:group:456',sessionId='qq:789:scope:group:456',type='group',content='普通发言')
        self.now=datetime.now().replace(hour=22,minute=0,second=0,microsecond=0)

    def start(self,ctx):
        with patch.object(sleep,'datetime') as clock:
            clock.now.return_value=self.now
            clock.fromisoformat.side_effect=datetime.fromisoformat
            return sleep.handle_auto({**ctx,'content':'/sleep'})

    def test_qq_sleep_wechat_any_group_message_invalidates_once(self):
        self.assertIn('已打卡',self.start(self.qq))
        self.assertIsNotNone(store.sleep_get_active('wxid_owner'))
        active=store.sleep_get_active('wxid_owner')
        with patch.object(sleep,'datetime') as clock:
            clock.now.return_value=self.now+timedelta(minutes=10);clock.fromisoformat.side_effect=datetime.fromisoformat
            response=sleep.handle_auto(self.wx)
        self.assertIn('扣除',response)
        self.assertEqual(points.get_points('qq:123'),90)
        self.assertIsNone(store.sleep_get_active('qq:123'))
        self.assertEqual(store.sleep_get_by_id(active['id'])['group_id'],self.qq['sessionId'])

    def test_wechat_sleep_qq_media_only_message_completes(self):
        self.start(self.wx)
        with patch.object(sleep,'datetime') as clock:
            clock.now.return_value=self.now+timedelta(hours=8);clock.fromisoformat.side_effect=datetime.fromisoformat
            response=sleep.handle_auto({**self.qq,'content':'','localType':3,'raw':{'message_type':'image'}})
        self.assertIn('获得',response)
        self.assertGreater(points.get_points('wxid_owner'),100)
        self.assertEqual(store.sleep_recent('qq:123')[0]['status'],'completed')

    def test_concurrent_cross_adapter_wakeup_never_double_penalizes(self):
        self.start(self.qq);active=store.sleep_get_active('wxid_owner')
        with ThreadPoolExecutor(max_workers=8) as workers:
            results=list(workers.map(lambda _:sleep._invalidate(active,self.now+timedelta(minutes=2)),range(12)))
        self.assertEqual(sum(bool(x) for x in results),1)
        self.assertEqual(points.get_points('qq:123'),90)

    def test_concurrent_cross_adapter_wakeup_never_double_rewards(self):
        self.start(self.wx);active=store.sleep_get_active('qq:123')
        with ThreadPoolExecutor(max_workers=8) as workers:
            results=list(workers.map(lambda _:sleep._complete(active,self.now+timedelta(hours=8)),range(12)))
        self.assertEqual(sum(bool(x) for x in results),1)
        reward=store.sleep_get_by_id(active['id'])['points_awarded']
        self.assertEqual(points.get_points('wxid_owner'),100+reward)

    def test_concurrent_checkin_has_one_record(self):
        period=sleep._sleep_period_start(self.now)
        with ThreadPoolExecutor(max_workers=8) as workers:
            ids=list(workers.map(lambda n:store.sleep_start_once('qq:123' if n%2 else 'wxid_owner','same','group',
                self.now.isoformat(),period.isoformat(),(period+timedelta(hours=12)).isoformat()),range(12)))
        self.assertEqual(sum(bool(x) for x in ids),1)

    def test_bot_notice_unbound_or_different_user_cannot_touch_state(self):
        self.start(self.wx)
        for extra in ({'is_bot':True},{'is_notify':True},{'source_kind':'system_event'},{'wxid':'wxid_other','user_id':'wxid_other'}):
            self.assertIsNone(sleep.handle_auto({**self.wx,**extra}))
        self.assertIsNotNone(store.sleep_get_active('qq:123'))
        self.assertEqual(points.get_points('wxid_owner'),100)

    def test_enabled_group_router_observes_media_without_prefix_but_disabled_group_ignores(self):
        register_raw_message_target('sleep')
        router=Router(['@bot'],['other@chatroom'],prefix_mode='strict')
        msg={**self.wx,'group':'改过名的群','content':''}
        event=router.parse(msg)
        self.assertIsNotNone(event);self.assertEqual(event['type'],'raw_auto')
        self.assertTrue(router.inspect_message(msg)['is_target_group'])
        dispatcher=Dispatcher();dispatcher.debug=False
        dispatcher.config={'group_features':{'other@chatroom':{'plugins_enabled':False,'auto_plugins_enabled':True,'llm_enabled':False,'plugin_allowlist':['sleep']}}}
        dispatcher.modules={'auto:sleep':sleep};dispatcher.auto_modules={'sleep':sleep};dispatcher.inited=True
        self.start(self.qq)
        with patch.object(sleep,'datetime') as clock:
            clock.now.return_value=self.now+timedelta(minutes=10);clock.fromisoformat.side_effect=datetime.fromisoformat
            response=dispatcher.dispatch(None,None,{**event,'raw':msg})
        self.assertIn('扣除',json.dumps(response,ensure_ascii=False))
        self.assertIsNone(store.sleep_get_active('qq:123'))
        self.assertIsNone(router.parse({**msg,'sessionId':'disabled@chatroom'}))
        self.assertFalse(dispatcher._module_enabled_for_group('auto:repeat_echo',msg['group'],event))

    def test_disabled_sleep_plugin_does_not_enforce(self):
        from core.maibot_shell_registry import set_enabled
        dispatcher=Dispatcher();dispatcher.debug=False;dispatcher.config={'group_features':{'default':{'auto_plugins_enabled':True}}}
        dispatcher.modules={'auto:sleep':sleep};dispatcher.auto_modules={'sleep':sleep}
        self.start(self.qq)
        set_enabled('gsmps.automation.sleep',False)
        try:
            self.assertIsNone(dispatcher.dispatch(None,None,self.wx))
            self.assertIsNotNone(store.sleep_get_active('qq:123'))
        finally:set_enabled('gsmps.automation.sleep',True)


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SleepAcrossGroups))
    from src.common.database.database import engine
    engine.dispose()
    report=dict(passed=result.wasSuccessful(),tests=result.testsRun,instance=str(INSTANCE),physical_messages=False,
        failures=[(str(t),e) for t,e in result.failures+result.errors])
    (ROOT/'docs/maibot-wechat-acceptance/sleep-cross-platform-20261030.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    raise SystemExit(not result.wasSuccessful())
