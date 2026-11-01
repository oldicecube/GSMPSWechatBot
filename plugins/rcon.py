"""/rcon: execute one raw RCON command for administrators."""

from mcrcon import MCRcon


COMMAND = "/rcon"
_config = {}


def init(config):
    global _config
    _config = config or {}


def _is_admin(context):
    wxid = str(
        (context or {}).get("wxid")
        or (context or {}).get("raw", {}).get("wxid")
        or ""
    ).strip()
    admin_wxids = (_config.get("llm", {}) or {}).get("admin_wxids", []) or []
    return wxid in {str(item).strip() for item in admin_wxids}


def handle(content, context):
    if not _is_admin(context):
        return "仅管理员可用"

    command = str(content or "").strip()
    if not command:
        return "用法：/rcon <服务器指令>"

    rcon_config = _config.get("rcon", {}) or {}
    host = rcon_config.get("host")
    port = rcon_config.get("port")
    password = rcon_config.get("password")
    if not all([host, port, password]):
        return "RCON配置不完整"

    try:
        with MCRcon(host, password, port=port) as client:
            response = client.command(command)
    except Exception as exc:
        return f"RCON执行失败: {exc}"

    response = "" if response is None else str(response).strip()
    return response or "发送成功，此命令无返回值"


WEBUI_CONFIG = {'version': 1, 'fields': {
    'host': {'type': 'str', 'default': '', 'max_length': 250, 'label': 'RCON 主机', 'config_path': 'rcon.host', 'reload': 'restart'},
    'port': {'type': 'int', 'default': 25575, 'min': 1, 'max': 65535, 'label': 'RCON 端口', 'config_path': 'rcon.port', 'reload': 'restart'},
    'password': {'type': 'str', 'default': '', 'secret': True, 'label': 'RCON 密码', 'config_path': 'rcon.password', 'reload': 'restart'},
}}

WEBUI_CONFIG_SCHEMA = {
    "title": "RCON",
    "fields": [
        {"key": "host", "type": "string", "label": "主机"},
        {"key": "port", "type": "number", "label": "端口"},
        {"key": "password", "type": "secret", "label": "密码"},
    ],
}
