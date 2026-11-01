import json
import os
import sqlite3
import threading
from datetime import datetime


BASE_DIR = os.environ.get('MAIBOT_INSTANCE_DIR') or os.path.dirname(os.path.dirname(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "bot.sqlite3")

_LOCK = threading.RLock()


def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS json_documents (
            name TEXT PRIMARY KEY,
            data TEXT NOT NULL,
            source_path TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS player_stats (
            player_name TEXT PRIMARY KEY,
            total_time INTEGER NOT NULL DEFAULT 0,
            bind_user TEXT,
            first_join_at TEXT,
            last_login_date TEXT,
            login_points_today INTEGER NOT NULL DEFAULT 0,
            online_time_points_today INTEGER NOT NULL DEFAULT 0,
            daily_theroom_points INTEGER NOT NULL DEFAULT 0,
            luckypillar_times INTEGER NOT NULL DEFAULT 0,
            battlepaint_times INTEGER NOT NULL DEFAULT 0,
            collapse_times INTEGER NOT NULL DEFAULT 0,
            theroom_stats_date TEXT,
            theroom_match_count_today INTEGER NOT NULL DEFAULT 0,
            theroom_luckypillar_today INTEGER NOT NULL DEFAULT 0,
            theroom_miner_chaos_today INTEGER NOT NULL DEFAULT 0,
            last_fortune_at TEXT
        )
        """
    )
    _ensure_player_stats_columns(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_points (
            wxid TEXT PRIMARY KEY,
            points REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_sign_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sleep_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            wxid TEXT NOT NULL,
            nickname TEXT NOT NULL DEFAULT '',
            group_id TEXT NOT NULL DEFAULT '',
            start_at TEXT NOT NULL,
            end_at TEXT,
            duration_seconds REAL,
            score REAL NOT NULL DEFAULT 0.0,
            points_awarded REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'active',
            invalid_reason TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_sleep_records_user_created
        ON sleep_records(wxid, created_at DESC)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_lottery_bets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draw_date TEXT NOT NULL,
            wxid TEXT NOT NULL,
            nickname TEXT NOT NULL DEFAULT '',
            number TEXT NOT NULL,
            amount REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            prize REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            settled_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_daily_lottery_bets_user_date
        ON daily_lottery_bets(draw_date, wxid)
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_lottery_draws (
            draw_date TEXT PRIMARY KEY,
            winning_number TEXT NOT NULL,
            total_bet REAL NOT NULL DEFAULT 0.0,
            total_prize REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS daily_lottery_fund (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            balance REAL NOT NULL DEFAULT 500.0,
            initial_seed REAL NOT NULL DEFAULT 500.0,
            seed_debt REAL NOT NULL DEFAULT 500.0,
            total_house_fee REAL NOT NULL DEFAULT 0.0,
            updated_at TEXT NOT NULL
        )
        """
    )
    fund_columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_lottery_fund)").fetchall()}
    for name, definition in {
        "month_key": "TEXT NOT NULL DEFAULT ''",
        "month_bet": "REAL NOT NULL DEFAULT 0.0",
        "month_prize": "REAL NOT NULL DEFAULT 0.0",
        "month_house_fee": "REAL NOT NULL DEFAULT 0.0",
        "month_participation": "REAL NOT NULL DEFAULT 0.0",
        "month_protection": "REAL NOT NULL DEFAULT 0.0",
    }.items():
        if name not in fund_columns:
            conn.execute(f"ALTER TABLE daily_lottery_fund ADD COLUMN {name} {definition}")

    bet_columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_lottery_bets)").fetchall()}
    for name, definition in {
        "option_label": "TEXT NOT NULL DEFAULT ''",
        "base_odds": "REAL NOT NULL DEFAULT 2.0",
        "locked_odds": "REAL NOT NULL DEFAULT 0.0",
        "participation_rebate": "REAL NOT NULL DEFAULT 0.0",
        "protection_rebate": "REAL NOT NULL DEFAULT 0.0",
    }.items():
        if name not in bet_columns:
            conn.execute(f"ALTER TABLE daily_lottery_bets ADD COLUMN {name} {definition}")

    draw_columns = {row[1] for row in conn.execute("PRAGMA table_info(daily_lottery_draws)").fetchall()}
    for name, definition in {
        "winning_options": "TEXT NOT NULL DEFAULT ''",
        "option_count": "INTEGER NOT NULL DEFAULT 6",
        "payout_budget": "REAL NOT NULL DEFAULT 0.0",
        "participation_budget": "REAL NOT NULL DEFAULT 0.0",
        "protection_budget": "REAL NOT NULL DEFAULT 0.0",
    }.items():
        if name not in draw_columns:
            conn.execute(f"ALTER TABLE daily_lottery_draws ADD COLUMN {name} {definition}")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_lottery_options (
            draw_date TEXT NOT NULL,
            option_id TEXT NOT NULL,
            label TEXT NOT NULL,
            base_odds REAL NOT NULL DEFAULT 2.0,
            total_stake REAL NOT NULL DEFAULT 0.0,
            demand_factor REAL NOT NULL DEFAULT 1.0,
            locked_odds REAL NOT NULL DEFAULT 0.0,
            is_winner INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY(draw_date, option_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lottery_player_streaks (
            wxid TEXT PRIMARY KEY,
            loss_streak INTEGER NOT NULL DEFAULT 0,
            last_bet_date TEXT,
            last_win_date TEXT,
            protection_available INTEGER NOT NULL DEFAULT 0,
            protection_used_date TEXT,
            updated_at TEXT NOT NULL
        )
    """)
    return conn


def _ensure_player_stats_columns(conn):
    """为既有 player_stats 表补齐 The Room 当日统计字段。"""
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(player_stats)").fetchall()
    }
    migrations = {
        "theroom_stats_date": "TEXT",
        "theroom_match_count_today": "INTEGER NOT NULL DEFAULT 0",
        "theroom_luckypillar_today": "INTEGER NOT NULL DEFAULT 0",
        "theroom_miner_chaos_today": "INTEGER NOT NULL DEFAULT 0",
    }
    for name, definition in migrations.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE player_stats ADD COLUMN {name} {definition}")


def _clone_default(default):
    if isinstance(default, list):
        return list(default)
    if isinstance(default, dict):
        return dict(default)
    return default


def _document_name(path_or_name):
    normalized = str(path_or_name or "").replace("\\", "/")
    base = os.path.abspath(BASE_DIR).replace("\\", "/")

    if os.path.isabs(normalized):
        try:
            rel = os.path.relpath(normalized, BASE_DIR)
            return rel.replace("\\", "/")
        except ValueError:
            return normalized

    if normalized.startswith(base + "/"):
        return os.path.relpath(normalized, BASE_DIR).replace("\\", "/")

    return normalized.strip("/")


def load_document(path_or_name, default=None):
    name = _document_name(path_or_name)

    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT data FROM json_documents WHERE name = ?",
                (name,),
            ).fetchone()

            if row:
                try:
                    return json.loads(row[0])
                except json.JSONDecodeError:
                    return _clone_default(default)

            migrated = _load_legacy_json(path_or_name, default)
            save_document(path_or_name, migrated)
            return migrated


def save_document(path_or_name, data):
    name = _document_name(path_or_name)
    now = datetime.now().isoformat()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    source_path = _source_path(path_or_name)

    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO json_documents (name, data, source_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    data = excluded.data,
                    source_path = excluded.source_path,
                    updated_at = excluded.updated_at
                """,
                (name, payload, source_path, now, now),
            )


def migrate_json_file(path, default=None, overwrite=False):
    name = _document_name(path)

    with _LOCK:
        with _connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM json_documents WHERE name = ?",
                (name,),
            ).fetchone()

        if exists and not overwrite:
            return False

        data = _load_legacy_json(path, default)
        save_document(path, data)
        return True


