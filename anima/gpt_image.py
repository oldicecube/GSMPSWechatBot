"""GPT Image 远端生图客户端（OpenAI 兼容网关）。

用于 /anima 的 ``gi2`` 子命令：文生图走 ``/images/generations``，
带参考图（同时写了「图生图」）时走 ``/images/edits``。
网关返回 ``b64_json`` 或 ``url``，两种都支持。
"""
from __future__ import annotations

import base64
import secrets
from pathlib import Path

import requests

DEFAULT_API_BASE = "https://api.uni-token.com/v1"
DEFAULT_MODEL = "gpt-image-2.6"
DEFAULT_SIZE = "1024x1024"
DEFAULT_TIMEOUT = 300
MAX_IMAGE_BYTES = 40 * 1024 * 1024
MIME_BY_SUFFIX = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


class GPTImageError(RuntimeError):
    """Raised when the gateway refuses or returns an unusable payload."""


class GPTImageClient:
    """Minimal OpenAI-compatible images client for the uni-token gateway."""

    def __init__(self, base, key, model=None, size=None, timeout=None, output_dir=None):
        self.api_base = str(base or DEFAULT_API_BASE).strip().rstrip("/")
        if not self.api_base.endswith("/v1"):
            # 允许只填域名；网关的 OpenAI 兼容入口固定挂在 /v1 下。
            self.api_base += "/v1"
        self.api_key = str(key or "").strip()
        self.model = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.size = str(size or DEFAULT_SIZE).strip() or DEFAULT_SIZE
        self.timeout = int(timeout or DEFAULT_TIMEOUT)
        self.output_dir = Path(output_dir) if output_dir else Path.cwd()

    # -- helpers ---------------------------------------------------------
    def _headers(self, json_body=False):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if json_body:
            headers["Content-Type"] = "application/json"
        return headers

    def check(self):
        """Return ``(ready, reason)`` after a cheap gateway probe."""

        if not self.api_key:
            return False, "未配置 GPT Image API Key（/anima gi2 需要）"
        try:
            # 首次请求要付 DNS + TLS 冷启动，10s 太紧会偶发误报“连不上”，放宽到 20s。
            response = requests.get(self.api_base + "/models", headers=self._headers(), timeout=20)
        except requests.RequestException as exc:
            return False, f"无法连接 GPT Image 网关：{type(exc).__name__}"
        if response.status_code != 200:
            return False, f"GPT Image 网关鉴权或服务异常（HTTP {response.status_code}）"
        return True, ""

    @staticmethod
    def _error_from(response):
        detail = ""
        try:
            body = response.json()
            error = body.get("error") if isinstance(body, dict) else None
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("code") or "")
            elif isinstance(error, str):
                detail = error
            elif isinstance(body, dict):
                detail = str(body.get("message") or "")
        except ValueError:
            detail = (response.text or "")[:200]
        detail = detail.strip().replace("\n", " ")
        return f"GPT Image 请求失败（HTTP {response.status_code}）" + (f"：{detail[:200]}" if detail else "")

    def _save(self, item):
        """Decode ``b64_json`` (or download ``url``) into the output cache."""

        raw = None
        if item.get("b64_json"):
            encoded = str(item["b64_json"])
            if encoded.startswith("data:"):
                encoded = encoded.split(",", 1)[-1]
            try:
                raw = base64.b64decode(encoded, validate=False)
            except (ValueError, TypeError) as exc:
                raise GPTImageError(f"返回图片无法解码：{type(exc).__name__}") from exc
        elif item.get("url"):
            try:
                download = requests.get(str(item["url"]), timeout=self.timeout)
                download.raise_for_status()
                raw = download.content
            except requests.RequestException as exc:
                raise GPTImageError(f"返回图片下载失败：{type(exc).__name__}") from exc
        if not raw:
            raise GPTImageError("网关没有返回图片数据")
        if len(raw) > MAX_IMAGE_BYTES:
            raise GPTImageError("返回图片过大，已放弃保存")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        suffix = ".png"
        mime = str(item.get("mime_type") or "").lower()
        for ext, mime_type in MIME_BY_SUFFIX.items():
            if mime == mime_type:
                suffix = ext
                break
        target = self.output_dir / f"gi2_{secrets.token_hex(12)}{suffix}"
        target.write_bytes(raw)
        return target

    # -- public ----------------------------------------------------------
    def generate(self, prompt, reference_png=None):
        """Generate (or edit) one image and return the saved file path."""

        prompt = str(prompt or "").strip()
        if not prompt:
            raise GPTImageError("提示词为空")
        if len(prompt) > 4000:
            raise GPTImageError("提示词超过 4000 字")

        if reference_png:
            files = {"image": ("reference.png", reference_png, "image/png")}
            data = {"model": self.model, "prompt": prompt, "n": "1", "size": self.size}
            try:
                response = requests.post(
                    self.api_base + "/images/edits",
                    headers=self._headers(),
                    files=files,
                    data=data,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise GPTImageError(f"图生图请求失败：{type(exc).__name__}") from exc
        else:
            payload = {"model": self.model, "prompt": prompt, "n": 1, "size": self.size}
            try:
                response = requests.post(
                    self.api_base + "/images/generations",
                    headers=self._headers(json_body=True),
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise GPTImageError(f"文生图请求失败：{type(exc).__name__}") from exc

        if response.status_code != 200:
            raise GPTImageError(self._error_from(response))
        try:
            body = response.json()
        except ValueError as exc:
            raise GPTImageError("网关返回了非 JSON 响应") from exc
        items = body.get("data") if isinstance(body, dict) else None
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            raise GPTImageError("网关响应里没有图片数据")
        return self._save(items[0])
