# -*- coding: utf-8 -*-
"""锦标赛数据库模型、迁移与 CRUD。

复用项目统一的 SQLite 数据库 (data/bot.sqlite3)，使用独立的
tourney_* 前缀表，不修改既有表结构，保证可向前扩展新的 game_type。

表结构：
    tourney_players          玩家基础信息（UUID 为主键标识）
    tourney_matches          对局原始数据
    tourney_match_players    对局内玩家表现 + 单局积分
    tourney_game_scores      每个小游戏的统计（派生，可重算）
    tourney_season_scores    赛季总榜（派生，可重算）

设计原则：
    - 仅保存原始对局数据，所有积分均可从原始数据重算。
    - game_scores / season_scores 为派生缓存表，recompute_all() 可全量重建。
    - game_type 为 TEXT 列，新增小游戏无需改表结构。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "bot.sqlite3")

_LOCK = threading.RLock()


# ============================================================
# 连接与迁移
# ============================================================

def _connect():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


_MIGRATIONS = [
    # ---------- players ----------
    """
    CREATE TABLE IF NOT EXISTS tourney_players (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        uuid       TEXT UNIQUE NOT NULL,
        name       TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tourney_players_name ON tourney_players(name)",

    # ---------- matches ----------
    """
    CREATE TABLE IF NOT EXISTS tourney_matches (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        minecraft_match_id INTEGER NOT NULL,
        game_type          TEXT NOT NULL,
        raw_fingerprint    TEXT,
        map                TEXT,
        player_count       INTEGER NOT NULL,
        created_at         TEXT NOT NULL,
        UNIQUE(minecraft_match_id, game_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tourney_matches_game ON tourney_matches(game_type)",

    # ---------- match_players ----------
    """
    CREATE TABLE IF NOT EXISTS tourney_match_players (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id     INTEGER NOT NULL,
        player_id    INTEGER NOT NULL,
        rank         INTEGER NOT NULL,
        survive_time INTEGER NOT NULL,
        match_score  REAL NOT NULL,
        UNIQUE(match_id, player_id),
        FOREIGN KEY (match_id)  REFERENCES tourney_matches(id) ON DELETE CASCADE,
        FOREIGN KEY (player_id) REFERENCES tourney_players(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tourney_mp_player ON tourney_match_players(player_id)",
    "CREATE INDEX IF NOT EXISTS idx_tourney_mp_match  ON tourney_match_players(match_id)",

    # ---------- game_scores（派生） ----------
    """
    CREATE TABLE IF NOT EXISTS tourney_game_scores (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id     INTEGER NOT NULL,
        game_type     TEXT NOT NULL,
        match_count   INTEGER NOT NULL DEFAULT 0,
        average_score REAL NOT NULL DEFAULT 0,
        total_score   REAL NOT NULL DEFAULT 0,
        rank          INTEGER,
        UNIQUE(player_id, game_type),
        FOREIGN KEY (player_id) REFERENCES tourney_players(id) ON DELETE CASCADE
    )
    """,

    # ---------- season_scores（派生） ----------
    """
    CREATE TABLE IF NOT EXISTS tourney_season_scores (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        player_id     INTEGER NOT NULL UNIQUE,
        match_score   REAL NOT NULL DEFAULT 0,
        activity_score REAL NOT NULL DEFAULT 0,
        final_score   REAL NOT NULL DEFAULT 0,
        rank          INTEGER,
        FOREIGN KEY (player_id) REFERENCES tourney_players(id) ON DELETE CASCADE
    )
    """,
]


def migrate():
    """执行表结构迁移（幂等，重复调用安全）。"""
    with _LOCK:
        with _connect() as conn:
            for stmt in _MIGRATIONS:
                conn.execute(stmt)
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(tourney_matches)").fetchall()
            }
            if "raw_fingerprint" not in columns:
                conn.execute("ALTER TABLE tourney_matches ADD COLUMN raw_fingerprint TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tourney_matches_fingerprint "
                "ON tourney_matches(game_type, raw_fingerprint)"
            )
            conn.commit()
    return True


def _now():
    return datetime.now().isoformat(timespec="seconds")


# ============================================================
# players CRUD
# ============================================================

def upsert_player(uuid_str: str, name: str = None) -> int:
    """插入或更新玩家，返回 player.id。

    若 UUID 已存在则更新 name（改名）与 updated_at，不创建新记录。
    """
    now = _now()
    with _LOCK:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tourney_players (uuid, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(uuid) DO UPDATE SET
                    name       = COALESCE(excluded.name, tourney_players.name),
                    updated_at = excluded.updated_at
                """,
                (uuid_str, name, now, now),
            )
            row = conn.execute(
                "SELECT id FROM tourney_players WHERE uuid = ?", (uuid_str,)
            ).fetchone()
            conn.commit()
            return row[0] if row else cur.lastrowid


def set_latest_player_name_mapping(uuid_str: str, name: str) -> int:
    """将名称唯一地映射到当前 UUID，并清除同名旧 UUID 的显示名称。"""
    now = _now()
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                UPDATE tourney_players
                SET name = NULL, updated_at = ?
                WHERE name = ? COLLATE NOCASE AND uuid != ?
                """,
                (now, name, uuid_str),
            )
            conn.execute(
                """
                INSERT INTO tourney_players (uuid, name, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(uuid) DO UPDATE SET
                    name = excluded.name,
                    updated_at = excluded.updated_at
                """,
                (uuid_str, name, now, now),
            )
            row = conn.execute(
                "SELECT id FROM tourney_players WHERE uuid = ?", (uuid_str,)
            ).fetchone()
            conn.commit()
    return row[0] if row else 0


def update_player_name(player_id: int, name: str):
    """更新玩家显示名（用于在线时解析到的最新名字）。"""
    if not name:
        return
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                "UPDATE tourney_players SET name = ?, updated_at = ? WHERE id = ?",
                (name, _now(), player_id),
            )
            conn.commit()