def migrate_legacy_storage(overwrite=False):
    """迁移旧版 JSON 数据到 SQLite 表结构（仅 player_stats）。"""
    migrated = []

    # player_stats → player_stats 表
    count = player_stats_migrate_from_json()
    if count > 0:
        migrated.append("data/player_stats.json → player_stats 表")

    return migrated


def _load_legacy_json(path_or_name, default):
    path = _source_path(path_or_name)
    if not path or not os.path.exists(path):
        return _clone_default(default)

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _clone_default(default)


def _source_path(path_or_name):
    raw = str(path_or_name or "")
    if os.path.isabs(raw):
        return raw
    return os.path.join(BASE_DIR, raw)


# ============================================================
# player_stats 表专用 CRUD（真正的 SQLite 列式存储）
# ============================================================

PLAYER_STATS_COLUMNS = [
    "player_name", "total_time", "bind_user", "first_join_at",
    "last_login_date", "login_points_today", "online_time_points_today",
    "daily_theroom_points", "luckypillar_times", "battlepaint_times",
    "collapse_times", "theroom_stats_date", "theroom_match_count_today",
    "theroom_luckypillar_today", "theroom_miner_chaos_today", "last_fortune_at",
]
PLAYER_STATS_SELECT = ", ".join(PLAYER_STATS_COLUMNS)


def _row_to_dict(row):
    """将 SQL 行转为字典"""
    if not row:
        return None
    return dict(zip(PLAYER_STATS_COLUMNS, row))


def player_stats_get(player_name):
    """获取单个玩家的统计数据，不存在返回 None"""
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                f"SELECT {PLAYER_STATS_SELECT} FROM player_stats WHERE player_name = ?",
                (player_name,),
            ).fetchone()
    return _row_to_dict(row)


