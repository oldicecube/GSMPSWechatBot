"""Tools exposed to the LLM.

The tool schema follows DeepSeek's OpenAI-compatible tool-calling format.  The
model only chooses the URL; this module performs the actual network request and
returns a bounded, plain-text representation of the page.
"""

from __future__ import annotations

import html
import ipaddress
import json
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
import requests


WEB_FETCH_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_webpage",
        "description": (
            "Fetch readable text from one public HTTP or HTTPS webpage. "
            "Use it when the user asks about a specific URL or needs current "
            "page content. Do not use it for local or private network URLs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The complete http:// or https:// URL to fetch.",
                }
            },
            "required": ["url"],
        },
    },
}

ORIGINAL_MESSAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "fetch_original_message",
        "description": (
            "Fetch the fully decoded and parsed content of one WeFlow message when the supplied "
            "message looks incomplete, such as a forwarded message or chat log. "
            "Only use an ID already present in the current conversation context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Current WeChat session ID."},
                "local_id": {"type": "integer", "description": "Message localId, if present."},
                "server_id": {"type": "string", "description": "Message serverId/svrid/rawid, if present."},
            },
            "required": ["session_id"],
        },
    },
}

DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_CHARS = 24000
AUTO_REPLY_URL_HOSTS = ("bilibili.com", "b23.tv")
DEFAULT_WEFLOW_API_BASE = "http://127.0.0.1:5031"
DEFAULT_ORIGINAL_MESSAGE_MAX_CHARS = 16000


def is_url_only(value: str) -> bool:
    """Return whether a message consists solely of one HTTP(S) URL."""
    text = str(value or "").strip()
    if not text or any(char.isspace() for char in text):
        return False
    parsed = urlparse(text)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _matches_allowed_host(hostname: str, allowed_hosts=None) -> bool:
    if not allowed_hosts:
        return True
    hostname = str(hostname or "").rstrip(".").lower()
    return any(
        hostname == str(item).rstrip(".").lower()
        or hostname.endswith("." + str(item).rstrip(".").lower())
        for item in allowed_hosts
    )


class _TextExtractor(HTMLParser):
    """Small dependency-free HTML to text converter."""

    _IGNORED_TAGS = {"script", "style", "noscript", "template", "svg"}
    _BLOCK_TAGS = {
        "address", "article", "aside", "blockquote", "br", "div", "dl",
        "dt", "dd", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "tr", "ul",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in self._IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        elif not self._ignored_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        value = html.unescape("".join(self.parts))
        lines = [" ".join(line.split()) for line in value.splitlines()]
        return "\n".join(line for line in lines if line).strip()


def _is_public_hostname(hostname: str) -> bool:
    hostname = hostname.rstrip(".").lower()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        return False

    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(item[4][0])
                for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
            ]
        except OSError as exc:
            raise ValueError(f"无法解析网址主机名: {hostname}") from exc

    return all(
        not (address.is_private or address.is_loopback or address.is_link_local
             or address.is_reserved or address.is_multicast or address.is_unspecified)
        for address in addresses
    )


def _validate_url(url: str, allowed_hosts=None) -> str:
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只允许访问完整的 http 或 https 网址")
    if parsed.username or parsed.password:
        raise ValueError("网址不能包含用户名或密码")
    if not _matches_allowed_host(parsed.hostname, allowed_hosts):
        raise ValueError("此主动回复仅允许检查 bilibili.com 或 b23.tv")
    if not _is_public_hostname(parsed.hostname):
        raise ValueError("出于安全原因，不允许访问本机或内网地址")
    return value


def fetch_webpage(url: str, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                  max_chars: int = DEFAULT_MAX_CHARS, allowed_hosts=None) -> dict:
    """Fetch and extract a bounded page body for a tool result."""
    try:
        safe_url = _validate_url(url, allowed_hosts=allowed_hosts)
        timeout = max(1.0, min(float(timeout), 30.0))
        max_chars = max(1000, min(int(max_chars), 50000))

        headers = {
            "User-Agent": "WechatBot/1.0 (LLM webpage tool)",
            "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.1",
        }
        with httpx.Client(timeout=timeout, follow_redirects=False, headers=headers) as client:
            response = None
            current_url = safe_url
            for _ in range(5):
                response = client.get(current_url)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = response.headers.get("location")
                if not location:
                    break
                current_url = _validate_url(
                    urljoin(current_url, location),
                    allowed_hosts=allowed_hosts,
                )
            if response is None:
                raise ValueError("网页请求失败")
            response.raise_for_status()

        content_type = response.headers.get("content-type", "").lower()
        raw = response.content[: max_chars * 8]
        encoding = response.encoding or "utf-8"
        body = raw.decode(encoding, errors="replace")

        if "html" in content_type or "<html" in body[:1000].lower():
            parser = _TextExtractor()
            parser.feed(body)
            content = parser.text()
        else:
            content = "\n".join(" ".join(line.split()) for line in body.splitlines()).strip()

        content = content[:max_chars]
        if not content:
            content = "网页没有可读取的文本内容"

        return {
            "ok": True,
            "url": str(response.url),
            "status_code": response.status_code,
            "content": content,
        }
    except Exception as exc:
        return {"ok": False, "url": str(url or ""), "error": str(exc)[:500]}