def get_player_by_uuid(uuid_str: str):
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM tourney_players WHERE uuid = ?", (uuid_str,)
            ).fetchone()
    return _player_row_to_dict(row)


def get_player_by_name(name: str):
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tourney_players
                WHERE name = ? COLLATE NOCASE
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """,
                (name,),
            ).fetchone()
    return _player_row_to_dict(row)


def get_player_by_id(player_id: int):
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM tourney_players WHERE id = ?", (player_id,)
            ).fetchone()
    return _player_row_to_dict(row)


def _player_row_to_dict(row):
    if not row:
        return None
    return {"id": row[0], "uuid": row[1], "name": row[2],
            "created_at": row[3], "updated_at": row[4]}


# ============================================================
# matches / match_players CRUD
# ============================================================

def match_exists(minecraft_match_id: int, game_type: str) -> bool:
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM tourney_matches
                WHERE minecraft_match_id = ? AND game_type = ?
                """,
                (minecraft_match_id, game_type),
            ).fetchone()
    return row is not None


def get_max_match_id(game_type: str) -> int:
    """返回某小游戏已入库的最大 minecraft_match_id，无记录返回 0。"""
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT MAX(minecraft_match_id) FROM tourney_matches WHERE game_type = ?",
                (game_type,),
            ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def insert_match(minecraft_match_id: int, game_type: str,
                 map_name: str, player_count: int, raw_fingerprint: str = None) -> int:
    """插入对局，返回 match.id。调用方需先确认不重复。"""
    with _LOCK:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tourney_matches
                    (minecraft_match_id, game_type, raw_fingerprint, map, player_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (minecraft_match_id, game_type, raw_fingerprint, map_name, player_count, _now()),
            )
            conn.commit()
            return cur.lastrowid