def player_stats_all():
    """获取所有玩家统计数据，返回 {player_name: {...}, ...} 字典"""
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(f"SELECT {PLAYER_STATS_SELECT} FROM player_stats").fetchall()
    result = {}
    for row in rows:
        d = _row_to_dict(row)
        name = d.pop("player_name")
        result[name] = d
    return result


def player_stats_top(limit=5):
    """获取总在线时长前 N 名，返回 [(player_name, stats_dict), ...]"""
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                f"SELECT {PLAYER_STATS_SELECT} FROM player_stats ORDER BY total_time DESC LIMIT ?",
                (limit,),
            ).fetchall()
    result = []
    for row in rows:
        d = _row_to_dict(row)
        name = d.pop("player_name")
        result.append((name, d))
    return result


def player_stats_upsert(player_name, **kwargs):
    """
    插入或更新玩家统计。
    只更新传入的关键字参数对应的列。
    """
    valid = {k: v for k, v in kwargs.items() if k in PLAYER_STATS_COLUMNS and k != "player_name"}
    if not valid:
        return

    columns = ["player_name"] + list(valid.keys())
    placeholders = ["?"] * len(columns)
    values = [player_name] + list(valid.values())

    set_clause = ", ".join(f"{k} = excluded.{k}" for k in valid.keys())

    with _LOCK:
        with _connect() as conn:
            conn.execute(
                f"""
                INSERT INTO player_stats ({', '.join(columns)})
                VALUES ({', '.join(placeholders)})
                ON CONFLICT(player_name) DO UPDATE SET {set_clause}
                """,
                values,
            )


def player_stats_ensure(player_name, first_join_at=None):
    """
    确保玩家记录存在（不存在则创建默认记录）。
    返回该玩家的 stats 字典。
    """
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO player_stats (player_name)
                VALUES (?)
                """,
                (player_name,),
            )
            if first_join_at:
                conn.execute(
                    "UPDATE player_stats SET first_join_at = ? WHERE player_name = ? AND first_join_at IS NULL",
                    (first_join_at, player_name),
                )
            row = conn.execute(
                f"SELECT {PLAYER_STATS_SELECT} FROM player_stats WHERE player_name = ?",
                (player_name,),
            ).fetchone()
    return _row_to_dict(row)


def player_stats_increment(player_name, field, delta):
    """原子性增加某个数值字段"""
    if field not in PLAYER_STATS_COLUMNS or field == "player_name":
        return
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                f"UPDATE player_stats SET {field} = {field} + ? WHERE player_name = ?",
                (int(delta), player_name),
            )


def player_stats_update(player_name, **kwargs):
    """更新玩家指定字段（非原子增量用）"""
    valid = {k: v for k, v in kwargs.items() if k in PLAYER_STATS_COLUMNS and k != "player_name"}
    if not valid:
        return
    set_clause = ", ".join(f"{k} = ?" for k in valid.keys())
    values = list(valid.values()) + [player_name]
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                f"UPDATE player_stats SET {set_clause} WHERE player_name = ?",
                values,
            )


def player_stats_delete(player_name):
    """删除玩家记录"""
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "DELETE FROM player_stats WHERE player_name = ?",
                (player_name,),
            )


def player_stats_migrate_from_json():
    """
    将旧的 player_stats JSON 数据迁移到新的 player_stats 表。
    使用 INSERT OR IGNORE：已存在的记录不会被覆盖。
    迁移成功后删除旧 JSON 文件，防止重复迁移。
    """
    legacy_name = "data/player_stats.json"
    legacy_path = os.path.join(BASE_DIR, legacy_name)

    # 1. 从旧 JSON 文件读取
    old_data = _load_legacy_json(legacy_name, default={})
    if not isinstance(old_data, dict) or not old_data:
        print("[SQLITE] player_stats 迁移：无旧数据")
        return 0

    count = 0
    for player_name, stats in old_data.items():
        if not isinstance(stats, dict):
            continue
        # 构建列值
        col_values = {"player_name": player_name}
        for col in PLAYER_STATS_COLUMNS:
            if col == "player_name":
                continue
            val = stats.get(col, 0 if col in (
                "total_time", "login_points_today", "online_time_points_today",
                "daily_theroom_points", "luckypillar_times", "battlepaint_times",
                "collapse_times", "theroom_match_count_today",
                "theroom_luckypillar_today", "theroom_miner_chaos_today",
            ) else None)
            col_values[col] = val

        columns = list(col_values.keys())
        placeholders = ["?"] * len(columns)
        values = list(col_values.values())

        with _LOCK:
            with _connect() as conn:
                conn.execute(
                    f"""
                    INSERT OR IGNORE INTO player_stats ({', '.join(columns)})
                    VALUES ({', '.join(placeholders)})
                    """,
                    values,
                )
        count += 1

    # 2. 删除旧的 json_documents 记录
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "DELETE FROM json_documents WHERE name = ?",
                (legacy_name,),
            )

    # 3. 删除旧的 JSON 文件，防止下次启动重复迁移覆盖数据
    if os.path.exists(legacy_path):
        try:
            os.remove(legacy_path)
            print(f"[SQLITE] 已删除旧 JSON 文件: {legacy_path}")
        except OSError as e:
            print(f"[SQLITE] 删除旧 JSON 文件失败: {e}")

    print(f"[SQLITE] player_stats 迁移完成：{count} 条记录")
    return count


# ============================================================
# user_points 表 CRUD
# ============================================================

def user_points_get(wxid):
    """获取用户积分，不存在返回 None"""
    from utils.account_identity import bound_user
    wxid = bound_user(wxid)
    if not wxid:
        return None
    wxid = str(wxid).strip()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT points, created_at, updated_at, last_sign_at FROM user_points WHERE wxid = ?",
                (wxid,),
            ).fetchone()
    if not row:
        return None
    return {"points": row[0], "created_at": row[1], "updated_at": row[2], "last_sign_at": row[3]}


def user_points_add(wxid, amount):
    """增加用户积分，返回新积分值。用户不存在则自动创建"""
    from utils.account_identity import bound_user
    wxid = bound_user(wxid)
    if not wxid:
        return 0.0
    wxid = str(wxid).strip()
    amount = float(amount)
    now = datetime.now().isoformat()

    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO user_points (wxid, points, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(wxid) DO UPDATE SET
                    points = points + ?,
                    updated_at = ?
                """,
                (wxid, amount, now, now, amount, now),
            )
            row = conn.execute(
                "SELECT points FROM user_points WHERE wxid = ?", (wxid,)
            ).fetchone()
    return float(row[0]) if row else 0.0



