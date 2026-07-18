# -*- coding: utf-8 -*-
"""排行榜查询与文本格式化。

提供三类排行榜：
    1. 赛季总榜（season）
    2. Lucky Pillar 榜
    3. Miner Chaos 榜

以及单局详情、玩家明细的格式化。
"""

from __future__ import annotations

from . import db
from .config import get_tournament_config
from .scoring import DEFAULT_MIN_MATCHES_FOR_RANKING


def _display_name(name, uuid_str):
    if name:
        return name
    return "未知玩家"


def _format_time(seconds):
    """将存活时间（秒）转为可读字符串。

    注意：Lucky Pillar / Miner Chaos 数据包记录的 time 字段单位均为秒
    （LP 的 lp_survive_time 每秒 +1；MC 的 miner_chaos.display.time 每秒 +1）。
    """
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        return "?"
    if seconds <= 0:
        return "0s"
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    if minutes < 60:
        return f"{minutes}m{secs:.0f}s"
    hours = minutes // 60
    mins = minutes - hours * 60
    return f"{hours}h{mins}m"


def _get_min_matches():
    try:
        return int(get_tournament_config().get("min_matches_for_ranking",
                                               DEFAULT_MIN_MATCHES_FOR_RANKING))
    except Exception:
        return DEFAULT_MIN_MATCHES_FOR_RANKING


def _get_top():
    try:
        return int(get_tournament_config().get("leaderboard_top", 10))
    except Exception:
        return 10


# ============================================================
# 排行榜文本
# ============================================================

def format_season_leaderboard(limit=None):
    min_matches = _get_min_matches()
    rows = db.query_season_leaderboard(min_matches)
    top = _get_top()
    limit = limit or top

    ranked = [r for r in rows if r.get("total_matches", 0) >= min_matches]
    unranked = [r for r in rows if r.get("total_matches", 0) < min_matches]

    lines = ["🏆 锦标赛赛季总榜", "-" * 22]

    if not ranked:
        lines.append("暂无正式排名玩家")
    else:
        shown = ranked[:limit]
        for r in shown:
            medal = _medal(r["rank"])
            name = _display_name(r["name"], r["uuid"])
            lines.append(
                f"{medal}{r['rank']:>2}. {name}  "
                f"{r['final_score']:.1f}分 "
                f"(对局{r['match_score']:.1f}×0.9 + 活跃{r['activity_score']:.1f}) "
                f"[{r['total_matches']}局]"
            )

    if unranked:
        lines.append("")
        lines.append(f"未达标（<{min_matches}局，不参与排名）:")
        for r in unranked[:limit]:
            name = _display_name(r["name"], r["uuid"])
            lines.append(
                f"   {name}  {r['final_score']:.1f}分 [{r['total_matches']}局]"
            )

    lines.append("-" * 22)
    lines.append(f"正式排名需至少 {min_matches} 局")
    return "\n".join(lines)


def format_game_leaderboard(game_type, limit=None):
    min_matches = _get_min_matches()
    rows = db.query_game_leaderboard(game_type, min_matches)
    top = _get_top()
    limit = limit or top

    ranked = [r for r in rows if r["match_count"] >= min_matches]
    unranked = [r for r in rows if r["match_count"] < min_matches]

    lines = [f"📊 {game_type} 排行榜", "-" * 22]

    if not ranked:
        lines.append("暂无正式排名玩家")
    else:
        shown = ranked[:limit]
        for r in shown:
            medal = _medal(r["rank"] or 0)
            name = _display_name(r["name"], r["uuid"])
            lines.append(
                f"{medal}{r['rank']:>2}. {name}  "
                f"均{r['average_score']:.1f} "
                f"(总{r['total_score']:.1f}) [{r['match_count']}局]"
            )

    if unranked:
        lines.append("")
        lines.append(f"未达标（<{min_matches}局，不参与排名）:")
        for r in unranked[:limit]:
            name = _display_name(r["name"], r["uuid"])
            lines.append(
                f"   {name}  均{r['average_score']:.1f} [{r['match_count']}局]"
            )

    lines.append("-" * 22)
    lines.append(f"正式排名需至少 {min_matches} 局")
    return "\n".join(lines)


