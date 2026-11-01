"""麦麦贴吧搬石 —— 贴吧抓取与解析。

只做搬运需要的事：读列表、读首页楼层、把正文里的插图取下来。不签到、不回帖、不读私信。

八件实测出来的事决定了这个模块的样子（2026-10 实测）：

1. **桌面版列表页已经废了**。tieba.baidu.com/f?kw=<吧名> 现在给的是「贴吧小程序」
   引导页，经典选择器一个都没有。服务端还在渲染的入口是**移动 m 站**：
   /mo/q/m?kw=<吧名>&pn=<偏移>，实测每页 30 条，标题在 div.ti_title 里。
2. **pn 是偏移量，步长必须等于 30**。pn=0 出第 1~30 条，pn=30 出第 31~60 条；
   步长写 50 会整段漏掉第 31~50 条。
3. **裸请求会被拦**。不带任何 cookie 打贴吧任意页面 → 403 +「百度安全验证」。
   它拦的是「一个 cookie 都没有」，不是「没登录」—— 先去 www.baidu.com 走一趟
   拿到访客 cookie 再回来就是 200。**所以本插件不需要 BDUSS 之类的任何票据。**
4. **帖子正文要把参数写成老式的 kz=**。/p/<tid> 免登录只给空壳（桌面版 11KB 的 SPA
   壳、移动版 7KB 的引导页），换成 /mo/q/m?kz=<tid> 服务端会把首页楼层整个渲染好。
5. **kz= 页面只有首页**。pn= 对它无效，实测 pn=1/2/99 返回同一批 20~30 层。
   想搬更多内容只能多挑几个帖子，而不是翻页。
6. **正文插图是懒加载占位，不是 img 标签**。服务端渲染出来的是：
       <div class="j_media_thumb_holder" data-url="http://tiebapic.baidu.com/forum/
            w%3D250%3Bq%3D100%3Bg%3D0/sign=xxx/<hash>.jpg?tbpicau=...&src=<原图直链>">
   data-url 本身带签名，**只能原样请求**，改宽（w%3D720）会让签名失效、返回一张
   238×238 的占位图（4262 字节）。带签名这个地址实测给的是 250 宽缩略图。
7. **原图要换域名**。data-url 的 src= 参数里就是原图直链
   （http://tiebapic.baidu.com/forum/pic/item/<hash>.jpg），但直接请求它同样只给
   占位图；把域名换成 **imgsrc.baidu.com** 再请求，实测拿到 1002×783 的原图。
   所以取图顺序是：imgsrc 直链 → 带签名缩略图。
8. **没有服务端热门排序**。sort / st / hot / order / tab 五组参数实测全被忽略，
   列表只按最后回复时间排。回复数印在列表页每条帖子上（btn_reply > span.btn_icon），
   与帖子页里的数字一致，所以「只看多少回复以上」可以在本地做。

被反爬拦住和「这个吧没帖子」是两回事：前者抛 StoneBlockedError，调用方必须停下、
保留已搬记录，绝不能当成「空了」。
"""

from __future__ import annotations

import asyncio
import html as _html
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import quote, unquote, urlsplit

import httpx

logger = logging.getLogger(__name__)

TIEBA_ORIGIN = "https://tieba.baidu.com"
BAIDU_HOME = "https://www.baidu.com/"
LIST_PATH = "/mo/q/m"

# m 站的 UA。用桌面 UA 打 m 站会被跳回「贴吧小程序」引导页。
MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)

# 列表页每页 30 条，pn 是偏移量不是页码，步长必须等于 30
LIST_PAGE_STEP = 30

# 触发反爬后多久不再请求（秒）。宁可这一轮不搬，也不要一直敲门。
BLOCK_COOLDOWN_SECONDS = 900
_BLOCK_MARKERS = ("百度安全验证", "请开启JavaScript", "security_verify")

# 反爬拦截的状态码。以前只认 403，结果 429（限流）/456/503 走通用重试 →
# 重试完返回 None → 上层报「本轮没有新帖可搬」，用户以为没人发帖，其实是被墙了，
# 下一轮还会立刻再撞一次。凡是「服务端在拦我」的状态码都要归到这里。
_BLOCK_STATUS = frozenset({403, 429, 456, 503})

# 图片域名。tiebapic 只给带签名的 250 宽缩略图，换成 imgsrc 才给原图（实测 1002×783）。
THUMB_IMAGE_HOST = "tiebapic.baidu.com"
ORIGIN_IMAGE_HOST = "imgsrc.baidu.com"

# 这些域名上的图是表情、头像、页面图标，不是正文插图
UI_IMAGE_HOSTS = (
    "tb1.bdstatic.com",
    "tb2.bdstatic.com",
    "tb3.bdstatic.com",
    "gss3.bdstatic.com",
    "b.bdstatic.com",
    "tieba-fe.cdn.bcebos.com",
    "tieba-ares.cdn.bcebos.com",
    "dlswbr.baidu.com",
    "passport.baidu.com",
    "hiphotos.baidu.com",
)


class StoneTiebaError(RuntimeError):
    """抓取失败，message 可直接展示给用户。"""


class StoneBlockedError(StoneTiebaError):
    """被贴吧反爬拦住了（403 +「百度安全验证」）。

    这是「暂时不给我看」，不是「没有内容」—— 调用方必须停下并保留已搬记录。
    """


# --- HTML 解析 ---------------------------------------------------------------
#
# 全是正则而不是 bs4：插件只带 httpx 一个依赖（_manifest.json 里写死的），
# 多一个依赖就多一步安装；贴吧这两处结构十几年没变过，正则够用，
# 也不会出现「bs4 没装 → 静默 0 结果」那种坑。

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{2,}")

# 列表项：<li class="tl_shadow ..." data-tid="11041271660"> … </li>
_LIST_ITEM_RE = re.compile(
    r'<li[^>]*class="[^"]*\btl_[^"]*"[^>]*data-tid="(?P<tid>\d+)"[^>]*>(?P<body>.*?)</li>',
    re.S,
)
_TITLE_RE = re.compile(r'<div[^>]*class="[^"]*\bti_title\b[^"]*"[^>]*>(?P<body>.*?)</div>', re.S)

# 帖子类型：40 = 视频帖（实测与列表项里的 media_flash 标记严格对应）
THREAD_TYPE_VIDEO = 40
_THREAD_TYPE_RE = re.compile(r'data-thread-type="(?P<t>\d+)"')
# 列表页内联的帖子 JSON 数组：{"id":<tid>,"tid":<tid>,"title":…
_LIST_JSON_HEAD_RE = re.compile(r'\{"id":(\d+),"tid":\1,"title":')
_SPAN_RE = re.compile(r"<span[^>]*>(?P<text>.*?)</span>", re.S)

# 回复数：<div class="ti_zan_reply ..."><div class="ti_func_btn btn_reply">
#             <span class="btn_icon">238</span></div></div>
_REPLY_BLOCK_RE = re.compile(r'class="[^"]*\bti_zan_reply\b[^"]*"[^>]*>(?P<block>.{0,600}?)</a>', re.S)
_REPLY_NUM_RE = re.compile(
    r'class="[^"]*\bbtn_reply\b[^"]*"[^>]*>.{0,200}?class="btn_icon"[^>]*>(?P<num>\d+)',
    re.S,
)

