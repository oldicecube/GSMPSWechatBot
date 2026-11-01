"""Verified platform links; transport IDs and operator permissions stay separate."""
from contextlib import closing
import hashlib
import secrets

from utils import sqlite_store as store


def platform_user(uid):
    uid = str(uid or '').strip()
    return ('qq', uid[3:]) if uid.startswith('qq:') else ('wechat', uid)


def actor(context):
    raw = context.get('raw') or {}
    platform = context.get('platform') or raw.get('platform') or 'wechat'
    uid = str(context.get('user_id') or raw.get('user_id') or context.get('wxid') or raw.get('wxid') or '').strip()
    if platform == 'qq':
        uid = uid.removeprefix('qq:')
        if not uid.isascii() or not uid.isdecimal() or int(uid) <= 0:
            return ''
        return 'qq:' + uid
    return uid if platform == 'wechat' and not uid.startswith('qq:') else ''


def _schema(conn):
    conn.execute('CREATE TABLE IF NOT EXISTS account_display_names (uid TEXT PRIMARY KEY, nickname TEXT NOT NULL)')
    conn.execute('CREATE TABLE IF NOT EXISTS linked_accounts '
                 '(id TEXT PRIMARY KEY, player_name TEXT NOT NULL UNIQUE, canonical_uid TEXT NOT NULL UNIQUE)')
    conn.execute('CREATE TABLE IF NOT EXISTS account_links '
                 '(uid TEXT PRIMARY KEY, platform TEXT NOT NULL, account_id TEXT NOT NULL REFERENCES linked_accounts(id), '
                 'UNIQUE(account_id, platform))')



def _find(conn, uid):
    row = conn.execute('SELECT a.id,a.player_name,a.canonical_uid FROM linked_accounts a '
                       'JOIN account_links l ON a.id=l.account_id WHERE l.uid=?', (uid,)).fetchone()
    if row:
        members = [r[0] for r in conn.execute('SELECT uid FROM account_links WHERE account_id=? ORDER BY platform', (row[0],))]
        return dict(id=row[0], player=row[1], canonical=row[2], members=members)
    legacy = conn.execute('SELECT player_name FROM player_stats WHERE bind_user=? ORDER BY player_name', (uid,)).fetchall()
    if len(legacy) == 1:
        return dict(id='', player=legacy[0][0], canonical=uid, members=[uid])
    return None


def details(uid):
    uid = str(uid or '').strip()
    if not uid:
        return None
    with store._LOCK, closing(store._connect()) as conn:
        _schema(conn)
        return _find(conn, uid)


def canonical_user(uid):
    info = details(uid)
    return info['canonical'] if info else str(uid or '').strip()


def bound_user(uid):
    info = details(uid)
    return info['canonical'] if info else None


def _adopt(conn, info):
    if not info['id']:
        info['id'] = secrets.token_hex(16)
        conn.execute('INSERT INTO linked_accounts VALUES(?,?,?)', (info['id'], info['player'], info['canonical']))
        conn.execute('INSERT INTO account_links VALUES(?,?,?)', (info['canonical'], platform_user(info['canonical'])[0], info['id']))
    return info


