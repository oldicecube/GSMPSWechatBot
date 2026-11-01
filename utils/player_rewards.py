"""Atomic shared daily Minecraft rewards, independent of chat platform.

The event server is explicitly scoped to login/activity rewards. Only main-server
observations update the legacy total_time field; neither path touches matches.
"""
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from utils import sqlite_store as store

ZONE = ZoneInfo('Asia/Shanghai')


@dataclass(frozen=True)
class RewardPolicy:
    login_points: int
    points_per_minute: int
    active_cap: int
    tracks_main_time: bool = False


POLICIES = {
    'main': RewardPolicy(5, 1, 10, True),
    'event': RewardPolicy(20, 2, 50),
}


def _schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS minecraft_daily_rewards ('
                 'player_key TEXT NOT NULL, day TEXT NOT NULL, login_server TEXT, '
                 'login_points INTEGER NOT NULL DEFAULT 0, active_points INTEGER NOT NULL DEFAULT 0, '
                 'PRIMARY KEY(player_key,day))')
    conn.execute('CREATE TABLE IF NOT EXISTS minecraft_activity_minutes ('
                 'player_key TEXT NOT NULL, server_id TEXT NOT NULL, day TEXT NOT NULL, '
                 'seconds REAL NOT NULL DEFAULT 0, settled_minutes INTEGER NOT NULL DEFAULT 0, '
                 'PRIMARY KEY(player_key,server_id,day))')
    conn.execute('CREATE TABLE IF NOT EXISTS minecraft_monitor_sessions ('
                 'server_id TEXT NOT NULL, player_key TEXT NOT NULL, player_name TEXT NOT NULL, '
                 'last_seen REAL NOT NULL, online INTEGER NOT NULL DEFAULT 1, '
                 'PRIMARY KEY(server_id,player_key))')


def _player(conn, name):
    rows = conn.execute('SELECT player_name,bind_user,last_login_date,login_points_today,'
                        'online_time_points_today FROM player_stats WHERE player_name=? COLLATE NOCASE',
                        (name,)).fetchall()
    # A missing/ambiguous identity must not create an event-only /player entry.
    return rows[0] if len(rows) == 1 else None


def ensure_primary_player(name, first_join_at):
    """Only primary observations register players for existing player features."""
    with store._LOCK, closing(store._connect()) as conn, conn:
        conn.execute('INSERT INTO player_stats(player_name,first_join_at) SELECT ?,? '
                     'WHERE NOT EXISTS(SELECT 1 FROM player_stats WHERE player_name=? COLLATE NOCASE)',
                     (name, first_join_at, name))
        row = _player(conn, name)
        if row:
            conn.execute('UPDATE player_stats SET first_join_at=coalesce(first_join_at,?) WHERE player_name=?',
                         (first_join_at, row[0]))
        return row[0] if row else name


def _owner(conn, row):
    if not row or not row[1]:
        return ''
    from utils.account_identity import _schema as identity_schema, _find
    identity_schema(conn)
    identity = _find(conn, row[1])
    return str(identity['canonical']) if identity else ''


def _daily(conn, key, day, row):
    # Adopt today's already-issued legacy rewards once; never reset the shared
    # activity total on a server change or after the first login award.
    old_day = row and row[2] == day
    conn.execute('INSERT OR IGNORE INTO minecraft_daily_rewards '
                 '(player_key,day,login_server,login_points,active_points) VALUES(?,?,?,?,?)',
                 (key, day, 'legacy' if old_day and row[3] else None,
                  int(row[3] or 0) if old_day else 0, int(row[4] or 0) if old_day else 0))
    return conn.execute('SELECT login_points,active_points FROM minecraft_daily_rewards '
                        'WHERE player_key=? AND day=?', (key, day)).fetchone()


def _credit(conn, owner, amount, stamp):
    if owner and amount > 0:
        conn.execute('INSERT INTO user_points(wxid,points,created_at,updated_at) VALUES(?,?,?,?) '
                     'ON CONFLICT(wxid) DO UPDATE SET points=points+excluded.points,updated_at=excluded.updated_at',
                     (owner, int(amount), stamp, stamp))


def _mirror(conn, key, row, now):
    if not row:
        return
    day = datetime.fromtimestamp(now, ZONE).date().isoformat()
    total = _daily(conn, key, day, row)
    conn.execute('UPDATE player_stats SET last_login_date=?,login_points_today=?,online_time_points_today=? '
                 'WHERE player_name=?', (day, *total, row[0]))


