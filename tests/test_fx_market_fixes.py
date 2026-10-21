"""Isolated FX calibration/feed/session regressions. No production DB or sends."""
import hashlib
import json
import math
from pathlib import Path
import random
import statistics
import tempfile
import threading
import unittest
from datetime import datetime,timezone
from unittest.mock import patch,MagicMock

from plugins import fx


def when(hour,minute=0,date="2026-10-10"):
    return datetime.fromisoformat(f"{date}T{hour:02d}:{minute:02d}:00").replace(tzinfo=fx._TZ).timestamp()

class FXFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix="fx-fixes-")
        self.patches=[patch.object(fx,"_DB_PATH",Path(self.tmp.name)/"fx.sqlite3"),patch.object(fx,"_GOLD_OPEN",None)]
        for p in self.patches:p.start()
        fx._ensure_schema()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def read(self,sql,args=()):
        conn=fx._connect()
        try:return [dict(row) for row in conn.execute(sql,args)]
        finally:conn.close()
    def sync(self,now,gold=1000,copper=150,source_time=None):
        stamp=now if source_time is None else source_time
        payloads=[{"price":gold,"updatedAt":datetime.fromtimestamp(stamp,timezone.utc).isoformat()}, {"success":True,"rates":{"JPY":copper},"timestamp":stamp}]
        with patch.object(fx,"_fetch_quote_payload",side_effect=payloads):return fx._sync_external_feeds(now)
    def session(self,now):
        with fx._transaction() as conn:
            fx._advance_market(conn,now)
            date=fx._local_datetime(now).date().isoformat()
            conn.execute("INSERT OR REPLACE INTO fx_runtime_state(key,value,updated_at) VALUES(?,?,?)",(f"open_notice:{date}","1",now))

    def test_both_feeds_sync_and_preserve_anchors_and_same_minute_open(self):
        now=when(19,20)
        self.assertTrue(self.sync(now))
        self.assertTrue(self.sync(now+20,gold=1010,copper=151.5))
        rows={r['asset']:r for r in self.read('SELECT * FROM fx_external_quotes')}
        self.assertAlmostEqual(rows['gold']['point_price'],101)
        self.assertAlmostEqual(rows['copper']['point_price'],101)
        self.assertEqual(rows['gold']['anchor_price'],1000)
        self.assertEqual(rows['copper']['anchor_price'],150)
        candles=self.read('SELECT * FROM fx_market_candles')
        self.assertEqual(len(candles),2)
        self.assertTrue(all(r['open']==100 and r['close']==101 for r in candles))
        # A stale response cannot move quotes backwards or invent candles.
        self.assertFalse(self.sync(now+80,gold=990,copper=149,source_time=now-60))
        self.assertEqual(len(self.read('SELECT * FROM fx_market_candles')),2)
        self.assertAlmostEqual(self.read("SELECT point_price FROM fx_external_quotes WHERE asset='gold'")[0]['point_price'],101)

    def test_quote_poll_is_independent_of_simulated_session(self):
        now=when(22)
        self.sync(now);self.sync(now+60,gold=1001,copper=150.15)
        rows={r['asset']:r for r in self.read('SELECT * FROM fx_external_quotes')}
        self.assertAlmostEqual(rows['copper']['point_price'],100.1)
        self.assertFalse(rows['copper']['market_open'])
        self.assertEqual(len(self.read('SELECT * FROM fx_market_candles')),4)
        self.assertFalse(fx._asset_trade_open('copper',now))

    def test_timestamp_formats_ms_iso_and_invalid_sources(self):
        now=when(19)
        self.assertEqual(fx._source_timestamp(now*1000,0),now)
        self.assertEqual(fx._source_timestamp(datetime.fromtimestamp(now,timezone.utc).isoformat(),0),now)
        with patch.object(fx,"_fetch_quote_payload",side_effect=[{'price':1000},{'rates':{'JPY':150}}]):
            self.assertFalse(fx._sync_external_feeds(now))
        self.assertEqual(self.read('SELECT * FROM fx_external_quotes'),[])
        with patch.object(fx,"_fetch_quote_payload",side_effect=[{'price':float('nan'),'timestamp':now},{'success':False,'rates':{'JPY':150},'timestamp':now}]):
            self.assertFalse(fx._sync_external_feeds(now))
        self.assertEqual(self.read('SELECT * FROM fx_external_quotes'),[])

    def test_successful_fetch_does_not_make_stale_source_ready(self):
        now=when(19)
        self.sync(now,source_time=now-600)
        with patch.object(fx.time,'time',return_value=now):
            self.assertFalse(fx._external_quote_ready('gold'))
            self.assertFalse(fx._external_quote_ready('copper'))
        self.assertFalse(fx._GOLD_OPEN)
        self.sync(now+60)
        with patch.object(fx.time,'time',return_value=now+60):
            self.assertTrue(fx._external_quote_ready('gold'))
            self.assertTrue(fx._external_quote_ready('copper'))

    def test_partial_outage_updates_other_source_and_expires_gold_flag(self):
        now=when(19)
        self.sync(now)
        with patch.object(fx,'_fetch_quote_payload',side_effect=[None,{'success':True,'rates':{'JPY':153},'timestamp':now+400}]):
            self.assertTrue(fx._sync_external_feeds(now+400))
        self.assertFalse(fx._GOLD_OPEN)
        self.assertEqual(self.read("SELECT market_open FROM fx_external_quotes WHERE asset='gold'")[0]['market_open'],0)
        self.assertAlmostEqual(self.read("SELECT point_price FROM fx_external_quotes WHERE asset='copper'")[0]['point_price'],102)

    def test_feed_get_retry_is_bounded(self):
        with patch.object(fx,'_fetch_json',side_effect=[None,{'price':1}]) as call:
            self.assertEqual(fx._fetch_quote_payload('unused'),{'price':1})
            self.assertEqual(call.call_count,2)
        with patch.object(fx,'_fetch_json',return_value=None) as call:
            self.assertIsNone(fx._fetch_quote_payload('unused'))
            self.assertEqual(call.call_count,2)

    def test_query_refresh_is_outside_db_lock_and_skips_recent_poll(self):
        now=when(19)
        with patch.object(fx,'_sync_external_feeds') as sync:
            fx._refresh_external_if_stale(now);sync.assert_called_once_with(now)
        self.sync(now)
        with patch.object(fx,'_sync_external_feeds') as sync:
            fx._refresh_external_if_stale(now+30);sync.assert_not_called()
        lock=threading.Lock();lock.acquire()
        try:
            with patch.object(fx,'_FEED_LOCK',lock),patch.object(fx,'_sync_external_feeds') as sync:
                fx._refresh_external_if_stale(now+61);sync.assert_not_called()
        finally:lock.release()

    def test_simulated_session_scale_and_event_returns_are_fx_sized(self):
        expected={'iron':.0029742839686254427*50,'emerald':.004938082635636979*50,'diamond':.005059129907990951*50}
        for asset,daily in expected.items():
            self.assertAlmostEqual(fx._ASSETS[asset]['vol']*math.sqrt(120),daily)
            totals=[]
            for day in range(350):
                rng=random.Random(day*31+len(asset))
                totals.append(sum(rng.gauss(0,fx._ASSETS[asset]['vol']) for _ in range(120)))
            self.assertLess(abs(statistics.stdev(totals)/daily-1),.12)
        for theme in ['fear_sell','long_bear','capital_inflow','black_swan']:
            for severity in ['minor','major','black_swan']:
                for asset in fx._SIM_ASSETS:
                    drift,mult=fx._event_effect(theme,asset,severity)
                    self.assertLessEqual(abs(drift),.0003*50)
                    self.assertLessEqual(mult,2.5)
        self.assertEqual(fx._event_effect('black_swan','gold'),(0.0,1.0))

    def test_existing_prices_not_reset_and_event_duration_not_replayed_for_full_session(self):
        now=when(19)
        with fx._transaction() as conn:
            for asset in fx._SIM_ASSETS:
                conn.execute('INSERT INTO fx_market_state VALUES(?,?,?,?)',(asset,fx._now_bucket(now)-1,2000,now))
            conn.execute("INSERT INTO fx_events(trade_date,event_kind,title,body,theme,severity,remaining_buckets,created_at,consumed) VALUES(?,?,?,?,?,?,?,?,?)",('2026-10-10','long_bear','t','b','long_bear','major',3,now,1))
            prices,_=fx._advance_market(conn,when(20,59))
            self.assertTrue(all(500<prices[a]<8000 for a in fx._SIM_ASSETS))
        candles=self.read('SELECT * FROM fx_market_candles')
        self.assertEqual(len(candles),72)
        for row in candles:
            self.assertLessEqual(abs(math.log(row['close']/row['open'])),fx._SIM_MINUTE_RETURN_CAP+1e-10)
            self.assertLess((row['high']/row['low']-1),.0035*50)
        self.assertEqual(self.read('SELECT remaining_buckets FROM fx_events')[0]['remaining_buckets'],0)
        self.assertEqual(self.read('SELECT elapsed_buckets,duration_buckets FROM fx_events')[0]['elapsed_buckets'],3)

    def event(self,theme,now,duration=240):
        with fx._transaction() as conn:
            conn.execute("INSERT INTO fx_events(trade_date,event_kind,title,body,theme,severity,remaining_buckets,reference_prices,created_at,consumed,duration_buckets) VALUES(?,?,?,?,?,?,?,?,?,1,?)",(fx._local_datetime(now).date().isoformat(),theme,'t','b',theme,'minor',duration,'{"iron":12,"emerald":32,"diamond":64}',now,duration))

    def test_long_event_pauses_overnight_and_elapsed_counts_minutes_not_assets(self):
        self.event('trend_follow',when(19),240)
        with fx._transaction() as conn:fx._advance_market(conn,when(20,59))
        row=self.read('SELECT remaining_buckets,elapsed_buckets FROM fx_events')[0]
        self.assertEqual(row,{'remaining_buckets':120,'elapsed_buckets':120})
        with fx._transaction() as conn:fx._advance_market(conn,when(23))
        self.assertEqual(self.read('SELECT remaining_buckets,elapsed_buckets FROM fx_events')[0],row)
        with fx._transaction() as conn:fx._advance_market(conn,when(19,date='2026-10-11'))
        self.assertEqual(self.read('SELECT remaining_buckets,elapsed_buckets FROM fx_events')[0],{'remaining_buckets':119,'elapsed_buckets':121})

    def test_future_event_does_not_change_prior_candles_and_catchup_matches_stepwise(self):
        self.event('liquidity_crunch',when(19,5),30)
        with fx._transaction() as conn:fx._advance_market(conn,when(19,8))
        catchup=self.read('SELECT * FROM fx_market_candles ORDER BY asset,bucket')
        with fx._transaction() as conn:
            conn.execute('DELETE FROM fx_market_state');conn.execute('DELETE FROM fx_market_candles')
            conn.execute('UPDATE fx_events SET remaining_buckets=30,elapsed_buckets=0')
        for minute in range(9):
            with fx._transaction() as conn:fx._advance_market(conn,when(19,minute))
        self.assertEqual(self.read('SELECT * FROM fx_market_candles ORDER BY asset,bucket'),catchup)
        self.assertEqual(self.read('SELECT elapsed_buckets FROM fx_events')[0]['elapsed_buckets'],4)

    def test_phase_effects_reverse_and_reversion_direction_depends_on_price(self):
        event={'theme':'relief_rally','severity':'minor','duration_buckets':100,'remaining_buckets':100,'elapsed_buckets':0,'reference_prices':'{"iron":12}'}
        self.assertLess(fx._event_impact(event,'iron',0,12)[0],0)
        event['elapsed_buckets']=50
        self.assertGreater(fx._event_impact(event,'iron',0,12)[0],0)
        event['theme']='mean_revert'
        self.assertGreater(fx._event_impact(event,'iron',0,6)[0],0)
        self.assertLess(fx._event_impact(event,'iron',0,24)[0],0)
        self.assertEqual(fx._event_impact(event,'gold',0,100),(0.0,1.0))

    def test_event_themes_are_diverse_and_long_lived(self):
        templates=fx._event_templates()
        themes={item[1] for item in templates}
        self.assertTrue({'mean_revert','trend_follow','liquidity_crunch','relief_rally','oscillation','quiet_liquidity'}<=themes)
        self.assertGreaterEqual(max(int(item[4]) for item in templates),240)
        with fx._transaction() as conn:
            fx._prepare_event(conn,when(19),force=True)
            row=conn.execute('SELECT duration_buckets,remaining_buckets FROM fx_events ORDER BY id DESC LIMIT 1').fetchone()
            self.assertIsNotNone(row)
            self.assertGreaterEqual(row[0],2)

    def test_close_notice_retries_until_success_and_survives_restart(self):
        self.session(when(20,55))
        with patch.object(fx,'_broadcast_text',side_effect=[False,True]) as send:
            self.assertFalse(fx._flush_closing_notice(when(21)))
            self.assertTrue(fx._flush_closing_notice(when(21,1)))
            self.assertIn('今日收盘',send.call_args.args[0])
            self.assertIn('仍可平仓',send.call_args.args[0])
        with patch.object(fx,'_broadcast_text') as send:
            self.assertFalse(fx._flush_closing_notice(when(22)));send.assert_not_called()
        states=self.read('SELECT * FROM fx_market_state')
        self.assertTrue(all(row['last_bucket']==fx._now_bucket(when(21))-1 for row in states))
        old=[row['price'] for row in states]
        with fx._transaction() as conn:fx._advance_market(conn,when(22))
        self.assertEqual([row['price'] for row in self.read('SELECT * FROM fx_market_state')],old)

    def test_no_close_message_for_unobserved_session(self):
        self.assertIsNone(fx._closing_notice(when(21)))
        self.assertIsNone(fx._closing_notice(when(20,59)))

    def test_worker_exception_does_not_kill_following_ticks(self):
        stop=MagicMock();stop.is_set.side_effect=[False,False,True]
        with patch.object(fx,'_STOP',stop),patch.object(fx,'_session_tick',side_effect=[ValueError('injected'),None]) as tick:
            fx._session_worker();self.assertEqual(tick.call_count,2)
        stop=MagicMock();stop.is_set.side_effect=[False,False,True]
        with patch.object(fx,'_STOP',stop),patch.object(fx,'_sync_external_feeds',side_effect=[ValueError('injected'),False]) as sync,patch.object(fx,'_broadcast_text'):
            fx._feed_worker();self.assertEqual(sync.call_count,2)

if __name__=='__main__':unittest.main()