def bind_game(uid, player):
    """Link by an exact game username, as requested; one account per platform."""
    if not uid:
        raise ValueError('无法识别当前平台账号')
    with store._LOCK, closing(store._connect()) as conn, conn:
        _schema(conn)
        conn.execute('BEGIN IMMEDIATE')
        old = _find(conn, uid)
        if old:
            if old['player'].casefold() != player.casefold():
                raise ValueError('账号已有游戏绑定，不能直接替换或合并其他玩家')
            return _adopt(conn, old)
        rows = conn.execute('SELECT player_name,bind_user FROM player_stats WHERE player_name=? COLLATE NOCASE', (player,)).fetchall()
        if len(rows) != 1:
            raise ValueError('游戏玩家不存在或名称有歧义')
        name, owner = rows[0]
        if owner and owner != uid:
            if platform_user(owner)[0] == platform_user(uid)[0]:
                raise ValueError('该游戏账号已经绑定了本平台的其他账号，不能覆盖')
            owner_info = _find(conn, owner)
            if not owner_info:
                raise ValueError('已有绑定记录存在歧义，需要先在游戏账户管理中核对')
            info = _adopt(conn, owner_info)
            if conn.execute('SELECT 1 FROM account_links WHERE account_id=? AND platform=?', (info['id'], platform_user(uid)[0])).fetchone():
                raise ValueError('此游戏账号已经绑定了本平台的其他账号')
            _merge_records(conn, uid, owner)
            conn.execute('INSERT INTO account_links VALUES(?,?,?)', (uid, platform_user(uid)[0], info['id']))
            return _find(conn, uid)
        conn.execute('UPDATE player_stats SET bind_user=? WHERE player_name=?', (uid, name))
        return _adopt(conn, dict(id='', player=name, canonical=uid, members=[uid]))




def _merge_records(conn, alias, canonical):
    """A one-time transactional merge, preserving source IDs on historical records."""
    conn.execute('INSERT INTO user_points(wxid,points,created_at,updated_at,last_sign_at) '
                 'SELECT ?,points,created_at,updated_at,last_sign_at FROM user_points WHERE wxid=? '
                 'ON CONFLICT(wxid) DO UPDATE SET points=points+excluded.points, '
                 'last_sign_at=max(coalesce(last_sign_at,\'\'),coalesce(excluded.last_sign_at,\'\'))', (canonical, alias))
    conn.execute('DELETE FROM user_points WHERE wxid=?', (alias,))
    for table in ('sleep_records', 'daily_lottery_bets'):
        columns = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        if 'origin_uid' not in columns:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN origin_uid TEXT')
        conn.execute(f'UPDATE {table} SET origin_uid=coalesce(origin_uid,wxid),wxid=? WHERE wxid=?', (canonical, alias))
    active = conn.execute("SELECT id FROM sleep_records WHERE wxid=? AND status='active' ORDER BY start_at,id",(canonical,)).fetchall()
    for row in active[1:]:
        conn.execute("UPDATE sleep_records SET status='invalid',invalid_reason='linked_duplicate_sleep' WHERE id=?",(row[0],))
    # Keep the stricter daily/losing-streak state rather than resetting eligibility.
    conn.execute('INSERT INTO lottery_player_streaks '
                 'SELECT ?,loss_streak,last_bet_date,last_win_date,protection_available,protection_used_date,updated_at '
                 'FROM lottery_player_streaks WHERE wxid=? ON CONFLICT(wxid) DO UPDATE SET '
                 'loss_streak=max(loss_streak,excluded.loss_streak),last_bet_date=max(coalesce(last_bet_date,\'\'),coalesce(excluded.last_bet_date,\'\')), '
                 'last_win_date=max(coalesce(last_win_date,\'\'),coalesce(excluded.last_win_date,\'\')), '
                 'protection_used_date=max(coalesce(protection_used_date,\'\'),coalesce(excluded.protection_used_date,\'\'))', (canonical, alias))
    conn.execute('DELETE FROM lottery_player_streaks WHERE wxid=?', (alias,))




def memory_aliases(platform, user_id):
    uid = 'qq:' + str(user_id) if platform == 'qq' else str(user_id)
    info = details(uid)
    members = info['members'] if info else [uid]
    return [(p, u, hashlib.md5((p + '_' + u).encode()).hexdigest()) for p, u in map(platform_user, members)]


def identity_hint(platform, user_id):
    uid = 'qq:' + str(user_id) if platform == 'qq' else str(user_id)
    info = details(uid)
    if not info or len(info['members']) < 2:
        return ''
    return ('[已绑定的关联身份] 游戏账号=' + info['player'] + '；平台账号=' + ', '.join(info['members'])
            + '。这些账号属于同一人，积分、游戏记录及人物记忆关联；当前平台会话仍独立，'
            '历史来源、管理员权限和@目标不可跨平台替换。')


