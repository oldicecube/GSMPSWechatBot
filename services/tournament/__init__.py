# -*- coding: utf-8 -*-
"""
Minecraft 锦标赛系统

模块组成：
    config         锦标赛配置读取
    snbt           Minecraft SNBT (Stringified NBT) 解析器
    uuid_utils     Minecraft UUID [I;a,b,c,d] -> 标准格式转换
    scoring        积分算法（独立模块，可单独测试/复用）
    db             数据库模型、迁移、CRUD
    storage_reader RCON Storage 读取器
    ingest         比赛自动入库 + 积分重算
    leaderboard    排行榜查询与格式化

数据流：
    Minecraft Storage -> RCON -> snbt 解析 -> uuid 转换
    -> 查询/创建玩家 -> 写入 matches -> 写入 match_players
    -> 计算 match_score -> 更新 game_scores/season_scores -> 排行榜
"""

from .config import get_tournament_config  # noqa: F401
