# -*- coding: utf-8 -*-
"""比赛自动入库与积分重算。

数据处理流程：
    Minecraft Storage -> RCON -> SNBT 解析 -> UUID 转换
    -> 查询/创建玩家 -> 写入 matches -> 写入 match_players
    -> 计算 match_score -> 更新 game_scores/season_scores

异常检测：
    - 重复 minecraft_match_id 禁止写入
    - 玩家数量异常（<3 或与 count 不一致）记录日志
    - 数据格式错误记录日志
"""

from __future__ import annotations

import json
import os
import traceback
import hashlib
from datetime import datetime
from typing import Any

from . import db
from .scoring import (
    calc_match_score,
    calc_season_score,
    activity_score,
    DEFAULT_MIN_MATCHES_FOR_RANKING,
    MIN_PLAYER_COUNT,
)
from .uuid_utils import ints_to_uuid_str


# ============================================================
# 单局数据校验
# ============================================================

def _validate_match_data(data: Any):
    """校验并规范化解析后的对局数据。

    返回 (ok, match_dict, error_msg)
    """
    if not isinstance(data, dict):
        return False, None, f"对局数据不是字典: {type(data).__name__}"

    mc_id = data.get("id")
    if mc_id is None:
        return False, None, "缺少 id 字段"
    try:
        mc_id = int(mc_id)
    except (TypeError, ValueError):
        return False, None, f"id 不是整数: {mc_id!r}"

    game_type = data.get("game") or data.get("game_type")
    if not game_type:
        return False, None, "缺少 game 字段"
    game_type = str(game_type)

    map_name = data.get("map", "")
    map_name = str(map_name) if map_name is not None else ""

    count = data.get("count", 0)
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 0

    players = data.get("players", [])
    if not isinstance(players, list):
        return False, None, f"players 不是列表: {type(players).__name__}"

    return True, {
        "id": mc_id,
        "game_type": game_type,
        "map": map_name,
        "count": count,
        "players": players,
    }, None


def _parse_player_entry(entry, index):
    """解析单个玩家条目，返回 (ok, player_dict, error_msg)。

    uuid 支持两种来源：
        - SNBT 解析的 [I;a,b,c,d] -> 4 个整数的列表
        - 标准 UUID 字符串 "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    """
    if not isinstance(entry, dict):
        return False, None, f"玩家 #{index} 不是字典: {type(entry).__name__}"

    raw_uuid = entry.get("uuid")
    if raw_uuid is None:
        return False, None, f"玩家 #{index} 缺少 uuid"

    uuid_str = None
    if isinstance(raw_uuid, list):
        if len(raw_uuid) != 4:
            return False, None, f"玩家 #{index} uuid 数组长度错误: {raw_uuid!r}"
        try:
            uuid_str = ints_to_uuid_str([int(x) for x in raw_uuid])
        except Exception as e:
            return False, None, f"玩家 #{index} uuid 转换失败: {e}"
    elif isinstance(raw_uuid, str):
        # 标准 UUID 字符串
        cleaned = raw_uuid.strip()
        if len(cleaned.replace("-", "")) != 32:
            return False, None, f"玩家 #{index} uuid 字符串格式错误: {raw_uuid!r}"
        uuid_str = cleaned.lower()
    else:
        return False, None, f"玩家 #{index} uuid 类型不支持: {type(raw_uuid).__name__}"

    rank = entry.get("rank")
    try:
        rank = int(rank) if rank is not None else 0
    except (TypeError, ValueError):
        rank = 0

    survive_time = entry.get("time", 0)
    try:
        survive_time = int(survive_time) if survive_time is not None else 0
    except (TypeError, ValueError):
        survive_time = 0

    return True, {
        "uuid": uuid_str,
        "rank": rank,
        "survive_time": survive_time,
    }, None


# ============================================================
# 入库主流程
# ============================================================