# 楼层：<li tid="153971846150" data-tid="153971846150" fn="1"
#         data-info="{&quot;un&quot;:&quot;&quot;,&quot;floor_num&quot;:1,...}"
#         class="list_item post_list_item ...">
_FLOOR_OPEN_RE = re.compile(r'<li(?P<attrs>[^>]*\btid="(?P<tid>\d+)"[^>]*)>', re.S)
_ATTR_RE = re.compile(r'(?P<key>[a-zA-Z_:-]+)="(?P<value>[^"]*)"')
_CONTENT_OPEN_RE = re.compile(r'<div[^>]*class="[^"]*\bcontent\b[^"]*"[^>]*>')
_DIV_TAG_RE = re.compile(r"<(?P<close>/?)div\b[^>]*>")
_FLOOR_TIME_RE = re.compile(r'class="list_item_time">(?P<time>.*?)</span>', re.S)

# 图片占位一律按 data-url 认，不再枚举 class（2026-10 实测）：
#   正文插图：<div class="j_media_thumb_holder img_placeholder" data-url='...'>
#   单图首楼：<div id="pb_less_imgs"><div class="pb_img_item" data-url='...'>
#   多图首楼：<div id="pb_imgs"><div id="pb_imgs_div">
#               <div class="pb_imgs_top" data-url='...'>
#               <div class="pb_imgs_bottom_left" data-url='...'>
#               <div class="pb_imgs_bottom_right" data-url='...'>
# 以前只认 j_media_thumb_holder|pb_img_item，多图首楼（首楼带 3 张图那种）整层丢图。
# 装图片的标签才带 data-url，容器（pb_imgs、pb_imgs_div）不带，所以按 data-url 认不漏也不误伤。
# 属性可能单引号也可能双引号，用反向引用成对匹配。
_IMG_PLACEHOLDER_RE = re.compile(
    r"""<(?P<tag>[a-zA-Z]+)\b[^>]*\bdata-url=(?P<q>["'])(?P<u>.*?)(?P=q)""",
    re.I | re.S,
)
_IMG_TAG_RE = re.compile(r"<img[^>]*>", re.I)
_IMG_SRC_ATTR_RE = re.compile(r"""(?:data-original|data-src|src)=["'](?P<u>[^"']+)["']""", re.I)
# 同一个 <img> 上这三个属性可能同时存在，取哪个有讲究（见 _best_image_src）
_IMG_SRC_ANY_RE = re.compile(r"""\b(data-original|data-src|src)=["\'](?P<u>[^"\']+)["\']""", re.I)
_IMG_SRC_PRIORITY = {"data-original": 0, "data-src": 1, "src": 2}


def _best_image_src(tag: str) -> str:
    """从 <img> 标签里挑出真正该用的那个地址（已反转义、已补全协议）。

    懒加载的 img 常常同时挂着 ``data-src``（真图）和 ``src``（loading.gif 之类的
    占位图）。以前只取第一个命中的属性，碰上 ``src`` 写在前面就取到占位图，
    再被 UI 图过滤掉，整张图就这么丢了。这里按 data-original → data-src → src
    的优先级挑，同级取先出现的那个。
    """
    best: tuple[int, int, str] | None = None
    for m in _IMG_SRC_ANY_RE.finditer(tag or ""):
        name = m.group(1).lower()
        url = _html.unescape(m.group("u"))
        if url.startswith("//"):
            url = "https:" + url
        rank = (_IMG_SRC_PRIORITY.get(name, 3), m.start())
        if best is None or rank < best[:2]:
            best = (rank[0], rank[1], url)
    return best[2] if best else ""

# 视频帖在 HTML 里留下三样东西，取哪一样是实测出来的（2026-10）：
#   1) <a class="video" data-vhsrc=".../tieba-smallvideo/<hash>.mp4"> —— 不带签名的
#      源文件，实测 **401**（172 字节错误页），只能当「这层有视频」的标记用。
#   2) 紧跟其后的 <span class="net_search_url" data-vhsrc=".../tieba-smallvideo-
#      transcode-cae|crf/<hash>_0[_cae3].mp4?...&tbau=<签名>&ptid=<tid>"> ——
#      **这个才是能下的**（实测 200 / 2191591 字节 / ftypisom）。把 tbau 去掉就 401，
#      所以只能原样搬，不能自己拼。它在 HTML 里被转义成 &amp;，取用前必须 unescape。
#   3) 封面在 <a class="video"><img src="..."> 里，缩略图规则和正文插图一样。
_VIDEO_TRANSCODE_RE = re.compile(
    r"""<span[^>]*class=["'][^"']*\bnet_search_url\b[^"']*["'][^>]*data-vhsrc=(?P<q>["'])(?P<u>.*?)(?P=q)""",
    re.I | re.S,
)
_VIDEO_ANCHOR_RE = re.compile(
    r"""<a[^>]*class=["'][^"']*\bvideo\b[^"']*["'][^>]*>(?P<body>.*?)</a>""",
    re.I | re.S,
)
_VIDEO_RAW_RE = re.compile(
    r"""data-vhsrc=(?P<q>["'])(?P<u>[^"']*\.mp4[^"']*)(?P=q)""",
    re.I | re.S,
)
# 只认这些域名，免得把页面里别的 mp4 当成帖子视频
VIDEO_HOSTS = ("tb-video.bdstatic.com", "tieba-video.bdstatic.com")

# 首楼多图：服务端只渲染 3 个格子（pb_imgs_top / pb_imgs_bottom_left /
# pb_imgs_bottom_right），真实张数写在同层的「9图」标签里，其余图**只在**内联
# JS 的 contentTotal 载荷里。它是 JS 单引号字面量：\/ 转义斜杠、\' 转义引号，
# 内部还是 HTML 转义的（&amp;src=）。不解析它的话，标着「9图」的首楼只会搬到
# 3 张，而且一声不响。
_CONTENT_TOTAL_KEY = "contentTotal"

# 移动页会往正文里塞导流提示，搬过去没意义，顺手删掉
_NAG_RE = re.compile(
    r"^.*(?:打开贴吧\s*App|看高清大图|下载贴吧\s*App|打开手百\s*App|点击展开).*$",
    re.M | re.I,
)


def _to_int(value: object, default: int = 0) -> int:
    """把 JSON 里的数字/数字字符串转成 int，转不了就用默认值。"""

    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # JSON 里的 agree_num 偶尔是 12.0 这种浮点，能取整就别丢
        return int(value)
    try:
        return int(str(value).strip() or default)
    except (TypeError, ValueError):
        return default

def _text_of(raw: str) -> str:
    """把一段 HTML 片段变成可读文本：去标签、还原实体、压空白。"""
    if not raw:
        return ""
    out = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    out = re.sub(r"</p>", "\n", out, flags=re.I)
    out = re.sub(r"</div>", "\n", out, flags=re.I)
    out = _TAG_RE.sub("", out)
    out = _html.unescape(out)
    out = _WS_RE.sub(" ", out)
    return _BLANK_RE.sub("\n", out).strip()


def _clean_text(text: str) -> str:
    """去掉正文里的导流提示，再压一遍空行。"""
    if not text:
        return ""
    return _BLANK_RE.sub("\n", _NAG_RE.sub("", text)).strip()


