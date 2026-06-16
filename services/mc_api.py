from mcstatus import JavaServer
import threading

DEFAULT_GAME_HOST = "game.sie.gdutmc.com"
_game_host = DEFAULT_GAME_HOST
_game_port = None

FAKE_NAME = "Anonymous Player"


def configure(config=None):
    global _game_host, _game_port

    mc_config = {}
    if isinstance(config, dict):
        mc_config = (
            config.get("mc")
            or config.get("minecraft")
            or config.get("mc_server")
            or {}
        )

    host = mc_config.get("host") or mc_config.get("server") or DEFAULT_GAME_HOST
    port = mc_config.get("port")

    _game_host = str(host).strip() or DEFAULT_GAME_HOST

    try:
        _game_port = int(port) if port not in (None, "") else None
    except (TypeError, ValueError):
        _game_port = None


def _create_server():
    if _game_port is None:
        return JavaServer.lookup(_game_host)
    return JavaServer(_game_host, _game_port)


# =========================
# MC 状态查询（核心）
# =========================
def get_status(timeout=3):
    result = {}
    finished = threading.Event()

    def worker():
        try:
            server = _create_server()
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
