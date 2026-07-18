# -*- coding: utf-8 -*-
"""/escape 命令 — 自助 RCON 脱离卡死。

不带参数：使用当前微信绑定的 MC 玩家名作为选择器执行：
  1. 移除 theroom / luckypillar / battlepaint / minerchaos 四个 tag
  2. 出生点设为 -409 71 -55
  3. 游戏模式改为生存
  4. 杀死玩家（强制重置位置与状态）
  5. tellraw 提示玩家
"""

from __future__ import annotations

from mcrcon import MCRcon

from utils.sqlite_store import player_stats_all

COMMAND = "/escape"

_config = {}


def init(config):
    global _config
    _config = config or {}


def _get_bound_player(wxid):
    """返回当前微信账号绑定的 MC 玩家名，未绑定返回 None。"""
    data = player_stats_all()
    if not data:
        return None
    for player, stats in data.items():
        if not isinstance(stats, dict):
            continue
        bind_user = stats.get("bind_user")
        if isinstance(bind_user, str) and bind_user.strip() == wxid:
            return player
    return None


def _run_escape_rcon(player_name):
    """通过 RCON 对指定玩家执行脱离卡死操作。"""
    rcon_cfg = _config.get("rcon", {}) or {}
    host = rcon_cfg.get("host")
    port = rcon_cfg.get("port")
    password = rcon_cfg.get("password")

    if not all([host, port, password]):
        return False, "RCON 未配置"

    commands = [
        f"tag {player_name} remove theroom",
        f"tag {player_name} remove luckypillar",
        f"tag {player_name} remove battlepaint",
        f"tag {player_name} remove minerchaos",
        f"execute in minecraft:overworld run spawnpoint {player_name} -409 71 -55",
        f"gamemode survival {player_name}",
        f"kill {player_name}",
        f'tellraw {player_name} {{"text":"已尝试 脱离卡死，若问题仍然存在，请联系管理员","color":"gold"}}',
    ]

    try:
        with MCRcon(host, password, port=port) as mcr:
            for cmd in commands:
                mcr.command(cmd)
        return True, None
    except Exception as e:
        return False, str(e)


def handle(content, context):
    wxid = str(context.get("wxid") or context.get("raw", {}).get("wxid") or "").strip()
    if not wxid:
        return "无法获取用户信息，请先绑定玩家 (/bind <玩家名>)"

    player_name = _get_bound_player(wxid)
    if not player_name:
        return "你还未绑定玩家，请先使用 /bind <玩家名> 绑定"

    ok, err = _run_escape_rcon(player_name)
    if not ok:
        print(f"[ESCAPE ERROR] {player_name}: {err}")
        return f"脱离卡死失败: {err or '未知错误'}"

    return f"已对 {player_name} 执行脱离卡死，请稍后重连"