def user_points_try_spend(wxid, amount):
    """Atomically spend points only when the user has sufficient balance."""
    from utils.account_identity import bound_user
    wxid = bound_user(wxid)
    if not wxid:
        return False, 0.0
    wxid = str(wxid or "").strip()
    amount = float(amount)
    if not wxid or amount < 0:
        return False, 0.0
    now = datetime.now().isoformat()
    with _LOCK:
        with _connect() as conn:
            cursor = conn.execute(
                "UPDATE user_points SET points = points - ?, updated_at = ? WHERE wxid = ? AND points >= ?",
                (amount, now, wxid, amount),
            )
            row = conn.execute("SELECT points FROM user_points WHERE wxid = ?", (wxid,)).fetchone()
    return bool(cursor.rowcount), (float(row[0]) if row else 0.0)

def user_points_set(wxid, amount):
    """设置用户积分（覆盖），返回新积分值"""
    from utils.account_identity import bound_user
    wxid = bound_user(wxid)
    if not wxid:
        return 0.0
    wxid = str(wxid).strip()
    amount = float(amount)
    now = datetime.now().isoformat()

    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO user_points (wxid, points, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(wxid) DO UPDATE SET
                    points = ?,
                    updated_at = ?
                """,
                (wxid, amount, now, now, amount, now),
            )
    return amount


def user_points_set_sign(wxid):
    """设置签到时间为现在"""
    from utils.account_identity import bound_user
    wxid = bound_user(wxid)
    if not wxid:
        return False
    wxid = str(wxid).strip()
    now = datetime.now().isoformat()

    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO user_points (wxid, points, created_at, updated_at, last_sign_at)
                VALUES (?, 0.0, ?, ?, ?)
                ON CONFLICT(wxid) DO UPDATE SET
                    last_sign_at = ?,
                    updated_at = ?
                """,
                (wxid, now, now, now, now, now),
            )


def user_points_get_sign_date(wxid):
    """获取上次签到日期（date 对象或 None）"""
    from utils.account_identity import bound_user
    wxid = bound_user(wxid)
    if not wxid:
        return None
    wxid = str(wxid).strip()
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT last_sign_at FROM user_points WHERE wxid = ?", (wxid,)
            ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(str(row[0])).date()
    except (TypeError, ValueError):
        return None


def user_points_all_wxids():
    """获取所有有积分记录的用户 wxid 列表"""
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute("SELECT wxid FROM user_points").fetchall()
    from utils.account_identity import bound_user
    return sorted({key for r in rows if (key := bound_user(r[0]))})


# ============================================================
# sleep records
# ============================================================

