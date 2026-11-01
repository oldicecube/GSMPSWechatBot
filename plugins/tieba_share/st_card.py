"""麦麦贴吧搬石 —— 长图卡片渲染。

把一条帖子的首页楼层拼成一张竖图，交给宿主的渲染器出图：
    ctx.render.html2png(html, selector="#card", full_page=True, device_scale_factor=2.0)

三条硬约束（照宿主实现来的，踩过就废）：

1. 根节点必须带 id="card"，宿主的截图器按这个选择器找元素。
2. 宿主渲染器没有外网，HTML 里出现的图片一律得是 base64 data URI，
   直接写 https 图片地址会渲成空白。
3. Chromium 的截图画布有高度上限，一张图太高会直接截失败。
   所以调用方要按层数分段（见 plugin.py 的 _plan_card_chunks），
   这里只负责画一段，并在标题上标出「第几段」。
"""

from __future__ import annotations

import base64
import html
import io
import logging
from typing import Any

from st_tieba import pick_comments

logger = logging.getLogger(__name__)

try:  # Pillow 是宿主自带的，缺了也能跑（只是不压缩）
    from PIL import Image

    _HAS_PIL = True
except Exception:  # noqa: BLE001
    Image = None  # type: ignore[assignment]
    _HAS_PIL = False

DEFAULT_WIDTH = 830
# 贴吧手机版的主色。取色来自真实页面（tb1.bdstatic.com 的 common CSS 与
# 渲染后的计算样式）：链接/强调 #3385ff、用户名 #3361a7、正文 #333333、
# 次要文字 #a9aeb3、页面底色 #f2f3f5、楼层白卡 #ffffff、楼中楼灰底 #f5f5f5。
DEFAULT_ACCENT = "#3385ff"

