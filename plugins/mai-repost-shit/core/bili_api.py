"""B站数据源与下载客户端。

三条硬规则（都是实测踩出来的，改代码前先读）：
  1. 搜索接口必须带 buvid3（先访问一次首页种 cookie），否则返回 -412。
  2. 评论接口必须完全不带 cookie：一旦带上 buvid3，每页只给 3 条且 is_end=True。
     所以这里开两个 client——搜索用带 cookie 的，评论用裸的，绝不混用。
  3. 视频 CDN 校验 Referer，必须带 https://www.bilibili.com/ 才能下载；错误 Referer 直接 403。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

API_HOST = "api.bilibili.com"
WEB_HOST = "www.bilibili.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REFERER = f"https://{WEB_HOST}/"

# 接口路径白名单，避免出现可拼接的任意 URL
PATH_NAV = "/x/web-interface/nav"
PATH_SEARCH = "/x/web-interface/search/type"
PATH_VIEW = "/x/web-interface/view"
PATH_REPLY = "/x/v2/reply/wbi/main"
PATH_PLAYURL = "/x/player/playurl"

# wbi 签名用的 mixin key 打乱表（B站前端固定算法，公开）
_MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

_AT_RE = re.compile(r"@[\w\u4e00-\u9fa5\-]{1,20}")
_WBI_TTL = 1800.0


def _is_public_https(url: str) -> bool:
    """只放行 https，且目标主机不能解析到内网/环回/链路本地地址。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


class BiliError(RuntimeError):
    """B站接口返回了非预期结果。"""


