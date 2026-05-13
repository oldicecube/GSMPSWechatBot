import os
import random
import time

import requests
from mcrcon import MCRcon


SEND_DELAY_CONFIG = {
    "enabled": False,
    "min_seconds": 0.0,
    "max_seconds": 0.0
}


# =========================
# 🚫 内容合法性检查（核心）
# =========================
def _is_invalid_content(content):
    if content is None:
        return True

    if isinstance(content, str):
        c = content.strip().lower()

        # 拦截“伪空内容”
        if c in ("none", "null", "undefined", ""):
            return True

    return False


# =========================
# 🧠 文本处理
# =========================
def _normalize_content(content):
    if content is None:
        return ""

    content = str(content)

    # 再次兜底防止 "None" 漏网
    if content.strip().lower() in ("none", "null", "undefined"):
        return ""

    return content.replace("\r\n", "\n").replace("\r", "\n")


def configure(config=None):
    delay_cfg = (config or {}).get("send_delay", {}) or {}

    enabled = bool(delay_cfg.get("enabled", False))

    try:
        min_seconds = float(delay_cfg.get("min_seconds", 0))
    except Exception:
        min_seconds = 0.0

    try:
        max_seconds = float(delay_cfg.get("max_seconds", 0))
    except Exception:
        max_seconds = 0.0

    min_seconds = max(0.0, min_seconds)
    max_seconds = max(0.0, max_seconds)

    if max_seconds < min_seconds:
        min_seconds, max_seconds = max_seconds, min_seconds

    SEND_DELAY_CONFIG["enabled"] = enabled
    SEND_DELAY_CONFIG["min_seconds"] = min_seconds
    SEND_DELAY_CONFIG["max_seconds"] = max_seconds


def preview_delay_seconds(mode="wechat_text"):
    if mode not in ("wechat_text", "wechat_file"):
        return 0.0

    if not SEND_DELAY_CONFIG["enabled"]:
        return 0.0

    min_seconds = SEND_DELAY_CONFIG["min_seconds"]
    max_seconds = SEND_DELAY_CONFIG["max_seconds"]

    if max_seconds <= 0:
        return 0.0

    if min_seconds == max_seconds:
        return min_seconds

    return random.uniform(min_seconds, max_seconds)


def _apply_send_delay(mode, delay_seconds=None):
    actual_delay = delay_seconds
    if actual_delay is None:
        actual_delay = preview_delay_seconds(mode)

    try:
        actual_delay = float(actual_delay)
    except Exception:
        actual_delay = 0.0

    actual_delay = max(0.0, actual_delay)

    if actual_delay > 0:
        print(f"[SEND DELAY] mode={mode}, sleep={actual_delay:.3f}s")
        time.sleep(actual_delay)

    return actual_delay


# =========================
# 💬 统一发送入口
# =========================
def send(target, content=None, file_path=None, mode="wechat_text", rcon=None, delay_seconds=None):
    """
    mode:
        wechat_text
        wechat_file
        rcon
    """

    try:

        # =========================
        # 🚨 全局拦截（修复版）
        # =========================
        if mode in ("wechat_text", "rcon") and _is_invalid_content(content):
            print("[SEND BLOCK] invalid content -> blocked")
            return False, "invalid content"

        # =========================
        # 💬 微信文本
        # =========================
        if mode == "wechat_text":
            content = _normalize_content(content)

            if not content.strip():
                print("[SEND BLOCK] empty content after normalize")
                return False, "empty content"

            _apply_send_delay(mode, delay_seconds=delay_seconds)
            return _send_wechat_text(target, content)

        # =========================
        # 📁 微信文件
        # =========================
        if mode == "wechat_file":
            _apply_send_delay(mode, delay_seconds=delay_seconds)
            return _send_wechat_file(target, file_path)

        # =========================
        # 🟢 RCON
        # =========================
        if mode == "rcon":
            if _is_invalid_content(content):
                return False, "invalid content"

            return _send_rcon(content, rcon)

        return False, "unknown mode"

    except Exception as e:
        return False, str(e)


# =========================
# 💬 微信文本
# =========================
def _send_wechat_text(target, content):
    url = "http://localhost:9999/wxSend"

    data = {
        "target": target,
        "content": content
    }

    requests.post(url, json=data, timeout=5)
    return True, None


# =========================
# 📁 微信文件
# =========================
def _send_wechat_file(target, file_path):
    if not file_path:
        return False, "file_path is empty"

    file_path = str(file_path)

    if not os.path.exists(file_path):
        return False, "file not found"

    url = "http://localhost:9999/wxSend"

    data = {
        "target": target,
        "file": file_path
    }

    requests.post(url, json=data, timeout=10)
    return True, None


# =========================
# 🟢 RCON
# =========================
def _send_rcon(content, rcon):
    if not rcon:
        return False, "missing rcon config"

    host = rcon.get("host")
    port = rcon.get("port")
    password = rcon.get("password")

    if not all([host, port, password]):
        return False, "invalid rcon config"

    with MCRcon(host, password, port) as mcr:
        _send_multiline_rcon(mcr, content)

    return True, None


# =========================
# 🧠 RCON 多行
# =========================
def _send_multiline_rcon(mcr, msg: str):
    if not msg:
        return

    for line in msg.split("\n"):
        line = line.strip()
        if not line:
            continue

        line = line.replace("\\", "\\\\").replace('"', '\\"')

        cmd = f'tellraw @a {{"text":"{line}","color":"green"}}'
        mcr.command(cmd)
