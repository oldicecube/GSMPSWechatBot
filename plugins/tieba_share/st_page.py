"""麦麦贴吧搬石 —— 把贴吧帖子页原样搬成一张长图。

思路：不自己排版，直接取移动版帖子页的 HTML，去掉脚本、补上必需的样式，把正文插图换成
我们自己下好的 base64 图，再交给宿主的渲染服务整页截图 —— 出来就是贴吧手机版的样子。

三个必须知道的前提：

1. 样式表、图标原本都在 CDN 上。渲染前统一由 zero_net_html 收成零外链（内联 CSS、
   图转 data:、砍 url()、盖 CSP），所以页面一个请求都不发 —— 既绕开宿主在
   playwright <=1.60 上的路由回调缺陷，也比现场拉外链快。收之前先清掉页面上能主动
   发起请求的东西（见 _sanitize）：内联事件、iframe/object/embed、meta refresh、
   javascript: 链接，一律不留。
2. 帖子页默认只展开前两层，第 3 层起带 class_hide_flag（display:none）。这里注入 CSS 打开，
   并按 max_floors 把多余的楼层收掉。
3. 首楼多图在 DOM 里只有 3 个格子，其余图只在内联 JS 的 contentTotal 里。脚本剥掉以后
   那些图没有任何标签可换，只能补在拼贴块后面（见 _expand_first_floor_images）。

这一层只做字符串处理，不联网、不依赖宿主，方便单独测。
"""

from __future__ import annotations

import re
from html import escape as html_escape

from st_tieba import images_of

TIEBA_ORIGIN = "https://tieba.baidu.com"
MOBILE_PATH = "/mo/q/m"
BASE_HREF = TIEBA_ORIGIN + MOBILE_PATH

# 手机版页面的宽度，截图出来的排版才和贴吧 App 里看到的一致
PAGE_WIDTH = 414

_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.I | re.S)
_SCRIPT_OPEN_RE = re.compile(r"<script\b[^>]*>", re.I | re.S)
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.I)
_CLASS_ATTR_RE = re.compile(r"""\bclass=["']([^"']*)["']""", re.I)
# 找「可能要补图的标签」：占位 div 和真 img，一次扫完
_IMAGE_SPOT_RE = re.compile(r"<div\b[^>]*>|<img\b[^>]*>", re.I | re.S)
# 首楼多图的拼贴块开标签。收尾位置必须做标签配平算，不能拿正则数 </div>：
# 格子里面还嵌着 <div class="pb_imgs_img"></div>，数到第一个就把整块截断了。
_PB_IMGS_OPEN_RE = re.compile(r"""<div\b[^>]*\bid=["']pb_imgs["'][^>]*>""", re.I | re.S)
_DIV_TAG_RE = re.compile(r"<div\b[^>]*>|</div\s*>", re.I | re.S)
_DATA_URI_SRC_RE = re.compile(r"""(?:src|data-src|data-original)=["'](data:[^"']+)["']""", re.I)
_PROTOCOL_RE = re.compile(r"""(?P<attr>\b(?:href|src|data-src|data-original)=)(?P<q>["'])//""", re.I)
# 净化用：页面上能主动联网或跳转的东西
_DANGEROUS_TAG_RE = re.compile(
    r"<(?:iframe|frame|object|embed|applet)\b[^>]*>.*?</(?:iframe|frame|object|embed|applet)\s*>",
    re.I | re.S,
)
_DANGEROUS_OPEN_RE = re.compile(r"<(?:iframe|frame|object|embed|applet)\b[^>]*/?>", re.I)
_META_REFRESH_RE = re.compile(
    r"""<meta\b[^>]*http-equiv\s*=\s*["']?refresh["']?[^>]*>""", re.I | re.S
)
_ON_ATTR_RE = re.compile(r"""\son[a-z]{3,}\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)""", re.I)
_JS_HREF_RE = re.compile(r"""\b(?:href|src)\s*=\s*(["'])\s*javascript:[^"']*\1""", re.I)

# 图片占位一律认「标签里有没有 data-url」，判据统一交给 st_tieba.images_of：
#   正文插图 j_media_thumb_holder、单图首楼 pb_img_item、多图首楼 pb_imgs_top /
#   pb_imgs_bottom_left / pb_imgs_bottom_right 都带 data-url；容器 div（pb_imgs、
#   pb_imgs_div）不带，所以不会把容器当成图片。多认了 data-url 就自动覆盖上面三种。