def sleep_get_active(wxid):
    from utils.account_identity import canonical_user
    wxid = canonical_user(wxid)
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT id, wxid, nickname, group_id, start_at, end_at, duration_seconds, "
                "score, points_awarded, status, invalid_reason, created_at, updated_at "
                "FROM sleep_records WHERE wxid=? AND status='active' ORDER BY id DESC LIMIT 1",
                (str(wxid or "").strip(),),
            ).fetchone()
    return _sleep_row(row) if row else None


def sleep_start(wxid, nickname, group_id, start_at=None):
    from utils.account_identity import canonical_user
    wxid = canonical_user(wxid)
    now = start_at or datetime.now().isoformat()
    with _LOCK:
        with _connect() as conn:
            cursor = conn.execute(
                "INSERT INTO sleep_records "
                "(wxid, nickname, group_id, start_at, status, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'active', ?, ?)",
                (str(wxid or "").strip(), str(nickname or ""), str(group_id or ""), now, now, now),
            )
            return int(cursor.lastrowid)


def sleep_finish(record_id, status, end_at, duration_seconds, score=0.0,
                 points_awarded=0.0, invalid_reason=""):
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "UPDATE sleep_records SET status=?, end_at=?, duration_seconds=?, score=?, "
                "points_awarded=?, invalid_reason=?, updated_at=? "
                "WHERE id=? AND status='active'",
                (str(status), str(end_at), float(duration_seconds), float(score),
                 float(points_awarded), str(invalid_reason or ""), datetime.now().isoformat(), int(record_id)),
            )
    return sleep_get_by_id(record_id)


def sleep_start_once(wxid, nickname, group_id, start_at, period_start, period_end):
    """One check-in across bound adapters and concurrent group messages."""
    from contextlib import closing
    from utils.account_identity import bound_user
    with _LOCK:
        uid = bound_user(wxid)
        if not uid:
            return None
        with closing(_connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            if conn.execute("SELECT 1 FROM sleep_records WHERE wxid=? AND (status='active' OR (start_at>=? AND start_at<?))", (uid,period_start,period_end)).fetchone():
                return None
            return conn.execute('INSERT INTO sleep_records(wxid,nickname,group_id,start_at,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                (uid,str(nickname),str(group_id),start_at,'active',start_at,start_at)).lastrowid


def sleep_settle_once(record_id, status, end_at, duration_seconds, score=0.0,
                      points_awarded=0.0, invalid_reason='', points_delta=0.0):
    """State transition and shared wallet update commit together, once only."""
    from contextlib import closing
    from utils.account_identity import bound_user
    if status not in {'completed','invalid'}:
        raise ValueError('invalid sleep settlement')
    with _LOCK:
        with closing(_connect()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute("SELECT wxid FROM sleep_records WHERE id=? AND status='active'", (int(record_id),)).fetchone()
            if not row:
                return False
            uid=bound_user(row[0])
            if not uid:
                return False
            conn.execute("UPDATE sleep_records SET status=?,end_at=?,duration_seconds=?,score=?,points_awarded=?,invalid_reason=?,updated_at=? WHERE id=? AND status='active'",
                (status,end_at,float(duration_seconds),float(score),float(points_awarded),str(invalid_reason),end_at,int(record_id)))
            conn.execute('INSERT INTO user_points(wxid,points,created_at,updated_at) VALUES(?,?,?,?) '
                'ON CONFLICT(wxid) DO UPDATE SET points=points+excluded.points,updated_at=excluded.updated_at',
                (uid,float(points_delta),end_at,end_at))
            return True


def sleep_get_by_id(record_id):
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT id, wxid, nickname, group_id, start_at, end_at, duration_seconds, "
                "score, points_awarded, status, invalid_reason, created_at, updated_at "
                "FROM sleep_records WHERE id=?", (int(record_id),),
            ).fetchone()
    return _sleep_row(row) if row else None


def sleep_recent(wxid, limit=10):
    from utils.account_identity import canonical_user
    wxid = canonical_user(wxid)
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, wxid, nickname, group_id, start_at, end_at, duration_seconds, "
                "score, points_awarded, status, invalid_reason, created_at, updated_at "
                "FROM sleep_records WHERE wxid=? ORDER BY id DESC LIMIT ?",
                (str(wxid or "").strip(), max(1, min(int(limit or 10), 50))),
            ).fetchall()
    return [_sleep_row(row) for row in rows]


def _sleep_row(row):
    return {
        "id": int(row[0]), "wxid": row[1], "nickname": row[2] or "", "group_id": row[3] or "",
        "start_at": row[4], "end_at": row[5], "duration_seconds": float(row[6] or 0),
        "score": float(row[7] or 0), "points_awarded": float(row[8] or 0), "status": row[9],
        "invalid_reason": row[10] or "", "created_at": row[11], "updated_at": row[12],
    }


# ============================================================
# daily lottery
# ============================================================

def lottery_get_draw(draw_date):
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT draw_date, winning_number, total_bet, total_prize, created_at, "
                "winning_options, option_count, payout_budget, participation_budget, protection_budget "
                "FROM daily_lottery_draws WHERE draw_date = ?",
                (str(draw_date),),
            ).fetchone()
    if not row:
        return None
    return {
        "draw_date": row[0],
        "winning_number": row[1],
        "total_bet": float(row[2] or 0),
        "total_prize": float(row[3] or 0),
        "created_at": row[4],
        "winning_options": json.loads(row[5] or "[]") if row[5] else [],
        "option_count": int(row[6] or 6),
        "payout_budget": float(row[7] or 0),
        "participation_budget": float(row[8] or 0),
        "protection_budget": float(row[9] or 0),
    }