# 卡片配色照贴吧手机版来：浅灰底 + 白色楼层卡 + 蓝色强调。以前是深色主题，
# 和「贴吧原页面」模式出来的图完全是两种观感，用户切模式会以为换了个插件。
# 楼层之间的 16px 灰缝用 border 实现而不是 margin —— margin 会被 #card 的
# 边界吃掉，截图时看不到，两张卡就贴在一起了。
_CARD_CSS = """
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { background: #f2f3f5; }
#card {
  width: #WIDTH#px;
  background: #f2f3f5;
  color: #333333;
  font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans SC", system-ui, sans-serif;
  padding-bottom: 16px;
}
.st-head { padding: 26px 28px 20px; background: #ffffff; border-bottom: 16px solid #f2f3f5; }
.st-tagline { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; }
.st-forum {
  font-size: 13px; font-weight: 700; letter-spacing: .5px;
  color: #ffffff; background: #ACCENT#; border-radius: 4px; padding: 3px 10px;
}
.st-kind { font-size: 12px; letter-spacing: 2px; color: #a9aeb3; text-transform: uppercase; }
.st-title { font-size: 26px; font-weight: 700; line-height: 1.45; color: #222222; }
.st-meta { margin-top: 12px; font-size: 13px; color: #a9aeb3; }
.st-meta b { color: #ACCENT#; font-weight: 700; }
.st-floor { padding: 20px 28px; background: #ffffff; }
.st-floor + .st-floor { border-top: 16px solid #f2f3f5; }
.st-fhead { display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; }
.st-fn { font-size: 13px; font-weight: 700; color: #a9aeb3; }
.st-author { font-size: 13.5px; font-weight: 600; color: #3361a7; }
.st-time { margin-left: auto; font-size: 12px; color: #a9aeb3; }
.st-text { font-size: 16px; line-height: 1.75; color: #333333; white-space: pre-wrap; word-break: break-word; }
.st-empty { font-size: 14px; color: #a9aeb3; font-style: italic; }
.st-img { display: block; width: 100%; height: auto; margin-top: 12px; border-radius: 6px; }
/* 楼中楼：贴在楼层正文下面的一块浅灰圆角，跟贴吧手机版一致 */
.st-lzl { margin-top: 14px; padding: 12px 14px; background: #f5f5f5; border-radius: 6px; }
.st-lzl-item { font-size: 14px; line-height: 1.7; color: #333333; }
.st-lzl-item + .st-lzl-item { margin-top: 7px; }
.st-lzl-name { color: #3361a7; font-weight: 600; }
.st-lzl-owner { color: #ACCENT#; font-weight: 700; }
.st-lzl-more { margin-top: 8px; font-size: 12.5px; color: #a9aeb3; }
.st-foot {
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 28px 20px; background: #ffffff; border-top: 16px solid #f2f3f5;
  font-size: 12px; color: #a9aeb3;
}
.st-foot .st-brand { color: #ACCENT#; letter-spacing: 1px; }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _sniff_mime(raw: bytes) -> str:
    if raw[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def image_data_uri(raw: bytes, *, max_width: int = 1080, quality: int = 85) -> str:
    """把图片字节转成 data URI，顺手等比缩小并压成 JPEG。

    压不动（没有 Pillow、动图、格式不认）就原样 base64。
    """
    if not raw:
        return ""
    if _HAS_PIL:
        try:
            with Image.open(io.BytesIO(raw)) as im:
                fmt = str(im.format or "").upper()
                if fmt == "GIF" and int(getattr(im, "n_frames", 1) or 1) > 1:
                    # 动图转 JPEG 只会剩第一帧，只能原样发。单帧 GIF 继续往下走，
                    # 否则它绕过 max_width，一张大 GIF 就能把整张长图撑爆截图上限。
                    return "data:image/gif;base64," + base64.b64encode(raw).decode("ascii")
                work = im.convert("RGB")
                width, height = work.size
                resized = False
                if max_width and width > max_width:
                    work = work.resize(
                        (int(max_width), max(1, int(height * max_width / width))), Image.LANCZOS
                    )
                    resized = True
                buf = io.BytesIO()
                work.save(buf, format="JPEG", quality=int(quality), optimize=True)
                data = buf.getvalue()
                if not resized and len(data) >= len(raw):
                    # 压完反而更大（小图、纯色图）就留着原图
                    return "data:%s;base64,%s" % (_sniff_mime(raw), base64.b64encode(raw).decode("ascii"))
                return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
        except Exception as exc:  # noqa: BLE001
            logger.debug("[搬石/渲染] 图片压缩失败，按原图发送：%s", exc)
    return "data:%s;base64,%s" % (_sniff_mime(raw), base64.b64encode(raw).decode("ascii"))


def _comments_html(floor: Any, *, limit: int = 3) -> str:
    """把一个楼层的楼中楼画成一小块。没有评论就返回空串。

    只画前 limit 条（楼主回复优先，见 st_tieba.pick_comments）。页面本身也只渲染前几条，
    所以用 comment_total 把「还有多少条没显示」写出来，免得看着像是帖子只有两条评论。
    """

    comments = list(getattr(floor, "comments", None) or [])
    if not comments:
        return ""
    shown = pick_comments(comments, limit=limit)
    items = []
    for item in shown:
        name = str(getattr(item, "author", "") or "").strip() or "匿名"
        text = str(getattr(item, "text", "") or "").strip()
        if not text:
            continue
        cls = "st-lzl-owner" if getattr(item, "is_author", False) else "st-lzl-name"
        label = _esc(name) + ("（楼主）" if getattr(item, "is_author", False) else "")
        items.append(
            '<div class="st-lzl-item"><span class="%s">%s</span>：%s</div>'

            % (cls, label, _esc(text))
        )
    if not items:
        return ""
    try:
        total = int(getattr(floor, "comment_total", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    # 用「真正画出来的条数」算剩余：挑出来但正文为空的那几条不会出现在图里，
    # 按 pick_comments 的条数算会把「还有几条」少报。
    more = max(total - len(items), 0)
    tail = '<div class="st-lzl-more">还有 %d 条评论</div>' % more if more else ""
    return '<div class="st-lzl">%s%s</div>' % ("".join(items), tail)


def _floors_html(
    floors: list[Any], images: dict[str, str], *, max_images: int
) -> tuple[str, int, int]:
    """画楼层。返回（HTML, 用掉的图片张数, 真正画出来的层数）。

    层数要单独返回：空楼层（既没正文也没图）会被跳过，直接拿 len(floors) 报
    会在标题上写成「本段 12 层」而图里只有 8 层。
    """
    parts: list[str] = []
    used = 0
    drawn = 0
    for floor in floors:
        text = str(getattr(floor, "text", "") or "").strip()
        picture_html: list[str] = []
        for url in list(getattr(floor, "images", None) or []):
            if used >= max_images:
                break
            uri = images.get(url)
            if not uri:
                continue
            picture_html.append('<img class="st-img" src="%s" alt="">' % uri)
            used += 1
        if not text and not picture_html:
            continue
        fn = int(getattr(floor, "fn", 0) or 0)
        author = str(getattr(floor, "author", "") or "").strip() or "匿名"
        stamp = str(getattr(floor, "time", "") or "").strip()
        head = (
            '<div class="st-fhead"><span class="st-fn">%d楼</span>'
            '<span class="st-author">%s</span>'
            '<span class="st-time">%s</span></div>' % (fn, _esc(author), _esc(stamp))
        )
        body = '<div class="st-text">%s</div>' % _esc(text) if text else ""
        parts.append(
            '<div class="st-floor">%s%s%s%s</div>'
            % (head, body, "".join(picture_html), _comments_html(floor))
        )
        drawn += 1
    return "".join(parts), used, drawn


def build_thread_html(
    thread: Any,
    floors: list[Any],
    *,
    images: dict[str, str] | None = None,
    width: int = DEFAULT_WIDTH,
    max_images: int = 9,
    part: int = 0,
    parts: int = 1,
    accent: str = DEFAULT_ACCENT,
) -> str:
    """把一个帖子的若干楼层渲染成卡片 HTML。

    Args:
        thread: StoneThread。
        floors: 这一段要画的楼层（StoneFloor 列表）。
        images: 原图地址到 data URI 的映射，没命中的图会被跳过。
        width: 卡片宽度（像素）。
        max_images: 整张卡片最多放几张图。
        part/parts: 分段信息，parts 大于 1 时标题后面会标「第 n/m 段」。
    """
    forum = str(getattr(thread, "forum", "") or "").strip() or "贴吧"
    title = str(getattr(thread, "title", "") or "").strip() or "（无标题）"
    replies = int(getattr(thread, "replies", 0) or 0)
    body_html, used, drawn = _floors_html(list(floors), images or {}, max_images=max_images)
    if not body_html:
        body_html = '<div class="st-floor"><div class="st-empty">这一段没有可显示的楼层</div></div>'
    suffix = ""
    if parts > 1:
        suffix = "（第 %d/%d 段）" % (int(part) + 1, int(parts))
    meta = "<b>%d</b> 回复 · 本段 <b>%d</b> 层 · <b>%d</b> 图" % (replies, drawn, used)
    css = _CARD_CSS.replace("#ACCENT#", accent)
    css = css.replace("#WIDTH#", str(int(width or DEFAULT_WIDTH)))
    return (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
        "<style>%s</style></head><body>"
        "<div id=\"card\">"
        "<div class=\"st-head\">"
        "<div class=\"st-tagline\"><span class=\"st-forum\">%s</span>"
        "<span class=\"st-kind\">tieba</span></div>"
        "<div class=\"st-title\">%s%s</div>"
        "<div class=\"st-meta\">%s</div>"
        "</div>"
        "%s"
        "<div class=\"st-foot\"><span class=\"st-brand\">麦麦贴吧搬石</span>"
        "<span>tid %s</span></div>"
        "</div></body></html>"
    ) % (
        css,
        _esc(forum),
        _esc(title),
        _esc(suffix),
        meta,
        body_html,
        _esc(str(getattr(thread, "tid", "") or "")),
    )


def build_thread_text(
    thread: Any, floors: list[Any], *, max_images: int = 9, with_images: bool = True
) -> str:
    """纯文本回退：渲染失败或关掉卡片时用这个发。"""
    forum = str(getattr(thread, "forum", "") or "").strip()
    title = str(getattr(thread, "title", "") or "").strip() or "（无标题）"
    lines = ["【%s】%s" % (forum, title) if forum else "【%s】" % title]
    used = 0
    for floor in list(floors):
        text = str(getattr(floor, "text", "") or "").strip()
        if not text:
            continue
        fn = int(getattr(floor, "fn", 0) or 0)
        author = str(getattr(floor, "author", "") or "").strip() or "匿名"
        lines.append("%d楼 %s：%s" % (fn, author, text))
        for item in pick_comments(list(getattr(floor, "comments", None) or []), limit=3):
            name = str(getattr(item, "author", "") or "").strip() or "匿名"
            body = str(getattr(item, "text", "") or "").strip()
            if not body:
                continue
            tag = "（楼主）" if getattr(item, "is_author", False) else ""
            lines.append("  └ %s%s：%s" % (name, tag, body))
        if with_images:
            pics = len(list(getattr(floor, "images", None) or []))
            if pics and used < max_images:
                take = min(pics, max_images - used)
                used += take
                lines.append("（本层 %d 张图）" % take)
    return "\n".join(lines)