# 页面样式：两个 %d 是页面宽度
_CSS_COMMON = """
html,body{margin:0!important;padding:0!important;background:#f2f3f5!important;}
body{width:%dpx!important;max-width:%dpx!important;overflow-x:hidden!important;}
/* 顶栏是 fixed 的，去掉以后要把外层的上边距一起收掉，不然最上面留一条白带；
   #main 还给顶栏留了 45px 的 margin-top，不一起清掉就会在标题上方空出一块 */
#glob{padding-top:0!important;margin-top:0!important;}
#main{margin-top:0!important;padding-top:0!important;}
/* 帖子页默认把第 3 层往后折叠起来，这里全部展开 */
li.list_item.post_list_item,.class_hide_flag{display:block!important;}
/* 只留帖子本身：去顶栏、去 App 引导、去广告、去页脚、去下载弹窗、去楼中楼「打开 App 看评论」按钮。
   注意页脚容器的类名是 pb-footer（连字符），不是 pb_footer，写错就整块留在图里。
   楼中楼那块有 normal / from-toutiao 两份，一起收掉。 */
#i_head,.pb_mask,.wake_app_tip,.app_wakeup_component,.bdps_login_ad,.liuliang_pop,
.pb_transfer_app,.j_pb_footer,.pb-footer,.j_footer,.footer_logo,
.footer_link,.footer_read,.footer_read_link,.j_footer_link,
.pb_prompt_background,.pb_prompt_content,.hongbao_close,.hongbao_fire,
.father-cut-daoliu-normal-box,.father-cut-daoliu-from-toutiao-box,
.father-cut-recommend-normal-box,.father-cut-recommend-from-toutiao-box,
.pb_cut_hot_recommend,.operate_statistic_wrapper,.more_newspinner,.pb_new_popup,
.post_star,.pb_newshare,.mini_program,.pb_send_bar,.recommend,.d_post_recommend,
.pb_ads,[class*="ad_"],#diversBanner,.list_item_more_operation,.poster_only,
.img_desc,.pb_imgs_weixin{display:none!important;}
/* 正文插图：补到的铺满宽度，没补到的整块收掉，别在图上留一片灰框 */
.st-page-img-wrap{width:100%%!important;height:auto!important;margin:6px 0!important;}
.st-page-img{display:block!important;width:100%%!important;height:auto!important;}
.st-page-miss,.st-page-miss-img{display:none!important;}
/* 首楼多图的拼贴块在贴吧自己的 CSS 里是 display:none，靠 pb_imgs 那个 JS 组件
   在运行时放开。我们剥掉了脚本，所以它永远不显示 —— 首楼多图会一张都看不到。
   这里直接放开，并把三个格子改成竖排（拼贴在手机宽度下本来也是三张挤一起）。
   另外贴吧给 .pb_imgs img 写了 position:absolute + translate 居中，格子自身又是
   float，撑不出高度；下面必须把图片改回文档流，否则整个拼贴块高度是 0。 */
#pb_imgs{display:block!important;height:auto!important;overflow:visible!important;}
#pb_imgs_div{height:auto!important;overflow:visible!important;}
#pb_imgs_div::after{content:"";display:block;clear:both;}
.pb_imgs_top,.pb_imgs_bottom_left,.pb_imgs_bottom_right{
  width:100%%!important;height:auto!important;float:none!important;margin:0!important;}
#pb_imgs img,.pb_imgs img{
  position:static!important;transform:none!important;top:auto!important;left:auto!important;
  width:100%%!important;height:auto!important;}
.st-page-img-extra{clear:both!important;}
.st-page-img-extra .st-page-img-wrap:first-child{margin-top:0!important;}
"""


def _page_css(max_floors: int) -> str:
    """拼出注入用的 CSS。max_floors 大于 0 时只留前几层。"""
    css = _CSS_COMMON % (PAGE_WIDTH, PAGE_WIDTH)
    keep = int(max_floors or 0)
    if keep > 0:
        # 楼层都是同一个 ul 下的兄弟节点，用 nth-child 裁最省事
        css += "ul.father_cut_list_class>li:nth-child(n+%d){display:none!important;}" % (keep + 1)
    return css