def lottery_create_draw(draw_date, winning_number, total_bet=0.0, total_prize=0.0,
                        winning_options=None, option_count=6, payout_budget=0.0,
                        participation_budget=0.0, protection_budget=0.0):
    """Create a draw exactly once. Returns False when the date already exists."""
    draw_date = str(draw_date)
    now = datetime.now().isoformat()
    with _LOCK:
        with _connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO daily_lottery_draws"
                "(draw_date, winning_number, total_bet, total_prize, created_at, "
                "winning_options, option_count, payout_budget, participation_budget, protection_budget) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (draw_date, str(winning_number), float(total_bet), float(total_prize), now,
                 json.dumps(list(winning_options or []), ensure_ascii=False), int(option_count or 6),
                 float(payout_budget or 0), float(participation_budget or 0), float(protection_budget or 0)),
            )
            created = bool(cursor.rowcount)
    return created


def lottery_update_draw_totals(draw_date, total_bet, total_prize):
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "UPDATE daily_lottery_draws SET total_bet = ?, total_prize = ? "
                "WHERE draw_date = ?",
                (float(total_bet), float(total_prize), str(draw_date)),
            )


def lottery_add_bet(draw_date, wxid, nickname, number, amount, created_at=None,
                     option_label="", base_odds=2.0, locked_odds=0.0,
                     participation_rebate=0.0, protection_rebate=0.0):
    from utils.account_identity import bound_user
    wxid = bound_user(wxid)
    if not wxid:
        raise ValueError('请先绑定游戏账号后使用积分系统')
    draw_date = str(draw_date)
    wxid = str(wxid or "").strip()
    number = str(number or "").strip()
    amount = float(amount)
    now = created_at or datetime.now().isoformat()
    with _LOCK:
        with _connect() as conn:
            cursor = conn.execute(
                "INSERT INTO daily_lottery_bets"
                "(draw_date, wxid, nickname, number, amount, status, prize, created_at, "
                "option_label, base_odds, locked_odds, participation_rebate, protection_rebate) "
                "VALUES (?, ?, ?, ?, ?, 'pending', 0.0, ?, ?, ?, ?, ?, ?)",
                (draw_date, wxid, str(nickname or ""), number, amount, now,
                 str(option_label or ""), float(base_odds or 2.0), float(locked_odds or 0.0),
                 float(participation_rebate or 0.0), float(protection_rebate or 0.0)),
            )
            return int(cursor.lastrowid)


def lottery_user_bets(draw_date, wxid):
    from utils.account_identity import canonical_user
    wxid = canonical_user(wxid)
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, draw_date, wxid, nickname, number, amount, status, prize, "
                "created_at, settled_at, option_label, base_odds, locked_odds, "
                "participation_rebate, protection_rebate FROM daily_lottery_bets "
                "WHERE draw_date = ? AND wxid = ? ORDER BY id ASC",
                (str(draw_date), str(wxid or "").strip()),
            ).fetchall()
    return [_lottery_bet_row(r) for r in rows]


def lottery_pending_bets(draw_date):
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, draw_date, wxid, nickname, number, amount, status, prize, "
                "created_at, settled_at, option_label, base_odds, locked_odds, "
                "participation_rebate, protection_rebate FROM daily_lottery_bets "
                "WHERE draw_date = ? AND status = 'pending' ORDER BY id ASC",
                (str(draw_date),),
            ).fetchall()
    return [_lottery_bet_row(r) for r in rows]


def lottery_settle_bet(bet_id, status, prize, settled_at=None, locked_odds=None,
                        participation_rebate=None, protection_rebate=None):
    now = settled_at or datetime.now().isoformat()
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "UPDATE daily_lottery_bets SET status = ?, prize = ?, settled_at = ?, "
                "locked_odds = COALESCE(?, locked_odds), "
                "participation_rebate = COALESCE(?, participation_rebate), "
                "protection_rebate = COALESCE(?, protection_rebate) "
                "WHERE id = ? AND status = 'pending'",
                (str(status), float(prize), now, locked_odds, participation_rebate,
                 protection_rebate, int(bet_id)),
            )


