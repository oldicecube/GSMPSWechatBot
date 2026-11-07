from io import BytesIO

import requests
from PIL import Image


BASE_URL = "http://127.0.0.1:5031"
TOKEN = ""


def configure(config=None):
    global TOKEN, BASE_URL

    if not isinstance(config, dict):
        return

    TOKEN = str(config.get("token") or "").strip()
    weflow = config.get("weflow") or {}
    BASE_URL = str(weflow.get("apiBase") or f"http://{weflow.get('apiHost') or '127.0.0.1'}:{weflow.get('apiPort') or 5031}").rstrip('/')


def _headers():
    if not TOKEN:
        return {}
    return {"Authorization": f"Bearer {TOKEN}"}


def _with_base(url):
    if not url:
        return url

    if url.startswith("http://") or url.startswith("https://"):
        return url

    if url.startswith("/"):
        return f"{BASE_URL}{url}"

    return f"{BASE_URL}/{url}"


def extract_timestamp(data):
    if not isinstance(data, dict):
        return None

    for key in ("createTime", "msgTime", "timestamp", "time", "sendTime", "_ts"):
        value = data.get(key)
        if value is None:
            continue

        try:
            return float(value)
        except Exception:
            continue

    return None


def list_messages(session_id, limit=20, media=True, image=True, timeout=8):
    url = f"{BASE_URL}/api/v1/messages"
    params = {
        "talker": session_id,
        "limit": limit,
    }

    if media:
        params["media"] = 1
    if image:
        params["image"] = 1

    response = requests.get(url, params=params, headers=_headers(), timeout=timeout)
    response.raise_for_status()
    data = response.json()
    return data.get("messages", []) or []


def find_image_url_by_timestamp(session_id, target_ts, limit=20):
    msgs = list_messages(session_id=session_id, limit=limit, media=True, image=True)

    # 调试：打印收到的消息
    image_msgs = [m for m in msgs if m.get("localType") == 3]
    print(f"[symmetry-diag] API返回 {len(msgs)} 条消息, 其中图片 {len(image_msgs)} 条")
    for m in image_msgs[:3]:
        print(f"[symmetry-diag]   图片消息 localId={m.get('localId')} mediaType={m.get('mediaType')} mediaUrl={m.get('mediaUrl','(无)')[:60] if m.get('mediaUrl') else '(无)'}")

    best_url = None
    best_diff = None

    for msg in msgs:
        if msg.get("mediaType") != "image":
            continue

        media_url = msg.get("mediaUrl")
        if not media_url:
            continue

        msg_ts = extract_timestamp(msg)
        if target_ts is None or msg_ts is None:
            if best_url is None:
                best_url = media_url
            continue

        diff = abs(msg_ts - target_ts)
        if best_diff is None or diff < best_diff:
            best_diff = diff
            best_url = media_url

            if diff == 0:
                break

    return best_url, best_diff


def download_media(url, timeout=15, *, max_bytes=None):
    final_url = _with_base(url)
    if max_bytes is not None:
        from urllib.parse import urlsplit
        base, destination = urlsplit(BASE_URL), urlsplit(final_url)
        if destination.scheme not in {'http', 'https'}:
            raise ValueError('Unsupported media download URL')
        headers = _headers() if (base.scheme, base.netloc) == (destination.scheme, destination.netloc) else {}
        with requests.get(final_url, headers=headers, timeout=timeout, stream=True) as response:
            response.raise_for_status()
            length = response.headers.get('Content-Length')
            if length and int(length) > max_bytes:
                raise ValueError('图片或表情包超过 20 MB')
            content = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
                content.extend(chunk)
                if len(content) > max_bytes:
                    raise ValueError('图片或表情包超过 20 MB')
            return bytes(content)
    response = requests.get(final_url, headers=_headers(), timeout=timeout)

    if response.status_code == 401 and TOKEN:
        response = requests.get(
            final_url,
            params={"access_token": TOKEN},
            timeout=timeout
        )

    response.raise_for_status()
    return response.content


def load_visual_bytes(context, *, max_bytes=20 * 1024 * 1024):
    """Read this exact image/sticker, preserving animation; never borrow a recent image."""
    import base64
    from llm.core.wechat_translation import wire_fields
    fields = wire_fields(context)
    binary = fields.get('_image_binary') or fields.get('binary_data')
    if isinstance(binary, bytes):
        if len(binary) > max_bytes:
            raise ValueError('图片或表情包超过 20 MB')
        return binary
    encoded = fields.get('base64')
    if encoded:
        if len(encoded) > ((max_bytes + 2) // 3) * 4:
            raise ValueError('图片或表情包超过 20 MB')
        binary = base64.b64decode(encoded, validate=True)
        if len(binary) > max_bytes:
            raise ValueError('图片或表情包超过 20 MB')
        return binary
    if str(fields.get('platform') or fields.get('source_platform') or 'wechat') == 'qq':
        # Native adapter bytes belong to this message; do not consult WeFlow.
        url = fields.get('_image_file_url')
        if url:
            return download_media(url, max_bytes=max_bytes)
        raise ValueError('未取得这条 QQ 图片或表情包的原文件，请重新发送')
    url = fields.get('mediaUrl') or fields.get('media_url')
    if not url:
        from core.weflow_client import WeFlowClient
        client = WeFlowClient(TOKEN, None, None)
        client.api_base = BASE_URL
        expected = fields.get('serverId') or fields.get('svrid') or fields.get('server_id')
        result = client.get_original_message(fields.get('sessionId'),
            fields.get('localId') or fields.get('local_id'), expected, timeout=25, media=True)
        original = (result.get('message') or {}) if result.get('ok') else {}
        actual = original.get('serverIdRaw') or original.get('serverId')
        if expected and actual and str(expected) != '0' and str(actual) != '0' and str(expected) != str(actual):
            raise ValueError('取得的原消息 ID 不匹配，已停止处理')
        url = original.get('mediaUrl') or original.get('media_url')
    if not url:
        raise ValueError('未取得这条图片或表情包的原文件，请重新发送')
    return download_media(str(url), max_bytes=max_bytes)


def download_image(url, timeout=15):
    content = download_media(url, timeout=timeout)
    return Image.open(BytesIO(content)).convert("RGBA")