def match_fingerprint_exists(game_type: str, raw_fingerprint: str) -> bool:
    """检查同一游戏的原始对局内容是否已经入库。"""
    if not raw_fingerprint:
        return False
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM tourney_matches
                WHERE game_type = ? AND raw_fingerprint = ?
                """,
                (game_type, raw_fingerprint),
            ).fetchone()
    return row is not None


def insert_match_player(match_id: int, player_id: int,
                        rank: int, survive_time: int, match_score: float):
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO tourney_match_players
                    (match_id, player_id, rank, survive_time, match_score)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(match_id, player_id) DO UPDATE SET
                    rank = excluded.rank,
                    survive_time = excluded.survive_time,
                    match_score = excluded.match_score
                """,
                (match_id, player_id, rank, survive_time, match_score),
            )
            conn.commit()


def get_match(match_db_id: int):
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM tourney_matches WHERE id = ?", (match_db_id,)
            ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "minecraft_match_id": row[1], "game_type": row[2],
        "map": row[3], "player_count": row[4], "created_at": row[5],
    }


def get_match_by_mc_id(minecraft_match_id: int, game_type: str):
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM tourney_matches
                WHERE minecraft_match_id = ? AND game_type = ?
                """,
                (minecraft_match_id, game_type),
            ).fetchone()
    if not row:
        return None
    return {
        "id": row[0], "minecraft_match_id": row[1], "game_type": row[2],
        "map": row[3], "player_count": row[4], "created_at": row[5],
    }


def get_match_players(match_db_id: int):
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT mp.rank, mp.survive_time, mp.match_score,
                       p.id, p.uuid, p.name
                FROM tourney_match_players mp
                JOIN tourney_players p ON p.id = mp.player_id
                WHERE mp.match_id = ?
                ORDER BY mp.rank ASC
                """,
                (match_db_id,),
            ).fetchall()
    return [
        {"rank": r[0], "survive_time": r[1], "match_score": r[2],
         "player_id": r[3], "uuid": r[4], "name": r[5]}
        for r in rows
    ]


# ============================================================
# 聚合查询（供 recompute 使用）
# ============================================================

def get_all_player_ids():
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute("SELECT id FROM tourney_players").fetchall()
    return [r[0] for r in rows]


def get_game_stats_for_player(player_id: int, game_type: str):
    """返回某玩家在某小游戏的有效局统计。

    有效局定义：player_count >= MIN_PLAYER_COUNT(3)。
    返回 dict: {match_count, total_score, average_score}
    """
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*), SUM(mp.match_score), AVG(mp.match_score)
                FROM tourney_match_players mp
                JOIN tourney_matches m ON m.id = mp.match_id
                WHERE mp.player_id = ?
                  AND m.game_type = ?
                  AND m.player_count >= 3
                """,
                (player_id, game_type),
            ).fetchone()
    count = int(row[0]) if row and row[0] is not None else 0
    total = float(row[1]) if row and row[1] is not None else 0.0
    average = float(row[2]) if row and row[2] is not None else 0.0
    return {"match_count": count, "total_score": total, "average_score": average}


def get_season_stats_for_player(player_id: int):
    """返回某玩家所有有效局的赛季统计（跨所有 game_type）。"""
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*), AVG(mp.match_score)
                FROM tourney_match_players mp
                JOIN tourney_matches m ON m.id = mp.match_id
                WHERE mp.player_id = ?
                  AND m.player_count >= 3
                """,
                (player_id,),
            ).fetchone()
    count = int(row[0]) if row and row[0] is not None else 0
    average = float(row[1]) if row and row[1] is not None else 0.0
    return {"match_count": count, "average_score": average}


def get_activity_match_count_for_player(player_id: int):
    """返回某玩家的活跃局数：双人及以上局都计入。"""
    with _LOCK:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*)
                FROM tourney_match_players mp
                JOIN tourney_matches m ON m.id = mp.match_id
                WHERE mp.player_id = ? AND m.player_count >= 2
                """,
                (player_id,),
            ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def get_distinct_game_types():
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT game_type FROM tourney_matches"
            ).fetchall()
    return [r[0] for r in rows]


def get_daily_match_counts_by_uuid(uuid_str: str, day_start: str, day_end: str):
    """统计玩家在指定自然日内的活跃对局次数，按小游戏分组。

    对局时间以 Bot 成功入库的 matches.created_at 为准；因玩家通过 UUID
    关联，Minecraft 改名不会影响统计结果。
    """
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT m.game_type, COUNT(*)
                FROM tourney_match_players mp
                JOIN tourney_matches m ON m.id = mp.match_id
                JOIN tourney_players p ON p.id = mp.player_id
                WHERE p.uuid = ?
                  AND m.player_count >= 2
                  AND m.created_at >= ?
                  AND m.created_at < ?
                GROUP BY m.game_type
                """,
                (uuid_str, day_start, day_end),
            ).fetchall()
    return {str(game_type): int(match_count) for game_type, match_count in rows}