def format_match_detail(minecraft_match_id, game_type=None):
    """格式化单局详情。指定 game_type 时精确查找；否则全库搜索。"""
    match = None
    if game_type:
        match = db.get_match_by_mc_id(minecraft_match_id, game_type)
        if not match:
            return f"未找到 {game_type} 的对局 #{minecraft_match_id}"
    else:
        # 未指定游戏类型时，在所有 game_type 中查找
        for gtype in db.get_distinct_game_types():
            m = db.get_match_by_mc_id(minecraft_match_id, gtype)
            if m:
                match = m
                break
        if not match:
            return f"未找到对局 #{minecraft_match_id}（可用 /theroom lp|mc #<编号> 指定游戏）"

    players = db.get_match_players(match["id"])
    lines = [
        f"🎮 对局 #{match['minecraft_match_id']} 详情",
        "-" * 22,
        f"游戏: {match['game_type']}",
        f"地图: {match['map'] or '未知'}",
        f"人数: {match['player_count']}",
        f"时间: {match['created_at']}",
        "",
        "排名 | 玩家 | 存活 | 单局积分",
    ]
    for p in players:
        name = _display_name(p["name"], p["uuid"])
        lines.append(
            f"  {p['rank']}  | {name} | {_format_time(p['survive_time'])} | {p['match_score']:.1f}"
        )
    return "\n".join(lines)


def format_player_detail(name_or_uuid):
    """格式化玩家详情。按名称或 UUID 前缀查找。"""
    player = db.get_player_by_name(name_or_uuid)
    if not player:
        # 尝试 UUID 前缀匹配
        from . import db as _db
        with _db._LOCK:
            with _db._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM tourney_players WHERE uuid LIKE ? OR uuid = ?",
                    (f"{name_or_uuid}%", name_or_uuid),
                ).fetchone()
        if row:
            player = _db._player_row_to_dict(row)

    if not player:
        return f"未找到玩家: {name_or_uuid}"

    detail = db.query_player_detail(player["id"])
    display = _display_name(player["name"], player["uuid"])
    lines = [
        f"👤 玩家详情: {display}",
        "-" * 22,
    ]

    has_match_record = bool(detail.get("season") or detail.get("games") or detail.get("recent"))
    if not has_match_record:
        lines.append("暂无 The Room 游玩记录")
        return "\n".join(lines)

    season = detail.get("season")
    if season:
        rank_str = f"#{season['rank']}" if season["rank"] else "未排名"
        lines.append("")
        lines.append("赛季总榜:")
        lines.append(
            f"  最终积分: {season['final_score']:.1f} ({rank_str})\n"
            f"  对局表现: {season['match_score']:.1f} × 0.9\n"
            f"  活跃奖励: {season['activity_score']:.1f}"
        )
    else:
        lines.append("赛季总榜: 暂无数据")

    games = detail.get("games", [])
    if games:
        lines.append("")
        lines.append("分游戏统计:")
        for g in games:
            rank_str = f"#{g['rank']}" if g["rank"] else "未排名"
            lines.append(
                f"  {g['game_type']}: 均{g['average_score']:.1f} "
                f"[{g['match_count']}局] ({rank_str})"
            )

    recent = detail.get("recent", [])
    if recent:
        lines.append("")
        lines.append("最近对局:")
        for r in recent[:5]:
            lines.append(
                f"  #{r['minecraft_match_id']} {r['game_type']} "
                f"{r['map'] or ''} | 第{r['rank']}名 | {r['match_score']:.1f}分"
            )

    return "\n".join(lines)


def format_my_theroom(player_name):
    """格式化“我的”The Room 锦标赛详情（按绑定的玩家名查找）。"""
    if not player_name:
        return "你还未绑定玩家，请先使用 /bind <玩家名> 绑定"

    player = db.get_player_by_name(player_name)
    if not player:
        return (
            f"已绑定玩家 {player_name}，但没有 UUID 信息。\n"
            "请找管理员绑定或同步玩家信息。"
        )
    detail = db.query_player_detail(player["id"])
    if not (detail.get("season") or detail.get("games") or detail.get("recent")):
        return f"玩家 {player_name} 暂无 The Room 游玩记录"
    return format_player_detail(player_name)


