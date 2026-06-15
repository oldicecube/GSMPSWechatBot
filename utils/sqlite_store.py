import json
import os
import sqlite3
import threading
from datetime import datetime


BASE_DIR = os.path.dirname(os.path.dirname(__file__))
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
            last_fortune_at TEXT
        )
        """
    )
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
    return conn


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
    "collapse_times", "last_fortune_at",
]


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
                "SELECT * FROM player_stats WHERE player_name = ?",
                (player_name,),
            ).fetchone()
    return _row_to_dict(row)


def player_stats_all():
    """获取所有玩家统计数据，返回 {player_name: {...}, ...} 字典"""
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute("SELECT * FROM player_stats").fetchall()
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
                "SELECT * FROM player_stats ORDER BY total_time DESC LIMIT ?",
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
                "SELECT * FROM player_stats WHERE player_name = ?",
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
                "collapse_times",
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


def user_points_set(wxid, amount):
    """设置用户积分（覆盖），返回新积分值"""
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
    return [r[0] for r in rows]
