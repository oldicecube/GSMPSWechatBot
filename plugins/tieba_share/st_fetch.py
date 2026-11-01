"""贴吧 protobuf 抓取层。

这一层跟 :mod:`st_tieba` 的 HTML 路线**并行**，不是替换关系：

* HTML 路线负责「贴吧原页面」长图的**原始页面**（``StoneDetail.raw``）——
  那必须是真的 HTML，protobuf 给不了。
* 这一层负责 HTML 拿不到或拿不好的东西：**帖子点赞数**（列表页 ThreadInfo.agree）、
  **翻页**（HTML 的 ``pn=`` 对 ``kz=`` 页面完全无效，protobuf 的 ``pn`` 真能翻）、
  **楼中楼正文**（HTML 只渲染前几条且没有点赞数，protobuf 能按 ``floor_rn`` 要更多）。

两个接口都不需要登录，也不需要 BDUSS —— 实测无 cookie 直接 POST 就是 ``errno=0``。
（以前这里写过「还要播种访客 cookie 保持出口指纹一致」，但这一层其实从来没有
播种过 cookie，那句话是错的：protobuf 接口按上面的实测根本不需要它。）
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import httpx

from st_client import (
    FRS_CMD,
    FRS_URL,
    PAGE_CMD,
    PAGE_URL,
    build_frs_request,
    build_page_request,
    content_images,
    content_text,
    looks_like_proto,
    multipart_body,
    parse_frs_response,
    parse_page_response,
    proto_headers,
)
from st_tieba import (
    _BLOCK_STATUS,
    BLOCK_COOLDOWN_SECONDS,
    StoneBlockedError,
    StoneComment,
    StoneDetail,
    StoneFloor,
    StoneThread,
    StoneThread as _StoneThread,
    normalize_forum,
)

logger = __import__("logging").getLogger(__name__)

# 视频帖的 thread_type 标记（实测 40 = 视频帖，与 st_tieba 的 HTML 判定口径一致）
VIDEO_THREAD_TYPE = 40

# protobuf 路线一次能要多少层：接口自己按 30 一页切，要更多就得翻页
PB_PAGE_SIZE = 30
# 翻页硬上限。max_floors 传 0 时语义是「不限制」，但**不能真的不限制**：
# 服务端一旦忽略 pn 反复返回满页，或者帖子上万层，这里就会一直翻下去，
# 楼层列表无限增长直到把内存吃光。20 页 = 600 层，远超实际用得上的深度
# （配置上限是 60 层），纯粹是给循环一个出口。
PB_MAX_PAGES = 20



class BlockState:
    """反爬冷却的共享状态。

    两个客户端（HTML 与 protobuf）共用一份：拦的是这台机器，不是某个接口，
    一个接口被拦时另一个继续敲只会加深封禁。
    """

    __slots__ = ("_at",)

    def __init__(self) -> None:
        self._at = 0.0

    def mark(self) -> None:
        """记下「刚刚被拦了」。"""
        self._at = time.monotonic()

    def clear(self) -> None:
        """手动解除冷却。只清冷却、不动搬运记录。"""
        self._at = 0.0

    def remaining(self) -> float:
        """还在冷却期里就返回剩余秒数，否则 0。"""
        if not self._at:
            return 0.0
        left = BLOCK_COOLDOWN_SECONDS - (time.monotonic() - self._at)
        return left if left > 0 else 0.0


def comment_from_proto(raw: dict[str, Any], *, author_name: str = "") -> StoneComment:
    """把楼中楼字典转成 :class:`StoneComment`。

    接口不给「被回复人是谁」，正文里只有字面的「回复 」（网页版才有
    ``<a href="/home/main?un=...">`` 那种链接）。原样保留，不做猜测性补全。
    """
    text = str(raw.get("text") or "").strip()
    who = str(raw.get("author") or "").strip() or author_name
    return StoneComment(author=who, text=text, is_author=False)


def _floor_time_text(value: Any) -> str:
    """把楼层时间统一成人读文本。

    两条路线给的格式不一样：HTML 路线从页面里抠出来的本来就是
    「2026-10-18 12:34」这样的文本，protobuf 给的是 Unix 秒。
    以前直接把数字转成字符串塞进去，长图上那一层就显示成 1700000000。
    小数字（< 1e9，多半是相对量或压根不是时间）原样留着。
    """
    text = str(value or "").strip()
    if not text.isdigit():
        return text
    stamp = int(text)
    if stamp < 1000000000:
        return text
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))
    except (OSError, ValueError, OverflowError):
        return text


def floor_from_proto(raw: dict[str, Any], *, comment_author: str = "") -> StoneFloor:
    """把楼层字典转成 :class:`StoneFloor`。

    ``agree`` 是楼层自己的点赞数（protobuf 独有，HTML 页面给不了）。
    """
    floor = StoneFloor(
        fn=int(raw.get("fn") or 0),
        author=str(raw.get("author") or "").strip() or comment_author,
        text=content_text(list(raw.get("content") or [])),
        time=_floor_time_text(raw.get("time")),
        comment_total=int(raw.get("comment_total") or 0),
        agree=int(raw.get("agree") or 0),
    )
    for url in content_images(list(raw.get("content") or [])):
        if url:
            floor.images.append(url)
    for item in raw.get("comments") or []:
        floor.comments.append(comment_from_proto(item))
    return floor


def thread_from_proto(raw: dict[str, Any]) -> StoneThread:
    """把 :func:`st_client.parse_frs_response` 的字典转成 :class:`StoneThread`。"""
    return StoneThread(
        tid=str(raw.get("tid") or ""),
        title=str(raw.get("title") or ""),
        forum=str(raw.get("forum") or ""),
        is_top=bool(raw.get("is_top")),
        replies=int(raw.get("replies") or 0),
        thread_type=int(raw.get("thread_type") or 0),
        agree=int(raw.get("agree") or 0),
        views=int(raw.get("views") or 0),
        is_good=bool(raw.get("is_good")),
        create_time=int(raw.get("create_time") or 0),
    )



class StoneProtoClient:
    """贴吧 protobuf 只读客户端。

    跟 :class:`st_tieba.StoneTiebaClient` 共用同一套「延迟 + 反爬冷却」语义，
    但走的是 ``tiebac.baidu.com`` 的 protobuf 接口。反爬拦截（403/429/456/503）
    同样会抛 :class:`StoneBlockedError`，让整轮抓取立刻停下。

    两个客户端**共用一份冷却状态**时最安全 —— 拦的是这台机器，不是某个接口。
    所以 ``blocked_flag`` 参数允许外部传入一个共享的标记容器；不传就自己管。
    """

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        proxy: str = "",
        delay_seconds: float = 3.0,
        retries: int = 2,
        shared_block: "BlockState | None" = None,
    ) -> None:
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.delay_seconds = max(0.0, float(delay_seconds))
        self._block = shared_block if shared_block is not None else BlockState()
        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
            # 跟 HTML 路线一致：代理只走插件自己的配置项，不吃环境变量
            "trust_env": False,
        }
        if proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- 反爬 -----------------------------------------------------------------

    def blocked_remaining(self) -> float:
        """还在冷却期里就返回剩余秒数，否则 0。"""
        return self._block.remaining()

    def reset_block(self) -> None:
        """手动解除冷却（``/搬石 重置 冷却`` 用）。"""
        self._block.clear()

    async def _sleep(self) -> None:
        """请求之间歇一下。加抖动是因为固定间隔反而更像脚本。"""
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds * random.uniform(0.8, 1.3))

    async def _post_proto(self, url: str, cmd: int, payload: bytes, *, what: str) -> bytes | None:
        """POST 一个 protobuf 请求，返回响应字节。失败返回 None。

        反爬拦截会抛 :class:`StoneBlockedError`：那是「暂时不给我看」，重试只会
        把封禁加深，必须让整轮抓取立刻停下来。
        """
        left = self.blocked_remaining()
        if left > 0:
            raise StoneBlockedError("还在反爬冷却期（剩 %s 秒），本轮不再请求贴吧" % int(left))
        body, ctype = multipart_body(payload)
        headers = proto_headers()
        headers["Content-Type"] = ctype
        target = "%s?cmd=%d" % (url, cmd)
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = await self._client.post(target, content=body, headers=headers)
                if resp.status_code in _BLOCK_STATUS:
                    self._block.mark()
                    logger.warning(
                        "[搬石/proto] 触发反爬拦截（%s）：%s —— 本轮停止，%s 秒内不再请求贴吧",
                        resp.status_code,
                        what,
                        BLOCK_COOLDOWN_SECONDS,
                    )
                    raise StoneBlockedError("贴吧返回 HTTP %s，已停止本轮抓取" % resp.status_code)
                raw = resp.content or b""
                if resp.status_code >= 400:
                    raise httpx.HTTPError("HTTP %s" % resp.status_code)
                if not raw:
                    raise httpx.HTTPError("空响应")
                if not looks_like_proto(raw):
                    # 返回 HTML 说明被重定向到验证页了，按反爬处理
                    self._block.mark()
                    logger.warning("[搬石/proto] %s 返回的不是 protobuf（疑似验证页）", what)
                    raise StoneBlockedError("贴吧返回了 HTML 而不是 protobuf，已停止本轮抓取")
                return raw
            except StoneBlockedError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.8 * (attempt + 1))
        logger.warning("[搬石/proto] 请求失败 %s：%s", what, last)
        return None
    # -- 列表页 ---------------------------------------------------------------

    async def fetch_thread_list(
        self, *, forum: str, pages: int = 1, sort: int = 0, is_good: bool = False
    ) -> list[StoneThread]:
        """抓某吧的帖子列表，**带点赞数**。

        Args:
            forum: 吧名，带不带末尾的「吧」都行（跟 HTML 路线同口径：先用原名查，
                一条都没解析到才去掉末尾的「吧」再查一次）。
            pages: 翻几页，每页 30 条。
            sort: 0 = 回复时间，1 = 发布时间。**不要用 2（热门）** —— 实测返回 0 条。
            is_good: 只要精品帖。
        """
        name = normalize_forum(forum)
        if not name:
            return []
        out = await self._fetch_frs_pages(name, pages=pages, sort=sort, is_good=is_good)
        if not out and len(name) > 1 and name.endswith("吧"):
            trimmed = name[:-1]
            out = await self._fetch_frs_pages(trimmed, pages=pages, sort=sort, is_good=is_good)
            if out:
                logger.info(
                    "[搬石/proto] 「%s」没查到帖子，去掉末尾的「吧」用「%s」查到了", name, trimmed
                )
        # 解析出来的帖子把 forum 统一标成调用方给的原名（展示与去重记录都用它）
        for item in out:
            item.forum = name
        return out

    async def _fetch_frs_pages(
        self, keyword: str, *, pages: int, sort: int, is_good: bool
    ) -> list[StoneThread]:
        """按 kw=keyword 翻 pages 页列表。"""
        out: list[StoneThread] = []
        seen: set[str] = set()
        for page in range(1, max(1, int(pages)) + 1):
            payload = build_frs_request(keyword, page=page, rn=PB_PAGE_SIZE, sort=sort, is_good=is_good)
            raw = await self._post_proto(
                FRS_URL, FRS_CMD, payload, what="列表页 %s 第 %s 页" % (keyword, page)
            )
            await self._sleep()
            if raw is None:
                # 单页失败就跳过去继续翻，别把「没取到」当成「没有更多」
                continue
            items, err = parse_frs_response(raw, forum=keyword)
            if err:
                logger.debug("[搬石/proto] 列表页 %s 第 %s 页解析失败：%s", keyword, page, err)
                continue
            for item in items:
                thread = thread_from_proto(item)
                if not thread.tid or thread.tid in seen:
                    continue
                seen.add(thread.tid)
                out.append(thread)
            logger.debug(
                "[搬石/proto] %s 第 %s 页：解析 %s 条（累计 %s）", keyword, page, len(items), len(out)
            )
            if not items:
                break
        return out
    # -- 楼层页 ---------------------------------------------------------------

    async def fetch_thread_detail(
        self,
        thread: StoneThread,
        *,
        max_floors: int = 30,
        max_chars: int = 0,
        max_comments: int = 3,
    ) -> StoneDetail:
        """抓一个帖子的楼层（**能翻页**，这是 HTML 路线做不到的）。

        HTML 的 ``kz=`` 页面只有第一页，``pn=`` 对它完全无效；protobuf 的 ``pn``
        真能翻，所以 ``max_floors`` 超过 30 时会自动接着翻页，楼层号跨页累加。

        Args:
            max_floors: 最多要几层。0 表示不限制，此时只受翻页硬上限
                （PB_MAX_PAGES 页）约束 —— 不能真的无限翻。
            max_chars: 单层正文最多留几个字（0 表示不截断）。
            max_comments: 每层最多要几条楼中楼；**0 表示不要楼中楼**
                （跟 HTML 路线语义一致）。要楼中楼会额外带 ``with_floor=1``。
        """
        detail = StoneDetail(thread=thread)
        want = max(1, int(max_floors)) if max_floors else 0
        floors: list[StoneFloor] = []
        seen_pid: set[str] = set()
        page = 1
        # 页面本身有总页数，但那个字段实测不可靠（恒等于每页条数），
        # 所以用「这一页解析出几条」自己判断要不要继续翻。
        # 循环**必须**有个页数出口：want 为 0（不限制）时下面那些
        # len(floors) >= want 的判断全部失效，只剩「这一页没满」一条退路，
        # 而服务端忽略 pn、一直回满页时那条永远不会命中。
        while page <= PB_MAX_PAGES:
            payload = build_page_request(
                thread.tid,
                page=page,
                rn=PB_PAGE_SIZE,
                with_floor=bool(max_comments),
                floor_rn=max(0, int(max_comments)),
            )
            raw = await self._post_proto(
                PAGE_URL, PAGE_CMD, payload, what="帖子 %s 第 %s 页" % (thread.tid, page)
            )
            await self._sleep()
            if raw is None:
                break
            page_floors, info, err = parse_page_response(
                raw, tid=thread.tid, with_floor=bool(max_comments), floor_number_base=(page - 1) * PB_PAGE_SIZE
            )
            if err:
                logger.debug("[搬石/proto] 帖子 %s 第 %s 页解析失败：%s", thread.tid, page, err)
                break
            if page == 1 and info:
                # 详情页能补全列表页可能缺的标题/回复数/点赞数
                if not thread.title and info.get("title"):
                    thread.title = str(info["title"])
                detail.info = info
            added = 0
            for item in page_floors:
                floor = floor_from_proto(item)
                # 去重键用 **pid**，不能用 fn：fn 是 floor_number_base + idx + 1
                # 现编的，每页 base 都不一样，同一个楼层翻两页拿回来 fn 天然不同，
                # 按它去重等于没去重（服务端忽略 pn 时会把同一批帖子搬两遍）。
                # 拿不到 pid 时才退回 fn 兜底。
                key = str(item.get("pid") or "").strip() or ("fn:%s" % floor.fn)
                if key in seen_pid:
                    continue
                seen_pid.add(key)
                if max_chars and len(floor.text) > max_chars:
                    floor.text = floor.text[:max_chars].rstrip() + "……"
                floors.append(floor)
                added += 1
                if want and len(floors) >= want:
                    break
            if want and len(floors) >= want:
                break
            if added < PB_PAGE_SIZE:
                # 这一页没满，说明到底了
                break
            page += 1
        else:
            # 撞上翻页硬上限（while 是正常结束的，不是 break 出来的）。
            logger.debug(
                "[搬石/proto] 帖子 %s 翻到 %s 页硬上限就停了，已收 %s 层",
                thread.tid,
                PB_MAX_PAGES,
                len(floors),
            )
        detail.floors = floors
        if not floors:
            logger.debug("[搬石/proto] 帖子 %s 没解析出楼层（可能被删/结构变了）", thread.tid)
        return detail
