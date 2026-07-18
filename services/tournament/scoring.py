# -*- coding: utf-8 -*-
"""锦标赛积分算法（独立模块）

本模块不依赖数据库与 RCON，仅包含纯函数，便于单元测试与复用。
所有公式均来自锦标赛规则文档。

================================================================
一、单局积分（满分 100）
================================================================

    match_score = performance_score + player_count_score

    表现分：
        performance_score = 80 * (N - R) / (N - 1)
        N = 本局人数, R = 玩家名次（1 = 第一名）
        第一名 80 分，最后一名 0 分

    人数质量分（抑制小规模刷分）：
        3 人 -> 8
        4 人 -> 11
        5 人 -> 14
        6 人 -> 16
        7 人 -> 18
        >=8 -> 20

================================================================
二、赛季积分
================================================================

    final_score = match_average_score * 0.9 + activity_score

    对局表现：
        match_average_score = 所有有效比赛 match_score 的平均值

    活跃奖励：
        activity_score = min(match_count / 20 * 10, 10)   # 最高 10

================================================================
三、排行榜资格
================================================================

    正式排名需至少参加 MIN_MATCHES_FOR_RANKING（默认 5）局；
    不足则显示但不参与排名。
"""

from __future__ import annotations


# ---------------- 常量 ----------------

PERFORMANCE_MAX = 80.0          # 表现分满分
PLAYER_COUNT_SCORE_MAX = 20.0   # 人数质量分上限（8 人及以上）

# 人数 -> 质量分 查表（<3 人视为无效局，质量分为 0）
PLAYER_COUNT_SCORE_TABLE = {
    3: 8.0,
    4: 11.0,
    5: 14.0,
    6: 16.0,
    7: 18.0,
}

SEASON_MATCH_WEIGHT = 0.9       # 对局表现权重
SEASON_ACTIVITY_WEIGHT = 0.1    # 活跃奖励权重（隐含在 activity 上限 10 中）

ACTIVITY_CAP_MATCHES = 20       # 满活跃所需局数
ACTIVITY_MAX_SCORE = 10.0       # 活跃奖励上限

MIN_PLAYER_COUNT = 3            # 有效局最低人数
DEFAULT_MIN_MATCHES_FOR_RANKING = 5  # 正式排名最低参赛局数


# ---------------- 单局积分 ----------------

def player_count_quality_score(player_count: int) -> float:
    """人数质量分。"""
    if player_count < MIN_PLAYER_COUNT:
        return 0.0
    if player_count >= 8:
        return PLAYER_COUNT_SCORE_MAX
    return PLAYER_COUNT_SCORE_TABLE.get(player_count, 0.0)


def performance_score(player_count: int, rank: int) -> float:
    """表现分 = 80 * (N - R) / (N - 1)。

    第一名得 80，最后一名得 0。N<=1 时返回 0。
    """
    if player_count <= 1:
        return 0.0
    if rank <= 1:
        return PERFORMANCE_MAX
    if rank >= player_count:
        return 0.0
    return PERFORMANCE_MAX * (player_count - rank) / (player_count - 1)


def calc_match_score(player_count: int, rank: int) -> float:
    """计算单局积分（0-100）。

    不足 3 人的局返回 0（无效局，聚合时会被过滤）。
    """
    if player_count < MIN_PLAYER_COUNT:
        return 0.0
    perf = performance_score(player_count, rank)
    quality = player_count_quality_score(player_count)
    return round(perf + quality, 4)


# ---------------- 赛季积分 ----------------

def activity_score(match_count: int) -> float:
    """活跃奖励 = min(局数 / 20 * 10, 10)。"""
    if match_count <= 0:
        return 0.0
    raw = (match_count / ACTIVITY_CAP_MATCHES) * ACTIVITY_MAX_SCORE
    return round(min(raw, ACTIVITY_MAX_SCORE), 4)


def calc_season_score(match_average_score: float, match_count: int) -> float:
    """赛季最终积分 = 平均分 * 0.9 + 活跃奖励。"""
    if match_count <= 0:
        return 0.0
    activity = activity_score(match_count)
    final = match_average_score * SEASON_MATCH_WEIGHT + activity
    return round(final, 4)


def is_ranked(match_count: int, min_matches: int = DEFAULT_MIN_MATCHES_FOR_RANKING) -> bool:
    """是否满足正式排名条件。"""
    return match_count >= max(1, min_matches)