def ingest_match(data: dict, game_type_hint: str = None) -> dict:
    """将一局解析后的数据入库。

    返回:
        {
            "status": "created" | "duplicate" | "skipped" | "error",
            "match_id": int,
            "game_type": str,
            "player_count": int,
            "message": str,
        }
    """
    try:
        ok, match, err = _validate_match_data(data)
        if not ok:
            print(f"[TOURNAMENT INGEST ERROR] 数据校验失败: {err}")
            return {"status": "error", "match_id": None,
                    "game_type": game_type_hint or "?", "player_count": 0, "message": err}

        mc_id = match["id"]
        game_type = match["game_type"]
        map_name = match["map"]
        declared_count = match["count"]
        players_raw = match["players"]
        actual_count = len(players_raw)

        # 原始数据指纹用于处理数据包 reload 后可能重复的 Minecraft 对局编号。
        fingerprint_payload = json.dumps(match, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        raw_fingerprint = hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest()

        # 相同原始记录禁止重复写入；同 id 但内容不同的历史记录允许补入。
        if db.match_fingerprint_exists(game_type, raw_fingerprint):
            return {"status": "duplicate", "match_id": mc_id,
                    "game_type": game_type, "player_count": actual_count,
                "message": f"对局已存在 {game_type}#{mc_id}"}

        # 人数异常检测
        if actual_count != declared_count and declared_count > 0:
            print(
                f"[TOURNAMENT WARN] {game_type}#{mc_id} 人数不一致: "
                f"count={declared_count} 实际 players={actual_count}，以实际为准"
            )
        if actual_count < 2:
            print(
                f"[TOURNAMENT WARN] {game_type}#{mc_id} 单人对局，丢弃结果"
            )
            return {"status": "skipped", "match_id": mc_id,
                    "game_type": game_type, "player_count": actual_count,
                    "message": "单人对局已丢弃"}
        if actual_count < MIN_PLAYER_COUNT:
            print(
                f"[TOURNAMENT WARN] {game_type}#{mc_id} 人数不足 "
                f"{MIN_PLAYER_COUNT}（实际 {actual_count}），计入活跃但不计入积分"
            )

        # 以实际玩家数计算积分（更稳健）
        player_count_for_score = actual_count

        # 解析玩家并 upsert
        parsed_players = []
        for i, entry in enumerate(players_raw):
            ok, p, err = _parse_player_entry(entry, i)
            if not ok:
                print(f"[TOURNAMENT INGEST ERROR] {err}")
                continue
            parsed_players.append(p)

        # 写入对局
        # 旧数据包可能因 /reload 产生同 game_type + id；为满足现有唯一约束，
        # 使用内部补偿 id 保存原始 id 冲突的不同对局。
        stored_mc_id = mc_id
        while db.match_exists(stored_mc_id, game_type):
            stored_mc_id += 1_000_000_000
        match_db_id = db.insert_match(
            stored_mc_id, game_type, map_name, player_count_for_score, raw_fingerprint
        )

        # 写入玩家表现 + 单局积分
        for p in parsed_players:
            player_id = db.upsert_player(p["uuid"])
            match_score = calc_match_score(player_count_for_score, p["rank"])
            db.insert_match_player(
                match_db_id, player_id, p["rank"], p["survive_time"], match_score
            )

        # 重算受影响玩家的积分
        affected = [db.upsert_player(p["uuid"]) for p in parsed_players]
        recompute_for_players(affected)

        print(
            f"[TOURNAMENT INGEST] 入库成功 {game_type}#{mc_id} "
            f"地图={map_name} 人数={player_count_for_score}"
        )
        return {
            "status": "created",
            "match_id": mc_id,
            "game_type": game_type,
            "player_count": player_count_for_score,
            "message": f"已入库 {game_type}#{mc_id}",
        }

    except Exception as e:
        print(f"[TOURNAMENT INGEST ERROR] 异常: {e}")
        traceback.print_exc()
        return {"status": "error", "match_id": None,
                "game_type": game_type_hint or "?", "player_count": 0,
                "message": str(e)}


# ============================================================
# 积分重算（派生表重建，保证可重算）
# ============================================================

def recompute_for_players(player_ids):
    """重算指定玩家的 game_scores 与 season_scores。"""
    if not player_ids:
        return
    player_ids = list(dict.fromkeys(player_ids))  # 去重保序

    game_types = db.get_distinct_game_types()

    for pid in player_ids:
        # ---- 每个小游戏（跳过该玩家未参与的游戏，避免空行） ----
        for gtype in game_types:
            stats = db.get_game_stats_for_player(pid, gtype)
            if stats["match_count"] <= 0:
                continue
            db.upsert_game_score(
                pid, gtype,
                match_count=stats["match_count"],
                average_score=round(stats["average_score"], 4),
                total_score=round(stats["total_score"], 4),
                rank=None,  # rank 在 recompute_ranks 中统一赋值
            )
        # ---- 赛季汇总 ----
        season = db.get_season_stats_for_player(pid)
        activity_matches = db.get_activity_match_count_for_player(pid)
        if activity_matches <= 0:
            continue
        match_avg = round(season["average_score"], 4)
        activity = activity_score(activity_matches)
        final = calc_season_score(match_avg, activity_matches)
        db.upsert_season_score(pid, match_avg, activity, final, rank=None)

    # 重算这些玩家涉及的排名
    recompute_ranks(game_types)


def recompute_all():
    """全量重算所有玩家的 game_scores 与 season_scores（可随时调用）。"""
    db.migrate()
    player_ids = db.get_all_player_ids()
    game_types = db.get_distinct_game_types()

    # 收集所有 (player, game) 的原始统计
    # 先清理派生表，再全量重建
    db.clear_game_scores()
    db.clear_season_scores()

    for pid in player_ids:
        for gtype in game_types:
            stats = db.get_game_stats_for_player(pid, gtype)
            if stats["match_count"] <= 0:
                continue
            db.upsert_game_score(
                pid, gtype,
                match_count=stats["match_count"],
                average_score=round(stats["average_score"], 4),
                total_score=round(stats["total_score"], 4),
                rank=None,
            )
        season = db.get_season_stats_for_player(pid)
        activity_matches = db.get_activity_match_count_for_player(pid)
        if activity_matches <= 0:
            continue
        match_avg = round(season["average_score"], 4)
        activity = activity_score(activity_matches)
        final = calc_season_score(match_avg, activity_matches)
        db.upsert_season_score(pid, match_avg, activity, final, rank=None)

    recompute_ranks(game_types)
    return {"players": len(player_ids), "games": len(game_types)}


def recompute_ranks(game_types, min_matches: int = DEFAULT_MIN_MATCHES_FOR_RANKING):
    """为各排行榜赋予名次。

    排名规则：参赛达 min_matches 局者按分数降序排名；
    不足者不排名（rank = NULL）但仍显示。
    """
    # ---- 各小游戏排名 ----
    for gtype in game_types:
        rows = db.query_game_leaderboard(gtype, min_matches)
        # 仅对达标玩家排名
        rank_counter = 0
        for r in rows:
            if r["match_count"] >= min_matches:
                rank_counter += 1
                db.upsert_game_score(
                    r["player_id"], gtype,
                    match_count=r["match_count"],
                    average_score=r["average_score"],
                    total_score=r["total_score"],
                    rank=rank_counter,
                )
            else:
                db.upsert_game_score(
                    r["player_id"], gtype,
                    match_count=r["match_count"],
                    average_score=r["average_score"],
                    total_score=r["total_score"],
                    rank=None,
                )

    # ---- 赛季总榜排名 ----
    season_rows = db.query_season_leaderboard(min_matches)
    rank_counter = 0
    for r in season_rows:
        total_matches = r.get("total_matches", 0)
        if total_matches >= min_matches:
            rank_counter += 1
            db.upsert_season_score(
                r["player_id"],
                match_score=r["match_score"],
                activity_score=r["activity_score"],
                final_score=r["final_score"],
                rank=rank_counter,
            )
        else:
            db.upsert_season_score(
                r["player_id"],
                match_score=r["match_score"],
                activity_score=r["activity_score"],
                final_score=r["final_score"],
                rank=None,
            )


# ============================================================
# usercache.json 名称映射导入
# ============================================================

def import_usercache(path: str) -> dict:
    """从 Minecraft usercache.json 导入 uuid↔name 映射到 players 表。

    usercache.json 格式：
        [{"name": "Steve", "uuid": "xxxx-xxxx-...", "expiresOn": "..."}, ...]

    对已存在的 UUID 更新 name（支持改名），对新 UUID 创建玩家记录。
    返回 {"ok": bool, "imported": int, "message": str}
    """
    path = str(path or "").strip()
    if not path or not os.path.exists(path):
        msg = f"usercache 文件不存在: {path or '(未配置)'}"
        print(f"[TOURNAMENT USERCACHE] {msg}")
        return {"ok": False, "imported": 0, "message": msg}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        msg = f"读取 usercache 失败: {e}"
        print(f"[TOURNAMENT USERCACHE] {msg}")
        return {"ok": False, "imported": 0, "message": msg}

    if not isinstance(data, list):
        msg = "usercache 格式错误（应为列表）"
        print(f"[TOURNAMENT USERCACHE] {msg}")
        return {"ok": False, "imported": 0, "message": msg}

    # usercache 可能保留同名玩家的历史 UUID。仅导入 expiresOn 最新的条目，
    # 避免旧 UUID 与当前 UUID 竞争，导致名称查询命中错误记录。
    newest_by_name = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        uuid_str = entry.get("uuid")
        if not name or not uuid_str:
            continue
        key = str(name).casefold()
        raw_expiry = str(entry.get("expiresOn") or "")
        try:
            expiry = datetime.strptime(raw_expiry, "%Y-%m-%d %H:%M:%S %z")
        except ValueError:
            expiry = datetime.min.replace(tzinfo=None)
        current = newest_by_name.get(key)
        if current is None or expiry > current[0]:
            newest_by_name[key] = (expiry, str(name), str(uuid_str))

    count = 0
    skipped = len(data) - len(newest_by_name)
    for _, name, uuid_str in newest_by_name.values():
        try:
            db.set_latest_player_name_mapping(uuid_str, name)
            count += 1
        except Exception as e:
            print(f"[TOURNAMENT USERCACHE] 跳过 {name}/{uuid_str}: {e}")
            skipped += 1

    print(f"[TOURNAMENT USERCACHE] 同步完成: {count} 名玩家（跳过 {skipped}）")
    return {"ok": True, "imported": count,
            "message": f"已同步 {count} 名玩家名称" + (f"（跳过 {skipped}）" if skipped else "")}