def lottery_history(wxid, limit=10):
    from utils.account_identity import canonical_user
    wxid = canonical_user(wxid)
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT id, draw_date, wxid, nickname, number, amount, status, prize, "
                "created_at, settled_at, option_label, base_odds, locked_odds, "
                "participation_rebate, protection_rebate FROM daily_lottery_bets "
                "WHERE wxid = ? ORDER BY id DESC LIMIT ?",
                (str(wxid or "").strip(), max(1, min(int(limit or 10), 50))),
            ).fetchall()
    return [_lottery_bet_row(r) for r in rows]


def lottery_daily_rank(draw_date, limit=10):
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT wxid, MAX(nickname) AS nickname, SUM(amount) AS amount, COUNT(*) AS bets "
                "FROM daily_lottery_bets WHERE draw_date = ? "
                "GROUP BY wxid ORDER BY amount DESC, bets DESC LIMIT ?",
                (str(draw_date), max(1, min(int(limit or 10), 50))),
            ).fetchall()
    return [
        {"wxid": r[0], "nickname": r[1] or "", "amount": float(r[2] or 0), "bets": int(r[3] or 0)}
        for r in rows
    ]


def lottery_daily_total(draw_date):
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(amount), 0), COUNT(*) FROM daily_lottery_bets "
                "WHERE draw_date = ?",
                (str(draw_date),),
            ).fetchone()
    return float(row[0] or 0), int(row[1] or 0)


def lottery_get_fund():
    now = datetime.now().isoformat()
    month_key = datetime.now().strftime("%Y-%m")
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO daily_lottery_fund "
                "(id, balance, initial_seed, seed_debt, total_house_fee, month_key, updated_at) "
                "VALUES (1, 500.0, 500.0, 500.0, 0.0, ?, ?)",
                (month_key, now),
            )
            row = conn.execute(
                "SELECT balance, initial_seed, seed_debt, total_house_fee, month_key, "
                "month_bet, month_prize, month_house_fee, month_participation, month_protection, updated_at "
                "FROM daily_lottery_fund WHERE id=1"
            ).fetchone()
            if row and str(row[4] or "") != month_key:
                conn.execute(
                    "UPDATE daily_lottery_fund SET month_key=?, month_bet=0.0, "
                    "month_prize=0.0, month_house_fee=0.0, updated_at=? WHERE id=1",
                    (month_key, now),
                )
                row = conn.execute(
                    "SELECT balance, initial_seed, seed_debt, total_house_fee, month_key, "
                    "month_bet, month_prize, month_house_fee, month_participation, month_protection, updated_at "
                    "FROM daily_lottery_fund WHERE id=1"
                ).fetchone()
    return {
        "balance": float(row[0] or 0),
        "initial_seed": float(row[1] or 0),
        "seed_debt": float(row[2] or 0),
        "total_house_fee": float(row[3] or 0),
        "month_key": row[4] or month_key,
        "month_bet": float(row[5] or 0),
        "month_prize": float(row[6] or 0),
        "month_house_fee": float(row[7] or 0),
        "month_participation": float(row[8] or 0),
        "month_protection": float(row[9] or 0),
        "updated_at": row[10],
    }


def lottery_update_fund(balance=None, seed_debt=None, total_house_fee=None,
                        month_bet=None, month_prize=None, month_house_fee=None,
                        month_participation=None, month_protection=None):
    current = lottery_get_fund()
    values = {
        "balance": current["balance"] if balance is None else float(balance),
        "seed_debt": current["seed_debt"] if seed_debt is None else max(0.0, float(seed_debt)),
        "total_house_fee": current["total_house_fee"] if total_house_fee is None else float(total_house_fee),
        "month_bet": current["month_bet"] if month_bet is None else float(month_bet),
        "month_prize": current["month_prize"] if month_prize is None else float(month_prize),
        "month_house_fee": current["month_house_fee"] if month_house_fee is None else float(month_house_fee),
        "month_participation": current.get("month_participation", 0.0) if month_participation is None else float(month_participation),
        "month_protection": current.get("month_protection", 0.0) if month_protection is None else float(month_protection),
    }
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "UPDATE daily_lottery_fund SET balance=?, seed_debt=?, total_house_fee=?, "
                "month_bet=?, month_prize=?, month_house_fee=?, month_participation=?, month_protection=?, updated_at=? WHERE id=1",
                (values["balance"], values["seed_debt"], values["total_house_fee"],
                 values["month_bet"], values["month_prize"], values["month_house_fee"],
                 values["month_participation"], values["month_protection"], datetime.now().isoformat()),
            )
    return lottery_get_fund()