def _attrs_of(raw: str) -> dict[str, str]:
    return {m.group("key"): m.group("value") for m in _ATTR_RE.finditer(raw or "")}


def _data_info(raw: str) -> dict[str, Any]:
    """读楼层上的 data-info（HTML 转义过的 JSON）。读不出来就返回空 dict。"""
    if not raw:
        return {}
    try:
        obj = json.loads(_html.unescape(raw))
    except Exception:  # noqa: BLE001
        return {}
    return obj if isinstance(obj, dict) else {}


def _reply_count_of(body: str) -> int:
    """从列表项里读回复数。读不到返回 0（置顶帖就是这种情况）。"""
    block_m = _REPLY_BLOCK_RE.search(body or "")
    if not block_m:
        return 0
    num_m = _REPLY_NUM_RE.search(block_m.group("block"))
    if not num_m:
        return 0
    try:
        return int(num_m.group("num"))
    except (TypeError, ValueError):
        return 0


def _content_segment(body: str) -> str:
    """从楼层片段里取出正文 div 的内容。

    用配平扫描而不是非贪婪正则：正文里可能嵌 div（引用、图片、楼中楼），
    非贪婪会在一半就截断。
    """
    open_m = _CONTENT_OPEN_RE.search(body or "")
    if not open_m:
        return ""
    start = open_m.end()
    seg = body[start:]
    depth = 1
    for tag_m in _DIV_TAG_RE.finditer(body, start):
        depth += -1 if tag_m.group("close") else 1
        if depth == 0:
            seg = body[start : tag_m.start()]
            break
    return seg


def _direct_image_url(url: str) -> str:
    """从 data-url 里取出 src= 参数（原图直链）。取不到返回空串。

    进来的 url 可能是没反转义的（``&amp;src=``），这里先 unescape 再找，
    免得调用方忘了转义就静默取不到原图。重复 unescape 是无害的。
    """
    m = re.search(r"[?&]src=([^&]+)", _html.unescape(url or ""))
    if not m:
        return ""
    direct = unquote(m.group(1))
    return direct if direct.startswith("http") else ""


def to_origin_image(url: str) -> str:
    """把缩略图域名换成能拿原图的域名。"""
    if not url:
        return ""
    return url.replace(THUMB_IMAGE_HOST, ORIGIN_IMAGE_HOST)


def _is_ui_image(url: str) -> bool:
    low = (url or "").lower()
    if not low:
        return True
    if any(host in low for host in UI_IMAGE_HOSTS):
        return True
    return "/tb/editor/images/" in low


# 只从贴吧自己的图库域名上取图（含子域）。这里用后缀比对，不用 `"baidu.com" in url`：
# 子串比对会让 baidu.com.evil.com 这类域名蒙混过关。正文里的外链图一律不取 ——
# 那等于让远端页面决定插件去请求哪个地址，还能顺手指向 localhost、内网和云元数据。
IMAGE_HOST_SUFFIXES = ("baidu.com", "bdstatic.com", "bdimg.com", "bcebos.com")


def _host_of(url: str) -> str:
    """取 URL 的 host（小写、不含端口）；取不到返回空串。"""
    raw = (url or "").strip()
    if raw.startswith("//"):
        raw = "https:" + raw
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https"):
        return ""
    return (parsed.hostname or "").strip().lower()


def _host_ok(url: str, suffixes: tuple[str, ...]) -> bool:
    """URL 的域名是不是给定后缀之一（后缀本身，或它的子域）。"""
    host = _host_of(url)
    if not host:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _image_host_ok(url: str) -> bool:
    """URL 是不是贴吧图库域名上的。"""
    return _host_ok(url, IMAGE_HOST_SUFFIXES)


def _looks_like_image(url: str) -> bool:
    """data-url 里的值是不是真的用户图片。

    容器标签（pb_imgs、pb_imgs_div）本来就不带 data-url，这里再挡一道，
    免得以后贴吧给别的元素也加上 data-url 时被误当成图片。

    域名必须落在 IMAGE_HOST_SUFFIXES 里：贴吧正文图只有两个来源（imgsrc 原图直链、
    tiebapic 带签名缩略图），页面里别处出现的地址（包括吧友贴的外链图床）一概不取。
    """
    low = (url or "").strip().lower()
    if not low.startswith(("http://", "https://", "//")):
        return False
    if _is_ui_image(low):
        return False
    return _image_host_ok(low)


def _int_or_none(raw: Any) -> int | None:
    """把响应头里的数字文本转成 int；空值/非数字一律 None。"""
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _parse_content_range(raw: Any) -> int | None:
    """从 ``bytes 0-0/12345`` 里取出总长度 12345；``*`` 或畸形值返回 None。"""
    text = str(raw or "").strip()
    if "/" not in text:
        return None
    return _int_or_none(text.rsplit("/", 1)[-1])


def _js_literal(raw: str, key: str) -> str | None:
    """取 ``key`` 后面那个 JS 单引号字面量的内容（正确处理 \\' 转义）。

    找不到返回 None。这里不用正则：字面量里全是转义引号，正则容易写错。
    """
    i = (raw or "").find(key)
    if i < 0:
        return None
    start = raw.find("'", i)
    if start < 0:
        return None
    buf: list[str] = []
    k = start + 1
    while k < len(raw):
        ch = raw[k]
        if ch == "\\":
            buf.append(raw[k : k + 2])
            k += 2
            continue
        if ch == "'":
            break
        buf.append(ch)
        k += 1
    return "".join(buf)


def _js_unescape(text: str) -> str:
    """按 JS 字符串规则去掉反斜杠转义：\\/ -> / 、\\' -> ' 、\\" -> " 。"""
    out: list[str] = []
    k = 0
    while k < len(text):
        if text[k] == "\\" and k + 1 < len(text):
            out.append(text[k + 1])
            k += 2
            continue
        out.append(text[k])
        k += 1
    return "".join(out)


def content_total_images(raw: str) -> list[str]:
    """从页面内联 JS 的 contentTotal 载荷里取出首楼全部图片（带签名的原始串）。

    首楼多图时服务端只渲染 3 个格子，其余图只在这里。取不到就返回空列表。
    返回的是**按 JS 里的顺序**、已去重的 data-url（还是转义态，调用方照常
    unescape 即可，因为这里 unescape 过了）。
    """
    literal = _js_literal(raw or "", _CONTENT_TOTAL_KEY)
    if not literal:
        return []
    payload = _html.unescape(_js_unescape(literal))
    out: list[str] = []
    seen: set[str] = set()
    for m in _IMG_PLACEHOLDER_RE.finditer(payload):
        signed = _html.unescape(m.group("u"))
        if not _looks_like_image(signed) or signed in seen:
            continue
        seen.add(signed)
        out.append(signed)
    return out


def images_of(segment: str) -> tuple[list[str], list[str]]:
    """取一段正文里的图片。

    Returns:
        (原图直链列表, 带签名缩略图列表)。两者一一对应，原图下不动时用缩略图兜底。
    """
    origin: list[str] = []
    thumbs: list[str] = []
    for m in _IMG_PLACEHOLDER_RE.finditer(segment or ""):
        signed = _html.unescape(m.group("u"))
        if not _looks_like_image(signed):
            continue
        direct = _direct_image_url(signed)
        thumbs.append(signed)
        origin.append(to_origin_image(direct) if direct else signed)
    for m in _IMG_TAG_RE.finditer(segment or ""):
        u = _best_image_src(m.group(0))
        if not u:
            continue
        if _is_ui_image(u) or not _image_host_ok(u):
            continue
        origin.append(to_origin_image(u))
        thumbs.append(u)
    return origin, thumbs