def placeholder_urls(raw: str) -> list[tuple[str, str]]:
    """列出页面里所有要补图的图片，按出现顺序去重。

    图片在页面里有两种装法，两边都得收：

    1. ``<div data-url=...>`` 占位（正文插图 j_media_thumb_holder、单图首楼
       pb_img_item、多图首楼 pb_imgs_top / pb_imgs_bottom_left / pb_imgs_bottom_right）。
       容器 div（pb_imgs、pb_imgs_div）不带 data-url，交给 images_of 放过即可。
    2. 真 ``<img>`` 标签（部分楼层直接给 img，不是占位 div）。

    以前只扫 div，第二种就永远不会被预下成 base64 —— 长图里留一片灰框。
    这里和 images_of 的口径必须一致，否则「替换时认得的」和「下载时认得的」对不上。

    Returns:
        [(钥匙, 原图直链)]。钥匙是 data-url 或 img 的 src，既当下载兜底键，
        也当后面替换时的查找键。
    """
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _IMAGE_SPOT_RE.finditer(raw or ""):
        tag = match.group(0)
        origin, thumbs = images_of(tag)
        if not thumbs:
            continue
        signed = thumbs[0]
        if signed in seen:
            continue
        seen.add(signed)
        out.append((signed, origin[0] if origin else signed))
    return out


def _lookup(tag: str, pool: dict[str, str]) -> tuple[str, str]:
    """返回 (命中的池键, data URI)；没命中就是 ("", "")。"""
    origin, thumbs = images_of(tag)
    if not thumbs:
        return "", ""
    for key in list(thumbs) + list(origin):
        uri = pool.get(key, "")
        if uri:
            return key, uri
    return "", ""


def _lookup_uri(tag: str, pool: dict[str, str]) -> str:
    """在补图池里找这个标签对应的 data URI，找不到返回空串。"""
    return _lookup(tag, pool)[1]


def _lookup_key(tag: str, pool: dict[str, str]) -> str:
    """在补图池里找这个标签命中的那个键（用来判断哪些属性该换）。"""
    return _lookup(tag, pool)[0]


def _swap_placeholder(tag: str, pool: dict[str, str]) -> str:
    """把一个图片占位 div 换成我们下好的图；没下到就换成待隐藏的占位。

    只换开标签：占位本身是空 div，原来的 </div> 正好收掉这里新开的 div。
    解析不出图片的标签原样放回去（容器 div、非图片的 data-url 都在这里被放过）。
    """
    if not images_of(tag)[1]:
        return tag
    uri = _lookup_uri(tag, pool)
    if not uri:
        return '<div class="st-page-miss">'
    return '<div class="st-page-img-wrap"><img class="st-page-img" src="%s">' % uri


def _swap_img_tag(tag: str, pool: dict[str, str]) -> str:
    """把真 ``<img>`` 换成我们下好的图。

    这里**不能**照搬 _swap_placeholder 换成 div —— img 是空元素、没有闭合标签，
    换成 div 会让整份文档的标签配平垮掉（渲染出来就是一片错位）。
    图没下到就整块收掉（加 style 隐藏），同样不留下灰框。

    命中以后整条标签重建，而不是只改某一个属性的值：懒加载的 img 常常同时挂着
    data-src（真图）和 src（loading.gif），而且属性里的 URL 还是 HTML 转义过的
    （&amp; 之类），逐个属性比字符串既容易漏也容易错。重建时只留原 class 和 src。
    """
    if not images_of(tag)[1]:
        return tag
    uri = _lookup_uri(tag, pool)
    if not uri:
        return '<img class="st-page-miss-img" style="display:none!important">'
    cls = _CLASS_ATTR_RE.search(tag)
    classes = "st-page-img"
    if cls:
        extra = " ".join(cls.group(1).split())
        if extra and "st-page-img" not in extra.split():
            classes = classes + " " + extra
    return '<img class="%s" src="%s">' % (classes, uri)


def _div_block_end(html: str, start: int) -> tuple[int, int]:
    """从 start（某个 ``<div`` 开标签的起点）起做标签配平，算出它自己的 ``</div>``。

    Returns:
        (闭合标签起点, 闭合标签终点)；配不平就是 (-1, -1)。

    为什么不用正则：拼贴块的格子里面还嵌着 ``<div class="pb_imgs_img"></div>``，
    按 ``</div>\\s*</div>`` 去数会在第一个格子上就收尾，补图区被塞进块内部。
    """
    depth = 0
    for m in _DIV_TAG_RE.finditer(html, start):
        if m.group(0).lower().startswith("</"):
            depth -= 1
            if depth <= 0:
                return m.start(), m.end()
        else:
            depth += 1
    return -1, -1