def lottery_get_options(draw_date):
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT draw_date, option_id, label, base_odds, total_stake, demand_factor, locked_odds, is_winner, created_at "
                "FROM daily_lottery_options WHERE draw_date=? ORDER BY option_id",
                (str(draw_date),),
            ).fetchall()
    return [{"draw_date": r[0], "option_id": r[1], "label": r[2], "base_odds": float(r[3] or 0),
             "total_stake": float(r[4] or 0), "demand_factor": float(r[5] or 1),
             "locked_odds": float(r[6] or 0), "is_winner": bool(r[7]), "created_at": r[8]} for r in rows]


def lottery_create_options(draw_date, options):
    now = datetime.now().isoformat()
    with _LOCK:
        with _connect() as conn:
            for item in options or []:
                conn.execute(
                    "INSERT OR IGNORE INTO daily_lottery_options "
                    "(draw_date, option_id, label, base_odds, created_at) VALUES (?, ?, ?, ?, ?)",
                    (str(draw_date), str(item.get("option_id") or ""), str(item.get("label") or ""),
                     float(item.get("base_odds") or 2.0), now),
                )
    return lottery_get_options(draw_date)


def lottery_update_option(option_date, option_id, *, total_stake=None, demand_factor=None,
                          locked_odds=None, is_winner=None):
    current = {item["option_id"]: item for item in lottery_get_options(option_date)}.get(str(option_id), {})
    values = {
        "total_stake": current.get("total_stake", 0.0) if total_stake is None else float(total_stake),
        "demand_factor": current.get("demand_factor", 1.0) if demand_factor is None else float(demand_factor),
        "locked_odds": current.get("locked_odds", 0.0) if locked_odds is None else float(locked_odds),
        "is_winner": int(current.get("is_winner", False) if is_winner is None else bool(is_winner)),
    }
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "UPDATE daily_lottery_options SET total_stake=?, demand_factor=?, locked_odds=?, is_winner=? WHERE draw_date=? AND option_id=?",
                (values["total_stake"], values["demand_factor"], values["locked_odds"], values["is_winner"], str(option_date), str(option_id)),
            )
    return lottery_get_options(option_date)


def lottery_get_player_streak(wxid):
    from utils.account_identity import canonical_user
    wxid = canonical_user(wxid)
    with _LOCK:
        with _connect() as conn:
            row = conn.execute("SELECT wxid, loss_streak, last_bet_date, last_win_date, protection_available, protection_used_date, updated_at FROM lottery_player_streaks WHERE wxid=?", (str(wxid or ""),)).fetchone()
    if not row:
        return {"wxid": str(wxid or ""), "loss_streak": 0, "last_bet_date": "", "last_win_date": "", "protection_available": False, "protection_used_date": "", "updated_at": ""}
    return {"wxid": row[0], "loss_streak": int(row[1] or 0), "last_bet_date": row[2] or "", "last_win_date": row[3] or "", "protection_available": bool(row[4]), "protection_used_date": row[5] or "", "updated_at": row[6]}


def lottery_update_player_streak(wxid, loss_streak=0, last_bet_date=None, last_win_date=None,
                                 protection_available=None, protection_used_date=None):
    from utils.account_identity import canonical_user
    wxid = canonical_user(wxid)
    now = datetime.now().isoformat()
    current = lottery_get_player_streak(wxid)
    values = (str(wxid or ""), int(loss_streak), last_bet_date if last_bet_date is not None else current["last_bet_date"],
              last_win_date if last_win_date is not None else current["last_win_date"],
              int(bool(current["protection_available"] if protection_available is None else protection_available)),
              protection_used_date if protection_used_date is not None else current["protection_used_date"], now)
    with _LOCK:
        with _connect() as conn:
            conn.execute("INSERT INTO lottery_player_streaks (wxid, loss_streak, last_bet_date, last_win_date, protection_available, protection_used_date, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(wxid) DO UPDATE SET loss_streak=excluded.loss_streak, last_bet_date=excluded.last_bet_date, last_win_date=excluded.last_win_date, protection_available=excluded.protection_available, protection_used_date=excluded.protection_used_date, updated_at=excluded.updated_at", values)
    return lottery_get_player_streak(wxid)


def _lottery_bet_row(row):
    return {
        "id": int(row[0]),
        "draw_date": row[1],
        "wxid": row[2],
        "nickname": row[3] or "",
        "number": row[4],
        "amount": float(row[5] or 0),
        "status": row[6],
        "prize": float(row[7] or 0),
        "created_at": row[8],
        "settled_at": row[9],
        "option_label": row[10] if len(row) > 10 else "",
        "base_odds": float(row[11] or 0) if len(row) > 11 else 0.0,
        "locked_odds": float(row[12] or 0) if len(row) > 12 else 0.0,
        "participation_rebate": float(row[13] or 0) if len(row) > 13 else 0.0,
        "protection_rebate": float(row[14] or 0) if len(row) > 14 else 0.0,
    }
