# -*- coding: utf-8 -*-
"""Wechatbot /redeem plugin backed by PrintServer coupon credits."""

from __future__ import annotations

import os
import threading
import time
import uuid

import requests

from utils.points_manager import add_points, get_points, try_spend_points


COMMAND = "/redeem"
_PENDING_TTL = 90

PRINT_COUPON = {
    "id": "print_coupon",
    "name": "一张打印券",
    "cost": 500,
    "coupons": 1,
    "aliases": {"打印券", "一张打印券", "print_coupon"},
}

_config = {}
_PENDING = {}
_LOCK = threading.RLock()


def init(config):
    """Receive the global Wechatbot config during plugin initialization."""
    global _config
    _config = config if isinstance(config, dict) else {}


def _wxid(context):
    from utils.account_identity import canonical_user
    context = context if isinstance(context, dict) else {}
    raw = context.get("raw") if isinstance(context.get("raw"), dict) else {}
    return canonical_user(str(context.get("wxid") or raw.get("wxid") or "").strip())


def _group_name(context):
    context = context if isinstance(context, dict) else {}
    return (
        str(context.get("group") or context.get("sessionId") or "当前群聊").strip()[:80]
        or "当前群聊"
    )


def _normalize_product(content):
    if isinstance(content, (list, tuple)):
        content = " ".join(str(item) for item in content)
    value = str(content or "").strip().casefold()
    aliases = {str(item).casefold() for item in PRINT_COUPON["aliases"]}
    return PRINT_COUPON if value in aliases else None


def _server_settings():
    settings = _config.get("print_server") or _config.get("printServer") or {}
    if not isinstance(settings, dict):
        settings = {}
    url = str(
        settings.get("redeem_url")
        or settings.get("redeemUrl")
        or os.getenv("PRINTSERVER_REDEEM_URL")
        or "http://127.0.0.1:3001/api/redeem/bot"
    ).strip()
    token = str(
        settings.get("redeem_token")
        or settings.get("redeemToken")
        or os.getenv("PRINTSERVER_REDEEM_TOKEN")
        or ""
    ).strip()
    timeout = settings.get("timeout_seconds", settings.get("timeoutSeconds", 8))
    try:
        timeout = max(1.0, min(float(timeout), 30.0))
    except (TypeError, ValueError):
        timeout = 8.0
    return url, token, timeout


def _print_server_redeem(wxid, product, request_id):
    """Return (success, result). Network errors are explicitly marked for retry."""
    url, token, timeout = _server_settings()
    if not token:
        return False, {
            "kind": "configuration",
            "message": "PrintServer 兑换接口尚未配置，请联系管理员",
        }

    try:
        response = requests.post(
            url,
            json={
                "wxid": wxid,
                "productId": product["id"],
                "couponCount": product["coupons"],
                "requestId": request_id,
            },
            headers={
                "Content-Type": "application/json",
                "X-PrintServer-Token": token,
            },
            timeout=timeout,
        )
        try:
            data = response.json() or {}
        except ValueError:
            data = {}
        if 200 <= response.status_code < 300 and data.get("ok"):
            return True, data
        return False, {
            "kind": "server",
            "message": str(
                data.get("error") or f"PrintServer 返回 HTTP {response.status_code}"
            ).strip(),
        }
    except requests.RequestException as exc:
        # Keep the same request_id and the charged state so a retry is safe even
        # when PrintServer completed the transaction but the response was lost.
        print(
            f"[REDEEM] PrintServer request failed request={request_id}: {exc}",
            flush=True,
        )
        return False, {
            "kind": "network",
            "message": "兑换服务暂时无法连接，请稍后再次输入 /redeem 打印券 重试",
        }


def _pending_key(context, wxid):
    # A confirmation must not be reusable across users or groups.
    return _group_name(context), wxid


def _cleanup_pending(now=None):
    now = time.time() if now is None else now
    expired = [
        key
        for key, value in _PENDING.items()
        if float(value.get("expires_at") or 0) <= now
    ]
    for key in expired:
        _PENDING.pop(key, None)


def _confirmation_message(context, product):
    return (
        f"若（{_group_name(context)}）确定使用 {product['cost']} 积分兑换 "
        f"{product['name']}，请再次输入 /redeem 打印券 来确认兑换"
    )


