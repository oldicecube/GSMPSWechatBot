"""Real Router -> Dispatcher -> binding transaction on both transports, no sends."""
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
INSTANCE=Path(tempfile.mkdtemp(prefix='bind-entry-',dir=ROOT/'temp'))
os.environ['MAIBOT_INSTANCE_DIR']=str(INSTANCE)
(INSTANCE/'config').mkdir()
for name in ('bot_config.toml','model_config.toml'):shutil.copy2(ROOT/'config'/name,INSTANCE/'config'/name)
for folder in ('plugins','auto'):
    (INSTANCE/folder).mkdir()
    for source in (ROOT/folder).glob('*.py'):shutil.copy2(source,INSTANCE/folder/source.name)
from utils import sqlite_store as store,account_identity as identity
from core.dispatcher import Dispatcher
from core.router import Router
from plugins import bind


class BindingEntry(unittest.TestCase):
    def setUp(self):
        store.DB_PATH=str(INSTANCE/(self._testMethodName+'.sqlite3'))
        store.player_stats_ensure('Player')
        self.wx=dict(platform='wechat',wxid='wxid_fixture',user='微信改名',group='fixture',sessionId='fixture@chatroom')
        self.qq=dict(platform='qq',user_id='234567',wxid='qq:234567',user='QQ改名',group='qq:123456:scope:group:345678',
            sessionId='qq:123456:scope:group:345678',account_id='123456',scope='scope',group_id='345678')
        self.dispatcher=Dispatcher();self.dispatcher.debug=False;self.dispatcher.inited=True
        self.dispatcher.config={'group_features':{'fixture':{'plugins_enabled':True,'auto_plugins_enabled':True,'llm_enabled':True}}}
        self.dispatcher.plugins={'/bind':bind};self.dispatcher.modules={'bind':bind}
        self.dispatcher.llm_service=NS(native_ingress=True,ingest_context=Mock(),record_plugin_delivery=Mock())

    def invoke(self, ctx, text='/bind Player'):
        message={**ctx,'content':text,'serverId':'bind-entry-fixture'}
        event=Router([], [ctx['group']],prefix_mode='mixed').parse(message)
        self.assertIsNotNone(event)
        result=self.dispatcher.dispatch(event['command'],event['args'],{**message,**event,'raw':message})
        return '\n'.join(result.get('messages',[])) if isinstance(result,dict) else result

    def test_wechat_then_qq_actual_entry(self):
        self.assertIn('绑定成功',self.invoke(self.wx))
        self.assertIn('绑定成功',self.invoke(self.qq))
        self.assertEqual(len(identity.details('qq:234567')['members']),2)
        self.assertIn('wechat',self.invoke(self.qq,'/bind status'))

    def test_qq_then_wechat_actual_entry(self):
        self.assertIn('绑定成功',self.invoke(self.qq))
        receipt=self.invoke(self.wx)
        self.assertIn('绑定成功',receipt);self.assertIn('QQ：QQ改名',receipt)
        self.assertEqual(identity.canonical_user('wxid_fixture'),'qq:234567')

    def test_optional_impression_error_cannot_misreport_committed_binding(self):
        with patch.object(identity,'binding_peer_note',side_effect=RuntimeError('synthetic profile unavailable')):
            receipt=self.invoke(self.qq)
        self.assertIn('绑定成功',receipt)
        self.assertIsNotNone(identity.details('qq:234567'))
        self.assertIn('绑定成功',self.invoke(self.qq))

    def test_old_cached_module_without_optional_helper_still_binds(self):
        with patch.object(identity,'binding_peer_note',None):
            self.assertIn('绑定成功',self.invoke(self.wx))

    def test_database_exception_is_actionable_and_no_false_success(self):
        with patch.object(identity,'bind_game',side_effect=sqlite3.OperationalError('synthetic lock')):
            receipt=self.invoke(self.wx)
        self.assertIn('绑定存储暂不可用',receipt)
        self.assertIsNone(identity.details('wxid_fixture'))

    def test_missing_user_and_invalid_name_fail_without_generic_exception(self):
        self.assertIn('不存在',self.invoke(self.qq,'/bind NoSuchPlayer'))
        self.assertIn('无法识别',self.invoke({**self.qq,'user_id':'','wxid':''}))

    def test_current_production_database_schema_and_legacy_owner_upgrade(self):
        copy=INSTANCE/'production-copy.sqlite3'
        with sqlite3.connect((ROOT/'data/bot.sqlite3').resolve().as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(copy) as target:
            source.backup(target)
        store.DB_PATH=str(copy)
        # Real bindings may already include both platforms; they must reject
        # a third account. Seed a legacy owner only in this isolated copy.
        player,uid='LegacyBindingAcceptance','wxid_legacy_binding_acceptance'
        store.player_stats_ensure(player)
        with sqlite3.connect(copy) as database:
            database.execute('UPDATE player_stats SET bind_user=? WHERE player_name=?',(uid,player))
        self.assertIn('绑定成功',self.invoke({**self.wx,'wxid':uid},'/bind '+player))
        self.assertIn('绑定成功',self.invoke({**self.qq,'user_id':'99999000','wxid':'qq:99999000'},'/bind '+player))
        self.assertEqual(identity.canonical_user('qq:99999000'),uid)

    def test_existing_dual_binding_never_accepts_third_account(self):
        self.assertIn('绑定成功',self.invoke(self.wx))
        self.assertIn('绑定成功',self.invoke(self.qq))
        receipt=self.invoke({**self.qq,'user_id':'99999000','wxid':'qq:99999000'})
        self.assertIn('已经绑定了本平台的其他账号',receipt)
        self.assertIsNone(identity.details('qq:99999000'))
        self.assertEqual(len(identity.details('wxid_fixture')['members']),2)


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(BindingEntry))
    from src.common.database.database import engine
    engine.dispose()
    report=dict(passed=result.wasSuccessful(),tests=result.testsRun,instance=str(INSTANCE),physical_messages=False,
        failures=[(str(t),e) for t,e in result.failures+result.errors])
    (ROOT/'docs/maibot-wechat-acceptance/bind-entry-20261031.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    raise SystemExit(not result.wasSuccessful())
