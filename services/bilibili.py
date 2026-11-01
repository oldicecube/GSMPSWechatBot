"""B站 视频解析与下载。

思路对齐 SnapAny 浏览器扩展（读取站点自己用的 playurl 文档），但那个扩展是靠
hook 已登录页面里的 window.__playinfo__ / fetch / XHR 拿到响应，运行时就是浏览器
本身；这里改为直接调用公开 API，因此：

- 不需要登录态，画质上限为 720P（未登录时 B站 对普通视频的天花板）。
- 用 fnval=1 请求 durl（单文件 MP4），而不是 fnval=4048 的 DASH 分离流，
  于是不需要 ffmpeg 合流。代价是无法拿到 1080P 及以上。
- 所有请求必须带 Referer: https://www.bilibili.com/，否则 CDN 直接 403。
"""

from __future__ import annotations

import os
import re
import time
from html import unescape
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_REFERER = "https://www.bilibili.com/"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

API_VIEW = "https://api.bilibili.com/x/web-interface/view"
API_PLAYURL = "https://api.bilibili.com/x/player/playurl"

# qn -> 画质名。未登录时实际返回值通常止步于 64（720P），所以只要展示
# 服务端回传的 quality，而不是我们请求的那个 qn。
QUALITY_LABELS = {
    6: "240P",
    16: "360P",
    32: "480P",
    64: "720P",
    74: "720P60",
    80: "1080P",
    100: "智能修复",
    112: "1080P+",
    116: "1080P60",
    120: "4K",
    125: "HDR",
}

BVID_RE = re.compile(r"(?<![A-Za-z0-9])BV[0-9A-Za-z]{10}(?![A-Za-z0-9])", re.IGNORECASE)
AVID_RE = re.compile(r"(?<![A-Za-z0-9])av(\d+)(?![A-Za-z0-9])", re.IGNORECASE)
B23_RE = re.compile(
    r"(?<![A-Za-z0-9_./:@%+-])(?:https?://)?b23\.tv/[0-9A-Za-z]+/?(?![0-9A-Za-z_/%-])"
    r"(?:\?[^\s<>\"'{}\[\]（）【】《》，。！？、；：]*)?",
    re.IGNORECASE,
)
BILI_URL_RE = re.compile(r"https?://(?:[\w-]+\.)*(?:bilibili\.com|b23\.tv)/\S*", re.IGNORECASE)
PAGE_RE = re.compile(r"[?&]p=(\d+)")

_config = {}


def configure(config=None):
    global _config
    _config = config if isinstance(config, dict) else {}


def _settings():
    block = _config.get("bilibili") if isinstance(_config, dict) else None
    return block if isinstance(block, dict) else {}


def _headers():
    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": DEFAULT_REFERER,
    }


def _timeout():
    try:
        return max(5.0, min(120.0, float(_settings().get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS))))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_SECONDS


def format_size(num_bytes):
    """把字节数渲染成人类可读体积。"""
    try:
        value = float(num_bytes or 0)
    except (TypeError, ValueError):
        return "未知大小"
    if value >= 1024 * 1024 * 1024:
        return f"{value / (1024 ** 3):.2f} GB"
    if value >= 1024 * 1024:
        return f"{value / (1024 ** 2):.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{int(value)} B"


def quality_label(quality_id):
    try:
        return QUALITY_LABELS.get(int(quality_id), f"{int(quality_id)}P")
    except (TypeError, ValueError):
        return "未知画质"


class BilibiliError(RuntimeError):
    """解析或下载失败；消息面向群聊，可直接展示。"""


def _api_get(url, params):
    try:
        response = requests.get(
            url, params=params, headers=_headers(), timeout=_timeout()
        )
    except requests.RequestException as exc:
        raise BilibiliError(f"网络请求失败（{exc.__class__.__name__}）") from exc

    if response.status_code >= 400:
        raise BilibiliError(f"接口返回 HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise BilibiliError("接口返回的不是 JSON") from exc

    code = payload.get("code")
    if code != 0:
        message = str(payload.get("message") or "").strip() or "未知错误"
        raise BilibiliError(f"B站返回错误 {code}：{message}")
    return payload.get("data") or {}


def extract_short_link(text):
    """Local-only scan of share text, including scheme-less WeChat links."""
    match = B23_RE.search(unescape(str(text or '')))
    if not match:
        return ''
    value = match.group(0).rstrip('.,;:!?)]}')
    if not value.lower().startswith(('http://', 'https://')):
        value = 'https://' + value
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ''))