def binding_peer_note(uid, nickname=''):
    """Best-effort cached native profile display; never make an LLM call to bind."""
    import json
    import sqlite3
    import time
    from pathlib import Path
    with store._LOCK, closing(store._connect()) as conn, conn:
        _schema(conn)
        if nickname:
            conn.execute('INSERT OR REPLACE INTO account_display_names VALUES(?,?)', (uid, str(nickname)[:120]))
        info = _find(conn, uid)
        peers = [p for p in (info or {}).get('members', []) if platform_user(p)[0] != platform_user(uid)[0]]
        names = dict(conn.execute('SELECT uid,nickname FROM account_display_names'))
    notes = []
    for peer in peers:
        platform, user_id = platform_user(peer)
        name, impression = names.get(peer, user_id), ''
        base = Path(store.BASE_DIR) / 'data'
        try:
            with closing(sqlite3.connect((base/'MaiBot.db').resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)) as db:
                row = db.execute('SELECT user_nickname,person_name,memory_points FROM person_info WHERE platform=? AND user_id=?', (platform,user_id)).fetchone()
                if row:
                    name = row[0] or row[1] or name
                    traits = json.loads(row[2] or '[]')
                    if isinstance(traits, list) and traits:
                        impression = str(traits[0])
            path = base/'a-memorix'/'metadata'/'metadata.db'
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=.2)) as db:
                pid = hashlib.md5((platform+'_'+user_id).encode()).hexdigest()
                row = db.execute('SELECT profile_text FROM person_profile_snapshots WHERE person_id=? AND expires_at>? ORDER BY updated_at DESC LIMIT 1', (pid,time.time())).fetchone()
                if row and row[0]:
                    section = ''
                    for line in row[0].splitlines():
                        if line.startswith('## '):
                            section = line[3:].strip()
                            continue
                        text = line.strip(' #*-')
                        if text and text != '暂无' and section in {'稳定了解','相处偏好','近期互动'}:
                            impression = text
                            break
        except (sqlite3.Error, ValueError, TypeError, OSError):
            pass  # Binding has already committed; absent/stale memories cannot fail it.
        clean = lambda x, limit: ' '.join(str(x).split())[:limit]
        notes.append(('QQ' if platform=='qq' else '微信')+'：'+clean(name,80)+'；印象：'+(clean(impression,90) if impression else '暂无足够印象'))
    return ('\n已关联另一侧账号：'+'；'.join(notes)) if notes else ''


def linked_person_ids(person_id):
    """Keep historical native person IDs, but retrieve verified aliases together."""
    if not person_id:
        return ['']
    with store._LOCK, closing(store._connect()) as conn:
        _schema(conn)
        rows = conn.execute('SELECT uid,account_id FROM account_links').fetchall()
        accounts = {account for uid,account in rows if hashlib.md5('_'.join(platform_user(uid)).encode()).hexdigest()==person_id}
        aliases = {hashlib.md5('_'.join(platform_user(uid)).encode()).hexdigest() for uid,account in rows if account in accounts}
        return [person_id, *sorted(aliases-{person_id})]


def linked_people(person_id):
    """Verified identity labels for existing native IDs, independent of nicknames."""
    ids = set(linked_person_ids(person_id))
    with store._LOCK, closing(store._connect()) as conn:
        _schema(conn)
        rows = conn.execute('SELECT uid FROM account_links').fetchall()
    result = []
    for (uid,) in rows:
        platform, user_id = platform_user(uid)
        pid = hashlib.md5((platform+'_'+user_id).encode()).hexdigest()
        if pid in ids:
            result.append(dict(platform=platform,user_id=user_id,person_id=pid))
    return result
