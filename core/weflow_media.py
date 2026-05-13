from io import BytesIO

import requests
from PIL import Image


BASE_URL = "http://127.0.0.1:5031"
TOKEN = ""


def configure(config=None):
    global TOKEN

    if not isinstance(config, dict):
        return

    TOKEN = str(config.get("token") or "").strip()


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


def download_media(url, timeout=15):
    final_url = _with_base(url)
    response = requests.get(final_url, headers=_headers(), timeout=timeout)

    if response.status_code == 401 and TOKEN:
        response = requests.get(
            final_url,
            params={"access_token": TOKEN},
            timeout=timeout
        )

    response.raise_for_status()
    return response.content


def download_image(url, timeout=15):
    content = download_media(url, timeout=timeout)
    return Image.open(BytesIO(content)).convert("RGBA")
