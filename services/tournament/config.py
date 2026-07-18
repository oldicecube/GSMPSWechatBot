# -*- coding: utf-8 -*-
"""锦标赛配置读取。

从全局 config.json 的 "tournament" 段读取配置，提供合理默认值。

配置示例（config.json）：

    "tournament": {
        "enabled": true,
        "min_matches_for_ranking": 5,
        "rcon_timeout": 10,
        "games": {
            "Lucky Pillar": {"storage": "luckypillar:games", "enabled": true},
            "Miner Chaos":  {"storage": "miner_chaos:games", "enabled": true}
        }
    }
"""

from __future__ import annotations


_DEFAULTS = {
    "enabled": True,
    "min_matches_for_ranking": 5,
    "rcon_timeout": 10,
    "leaderboard_top": 10,
    "usercache_path": "",
}

# 内置小游戏默认 Storage 命名空间
_DEFAULT_GAMES = {
    "Lucky Pillar": {"storage": "luckypillar:games", "enabled": True},
    "Miner Chaos": {"storage": "miner_chaos:games", "enabled": True},
}

_config_cache = None


def set_config(config):
    """注入/刷新全局配置缓存（由 main / dispatcher init 调用）。"""
    global _config_cache
    _config_cache = config or {}


def get_tournament_config():
    """返回锦标赛配置字典（合并默认值）。"""
    raw = {}
    if isinstance(_config_cache, dict):
        raw = _config_cache.get("tournament", {}) or {}
    elif isinstance(_config_cache, dict) is False:
        raw = {}

    cfg = dict(_DEFAULTS)
    cfg.update({k: v for k, v in raw.items() if v is not None})

    # games 段合并默认值
    games = dict(_DEFAULT_GAMES)
    user_games = raw.get("games", {}) or {}
    for name, gcfg in user_games.items():
        merged = dict(games.get(name, {"storage": None, "enabled": False}))
        merged.update(gcfg or {})
        games[name] = merged
    cfg["games"] = games

    return cfg


def get_rcon_config():
    """返回 RCON 配置（来自 config.json 的 rcon 段）。"""
    if not isinstance(_config_cache, dict):
        return {}
    return _config_cache.get("rcon", {}) or {}


def get_usercache_path():
    """返回 usercache.json 路径（用于 uuid↔name 映射导入）。"""
    cfg = get_tournament_config()
    return cfg.get("usercache_path", "") or ""


def get_enabled_games():
    """返回已启用的小游戏列表 [{name, storage}]。"""
    cfg = get_tournament_config()
    games = cfg.get("games", {}) or {}
    result = []
    for name, gcfg in games.items():
        if gcfg.get("enabled") and gcfg.get("storage"):
            result.append({"name": name, "storage": gcfg["storage"]})
    return result