def _activity(conn, key, server_id, start, end, row, owner):
    """Split elapsed observed time at Shanghai midnight; settle full minutes."""
    policy = POLICIES[server_id]
    awarded = 0
    if end <= start:
        return awarded
    if policy.tracks_main_time and row:
        conn.execute('UPDATE player_stats SET total_time=total_time+? WHERE player_name=?',
                     (max(0, int(end) - int(start)), row[0]))
    while start < end:
        local = datetime.fromtimestamp(start, ZONE)
        boundary = (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp()
        stop = min(end, boundary)
        day = local.date().isoformat()
        _daily(conn, key, day, row)
        conn.execute('INSERT INTO minecraft_activity_minutes(player_key,server_id,day,seconds) VALUES(?,?,?,?) '
                     'ON CONFLICT(player_key,server_id,day) DO UPDATE SET seconds=seconds+excluded.seconds',
                     (key, server_id, day, stop - start))
        seconds, settled = conn.execute('SELECT seconds,settled_minutes FROM minecraft_activity_minutes '
                                        'WHERE player_key=? AND server_id=? AND day=?', (key, server_id, day)).fetchone()
        minutes = int((seconds + 1e-6) // 60)
        active = conn.execute('SELECT active_points FROM minecraft_daily_rewards WHERE player_key=? AND day=?',
                              (key, day)).fetchone()[0]
        delta = min(max(0, minutes - settled) * policy.points_per_minute, max(0, policy.active_cap - active)) if owner else 0
        conn.execute('UPDATE minecraft_activity_minutes SET settled_minutes=? '
                     'WHERE player_key=? AND server_id=? AND day=?', (minutes, key, server_id, day))
        if delta:
            conn.execute('UPDATE minecraft_daily_rewards SET active_points=active_points+? WHERE player_key=? AND day=?',
                         (delta, key, day))
            _credit(conn, owner, delta, datetime.fromtimestamp(stop, ZONE).isoformat())
            awarded += delta
        start = stop
    return awarded


def observe(server_id, players, now, *, baseline=False, max_gap_seconds=60):
    """Apply one successful snapshot, atomically with all point balances.

    Baseline/reconnect observations never credit unobserved downtime. Repeating
    a snapshot at the same timestamp cannot issue another login/minute reward.
    """
    if server_id not in POLICIES:
        raise ValueError('Unknown reward server')
    policy = POLICIES[server_id]
    current = {str(name).strip().casefold(): str(name).strip() for name in players if str(name).strip()}
    result = {}
    stamp = datetime.fromtimestamp(now, ZONE).isoformat()
    today = datetime.fromtimestamp(now, ZONE).date().isoformat()
    with store._LOCK, closing(store._connect()) as conn, conn:
        _schema(conn)
        conn.commit()
        conn.execute('BEGIN IMMEDIATE')
        previous = {key: (name, last, bool(online)) for key, name, last, online in conn.execute(
            'SELECT player_key,player_name,last_seen,online FROM minecraft_monitor_sessions WHERE server_id=?', (server_id,))}
        for key in sorted(set(current) | {k for k, value in previous.items() if value[2]}):
            name = current.get(key) or previous[key][0]
            row = _player(conn, name)
            owner = _owner(conn, row)
            past = previous.get(key)
            login = active = 0
            if past and now < past[1]:
                continue  # A backwards clock must not replay already-settled intervals.
            if past and past[2] and not baseline and 0 <= now - past[1] <= max_gap_seconds:
                active = _activity(conn, key, server_id, past[1], now, row, owner)
            joined = key in current and (baseline or not past or not past[2])
            if joined and owner:
                login_today, _ = _daily(conn, key, today, row)
                if login_today == 0:
                    login = policy.login_points
                    conn.execute('UPDATE minecraft_daily_rewards SET login_server=?,login_points=? WHERE player_key=? AND day=?',
                                 (server_id, login, key, today))
                    _credit(conn, owner, login, stamp)
            conn.execute('INSERT INTO minecraft_monitor_sessions VALUES(?,?,?,?,?) '
                         'ON CONFLICT(server_id,player_key) DO UPDATE SET player_name=excluded.player_name,'
                         'last_seen=excluded.last_seen,online=excluded.online',
                         (server_id, key, name, now, int(key in current)))
            _mirror(conn, key, row, now)
            result[name] = {'login_points': login, 'active_points': active, 'bound': bool(owner)}
    return result