@dataclass
class StoneComment:
    """楼层下的一条楼中楼（评论）。

    贴吧页面给每条评论的只有 pid 和作者名 —— 没有点赞数、没有时间、没有楼中楼层号
    （实测 55 个样本，含楼中楼的面板里 agree/zan/j_agree 命中数全是 0）。所以没法按
    「高赞」排序，只能靠「是不是楼主」和页面顺序来挑。
    """

    author: str = ""
    text: str = ""
    is_author: bool = False


@dataclass
class StoneVideo:
    """楼层里的一个视频。

    ``url`` 是带 tbau 签名的转码直链（唯一能下的那个），``cover`` 是封面原图。
    签名有过期时间，所以拿到就尽快下；下不动时调用方只发链接。
    """

    url: str = ""
    cover: str = ""
    cover_thumb: str = ""

    @property
    def filename(self) -> str:
        """给视频起个带 .mp4 的名字（适配器按扩展名判断媒体类型）。"""
        name = (self.url or "").split("?", 1)[0].rsplit("/", 1)[-1]
        if not name.lower().endswith(".mp4"):
            name = (name or "tieba") + ".mp4"
        return name


def videos_of(segment: str) -> list[StoneVideo]:
    """取一段正文里的视频。

    转码直链在 ``span.net_search_url[data-vhsrc]`` 里，封面在 ``a.video > img`` 里。
    两者按**字节位置就近**配对（见下）；只有封面没有直链的也返回（调用方至少能
    提示「这层有视频」）。
    """
    out: list[StoneVideo] = []
    # spots 与 out 一一对应：每个视频在原文里的字节位置，下面配封面要用
    spots: list[int] = []
    for m in _VIDEO_TRANSCODE_RE.finditer(segment or ""):
        url = _html.unescape(m.group("u")).strip()
        if url.startswith("//"):
            url = "https:" + url
        if not url.lower().startswith("http"):
            continue
        if not _host_ok(url, VIDEO_HOSTS):
            continue
        out.append(StoneVideo(url=url))
        spots.append(m.start())
    # 封面按「位置就近」配对，不能用 idx：out 是按转码地址的顺序攒的，
    # 而 a.video 块是另一个正则数出来的，两者数量本来就未必相等 —— 只要有一个
    # 块因为域名不在白名单/没取到图被丢掉，按 idx 配对后面所有封面就整体错一位。
    # 所以按字节偏移找「紧邻这个转码地址之前、且还没被用过」的那个块。
    anchors: list[tuple[int, int, tuple[str, str] | None]] = []
    for m in _VIDEO_ANCHOR_RE.finditer(segment or ""):
        picked: tuple[str, str] | None = None
        for im in _IMG_TAG_RE.finditer(m.group("body")):
            u = _best_image_src(im.group(0))
            if not u:
                continue
            if _is_ui_image(u):
                continue
            picked = (to_origin_image(u), u)
            break  # 一个视频只要一张封面，后面的（播放图标等）不要
        anchors.append((m.start(), m.end(), picked))
    used: set[int] = set()
    for idx, video in enumerate(out):
        spot = spots[idx]
        best = -1
        for pos, (a_start, _a_end, picked) in enumerate(anchors):
            if pos in used or picked is None:
                continue
            if a_start > spot:
                break
            best = pos
        if best >= 0:
            used.add(best)
            video.cover, video.cover_thumb = anchors[best][2] or ("", "")
    return out


@dataclass
class StoneThread:
    """列表页里的一个帖子。"""

    tid: str
    title: str
    forum: str = ""
    is_top: bool = False
    replies: int = 0
    # 列表页 li 上的 data-thread-type：40 = 视频帖。跨吧实测与 li 内的
    # div.media_flash 标记 100% 一致（视频吧/搬石吧/孙笑川吧各 30 条零分歧），
    # 所以不用为了筛视频去抓详情页。
    thread_type: int = 0
    # 列表页内联 JSON 里的点赞数与浏览量（详情页没有这两个数）。
    # 页面结构变了就都是 0，筛选端要把它当成「不知道」而不是「零赞」。
    agree: int = 0
    views: int = 0
    # 精品帖标记与发帖时间戳。HTML 列表页也能看出精品（ti_title 旁边的小图标），
    # protobuf 路线直接有布尔字段，两条路都填得上。
    is_good: bool = False
    create_time: int = 0

    @property
    def has_video(self) -> bool:
        """列表页就能看出这帖有没有视频。"""
        return int(self.thread_type or 0) == THREAD_TYPE_VIDEO

    @property
    def url(self) -> str:
        return TIEBA_ORIGIN + "/p/" + self.tid

    @property
    def mobile_url(self) -> str:
        """真正能拿到正文的那个地址（老式 kz= 参数）。"""
        return TIEBA_ORIGIN + LIST_PATH + "?kz=" + self.tid


@dataclass
class StoneFloor:
    """帖子里的一个楼层。"""

    fn: int = 0
    author: str = ""
    text: str = ""
    time: str = ""
    images: list[str] = field(default_factory=list)
    thumbs: list[str] = field(default_factory=list)
    videos: list[StoneVideo] = field(default_factory=list)
    # 楼中楼：页面只渲染前几条，comment_total 是真实总数（data-list-count）。
    comments: list[StoneComment] = field(default_factory=list)
    comment_total: int = 0
    # 这一层自己的点赞数。HTML 页面给不了（实测 55 个样本里 agree/zan/j_agree
    # 命中数全是 0），只有 protobuf 路线有；HTML 路线留 0。
    agree: int = 0


@dataclass
class StoneDetail:
    """一个帖子的首页内容。"""

    thread: StoneThread
    floors: list[StoneFloor] = field(default_factory=list)
    blocked: bool = False
    # 页面原文：「贴吧原页面」长图模式下直接把这份 HTML 交给渲染器
    raw: str = ""
    # protobuf 路线额外拿到的帖子级信息（标题/回复数/点赞数/总页数…）。
    # HTML 路线留空 —— 那边这些数在 StoneThread 上已经有了。
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def images(self) -> list[str]:
        out: list[str] = []
        for f in self.floors:
            out.extend(f.images)
        return out

    @property
    def videos(self) -> list[StoneVideo]:
        out: list[StoneVideo] = []
        for f in self.floors:
            out.extend(f.videos)
        return out


# --- 纯函数：解析（拆出来是为了能脱离网络单测） --------------------------------


def normalize_forum(name: str) -> str:
    """吧名去掉首尾空白和斜杠（末尾的「吧」字不在这里处理，见 fetch_thread_list）。"""
    return str(name or "").strip().strip("/／").strip()


