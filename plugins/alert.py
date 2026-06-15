import logging
from core.sender import send

CONFIG = None
COMMAND = "/alert"

logger = logging.getLogger("alert_plugin")
logger.setLevel(logging.INFO)

if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(h)


def init(config):
    global CONFIG
    CONFIG = config
    logger.info("Alert插件已初始化")


def _normalize_content(content):
    if content is None:
        return ""

    if isinstance(content, (list, tuple)):
        return "\n".join(map(str, content))

    if isinstance(content, dict):
        return str(content)

    return str(content)


def handle(content, context):
    if CONFIG is None:
        return "插件未初始化"

    rcon_cfg = CONFIG.get("rcon", {})
    host = rcon_cfg.get("host")
    port = rcon_cfg.get("port")
    password = rcon_cfg.get("password")

    if not host or not port or not password:
        return "RCON配置不完整"

    username = context.get("user", "未知用户")

    msg_text = _normalize_content(context.get("content", content))
    msg = f"[微信群] <{username}>\n{msg_text}"

    try:
        ok, err = send(
            target="",
            content=msg,
            mode="rcon",
            rcon={
                "host": host,
                "port": port,
                "password": password
            }
        )

        return "发送成功" if ok else f"发送失败: {err}"

    except Exception as e:
        logger.exception("调用sender失败")
        return f"发送失败: {str(e)}"