# -*- coding: utf-8 -*-
"""/acgpic - send one random ACG landscape or portrait image.

The API's content is random; image orientation only selects the documented
horizontal (pc.php) or vertical (pe.php) endpoint. A successful invocation
sends exactly one image and no accompanying text. Errors are sent as text.
"""
from __future__ import annotations

# GSMPS WebUI metadata only; execution remains in this shell module.
WEBUI_CONFIG = {'version': 1, 'fields': {}}


import os
import secrets
import threading
import time
import uuid
from pathlib import Path

import requests

from core.sender import send

COMMAND = "/acgpic"
ALIASES = []

BASE_DIR = Path(__file__).resolve().parent.parent
TEMP_DIR = BASE_DIR / "temp" / "acgpic"
LANDSCAPE_API = "https://api.yppp.net/pc.php?return=json"
PORTRAIT_API = "https://api.yppp.net/pe.php?return=json"
TIMEOUT_SECONDS = 15
MAX_IMAGE_BYTES = 20 * 1024 * 1024
LOCAL_COOLDOWN_SECONDS = 3.0

_lock = threading.RLock()
_last_request_by_target = {}
_config = {}


def init(config):
    global _config
    _config = config if isinstance(config, dict) else {}
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    print("[PLUGIN] /acgpic random image plugin ready", flush=True)


def _target(context):
    context = context if isinstance(context, dict) else {}
    return str(context.get("group") or context.get("user") or "").strip()


def _error_text(reason):
    return f"ACG 图片获取失败：{reason}"


def _rate_guard(target):
    now = time.monotonic()
    with _lock:
        previous = float(_last_request_by_target.get(target, 0.0) or 0.0)
        if now - previous < LOCAL_COOLDOWN_SECONDS:
            return False, LOCAL_COOLDOWN_SECONDS - (now - previous)
        _last_request_by_target[target] = now
    return True, 0.0


def _request_image_url(endpoint):
    try:
        response = requests.get(
            endpoint,
            headers={"User-Agent": "Wechatbot/ACGPic", "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"网络请求失败：{type(exc).__name__}") from exc

    if response.status_code == 429:
        raise RuntimeError("接口请求过于频繁，请稍后再试（HTTP 429）")
    if response.status_code >= 400:
        raise RuntimeError(f"接口返回 HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("接口返回格式异常") from exc

    code = str(payload.get("code") or "")
    if code != "200":
        message = str(payload.get("message") or payload.get("msg") or "接口拒绝请求").strip()
        if "频" in message or "限" in message or code in {"429", "509", "403"}:
            raise RuntimeError(f"接口限流或拒绝：{message}")
        raise RuntimeError(f"接口错误 {code or 'unknown'}：{message}")
    image_url = str(payload.get("acgurl") or "").strip()
    if not image_url.lower().startswith(("http://", "https://")):
        raise RuntimeError("接口未返回有效图片地址")
    return image_url, payload


def _download(url, suffix):
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    path = TEMP_DIR / f"acgpic_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.{suffix}"
    written = 0
    response = None
    try:
        response = requests.get(
            url,
            headers={"User-Agent": "Wechatbot/ACGPic", "Referer": "https://blog.yeqing.net/acg-api/"},
            timeout=TIMEOUT_SECONDS,
            stream=True,
        )
        if response.status_code == 429:
            raise RuntimeError("图片下载接口限流，请稍后再试（HTTP 429）")
        if response.status_code >= 400:
            raise RuntimeError(f"图片下载失败：HTTP {response.status_code}")
        with path.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_IMAGE_BYTES:
                    raise RuntimeError("图片超过大小限制")
                handle.write(chunk)
        if written <= 0:
            raise RuntimeError("图片内容为空")
        return str(path)
    except Exception:
        _delete(path)
        raise
    finally:
        try:
            response.close()
        except Exception:
            pass


def _delete(path):
    try:
        Path(path).unlink(missing_ok=True)
    except Exception:
        pass


def handle(args, context):
    args = str(args or "").strip()
    context = context if isinstance(context, dict) else {}
    target = _target(context)
    if args:
        return "用法：/acgpic（不需要参数）"
    if not target:
        return _error_text("找不到发送目标")

    allowed, wait = _rate_guard(target)
    if not allowed:
        return _error_text(f"本地请求冷却中，请 {max(1, int(wait + 0.99))} 秒后再试")

    endpoint = secrets.choice((LANDSCAPE_API, PORTRAIT_API))
    suffix = "jpg"
    path = None
    try:
        url, payload = _request_image_url(endpoint)
        suffix = str(payload.get("size") or "jpg").lower().lstrip(".")
        if suffix not in {"jpg", "jpeg", "png", "gif", "webp"}:
            suffix = "jpg"
        path = _download(url, suffix)
        ok, error = send(target=target, file_path=path, mode="wechat_file")
        if not ok:
            return _error_text(f"图片发送失败：{error}")
        # Success intentionally returns None: only the image is sent.
        return None
    except Exception as exc:
        print(f"[ACGPIC] failed target={target}: {type(exc).__name__}: {exc}", flush=True)
        return _error_text(str(exc))
    finally:
        if path:
            _delete(path)