# 楼中楼面板：<div class="fr_list 2 j_floor_panel" data-list-count="27">…</div>。
# 它是 div.content 的**兄弟**节点，所以从整段楼层片段里单独抓不会跟正文打架。
# 开标签要单独抓：面板体里嵌着 fmain/floor_footer_item 好几个 div，用非贪婪
# 正则配 </div> 会在第一个内层 div 就截断，必须按 div 配平扫。
_LZL_OPEN_RE = re.compile(r'<div[^>]*class="[^"]*\bj_floor_panel\b[^"]*"[^>]*>', re.S)
_LZL_DIV_OPEN_RE = re.compile(r"<div\b", re.I)
_LZL_DIV_CLOSE_RE = re.compile(r"</div>", re.I)
_LZL_COUNT_RE = re.compile(r'data-list-count="(?P<num>\d+)"')
_LZL_ITEM_RE = re.compile(
    r'<li[^>]*\bpid="(?P<pid>\d+)"[^>]*class="[^"]*\bj_list_item_floor\b[^"]*"[^>]*>(?P<body>.*?)</li>',
    re.S,
)
_LZL_AUTHOR_RE = re.compile(r'class="[^"]*\buser_name\b[^"]*"[^>]*>(?P<name>.*?)</a>', re.S)
_LZL_TEXT_RE = re.compile(r'<span[^>]*class="[^"]*\bfloor_content\b[^"]*"[^>]*>(?P<text>.*?)</span>', re.S)


def _lzl_panel_of(body: str) -> tuple[str, int]:
    """从一段楼层片段里取出楼中楼面板的 HTML 与真实评论总数。

    Returns:
        (面板 HTML, data-list-count)。没有面板就返回 ("", 0)。
    """

    match = _LZL_OPEN_RE.search(body or "")
    if match is None:
        return "", 0
    count_m = _LZL_COUNT_RE.search(body[match.start() : match.end()])
    try:
        total = int(count_m.group("num")) if count_m else 0
    except (TypeError, ValueError):
        total = 0
    # 从开标签之后按 <div>/</div> 配平，取到面板闭合为止（面板体里还有嵌套 div）
    panel = body[match.end() : _lzl_close_index(body, match.end())]
    return panel, total


def _lzl_close_index(body: str, start: int) -> int:
    """从 start 起按 div 配平，返回面板闭合标签的起始下标；配不平就返回 len(body)。"""

    depth = 1
    index = start
    length = len(body)
    while index < length:
        open_m = _LZL_DIV_OPEN_RE.search(body, index)
        close_m = _LZL_DIV_CLOSE_RE.search(body, index)
        if close_m is None:
            return length
        if open_m is not None and open_m.start() < close_m.start():
            depth += 1
            index = open_m.end()
            continue
        depth -= 1
        if depth == 0:
            return close_m.start()
        index = close_m.end()
    return length


def parse_comments(panel: str, *, author: str = "", limit: int = 0) -> list[StoneComment]:
    """解析一个楼中楼面板。

    页面只渲染前几条评论（其余被折叠成「打开APP查看N条评论」），所以返回的是子集。
    贴吧没给评论点赞数，无法按赞排序，调用方只能按「是不是楼主」和页面顺序挑。

    Args:
        panel: _lzl_panel_of 取出来的面板 HTML。
        author: 该楼层的作者，用来标记哪些评论是楼主自己回的。
        limit: 最多返回几条（0 表示不限制）。
    """

    out: list[StoneComment] = []
    owner = str(author or "").strip()
    for item in _LZL_ITEM_RE.finditer(panel or ""):
        seg = item.group("body")
        text_m = _LZL_TEXT_RE.search(seg)
        if text_m is None:
            continue
        text = _clean_text(_text_of(text_m.group("text")))
        if not text:
            continue
        name_m = _LZL_AUTHOR_RE.search(seg)
        # 昵称节点里可能嵌 nickemoji 的 <img>，末尾还带一个冒号，都要清掉
        name = _clean_text(_text_of(name_m.group("name"))) if name_m else ""
        name = name.rstrip(":").strip()
        out.append(
            StoneComment(
                author=name,
                text=text,
                is_author=bool(owner) and name == owner,
            )
        )
        if limit and len(out) >= limit:
            break
    return out


def pick_comments(comments: Sequence[StoneComment], *, limit: int = 3) -> list[StoneComment]:
    """从楼中楼里挑要展示的几条：楼主自己的回复优先，其余按页面顺序。

    之所以是「楼主优先」而不是「高赞优先」：贴吧页面根本没给评论点赞数（见 StoneComment）。
    楼主回复通常才是补充说明/纠错，信息量最大，放前面最有用。
    """

    if limit <= 0:
        return list(comments)
    mine = [c for c in comments if c.is_author]
    others = [c for c in comments if not c.is_author]
    return (mine + others)[:limit]


def _scan_json_object(raw: str, start: int) -> tuple[str, int]:
    """从 start 处的 `{` 起配平到对应 `}`，返回 (对象文本, 结束下标)。字符串里的花括号不算数。"""

    depth = 0
    in_str = False
    escaped = False
    index = start
    while index < len(raw):
        ch = raw[index]
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : index + 1], index + 1
        index += 1
    return "", -1


def _list_page_stats(raw: str) -> dict[str, tuple[int, int]]:
    """从列表页内联的帖子 JSON 里取 {tid: (点赞数, 浏览量)}。

    详情页没有帖子级点赞数，只有列表页这段 JSON 有，所以必须在这儿取 ——
    好在列表页本来就要抓，等于零额外请求。取不到就返回空表，调用方按「不知道」处理。
    """

    out: dict[str, tuple[int, int]] = {}
    match = _LIST_JSON_HEAD_RE.search(raw or "")
    if match is None:
        return out
    index = match.start()
    length = len(raw)
    while index < length and raw[index] == "{":
        body, end = _scan_json_object(raw, index)
        if not body:
            break
        try:
            item = json.loads(body)
        except (ValueError, TypeError):
            # 页面结构变了就整段放弃，别拿半截数据去筛帖
            break
        tid = str(item.get("tid") or "")
        if tid:
            agree = item.get("agree")
            if isinstance(agree, dict):
                likes = _to_int(agree.get("agree_num"))
            else:
                likes = _to_int(agree)
            out[tid] = (likes, _to_int(item.get("view_num")))
        index = end
        while index < length and raw[index] in ", \t\r\n":
            index += 1
    return out


def parse_thread_list(raw: str, *, forum: str = "") -> list[StoneThread]:
    """解析 m 站列表页。

    结构（实测）::

        <li class="tl_shadow tl_shadow_new" data-tid="11041271660">
          <div class="ti_infos clearfix">…作者 / 时间…</div>
          <a href="/p/11041271660?lp=5028&mo_device=1" class="j_common ti_item">
            <div class="ti_title"><span>真正的标题</span></div>
            <div class="ti_zan_reply clearfix">
              <div class="ti_func_btn btn_reply"><span class="btn_icon">238</span></div>
            </div>
          </a>
        </li>

    标题取 div.ti_title 里**最后一个** span —— 第一个是「置顶 / 精」图标。
    """
    out: list[StoneThread] = []
    seen: set[str] = set()
    stats = _list_page_stats(raw or "")
    for m in _LIST_ITEM_RE.finditer(raw or ""):
        tid = m.group("tid")
        if tid in seen:
            continue
        seen.add(tid)
        body = m.group("body")
        title = ""
        title_m = _TITLE_RE.search(body)
        if title_m:
            spans = _SPAN_RE.findall(title_m.group("body"))
            title = _text_of(spans[-1] if spans else title_m.group("body"))
        if not title:
            continue
        is_top = "tl_top" in m.group(0)[: max(0, m.group(0).find(">"))]
        tt = _THREAD_TYPE_RE.search(m.group(0)[: max(0, m.group(0).find(">"))])
        likes, views = stats.get(tid, (0, 0))
        out.append(
            StoneThread(
                tid=tid,
                title=title,
                forum=forum,
                is_top=is_top,
                replies=_reply_count_of(body),
                thread_type=int(tt.group("t")) if tt else 0,
                agree=likes,
                views=views,
            )
        )
    return out