def _replace_spots(html: str, pool: dict[str, str]) -> tuple[str, set[str]]:
    """把页面里所有图片位置换成 data URI，返回 (新 HTML, 有位置的池键集合)。

    自己走一遍扫描而不是 ``re.sub``：占位 div 里常常还嵌着一个 ``<img>``，
    两次替换下来同一张图会渲两遍。这里一旦换掉占位 div，就连它内部一起吃掉，
    只留下原来的 ``</div>`` 去收我们新开的那个 div。
    """
    out: list[str] = []
    placed: set[str] = set()
    pos = 0
    for m in _IMAGE_SPOT_RE.finditer(html):
        if m.start() < pos:
            continue  # 落在已经被吃掉的占位块内部
        tag = m.group(0)
        key = _lookup_key(tag, pool)
        if key:
            placed.add(key)
        if tag[:4].lower() == "<img":
            out.append(html[pos : m.start()])
            out.append(_swap_img_tag(tag, pool))
            pos = m.end()
            continue
        if not images_of(tag)[1]:
            # 容器 div：原样留着，里面的 img 还要单独处理
            out.append(html[pos : m.start()])
            out.append(tag)
            pos = m.end()
            continue
        out.append(html[pos : m.start()])
        out.append(_swap_placeholder(tag, pool))
        # 内部内容整段丢掉，闭合标签留着收尾；配不平就只换开标签。
        close_start, _close_end = _div_block_end(html, m.start())
        pos = close_start if close_start >= 0 else m.end()
    out.append(html[pos:])
    return "".join(out), placed


def _expand_first_floor_images(
    html: str, images: dict[str, str], placed: set[str]
) -> str:
    """把首楼多图里「DOM 里根本没有」的那些图补在拼贴块后面。

    贴吧移动版对首楼多图只服务端渲染 3 个格子（pb_imgs_top / _bottom_left /
    _bottom_right），其余图**只在内联 JS 的 contentTotal 里**，DOM 里没有任何标签 ——
    光靠替换占位补不出来，长图里那层就只有 3 张。

    补什么：池子里**没有对应 DOM 位置**的图（``placed`` 之外的）。有位置但没下到的
    不算 —— 那是故意隐藏的，挪到首楼反而打乱顺序。
    """
    if not images:
        return html
    block = _PB_IMGS_OPEN_RE.search(html)
    if not block:
        return html
    _close_start, close_end = _div_block_end(html, block.start())
    if close_end < 0:
        return html
    present = {m.group(1) for m in _DATA_URI_SRC_RE.finditer(html)}
    extra: list[str] = []
    seen: set[str] = set()
    for key, uri in images.items():
        if key in placed or uri in present or uri in seen:
            continue
        seen.add(uri)
        extra.append(uri)
    if not extra:
        return html
    add = "".join(
        '<div class="st-page-img-wrap"><img class="st-page-img" src="%s"></div>' % uri
        for uri in extra
    )
    insert = '<div class="st-page-img-extra">%s</div>' % add
    return html[:close_end] + insert + html[close_end:]


def _sanitize(html: str) -> str:
    """清掉页面上能主动联网或跳转的东西。

    贴吧页面的样式和图原本都在 CDN 上，所以页面里任何能自己发请求的元素都等于把
    渲染器当跳板。脚本已经整段剥掉，这里再补几刀：
    iframe / frame / object / embed / applet、meta refresh、内联事件、javascript: 链接。
    正常的 img / style / link 全部保留，观感不受影响，外链由 :func:`zero_net_html`
    在渲染前统一收掉。
    """
    html = _DANGEROUS_TAG_RE.sub("", html)
    html = _DANGEROUS_OPEN_RE.sub("", html)
    html = _META_REFRESH_RE.sub("", html)
    html = _ON_ATTR_RE.sub("", html)
    html = _JS_HREF_RE.sub("", html)
    return html


