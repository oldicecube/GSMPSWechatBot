# -*- coding: utf-8 -*-
"""锦标赛 Storage 同步服务（由 player_monitor 离服事件触发）。

职责：
    1. 同步各已启用小游戏 Storage 中的新对局并入库。
    2. 提供 /theroom --refresh 的手动同步入口。

遵循项目 auto 插件约定：
    init(config)   -> 注入配置
    start(sender)  -> 初始化服务，不启动轮询线程
"""

from __future__ import annotations

import threading

from services.tournament import config as tourney_config
from services.tournament import db
from services.tournament.ingest import ingest_match, recompute_all, import_usercache
from services.tournament.storage_reader import StorageReader


_reader: StorageReader = None
_config: dict = {}
_sender = None
_stop_event = threading.Event()

# 拉取健壮性：失败计数与熔断
#   同一 (game, mid) 连续失败超过 MAX_FETCH_FAILS 次则永久跳过，
#   避免 RCON 抖动或数据包缺失某 id 导致无限重试。
MAX_FETCH_FAILS = 3
#   当 latest_id 与已入库最大 id 差距超过此值时打印警告（可能数据被重置/异常）
GAP_WARN_THRESHOLD = 50
_fail_counts = {}   # {(game, mid): fail_count}
_skipped_ids = set()  # {(game, mid)}


def init(config):
    """注入全局配置并初始化数据库。"""
    global _config, _reader
    _config = config or {}
    tourney_config.set_config(_config)

    try:
        db.migrate()
    except Exception as e:
        print(f"[TOURNAMENT MIGRATE ERROR] {e}")

    # 启动时从 usercache.json 同步玩家名称映射
    try:
        result = import_usercache(tourney_config.get_usercache_path())
        if result["ok"]:
            print(f"[TOURNAMENT] {result['message']}")
    except Exception as e:
        print(f"[TOURNAMENT USERCACHE ERROR] {e}")

    tcfg = tourney_config.get_tournament_config()
    rcon_cfg = tourney_config.get_rcon_config()
    _reader = StorageReader(rcon_cfg, timeout=tcfg.get("rcon_timeout", 10))
    print("[TOURNAMENT] 监控任务初始化完成")


def sync_one_game(name, storage):
    """读取完整 Storage 列表，按原始内容指纹补入尚未处理的对局。"""
    try:
        matches = _reader.get_all_matches(storage)
    except Exception as e:
        print(f"[TOURNAMENT POLL ERROR] {name} list: {e}")
        return 0

    if not matches:
        return 0
    ingested = 0
    for match_data in matches:
        raw_id = match_data.get("id", "?")
        key = (name, str(raw_id), str(match_data.get("map", "")))
        result = ingest_match(match_data, game_type_hint=name)
        if result["status"] == "created":
            ingested += 1
            _notify_new_match(result)
            _fail_counts.pop(key, None)
        elif result["status"] == "duplicate":
            _fail_counts.pop(key, None)
        elif result["status"] == "error":
            _record_fetch_failure(key, name, raw_id, f"入库失败: {result['message']}")

    return ingested


def _record_fetch_failure(key, game, mid, reason):
    """记录一次拉取/入库失败，超过阈值则熔断跳过该 id。"""
    count = _fail_counts.get(key, 0) + 1
    _fail_counts[key] = count
    if count < MAX_FETCH_FAILS:
        print(f"[TOURNAMENT POLL WARN] {game}#{mid} {reason} (第 {count}/{MAX_FETCH_FAILS} 次)")
    else:
        _skipped_ids.add(key)
        _fail_counts.pop(key, None)
        print(
            f"[TOURNAMENT POLL WARN] {game}#{mid} {reason} 已失败 {count} 次，"
            f"熔断跳过该 id（如需重试可执行 /theroom --recompute 或重启监控）"
        )


def _notify_new_match(result):
    """新对局入库后向群内推送通知（可选）。"""
    if _sender is None:
        return
    try:
        msg = (
            f"🎮 新对局入库: {result['game_type']}#{result['match_id']} "
            f"({result['player_count']}人)"
        )
        _sender(msg)
    except Exception as e:
        print(f"[TOURNAMENT NOTIFY ERROR] {e}")


def start(sender):
    """初始化锦标赛服务；对局同步由 player_monitor 离服事件触发。"""
    global _sender
    _sender = sender

    if _reader is None:
        init(_config)

    tcfg = tourney_config.get_tournament_config()
    if not tcfg.get("enabled", True):
        print("[TOURNAMENT] 未启用，跳过初始化")
        return
    print("[TOURNAMENT] 已初始化；对局同步已注册到 player_monitor 离服事件")


def stop():
    """停止监控（供优雅退出调用）。"""
    _stop_event.set()


def sync_new_matches():
    """同步所有已启用小游戏的新对局，供 player_monitor 和手动命令调用。"""
    if _reader is None:
        init(_config)
    if not _reader or not _reader.available:
        return {"ok": False, "message": "RCON 未配置"}
    total = 0
    for game in tourney_config.get_enabled_games():
        total += sync_one_game(game["name"], game["storage"])
    return {"ok": True, "ingested": total, "message": f"本次新入库 {total} 局"}


def manual_poll():
    """兼容旧调用名：手动同步所有新对局。"""
    return sync_new_matches()


def manual_recompute():
    """手动触发全量重算（供命令插件调用）。"""
    # 重算前清除熔断状态，以便重新尝试之前跳过的 id
    reset_fetch_state()
    result = recompute_all()
    return {"ok": True, "message": f"重算完成: {result['players']} 玩家 / {result['games']} 小游戏"}


def manual_syncnames():
    """手动从 usercache 同步玩家名（供命令插件调用）。"""
    path = tourney_config.get_usercache_path()
    result = import_usercache(path)
    return {"ok": result["ok"], "message": result["message"]}


def reset_fetch_state():
    """清除拉取熔断状态（失败计数与跳过集合），供重算/重置后重新拉取。"""
    _fail_counts.clear()
    _skipped_ids.clear()