def parse_floors(
    raw: str, *, max_floors: int = 30, max_chars: int = 0, max_comments: int = 0
) -> list[StoneFloor]:
    """解析 kz= 帖子页的首页楼层。

    楼层以 li 标签上的 tid 数字开头，一直到一个同形状的 li 标签为止。
    正文取 div.content 的配平片段；插图取里面的 j_media_thumb_holder 占位。

    Args:
        max_floors: 最多返回几层（0 表示不限制）。
        max_chars: 单层正文最多留几个字（0 表示不截断）。
        max_comments: 每层最多解析几条楼中楼；**0 表示不解析**（页面本来就只给前几条，
            全解析没意义，所以这里的 0 跟 max_floors 的 0 语义不同）。
    """
    marks = list(_FLOOR_OPEN_RE.finditer(raw or ""))
    out: list[StoneFloor] = []
    for idx, m in enumerate(marks):
        end = marks[idx + 1].start() if idx + 1 < len(marks) else len(raw)
        body = raw[m.end() : end]
        cm = _CONTENT_OPEN_RE.search(body)
        if not cm:
            continue
        attrs = _attrs_of(m.group("attrs"))
        info = _data_info(attrs.get("data-info", ""))
        segment = _content_segment(body)
        text = _clean_text(_text_of(segment))
        if max_chars and len(text) > max_chars:
            text = text[:max_chars].rstrip() + "……"
        try:
            fn = int(attrs.get("fn") or info.get("floor_num") or 0)
        except (TypeError, ValueError):
            fn = 0
        time_text = ""
        time_m = _FLOOR_TIME_RE.search(body)
        if time_m:
            time_text = _text_of(time_m.group("time"))
        origin, thumbs = images_of(segment)
        # 首楼多图时 DOM 只给 3 个格子，其余图在 contentTotal 载荷里。
        # 只对第 1 层补：那个载荷装的就是楼主首楼的正文。
        if idx == 0:
            have = set(origin) | set(thumbs)
            extra: list[str] = []
            for signed in content_total_images(raw):
                if signed in have:
                    continue
                have.add(signed)
                extra.append(signed)
            for signed in extra:
                direct = _direct_image_url(signed)
                # 追加到末尾（JS 里本来就是正文顺序），缩略图用带签名那份兜底
                origin.append(to_origin_image(direct) if direct else signed)
                thumbs.append(signed)
        videos = videos_of(segment)
        # 视频封面会同时被 images_of（它是 <img>）和 videos_of 收到，不收掉的话
        # 同一帧会发两遍：一遍当正文图、一遍当视频封面，还白占一个图片名额。
        if videos:
            cover_keys = set()
            for video in videos:
                for key in (video.cover, video.cover_thumb):
                    if key:
                        cover_keys.add(key)
            if cover_keys:
                kept = [
                    (o, t) for o, t in zip(origin, thumbs) if o not in cover_keys and t not in cover_keys
                ]
                origin = [o for o, _t in kept]
                thumbs = [t for _o, t in kept]
        author = str(info.get("name_show") or info.get("un") or "").strip()
        # 楼中楼：面板是 content 的兄弟节点，单独从整段楼层片段里取
        panel, comment_total = _lzl_panel_of(body)
        out.append(
            StoneFloor(
                fn=fn or (idx + 1),
                author=author,
                text=text,
                time=time_text,
                images=origin,
                thumbs=thumbs,
                videos=videos,
                comments=parse_comments(panel, author=author, limit=max_comments) if max_comments else [],
                comment_total=comment_total,
            )
        )
        if max_floors and len(out) >= max_floors:
            break
    return out


# --- 网络客户端 ---------------------------------------------------------------


