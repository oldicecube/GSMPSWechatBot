# -*- coding: utf-8 -*-
"""Lightweight, delayed, group-friendly simulated commodity trading."""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import sqlite3
import requests
import threading
import shutil
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from core.sender import send
from PIL import Image, ImageDraw, ImageFont

COMMAND = "/fx"
_LOG = logging.getLogger("fx_plugin")
_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "bot.sqlite3"
_LOCK = threading.RLock()
_WAKE = threading.Event()
_THREAD = None
_SESSION_THREAD = None
_FEED_THREAD = None
_FEED_LOCK = threading.Lock()
_STOP = threading.Event()
_BROADCAST = None
_LLM_SERVICE = None
_TZ = ZoneInfo("Asia/Shanghai")
_NY_TZ = ZoneInfo("America/New_York")
_GOLD_OPEN = None

# Reference FX calibration retained, but requested simulation risk is 50x.
# Scaling standard deviation by 50 scales variance by 2500; real quotes do not scale.
_SIM_VOLATILITY_MULTIPLIER = 50.0
_ASSETS = {
    "iron": {"name": "\u94c1\u952d", "base": 12.0, "vol": 0.0029742839686254427 / math.sqrt(120) * _SIM_VOLATILITY_MULTIPLIER, "kind": "sim"},
    "emerald": {"name": "\u7eff\u5b9d\u77f3", "base": 32.0, "vol": 0.004938082635636979 / math.sqrt(120) * _SIM_VOLATILITY_MULTIPLIER, "kind": "sim"},
    "diamond": {"name": "\u94bb\u77f3", "base": 64.0, "vol": 0.005059129907990951 / math.sqrt(120) * _SIM_VOLATILITY_MULTIPLIER, "kind": "sim"},
    # Both real-market lines use persistent first-observation anchors and
    # continuously mirror the external percentage move in points.
    "gold": {"name": "\u91d1\u952d", "base": 100.0, "vol": 0.0, "kind": "xau_usd", "no_leverage": True, "no_liquidation": True},
    "copper": {"name": "\u94dc\u952d", "base": 100.0, "vol": 0.0, "kind": "usd_jpy"},
}
_SIM_ASSETS = ("iron", "emerald", "diamond")
_MARKET_OPEN_MINUTE = 19 * 60
_MARKET_CLOSE_MINUTE = 21 * 60
_EVENT_CHECK_MINUTES = 5
_EVENT_MIN_GAP_SECONDS = 300
_EVENT_DAILY_LIMIT = 6
_MAJOR_EVENT_COOLDOWN_DAYS = 14
_FEED_POLL_SECONDS = 60
_FEED_MAX_AGE_SECONDS = 300
_SIM_MINUTE_RETURN_CAP = 0.002 * _SIM_VOLATILITY_MULTIPLIER
_SIM_EVENT_DRIFT_CAP = 0.00012 * _SIM_VOLATILITY_MULTIPLIER
_GOLD_FEED_URL = "https://api.gold-api.com/price/XAU"
_COPPER_FEED_URL = "https://api.fxratesapi.com/latest?base=USD&currencies=JPY&resolution=1m&places=6&format=json"
_ASSET_ALIASES = {
    "iron": "iron", "iron_ingot": "iron", "\u94c1\u952d": "iron",
    "emerald": "emerald", "\u7eff\u5b9d\u77f3": "emerald",
    "diamond": "diamond", "\u94bb\u77f3": "diamond",
    "gold": "gold", "xau": "gold", "xau/usd": "gold", "\u91d1\u952d": "gold",
    "copper": "copper", "usd/jpy": "copper", "usdjpy": "copper", "\u94dc\u952d": "copper",
}
# Quotes and candles advance once per minute. Keep only the recent history
# needed for the compact chat snapshot and prompt injection.
_CANDLE_SECONDS = 60
_CANDLE_RETENTION = 24
_MAX_VISIBLE_CANDLES = 6
_MAX_MARGIN = float("inf")
_MAX_LEVERAGE = 20
_LIQUIDATION_LOSS_RATIO = 0.80
_CHART_DIR = Path(__file__).resolve().parents[1] / "temp" / "fx"
_CHART_WIDTH = 1400
_CHART_PANEL_HEIGHT = 380


