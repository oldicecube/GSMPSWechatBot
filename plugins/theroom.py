# -*- coding: utf-8 -*-
"""The Room 锦标赛命令插件。

命令：
    /theroom                  积分榜、参与榜、总览
    /theroom lp               Lucky Pillar 排行榜
    /theroom mc               Miner Chaos 排行榜
    /theroom lp #<编号>       Lucky Pillar 对局详情
    /theroom mc #<编号>       Miner Chaos 对局详情
    /theroom --player <名称>  指定玩家详情
    /theroom --refresh        手动拉取新对局（管理员）
    /theroom --recompute      全量重算积分（管理员）
    /theroom --syncnames      从 usercache 同步玩家名（管理员）
"""

from __future__ import annotations

from services.tournament import config as tourney_config
from services.tournament import db
from services.tournament import leaderboard
from services.tournament.ingest import import_usercache

try:
    from auto import tournament_monitor as monitor
except Exception:
    monitor = None

COMMAND = "/theroom"

_config = {}


def init(config):
    global _config
    _config = config or {}
    tourney_config.set_config(_config)
    try:
        db.migrate()
    except Exception as e:
        print(f"[THEROOM PLUGIN MIGRATE ERROR] {e}")


def _is_admin(context):
    wxid = context.get("wxid") or context.get("raw", {}).get("wxid") or ""
    wxid = str(wxid).strip()
    admin_wxids = _config.get("llm", {}).get("admin_wxids", []) or []
    return wxid in admin_wxids


def _norm_content(content):
    if isinstance(content, list):
        content = " ".join(str(c) for c in content)
    return str(content or "").strip()


def _resolve_game_type(alias):
    """将用户输入的别名映射到标准 game_type 字符串，无法识别返回 None。"""
    alias = alias.lower().strip()
    if alias in ("lp", "lucky", "luckypillar", "幸运"):
        return "Lucky Pillar"
    if alias in ("mc", "miner", "chaos", "minerchaos", "混沌", "矿工"):
        return "Miner Chaos"
    return None


def _help_text():
    return (
        "The Room 锦标赛命令:\n"
        "  /theroom            积分榜+参与榜+总览(无参数)\n"
        "  /theroom --rules    积分计算规则\n"
        "  /theroom lp         Lucky Pillar 榜\n"
        "  /theroom mc         Miner Chaos 榜\n"
        "  /theroom lp #<编号>  Lucky Pillar 对局详情\n"
        "  /theroom mc #<编号>  Miner Chaos 对局详情\n"
        "  /theroom --player <名称>  玩家详情\n"
        "  /theroom --refresh  手动拉取(管理员)\n"
        "  /theroom --recompute 重算积分(管理员)\n"
        "  /theroom --syncnames 同步玩家名(管理员)\n"
        "  /theroom --reset    清空对局数据(管理员)"
    )


def _rules_text():
    return (
        "📐 The Room 积分计算规则\n"
        + "-" * 22 + "\n"
        "单人局: 直接丢弃\n"
        "双人局: 计入游玩次数、每日参与积分和赛季活跃奖励，单局积分为 0\n"
        "         不计入对局平均分、分数排行榜或正式排名局数\n"
        "计分对局: 至少 3 人\n\n"
        "单局积分 = 表现分 + 人数质量分\n"
        "表现分 = 80 × (人数 - 名次) / (人数 - 1)\n"
        "第一名 80 分，最后一名 0 分\n"
        "人数质量分: 3人+8 / 4人+11 / 5人+14\n"
        "             6人+16 / 7人+18 / 8人及以上+20\n\n"
        "赛季最终分 = 有效对局平均分 × 0.9 + 活跃奖励\n"
        "活跃奖励 = min(参赛局数 ÷ 20 × 10, 10)\n"
        "正式排名: 至少参加 5 局；不足仍显示但不排名"
    )


def handle(content, context):
    args = _norm_content(content)

    # 无参数 -> 首页视图（积分榜 + 参与榜 + 帮助提示）
    if not args:
        return leaderboard.format_home()

    parts = args.split(None, 1)
    sub = parts[0].lower()
    rest = parts[1].strip() if len(parts) > 1 else ""

    # ---------- 积分规则 ----------
    if sub in ("--rules", "--rule", "--score", "--scoring"):
        return _rules_text()

    # ---------- 小游戏榜单 / 对局详情 ----------
    game_type = _resolve_game_type(sub)
    if game_type:
        if not rest:
            return leaderboard.format_game_leaderboard(game_type)
        match_ref = rest.strip()
        if match_ref.startswith("#"):
            match_ref = match_ref[1:].strip()
        try:
            match_id = int(match_ref)
        except ValueError:
            return f"用法: /theroom {sub} [#对局编号]"
        return leaderboard.format_match_detail(match_id, game_type=game_type)

    # ---------- 玩家详情 ----------
    if sub in ("--player", "--p") and rest:
        return leaderboard.format_player_detail(rest)

    # ---------- 手动拉取 ----------
    if sub in ("--refresh", "--poll"):
        if not _is_admin(context):
            return "仅管理员可手动拉取"
        if monitor is None:
            return "监控模块未加载"
        result = monitor.manual_poll()
        return result.get("message", "拉取完成")

    # ---------- 全量重算 ----------
    if sub in ("--recompute", "--rebuild"):
        if not _is_admin(context):
            return "仅管理员可重算积分"
        if monitor is None:
            from services.tournament.ingest import recompute_all
            r = recompute_all()
            return f"重算完成: {r['players']} 玩家 / {r['games']} 小游戏"
        result = monitor.manual_recompute()
        return result.get("message", "重算完成")

    # ---------- 同步玩家名 ----------
    if sub in ("--syncnames", "--sync"):
        if not _is_admin(context):
            return "仅管理员可同步玩家名"
        path = tourney_config.get_usercache_path()
        result = import_usercache(path)
        return result.get("message", "同步完成")

    # ---------- 重置所有对局数据（管理员） ----------
    if sub in ("--reset", "--wipe"):
        if not _is_admin(context):
            return "仅管理员可重置对局数据"
        try:
            db.reset_all_tournament_data()
            # 同步清除监控的拉取熔断状态，使后续能重新拉取
            if monitor is not None:
                monitor.reset_fetch_state()
            return "已清空所有锦标赛对局数据（表结构保留）"
        except Exception as e:
            return f"重置失败: {e}"

    # ---------- 帮助 ----------
    if sub in ("--help", "-h", "--?"):
        return _help_text()

    # ---------- 未知子命令 ----------
    return _help_text()