class StoneTiebaClient:
    """带访客 cookie 的贴吧只读客户端。

    生命周期跟插件绑在一起（on_unload 里 aclose()）。内部自己维护 cookie：
    只有访客 cookie，**不需要登录**。请求之间 sleep delay_seconds（带抖动），
    因为贴吧对高频访问很敏感。
    """

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        proxy: str = "",
        delay_seconds: float = 3.0,
        retries: int = 2,
        user_agent: str = "",
        shared_block: Any = None,
    ) -> None:
        # 反爬冷却可以跟别的客户端共用一份。分开记的话，一个接口被拦了，
        # 另一个还当自己没事继续敲，只会把封禁拖得更久。
        self._shared_block = shared_block
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.delay_seconds = max(0.0, float(delay_seconds))
        # 留空就用内置的手机 UA：换桌面 UA 会被贴吧跳回「贴吧小程序」引导页
        self.user_agent = str(user_agent or "").strip() or MOBILE_UA
        self._blocked_at = 0.0
        kwargs: dict[str, Any] = {
            "timeout": self.timeout,
            "follow_redirects": True,
            # 别让宿主环境里的 HTTP_PROXY / NO_PROXY 干扰，代理只走插件自己的配置项
            "trust_env": False,
            "headers": {
                "User-Agent": self.user_agent,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
            },
        }
        if proxy:
            kwargs["proxy"] = proxy
        self._client = httpx.AsyncClient(**kwargs)
        self._seeded = False
        self._seed_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- 底层 ---------------------------------------------------------------

    async def _sleep(self) -> None:
        """请求之间歇一下。加抖动是因为固定间隔反而更像脚本。"""
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds * random.uniform(0.8, 1.3))

    def blocked_remaining(self) -> float:
        """还在冷却期里就返回剩余秒数，否则 0。"""
        if self._shared_block is not None:
            return float(self._shared_block.remaining())
        if not self._blocked_at:
            return 0.0
        left = BLOCK_COOLDOWN_SECONDS - (time.monotonic() - self._blocked_at)
        return left if left > 0 else 0.0

    def _mark_blocked(self) -> None:
        if self._shared_block is not None:
            self._shared_block.mark()
            return
        self._blocked_at = time.monotonic()

    def reset_block(self) -> None:
        """手动解除冷却（``/搬石 重置 冷却`` 用）。

        只清冷却、不动搬运记录：记录是「搬过什么」，冷却是「被贴吧拦了多久」，
        两者互不相干，混在一起清会让用户为了解封而丢掉去重记录。
        """
        if self._shared_block is not None:
            self._shared_block.clear()
        self._blocked_at = 0.0

    async def _seed_guest_cookie(self) -> None:
        """先访问一次百度首页，把访客 cookie 拿回来。

        没有这一步，贴吧任意页面都是 403 +「百度安全验证」——
        实测拦的是「一个 cookie 都没有」，不是「没登录」。
        """
        if self._seeded:
            return
        async with self._seed_lock:
            if self._seeded:
                return
            try:
                await self._client.get(
                    BAIDU_HOME, headers={"User-Agent": self.user_agent, "Accept": "text/html,*/*"}
                )
            except Exception as exc:  # noqa: BLE001
                # 播种失败也放行：有些网络环境下百度首页不可达但贴吧本身可达
                logger.debug("[搬石/贴吧] 访客 cookie 播种失败（继续尝试）：%s", exc)
            self._seeded = True

    def _guard_blocked(self) -> None:
        """冷却期内一律不许再发请求，不管打的是页面还是图片/视频域名。

        反爬拦的是「这台机器」，不是某个接口 —— 冷却期间继续去敲图片域名同样会
        加深封禁。所以下载类方法入口都要先过这一道，和 ``_get`` 口径一致。
        """
        left = self.blocked_remaining()
        if left > 0:
            raise StoneBlockedError("还在反爬冷却期（剩 %s 秒），本轮不再请求贴吧" % int(left))

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> str | None:
        """取一页 HTML。失败返回 None（调用方决定是重试还是放弃）。

        唯独反爬拦截会抛 StoneBlockedError —— 那是「暂时不给我看」，
        重试只会把封禁加深，必须让整轮抓取立刻停下来。
        """
        self._guard_blocked()
        await self._seed_guest_cookie()
        merged = dict(headers or {})
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = await self._client.get(url, params=params, headers=merged)
                # 验证标记不一定在前 2000 字里，所以整段找；页面本身只有几十 KB，
                # 全文扫一遍的代价远小于「被拦了却当成没帖」的误判。
                if resp.status_code in _BLOCK_STATUS or any(
                    marker in (resp.text or "") for marker in _BLOCK_MARKERS
                ):
                    self._mark_blocked()
                    logger.warning(
                        "[搬石/贴吧] 触发反爬拦截（%s）：%s —— 本轮停止，%s 秒内不再请求贴吧",
                        resp.status_code,
                        url,
                        BLOCK_COOLDOWN_SECONDS,
                    )
                    raise StoneBlockedError("贴吧返回「百度安全验证」，已停止本轮抓取")
                if resp.status_code >= 400:
                    raise httpx.HTTPError("HTTP %s" % resp.status_code)
                text = resp.text
                if not text.strip():
                    raise httpx.HTTPError("空响应")
                return text
            except StoneBlockedError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < self.retries:
                    await asyncio.sleep(0.8 * (attempt + 1))
        logger.warning("[搬石/贴吧] 请求失败 %s：%s", url, last)
        return None

    # -- 列表 ---------------------------------------------------------------

    async def fetch_thread_list(self, forum: str, *, pages: int = 1) -> list[StoneThread]:
        """抓某吧的帖子列表（标题 + 回复数）。

        Args:
            forum: 吧名，带不带末尾的「吧」字都行。kw 参数要的是吧名本身：
                实测 kw=搬石吧 返回 0 条、kw=搬石 正常，所以原名一条都没解析到
                时会去掉末尾的「吧」再查一次（原名有结果就不走这一步，像「网吧」
                这种真名带「吧」的不会被误伤）。
            pages: 翻几页，每页 30 条，按 LIST_PAGE_STEP 递增偏移。
        """
        name = normalize_forum(forum)
        if not name:
            return []
        out = await self._fetch_pages(name, name, pages)
        if not out and len(name) > 1 and name.endswith("吧"):
            trimmed = name[:-1]
            out = await self._fetch_pages(name, trimmed, pages)
            if out:
                logger.info("[搬石/贴吧] 「%s」没查到帖子，去掉末尾的「吧」用「%s」查到了", name, trimmed)
        return out

    async def _fetch_pages(self, forum: str, keyword: str, pages: int) -> list[StoneThread]:
        """按 kw=keyword 翻 pages 页，解析出的帖子仍标成 forum（展示与记录用原吧名）。"""
        out: list[StoneThread] = []
        seen: set[str] = set()
        for page in range(max(1, int(pages))):
            offset = page * LIST_PAGE_STEP
            raw = await self._get(
                TIEBA_ORIGIN + LIST_PATH,
                params={"kw": keyword, "pn": offset},
                headers={"User-Agent": self.user_agent},
            )
            await self._sleep()
            if raw is None:
                # 单页失败就跳过去继续翻，别把「没取到」当成「没有更多」
                continue
            page_items = parse_thread_list(raw, forum=forum)
            for item in page_items:
                if item.tid in seen:
                    continue
                seen.add(item.tid)
                out.append(item)
            logger.debug(
                "[搬石/贴吧] %s 第 %s 页（偏移 %s）：解析 %s 条",
                keyword,
                page + 1,
                offset,
                len(page_items),
            )
            if not page_items:
                break
        return out

    # -- 正文 ---------------------------------------------------------------

    async def fetch_thread_detail(
        self,
        thread: StoneThread,
        *,
        max_floors: int = 30,
        max_chars: int = 0,
        max_comments: int = 3,
    ) -> StoneDetail:
        """抓一个帖子的首页楼层（免登录）。"""
        referer = (
            TIEBA_ORIGIN + LIST_PATH + "?kw=" + quote(thread.forum) if thread.forum else ""
        )
        headers = {"User-Agent": self.user_agent}
        if referer:
            headers["Referer"] = referer
        raw = await self._get(
            TIEBA_ORIGIN + LIST_PATH,
            # 老式 kz=（帖子 id）参数才会给服务端渲染的正文
            params={"kz": thread.tid, "pn": 1},
            headers=headers,
        )
        await self._sleep()
        detail = StoneDetail(thread=thread)
        if raw is None:
            return detail
        detail.raw = raw
        detail.floors = parse_floors(
            raw, max_floors=max_floors, max_chars=max_chars, max_comments=max_comments
        )
        if not detail.floors:
            logger.debug("[搬石/贴吧] 帖子 %s 没解析出楼层（可能被删/结构变了）", thread.tid)
        return detail

    # -- 图片 ---------------------------------------------------------------

    async def download_image(
        self,
        url: str,
        *,
        thumb: str = "",
        referer: str = "",
        max_bytes: int = 12 * 1024 * 1024,
    ) -> bytes | None:
        """下载一张正文图。

        先试原图域名（imgsrc），失败再退回 data-url 里带签名的缩略图。
        返回图片字节，两个都拿不到返回 None；冷却期内会抛 StoneBlockedError。
        """
        if not url and not thumb:
            return None
        self._guard_blocked()
        candidates: list[str] = []
        if url:
            candidates.append(url)
        if thumb and thumb not in candidates:
            candidates.append(thumb)
        await self._seed_guest_cookie()
        headers = {"User-Agent": self.user_agent, "Referer": referer or TIEBA_ORIGIN + "/"}
        # 正文图动辄几十上百张，一并放开请求等于自己往反爬枪口上撞，所以要歇；
        # 但只歇一次、放在试原图和试缩略图之前：退到缩略图是同一次「取这张图」
        # 的第二步，再歇一遍等于每张图花两个 delay，三十张图的帖子白等一分半。
        await self._sleep()
        for cand in candidates:
            # 候选地址是从页面里解析出来的，落盘前再验一道域名：真正的请求出口只认
            # 贴吧图库，免得页面内容把插件指到内网地址或者别的图床上去。
            if not _image_host_ok(cand):
                logger.debug("[搬石/贴吧] 图片域名不在白名单，跳过：%s", cand)
                continue
            # 必须用流式：以前写成 await self._client.get(...)，httpx 返回前
            # 会把整个响应体读进内存，下面那句 len(resp.content) > max_bytes 等于
            # 事后诸葛 —— 图已经全下完了，max_bytes 一点保护作用都没有。一张
            # 几十 MB 的巨图照样能把这一轮的内存和耗时吃光（和 _range_video_size
            # 是同一类坑）。这里一边收一边数字节，超了立刻跳出、连接关掉。
            try:
                async with self._client.stream("GET", cand, headers=headers) as resp:
                    if resp.status_code != 200:
                        continue
                    ctype = str(resp.headers.get("content-type") or "")
                    if not ctype.startswith("image"):
                        continue
                    # 先看声明长度：能提前拒就提前拒，连第一块都不用收
                    declared = _int_or_none(resp.headers.get("content-length"))
                    if declared is not None and declared > max_bytes:
                        logger.warning(
                            "[搬石/贴吧] 图片声明 %s 字节超过上限 %s，跳过：%s",
                            declared,
                            max_bytes,
                            cand,
                        )
                        continue
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf.extend(chunk)
                        if len(buf) > max_bytes:
                            break
            except Exception as exc:  # noqa: BLE001
                logger.debug("[搬石/贴吧] 图片请求失败 %s：%s", cand, exc)
                continue
            if not buf:
                continue
            if len(buf) > max_bytes:
                # 服务端没给 Content-Length（分块传输），只能收到超了才知道
                logger.warning("[搬石/贴吧] 图片超过 %s 字节，跳过：%s", max_bytes, cand)
                continue
            return bytes(buf)
        logger.debug("[搬石/贴吧] 两个地址都没取到图：%s", url or thumb)
        return None

    async def fetch_asset(
        self,
        url: str,
        *,
        referer: str = "",
        max_bytes: int = 2 * 1024 * 1024,
    ) -> bytes | None:
        """取一个页面外链资源（样式表、装饰图），取不到返回 None。

        和 :meth:`download_image` 有三处不同，都是为「一页几十个资源」准备的：

        1. **不 sleep**。那些是 CDN 上的静态资源，不是贴吧的接口，一页要几十个，
           每个都歇三秒就是几分钟；并发由调用方用信号量控住。
        2. **不校验 content-type**。样式表是 text/css，不是图片。
        3. **不记反爬冷却**。被拦了就是这一张没有，不该把整轮抓取停下来。

        域名仍然只认贴吧图库那几个后缀：地址来自远端页面，不能让它把插件指到内网。
        """
        if not _image_host_ok(url):
            logger.debug("[搬石/贴吧] 资源域名不在白名单，跳过：%s", url)
            return None
        headers = {
            "User-Agent": self.user_agent,
            "Referer": referer or TIEBA_ORIGIN + "/",
        }
        try:
            # 流式收，超上限立刻停：这些地址来自远端页面，大小不能信它
            async with self._client.stream("GET", url, headers=headers) as resp:
                if resp.status_code != 200:
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes(65536):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        logger.debug("[搬石/贴吧] 资源超过 %d 字节，跳过：%s", max_bytes, url)
                        return None
                return bytes(buf) if buf else None
        except Exception as exc:  # noqa: BLE001
            logger.debug("[搬石/贴吧] 资源下载失败 %s：%s", url, exc)
            return None

    async def probe_video_size(self, video: StoneVideo, *, referer: str = "") -> int | None:
        """问一下视频有多大，不下载视频本体。

        发送走 ``videourl`` 段之后，视频本体由协议端去取、插件不必下载，所以
        这里存在的唯一理由是「怕一条帖子太大、搬一轮拖太久」——提前问出大小，
        超限的整条跳过。

        只请求 ``StoneVideo.url`` 那个带 tbau 签名的转码地址：去掉签名或换成不带
        签名的源文件都是 401（见本模块顶部说明）。

        两条路依次试，都不成就算「问不到」：

        1. ``HEAD``，读 ``Content-Length``。最省事，但静态资源 CDN 未必回长度。
        2. ``GET`` + ``Range: bytes=0-0``，读 ``Content-Range`` 里的总长。
           这是 HTTP 标准写法，支持断点续传的 CDN 一定会给 ``Content-Range``；
           服务器要是不认这个头、直接把整个文件送回来，就只能读 ``Content-Length``
           判总长 —— 所以这一路必须用**流式**请求，见 :meth:`_range_video_size`。

        返回字节数；两条路都失败时返回 None ——调用方对 None 采取「照发不误」，
        宁可偶尔搬久一点，也不要因为问不到大小就默默把视频全丢掉。
        """
        if not video.url:
            return None
        self._guard_blocked()
        await self._seed_guest_cookie()
        # 探测本身也是往贴吧发请求，照样按 delay_seconds 歇一下 —— 一个视频帖
        # 常有两三个视频，不歇就是连着好几发，正是反爬最敏感的节奏。
        await self._sleep()
        headers = {"User-Agent": self.user_agent, "Referer": referer or TIEBA_ORIGIN + "/"}
        return await self._ask_video_size(video.url, headers)

    async def _ask_video_size(self, url: str, headers: dict[str, str]) -> int | None:
        """先 HEAD 后 Range，把「这条视频多大」问出来；问不到返回 None。"""
        size = await self._head_video_size(url, headers)
        if size is not None:
            return size
        return await self._range_video_size(url, headers)

    async def _head_video_size(self, url: str, headers: dict[str, str]) -> int | None:
        try:
            resp = await self._client.head(url, headers=headers)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[搬石/贴吧] 视频 HEAD 失败 %s：%s", url, exc)
            return None
        if resp.status_code != 200:
            logger.debug("[搬石/贴吧] 视频 HEAD 返回 HTTP %s：%s", resp.status_code, url)
            return None
        return _int_or_none(resp.headers.get("content-length"))

    async def _range_video_size(self, url: str, headers: dict[str, str]) -> int | None:
        """GET 一个字节，从 Content-Range（形如 ``bytes 0-0/12345``）里读总长。

        **必须用流式请求**。以前写成 ``await self._client.get(...)``，那是非流式
        调用：httpx 返回之前会把整个响应体读进内存。服务器一旦不认 ``Range``、
        回 200 加完整实体（贴吧的 CDN 就会这样），这个「只问大小」的探测等于把
        整个视频完整下载了一遍再丢掉 —— 正是要避免的事，一个 100MB 的视频能
        直接把这一轮的耗时和内存都吃光。改成 ``stream`` 只读响应头，连接随即关掉。
        """
        probe = dict(headers)
        probe["Range"] = "bytes=0-0"
        try:
            async with self._client.stream("GET", url, headers=probe) as resp:
                # 206 = 服务端接受了范围请求；200 = 它把整个文件送来了（那就看长度）
                if resp.status_code == 206:
                    return _parse_content_range(resp.headers.get("content-range"))
                if resp.status_code == 200:
                    return _int_or_none(resp.headers.get("content-length"))
                logger.debug(
                    "[搬石/贴吧] 视频范围探测返回 HTTP %s：%s", resp.status_code, url
                )
                return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("[搬石/贴吧] 视频范围探测失败 %s：%s", url, exc)
            return None
