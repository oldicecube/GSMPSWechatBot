"""Isolated binding, wallet and business record acceptance; no production edits."""
import importlib
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
INSTANCE=tempfile.TemporaryDirectory(prefix='account-identity-',dir=ROOT/'temp')
os.environ['MAIBOT_INSTANCE_DIR']=INSTANCE.name
from utils import sqlite_store as store
from utils import account_identity as identity
from utils import points_manager as points


class Accounts(unittest.TestCase):
    def setUp(self):
        store.DB_PATH=str(Path(INSTANCE.name)/('wallet-'+self._testMethodName+'.sqlite3'))
        store.player_stats_ensure('OldIceCube')
        self.wx={'platform':'wechat','wxid':'wxid_owner','user':'oldicecube','type':'group'}
        self.qq={'platform':'qq','wxid':'qq:123456','user_id':'123456','user':'oldicecube','type':'group'}

    def bind_both(self):
        identity.bind_game('wxid_owner','OldIceCube')
        return identity.bind_game('qq:123456','OldIceCube')

    def test_unbound_cannot_earn_spend_set_or_sign(self):
        self.assertEqual(points.add_points('wxid_owner',10),0)
        self.assertEqual(points.set_points('wxid_owner',100),0)
        self.assertEqual(points.try_spend_points('wxid_owner',1),(False,0))
        self.assertFalse(points.claim_sign('wxid_owner',10)[0])
        self.assertIsNone(store.user_points_get('wxid_owner'))

    def test_points_plugin_sign_and_lottery_explain_binding(self):
        from plugins import sign,points as plugin_points,lottery
        for module in (sign,plugin_points,lottery):
            self.assertIn('/bind',module.handle('',self.wx))

    def test_username_only_binding_shares_wallet(self):
        self.bind_both()
        points.add_points('wxid_owner',10)
        self.assertEqual(points.get_points('qq:123456'),10)
        self.assertEqual(points.try_spend_points('qq:123456',3),(True,7))
        self.assertEqual(points.get_points('wxid_owner'),7)
        self.assertEqual(points.get_all_wxids(),['wxid_owner'])

    def test_game_lookup_accepts_case_but_keeps_record_name(self):
        self.assertEqual(identity.bind_game('wxid_owner','oldicecube')['player'],'OldIceCube')

    def test_existing_legacy_owner_is_preserved(self):
        store.player_stats_update('OldIceCube',bind_user='wxid_owner')
        points.add_points('wxid_owner',17)
        self.assertEqual(identity.bind_game('qq:123456','OldIceCube')['canonical'],'wxid_owner')
        self.assertEqual(points.get_points('qq:123456'),17)

    def test_qq_first_then_wechat(self):
        identity.bind_game('qq:123456','OldIceCube')
        self.assertEqual(identity.bind_game('wxid_owner','OldIceCube')['canonical'],'qq:123456')
        points.add_points('wxid_owner',9)
        self.assertEqual(points.get_points('qq:123456'),9)

    def test_no_same_platform_takeover(self):
        self.bind_both()
        for uid in ('wxid_other','qq:654321'):
            with self.assertRaises(ValueError):identity.bind_game(uid,'OldIceCube')

    def test_no_other_game_account_merge(self):
        self.bind_both();store.player_stats_ensure('Another')
        with self.assertRaises(ValueError):identity.bind_game('qq:123456','Another')

    def test_missing_player_cannot_bind(self):
        with self.assertRaises(ValueError):identity.bind_game('wxid_owner','Missing')

    def test_merge_legacy_balance_and_daily_sign_once(self):
        identity.bind_game('wxid_owner','OldIceCube');points.add_points('wxid_owner',7)
        with store._connect() as conn:
            conn.execute("INSERT INTO user_points VALUES('qq:123456',11,'old','old','2026-10-30T12:00:00')")
        identity.bind_game('qq:123456','OldIceCube')
        self.assertEqual(points.get_points('wxid_owner'),18)
        self.assertEqual(str(points.get_last_sign_at('wxid_owner')),'2026-10-30')
        identity.bind_game('qq:123456','OldIceCube')
        self.assertEqual(points.get_points('qq:123456'),18)

    def test_concurrent_sign_one_reward_across_platforms(self):
        self.bind_both()
        with ThreadPoolExecutor(max_workers=8) as workers:
            results=list(workers.map(lambda uid:points.claim_sign(uid,5),['wxid_owner','qq:123456']*8))
        self.assertEqual(sum(success for success,_ in results),1)
        self.assertEqual(points.get_points('wxid_owner'),5)

    def test_concurrent_spending_never_overdraws(self):
        self.bind_both();points.add_points('wxid_owner',10)
        with ThreadPoolExecutor(max_workers=8) as workers:
            results=list(workers.map(lambda uid:points.try_spend_points(uid,3),['wxid_owner','qq:123456']*8))
        self.assertEqual(sum(success for success,_ in results),3)
        self.assertEqual(points.get_points('wxid_owner'),1)

    def test_sleep_and_lottery_records_shared_and_source_kept(self):
        identity.bind_game('wxid_owner','OldIceCube')
        record=store.sleep_start('qq:123456','qq name','qq:bot:conn:group:345678')
        identity.bind_game('qq:123456','OldIceCube')
        self.assertEqual(store.sleep_get_active('wxid_owner')['id'],record)
        self.assertEqual(store.sleep_recent('qq:123456')[0]['group_id'],'qq:bot:conn:group:345678')
        store.lottery_add_bet('2026-10-30','qq:123456','qq name','1',1)
        self.assertEqual(len(store.lottery_user_bets('2026-10-30','wxid_owner')),1)
        self.assertEqual(len(store.lottery_history('qq:123456')),1)

    def test_native_memory_aliases_unified_without_changing_transport(self):
        info=self.bind_both();aliases=identity.memory_aliases('qq','123456')
        self.assertEqual({r[0] for r in aliases},{'wechat','qq'})
        ids=identity.linked_person_ids(aliases[0][2])
        self.assertEqual(set(ids),{r[2] for r in aliases})
        self.assertEqual(identity.actor(self.qq),'qq:123456')
        self.assertIn('当前平台会话仍独立',identity.identity_hint('qq','123456'))
        self.assertNotEqual(aliases[0][2],aliases[1][2])

    def test_legacy_double_sleep_cannot_receive_duplicate_rewards_after_link(self):
        identity.bind_game('wxid_owner','OldIceCube')
        store.sleep_start('wxid_owner','wx name','wx-group')
        store.sleep_start('qq:123456','qq name','qq-group')
        identity.bind_game('qq:123456','OldIceCube')
        with store._connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM sleep_records WHERE wxid='wxid_owner' AND status='active'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM sleep_records WHERE invalid_reason='linked_duplicate_sleep'").fetchone()[0],1)

    def test_binding_plugin_uses_username_and_rejects_agent(self):
        from plugins import bind
        self.assertIn('绑定成功',bind.handle('OldIceCube',self.wx))
        self.assertIn('绑定成功',bind.handle('OldIceCube',self.qq))
        self.assertIn('qq',bind.handle('status',self.wx))
        self.assertIn('本人',bind.handle('OldIceCube',{**self.qq,'_agent_invocation':True}))

    def test_player_records_resolve_to_same_game(self):
        self.bind_both()
        from plugins.player import find_bound_player
        self.assertEqual(find_bound_player(identity.canonical_user('qq:123456'),store.player_stats_all()),'OldIceCube')


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Accounts))
    import gc
    gc.collect()
    INSTANCE.cleanup()
    raise SystemExit(not result.wasSuccessful())