def get_player_total_stats_by_name(player_name: str):
    """按玩家名查询锦标赛总对局次数与总存活时间（跨所有 game_type）。

    返回 {"match_count": int, "total_survive_seconds": int} 或 None。
    """
    with _LOCK:
        with _connect() as conn:
            player = conn.execute(
                "SELECT id, uuid, name FROM tourney_players WHERE name = ? COLLATE NOCASE LIMIT 1",
                (player_name,),
            ).fetchone()
            if not player:
                return None
            pid = int(player[0])
            row = conn.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(mp.survive_time), 0)
                FROM tourney_match_players mp
                JOIN tourney_matches m ON m.id = mp.match_id
                WHERE mp.player_id = ? AND m.player_count >= 2
                """,
                (pid,),
            ).fetchone()
    return {
        "match_count": int(row[0]) if row and row[0] else 0,
        "total_survive_seconds": int(row[1]) if row and row[1] else 0,
    }


# ============================================================
# game_scores / season_scores 写入
# ============================================================

def upsert_game_score(player_id: int, game_type: str,
                      match_count: int, average_score: float,
                      total_score: float, rank: int = None):
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO tourney_game_scores
                    (player_id, game_type, match_count, average_score, total_score, rank)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_id, game_type) DO UPDATE SET
                    match_count   = excluded.match_count,
                    average_score = excluded.average_score,
                    total_score   = excluded.total_score,
                    rank          = excluded.rank
                """,
                (player_id, game_type, match_count, average_score, total_score, rank),
            )
            conn.commit()


def upsert_season_score(player_id: int, match_score: float,
                        activity_score: float, final_score: float, rank: int = None):
    with _LOCK:
        with _connect() as conn:
            conn.execute(
                """
                INSERT INTO tourney_season_scores
                    (player_id, match_score, activity_score, final_score, rank)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(player_id) DO UPDATE SET
                    match_score    = excluded.match_score,
                    activity_score = excluded.activity_score,
                    final_score    = excluded.final_score,
                    rank           = excluded.rank
                """,
                (player_id, match_score, activity_score, final_score, rank),
            )
            conn.commit()


def clear_game_scores():
    with _LOCK:
        with _connect() as conn:
            conn.execute("DELETE FROM tourney_game_scores")
            conn.commit()


def clear_season_scores():
    with _LOCK:
        with _connect() as conn:
            conn.execute("DELETE FROM tourney_season_scores")
            conn.commit()


# ============================================================
# 排行榜读取
# ============================================================