def format_my_rank(player_name):
    """格式化“我的”排名概览（按绑定的玩家名查找）。"""
    if not player_name:
        return "你还未绑定玩家，请先使用 /bind <玩家名> 绑定"

    player = db.get_player_by_name(player_name)
    if not player:
        return (
            f"已绑定玩家 {player_name}，但没有 UUID 信息。\n"
            "请找管理员绑定或同步玩家信息。"
        )

    min_matches = _get_min_matches()
    detail = db.query_player_detail(player["id"])
    season = detail.get("season")
    games = detail.get("games", [])

    if not (season or games or detail.get("recent")):
        return f"玩家 {player_name} 暂无 The Room 游玩记录"

    display = _display_name(player["name"], player["uuid"])
    lines = [f"📊 {display} 的 The Room 排名", "-" * 22]

    if season:
        if season["rank"]:
            lines.append(f"赛季总榜: 第 {season['rank']} 名 ({season['final_score']:.1f}分)")
        else:
            total_matches = sum(g["match_count"] for g in games)
            lines.append(
                f"赛季总榜: 未排名 ({season['final_score']:.1f}分, "
                f"参赛 {total_matches} 局，需 ≥{min_matches} 局)"
            )
    else:
        lines.append("赛季总榜: 暂无数据")

    if games:
        lines.append("")
        for g in games:
            if g["rank"]:
                lines.append(
                    f"{g['game_type']}: 第 {g['rank']} 名 "
                    f"(均 {g['average_score']:.1f}, {g['match_count']}局)"
                )
            else:
                lines.append(
                    f"{g['game_type']}: 未排名 "
                    f"(均 {g['average_score']:.1f}, {g['match_count']}局)"
                )
    else:
        lines.append("暂无分游戏数据")

    return "\n".join(lines)


def format_home(limit=5):
    """/theroom 无参数时的首页视图：积分榜、参与榜、总览与帮助提示。"""
    min_matches = _get_min_matches()
    season_rows = db.query_season_leaderboard(min_matches)
    part_rows = db.query_participation_leaderboard(limit)

    lines = ["🏟️ The Room 锦标赛", "-" * 22]

    lines.extend([
        f"已入库对局: {db.count_matches()}",
        f"参赛玩家: {db.count_players()}",
        f"小游戏: {', '.join(db.get_distinct_game_types()) or '无'}",
        "",
    ])

    # ---- 积分榜 TOP N（已排名优先，按最终积分降序，不足用未排名补齐） ----
    lines.append(f"🏆 积分榜 TOP{limit}")
    shown = season_rows[:limit]
    if not shown:
        lines.append("  暂无数据")
    else:
        for r in shown:
            medal = _medal(r.get("rank") or 0)
            name = _display_name(r["name"], r["uuid"])
            tag = "" if r.get("rank") else "(未排名)"
            lines.append(
                f"{medal}{name}  {r['final_score']:.1f}分 "
                f"[{r.get('total_matches', 0)}局]{tag}"
            )

    # ---- 参与榜 TOP N（按总参赛局数降序） ----
    lines.append("")
    lines.append(f"📊 参与榜 TOP{limit}")
    if not part_rows:
        lines.append("  暂无参赛记录")
    else:
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for i, r in enumerate(part_rows[:limit]):
            name = _display_name(r["name"], r["uuid"])
            prefix = medals[i] if i < len(medals) else f"{i+1}."
            lines.append(f"{prefix} {name}  {r['total_matches']}局")

    lines.append("")
    lines.append("💡 使用 /theroom --help 查看可用子命令")
    return "\n".join(lines)

def _medal(rank):
    if rank == 1:
        return "🥇"
    if rank == 2:
        return "🥈"
    if rank == 3:
        return "🥉"
    return "   "
