import os

from utils.sqlite_store import player_stats_all, player_stats_ensure, player_stats_update

COMMAND = "/bind"
BASE_DIR = os.path.dirname(os.path.dirname(__file__))


def handle(content, context):

    if not content:
        return False, "用法: /bind <玩家名>"

    if isinstance(content, list):
        content = content[0]

    player = str(content).strip()

    # 显示名称（用于提示）
    username = (
        context.get("user")
        or context.get("raw", {}).get("user")
        or context.get("raw", {}).get("sourceName")
        or "未知用户"
    )

    # 实际绑定ID（wxid）
    wxid = (
        context.get("raw", {}).get("wxid")
        or context.get("wxid")
    )

    if not wxid:
        return False, "无法获取用户wxid"

    data = player_stats_all()
    if data is None:
        return False, "数据文件损坏"

    if not data:
        return False, "数据文件不存在"

    if player not in data:
        return False, f"玩家不存在: {player}"

    # 若该玩家已被其他 wxid 绑定，则禁止抢绑
    player_info = data.get(player)
    if isinstance(player_info, dict):
        bound_wxid = player_info.get("bind_user")
        if isinstance(bound_wxid, str):
            bound_wxid = bound_wxid.strip()
            if bound_wxid and bound_wxid != wxid:
                return False, f"玩家 {player} 已被其他用户绑定"

    # 清除该 wxid 旧绑定
    for p, info in data.items():
        if isinstance(info, dict):
            if info.get("bind_user") == wxid:
                player_stats_update(p, bind_user=None)

    # 确保玩家记录存在
    player_stats_ensure(player)

    # 写入新绑定（存 wxid）
    try:
        player_stats_update(player, bind_user=wxid)
    except Exception:
        return False, "写入失败"

    # 提示仍显示用户名
    return f"绑定成功：{username} → {player}"
