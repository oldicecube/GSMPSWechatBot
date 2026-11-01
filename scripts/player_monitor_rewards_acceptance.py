"""Real isolated SQLite reward and monitoring tests; no real credits or sends."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils import sqlite_store as store, player_rewards as rewards, account_identity
from auto import player_monitor as monitor
from services import mc_api


def timestamp(value='2026-11-01T12:00:00'):
    return datetime.fromisoformat(value).replace(tzinfo=rewards.ZONE).timestamp()


class RewardAcceptance(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='mc-rewards-')
        data = Path(self.tmp.name) / 'data'
        for field, value in [('BASE_DIR', self.tmp.name), ('DATA_DIR', str(data)),
                             ('DB_PATH', str(data / 'bot.sqlite3'))]:
            patcher = patch.object(store, field, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        # Legacy sqlite_store helpers use connection context managers which
        # commit but do not close on Python 3.13. Close test-owned handles
        # explicitly before removing the isolated DB on Windows.
        self.connections = []
        owner_thread = threading.get_ident()
        original_connect = store._connect
        def connect():
            conn = original_connect()
            if threading.get_ident() == owner_thread:
                self.connections.append(conn)
            return conn
        patcher = patch.object(store, '_connect', side_effect=connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(lambda: [conn.close() for conn in self.connections])
        self.now = timestamp()
        self.seed('Ice', 'wxid_ice')
        monitor.login_times.clear()

    def seed(self, name, owner=None, **kwargs):
        with store._LOCK, closing(store._connect()) as db, db:
            db.execute('INSERT INTO player_stats(player_name,bind_user) VALUES(?,?)', (name, owner))
            for key, value in kwargs.items():
                if key not in store.PLAYER_STATS_COLUMNS:
                    raise ValueError(key)
                db.execute(f'UPDATE player_stats SET {key}=? WHERE player_name=?', (value, name))

    def balance(self, owner='wxid_ice'):
        with closing(store._connect()) as db:
            row = db.execute('SELECT points FROM user_points WHERE wxid=?', (owner,)).fetchone()
        return row[0] if row else 0

    def observe(self, server, names, at, **kwargs):
        return rewards.observe(server, names, at, max_gap_seconds=7200, **kwargs)

    def test_event_first_login_blocks_primary_reward(self):
        self.assertEqual(self.observe('event', ['Ice'], self.now)['Ice']['login_points'], 20)
        self.assertEqual(self.observe('main', ['Ice'], self.now + 1)['Ice']['login_points'], 0)
        self.assertEqual(self.balance(), 20)

    def test_primary_first_login_blocks_event_reward(self):
        self.observe('main', ['Ice'], self.now)
        self.observe('event', ['Ice'], self.now + 1)
        self.assertEqual(self.balance(), 5)

    def test_requested_event_four_then_main_six_then_event_forty(self):
        self.observe('event', ['Ice'], self.now)
        self.observe('event', [], self.now + 120)
        self.assertEqual(store.player_stats_get('Ice')['online_time_points_today'], 4)
        self.observe('main', ['Ice'], self.now + 120)
        self.observe('main', [], self.now + 720)
        self.assertEqual(store.player_stats_get('Ice')['online_time_points_today'], 10)
        self.observe('event', ['Ice'], self.now + 720)
        self.observe('event', [], self.now + 1920)
        self.assertEqual(store.player_stats_get('Ice')['online_time_points_today'], 50)
        self.assertEqual(self.balance(), 70)
        self.assertEqual(store.player_stats_get('Ice')['total_time'], 600)

    def test_event_above_ten_primary_does_not_deduct_or_credit(self):
        self.observe('event', ['Ice'], self.now)
        self.observe('event', [], self.now + 600)
        self.observe('main', ['Ice'], self.now + 600)
        self.observe('main', [], self.now + 1200)
        self.assertEqual(store.player_stats_get('Ice')['online_time_points_today'], 20)
        self.assertEqual(self.balance(), 40)

    def test_partial_last_minute_only_adds_one_to_fifty(self):
        with closing(store._connect()) as db, db:
            db.execute("UPDATE player_stats SET last_login_date='2026-11-01',login_points_today=5,online_time_points_today=9")
        self.observe('event', ['Ice'], self.now)
        self.observe('event', [], self.now + 1200)
        self.assertEqual(self.balance(), 40)
        self.observe('event', ['Ice'], self.now + 1200)
        self.observe('event', [], self.now + 1260)
        self.assertEqual(self.balance(), 41)
        self.assertEqual(store.player_stats_get('Ice')['online_time_points_today'], 50)

    def test_repeated_snapshot_and_rejoin_cannot_duplicate_rewards(self):
        self.observe('event', ['Ice'], self.now)
        self.observe('event', ['Ice'], self.now + 120)
        self.observe('event', ['Ice'], self.now + 120)
        self.observe('event', [], self.now + 120)
        self.observe('event', ['Ice'], self.now + 120)
        self.assertEqual(self.balance(), 24)

    def test_complete_minutes_across_short_sessions_no_one_minute_farming(self):
        self.observe('event', ['Ice'], self.now)
        self.observe('event', [], self.now + 30)
        self.assertEqual(self.balance(), 20)
        self.observe('event', ['Ice'], self.now + 40)
        self.observe('event', [], self.now + 70)
        self.assertEqual(self.balance(), 22)

    def test_legacy_today_reward_is_adopted_without_reset(self):
        with closing(store._connect()) as db, db:
            db.execute("UPDATE player_stats SET last_login_date='2026-11-01',login_points_today=5,online_time_points_today=8")
        self.observe('event', ['Ice'], self.now)
        self.observe('event', [], self.now + 60)
        self.assertEqual(self.balance(), 2)
        self.assertEqual(store.player_stats_get('Ice')['online_time_points_today'], 10)

    def test_midnight_splits_activity_and_does_not_double_login(self):
        start = timestamp('2026-11-01T23:59:30')
        self.observe('event', ['Ice'], start)
        self.observe('event', ['Ice'], start + 90)
        self.assertEqual(self.balance(), 22)
        self.assertEqual(store.player_stats_get('Ice')['online_time_points_today'], 2)
        self.observe('event', [], start + 90)
        self.observe('main', ['Ice'], start + 91)
        self.assertEqual(self.balance(), 27)
        self.observe('event', ['Ice'], start + 92)
        self.assertEqual(self.balance(), 27)

    def test_restart_gap_no_offline_credit_and_preserves_partial_minute(self):
        self.observe('event', ['Ice'], self.now)
        self.observe('event', ['Ice'], self.now + 30)
        self.observe('event', ['Ice'], self.now + 3600, baseline=True)
        self.observe('event', ['Ice'], self.now + 3630)
        self.assertEqual(self.balance(), 22)

    def test_query_outage_gap_no_unobserved_credit(self):
        rewards.observe('event', ['Ice'], self.now)
        rewards.observe('event', ['Ice'], self.now + 3600, max_gap_seconds=60)
        self.assertEqual(self.balance(), 20)

    def test_unbound_users_do_not_receive_points_or_create_primary_players(self):
        self.seed('Guest')
        self.observe('event', ['Guest', 'OnlyEvent'], self.now)
        self.observe('event', [], self.now + 120)
        with closing(store._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM user_points').fetchone()[0], 0)
        self.assertIsNone(store.player_stats_get('OnlyEvent'))

    def test_linked_qq_wechat_share_one_balance_and_case_insensitive_player(self):
        account_identity.bind_game('wxid_ice', 'Ice')
        account_identity.bind_game('qq:123456', 'Ice')
        self.observe('event', ['ICE'], self.now)
        self.observe('event', ['ice'], self.now + 60)
        self.assertEqual(store.user_points_get('qq:123456')['points'], 22)
        self.assertEqual(store.user_points_get('wxid_ice')['points'], 22)

    def test_concurrent_server_claims_have_exactly_one_login_award(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda server: self.observe(server, ['Ice'], self.now), ['main', 'event']))
        amounts = [r['Ice']['login_points'] for r in claims]
        self.assertEqual(sum(amount > 0 for amount in amounts), 1)
        self.assertIn(self.balance(), (5, 20))

    def test_monitor_event_never_calls_tournament_or_main_time_tracking(self):
        instance = monitor.ServerMonitor('event', monitor.EVENT_HOST)
        sender = Mock()
        with patch.object(monitor.tournament_monitor, 'sync_new_matches') as sync, \
             patch.object(monitor, 'update_daily_theroom_from_matches') as matches:
            instance.accept(['Ice'], sender, now=self.now)
            instance.accept(['Ice'], sender, now=self.now + 60)
            instance.accept([], sender, now=self.now + 70)
            instance.accept([], sender, now=self.now + 80)
        sync.assert_not_called()
        matches.assert_not_called()
        self.assertEqual(store.player_stats_get('Ice')['total_time'], 0)
        self.assertIn(monitor.EVENT_HOST, sender.call_args.args[0])

    def test_primary_leave_keeps_match_rewards_and_case_canonicalization(self):
        instance = monitor.ServerMonitor('main')
        sender = Mock()
        with patch.object(monitor.tournament_monitor, 'sync_new_matches', return_value={'ok': True}) as sync, \
             patch.object(monitor, 'update_daily_theroom_from_matches',
                          return_value={'total': 1, 'reward_delta': 1}) as matches:
            instance.accept(['ICE'], sender, now=self.now)
            instance.accept(['ice'], sender, now=self.now + 60)
            instance.accept([], sender, now=self.now + 60)
            instance.accept([], sender, now=self.now + 60)
        sync.assert_called_once()
        matches.assert_called_once_with('Ice')
        self.assertEqual(self.balance(), 7)  # main login 5 + active 1 + match 1
        self.assertEqual(store.player_stats_get('Ice')['total_time'], 60)
        with closing(store._connect()) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM player_stats').fetchone()[0], 1)

    def test_failed_credit_rolls_back_eligibility_and_accounting(self):
        with patch.object(rewards, '_credit', side_effect=RuntimeError('synthetic credit failure')):
            with self.assertRaises(RuntimeError):
                self.observe('event', ['Ice'], self.now)
        self.assertEqual(self.balance(), 0)
        self.observe('event', ['Ice'], self.now)
        self.assertEqual(self.balance(), 20)

    def test_simultaneous_activity_settlement_obeys_shared_fifty_cap(self):
        self.observe('event', ['Ice'], self.now)
        self.observe('main', ['Ice'], self.now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda server: self.observe(server, ['Ice'], self.now + 3600), ['main', 'event']))
        self.assertEqual(store.player_stats_get('Ice')['online_time_points_today'], 50)
        self.assertEqual(self.balance(), 70)

    def test_failed_poll_is_not_leave_and_primary_still_independent(self):
        event = monitor.ServerMonitor('event', monitor.EVENT_HOST)
        primary = monitor.ServerMonitor('main')
        sender = Mock()
        event.accept(['Ice'], sender, now=self.now)
        with patch.object(mc_api, 'player_snapshot', return_value=None):
            event.poll(sender, now=self.now + 3600)
        sender.assert_not_called()
        primary.accept(['Ice'], sender, now=self.now + 3600)
        event.accept(['Ice'], sender, now=self.now + 3600)
        self.assertEqual(self.balance(), 20)

    def test_snapshot_uses_separate_host_and_rejects_incomplete_samples(self):
        initial_host, initial_port = mc_api._game_host, mc_api._game_port
        server = NS(status=Mock(return_value=NS(players=NS(online=1, sample=[NS(name='Ice')]))))
        with patch.object(mc_api.JavaServer, 'lookup', return_value=server) as lookup:
            self.assertEqual(mc_api.player_snapshot(host=monitor.EVENT_HOST), ['Ice'])
            lookup.assert_called_once_with(monitor.EVENT_HOST, timeout=3)
            server.status.return_value.players.online = 2
            self.assertIsNone(mc_api.player_snapshot(host=monitor.EVENT_HOST))
            server.status.side_effect = TimeoutError('synthetic')
            self.assertIsNone(mc_api.player_snapshot(host=monitor.EVENT_HOST))
        self.assertEqual((mc_api._game_host, mc_api._game_port), (initial_host, initial_port))


if __name__ == '__main__':
    unittest.main(verbosity=2)
