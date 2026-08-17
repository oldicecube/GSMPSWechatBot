import os
import random
import threading
import time

import requests
from mcrcon import MCRcon


SEND_DELAY_CONFIG = {
    "enabled": False,
    "min_seconds": 0.0,
    "max_seconds": 0.0
}

# Optional observers are kept outside the LLM layer so every successfully
# delivered Bot text (including plugin output) can be counted consistently.
_SEND_LISTENER_LOCK = threading.RLock()
_SEND_LISTENERS = {}

# 全局发送互斥锁：串行化所有发送（文本/文件/语音/RCON），
# 防止多个线程/发送路径同时操作微信窗口（切群、点击、键盘注入、语音 Shift 长按）造成冲突。
_SEND_LOCK = threading.RLock()


def register_send_listener(name, callback):
    """Register or replace a best-effort listener for successful sends."""
    key = str(name or "").strip()
    if not key:
        return False
    with _SEND_LISTENER_LOCK:
        if callable(callback):
            _SEND_LISTENERS[key] = callback
        else:
            _SEND_LISTENERS.pop(key, None)
    return True


def _notify_send_listeners(target, mode, content=None, file_path=None):
    with _SEND_LISTENER_LOCK:
        listeners = list(_SEND_LISTENERS.values())
    for callback in listeners:
        try:
            callback(target=target, mode=mode, content=content, file_path=file_path)
        except Exception as exc:
            print(f"[SEND LISTENER ERROR] {exc}")


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
    if mode not in ("wechat_text", "wechat_file", "wechat_voice"):
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
def send(target, content=None, file_path=None, mode="wechat_text", rcon=None, delay_seconds=None, duration=None, voice_start=None):
    """
    mode:
        wechat_text
        wechat_file
        wechat_voice
        rcon
    """

    with _SEND_LOCK:
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
                result = _send_wechat_text(target, content)
                if result and result[0]:
                    _notify_send_listeners(target, mode, content=content)
                return result

            # =========================
            # 📁 微信文件
            # =========================
            if mode == "wechat_file":
                _apply_send_delay(mode, delay_seconds=delay_seconds)
                result = _send_wechat_file(target, file_path)
                if result and result[0]:
                    _notify_send_listeners(target, mode, file_path=file_path)
                return result

            # =========================
            # 🎙️ 微信语音（注入虚拟麦克风）
            # =========================
            if mode == "wechat_voice":
                if _is_invalid_content(file_path):
                    print("[SEND BLOCK] invalid voice file -> blocked")
                    return False, "invalid voice file"
                _apply_send_delay(mode, delay_seconds=delay_seconds)
                result = _send_wechat_voice(target, file_path, duration, voice_start)
                if result and result[0]:
                    _notify_send_listeners(target, mode, file_path=file_path)
                return result

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
# 🎙️ 微信语音
# =========================
def _send_wechat_voice(target, voice_path, duration=None, voice_start=None):
    if not voice_path:
        return False, "voice_path is empty"

    voice_path = str(voice_path)

    if not os.path.exists(voice_path):
        return False, "voice file not found"

    url = "http://localhost:9999/wxSend"

    data = {
        "target": target,
        "voice": voice_path
    }
    if duration is not None:
        try:
            # 支持双精度浮点秒（如 59.5），钳制到 1~60 秒
            data["duration"] = max(1.0, min(float(duration), 60.0))
        except (TypeError, ValueError):
            return False, "invalid duration"
    if voice_start is not None:
        try:
            # 起始秒（从音频第 N 秒开始注入），非负
            data["start"] = max(0.0, float(voice_start))
        except (TypeError, ValueError):
            return False, "invalid voice start"

    # 语音注入最长 60 秒 + 群聊切换耗时，超时放宽到 90 秒
    requests.post(url, json=data, timeout=90)
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