def fetch_original_message(session_id: str, local_id=None, server_id=None, *,
                           current_session_id=None, api_base=DEFAULT_WEFLOW_API_BASE,
                           api_token="", timeout=8.0,
                           max_chars=DEFAULT_ORIGINAL_MESSAGE_MAX_CHARS) -> dict:
    """Fetch one fully decoded WeFlow message, restricted to the active session."""
    requested_session = str(session_id or "").strip()
    active_session = str(current_session_id or "").strip()
    if not requested_session or not active_session or requested_session != active_session:
        return {"ok": False, "error": "只能查询当前会话的消息"}

    try:
        parsed_local_id = int(local_id) if local_id is not None else 0
    except (TypeError, ValueError):
        parsed_local_id = 0
    normalized_server_id = str(server_id or "").strip()
    if parsed_local_id <= 0 and not normalized_server_id:
        return {"ok": False, "error": "需要 local_id 或 server_id"}

    params = {"talker": requested_session}
    if parsed_local_id > 0:
        params["localId"] = parsed_local_id
    else:
        params["serverId"] = normalized_server_id

    try:
        safe_base = str(api_base or DEFAULT_WEFLOW_API_BASE).rstrip("/")
        response = requests.get(
            f"{safe_base}/api/v1/messages/original",
            params=params,
            headers={"Authorization": f"Bearer {str(api_token or '').strip()}"},
            timeout=max(1.0, min(float(timeout), 20.0)),
        )
        if response.status_code == 404:
            return {"ok": False, "error": "未找到原始消息"}
        response.raise_for_status()
        payload = response.json()
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            return {"ok": False, "error": "WeFlow 返回的原始消息格式无效"}

        # WeFlow has already decoded and enriched this object. Preserve every
        # returned field, including chatRecordList and type-specific metadata;
        # the model needs those fields to reconstruct forwarded content.
        return {"ok": True, "session_id": requested_session, "message": message}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:500]}


def execute_tool(name: str, arguments: str | dict, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
                 max_chars: int = DEFAULT_MAX_CHARS, allowed_hosts=None,
                 current_session_id=None, api_base=DEFAULT_WEFLOW_API_BASE,
                 api_token="", original_timeout=8.0,
                 original_max_chars=DEFAULT_ORIGINAL_MESSAGE_MAX_CHARS) -> str:
    """Execute one model-requested tool and return JSON for the tool message."""
    known_names = {
        WEB_FETCH_TOOL["function"]["name"],
        ORIGINAL_MESSAGE_TOOL["function"]["name"],
    }
    if name not in known_names:
        return json.dumps({"ok": False, "error": f"未知工具: {name}"}, ensure_ascii=False)

    try:
        payload = arguments if isinstance(arguments, dict) else json.loads(arguments or "{}")
        if name == ORIGINAL_MESSAGE_TOOL["function"]["name"]:
            if not isinstance(payload, dict):
                raise ValueError("工具参数必须是 JSON 对象")
            result = fetch_original_message(
                payload.get("session_id"),
                payload.get("local_id"),
                payload.get("server_id"),
                current_session_id=current_session_id,
                api_base=api_base,
                api_token=api_token,
                timeout=original_timeout,
                max_chars=original_max_chars,
            )
            return json.dumps(result, ensure_ascii=False)
        if not isinstance(payload, dict) or not isinstance(payload.get("url"), str):
            raise ValueError("工具参数必须是包含 url 字符串的 JSON 对象")
        result = fetch_webpage(
            payload["url"],
            timeout=timeout,
            max_chars=max_chars,
            allowed_hosts=allowed_hosts,
        )
    except Exception as exc:
        result = {"ok": False, "error": str(exc)[:500]}

    return json.dumps(result, ensure_ascii=False)