class BiliClient:
    """B站免登录客户端（搜索 / 详情 / 评论 / 播放地址 / 下载）。"""

    def __init__(self, logger: Any, timeout: float = 20.0) -> None:
        self._logger = logger
        self._timeout = timeout
        # 搜索侧：带 cookie jar，靠访问首页拿到 buvid3
        self._search_client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": UA, "Referer": REFERER},
            follow_redirects=False,
        )
        # 评论侧：零 cookie，一旦带上 buvid3 会被降级成每页 3 条
        self._bare_client = httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": UA, "Referer": REFERER},
            follow_redirects=False,
        )
        self._warmed = False
        self._mixin_key: Optional[str] = None
        self._mixin_key_at = 0.0
        # 全局节流：相邻请求至少间隔 1.2s，避免触发风控
        self._last_request = 0.0

    async def aclose(self) -> None:
        await self._search_client.aclose()
        await self._bare_client.aclose()

    async def _throttle(self) -> None:
        gap = 1.2 - (time.monotonic() - self._last_request)
        if gap > 0:
            await asyncio.sleep(gap)
        self._last_request = time.monotonic()

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        allow_error: bool = False,
    ) -> Dict[str, Any]:
        """请求 api.bilibili.com 的白名单路径并解析 JSON。"""
        url = f"https://{API_HOST}{path}"
        await self._throttle()
        try:
            resp = await client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise BiliError(f"请求 {path} 失败: {exc!r}") from exc
        if resp.status_code != 200:
            raise BiliError(f"请求 {path} 返回 HTTP {resp.status_code}")
        data = resp.json()
        code = data.get("code")
        if code != 0 and not allow_error:
            raise BiliError(f"{path} 返回 code={code} message={data.get('message')}")
        return data

    async def warmup(self) -> None:
        """访问首页种下 buvid3（搜索接口的前置条件）。"""
        if self._warmed:
            return
        try:
            await self._search_client.get(f"https://{WEB_HOST}/")
        except httpx.HTTPError as exc:
            self._logger.warning(f"B站首页预热失败（搜索可能返回 -412）: {exc!r}")
        self._warmed = True

    # ===== 评论 @率判据 =====

    @staticmethod
    def at_ratio(messages: List[str]) -> Tuple[float, int, int]:
        """返回 (含@比例, 含@条数, 总条数)。"""
        if not messages:
            return 0.0, 0, 0
        hit = sum(1 for msg in messages if _AT_RE.search(msg))
        return hit / len(messages), hit, len(messages)

    async def _mixin(self) -> str:
        """取 wbi 签名密钥，缓存 30 分钟。

        注意：匿名请求 nav 会返回 code=-101（账号未登录），但 wbi_img 密钥照常给出，
        所以这里必须放行非零 code，否则拿不到密钥、评论功能整个失效。
        """
        if self._mixin_key and time.monotonic() - self._mixin_key_at < _WBI_TTL:
            return self._mixin_key
        data = await self._get_json(self._bare_client, PATH_NAV, allow_error=True)
        wbi = (data.get("data") or {}).get("wbi_img") or {}
        img = (wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
        sub = (wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
        if not img or not sub:
            raise BiliError(f"nav 接口没有返回 wbi 密钥（code={data.get('code')}）")
        raw = img + sub
        self._mixin_key = "".join(raw[i] for i in _MIXIN_KEY_ENC_TAB)[:32]
        self._mixin_key_at = time.monotonic()
        return self._mixin_key

    async def _signed(self, params: Dict[str, Any]) -> Dict[str, str]:
        """给参数做 wbi 签名（wts + w_rid）。"""
        mixin = await self._mixin()
        signed = {k: str(v) for k, v in params.items()}
        signed["wts"] = str(int(time.time()))
        query = urllib.parse.urlencode(sorted(signed.items()))
        # md5 是 B站 wbi 协议的既定算法，不是安全用途
        signed["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
        return signed

    async def fetch_comments(self, aid: int, pages: int) -> List[str]:
        """按游标翻页取评论正文（裸请求，每页 20 条）。"""
        messages: List[str] = []
        cursor = 0
        for _ in range(max(1, pages)):
            params = await self._signed({"oid": aid, "type": 1, "mode": 3, "ps": 20, "next": cursor})
            data = await self._get_json(self._bare_client, PATH_REPLY, params)
            payload = data.get("data") or {}
            replies = payload.get("replies") or []
            messages.extend((item.get("content") or {}).get("message", "") for item in replies)
            cursor_info = payload.get("cursor") or {}
            cursor = cursor_info.get("next") or 0
            if cursor_info.get("is_end") or not replies:
                break
        return [msg for msg in messages if msg]

    # ===== 检索 =====

    async def search_videos(
        self,
        keyword: str,
        order: str,
        page: int,
        window_begin: int,
        window_end: int,
        play_min: int,
        play_max: int,
        video_review_min: int,
        video_review_max: int = 0,
    ) -> List[Dict[str, Any]]:
        """按关键词 + 时间窗搜索，返回落在播放量区间内的候选。"""
        await self.warmup()
        params = {
            "search_type": "video",
            "keyword": keyword,
            "order": order,
            "page": page,
            "page_size": 50,
            "duration": 1,  # 只找 10 分钟以内，避免搬来超长视频
            "pubtime_begin_s": window_begin,
            "pubtime_end_s": window_end,
        }
        data = await self._get_json(self._search_client, PATH_SEARCH, params, allow_error=True)
        if data.get("code") != 0:
            self._logger.warning(f"搜索 {keyword} 返回 code={data.get('code')}，跳过本轮该关键词")
            return []
        out: List[Dict[str, Any]] = []
        for item in (data.get("data") or {}).get("result") or []:
            play = int(item.get("play") or 0)
            review = int(item.get("video_review") or 0)
            if not (play_min <= play <= play_max and review >= video_review_min):
                continue
            # 评论数上限：评论特别多的多是解说/剧集/大热门，实测艾特率反而低
            if video_review_max and review > video_review_max:
                continue
            out.append(item)
        return out

    async def video_detail(self, bvid: str) -> Dict[str, Any]:
        data = await self._get_json(self._bare_client, PATH_VIEW, {"bvid": bvid})
        return data.get("data") or {}

    # ===== 下载 =====

    async def playurl(self, bvid: str, cid: int, quality_preference: List[int]) -> Dict[str, Any]:
        """取播放地址。durl 模式（fnval=1）免登录最高给到 720P。"""
        params = {
            "bvid": bvid,
            "cid": cid,
            "qn": quality_preference[0] if quality_preference else 64,
            "fnval": 1,
            "fnver": 0,
            "fourk": 0,
        }
        data = await self._get_json(self._search_client, PATH_PLAYURL, params, allow_error=True)
        if data.get("code") != 0:
            raise BiliError(f"取播放地址失败 code={data.get('code')} message={data.get('message')}")
        payload = data.get("data") or {}
        durl = payload.get("durl") or []
        # 保留 size：调用方要拿它在下载前判断体积是否超限
        return {
            "quality": payload.get("quality"),
            "accept_quality": payload.get("accept_quality") or [],
            "segments": [
                {"url": item.get("url"), "size": item.get("size"), "length": item.get("length")}
                for item in durl
            ],
        }

    async def download(self, url: str, dest: Path, max_bytes: int) -> int:
        """带 Referer 下载到本地，边下边校验大小上限。"""
        if not _is_public_https(url):
            raise BiliError("播放地址不在公网 https 范围内，拒绝下载")
        written = 0
        await self._throttle()
        async with self._search_client.stream("GET", url, headers={"Referer": REFERER}) as resp:
            if resp.status_code != 200:
                raise BiliError(f"下载视频返回 HTTP {resp.status_code}")
            with dest.open("wb") as fp:
                async for chunk in resp.aiter_bytes(256 * 1024):
                    written += len(chunk)
                    if written > max_bytes:
                        raise BiliError(f"视频超过大小上限 {max_bytes // 1024 // 1024}MB，放弃")
                    fp.write(chunk)
        return written