def build_page_html(
    raw: str, *, images: dict[str, str] | None = None, max_floors: int = 0
) -> str:
    """把帖子页 HTML 收拾成可以直接交给渲染服务的整页。

    Args:
        raw: 帖子页原始 HTML。
        images: {data-url 或原图直链: data URI}，用来替换正文插图占位。
        max_floors: 最多留几层，0 表示不裁。
    """
    html = str(raw or "")
    if not html:
        return ""
    # 脚本一去，页面只剩静态结构，截图时不会有弹窗和二次跳转
    html = _SCRIPT_RE.sub("", html)
    html = _SCRIPT_OPEN_RE.sub("", html)
    html = _sanitize(html)
    # 协议相对地址（//tb1.bdstatic.com/...）在本地文档里没法解析，补成 https
    html = _PROTOCOL_RE.sub(lambda m: m.group("attr") + m.group("q") + "https://", html)
    head = _HEAD_OPEN_RE.search(html)
    inject = '<base href="%s"><style>%s</style>' % (BASE_HREF, _page_css(max_floors))
    if head:
        html = html[: head.end()] + inject + html[head.end() :]
    else:
        html = inject + html
    pool = {str(k): str(v) for k, v in (images or {}).items() if v}
    html, placed = _replace_spots(html, pool)
    # 补图要在替换之后做：先知道哪些池键有 DOM 位置，才知道哪些只能补在拼贴块后面。
    return _expand_first_floor_images(html, pool, placed)


# --- 零外链化 -------------------------------------------------------------------
# 「贴吧原页面」这条路原先必须开 allow_network：样式表和图标都在 CDN 上。但宿主的
# 网络路由回调在 playwright <=1.60 上必抛 TypeError（见 plugin._card_images 的说明），
# 于是每一个外链请求都悬空、load 永不触发、一路等到超时。
#
# 解法不是关掉网络，而是把页面变成一个**一个请求都不发**的页面：样式表内联、装饰图
# 转成 data:、CSS 里的 url() 全砍掉，最后再盖一张 CSP 兜底。这样路由回调一次都不会
# 被调用，缺陷就绕过去了；而在 1.61 以上反而更快（省掉几十次外链往返）。
#
# 这一层仍然只做字符串处理：要下的资源由调用方下好传进来，这里只负责拼回去。

# 换不上图时的占位：1x1 透明 GIF。不能用空串，空 src 在某些排版下会撑出一个框。
BLANK_IMAGE = (
    "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)
# 兜底的网络策略：就算上面哪一步漏了一个外链，也让它止步于 CSP，不要变成悬空请求。
_PAGE_CSP = (
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
    "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; "
    'media-src data: blob:; script-src \'none\'">'
)

_LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.I | re.S)
_REL_ATTR_RE = re.compile(r"""\brel\s*=\s*(?P<q>["'])(?P<v>.*?)(?P=q)""", re.I | re.S)
_HREF_ATTR_RE = re.compile(r"""\bhref\s*=\s*(?P<q>["'])(?P<v>.*?)(?P=q)""", re.I | re.S)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I | re.S)
_SRC_ATTR_RE = re.compile(r"""\bsrc\s*=\s*(?P<q>["'])(?P<v>.*?)(?P=q)""", re.I | re.S)
# 属性值用反引用配对：值里允许出现另一种引号（URL 里 ?q="1" 这种），
# 用 [^"']* 会在那个引号处截断，剩下的半截留在标签里把 HTML 弄坏。
# srcset / data-src 里还留着原地址，浏览器会拿它去请求，所以一律摘掉
_SRCSET_ATTR_RE = re.compile(
    r"""\s(?:srcset|data-src|data-srcset|data-original)\s*=\s*(?:"[^"]*"|'[^']*')""",
    re.I,
)
# CSS 里的 url()，但放过 data: —— 那些是我们自己嵌进去的图，砍了页面就空了
_CSS_URL_RE = re.compile(r"""url\(\s*(?!['"]?data:)['"]?[^)'"]*['"]?\s*\)""", re.I)

# 换图时把原地址留在这个属性上。贴吧靠属性选择器认图：
#   .content img[src*="tb/editor"] { height: 16px }
# src 一旦换成 data:，这类规则全部失配，表情就会按原图尺寸撑开。所以原地址要留下来，
# 并把 CSS 里的 [src...] 一并改指到这个属性（见 _keep_src_selector）。
_ORIG_SRC_ATTR = "data-orig-src"
# 只认属性选择器里的 src：`[src]` `[src*=` `[src^=` `[src$=` `[src~=` `[src|=` `[src =`
_CSS_SRC_SEL_RE = re.compile(r"\[\s*src\s*(?=[\]\^$*~|=])", re.I)
_STYLE_BLOCK_RE = re.compile(r"(<style\b[^>]*>)(.*?)(</style\s*>)", re.I | re.S)