def _connect():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ensure_schema():
    with _LOCK:
        conn = _connect()
        try:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS fx_market_state (
                asset TEXT PRIMARY KEY,
                last_bucket INTEGER NOT NULL,
                price REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fx_market_candles (
                asset TEXT NOT NULL,
                bucket INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                PRIMARY KEY(asset, bucket)
            );
            CREATE INDEX IF NOT EXISTS ix_fx_candles_bucket
                ON fx_market_candles(asset, bucket DESC);
            CREATE TABLE IF NOT EXISTS fx_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wxid TEXT NOT NULL,
                group_id TEXT NOT NULL DEFAULT '',
                asset TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('long','short')),
                margin REAL NOT NULL,
                leverage INTEGER NOT NULL,
                entry_price REAL NOT NULL,
                units REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                opened_at REAL NOT NULL,
                closed_at REAL,
                exit_price REAL,
                realized_pnl REAL,
                returned_margin REAL
            );
            CREATE INDEX IF NOT EXISTS ix_fx_positions_user_status
                ON fx_positions(wxid, status, id DESC);
            CREATE TABLE IF NOT EXISTS fx_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                wxid TEXT NOT NULL,
                group_id TEXT NOT NULL DEFAULT '',
                action TEXT NOT NULL,
                payload TEXT NOT NULL,
                due_at REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                last_error TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS ix_fx_jobs_pending
                ON fx_jobs(status, due_at, id);
            CREATE TABLE IF NOT EXISTS fx_runtime_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fx_external_quotes (
                asset TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                source_price REAL NOT NULL,
                point_price REAL NOT NULL,
                anchor_price REAL NOT NULL,
                source_timestamp REAL NOT NULL,
                fetched_at REAL NOT NULL,
                market_open INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS fx_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_date TEXT NOT NULL,
                event_kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                theme TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'minor',
                remaining_buckets INTEGER NOT NULL DEFAULT 0,
                reference_prices TEXT NOT NULL DEFAULT '{}',
                prepared_by TEXT NOT NULL DEFAULT 'template',
                created_at REAL NOT NULL,
                published_at REAL,
                consumed INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_fx_events_active
                ON fx_events(consumed, remaining_buckets, id);
            """)
            for column in ("duration_buckets INTEGER NOT NULL DEFAULT 0", "elapsed_buckets INTEGER NOT NULL DEFAULT 0"):
                if column.split()[0] not in {row[1] for row in conn.execute("PRAGMA table_info(fx_events)")}:
                    conn.execute(f"ALTER TABLE fx_events ADD COLUMN {column}")
            conn.execute("UPDATE fx_events SET duration_buckets=remaining_buckets WHERE duration_buckets=0 AND remaining_buckets>0")
            conn.commit()
        finally:
            conn.close()


def _transaction():
    """Context manager is kept as a tiny function to centralize write locks."""
    class Tx:
        def __enter__(self):
            _LOCK.acquire()
            try:
                self.conn = _connect()
                self.conn.execute("BEGIN IMMEDIATE")
                return self.conn
            except Exception:
                try:
                    if getattr(self, "conn", None) is not None:
                        self.conn.close()
                finally:
                    _LOCK.release()
                raise
        def __exit__(self, exc_type, exc, tb):
            try:
                self.conn.commit() if exc_type is None else self.conn.rollback()
                self.conn.close()
            finally:
                _LOCK.release()
            return False
    return Tx()


def _now_bucket(now=None):
    return int((time.time() if now is None else float(now)) // _CANDLE_SECONDS)


def _local_datetime(timestamp=None):
    return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, _TZ)


def _sim_market_open(timestamp=None):
    local = _local_datetime(timestamp)
    minute = local.hour * 60 + local.minute
    return _MARKET_OPEN_MINUTE <= minute < _MARKET_CLOSE_MINUTE


def _gold_market_open(timestamp=None):
    """Use the feed freshness for current status; fallback is DST-aware."""
    if timestamp is None and _GOLD_OPEN is not None:
        return bool(_GOLD_OPEN)
    now = datetime.fromtimestamp(time.time() if timestamp is None else timestamp, _NY_TZ)
    minute = now.hour * 60 + now.minute
    if now.weekday() == 5:
        return False
    if now.weekday() == 6:
        return minute >= 18 * 60 + 5
    # Daily rollover pause 17:00-18:00 New York; Friday closes at 17:00.
    if minute >= 17 * 60:
        return now.weekday() < 4 and minute >= 18 * 60
    return True


def _event_effect(theme, asset, severity="minor", reference_price=None, current_price=None):
    """Private price-impact policy. Only narrative is ever broadcast."""
    strength = 1.0 if severity == "minor" else 1.7 if severity == "major" else 2.8
    mapping = {
        "fear_sell": {"iron": -0.0018, "emerald": -0.0013, "diamond": -0.0017},
        "long_bear": {"iron": -0.00075, "emerald": -0.00065, "diamond": -0.0008},
        "capital_inflow": {"iron": 0.0010, "emerald": 0.00135, "diamond": 0.0011},
        "dip_buy": {"iron": 0.0004, "emerald": 0.00045, "diamond": 0.0005},
        "sector_rotation": {"iron": 0.0008, "emerald": -0.0007, "diamond": 0.00035},
        "supply_shock": {"iron": -0.0005, "emerald": 0.00015, "diamond": 0.0012},
        "uncertain_vol": {"iron": 0.0, "emerald": 0.0, "diamond": 0.0},
        "no_effect": {"iron": 0.0, "emerald": 0.0, "diamond": 0.0},
        "black_swan": {"iron": -0.0030, "emerald": -0.0024, "diamond": -0.0035},
    }
    if asset not in _SIM_ASSETS:
        return 0.0, 1.0
    drift = mapping.get(theme, mapping["no_effect"]).get(asset, 0.0) * strength
    if theme == "dip_buy" and reference_price and current_price:
        if float(current_price) < float(reference_price) * 0.985:
            drift += 0.0022 * strength
        else:
            drift -= 0.00035 * strength
    # Event effects are calibrated too: reducing Gaussian noise alone leaves
    # deterministic news drift large enough to produce extreme moves.
    drift *= 0.03 * _SIM_VOLATILITY_MULTIPLIER
    vol_mult = 1.25 if theme == "uncertain_vol" else 1.15 if theme in {"fear_sell", "black_swan"} else 1.0
    if severity == "black_swan":
        vol_mult = 1.5
    return drift, vol_mult



def _event_impact(event, asset, bucket, price):
    """Deterministic per-event response, diversity without model-generated prices."""
    if asset not in _SIM_ASSETS:
        return 0.0, 1.0
    elapsed = int(event.get("elapsed_buckets") or 0)
    duration = max(1, int(event.get("duration_buckets") or event.get("remaining_buckets") or 1))
    phase = min(1.0, elapsed / duration)
    theme = str(event.get("theme") or "no_effect")
    severity = str(event.get("severity") or "minor")
    refs = json.loads(event.get("reference_prices") or "{}")
    reference = float(refs.get(asset) or price)
    # Asset-specific coefficients distinguish sector rotation from uniform shocks.
    coefficient = {"iron": 0.8, "emerald": 1.0, "diamond": 1.2}[asset]
    strength = 1 if severity == "minor" else 1.5 if severity == "major" else 2
    unit = 0.00002 * _SIM_VOLATILITY_MULTIPLIER * coefficient * strength
    if theme == "mean_revert":
        displacement = math.log(reference / price) if price > 0 and reference > 0 else 0
        return max(-unit*2, min(unit*2, displacement*0.03)), 0.75
    if theme == "trend_follow":
        sign = 1 if price >= reference else -1
        return sign*unit*(0.3+0.7*phase), 1.1
    if theme == "liquidity_crunch":
        return -unit*(1-phase), 1.8 if phase < 0.4 else 1.2
    if theme == "relief_rally":
        # Delayed reversal: the event changes sign instead of only adding noise.
        return (-0.5 if phase < 0.3 else 0.8)*unit, 1.05
    if theme == "oscillation":
        return math.sin((elapsed+1)*math.tau/12)*unit, 1.2
    if theme == "quiet_liquidity":
        return 0.0, 0.55
    drift, volatility = _event_effect(theme, asset, severity, reference, price)
    # Older themes decay over the event's active minutes; black swans front-load.
    decay = math.exp(-4*phase) if theme == "black_swan" else (0.25+0.75*(1-phase))
    return drift*decay, volatility

def _active_events(conn, bucket):
    rows = conn.execute(
        "SELECT id,theme,severity,remaining_buckets,reference_prices,duration_buckets,elapsed_buckets,created_at FROM fx_events "
        "WHERE consumed=1 AND remaining_buckets>0 ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


def _advance_market(conn, now=None, *, finalize_session=False):
    """Advance simulated MC assets only during their configured local session."""
    now = time.time() if now is None else float(now)
    prices = {}
    state_rows = conn.execute("SELECT asset,price FROM fx_market_state").fetchall()
    prices.update({str(row[0]): float(row[1]) for row in state_rows})
    local = _local_datetime(now)
    can_finalize = finalize_session and local.hour * 60 + local.minute >= _MARKET_CLOSE_MINUTE
    if _sim_market_open(now) or can_finalize:
        session_start = local.replace(hour=19, minute=0, second=0, microsecond=0).timestamp()
        session_end = local.replace(hour=21, minute=0, second=0, microsecond=0).timestamp()
        first_bucket = _now_bucket(session_start)
        target_bucket = min(_now_bucket(now), _now_bucket(session_end) - 1)
        states = {}
        for asset in _SIM_ASSETS:
            row = conn.execute("SELECT last_bucket,price FROM fx_market_state WHERE asset=?", (asset,)).fetchone()
            if row is None:
                if can_finalize:
                    continue
                states[asset] = [first_bucket-1, float(_ASSETS[asset]["base"])]
                conn.execute("INSERT INTO fx_market_state(asset,last_bucket,price,updated_at) VALUES(?,?,?,?)", (asset,first_bucket-1,states[asset][1],now))
            else:
                states[asset] = [max(first_bucket-1,int(row[0])),float(row[1])]
        start_bucket = min((state[0]+1 for state in states.values()), default=target_bucket+1)
        # Iterate by MINUTE, not by ASSET: all curves observe the same event age,
        # effects expire once per active minute and long events pause overnight.
        for bucket in range(start_bucket,target_bucket+1):
            events = _active_events(conn,bucket)
            changed = False
            for asset,state in states.items():
                if state[0] >= bucket:
                    continue
                changed = True
                spec = _ASSETS[asset]
                opening = state[1]
                seed = int.from_bytes(hashlib.sha256(f"fx-v2:{asset}:{bucket}".encode("ascii")).digest()[:8],"big")
                rng = random.Random(seed)
                drift,vol_mult = 0.0,1.0
                for event in events:
                    # A freshly created event must never change earlier missing candles.
                    if bucket < _now_bucket(float(event.get("created_at") or now)):
                        continue
                    impact,multiplier = _event_impact(event,asset,bucket,opening)
                    drift += impact
                    vol_mult *= multiplier
                drift = max(-_SIM_EVENT_DRIFT_CAP,min(_SIM_EVENT_DRIFT_CAP,drift))
                vol_mult = max(0.4,min(2.5,vol_mult))
                ret = max(-_SIM_MINUTE_RETURN_CAP,min(_SIM_MINUTE_RETURN_CAP,rng.gauss(drift,float(spec["vol"])*vol_mult)))
                close = max(0.01,opening*math.exp(ret))
                wick_up = rng.uniform(0.00001,max(0.00001,float(spec["vol"])*0.5*vol_mult))
                wick_down = rng.uniform(0.00001,max(0.00001,float(spec["vol"])*0.5*vol_mult))
                high = max(opening,close)*(1+wick_up)
                low = max(0.01,min(opening,close)*(1-wick_down))
                conn.execute("INSERT OR REPLACE INTO fx_market_candles(asset,bucket,open,high,low,close) VALUES(?,?,?,?,?,?)",(asset,bucket,opening,high,low,close))
                state[:] = [bucket,close]
            if changed:
                for event in events:
                    if bucket >= _now_bucket(float(event.get("created_at") or now)):
                        conn.execute("UPDATE fx_events SET duration_buckets=CASE WHEN duration_buckets=0 THEN remaining_buckets ELSE duration_buckets END,remaining_buckets=MAX(0,remaining_buckets-1),elapsed_buckets=elapsed_buckets+1 WHERE id=?",(event['id'],))
        for asset,(last_bucket,price) in states.items():
            conn.execute("UPDATE fx_market_state SET last_bucket=?,price=?,updated_at=? WHERE asset=?",(last_bucket,price,now,asset))
            prices[asset] = price
            conn.execute("DELETE FROM fx_market_candles WHERE asset=? AND bucket<?",(asset,target_bucket-_CANDLE_RETENTION+1))
    # Real-market assets are only advanced by the external-feed synchronizer.
    for asset in ("gold", "copper"):
        row = conn.execute("SELECT point_price FROM fx_external_quotes WHERE asset=?", (asset,)).fetchone()
        if row:
            prices[asset] = float(row[0])
    return prices, _now_bucket(now)


def _fetch_json(url):
    try:
        response = requests.get(url, timeout=(4, 8), headers={"User-Agent": "WechatBot-FX/1.0", "Cache-Control": "no-cache"})
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except Exception as exc:
        _LOG.warning("FX external feed failed url=%s error=%s", url, exc)
        return None


def _asset_trade_open(asset, timestamp=None):
    spec = _ASSETS.get(asset) or {}
    if spec.get("kind") == "xau_usd":
        return _gold_market_open(timestamp)
    return _sim_market_open(timestamp)



def _source_timestamp(value, fallback):
    if value in (None, ""):
        return float(fallback)
    try:
        numeric = float(value)
        return numeric / 1000.0 if numeric > 10000000000 else numeric
    except (TypeError, ValueError):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()


def _fetch_quote_payload(url):
    # Idempotent GET retry, bounded and isolated from the session/notification worker.
    payload = _fetch_json(url)
    return payload if payload is not None else _fetch_json(url)

def _sync_external_feeds(now=None):
    """Cache XAU/USD and USD/JPY off-thread; never fetch while a trade runs."""
    global _GOLD_OPEN
    now = time.time() if now is None else float(now)
    gold = _fetch_quote_payload(_GOLD_FEED_URL)
    fx = _fetch_quote_payload(_COPPER_FEED_URL)
    quotes = []
    if isinstance(gold, dict):
        try:
            source = float(gold["price"])
            stamp = _source_timestamp(gold.get("updatedAt") or gold.get("timestamp"), 0)
            # Gold API exposes the latest update time, not a separate session
            # flag. Treat a fresh quote as open and a stale quote as closed;
            # this follows the observed XAU/USD curve rather than inventing a
            # local 19:00-21:00 schedule.
            gold_open = 0 <= (now - stamp) <= 180.0
            quotes.append(("gold", "XAU/USD", source, stamp, gold_open))
        except (KeyError, TypeError, ValueError):
            _LOG.warning("invalid XAU/USD payload")
    if isinstance(fx, dict) and fx.get("success") is not False:
        try:
            source = float((fx.get("rates") or {})["JPY"])
            stamp = _source_timestamp(fx.get("timestamp") or fx.get("date"), 0)
            quotes.append(("copper", "USD/JPY", source, stamp, _sim_market_open(now) and 0 <= now-stamp <= _FEED_MAX_AGE_SECONDS))
        except (KeyError, TypeError, ValueError):
            _LOG.warning("invalid USD/JPY payload")
    if not quotes:
        with _transaction() as conn:
            row = conn.execute("SELECT source_timestamp FROM fx_external_quotes WHERE asset='gold'").fetchone()
            if row and now-float(row[0])>180:
                _GOLD_OPEN = False
                conn.execute("UPDATE fx_external_quotes SET market_open=0 WHERE asset='gold'")
        return False
    accepted = 0
    with _transaction() as conn:
        for asset, symbol, source, stamp, is_open in quotes:
            if not math.isfinite(source) or source <= 0 or not math.isfinite(stamp) or stamp <= 0 or stamp > now + 60:
                _LOG.warning("FX rejected invalid external quote asset=%s", asset)
                continue
            row = conn.execute("SELECT anchor_price,source_timestamp FROM fx_external_quotes WHERE asset=?", (asset,)).fetchone()
            if row and stamp < float(row[1]):
                _LOG.warning("FX ignored out-of-order external quote asset=%s", asset)
                continue
            if asset == "gold":
                _GOLD_OPEN = bool(is_open)
            accepted += 1
            anchor = float(row[0]) if row else source
            point = float(_ASSETS[asset]["base"]) * source / anchor if anchor else float(_ASSETS[asset]["base"])
            conn.execute(
                "INSERT INTO fx_external_quotes(asset,symbol,source_price,point_price,anchor_price,source_timestamp,fetched_at,market_open) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(asset) DO UPDATE SET symbol=excluded.symbol,source_price=excluded.source_price,point_price=excluded.point_price,source_timestamp=excluded.source_timestamp,fetched_at=excluded.fetched_at,market_open=excluded.market_open",
                (asset, symbol, source, point, anchor, stamp, now, int(bool(is_open))),
            )
            # Quote history follows BOTH external feeds continuously. The
            # separate _asset_trade_open gate still restricts copper trading to
            # 19:00-21:00. Use source time, never create fresh candles from stale
            # cached responses, and keep the original open on same-minute polls.
            if not math.isfinite(point) or point <= 0:
                continue
            bucket = _now_bucket(stamp)
            state = conn.execute("SELECT last_bucket,price FROM fx_market_state WHERE asset=?", (asset,)).fetchone()
            if state and bucket < int(state[0]):
                continue
            candle = conn.execute(
                "SELECT open,high,low FROM fx_market_candles WHERE asset=? AND bucket=?", (asset, bucket)
            ).fetchone()
            opening = float(candle[0]) if candle else (float(state[1]) if state else point)
            high = max(float(candle[1]), point) if candle else max(opening, point)
            low = min(float(candle[2]), point) if candle else min(opening, point)
            conn.execute("INSERT OR REPLACE INTO fx_market_state(asset,last_bucket,price,updated_at) VALUES(?,?,?,?)", (asset, bucket, point, stamp))
            conn.execute("INSERT OR REPLACE INTO fx_market_candles(asset,bucket,open,high,low,close) VALUES(?,?,?,?,?,?)", (asset, bucket, opening, high, low, point))
            conn.execute(
                "DELETE FROM fx_market_candles WHERE asset=? AND bucket NOT IN "
                "(SELECT bucket FROM fx_market_candles WHERE asset=? ORDER BY bucket DESC LIMIT ?)",
                (asset, asset, _CANDLE_RETENTION),
            )
        gold_row = conn.execute("SELECT source_timestamp FROM fx_external_quotes WHERE asset='gold'").fetchone()
        if not gold_row or now-float(gold_row[0])>180:
            _GOLD_OPEN = False
            conn.execute("UPDATE fx_external_quotes SET market_open=0 WHERE asset='gold'")
    return bool(accepted)


def _event_templates():
    return [
        ("minor", "mean_revert", "长期库存收购计划启动", "资源商人开始分批建立库存，短期失衡报价逐渐受到长期订单牵引。", 360),
        ("minor", "trend_follow", "跨村庄工程陆续签约", "多个工程队按照相近进度分批采购，商人预计接下来数个交易时段都有需求。", 240),
        ("minor", "quiet_liquidity", "交易规则逐步规范", "交易大厅引入统一验货和稳定交割，报价争议减少。", 90),
        ("minor", "oscillation", "商队报价交替调整", "两条运输线路轮流到货，库存时紧时松。", 45),
        ("major", "liquidity_crunch", "仓储交割暂时受限", "货物短期难以交割，抢购与撤单交错，影响可能跨越多个交易时段。", 240),
        ("major", "relief_rally", "运输线路分阶段恢复", "早期清点仍然紧张，后续修复将逐步释放库存和运力。", 480),
        ("minor", "capital_inflow", "大型资本开始寻找新矿区", "数支大型采矿队带着库存进入市场，交易大厅突然挤满了新面孔。", 3),
        ("minor", "dip_buy", "低价收购传闻扩散", "有人在深夜挂出大额收购单，但没有人确认买家的真实身份。", 4),
        ("minor", "uncertain_vol", "村民交易所报价失去共识", "不同村庄给出了互相矛盾的报价，玩家开始频繁修改自己的出价。", 3),
        ("minor", "sector_rotation", "大型工程临时更换材料方案", "施工队放弃了原定的部分材料采购，转而寻找另一条供应路线。", 4),
        ("minor", "no_effect", "河边发现会捡鱼的狐狸", "目击者称它叼走了两条鱼，目前没有任何村民愿意发表评论。", 2),
        ("major", "fear_sell", "主世界矿道出现安全警报", "多支采矿队暂时撤离深层矿道，市场上开始出现集中抛售。", 5),
        ("major", "long_bear", "远古城市的补给线中断", "探险队推迟了返程计划，资源商人开始重新评估长期库存。", 360),
        ("major", "capital_inflow", "大型资本宣布建设跨区仓储", "一支大型资本集团开始在多个区域收集资源，交易量持续放大。", 240),
        ("major", "dip_buy", "匿名财团试探性抄底", "市场低迷时出现连续大额买单，卖方暂时无法确认资金来源。", 7),
        ("major", "uncertain_vol", "末地航线前景突然变得模糊", "新的航线报告互相矛盾，玩家情绪被放大，报价开始快速来回跳动。", 6),
        ("black_swan", "black_swan", "黑曜石门异常开启", "多个区域同时报告无法解释的跨区资源流动，原有供需判断暂时失去参考。", 4),
    ]


def _major_recent(conn, now):
    cutoff = now - _MAJOR_EVENT_COOLDOWN_DAYS * 86400
    return bool(conn.execute("SELECT 1 FROM fx_events WHERE severity IN ('major','black_swan') AND created_at>? LIMIT 1", (cutoff,)).fetchone())


def _llm_prepare_event(now):
    """Ask the configured LLM for one event candidate only when no event is ready.

    The candidate is constrained to a local JSON schema and stored as narrative
    plus a server-side theme; if the provider is unavailable, templates remain
    authoritative. This worker is never on the order path.
    """
    if _LLM_SERVICE is None:
        return None
    prompt = (
        "为 Minecraft 群聊 FX 模拟市场准备一条事件。只输出 JSON："
        '{"severity":"minor|major|black_swan","theme":"fear_sell|long_bear|capital_inflow|dip_buy|sector_rotation|uncertain_vol|no_effect|mean_revert|trend_follow|liquidity_crunch|relief_rally|oscillation|quiet_liquidity",'
        '"title":"不超过24字","body":"不超过100字","duration_minutes":5}。长期库存/运输/工程事件可以持续120至720个开盘分钟，普通事件2至30分钟。'
        "不要说明会影响哪种物品、方向、幅度或概率；black_swan 必须罕见。"
    )
    try:
        call = getattr(_LLM_SERVICE, "_provider_call_low_priority", None)
        if not callable(call):
            return None
        text = call([{"role": "user", "content": prompt}], timeout=12)
        payload = json.loads(str(text or "").strip().strip("`").replace("json", "", 1).strip())
        allowed_themes = {item[1] for item in _event_templates()}
        if not isinstance(payload, dict) or payload.get("theme") not in allowed_themes:
            return None
        severity = str(payload.get("severity") or "minor")
        if severity not in {"minor", "major", "black_swan"}:
            severity = "minor"
        if severity in {"major", "black_swan"}:
            with _transaction() as check_conn:
                if _major_recent(check_conn, now) or random.Random(int(now // 86400)).random() > 0.10:
                    severity = "minor"
        title = str(payload.get("title") or "市场出现新的传闻")[:36]
        body = str(payload.get("body") or "交易者开始重新评估今日资源流向。")[:160]
        duration = max(2, min(720, int(payload.get("duration_minutes") or 5)))
        if severity == "black_swan":
            duration = min(duration,30)
        with _transaction() as conn:
            if conn.execute("SELECT 1 FROM fx_events WHERE trade_date=? AND published_at IS NULL LIMIT 1", (_local_datetime(now).date().isoformat(),)).fetchone():
                return None
            refs = {row[0]: row[1] for row in conn.execute("SELECT asset,price FROM fx_market_state").fetchall()}
            conn.execute("INSERT INTO fx_events(trade_date,event_kind,title,body,theme,severity,remaining_buckets,reference_prices,prepared_by,created_at,consumed,duration_buckets) VALUES(?,?,?,?,?,?,?,?,?,?,1,?)", (_local_datetime(now).date().isoformat(), payload["theme"], title, body, payload["theme"], severity, duration, json.dumps(refs, ensure_ascii=False), "llm", now, duration))
            return True
    except Exception as exc:
        _LOG.debug("LLM event preparation skipped: %s", exc)
        return None


def _prepare_event(conn, now, *, force=False):
    date_key = _local_datetime(now).date().isoformat()
    if conn.execute("SELECT COUNT(*) FROM fx_events WHERE trade_date=?", (date_key,)).fetchone()[0] >= _EVENT_DAILY_LIMIT:
        return False
    templates = [item for item in _event_templates() if item[0] == "minor"]
    seed = int.from_bytes(hashlib.sha256(f"fx-event:{date_key}:{int(now)//300}".encode()).digest()[:8], "big")
    rng = random.Random(seed)
    if rng.random() < 0.10 and not _major_recent(conn, now):
        templates += [item for item in _event_templates() if item[0] in {"major", "black_swan"}]
    severity, theme, title, body, duration = rng.choice(templates)
    refs = {row[0]: row[1] for row in conn.execute("SELECT asset,price FROM fx_market_state").fetchall()}
    # prepared/consumed=1 means it is already allowed to influence the next
    # simulated candle; published_at controls when the narrative is revealed.
    conn.execute("INSERT INTO fx_events(trade_date,event_kind,title,body,theme,severity,remaining_buckets,reference_prices,prepared_by,created_at,consumed,duration_buckets) VALUES(?,?,?,?,?,?,?,?,?,?,1,?)", (date_key, theme, title, body, theme, severity, duration, json.dumps(refs, ensure_ascii=False), "template", now, duration))
    return True


def _publish_pending_event(conn, now, prefix="", group_id=""):
    date_key = _local_datetime(now).date().isoformat()
    if group_id:
        delivery_key = f"opening_news:{date_key}:{group_id}"
        if conn.execute("SELECT 1 FROM fx_runtime_state WHERE key=?", (delivery_key,)).fetchone():
            return None
        row = conn.execute("SELECT id FROM fx_events WHERE trade_date=? AND published_at IS NULL ORDER BY id LIMIT 1", (date_key,)).fetchone()
    else:
        # A random event is a broadcast event: publish the newest prepared row
        # globally so it is not attached to a later user query.
        row = conn.execute("SELECT id FROM fx_events WHERE trade_date=? AND published_at IS NULL ORDER BY id DESC LIMIT 1", (date_key,)).fetchone()
    if not row:
        return None
    event = conn.execute("SELECT * FROM fx_events WHERE id=?", (row[0],)).fetchone()
    if group_id:
        conn.execute("INSERT INTO fx_runtime_state(key,value,updated_at) VALUES(?,?,?)", (delivery_key, str(row[0]), now))
    else:
        conn.execute("UPDATE fx_events SET published_at=? WHERE id=?", (now, row[0]))
    return f"{prefix}{event['title']}\n{event['body']}"


def _opening_notice(now):
    with _transaction() as conn:
        date_key = _local_datetime(now).date().isoformat()
        key = f"open_notice:{date_key}"
        if conn.execute("SELECT 1 FROM fx_runtime_state WHERE key=?", (key,)).fetchone():
            return None
        prices, _ = _advance_market(conn, now)
        prepared = _prepare_event(conn, now, force=True)
        if not prepared:
            _llm_prepare_event(now)
        conn.execute("INSERT INTO fx_runtime_state(key,value,updated_at) VALUES(?,?,?)", (key, "1", now))
        lines = ["📈 FX 今日开盘", "交易时间：每日 19:00—21:00", "今日可交易：铁锭、绿宝石、钻石、金锭、铜锭"]
        for asset in _ASSETS:
            if asset in prices:
                lines.append(f"{_ASSETS[asset]['name']} {prices[asset]:.2f} 积分/件")
        return "\n".join(lines)


def _maybe_random_event(now):
    if not _sim_market_open(now):
        return None
    with _transaction() as conn:
        date_key = _local_datetime(now).date().isoformat()
        count = conn.execute("SELECT COUNT(*) FROM fx_events WHERE trade_date=? AND published_at IS NOT NULL", (date_key,)).fetchone()[0]
        last = conn.execute("SELECT MAX(published_at) FROM fx_events WHERE trade_date=? AND published_at IS NOT NULL", (date_key,)).fetchone()[0]
        if count >= _EVENT_DAILY_LIMIT or (last and now - float(last) < _EVENT_MIN_GAP_SECONDS):
            return None
        seed = int.from_bytes(hashlib.sha256(f"fx-drop:{date_key}:{int(now)//300}".encode()).digest()[:8], "big")
        if random.Random(seed).random() > 0.10:
            return None
        prepared = _prepare_event(conn, now)
        if not prepared:
            return None
        return _publish_pending_event(conn, now, "🎲 事件掉落\n")


def _broadcast_text(text):
    if not text or not callable(_BROADCAST):
        return False
    try:
        return bool(_BROADCAST(text))
    except Exception:
        _LOG.exception("FX broadcast failed")
        return False


def _closing_notice(now):
    local = _local_datetime(now)
    if local.hour * 60 + local.minute < _MARKET_CLOSE_MINUTE:
        return None
    date_key = local.date().isoformat()
    key = f"close_notice:{date_key}"
    with _transaction() as conn:
        if conn.execute("SELECT 1 FROM fx_runtime_state WHERE key=?", (key,)).fetchone():
            return None
        # Catch up only a session observed today, never invent missed historical sessions.
        if not conn.execute("SELECT 1 FROM fx_runtime_state WHERE key=?", (f"open_notice:{date_key}",)).fetchone():
            return None
        prices, _ = _advance_market(conn, now, finalize_session=True)
    lines = ["📉 FX 今日收盘", "铁锭、绿宝石、钻石及铜锭每日 19:00—21:00 开仓，现已收盘；持仓保留，仍可平仓。"]
    lines += [f"{_ASSETS[asset]['name']} 收盘价 {prices[asset]:.4f} 积分/件" for asset in (*_SIM_ASSETS, "copper") if asset in prices]
    lines.append("金锭另按 XAU/USD 行情源状态交易，不随模拟盘一起关闭。")
    return key, "\n".join(lines)


def _flush_closing_notice(now):
    pending = _closing_notice(now)
    if not pending:
        return False
    key, text = pending
    if not _broadcast_text(text):
        return False
    # Do not mark before successful send: transient failures retry next tick.
    with _transaction() as conn:
        conn.execute("INSERT OR IGNORE INTO fx_runtime_state(key,value,updated_at) VALUES(?,?,?)", (key, "sent", now))
    return True


def _session_tick(now):
    # Advance candles even with no user /fx query; neither poll nor LLM calls
    # should dictate whether a synthetic session has fresh data.
    if _sim_market_open(now):
        with _transaction() as conn:
            _advance_market(conn, now)
        opening = _opening_notice(now)
        if opening:
            _broadcast_text(opening)
        event = _maybe_random_event(now)
        if event:
            _broadcast_text(event)
    else:
        _flush_closing_notice(now)
    local = _local_datetime(now)
    if local.hour == 18 and 50 <= local.minute < 55:
        with _transaction() as conn:
            has_event = bool(conn.execute("SELECT 1 FROM fx_events WHERE trade_date=?", (local.date().isoformat(),)).fetchone())
        if not has_event:
            _llm_prepare_event(now)


def _feed_worker():
    last_gold_open = None
    while not _STOP.is_set():
        started = time.monotonic()
        try:
            with _FEED_LOCK:
                _sync_external_feeds()
            gold_open = _gold_market_open()
            if last_gold_open is not None and gold_open != last_gold_open:
                _broadcast_text("🟡 金锭 XAU/USD 行情已恢复更新。" if gold_open else "🟡 金锭 XAU/USD 暂无新报价，暂停开仓并保留最后报价，仍可平仓。")
            last_gold_open = gold_open
        except Exception:
            _LOG.exception("FX feed worker failed; retrying next cycle")
        _STOP.wait(max(1.0, _FEED_POLL_SECONDS - (time.monotonic() - started)))


def _refresh_external_if_stale(now):
    with _transaction() as conn:
        rows = {row["asset"]: row for row in conn.execute("SELECT asset,fetched_at FROM fx_external_quotes")}
    if all(asset in rows and now-float(rows[asset]["fetched_at"]) < _FEED_POLL_SECONDS for asset in ("gold", "copper")):
        return
    if not _FEED_LOCK.acquire(blocking=False):
        return
    try:
        _sync_external_feeds(now)
    finally:
        _FEED_LOCK.release()


def _session_worker():
    while not _STOP.is_set():
        try:
            _session_tick(time.time())
        except Exception:
            _LOG.exception("FX session worker failed; loop will continue")
        _STOP.wait(5.0)


def _credit(conn, wxid, amount, now):
    if amount <= 0:
        return
    conn.execute(
        "UPDATE user_points SET points=points+?, updated_at=? WHERE wxid=?",
        (float(amount), datetime.fromtimestamp(now).isoformat(), str(wxid)),
    )


def _position_pnl(position, price):
    sign = 1.0 if position["side"] == "long" else -1.0
    return float(position["margin"]) * int(position["leverage"]) * sign * (
        float(price) / float(position["entry_price"]) - 1.0
    )


def _liquidate_positions(conn, prices, now):
    rows = conn.execute(
        "SELECT * FROM fx_positions WHERE status IN ('open','closing') ORDER BY id"
    ).fetchall()
    liquidated = []
    for position in rows:
        price = prices.get(position["asset"])
        if not price:
            continue
        pnl = _position_pnl(position, price)
        margin = float(position["margin"])
        if _ASSETS.get(position["asset"], {}).get("no_liquidation"):
            continue
        if pnl <= -margin * _LIQUIDATION_LOSS_RATIO + 1e-9:
            returned = round(margin * (1.0 - _LIQUIDATION_LOSS_RATIO), 4)
            conn.execute(
                "UPDATE fx_positions SET status='liquidated',closed_at=?,exit_price=?,realized_pnl=?,returned_margin=? WHERE id=? AND status IN ('open','closing')",
                (now, price, -margin * _LIQUIDATION_LOSS_RATIO, returned, position["id"]),
            )
            _credit(conn, position["wxid"], returned, now)
            liquidated.append({"id": position["id"], "wxid": position["wxid"], "group_id": position["group_id"], "returned": returned, "asset": position["asset"]})
    return liquidated


def _enqueue(wxid, group_id, action, payload, *, margin_to_reserve=0.0):
    now = time.time()
    # FX operations settle immediately. The market quote used for the
    # operation is still the current fixed one-minute bucket quote.
    delay = 0.0
    with _transaction() as conn:
        if margin_to_reserve > 0:
            row = conn.execute("SELECT points FROM user_points WHERE wxid=?", (wxid,)).fetchone()
            balance = float(row[0]) if row else 0.0
            if balance + 1e-9 < margin_to_reserve:
                raise ValueError(f"Insufficient points; available {balance:.1f}")
            cursor = conn.execute(
                "UPDATE user_points SET points=points-?,updated_at=? WHERE wxid=? AND points>=?",
                (margin_to_reserve, datetime.fromtimestamp(now).isoformat(), wxid, margin_to_reserve),
            )
            if cursor.rowcount != 1:
                raise ValueError("Balance changed; retry the order.")
        due = now + delay
        cur = conn.execute(
            "INSERT INTO fx_jobs(wxid,group_id,action,payload,due_at,status,created_at) VALUES(?,?,?,?,?,'pending',?)",
            (wxid, group_id or "", action, json.dumps(payload, ensure_ascii=False, separators=(",", ":")), due, now),
        )
        job_id = int(cur.lastrowid)
    _WAKE.set()
    return job_id, delay, None


def _format_price(asset, price):
    return f"{_ASSETS[asset]['name']} {price:.2f}\u79ef\u5206/\u4ef6"


def _market_lines(conn, assets=None, candle_count=_MAX_VISIBLE_CANDLES):
    assets = assets or list(_ASSETS)
    candle_count = max(1, min(int(candle_count or _MAX_VISIBLE_CANDLES), _MAX_VISIBLE_CANDLES))
    lines = ["\u6a21\u62df\u884c\u60c5\uff081\u5206\u949fK\u7ebf\uff1b\u6309\u6700\u65b0\u884c\u60c5\u66f4\u65b0\uff09"]
    for asset in assets:
        state = conn.execute("SELECT last_bucket,price FROM fx_market_state WHERE asset=?", (asset,)).fetchone()
        if not state:
            continue
        prev = conn.execute(
            "SELECT close FROM fx_market_candles WHERE asset=? AND bucket<? ORDER BY bucket DESC LIMIT 1",
            (asset, state[0]),
        ).fetchone()
        change = (float(state[1]) / float(prev[0]) - 1.0) * 100 if prev and prev[0] else 0.0
        lines.append(f"{_format_price(asset, float(state[1]))} 1m {change:+.2f}%")
        candles = conn.execute(
            "SELECT bucket,open,high,low,close FROM fx_market_candles WHERE asset=? ORDER BY bucket DESC LIMIT ?",
            (asset, candle_count),
        ).fetchall()
        candles = list(reversed(candles))
        bars = []
        for candle in candles:
            label = datetime.fromtimestamp(int(candle[0]) * _CANDLE_SECONDS).strftime("%m-%d %H:%M")
            bars.append(f"{label} O{candle[1]:.1f} H{candle[2]:.1f} L{candle[3]:.1f} C{candle[4]:.1f}")
        if bars:
            lines.append("K\u7ebf: " + " | ".join(bars))
    for external in ("gold", "copper"):
        quote = conn.execute("SELECT source_price,source_timestamp,fetched_at,market_open,point_price FROM fx_external_quotes WHERE asset=?", (external,)).fetchone()
        if quote:
            age = max(0, time.time()-float(quote[1]))
            status = "报价已同步" if age <= _FEED_MAX_AGE_SECONDS else "源报价延迟/缓存"
            status += "；开仓时段内" if _asset_trade_open(external) else "；收盘，仅可平仓"
            symbol = "XAU/USD" if external == "gold" else "USD/JPY"
            lines.append(f"{_ASSETS[external]['name']} {float(quote[4]):.2f} 积分/件 | {symbol} {float(quote[0]):.5f} | 源时间 {_local_datetime(float(quote[1])).strftime('%m-%d %H:%M:%S')} | {status}")
    lines.append("报价每1分钟更新；只保留最新必要的K线变动。金锭状态按 XAU/USD 数据更新时间判定。")
    return "\n".join(lines)



def _chart_font(size, bold=False):
    # Use a Simplified-Chinese font for ALL chart labels, including small text.
    # A font loading successfully does not imply it contains Chinese glyphs.
    font_root = Path(__file__).resolve().parents[1] / "assets" / "fonts"
    candidates = [
        font_root / "GlowSansSC-Normal-Bold.otf",
        Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "msyh.ttc",
        font_root / "Minecraft AE.ttf",
    ]
    for candidate in candidates:
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                pass
    raise RuntimeError("No Chinese chart font available; restore assets/fonts/GlowSansSC-Normal-Bold.otf")


def _chart_wrapped_lines(draw, text, font, max_width):
    """Pixel-based wrapping keeps Chinese footer text inside the PNG."""
    lines = []
    line = ""
    for char in text:
        if char == "\n":
            lines.append(line)
            line = ""
        elif line and draw.textlength(line + char, font=font) > max_width:
            lines.append(line)
            line = char
        else:
            line += char
    if line:
        lines.append(line)
    return lines or [""]


def _create_market_chart(conn, assets=None, candle_count=24):
    """Five readable panels; absent candles never silently remove an asset."""
    assets = list(dict.fromkeys(item for item in (assets or list(_ASSETS)) if item in _ASSETS))
    if not assets:
        assets = list(_ASSETS)
    candle_count = max(1, min(int(candle_count or _CANDLE_RETENTION), _CANDLE_RETENTION))
    _CHART_DIR.mkdir(parents=True, exist_ok=True)
    now = time.time()
    output = _CHART_DIR / f"market-{time.time_ns()}.png"
    width = _CHART_WIDTH
    panel_top, panel_gap, panel_h = 132, 24, _CHART_PANEL_HEIGHT
    footer_top = panel_top + len(assets) * (panel_h + panel_gap) + 12
    title_font = _chart_font(34, bold=True)
    label_font = _chart_font(26, bold=True)
    small_font = _chart_font(20)
    # Fixed panel height, dynamic canvas: adding assets cannot flatten Y axes.
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    footer_sections = [
        ("常用指令", [
            "/fx market pic：查看五种物品 K 线图   /fx market copper：查看铜锭行情",
            "/fx positions：持仓   /fx balance：积分   /fx long iron 50 5：50积分、5倍做多铁锭",
            "/fx short copper 50 5：做空铜锭   /fx long gold 50：无杠杆做多金锭",
            "/fx close 12：平掉 #12   /fx close all：全部平仓   /fx close all diamond：平掉全部钻石",
        ]),
        ("交易规则与行情来源", [
            "铁锭 iron / 绿宝石 emerald / 钻石 diamond：MC 模拟行情，每日19:00—21:00允许开仓。",
            "铜锭 copper：映射 USD/JPY（美元/日元）相对走势；连续记录报价，开仓仍限每日19:00—21:00。",
            "铁锭、绿宝石、钻石、铜锭：支持做多/做空，最高20倍；浮亏达保证金80%强平并 @ 提醒。",
            "金锭 gold：映射 XAU/USD 相对走势；只允许1倍、不强平；开仓按行情源状态，不受19:00—21:00限制。",
            "全品种收盘后仍允许平仓，保证金无单笔上限；操作无额外延迟，行情按分钟记录。",
            "外部标的以首次有效报价为固定锚点映射积分价，不每日重置；MC虚拟事件不改变金锭和铜锭报价。",
            "仅为群内积分模拟，不是真实交易；数据源异常暂停新开仓。未积累K线时仅显示缓存报价，不补造历史。",
        ]),
    ]
    footer_layout = []
    footer_h = 24
    for title, descriptions in footer_sections:
        lines = [line for text in descriptions for line in _chart_wrapped_lines(measure, text, small_font, width - 96)]
        footer_layout.append((title, lines))
        footer_h += 46 + len(lines) * 32 + 22
    height = footer_top + footer_h
    image = Image.new("RGB", (width, height), (18, 22, 31))
    draw = ImageDraw.Draw(image)
    draw.text((42, 22), "FX MARKET  |  1m K-LINE", fill=(240, 244, 250), font=title_font)
    draw.text((42, 72), _local_datetime(now).strftime("生成时间 %Y-%m-%d %H:%M:%S") + "（北京时间）", fill=(150, 164, 180), font=small_font)
    draw.text((width - 354, 72), "红色上涨 | 绿色下跌", fill=(240, 196, 110), font=small_font)
    colors = {"iron": (215, 154, 92), "emerald": (53, 210, 145), "diamond": (92, 182, 255), "gold": (248, 205, 86), "copper": (194, 112, 72)}
    for index, asset in enumerate(assets):
        rows = list(reversed(conn.execute(
            "SELECT bucket,open,high,low,close FROM fx_market_candles WHERE asset=? ORDER BY bucket DESC LIMIT ?",
            (asset, candle_count),
        ).fetchall()))
        quote = conn.execute("SELECT * FROM fx_external_quotes WHERE asset=?", (asset,)).fetchone()
        state = conn.execute("SELECT price,updated_at FROM fx_market_state WHERE asset=?", (asset,)).fetchone()
        y0 = panel_top + index * (panel_h + panel_gap)
        y1 = y0 + panel_h
        draw.rounded_rectangle((28, y0, width - 28, y1), radius=16, fill=(25, 31, 43), outline=(53, 65, 82), width=2)
        draw.text((52, y0 + 18), _ASSETS[asset]["name"], fill=colors[asset], font=label_font)
        price = float(rows[-1][4]) if rows else (float(quote["point_price"]) if quote else float(state[0]) if state else None)
        if price is not None:
            label = f"{price:.4f} 积分/件"
            if rows:
                previous = float(rows[-2][4]) if len(rows) > 1 else float(rows[-1][1])
                change = (price / previous - 1) * 100 if previous else 0
                label += f"   {change:+.3f}%"
                color = (245, 105, 112) if change >= 0 else (93, 220, 157)
            else:
                color = (220, 226, 235)
            draw.text((210, y0 + 18), label, fill=color, font=label_font)
        if quote:
            stamp = float(quote["source_timestamp"])
            symbol = "XAU/USD" if asset == "gold" else "USD/JPY"
            age = now - stamp
            status = "数据延迟/缓存" if age > 180 else "报价已同步"
            status += " | " + ("开仓时段内" if _asset_trade_open(asset) else "收盘，仅允许平仓")
            subtitle = f"{symbol} {float(quote['source_price']):.5f} | 源时间 {_local_datetime(stamp).strftime('%m-%d %H:%M:%S')} | {status}"
        elif asset in {"gold", "copper"}:
            subtitle = "外部行情尚未同步；不使用随机价格替代"
        else:
            subtitle = "MC模拟 | 每日19:00—21:00 | " + ("开盘" if _sim_market_open(now) else "收盘，保留最后行情")
            if rows:
                subtitle += " | 最近记录 " + _local_datetime(int(rows[-1][0]) * _CANDLE_SECONDS).strftime("%m-%d %H:%M")
        draw.text((52, y0 + 60), subtitle, fill=(150, 164, 180), font=small_font)
        left, right, top, bottom = 164, width - 58, y0 + 106, y1 - 42
        if not rows:
            # Do not turn a lone cache value into an invented candle series.
            hint = "已有缓存报价，尚未积累 K 线；下一次有效源报价将开始记录" if quote else "暂无 K 线数据，等待行情同步"
            draw.text((left, top + 80), hint, fill=(200, 208, 220), font=small_font)
            continue
        lo, hi = min(float(row[3]) for row in rows), max(float(row[2]) for row in rows)
        span = max(hi - lo, abs(price or 0) * 0.0002, 0.0001)
        lo -= span * 0.10
        hi = max(hi, lo + span) + span * 0.10
        span = hi - lo
        # Numeric labels plus generous plot height make tiny real-market moves legible.
        decimals = 4 if span < 0.1 else 3 if span < 1 else 2
        for grid in range(5):
            gy = top + (bottom - top) * grid / 4
            draw.line((left, gy, right, gy), fill=(46, 56, 70), width=1)
            value = hi - span * grid / 4
            draw.text((52, gy - 12), f"{value:.{decimals}f}", fill=(150, 164, 180), font=small_font)
        draw.line((left, top, left, bottom), fill=(92, 105, 123), width=1)
        draw.line((left, bottom, right, bottom), fill=(92, 105, 123), width=1)
        step = (right - left) / max(len(rows), 1)
        body_w = max(5, min(32, int(step * 0.58)))
        for i, row in enumerate(rows):
            _, opening, high, low, close = row
            x = left + step * (i + 0.5)
            def py(value):
                return bottom - (float(value) - lo) / span * (bottom - top)
            y_high, y_low, y_open, y_close = py(high), py(low), py(opening), py(close)
            candle_color = (245, 105, 112) if float(close) >= float(opening) else (93, 220, 157)
            draw.line((x, y_high, x, y_low), fill=candle_color, width=2)
            draw.rectangle((x - body_w / 2, min(y_open, y_close), x + body_w / 2, max(y_open, y_close) + 1), fill=candle_color)
        for i in sorted({0, len(rows) // 2, len(rows) - 1}):
            label = _local_datetime(int(rows[i][0]) * _CANDLE_SECONDS).strftime("%m-%d %H:%M")
            label_w = draw.textlength(label, font=small_font)
            x = min(right - label_w, max(left, left + step * (i + 0.5) - label_w / 2))
            draw.text((x, bottom + 9), label, fill=(150, 164, 180), font=small_font)
    draw.line((36, footer_top - 12, width - 36, footer_top - 12), fill=(53, 65, 82), width=2)
    footer_y = footer_top + 16
    for title, lines in footer_layout:
        draw.text((44, footer_y), title, fill=(240, 196, 110), font=label_font)
        footer_y += 46
        for line in lines:
            draw.text((44, footer_y), line, fill=(205, 215, 229), font=small_font)
            footer_y += 32
        footer_y += 22
    image.save(output, format="PNG", optimize=True)
    files = sorted(_CHART_DIR.glob("market-*.png"), key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in files[12:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return str(output)


def _positions_text(conn, wxid, prices):
    rows = conn.execute(
        "SELECT * FROM fx_positions WHERE wxid=? AND status IN ('open','closing') ORDER BY id DESC LIMIT 20",
        (wxid,),
    ).fetchall()
    if not rows:
        return "\u5f53\u524d\u6ca1\u6709\u6301\u4ed3\u3002"
    out = ["\u5f53\u524d\u6301\u4ed3\uff1a"]
    for p in rows:
        price = prices.get(p["asset"], p["entry_price"])
        pnl = _position_pnl(p, price)
        equity = max(0.0, float(p["margin"]) + pnl)
        side = "\u505a\u591a" if p["side"] == "long" else "\u505a\u7a7a"
        status = "\u5e73\u4ed3\u4e2d" if p["status"] == "closing" else "\u6301\u4ed3\u4e2d"
        out.append(
            f"#{p['id']} {_ASSETS[p['asset']]['name']} {side} {p['leverage']}x {status}\n"
            f"\u4fdd\u8bc1\u91d1 {p['margin']:.1f} | \u5165\u573a {p['entry_price']:.2f} | \u73b0\u4ef7 {price:.2f} | \u6d6e\u76c8\u4e8f {pnl:+.2f} | \u51c0\u503c {equity:.2f}"
        )
    out.append("\u6d6e\u4e8f\u8fbe\u5230\u4fdd\u8bc1\u91d1\u768480%\u65f6\u5f3a\u5e73\u3002")
    return "\n".join(out)


def _close_position(conn, position, price, now):
    pnl = _position_pnl(position, price)
    margin = float(position["margin"])
    # Gold follows XAU/USD without leverage and is never force-liquidated.
    if _ASSETS.get(position["asset"], {}).get("no_liquidation"):
        returned = round(max(0.0, margin + pnl), 4)
        status = "closed"
    # A price gap across the threshold is settled exactly at the 80% loss line.
    elif pnl <= -margin * _LIQUIDATION_LOSS_RATIO + 1e-9:
        returned = round(margin * (1.0 - _LIQUIDATION_LOSS_RATIO), 4)
        status = "liquidated"
        pnl = -margin * _LIQUIDATION_LOSS_RATIO
    else:
        returned = round(max(0.0, margin + pnl), 4)
        status = "closed"
    conn.execute(
        "UPDATE fx_positions SET status=?,closed_at=?,exit_price=?,realized_pnl=?,returned_margin=? WHERE id=? AND status IN ('open','closing')",
        (status, now, price, pnl, returned, position["id"]),
    )
    _credit(conn, position["wxid"], returned, now)
    return status, pnl, returned, price


def _execute_job(job_id):
    now = time.time()
    with _transaction() as conn:
        pending = conn.execute("SELECT action FROM fx_jobs WHERE id=? AND status='pending'", (job_id,)).fetchone()
    if pending and pending[0] in {"market", "market_pic", "candles", "open", "close", "positions"}:
        _refresh_external_if_stale(now)
        now = time.time()
    with _transaction() as conn:
        job = conn.execute("SELECT * FROM fx_jobs WHERE id=? AND status='pending'", (job_id,)).fetchone()
        if not job:
            return None
        payload = json.loads(job["payload"] or "{}")
        prices, _ = _advance_market(conn, now)
        forced = _liquidate_positions(conn, prices, now)
        action = job["action"]
        wxid = job["wxid"]
        if action == "market":
            result = _market_lines(conn, payload.get("assets"), payload.get("candles", _MAX_VISIBLE_CANDLES))
        elif action == "market_pic":
            result = _create_market_chart(conn, payload.get("assets"), payload.get("candles", _CANDLE_RETENTION))
        elif action == "candles":
            asset = payload.get("asset")
            result = _market_lines(conn, [asset] if asset else None, payload.get("candles", _MAX_VISIBLE_CANDLES))
        elif action == "balance":
            row = conn.execute("SELECT points FROM user_points WHERE wxid=?", (wxid,)).fetchone()
            points = float(row[0]) if row else 0.0
            locked = conn.execute(
                "SELECT COALESCE(SUM(margin),0) FROM fx_positions WHERE wxid=? AND status IN ('open','closing')",
                (wxid,),
            ).fetchone()[0]
            result = f"\u53ef\u7528\u79ef\u5206: {points:.1f} | \u6301\u4ed3\u4fdd\u8bc1\u91d1: {float(locked):.1f}"
        elif action == "positions":
            result = _positions_text(conn, wxid, prices)
        elif action == "open":
            asset = payload["asset"]
            price = prices[asset]
            margin = float(payload["margin"])
            leverage = int(payload["leverage"])
            units = margin * leverage / price
            cur = conn.execute(
                "INSERT INTO fx_positions(wxid,group_id,asset,side,margin,leverage,entry_price,units,status,opened_at) VALUES(?,?,?,?,?,?,?,?, 'open',?)",
                (wxid, job["group_id"], asset, payload["side"], margin, leverage, price, units, now),
            )
            side_text = "\u505a\u591a" if payload["side"] == "long" else "\u505a\u7a7a"
            result = (
                f"\u5f00\u4ed3\u6210\u4ea4 #{cur.lastrowid}: {_ASSETS[asset]['name']} {side_text} {leverage}x\n"
                f"\u4fdd\u8bc1\u91d1 {margin:.1f}\u79ef\u5206 | \u540d\u4e49\u4ef7\u503c {margin*leverage:.1f}\u79ef\u5206 | "
                f"\u6570\u91cf {units:.4f}\u4ef6 | \u6210\u4ea4\u4ef7 {price:.2f}\u79ef\u5206\n"
                f"\u5f3a\u5e73\u7ebf: \u6d6e\u4e8f\u8fbe\u4fdd\u8bc1\u91d1 80%\uff1b\u884c\u60c5\u8df3\u7a7a\u65f6\u6309\u9608\u503c\u7ed3\u7b97\u3002"
            )
        elif action == "close":
            requested_ids = payload.get("position_ids")
            if not isinstance(requested_ids, list):
                requested = payload.get("position_id")
                requested_ids = [requested] if requested is not None else None
            query = "SELECT * FROM fx_positions WHERE wxid=? AND status='closing'"
            args = [wxid]
            if requested_ids:
                placeholders = ",".join("?" for _ in requested_ids)
                query += f" AND id IN ({placeholders})"
                args.extend(int(item) for item in requested_ids)
            query += " ORDER BY id"
            rows = conn.execute(query, args).fetchall()
            if not rows:
                result = "\u6ca1\u6709\u5f85\u5e73\u4ed3\u7684\u6301\u4ed3\uff1b\u53ef\u80fd\u5df2\u89e6\u53ca\u5f3a\u5e73\u3002\u4f7f\u7528 /fx positions \u67e5\u770b\u3002"
            else:
                lines = []
                for p in rows:
                    if p["status"] == "liquidated":
                        continue
                    price = prices[p["asset"]]
                    status, pnl, returned, _ = _close_position(conn, p, price, now)
                    tag = "\u5f3a\u5236\u5e73\u4ed3" if status == "liquidated" else "\u5e73\u4ed3\u6210\u4ea4"
                    lines.append(f"{tag} #{p['id']} {_ASSETS[p['asset']]['name']} \u76c8\u4e8f {pnl:+.2f}\u79ef\u5206\uff0c\u8fd4\u8fd8 {returned:.2f}\u79ef\u5206")
                result = "\n".join(lines) if lines else "\u5e73\u4ed3\u7b49\u5f85\u671f\u95f4\u6301\u4ed3\u5df2\u89e6\u53ca\u5f3a\u5e73\u7ebf\u5e76\u6309\u89c4\u5219\u7ed3\u7b97\u3002"
        elif action == "help":
            result = _help_text()
        else:
            raise ValueError(f"unknown fx job action: {action}")
        news = None
        # The opening-day article is attached only after the MC session opens;
        # a pre-open / gold-only query must not consume it.
        if _sim_market_open(now):
            news = _publish_pending_event(conn, now, "📰 今日方块日报\n", group_id=str(job["group_id"] or ""))
        if news and action != "market_pic":
            result = f"{result}\n\n{news}"
        conn.execute("UPDATE fx_jobs SET status='done' WHERE id=?", (job_id,))
        conn.execute("DELETE FROM fx_jobs WHERE status IN ('done','failed') AND id < (SELECT COALESCE(MAX(id),0)-1000 FROM fx_jobs)")
    for item in forced:
        target_group = item.get("group_id") or job["group_id"]
        marker = f"[[WX_AT:{item['wxid']}]] " if item.get("wxid") else ""
        _safe_send(
            target_group,
            f"{marker}FX {_u('\u5f3a\u5236\u5e73\u4ed3')} #{item['id']} {_ASSETS[item['asset']]['name']}: "
            f"{_u('\u6d6e\u4e8f\u8fbe\u5230\u4fdd\u8bc1\u91d1 80%\uff0c\u8fd4\u8fd8')} {item['returned']:.2f} {_u('\u79ef\u5206')}",
        )
    if action == "market_pic" and result:
        try:
            send(target=job["group_id"], file_path=result, mode="wechat_file")
        except Exception:
            _LOG.exception("failed to send /fx market chart")
        if news:
            _safe_send(job["group_id"], news)
        return result
    _safe_send(job["group_id"], result)
    return result


def _defer_or_fail(job_id, error):
    now = time.time()
    with _transaction() as conn:
        row = conn.execute("SELECT * FROM fx_jobs WHERE id=? AND status='pending'", (job_id,)).fetchone()
        if not row:
            return
        attempts = int(row["attempts"]) + 1
        if attempts < 5:
            conn.execute(
                "UPDATE fx_jobs SET attempts=?,due_at=?,last_error=? WHERE id=?",
                (attempts, now + 5.0, str(error)[:500], job_id),
            )
            return
        payload = json.loads(row["payload"] or "{}")
        if row["action"] == "open":
            margin = float(payload.get("margin") or 0)
            _credit(conn, row["wxid"], margin, now)
        elif row["action"] == "close":
            pids = payload.get("position_ids")
            if isinstance(pids, list) and pids:
                placeholders = ",".join("?" for _ in pids)
                conn.execute(
                    f"UPDATE fx_positions SET status='open' WHERE wxid=? AND status='closing' AND id IN ({placeholders})",
                    [row["wxid"], *[int(item) for item in pids]],
                )
            else:
                pid = payload.get("position_id")
                if pid is not None:
                    conn.execute("UPDATE fx_positions SET status='open' WHERE id=? AND wxid=? AND status='closing'", (int(pid), row["wxid"]))
                else:
                    conn.execute("UPDATE fx_positions SET status='open' WHERE wxid=? AND status='closing'", (row["wxid"],))
        conn.execute(
            "UPDATE fx_jobs SET status='failed',attempts=?,last_error=? WHERE id=?",
            (attempts, str(error)[:500], job_id),
        )
        conn.execute("DELETE FROM fx_jobs WHERE status IN ('done','failed') AND id < (SELECT COALESCE(MAX(id),0)-1000 FROM fx_jobs)")
    _safe_send(row["group_id"], "FX\u64cd\u4f5c\u591a\u6b21\u5931\u8d25\uff0c\u672a\u6210\u4ea4\uff1b\u5df2\u9000\u56de\u9884\u7559\u4fdd\u8bc1\u91d1\uff08\u5982\u6709\uff09\u3002\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")


def _safe_send(group_id, text):
    if not group_id:
        return False
    try:
        success, error = send(target=group_id, content=text, mode="wechat_text")
        if not success:
            # HTTP/GUI failures are normally returned, not raised. Do not
            # silently lose liquidation notification diagnostics or re-run a trade.
            _LOG.error("failed to send /fx notification group=%s: %s", group_id, error)
        return bool(success)
    except Exception:
        _LOG.exception("failed to send /fx notification group=%s", group_id)
        return False


def _worker():
    while not _STOP.is_set():
        try:
            with _LOCK:
                conn = _connect()
                try:
                    job = conn.execute(
                        "SELECT id,due_at FROM fx_jobs WHERE status='pending' ORDER BY due_at,id LIMIT 1"
                    ).fetchone()
                finally:
                    conn.close()
            if not job:
                _WAKE.wait(1.0)
                _WAKE.clear()
                continue
            delay = float(job["due_at"]) - time.time()
            if delay > 0:
                _WAKE.wait(min(delay, 1.0))
                _WAKE.clear()
                continue
            try:
                _execute_job(int(job["id"]))
            except Exception as exc:
                _LOG.exception("/fx delayed job %s failed", job["id"])
                try:
                    _defer_or_fail(int(job["id"]), exc)
                except Exception:
                    _LOG.exception("/fx failed to defer job %s", job["id"])
        except Exception:
            _LOG.exception("/fx scheduler loop failed")
            _STOP.wait(1.0)


def set_llm_service(service):
    global _LLM_SERVICE
    _LLM_SERVICE = service


def init(config):
    global _THREAD
    _ensure_schema()
    if _THREAD is None or not _THREAD.is_alive():
        _STOP.clear()
        _THREAD = threading.Thread(target=_worker, daemon=True, name="fx-delayed-orders")
        _THREAD.start()
    _WAKE.set()


def start(broadcast):
    global _BROADCAST, _SESSION_THREAD, _FEED_THREAD
    _BROADCAST = broadcast if callable(broadcast) else None
    if _FEED_THREAD is None or not _FEED_THREAD.is_alive():
        _STOP.clear()
        _FEED_THREAD = threading.Thread(target=_feed_worker, daemon=True, name="fx-external-feeds")
        _FEED_THREAD.start()
    if _SESSION_THREAD is None or not _SESSION_THREAD.is_alive():
        _STOP.clear()
        _SESSION_THREAD = threading.Thread(target=_session_worker, daemon=True, name="fx-market-session")
        _SESSION_THREAD.start()


def _u(value):
    """Compatibility wrapper for user-visible UTF-8 strings."""
    return str(value)



def stop():
    """Stop background loops without cancelling already committed trades."""
    _STOP.set()
    _WAKE.set()
    for thread in (_THREAD, _SESSION_THREAD, _FEED_THREAD):
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=2.0)

def _help_text():
    return (
        "⚒️ FX 物品合约（纯积分模拟）\n"
        "标的：铁锭、绿宝石、钻石；支持做多/做空，杠杆最高 20 倍。\n"
        "每笔保证金无上限；浮亏达到保证金 80% 时强平。\n"
        "MC 模拟盘每日 19:00—21:00 开盘，K 线每分钟更新；收盘后仍可平仓。\n"
        "模拟盘波动已扩大至外汇校准基准的50倍（每分钟标准差约1.36%—2.31%）；仅积分模拟，杠杆收益和亏损都会显著放大。\n\n"
        "/fx market [标的]：查看行情/K 线\n"
        "/fx market pic：发送铁锭、绿宝石、钻石、金锭、铜锭的 K 线图\n"
        "/fx balance：查看可用积分与持仓保证金\n"
        "/fx positions：查看持仓\n"
        "/fx long <标的> <保证金> [杠杆]：开多单\n"
        "/fx short <标的> <保证金> [杠杆]：开空单\n"
        "/fx close <持仓ID|all>：平仓\n"
        "标的：铁锭/iron、绿宝石/emerald、钻石/diamond、金锭/gold、铜锭/copper。\n"
        "金锭跟随 XAU/USD 实际报价，按数据源更新时间同步开收盘；只允许 1 倍、无强平。\n"
        "铜锭跟随 USD/JPY 报价，但交易时间仍使用 MC 模拟盘 19:00—21:00。\n"
        "示例：/fx long 钻石 50 5；/fx short emerald 20 3"
    )

def _parse_asset(value):
    return _ASSET_ALIASES.get(str(value or "").strip().casefold())


def _external_quote_ready(asset):
    if asset not in {"gold", "copper"}:
        return True
    try:
        with _transaction() as conn:
            row = conn.execute("SELECT source_price,fetched_at,source_timestamp FROM fx_external_quotes WHERE asset=?", (asset,)).fetchone()
            now = time.time()
            return bool(row and float(row[0]) > 0 and 0 <= now-float(row[1]) <= _FEED_MAX_AGE_SECONDS
                        and -60 <= now-float(row[2]) <= _FEED_MAX_AGE_SECONDS)
    except Exception:
        return False


def _schedule_query(context, action, payload=None):
    wxid = str(context.get("wxid") or (context.get("raw") or {}).get("wxid") or "").strip()
    group_id = str(context.get("group") or context.get("user") or "").strip()
    if not wxid:
        return _u("\u65e0\u6cd5\u8bc6\u522b\u4f60\u7684\u8d26\u53f7\u3002")
    try:
        job_id, delay, _ = _enqueue(wxid, group_id, action, payload or {})
    except Exception:
        _LOG.exception("failed to queue /fx action")
        return _u("FX\u8bf7\u6c42\u5165\u961f\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")
    return None


def handle(content, context):
    context = context if isinstance(context, dict) else {}
    wxid = str(context.get("wxid") or (context.get("raw") or {}).get("wxid") or "").strip()
    if not wxid:
        return _u("\u65e0\u6cd5\u8bc6\u522b\u4f60\u7684\u8d26\u53f7\u3002")
    text = str(content or "").strip()
    parts = text.split()
    if not parts or parts[0].casefold() in {"help", "\u5e2e\u52a9", "\u89c4\u5219"}:
        # Even the help response is delayed like other valid FX operations.
        _schedule_query(context, "help")
        return None
    command = parts[0].casefold()
    if command in {"market", "quote", "\u884c\u60c5", "\u62a5\u4ef7", "candles", "kline", "k", "k\u7ebf"}:
        if command == "market" and len(parts) == 2 and parts[1].casefold() == "pic":
            return _schedule_query(context, "market_pic", {"assets": list(_ASSETS), "candles": _CANDLE_RETENTION})
        asset = _parse_asset(parts[1]) if len(parts) > 1 else None
        if len(parts) > 1 and asset is None:
            return _u("允许铁锭、绿宝石、钻石、金锭和铜锭作为交易标的。")
        action = "candles" if command in {"candles", "kline", "k", "k\u7ebf"} else "market"
        return _schedule_query(context, action, {"assets": [asset] if asset else None, "asset": asset, "candles": 8 if action == "candles" else 4})
    if command in {"balance", "points", "\u79ef\u5206"}:
        return _schedule_query(context, "balance")
    if command in {"positions", "position", "\u6301\u4ed3"}:
        return _schedule_query(context, "positions")
    if command in {"long", "buy", "\u505a\u591a", "short", "sell", "\u505a\u7a7a", "open"}:
        if command == "open":
            if len(parts) != 5:
                return _help_text()
            asset = _parse_asset(parts[1])
            side_text, margin_text, leverage_text = parts[2].casefold(), parts[3], parts[4]
            if side_text not in {"long", "short", "buy", "sell", "\u505a\u591a", "\u505a\u7a7a"}:
                return _help_text()
        else:
            if len(parts) not in (3, 4):
                return _help_text()
            asset = _parse_asset(parts[1])
            side_text = command
            margin_text = parts[2]
            leverage_text = parts[3] if len(parts) == 4 else "1"
        if asset is None:
            return _u("标的无效：可用铁锭、绿宝石、钻石、金锭或铜锭。")
        if not _asset_trade_open(asset):
            return _u("当前已收盘；MC 模拟盘每日 19:00—21:00 开盘，金锭按 XAU/USD 实际市场时段交易；收盘后仍可平仓。")
        if not _external_quote_ready(asset):
            return _u("当前外部行情源尚未同步或已过期，暂不接受该标的新仓位；稍后可重试。")
        try:
            margin = float(margin_text)
            leverage = int(leverage_text)
        except (TypeError, ValueError):
            return _u("\u4fdd\u8bc1\u91d1\u5fc5\u987b\u662f\u6570\u5b57\uff0c\u6760\u6746\u5fc5\u987b\u662f\u6574\u6570\u3002")
        if not math.isfinite(margin) or margin <= 0:
            return _u("\u4fdd\u8bc1\u91d1\u5fc5\u987b\u662f\u5927\u4e8e0\u7684\u6709\u6548\u6570\u5b57\u3002")
        if leverage < 1:
            return _u("\u6760\u6746\u5fc5\u987b\u662f\u5927\u4e8e\u6216\u7b49\u4e8e1\u7684\u6574\u6570\u3002")
        if _ASSETS[asset].get("no_leverage") and leverage != 1:
            return _u("金锭仅允许 1 倍、无杠杆交易。")
        if not _ASSETS[asset].get("no_leverage") and leverage > _MAX_LEVERAGE:
            return _u(f"MC 模拟标的杠杆最高 {_MAX_LEVERAGE} 倍。")
        side = "short" if side_text in {"short", "sell", "\u505a\u7a7a"} else "long"
        payload = {"asset": asset, "side": side, "margin": round(margin, 2), "leverage": leverage}
        try:
            job_id, delay, _ = _enqueue(
                str(context.get("wxid") or (context.get("raw") or {}).get("wxid") or "").strip(),
                str(context.get("group") or context.get("user") or "").strip(),
                "open", payload, margin_to_reserve=margin,
            )
        except ValueError as exc:
            return str(exc)
        except Exception:
            _LOG.exception("failed to reserve /fx margin")
            return _u("\u5f00\u4ed3\u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")
        return None
    if command == "close":
        # /fx close <position_id>
        # /fx close all
        # /fx close <asset> (all positions of an asset)
        # /fx close all <asset> (explicit asset-all form)
        if len(parts) not in (2, 3):
            return _help_text()
        wxid = str(context.get("wxid") or (context.get("raw") or {}).get("wxid") or "").strip()
        group_id = str(context.get("group") or context.get("user") or "").strip()
        selector = parts[1].casefold()
        asset_filter = None
        position_id = None
        if len(parts) == 3:
            if selector not in {"all", "\u5168\u90e8"}:
                return _help_text()
            asset_filter = _parse_asset(parts[2])
            if asset_filter is None:
                return _u("\u7269\u54c1\u65e0\u6548\uff1a\u53ea\u80fd\u4f7f\u7528\u94c1\u952d\u3001\u7eff\u5b9d\u77f3\u6216\u94bb\u77f3\u3002")
        elif selector in {"all", "\u5168\u90e8"}:
            pass
        else:
            asset_filter = _parse_asset(parts[1])
            if asset_filter is None:
                if not parts[1].isdigit():
                    return _help_text()
                position_id = int(parts[1])
        try:
            with _transaction() as conn:
                query = "SELECT id FROM fx_positions WHERE wxid=? AND status='open'"
                args = [wxid]
                if asset_filter:
                    query += " AND asset=?"
                    args.append(asset_filter)
                if position_id is not None:
                    query += " AND id=?"
                    args.append(position_id)
                rows = conn.execute(query, args).fetchall()
                if not rows:
                    return _u("\u5f53\u524d\u6ca1\u6709\u53ef\u5e73\u4ed3\u6301\u4ed3\u3002")
                ids = [int(row[0]) for row in rows]
                conn.executemany(
                    "UPDATE fx_positions SET status='closing' WHERE id=? AND status='open'",
                    [(item,) for item in ids],
                )
            _enqueue(wxid, group_id, "close", {"position_ids": ids})
        except Exception:
            _LOG.exception("failed to queue /fx close")
            with _transaction() as conn:
                if position_id is not None:
                    conn.execute("UPDATE fx_positions SET status='open' WHERE id=? AND wxid=? AND status='closing'", (position_id, wxid))
                elif asset_filter:
                    conn.execute("UPDATE fx_positions SET status='open' WHERE wxid=? AND asset=? AND status='closing'", (wxid, asset_filter))
                else:
                    conn.execute("UPDATE fx_positions SET status='open' WHERE wxid=? AND status='closing'", (wxid,))
            return _u("\u5e73\u4ed3\u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002")
        return None
    return _help_text()


# Ensure strings written with escaped code points are decoded at call sites.
__all__ = ["COMMAND", "init", "handle"]
