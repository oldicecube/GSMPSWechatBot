from mcstatus import JavaServer
import threading

GAME_HOST = "game.sie.gdutmc.com"

FAKE_NAME = "Anonymous Player"


# =========================
# MC 状态查询（核心）
# =========================
def get_status(timeout=3):
    result = {}
    finished = threading.Event()

    def worker():
        try:
            server = JavaServer.lookup(GAME_HOST)
            status = server.status()

            # =========================
            # ✔ 修复点1：正确在线人数
            # =========================
            online_players = status.players.online
            max_players = status.players.max

            # =========================
            # ✔ 修复点2：安全处理 sample
            # =========================
            players = []
            if status.players.sample:
                for p in status.players.sample:
                    if p.name != FAKE_NAME:
                        players.append(p.name)

            result["data"] = {
                "online": True,
                "latency_ms": round(status.latency, 2),
                "online_players": online_players,   # ✔ 用真实值
                "max_players": max_players,
                "players": players
            }

        except Exception as e:
            result["error"] = str(e)

        finally:
            finished.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    if not finished.wait(timeout):
        return None

    if "error" in result:
        return None

    return result["data"]


# =========================
# 对外 API：状态（不变）
# =========================
def status():
    return get_status()


# =========================
# 对外 API：玩家列表（不变）
# =========================
def player_list():
    data = get_status()
    if not data:
        return None
    return data.get("players", [])