def _keep_src_selector(css: str) -> str:
    """把 CSS 里的 [src...] 属性选择器改指到 data-orig-src。

    换图之后 src 已经是 data:，原样的选择器一条都命中不了，而页面的排版很依赖它们
    （最典型的是表情高度）。原地址留在 data-orig-src 上，这里把选择器改成读那个属性，
    匹配结果就和换图前一致。
    """
    return _CSS_SRC_SEL_RE.sub("[" + _ORIG_SRC_ATTR, str(css or ""))


def _is_http(url: str) -> bool:
    """地址是不是外链（http/https）。"""
    low = (url or "").strip().lower()
    return low.startswith("http://") or low.startswith("https://")


def page_asset_urls(html: str) -> tuple[list[str], list[str]]:
    """列出页面上的外链样式表和外链图片，各按出现顺序去重。

    Returns:
        (样式表 href 列表, 图片 src 列表)。
    """
    styles: list[str] = []
    for tag in _LINK_TAG_RE.findall(html or ""):
        rel = _REL_ATTR_RE.search(tag)
        if not rel or "stylesheet" not in rel.group("v").lower():
            continue
        href = _HREF_ATTR_RE.search(tag)
        href_url = href.group("v") if href else ""
        if _is_http(href_url) and href_url not in styles:
            styles.append(href_url)
    images: list[str] = []
    for tag in _IMG_TAG_RE.findall(html or ""):
        src = _SRC_ATTR_RE.search(tag)
        url = src.group("v") if src else ""
        if _is_http(url) and url not in images:
            images.append(url)
    return styles, images


def zero_net_html(
    html: str,
    *,
    styles: dict[str, str] | None = None,
    images: dict[str, str] | None = None,
) -> str:
    """把整页拼成零外链：样式表内联、装饰图换 data:、url() 全砍、盖 CSP。

    Args:
        html: :func:`build_page_html` 的产物（正文插图已经是 data: 了）。
        styles: {样式表 href: CSS 文本}。没下到的样式表整段丢掉。
        images: {图片 src: data URI}。没下到、超预算的换成 :data:`BLANK_IMAGE`。
    """
    html = str(html or "")
    if not html:
        return ""
    style_map = {str(k): str(v) for k, v in (styles or {}).items() if v}
    image_map = {str(k): str(v) for k, v in (images or {}).items() if v}

    def _inline(match: "re.Match[str]") -> str:
        tag = match.group(0)
        rel = _REL_ATTR_RE.search(tag)
        if not rel or "stylesheet" not in rel.group("v").lower():
            return tag
        href = _HREF_ATTR_RE.search(tag)
        css = style_map.get(href.group("v"), "") if href else ""
        if not css:
            return ""
        # 样式表里的背景图/图标也是外链，内联之后照样会发请求，所以要一起砍掉
        return "<style>%s</style>" % _CSS_URL_RE.sub("none", css)

    html = _LINK_TAG_RE.sub(_inline, html)

    def _swap(match: "re.Match[str]") -> str:
        tag = match.group(0)
        src = _SRC_ATTR_RE.search(tag)
        if not src:
            return tag
        url = src.group("v")
        if not _is_http(url):
            return tag
        data = image_map.get(url) or BLANK_IMAGE
        # 原地址必须留下：CSS 里的 [src*=...] 改写后要靠它命中
        keep = ' %s="%s"' % (_ORIG_SRC_ATTR, html_escape(url, quote=True))
        # 整条 src 属性重建，引号沿用原来的那种，其余属性一个不动
        q = src.group("q")
        return tag[: src.start()] + "src=" + q + data + q + keep + tag[src.end() :]

    html = _IMG_TAG_RE.sub(_swap, html)
    html = _SRCSET_ATTR_RE.sub("", html)
    # 页面自带的 <style> 和上面内联进来的样式表，里面的 [src...] 都要跟着改
    html = _STYLE_BLOCK_RE.sub(
        lambda m: m.group(1) + _keep_src_selector(m.group(2)) + m.group(3), html
    )
    # 内联 style 和残留 CSS 里的 url() 也要砍：那是最难找的一类外链
    html = _CSS_URL_RE.sub("none", html)
    head = _HEAD_OPEN_RE.search(html)
    if head:
        return html[: head.end()] + _PAGE_CSP + html[head.end() :]
    return _PAGE_CSP + html