def _short_redirect_target(url):
    """Validate every hop before making a request; final pages are not fetched."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or '').lower()
        valid = (parsed.scheme in ('http', 'https') and not parsed.username and not parsed.password
                 and parsed.port in (None, 80, 443))
    except ValueError:
        valid = False
    if not valid or not (host == 'b23.tv' or host == 'bilibili.com' or host.endswith('.bilibili.com')):
        raise BilibiliError('短链跳转地址不受支持')
    return host


def _resolve_short_link(url):
    """Read bounded HTTP Location hops without downloading Bilibili HTML."""
    current = url
    visited = set()
    deadline = time.monotonic() + min(20.0, _timeout())
    for _ in range(5):
        host = _short_redirect_target(current)
        if host != 'b23.tv':
            return current
        if current in visited:
            raise BilibiliError('短链出现循环跳转')
        visited.add(current)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BilibiliError('短链解析超时')
        try:
            response = requests.get(current, headers=_headers(), timeout=remaining,
                                    allow_redirects=False, stream=True)
        except requests.RequestException as exc:
            raise BilibiliError(f"短链解析失败（{exc.__class__.__name__}）") from exc
        try:
            if response.status_code >= 400:
                raise BilibiliError(f'短链返回 HTTP {response.status_code}')
            location = response.headers.get('Location')
            if response.status_code not in (301, 302, 303, 307, 308) or not location:
                raise BilibiliError('短链无效或已失效（未返回视频跳转地址）')
            current = urljoin(current, location)
            if _short_redirect_target(current) != 'b23.tv':
                return current
        finally:
            response.close()
    raise BilibiliError('短链跳转次数过多')


def extract_identifier(text):
    """从任意文本里提取 B站 视频标识。

    返回 {"bvid": str|None, "aid": int|None, "page": int}，提取不到时返回 None。
    """
    raw = str(text or "")
    if not raw.strip():
        return None

    page = 1
    page_match = PAGE_RE.search(raw)
    if page_match:
        try:
            page = max(1, int(page_match.group(1)))
        except (TypeError, ValueError):
            page = 1

    short_url = extract_short_link(raw)
    if short_url:
        final_url = _resolve_short_link(short_url)
        # Parse the destination path only. A live/profile/article URL with a BV
        # in a query parameter is not an ordinary downloadable video.
        parsed = urlsplit(final_url)
        video = re.fullmatch(r'/video/(BV[0-9A-Za-z]{10}|av\d+)/?', parsed.path, re.IGNORECASE)
        identifier = extract_identifier(video.group(1)) if video else None
        if identifier:
            # 短链自带的分P参数优先，否则沿用调用方给的
            final_page = PAGE_RE.search('?' + parsed.query)
            if final_page:
                identifier['page'] = max(1, int(final_page.group(1)))
            else:
                identifier["page"] = page
            return identifier
        raise BilibiliError("不支持的短链类型（可能是番剧或直播）")

    bv_match = BVID_RE.search(raw)
    if bv_match:
        raw_bvid = bv_match.group(0)
        bvid = "BV" + raw_bvid[2:]
        return {"bvid": bvid, "aid": None, "page": page}

    av_match = AVID_RE.search(raw)
    if av_match:
        return {"bvid": None, "aid": int(av_match.group(1)), "page": page}

    return None


def fetch_video_info(identifier):
    """取标题、时长与分P列表，并定位到目标分P。"""
    params = {}
    if identifier.get("bvid"):
        params["bvid"] = identifier["bvid"]
    elif identifier.get("aid"):
        params["aid"] = identifier["aid"]
    else:
        raise BilibiliError("缺少视频标识")

    data = _api_get(API_VIEW, params)
    pages = data.get("pages") or []

    page_index = int(identifier.get("page") or 1)
    if pages and not 1 <= page_index <= len(pages):
        raise BilibiliError(f"该视频只有 {len(pages)} 个分P，找不到第 {page_index} 个")

    cid = data.get("cid")
    title = str(data.get("title") or "").strip() or "B站视频"
    duration = data.get("duration") or 0

    if pages:
        selected = pages[page_index - 1]
        cid = selected.get("cid") or cid
        part = str(selected.get("part") or "").strip()
        if part and len(pages) > 1:
            title = f"{title} - {part}"

    if not cid:
        raise BilibiliError("没拿到 cid，无法解析播放地址")

    try:
        duration = int(duration)
    except (TypeError, ValueError):
        duration = 0

    return {
        "bvid": data.get("bvid") or identifier.get("bvid"),
        "aid": data.get("aid") or identifier.get("aid"),
        "cid": cid,
        "title": title,
        "duration": duration,
        "page": page_index,
        "page_count": max(1, len(pages)),
    }


def _request_stream(info, qn):
    """按指定 qn 请求播放地址。fnval=1 才会返回单文件 durl 而非 DASH 分离流。"""
    params = {"cid": info["cid"], "qn": qn, "fnval": 1, "fourk": 1}
    if info.get("bvid"):
        params["bvid"] = info["bvid"]
    elif info.get("aid"):
        params["aid"] = info["aid"]
    else:
        raise BilibiliError("缺少视频标识")

    data = _api_get(API_PLAYURL, params)
    durl = data.get("durl") or []
    if not durl:
        raise BilibiliError("B站没有返回可下载的播放地址（可能是会员或付费视频）")

    fmt = str(data.get("format") or "").lower()
    segments = [str(item.get("url") or "").strip() for item in durl]
    if not all(segments):
        raise BilibiliError("播放地址为空")

    if len(segments) > 1 and not fmt.startswith("flv"):
        raise BilibiliError(f"该视频被切成 {len(segments)} 个分片（{fmt or '未知格式'}），暂不支持合并")

    total = 0
    for item in durl:
        try:
            total += int(item.get("size") or 0)
        except (TypeError, ValueError):
            continue

    quality = data.get("quality") or qn
    return {
        "urls": segments,
        "format": fmt or "mp4",
        "quality": quality,
        "quality_label": quality_label(quality),
        "size": total,
        "is_flv": fmt.startswith("flv"),
    }


def pick_stream(info, max_bytes):
    """720P 优先；预估体积超限就降到 360P；仍然超限则报错。"""
    settings = _settings()

    def _quality(key, default):
        try:
            return int(settings.get(key, default))
        except (TypeError, ValueError):
            return default

    candidates = []
    for qn in (_quality("prefer_quality", 80), _quality("fallback_quality", 32)):
        if qn not in candidates:
            candidates.append(qn)

    attempts = []
    for qn in candidates:
        try:
            stream = _request_stream(info, qn)
        except BilibiliError as exc:
            attempts.append(str(exc))
            continue
        if not max_bytes or stream["size"] <= max_bytes:
            return stream
        attempts.append(
            f"{stream['quality_label']} 有 {format_size(stream['size'])}，超过上限"
        )

    detail = "；".join(attempts) if attempts else "没有可用画质"
    raise BilibiliError(detail)


def download(stream, dest_path, max_bytes=0, progress=None):
    """流式下载到 dest_path，边下边卡体积，避免磁盘被写满。"""
    dest_path = str(dest_path)
    directory = os.path.dirname(dest_path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    written = 0
    started = time.monotonic()
    last_report = 0.0

    try:
        with open(dest_path, "wb") as handle:
            for index, url in enumerate(stream.get("urls") or []):
                response = requests.get(
                    url, headers=_headers(), timeout=_timeout(), stream=True
                )
                if response.status_code != 200:
                    raise BilibiliError(f"下载失败，CDN 返回 HTTP {response.status_code}")
                try:
                    for chunk in response.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        written += len(chunk)
                        if max_bytes and written > max_bytes:
                            raise BilibiliError(
                                f"下载超过体积上限（>{format_size(max_bytes)}）"
                            )
                        handle.write(chunk)

                        now = time.monotonic()
                        if progress and now - last_report >= 2.0:
                            last_report = now
                            elapsed = max(now - started, 0.001)
                            speed = written / elapsed
                            if stream.get("size"):
                                pct = min(99, int(written * 100 / stream["size"]))
                                progress(f"{pct}%", written, stream["size"], speed)
                            else:
                                progress("", written, 0, speed)
                finally:
                    response.close()
    except Exception:
        _remove_quietly(dest_path)
        raise

    if not written:
        _remove_quietly(dest_path)
        raise BilibiliError("下载到的文件是空的")

    return dest_path


def _remove_quietly(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def resolve(text, max_bytes=0):
    """解析出可下载的播放方案，供插件在下载前预检。

    返回 {"info":..., "stream":...}。任何失败都抛 BilibiliError。
    """
    identifier = extract_identifier(text)
    if not identifier:
        raise BilibiliError("没找到 B站 链接")

    info = fetch_video_info(identifier)

    settings = _settings()
    try:
        max_duration = int(settings.get("max_duration_seconds", 1800) or 0)
    except (TypeError, ValueError):
        max_duration = 1800
    if max_duration and info["duration"] > max_duration:
        raise BilibiliError(
            f"视频长 {info['duration'] // 60} 分钟，超过 {max_duration // 60} 分钟上限"
        )

    stream = pick_stream(info, max_bytes)
    return {"info": info, "stream": stream}


def delete_file(path):
    _remove_quietly(path)
