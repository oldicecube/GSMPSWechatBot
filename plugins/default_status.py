try:
    from services.mc_api import configure as configure_mc_api
    from services.mc_api import status
except Exception:
    # 防止热加载/路径问题直接炸插件
    configure_mc_api = None
    status = None

COMMAND = "/default_status"


def init(config):
    if configure_mc_api is not None:
        configure_mc_api(config)


def handle(content, context):
    if status is None:
        return "服务模块未加载（services.mc_api 导入失败）"

    try:
        data = status()
    except Exception:
        data = None

    if not data:
        return "服务器：关闭/不可用\n延迟：N/A\n玩家：0/0"

    online = "开启" if data.get("online") else "关闭"

    latency = data.get("latency_ms")
    latency = f"{latency:.1f}" if isinstance(latency, (int, float)) else "N/A"

    online_players = data.get("online_players", 0)
    max_players = data.get("max_players", 0)

    return (
        f"服务器当前状态：{online}\n"
        f"延迟：{latency} ms\n"
        f"当前玩家数：{online_players}/{max_players}"
    )