def handle(content, context):
    from utils.points_manager import binding_required
    error = binding_required(context)
    if error:
        return error
    context = context if isinstance(context, dict) else {}
    product = _normalize_product(content)
    if product is None:
        if not str(content or "").strip():
            return "请使用 /redeem 打印券 兑换一张打印券（500积分）"
        return "暂时只支持兑换：一张打印券（500积分）"

    wxid = _wxid(context)
    if not wxid:
        return "无法识别当前微信账户，暂时不能兑换打印券"

    key = _pending_key(context, wxid)
    now = time.time()
    with _LOCK:
        _cleanup_pending(now)
        pending = _PENDING.get(key)
        is_confirmation = bool(
            pending
            and pending.get("product_id") == product["id"]
            and float(pending.get("expires_at") or 0) > now
        )
        if not is_confirmation:
            _PENDING[key] = {
                "product_id": product["id"],
                "request_id": uuid.uuid4().hex,
                "created_at": now,
                "expires_at": now + _PENDING_TTL,
                "points_charged": False,
                "processing": False,
            }
            return _confirmation_message(context, product)
        if pending.get("processing"):
            return "兑换正在处理中，请稍候再试"
        pending["processing"] = True
        request_id = str(pending.get("request_id") or uuid.uuid4().hex)
        already_charged = bool(pending.get("points_charged"))

    # On a network retry, points were already charged and must not be charged a
    # second time. PrintServer's request_id makes the server-side grant idempotent.
    remaining = get_points(wxid)
    if not already_charged:
        try:
            ok, remaining = try_spend_points(wxid, product["cost"])
        except Exception as exc:
            with _LOCK:
                current = _PENDING.get(key)
                if current:
                    current["processing"] = False
            print(f"[REDEEM] point spend failed wxid={wxid}: {exc}", flush=True)
            return "积分服务暂时不可用，请稍后重试"
        if not ok:
            with _LOCK:
                _PENDING.pop(key, None)
            return f"积分不足，兑换一张打印券需要 {product['cost']} 积分"
        with _LOCK:
            current = _PENDING.get(key)
            if current:
                current["points_charged"] = True

    success, result = _print_server_redeem(wxid, product, request_id)
    if success:
        with _LOCK:
            _PENDING.pop(key, None)
        print(
            f"[REDEEM] wxid={wxid} product={product['id']} cost={product['cost']} "
            f"remaining={float(remaining):.1f} request={request_id}",
            flush=True,
        )
        return f"兑换成功！已发放 {product['name']}，当前积分余额：{float(remaining):.1f}"

    kind = result.get("kind") if isinstance(result, dict) else "server"
    message = result.get("message") if isinstance(result, dict) else str(result)
    if kind == "network":
        with _LOCK:
            current = _PENDING.get(key)
            if current:
                current["processing"] = False
                current["points_charged"] = True
        return message

    # A definite server rejection means no coupon was granted, so refund the
    # points. A network uncertainty is intentionally handled above without a refund.
    add_points(wxid, product["cost"])
    with _LOCK:
        _PENDING.pop(key, None)
    return f"兑换失败：{message}，已退回 {product['cost']} 积分"

WEBUI_CONFIG = {'version': 1, 'fields': {
    'redeem_url': {'type': 'str', 'default': 'http://127.0.0.1:3001/api/redeem/bot', 'label': '兑换服务地址', 'config_path': 'print_server.redeem_url', 'reload': 'restart'},
    'redeem_token': {'type': 'str', 'default': '', 'secret': True, 'label': '兑换服务令牌', 'config_path': 'print_server.redeem_token', 'reload': 'restart'},
    'timeout_seconds': {'type': 'float', 'default': 8, 'min': 1, 'max': 30, 'label': '请求超时（秒）', 'config_path': 'print_server.timeout_seconds', 'reload': 'restart'},
}}

WEBUI_CONFIG_SCHEMA = {
    "title": "兑换服务",
    "fields": [
        {"key": "redeem_url", "type": "string", "label": "服务地址"},
        {"key": "redeem_token", "type": "secret", "label": "服务令牌"},
        {"key": "timeout_seconds", "type": "number", "label": "超时秒数"},
    ],
}