def query_season_leaderboard(min_matches: int):
    """返回赛季总榜（含未达标者，rank 为 NULL 表示未排名）。"""
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT s.player_id, p.uuid, p.name,
                       s.match_score, s.activity_score, s.final_score, s.rank,
                       (
                         SELECT COUNT(*)
                         FROM tourney_match_players mp
                         JOIN tourney_matches m ON m.id = mp.match_id
                         WHERE mp.player_id = s.player_id AND m.player_count >= 3
                       ) AS total_matches
                FROM tourney_season_scores s
                JOIN tourney_players p ON p.id = s.player_id
                ORDER BY
                    CASE WHEN total_matches >= ? THEN 0 ELSE 1 END,
                    s.final_score DESC,
                    s.match_score DESC,
                    total_matches DESC
                """,
                (min_matches,),
            ).fetchall()
    return [
        {"player_id": r[0], "uuid": r[1], "name": r[2],
         "match_score": r[3], "activity_score": r[4], "final_score": r[5],
         "rank": r[6], "total_matches": r[7]}
        for r in rows
    ]


def query_game_leaderboard(game_type: str, min_matches: int):
    """返回某小游戏排行榜。"""
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT g.player_id, p.uuid, p.name,
                       g.match_count, g.average_score, g.total_score, g.rank
                FROM tourney_game_scores g
                JOIN tourney_players p ON p.id = g.player_id
                WHERE g.game_type = ?
                ORDER BY
                    CASE WHEN g.match_count >= ? THEN 0 ELSE 1 END,
                    g.average_score DESC,
                    g.match_count DESC
                """,
                (game_type, min_matches),
            ).fetchall()
    return [
        {"player_id": r[0], "uuid": r[1], "name": r[2],
         "match_count": r[3], "average_score": r[4], "total_score": r[5],
         "rank": r[6]}
        for r in rows
    ]


def query_player_detail(player_id: int):
    """返回某玩家的分游戏明细 + 赛季汇总。"""
    with _LOCK:
        with _connect() as conn:
            season = conn.execute(
                """
                SELECT match_score, activity_score, final_score, rank
                FROM tourney_season_scores WHERE player_id = ?
                """,
                (player_id,),
            ).fetchone()
            games = conn.execute(
                """
                SELECT game_type, match_count, average_score, total_score, rank
                FROM tourney_game_scores WHERE player_id = ?
                ORDER BY game_type
                """,
                (player_id,),
            ).fetchall()
            recent = conn.execute(
                """
                SELECT m.minecraft_match_id, m.game_type, m.map, m.player_count,
                       m.created_at, mp.rank, mp.survive_time, mp.match_score
                FROM tourney_match_players mp
                JOIN tourney_matches m ON m.id = mp.match_id
                WHERE mp.player_id = ?
                ORDER BY m.minecraft_match_id DESC
                LIMIT 10
                """,
                (player_id,),
            ).fetchall()
    return {
        "season": {"match_score": season[0], "activity_score": season[1],
                   "final_score": season[2], "rank": season[3]} if season else None,
        "games": [
            {"game_type": g[0], "match_count": g[1], "average_score": g[2],
             "total_score": g[3], "rank": g[4]} for g in games
        ],
        "recent": [
            {"minecraft_match_id": r[0], "game_type": r[1], "map": r[2],
             "player_count": r[3], "created_at": r[4], "rank": r[5],
             "survive_time": r[6], "match_score": r[7]} for r in recent
        ],
    }


def count_matches():
    with _LOCK:
        with _connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM tourney_matches").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def count_players():
    with _LOCK:
        with _connect() as conn:
            row = conn.execute("SELECT COUNT(*) FROM tourney_players").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def query_participation_leaderboard(limit=5):
    """返回按总活跃局数降序的排行榜（跨所有 game_type，双人及以上）。"""
    with _LOCK:
        with _connect() as conn:
            rows = conn.execute(
                """
                SELECT p.id, p.uuid, p.name, COUNT(*) AS total_matches
                FROM tourney_match_players mp
                JOIN tourney_matches m ON m.id = mp.match_id
                JOIN tourney_players p ON p.id = mp.player_id
                WHERE m.player_count >= 2
                GROUP BY p.id, p.uuid, p.name
                ORDER BY total_matches DESC, p.name ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [
        {"player_id": r[0], "uuid": r[1], "name": r[2], "total_matches": int(r[3])}
        for r in rows
    ]


def reset_all_tournament_data():
    """清空所有锦标赛数据（players/matches/match_players/game_scores/season_scores）。

    供管理员在测试或新赛季开始时调用。表结构保留。
    """
    with _LOCK:
        with _connect() as conn:
            # 按外键依赖顺序删除
            for t in (
                "tourney_season_scores",
                "tourney_game_scores",
                "tourney_match_players",
                "tourney_matches",
                "tourney_players",
            ):
                conn.execute(f"DELETE FROM {t}")
            conn.commit()
    return True
