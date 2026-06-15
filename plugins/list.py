try:
    from services.mc_api import player_list
except Exception:
    player_list = None

COMMAND = "/list"


def handle(content, context):
    if player_list is None:
        return "服务模块未加载（services.mc_api 导入失败）"

    try:
        data = player_list()
    except Exception:
        data = None

    if not data:
        return "当前无玩家或获取失败"

    return "当前在线玩家：\n" + "\n".join(data)