"""麦麦贴吧搬石 —— 让麦麦把贴吧的帖子搬进群。

只做三件事：逛吧、取帖、发到群里。签到、回帖、私信一概不碰。

设计取舍：

1. **不登录**。贴吧对「一个 cookie 都不带」的请求会拦成 403，先去百度首页拿一次访客
   cookie 再回来就正常了 —— 所以插件不需要任何人提供账号或票据。
2. **被拦就停**。触发「百度安全验证」时立刻结束本轮并冷却 15 分钟，而不是换个姿势继续
   敲门；已经搬过的记录保留，下一轮接着来。
3. **整帖渲染成长图**：每帖渲成一张长图（过长就切成几张），也可以打包成一条聊天记录。
   长图渲不出来就跳过这帖 —— 既不补纯文字提要，也不退回逐层发图文，那两种兜底在群里
   都是刷屏、内容还不完整；这帖不记成「已搬过」，下一轮会重来。
4. **去重靠本地记录**（插件 data 目录里的 JSON），不动宿主的数据库 ——
   记录丢了最多重搬一次，不会影响机器人本身。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json as _json
import random
import re
import sys as _sys
import time
import urllib.parse
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Sequence

_PLUGIN_DIR = Path(__file__).resolve().parent
# 宿主是按文件路径加载 plugin.py 的，插件目录名（mizuku003-tieba-share）带连字符，
# 本来就不是一个包，所以 `from . import st_client` 这种相对导入根本用不了；
# st_client / st_fetch 这些同级模块之间也互相导入，同样依赖这一条。
# 代价是往解释器的 sys.path 里插了一项进程级全局状态 —— 模块名都带 st_ 前缀，撞名概率极低。
if str(_PLUGIN_DIR) not in _sys.path:
    _sys.path.insert(0, str(_PLUGIN_DIR))

_DEFAULT_FORWARD_NAME = "？！石！？"


_TIMEOUT_HINTS = ("timeout", "timed out", "超时", "e_timeout")


def _mime_of_image(body: bytes) -> str:
    """按文件头猜图片 MIME。猜不出就按 JPEG —— 贴吧 CDN 上绝大多数是 JPEG。"""
    if body[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if body[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _looks_like_timeout(text: str) -> bool:
    """判断一条渲染失败信息是不是「等超时了」。

    超时和别的失败要分开对待：超时说明这页在给定的耐心内就是没 load 完，换一档
    缩放重来（页面要加载的外链一张不少）或者原样重试，都只是把同样的等待再来一遍；
    而浏览器刚崩、RPC 抖一下这类瞬时故障，隔几秒重试一次往往就过了。
    """
    low = str(text or "").lower()
    return any(hint in low for hint in _TIMEOUT_HINTS)


def _file_uri(path: Path) -> str:
    """把本地路径转成 ``file:///`` URI。

    Windows 下必须是三个斜杠（``file:///H:/...``）：NapCat 的 checkUriType 对
    ``file:`` 开头的 URI 在 win32 上按 ``uri.slice(8)`` 取路径，正好跳过
    ``file:///``；两斜杠也会退化成 ``slice(7)`` 而侥幸可用，但不能指望。
    反斜杠要换成斜杠，盘符前的斜杠不能少。

    路径里可能有空格或中文（主程序装在 ``D:/我的机器人/...`` 是常事），
    而 NapCat 会对 ``file:`` 的 URI 做 ``decodeURIComponent``，所以这里必须
    按 URL 规则转义：不转义的话空格与中文会被解成别的字节，轻则取不到图，
    重则 ``%`` 后面跟错字符直接抛异常。斜杠与盘符冒号要留原样。
    """
    try:
        resolved = Path(path).resolve()
    except Exception:  # noqa: BLE001
        resolved = Path(path)
    text = str(resolved).replace("\\", "/")
    if not text.startswith("/"):
        text = "/" + text
    return "file://" + urllib.parse.quote(text, safe="/:")


def _read_manifest_version() -> str:
    try:
        with open(_PLUGIN_DIR / "_manifest.json", encoding="utf-8") as handle:
            return str(_json.load(handle).get("version") or "")
    except Exception:  # noqa: BLE001
        return ""


_PLUGIN_VERSION = _read_manifest_version()

from maibot_sdk import Command, MaiBotPlugin, Tool  # noqa: E402
from maibot_sdk.types import ToolParameterInfo, ToolParamType  # noqa: E402

from st_card import build_thread_html, image_data_uri  # noqa: E402
from st_config import StoneConfig, in_quiet_hours, migrate_config, resolve_forums  # noqa: E402
from st_page import (  # noqa: E402
    build_page_html,
    page_asset_urls,
    placeholder_urls,
    zero_net_html,
)
from st_store import StoneStore  # noqa: E402
from st_fetch import BlockState, StoneProtoClient, thread_from_proto  # noqa: E402
from st_tieba import (  # noqa: E402
    StoneBlockedError,
    StoneTiebaClient,
    StoneTiebaError,
    _direct_image_url,
    content_total_images,
    to_origin_image,
)


# 长图分段：Chromium 的截图画布有高度上限，一张图太高会直接截失败。
# 高度只能估：正文按每行 36 字、行高 27 像素，图片按卡片宽等比缩放的真实比例算。
_MAX_CARD_HEIGHT = 6800.0
_TEXT_LINE_HEIGHT = 27.0
_TEXT_CHARS_PER_LINE = 36.0
_HEAD_HEIGHT = 320.0
_FLOOR_PADDING = 66.0
_DEFAULT_IMAGE_HEIGHT = 620.0

# 合并转发里一条帖子最多贡献几个节点（层多了聊天记录翻起来也累）
_FORWARD_FLOOR_NODES = 12

# 发视频走 ctx.send.custom("videourl", ...)：宿主没有原生视频组件，但会把它包成
# DictComponent 原样透传，适配器再把 dict 里的 videourl 编成真视频段（实测链路）。
# 视频是最慢的一次能力调用——两个 MB 实测要几秒，几十 MB 更久，而适配器侧
# action_timeout_sec 默认只有 15 秒，所以必须显式加时，否则会「其实发出去了却判超时」
# 又降级重发一遍，群里收到两条。参考宿主的 jm2pdf 插件，同样是 300 秒。
_VIDEO_SEND_TIMEOUT_MS = 300_000
# 合成聊天记录同样慢：一条转发里要塞好几个节点、每个节点还有 base64 长图，
# 同样得显式加时。少了它，一次慢转发会被判超时抛异常，走进 `if not sent` 那条
# 降级分支把视频再发一遍，群里就多出一份。
_FORWARD_TIMEOUT_MS = 300_000

# -- 发送失败重试 ----------------------------------------------------------
#
# 宿主的发送能力（`cap.call`）只要拿到返回值就算成功，它并不会告诉我们协议端
# 有没有真的把消息投出去：签名过期、`401`、适配器拒收，在插件看起来都和成功
# 一样。能观察到的失败只有两类 —— 抛异常（RPC 层断了、超时）和返回 None。
#
# **但重试本身就是重复的来源**。既然「失败」根本不能证明消息没发出去，那重试
# 一次就是在赌「刚才那次真的没送出去」—— 赌输一次，群里就多一条一模一样的。
# 聊天记录尤其明显：一条转发里装着一整轮内容，重试四次就是四条一样的记录
# （issue #1 的「重复发送多次合并的聊天记录」）。
#
# 所以普通消息给 3 次（单条文字/图片重了也只是多一句话，丢一条反而更糟），
# 而**合并转发只给 0 次**：它最慢、最容易超时，也最不该重复。
_SEND_RETRY_TIMES = 3
# 合并转发一次就够。转发是全轮最贵的一次调用，超时多半是「其实已经发出去了」，
# 再试一遍就是在群里贴第二条同样的聊天记录。
_FORWARD_RETRY_TIMES = 0
# 两次重试之间等多久。**指数退避**：RPC 抖动通常几十毫秒就过去了，
# 等 1 秒足够；真遇上宿主忙，2 秒、4 秒给足它缓过来。
_SEND_RETRY_BASE_S = 1.0

# -- 发送令牌桶 ------------------------------------------------------------
#
# 一轮搬运会连续发几十条消息，中间**一条都不歇**。QQ 服务端对此有频率限制，
# 发太快会被临时拒收 —— 而拒收在插件看来等于「发送成功」（见上），
# 于是消息静默丢失、用户只收到半批。
#
# 令牌桶按「每秒补几个令牌」限速，桶容量允许短时突发（首帖要秒发，
# 不能因为限速让用户等）。默认 1.5 个/秒、桶容量 5 —— 实测一轮 5 帖
# x 十几条消息不会被拒，同时也远没慢到用户能察觉。
_SEND_RATE_PER_SECOND = 1.5
_SEND_BURST = 5.0
# 一条合并转发是一个 RPC 帧，上限 16MB（host transport/base.py 的 MAX_FRAME_SIZE）。
#
# 算这个预算必须知道两件事，都跟直觉不一样：
#
#   1. 图片要 base64，比原图胀 4/3；
#   2. **宿主会把 base64 在段里存两份**。capabilities/core.py:411 的
#      ``_normalize_plugin_segment`` 对 image 段是 ``ns = dict(segment)`` 之后
#      再补一个 ``binary_data_base64``，原来那个 ``content`` 键并不删。于是同一个
#      字符串在 JSON 里出现两遍，实际占用是原图的 2 x 4/3 = 2.67 倍。
#
# 合起来：一张图能进帧的原文件上限只有 6.000 MiB（不是 12MB），整条聊天记录里
# 所有图片的「单份 base64 长度」加起来不能超过约 6.5MB。这里按**单份 base64 的
# 长度**累加（代码里就是 ``len(blob)``），所以预算取 6MB，留出昵称、楼号、JSON
# 结构等零碎的余量。实测纯图文 5 帖 x 12 楼 x 2 张 200KB 图，单份合计 31MB ——
# 不放预算这条聊天记录必然发不出去，而失败形式是整条丢，用户什么都看不到。
_FORWARD_FRAME_BUDGET_BYTES = 6 * 1024 * 1024
# 每个转发节点的固定开销（昵称、user_id、message_id、JSON 括号等）的粗略估值。
_FORWARD_NODE_OVERHEAD = 256
# 一个「本地路径引用」图片段在帧里的开销：路径字符串 + 外壳 JSON 的估值。
# 走这条路时图片本体根本不进帧，所以只有这点字节要算。
_PATH_SEGMENT_COST = 512

# 只搬哪种帖：列表页只能看出「有没有 media_flash」，但那只标记了首楼，
# 楼中楼里的视频看不出来，所以过滤放在取到详情之后按真实解析结果做。
# 「只搬视频帖」这类筛选多留几倍候选：列表页标记偶尔失准（视频帖被标成 0），
# 预筛看走眼时还能靠详情页复核捞回来。多取的只会多花几次详情页请求。
_FILTER_CANDIDATE_FACTOR = 3
_FILTER_ALL = "不限"
_FILTER_TEXT = "只发图文帖"
_FILTER_VIDEO = "只发视频帖"

# 工具「搬贴吧」只挑帖型，不管发法 —— 怎么发一律照配置走。
#   any   不限（用配置里「只搬哪种帖」那个值，默认）
#   text  只要图文帖      video 只要视频帖
_KIND_ANY = "any"
_KIND_TEXT = "text"
_KIND_VIDEO = "video"
# 工具调用最多一次搬几条（模型给大了就钳住，免得一次淹了整个群）
_TOOL_MAX_COUNT = 5
# 工具那一轮要改读配置的「工具」一节。以前把这个标记塞进 _kind_override，
# 但 _kind_override 装的是「只搬哪种帖」（中文值或空串），永远等不到这个标记，
# 于是工具节整节失效。现在单独用一个布尔量 _tool_round 背它。
# 吧名允许的字符：中英文数字下划线加间隔号，长度限制跟贴吧本身差不多
_FORUM_NAME_RE = re.compile(r"^[0-9A-Za-z_\u4e00-\u9fff·]{1,20}$")


def _thread_kind(value: Any) -> str:
    """把工具传来的帖型参数翻成配置里的说法，认不出的一律当「不限」。

    工具对模型只暴露 any/text/video 三个短词（模型不会念错、token 也省），
    内部统一用配置面板那一套中文值，免得两套词在代码里来回翻译。
    """
    text = str(value or "").strip().lower()
    if text == _KIND_TEXT:
        return _FILTER_TEXT
    if text == _KIND_VIDEO:
        return _FILTER_VIDEO
    return _FILTER_ALL


# 长图样式：自绘卡片（插件排版）/ 贴吧原页面（整页原样截图）
_CARD_STYLE_CARD = "自绘卡片"
_CARD_STYLE_PAGE = "贴吧原页面"
# 「贴吧原页面」的两种渲染方式（配置项 send.page_render）。
# * 零外链：插件先把样式表和装饰图内嵌成 data:、盖一条禁止外链的 CSP，页面一个
#   请求都不发。任何主程序版本都能出图 —— 这是默认值。
# * 原生：把原始页面直接丢给宿主浏览器，让它自己加载外链。只有 playwright >=1.61
#   才正常；<=1.60 会因为宿主那个路由回调缺陷每个请求都报错、最后超时。
_PAGE_RENDER_ZERO = "零外链"
_PAGE_RENDER_RAW = "原生"
# 原页面模式的截图参数：按手机屏宽截、允许联网去取 CDN 上的样式表、
# 补进页面的图按卡片宽留 820 像素就够清楚（页面里文字区就这么宽）
_PAGE_VIEWPORT_WIDTH = 414
_PAGE_VIEWPORT_HEIGHT = 900
# 60 秒。原页面模式的 wait_until 是 load，一个 30 楼的帖有七十多张外链图要走贴吧 CDN，
# 网速一差就得等；实测正常一页只要 1~4 秒，所以这个值平时根本用不到，只在慢网时救命。
#
# 它一度被收到 25 秒，起因是当时云端宿主有 bug（Playwright 1.58 上 page.route 的
# functools.partial handler 根本不进），每一档都必然等满超时，看起来就像「渲染就是慢」。
# 那个 bug 已在宿主侧靠升级 Playwright 修掉，一页现在 3~4 秒出图。既然正常路径用不到
# 这 60 秒，就没有理由为了「失败时少白等」去砍它 —— 砍了只会在慢网时误杀本来能出的图，
# 而误杀的代价是整帖不发。
# 真要减少白等，靠的是下面少留几档缩放（档数就是白等倍率），不是砍超时。
_PAGE_RENDER_TIMEOUT_MS = 60000
_PAGE_IMAGE_MAX_WIDTH = 820
# 原页面模式要把页面里每个图片占位都补上，缺一张就是一个洞（补不到的占位会被 CSS 收掉，
# 于是在长图里凭空少一张图）。所以这里不受 max_images 限制，只用这个硬上限兜住超长帖。
_PAGE_IMAGE_LIMIT = 80
# 零外链化的预算（见 :meth:`_localize_page`）。原页面除了正文插图，还有几十个界面
# 装饰图（头像框、表情、等级图标、底部按钮）散在 CDN 上。实测一页去重后二十来个、
# 合计几百 KB，所以 2 MiB 的总预算绰绰有余；单图 512 KiB 是给那张躲在红包弹窗里、
# 用户根本看不见的大图设的闸 —— 它一张就 673 KB，占掉三分之一的预算。
_PAGE_ASSET_BUDGET_BYTES = 2 * 1024 * 1024
_PAGE_ASSET_MAX_BYTES = 512 * 1024
_PAGE_CSS_MAX_BYTES = 512 * 1024
# 并发数。贴吧 CDN 吃得下这个量级，实测二十多个资源 0.8~1.6 秒就下完；
# 再往上加没有收益，反而更容易撞上 CDN 的限速。
_PAGE_ASSET_CONCURRENCY = 8
# 长图**发送**走的是本地路径，不占帧（见 _send_page_image），所以「图太大发不出去」
# 这条不成立。真正存在上限的只有一次：渲染结果从宿主回插件那一次 RPC ——
# transport/base.py 的 MAX_FRAME_SIZE 是 16 MiB，而结果里放的是 base64（膨胀 4/3），
# 于是**原图约 12 MB 是硬顶**，超了宿主直接抛 ValueError，用户一张图都看不到。
# 留一点余量给 JSON 包装字段，按 11.5 MiB 算。
_PAGE_IMAGE_HARD_LIMIT_BYTES = int(11.5 * 1024 * 1024)
# 长图渲染倍率，固定 1.5。这里以前是一圈「渲出来太大就降档重渲」的循环，
# 已经删掉 —— 降档是**再渲一遍**，总时间只会更长；实测 1.5x 换 1.0x 只省
# 0.15 秒，而一整轮真实耗时 1.7~2.4 秒里零外链化下载就占 0.8~1.07 秒，
# 真正慢的是网络。拿一张肉眼可见更糊的图去换这 0.15 秒不划算。
_PAGE_RENDER_SCALE = 1.5
# 渲染的 RPC 超时要在宿主渲染超时之上再留一点余量：让宿主自己先超时并回一个干净的
# 错误，而不是这边先掐断 RPC 把一个还在跑的渲染丢掉。
_RENDER_RPC_SLACK_MS = 15_000
# 自绘卡片是本地 HTML（图都内嵌成 data:，不联网），正常一两秒就出来。
# 同样给足余量：机器忙的时候多等几十秒，好过把一条本来能发的帖子判死。
_CARD_RENDER_TIMEOUT_MS = 45_000
# 渲染失败后在同一档重试的次数。只对「瞬时故障」生效（浏览器刚崩、RPC 抖了一下），
# 超时不重试 —— 超时说明这一页在外网那头就是等不到 load，原样再来一遍只会白等
# 第二个 60 秒，而慢网恰恰是常态（云端出网只有 3Mbps），等不到就是等不到。
_RENDER_TRANSIENT_RETRY = 1
# 重试前歇一下，给宿主那边重启浏览器的工夫（它的失败路径会 reset 掉实例）。
_RENDER_RETRY_PAUSE_S = 3.0

# 长图落盘目录（插件 data 目录下的 pages/）。
#
# 为什么要把长图写进文件、而不是直接把 base64 塞进转发节点：
# 整条合并转发是**一个** RPC 帧，上限 16MB（宿主 transport/base.py 的
# MAX_FRAME_SIZE）。base64 进帧还要被宿主存两份，于是「一张 6MB 的长图就吃掉
# 整条预算」，后面的帖子只能被踢出去，这一轮就发不成了。
#
# 改成让节点只带一个本地路径后，帧里就只有两百来字节，**与图片体积无关**：
# 实测 4 张各 6MB 的长图（合计 23.4MB）进同一条转发，插件发出的帧仍只有 1.4KB。
# 这条路能成是因为 NapCat 的 uriToLocalFile 明确认 file:// 前缀
# （napcat.mjs 的 checkUriType：`uri.startsWith("file:")` -> Local，且本地文件
# 原样使用不复制），已在真机验证。
#
# 代价是**协议端必须能按同一个绝对路径读到这张图**：路径只是字符串，
# 插件不负责把字节送过去。协议端与宿主同机（NapCat 与 MaiBot 装在同一台
# 机器上）时天然成立；协议端跑在容器里时必须把这个目录按相同路径挂进去
# （`-v <宿主 pages 目录>:<同一路径>:ro`），否则协议端打开文件失败，图片
# 静默不显示。这条在单机部署下永远不会暴露，换容器部署才踩得到。
_PAGE_CACHE_FILENAME = "page_%s_%d.png"
# 落盘前先认一眼文件头。渲染器偶尔会返回一段空壳或错误页文本，
# 那种东西写进 pages/ 只会变成一堆协议端读不出来的垃圾（曾经因此在插件
# 目录里堆过上万张全 0xFF 的假图）。认不出来就不落盘，调用方退回内嵌图片。
_PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
_JPEG_MAGIC = bytes([0xFF, 0xD8, 0xFF])
# 落盘的长图保留份数：只留最近这些张，避免长年累月把磁盘塞满。
# 转发是异步投递的，删太早会让 QQ 那边取不到图，所以按「张数」而不是「时间」清。
_PAGE_CACHE_KEEP = 60
# 给协议端留出的读文件时间：刚落盘的图在这一段时间内绝不删。
# NapCat 收到 file:// 之后要自己去读盘再上传，这条转发是异步投递的，
# 上一张贴出去、下一轮马上开始清理时，前面那张可能还没被读完。
_PAGE_CACHE_CLEAN_DELAY_S = 600.0
# 清理节流：一轮搬运会落盘上百张小图，每张都去 glob 一遍整个目录太浪费
# （目录常驻几十个文件，一轮下来就是上万次同步 stat，还会卡住事件循环）。
# 改成每隔这么多秒最多真正扫一次目录。
_PAGE_CACHE_PRUNE_INTERVAL_S = 30.0

# 没权限时的回复。两类命令各一句：搬石类群里可用，维护类只放管理员。
_MOVE_DENIED = "你没有使用此命令的权限。"
_MAINTENANCE_DENIED = "你没有使用此命令的权限（仅管理员）。"

_HELP_TEXT = "\n".join(
    [
        "麦麦贴吧搬石",
        "",
        "/搬石　　　　　　按配置搬一轮",
        "/搬视频　　　　只搬视频帖",
        "/搬石 状态　　看每个吧搬了多少",
        "/搬石 重置 [吧]　清空搬运记录",
        "/搬石 重置 冷却　解除反爬冷却",
        "/搬石 帮助　　这条说明",
    ]
)


# 宿主拼「引用回复 + 用户正文」时留下的包装。被引用原文总是包在
# 「[回复了…的消息: …]」这类壳子里，而用户这次敲的正文一定在壳子外面。
# 只认这些壳子 —— 不能把冒号也算进来，否则「/搬石 状态：xxx」这种正常输入
# 会被误判成引用而整条丢掉。
_QUOTE_SHELL_RE = re.compile(r"[\[【]\s*(?:回复|引用)[^\]】]{0,200}[\]】]")


def _quoted_command(text: str, command: str) -> bool:
    """命令是不是藏在被引用的原文里。

    宿主把「[回复了某人的消息: 原文] 用户正文」拼成一段文本再交给命令匹配，
    被引用原文里的命令会被一并匹配上。文本里凡是被「[回复了…]」包住的那一段，
    其中的命令都不算数；壳子外面的命令照常执行。
    """
    for shell in _QUOTE_SHELL_RE.finditer(text):
        if command in shell.group(0):
            return True
    return False


def _command_text(kwargs: dict[str, Any]) -> str:
    """命令入口拿到的原始文本（宿主把被引用原文也拼在里面）。

    宿主命令通道给的键名是 ``text``（src/plugin_runtime/component_query.py 的
    ``_build_command_executor`` 里 ``invoke_args["text"] = processed_plain_text``），
    不是 ``processed_plain_text``。以前按后者取，永远是空串，于是「引用内容里的
    命令不算数」这道守卫从来没生效过。旧键名留着兜底。
    """
    for key in ("text", "processed_plain_text", "plain_text"):
        value = kwargs.get(key)
        if value:
            return str(value)
    return ""


def _command_message_id(kwargs: dict[str, Any]) -> str:
    """这条命令消息的 id，用来挡住重复投递。

    宿主**不在顶层给 message_id**：它藏在 ``invoke_args["message"]`` 这个序列化
    过的消息字典里（src/plugin_runtime/host/message_utils.py 的
    ``MessageDict(message_id=...)``）。以前按顶层取，永远是空串，去重守卫形同虚设。
    顶层没有就再往 message 里找一层。
    """
    direct = str(kwargs.get("message_id") or "").strip()
    if direct:
        return direct
    message = kwargs.get("message")
    if isinstance(message, dict):
        return str(message.get("message_id") or "").strip()
    return ""


def _scoped_user_id(platform: Any, user_id: Any) -> str:
    """拼出 platform:user_id（平台名小写，和宿主的拼法一致）。"""
    plat = str(platform or "").strip().lower()
    uid = str(user_id or "").strip()
    if not plat or not uid:
        return ""
    return plat + ":" + uid


def _permission_set(values: Any) -> set[str]:
    """把宿主那份 platform:user_id 名单规范成集合；形状不对就当空集。"""
    allowed: set[str] = set()
    if not isinstance(values, (list, tuple, set, frozenset)):
        return allowed
    for value in values:
        text = str(value or "")
        if ":" not in text:
            continue
        plat, uid = text.split(":", 1)
        scoped = _scoped_user_id(plat, uid)
        if scoped:
            allowed.add(scoped)
    return allowed


_DIGITS_RE = re.compile(r"\d{5,}")


def _admin_ids(values: Any) -> set[str]:
    """管理员名单 → 纯数字 QQ 号集合。

    面板上直接填数字就行；顺手写成 ``qq:123456``、``123456@qq.com`` 的也认，
    免得从别处抄来的格式被静默无视。取不出数字的条目直接丢掉。
    """
    found: set[str] = set()
    if not isinstance(values, (list, tuple, set, frozenset)):
        return found
    for value in values:
        text = "".join(
            chr(ord(ch) - 0xFEE0) if "\uff10" <= ch <= "\uff19" else ch for ch in str(value or "")
        )
        found.update(_DIGITS_RE.findall(text))
    return found


# 群名单的两种模式。默认白名单，且默认名单为空 —— 也就是**默认哪个群都不发**，
# 要用户自己把群号填进来才开始工作。
_GROUP_ALLOW = "白名单"
_GROUP_DENY = "黑名单"
# 老配置里存过 allow/deny 和 whitelist/blacklist（取值换过两次）。
# 迁移函数会把它们搬成中文，但万一有实例没走到迁移
# （比如用户手改过 config.toml），这里也认旧词 —— 认错方向的后果是
# 黑名单被当成白名单，从「只发这几个群」变成「除了这几个全发」。
_GROUP_ALIASES = {
    "allow": _GROUP_ALLOW,
    "deny": _GROUP_DENY,
    "whitelist": _GROUP_ALLOW,
    "blacklist": _GROUP_DENY,
}


def _group_set(values: Any) -> set[str]:
    """群号名单 → 纯数字集合；形状不对就当空集。"""
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    return {_clean_id(value) for value in values if _clean_id(value)}


def _pick_live_session(items: list[dict[str, Any]]) -> dict[str, Any]:
    """同一群的几条会话记录里，挑最可能是「能用」的那一条。

    宿主的会话表里偶尔对同一个群留下两行，其中一行是没归好档的孤儿行。行里能看出
    死活的线索是 ``account_id`` / ``user_id`` 以及有没有群名 —— 有账号能说明这条
    会话绑着某个已登录的账号，孤儿行这些字段往往是空的。挑不出来（全都一样、
    或者线索字段宿主根本没给）就退回第一条，保持原来的行为。
    """
    def score(item: dict[str, Any]) -> int:
        total = 0
        for key in ("account_id", "user_id", "group_name", "platform"):
            if str(item.get(key) or "").strip():
                total += 1
        return total

    return max(items, key=score)


def _clean_id(value: Any) -> str:
    """把群号洗成纯数字串。用户可能填「群 123456」或者带空格的全角数字。"""
    text = str(value or "").strip()
    if not text:
        return ""
    text = "".join(chr(ord(ch) - 0xFEE0) if "\uff10" <= ch <= "\uff19" else ch for ch in text)
    digits = re.sub(r"\D", "", text)
    return digits


def _parse_hhmm(text: Any) -> tuple[int, int] | None:
    """把 9:30 / 09：30 / 21点 解析成 (时, 分)。读不出来返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    raw = "".join(chr(ord(ch) - 0xFEE0) if "\uff10" <= ch <= "\uff19" else ch for ch in raw)
    for token in ("：", ".", "点", "时", "時", "h", "H"):
        raw = raw.replace(token, ":")
    # 「21点」「9:」这类写法收尾会留一个冒号，去掉再解析
    raw = raw.strip().strip(":").strip()
    matched = re.match(r"^(\d{1,2})(?::(\d{1,2}))?$", raw)
    if not matched:
        return None
    hour = int(matched.group(1))
    minute = int(matched.group(2) or 0)
    # 24:00 是「今天结束」的常见写法，当成次日零点（也就是 0:00）。只放行 24:00 这一个，
    # 24:30 之类仍然算错，否则「当天最后一个时刻」就没有明确的说法了。
    if hour == 24 and minute == 0:
        return 0, 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def _bad_daily_times(values: Any) -> list[str]:
    """挑出 daily_times 里认不出来的写法，好让用户知道哪条没生效。

    不这么报的话，「24:30」「明天早上」这类填错会被静默丢掉：定时照跑，
    但跑的时刻跟用户想的不一样，而且没有任何提示。
    """
    out: list[str] = []
    for item in values or []:
        text = str(item or "").strip()
        if text and _parse_hhmm(text) is None:
            out.append(text)
    return out


def _image_size(raw: bytes) -> tuple[int, int]:
    """量一下图片尺寸，用来估长图高度。量不出来返回 (0, 0)。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            return image.size
    except Exception:  # noqa: BLE001
        return (0, 0)


def _card_style(cfg: Any) -> str:
    """读长图样式。认不出来（比如配置被手改坏了）就按自绘卡片走。"""
    if str(getattr(cfg, "card_style", "") or "") == _CARD_STYLE_PAGE:
        return _CARD_STYLE_PAGE
    return _CARD_STYLE_CARD


def _page_render(cfg: Any) -> str:
    """读「贴吧原页面」的渲染方式。认不出来就按零外链走（对主程序版本无要求）。"""
    if str(getattr(cfg, "page_render", "") or "") == _PAGE_RENDER_RAW:
        return _PAGE_RENDER_RAW
    return _PAGE_RENDER_ZERO


def _select_floor_images(
    floors: Sequence[Any], available: Any, limit: int
) -> list[list[str]]:
    """按楼层顺序挑出这次真正会画的图，总共不超过 limit 张。"""
    if limit <= 0:
        return [[] for _ in floors]
    left = int(limit)
    picked: list[list[str]] = []
    for floor in floors:
        urls: list[str] = []
        for url in list(getattr(floor, "images", None) or []):
            if left <= 0:
                break
            if url in available and url not in urls:
                urls.append(url)
                left -= 1
        picked.append(urls)
    return picked


def _seg_images(
    chunk: Sequence[Any], floors: Sequence[Any], floor_images: Sequence[Any], available: Any
) -> dict[str, str]:
    """取出这一段要画的图，同一张图只出现在一段里。"""
    order = {id(floor): index for index, floor in enumerate(floors)}
    images: dict[str, str] = {}
    for floor in chunk:
        index = order.get(id(floor))
        if index is None:
            continue
        for url in floor_images[index]:
            if url in available:
                images[url] = available[url]
    return images


def _plan_card_chunks(
    floors: Sequence[Any],
    sizes: dict[str, tuple[int, int]],
    width: int,
    floor_images: Sequence[Any] | None = None,
) -> list[list[Any]]:
    """把楼层切成若干段，保证每段渲出来的长图不会高到截不下来。

    这只是估算：正文按字数折行，图片按卡片宽等比缩放后的真实比例算 ——
    估高了最多多切一图，估低了才会渲染失败，所以宁可保守。
    传了 floor_images 就只算这次真会画的图，没下载到的图不占高度。
    """
    chunks: list[list[Any]] = []
    current: list[Any] = []
    height = _HEAD_HEIGHT
    for position, floor in enumerate(floors):
        text = str(getattr(floor, "text", "") or "")
        lines = max(1.0, len(text) / _TEXT_CHARS_PER_LINE + text.count("\n"))
        height += _FLOOR_PADDING + lines * _TEXT_LINE_HEIGHT
        if floor_images is None:
            urls = list(getattr(floor, "images", None) or [])
        else:
            urls = list(floor_images[position]) if position < len(floor_images) else []
        for url in urls:
            size = sizes.get(url)
            if size and size[0]:
                height += width * size[1] / size[0] + 14.0
            else:
                height += _DEFAULT_IMAGE_HEIGHT
        current.append(floor)
        if height >= _MAX_CARD_HEIGHT:
            chunks.append(current)
            current = []
            height = _HEAD_HEIGHT
    if current:
        chunks.append(current)
    return chunks


class _ToolOptions:
    """工具那一轮用的发送选项：工具节有的取工具节，没有的回落到「发送」一节。

    _send_options() 的调用点都只按属性名取值（post_link / max_images …），
    这里把两个 section 拼成一层属性视图就够了，不必动那些调用点。
    """

    __slots__ = ("_tool", "_send")

    def __init__(self, config: Any) -> None:
        object.__setattr__(self, "_tool", getattr(config, "tool", None))
        object.__setattr__(self, "_send", getattr(config, "send", None))

    def __getattr__(self, name: str) -> Any:
        tool = object.__getattribute__(self, "_tool")
        if tool is not None and hasattr(tool, name):
            value = getattr(tool, name)
            if value is not None:
                return value
        send = object.__getattribute__(self, "_send")
        return getattr(send, name)


class _SendGate:
    """发送侧的令牌桶 + 失败重试。

    职责单一：把「发一条消息」包成「限速地发、失败了退避重试几次」。
    插件里有四类发送（文字、图片、custom 段、合并转发），各有各的签名，
    所以这里不接具体发送函数，只提供 wait 和 run：

    * wait —— 从桶里取一个令牌，取不到就睡到有为止；
    * run —— 调一次真的发送，失败就退避重试（失败判定见 run 的 docstring）。

    SDK 把发送能力的返回值归一成 ``bool``：``True`` 是发出去了，
    ``False`` / ``None`` 都是没发成。

    **但「没发成」并不等于「没送到」** —— 宿主的判定只是「RPC 这一趟拿到了
    返回值」，协议端真投没投出去它不知道。所以重试是把双刃剑：抖动时救命，
    超时时制造重复。默认给 3 次（单条消息重了只是多一句话），而合并转发这类
    「一次顶一整轮、最慢最容易超时」的调用按次传 ``_retries=0``。
    """

    __slots__ = ("_tokens", "_at", "_rate", "_burst", "_retries", "_base")

    def __init__(
        self,
        *,
        rate: float = _SEND_RATE_PER_SECOND,
        burst: float = _SEND_BURST,
        retries: int = _SEND_RETRY_TIMES,
        base_delay: float = _SEND_RETRY_BASE_S,
    ) -> None:
        self._rate = max(0.01, float(rate))
        self._burst = max(1.0, float(burst))
        self._tokens = self._burst
        self._at = time.monotonic()
        self._retries = max(0, int(retries))
        self._base = max(0.0, float(base_delay))

    async def wait(self) -> None:
        """取一个令牌；桶空就睡到下一个令牌补上。"""
        while True:
            now = time.monotonic()
            self._tokens = min(self._burst, self._tokens + (now - self._at) * self._rate)
            self._at = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            await asyncio.sleep((1.0 - self._tokens) / self._rate)

    async def run(
        self, send: Any, *args: Any, _retries: int | None = None, **kwargs: Any
    ) -> bool:
        """限速发一条消息，失败退避重试。返回是否发出去了。

        **什么算失败**：SDK 把发送能力的返回值归一成 ``bool``
        （``maibot_sdk/context.py:294``，``send.text``/``send.image``/``send.custom``/
        ``send.forward`` 都在 ``_BOOLEAN_SUCCESS_CAPABILITIES`` 里），
        依据是宿主 ``_build_send_result`` 的 ``success = sent_message is not None``。
        所以：

        * ``True``  —— 发出去了；
        * ``False`` —— **宿主明确说没发成**（缺参数、抛异常、平台拒绝）；
        * ``None``  —— 理论上不该出现（能力没被归一），按失败处理。

        以前这里写的是 ``ok is not None``，等于把 ``False`` 当成成功，
        「发失败就退而发链接」的兜底逻辑整个失效 —— 用户看到的是视频凭空消失。
        网关/适配器临时抽风返回 False 时重试一次往往就好了，这也是要重试的理由。

        ``_retries`` 只覆盖**这一次调用**的重试次数（``0`` = 不重试），不传就用
        建这个闸门时定的默认值。合并转发一律传 ``_FORWARD_RETRY_TIMES = 0``：
        它最慢、最容易超时，而超时多半意味着「其实已经发出去了」，重试一次
        群里就多一条一模一样的聊天记录。名字带下划线是为了不和转给 ``send``
        的参数撞。

        Args:
            send: 真的发送函数（self.ctx.send.text 之类）。
            *args: 原样转给 send。
            _retries: 只覆盖这一次调用的重试次数；None 用闸门默认值。
            **kwargs: 原样转给 send。

        Returns:
            发出去了是 True；重试用光还是没发成是 False。
        """
        limit = self._retries if _retries is None else max(0, int(_retries))
        for attempt in range(limit + 1):
            await self.wait()
            try:
                ok = await send(*args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # 异常不往上抛：一轮搬运几十条消息，一条发不出去
                # 不该让整轮崩掉、把后面的帖子全丢了。
                ok = False
            if ok:
                return True
            if attempt >= limit:
                return False
            # 指数退避：1s、2s、4s…，加抖动免得定时任务撞在一起。
            delay = self._base * (2 ** attempt) * random.uniform(0.8, 1.2)
            await asyncio.sleep(delay)
        return False


class _RoundSummary(str):
    """一轮搬石的结果。

    **是 ``str`` 的子类**：摘要要写进日志、要能被直接打印、探针本来也按字符串
    比对，所以本体必须还是字符串；「该不该发到群里」另由 ``error`` 表达。

    只有出错才值得说话。成功是常态 —— 帖子本身已经发进群了，再补一句
    「本轮搬了 N 条」没有任何信息量，只是刷屏。
    """

    __slots__ = ("error",)

    def __new__(cls, text: str, *, error: bool = False) -> "_RoundSummary":
        obj = super().__new__(cls, text)
        obj.error = error
        return obj


class StonePlugin(MaiBotPlugin):
    """麦麦贴吧搬石插件。"""

    config_model = StoneConfig

    # 兜底初值。宿主总是先走 on_load，但这几行要留着：测试桩会直接 new 一个
    # 实例去调 on_config_update / on_unload，没有它们就是 AttributeError。
    _client: Any = None
    _proto: Any = None
    _block: Any = None
    # 这两项也要有类级兜底：_send_options / _filter_mode 会在任何一次发送里被读到，
    # 缺了就是 AttributeError，而它们只由 on_load 建立。
    _kind_override: str = ""
    _tool_round: bool = False
    # 本轮「贴吧原页面」模式是否已判定渲不出来（见 :meth:`_card_images`）。
    # 每轮开头复位，所以宿主修好、网络变好之后下一轮自己就会恢复。
    _page_mode_down: bool = False

    # -- 生命周期 ---------------------------------------------------------

    async def on_load(self) -> None:
        self._client: StoneTiebaClient | None = None
        # protobuf 抓取层：跟 HTML 路线并存。它负责 HTML 拿不到的东西 ——
        # 帖子点赞数、真翻页、楼中楼正文。
        self._proto: StoneProtoClient | None = None
        self._block: BlockState | None = None
        # 发送侧的令牌桶 + 重试。所有对外发送都过它，别绕过。
        self._gate: _SendGate = _SendGate()
        self._store: StoneStore | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # 手动轮次在后台跑，这里留着任务本身，卸载时好一起收掉
        self._jobs: set[asyncio.Task] = set()
        # 「现在有一轮在跑」。不能只看 _lock：后台任务从派发到真正拿到锁之间
        # 还有一小段空窗，这期间第二个人敲命令会以为没人跑，于是两轮并行。
        self._busy = False
        # 同一条消息 id 只认一次：引用/重投递都可能让宿主把同一条命令再喂进来
        self._seen_message_ids: deque[str] = deque(maxlen=64)
        # 工具调用期间临时指定的「只搬哪种帖」；空串表示照配置走
        self._kind_override: str = ""
        # 本轮「贴吧原页面」是否已判定渲不出来，见 :meth:`_card_images`
        self._page_mode_down: bool = False
        # 本轮是不是模型调工具发起的那一轮：是则发送选项改读配置的「工具」一节
        self._tool_round: bool = False
        # 上次真正扫过落盘目录的时刻，用来节流清理（落盘很频繁，扫目录很贵）
        self._page_cache_pruned_at: float = 0.0
        self._setup()
        cfg = self.config
        self.ctx.logger.info(
            "[搬石] 已加载 v%s：吧=%s；自动搬石=%s；呈现=%s",
            _PLUGIN_VERSION,
            "、".join(resolve_forums(cfg.stone)) or "（没配）",
            "开" if cfg.schedule.auto_enabled else "关",
            self._mode_label(),
        )
        self._maybe_start_loop()

    async def on_unload(self) -> None:
        await self._cancel_loop()
        await self._cancel_jobs()
        if self._store is not None:
            # 记录是纯本机去重状态，卸载前落一次盘
            self._store.flush()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._proto is not None:
            await self._proto.aclose()
            self._proto = None
        self.ctx.logger.info("[搬石] 已卸载")

    def normalize_plugin_config(self, config_data):
        # 面板默认值改过之后，老 config.toml 里那个键还留着旧默认（比如空的屏蔽词列表），
        # 而宿主补齐/归一化都是「键存在就用旧值」，于是新默认永远盖不上去。
        # 先按迁移表把「没动过」的字段换成新默认，再走 SDK 原本的校验补齐。
        changed = migrate_config(config_data) if isinstance(config_data, dict) else False
        normalized, auto_changed = super().normalize_plugin_config(config_data)
        return normalized, bool(changed or auto_changed)

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        del config_data
        if scope != "self":
            return
        # 改配置时正在跑的手动/工具轮次不在 self._task 里（那个只装定时任务），
        # 直接 aclose 会让它们抛「client has been closed」并且用户看不到任何反应。
        # 拿住同一把锁就等于「等这一轮跑完再换 client」；_cancel_loop 也放进锁内，
        # 免得它取消定时任务的同时那一轮正用着 client。
        async with self._lock:
            await self._cancel_loop()
            if self._client is not None:
                await self._client.aclose()
                self._client = None
            if self._proto is not None:
                await self._proto.aclose()
                self._proto = None
            self._setup()
            self._maybe_start_loop()
        self.ctx.logger.info("[搬石] 配置已更新（version=%s），运行时已重建", version)

    # -- 装配 -------------------------------------------------------------

    def _data_dir(self) -> Path:
        try:
            base = Path(self.ctx.paths.data_dir)
        except Exception:  # noqa: BLE001 —— 老版本 SDK 没有 paths
            base = _PLUGIN_DIR / "data"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _page_cache_dir(self) -> Path:
        """长图落盘目录。转发节点里放的是这里的 file:// 路径。"""
        base = self._data_dir() / "pages"
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _save_page_image(self, image_b64: str) -> str:
        """把渲染好的长图写进落盘目录，返回 ``file:///`` 形式的绝对路径。

        写盘失败时返回空串，调用方据此退回 base64 —— 路径引用只是「省帧体积」，
        不该因为磁盘只读之类的小事让整条帖子发不出去。
        """
        return self._save_blob_image(image_b64, log_failure=True)

    def _save_blob_image(self, image_b64: str, *, log_failure: bool = False) -> str:
        """把一段 base64 图片写盘并返回 ``file:///`` 路径；失败返回空串。

        正文小图用同一个目录：它们和长图一样都只是「给协议端读的临时文件」，
        按同一个份数上限一起清理即可。
        """
        try:
            data = base64.b64decode(image_b64)
        except Exception:  # noqa: BLE001
            return ""
        if not (data.startswith(_PNG_MAGIC) or data.startswith(_JPEG_MAGIC)):
            # 不是能认的图片就别往盘上写：省得 pages/ 里堆一批永远读不出来的东西
            if log_failure:
                self.ctx.logger.warning(
                    "[搬石] 渲染结果不是有效图片（%s 字节，开头 %r），这条改用内嵌图片",
                    len(data),
                    data[:8],
                )
            return ""
        try:
            directory = self._page_cache_dir()
            path = directory / (_PAGE_CACHE_FILENAME % (uuid.uuid4().hex, int(time.time())))
            path.write_bytes(data)
        except Exception as exc:  # noqa: BLE001
            if log_failure:
                self.ctx.logger.warning("[搬石] 长图落盘失败，这条改用内嵌图片：%s", exc)
            return ""
        self._prune_page_cache()
        return _file_uri(path)

    def _prune_page_cache(self, *, keep: int | None = None) -> None:
        """只留最近 ``keep`` 张落盘图片，多出来的按修改时间从旧到新删掉。

        两个约束：
        * ``_PAGE_CACHE_CLEAN_DELAY_S`` 之内的文件一律不碰 —— 协议端可能正读着；
        * 每 ``_PAGE_CACHE_PRUNE_INTERVAL_S`` 秒最多真扫一次目录，否则一轮上百张
          小图会把同步 stat 打成几万次，白堵事件循环。
        """
        limit = _PAGE_CACHE_KEEP if keep is None else keep
        now = time.time()
        # getattr 兜底：清理只是维护动作，不该因为实例状态缺失把整轮搬运带崩
        last = float(getattr(self, "_page_cache_pruned_at", 0.0) or 0.0)
        if now - last < _PAGE_CACHE_PRUNE_INTERVAL_S:
            return
        self._page_cache_pruned_at = now
        try:
            files = sorted(
                self._page_cache_dir().glob("page_*.png"),
                key=lambda item: item.stat().st_mtime,
            )
        except Exception:  # noqa: BLE001
            return
        # 保护期内（刚落盘、协议端可能还没读完）的一律留下，不参与淘汰。
        files = [item for item in files if now - item.stat().st_mtime >= _PAGE_CACHE_CLEAN_DELAY_S]
        for stale in files[: max(0, len(files) - limit)]:
            try:
                stale.unlink()
            except Exception:  # noqa: BLE001
                pass

    def _setup(self) -> None:
        cfg = self.config
        # 反爬冷却由两个客户端共用：拦的是这台机器，不是某个接口。
        # 分成两份的话，HTML 那边被拦了，protobuf 这边还会接着敲，越敲越死。
        block = BlockState()
        self._block = block
        self._client = StoneTiebaClient(
            timeout=float(cfg.fetch.timeout),
            proxy=str(cfg.fetch.proxy or "").strip(),
            delay_seconds=float(cfg.fetch.delay_seconds),
            retries=2,
            user_agent=str(cfg.fetch.user_agent or "").strip(),
            shared_block=block,
        )
        self._proto = StoneProtoClient(
            timeout=float(cfg.fetch.timeout),
            proxy=str(cfg.fetch.proxy or "").strip(),
            delay_seconds=float(cfg.fetch.delay_seconds),
            retries=2,
            shared_block=block,
        )
        self._store = StoneStore(
            self._data_dir() / "moved.json", keep_per_forum=int(cfg.stone.keep_records)
        )
        self._store.load()

    def _gate_of(self) -> _SendGate:
        """拿发送闸门；懒建是为了让没走 on_load 的测试桩也能用。"""
        gate = getattr(self, "_gate", None)
        if gate is None:
            gate = _SendGate()
            self._gate = gate
        return gate

    def _mode_label(self) -> str:
        cfg = self.config.send
        # 长图是唯一的内容形态，没有开关 —— 关掉它就没有任何东西可发了。
        if _card_style(cfg) == _CARD_STYLE_PAGE:
            label = "长图（贴吧原页面·%s）" % _page_render(cfg)
        else:
            label = "长图（自绘卡片）"
        if cfg.forward:
            label += "（打包成聊天记录）"
        return label

    # -- 定时 -------------------------------------------------------------

    def _maybe_start_loop(self) -> None:
        cfg = self.config
        if not cfg.plugin.enabled:
            self.ctx.logger.info("[搬石] 配置里没启用插件，不启动定时")
            return
        if not cfg.schedule.auto_enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._schedule_loop())

    async def _cancel_loop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("[搬石] 定时任务退出时抛出：%s", exc)

    async def _cancel_jobs(self) -> None:
        """收掉所有还在跑的手动/工具轮次。"""
        jobs = list(self._jobs)
        self._jobs.clear()
        for job in jobs:
            if not job.done():
                job.cancel()
        for job in jobs:
            try:
                await job
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.debug("[搬石] 后台轮次退出时抛出：%s", exc)

    def _spawn_job(self, coro: Any) -> None:
        """把一轮搬运丢到后台跑，命令本身立刻回执。

        宿主对命令有 60 秒超时，而一轮搬运动辄一分多钟，同步等必然被截断 ——
        用户那边只看得到超时，搬运却在后台继续，看起来就像「说不认识却搬了」。
        """
        job = asyncio.create_task(coro)
        self._jobs.add(job)

        def _done(_task: asyncio.Task) -> None:
            self._jobs.discard(_task)
            self._busy = False

        job.add_done_callback(_done)

    # 定点「已经跑过」的判定容差。宿主里 asyncio.sleep 只保证「至少」睡够，
    # 实测普遍多睡 0.5~2 毫秒，所以醒来时 now 几乎必然已经越过定点。判定必须
    # 容忍这点漂移，否则同一轮会被当成「还没到」而顺延到明天（见 _next_fire）。
    _FIRE_TOLERANCE = timedelta(seconds=90)

    def _next_fire(self, *, after: datetime | None = None) -> datetime | None:
        """按「每天定点」算出的下一个触发时刻；没配定点就返回 None。

        ``after`` 是「已经跑到过的那一刻」。**判定不能写成 `target < now`**：
        以前就是那么写的，而 `asyncio.sleep` 只保证至少睡够，醒来时 now 比定点
        晚个一两毫秒是常态 —— 那一毫秒让 `target < now` 成立，目标被顺延到明天，
        这个「明天」又被记进 ``fired``、接着再被 ``skip`` 顶掉一天，于是变成
        **两天才跑一次**。用户看到的就是「不是设定的时间」。

        另一条同样要命的旧写法是「今天这个点已经过去就算命中」：插件 20:00 启动、
        配了 08:30/09:30/12:00/20:00 时，前三个点当天都已过去，于是连着补跑好几
        轮 —— 用户看到的就是「一次连续发两次」。所以这里**只认还没到的时刻**。

        现在只拿 ``after`` 判断「跑过没有」，完全不看 now 与定点的先后，采样漂移
        再也影响不到「算出来的是哪一天」。
        """
        cfg = self.config.schedule
        stamps = [parsed for parsed in (_parse_hhmm(x) for x in cfg.daily_times) if parsed]
        if not stamps:
            return None
        now = datetime.now()
        best: datetime | None = None
        for hour, minute in stamps:
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if after is not None and target - after <= self._FIRE_TOLERANCE:
                # 今天这个点这一轮已经跑过了，挪到明天。
                target = target + timedelta(days=1)
            elif after is None and target <= now:
                # 还没跑过任何一轮，而今天这个点已经过去了：别再补跑，
                # 直接等明天。第一次算的时候连补好几轮正是「一次连发两次」。
                target = target + timedelta(days=1)
            if best is None or target < best:
                best = target
        return best

    def _next_wait(self, *, after: datetime | None = None) -> float:
        """下一次该等多久（秒）。

        定点模式返回「到那个时刻还差几秒」，间隔模式返回一个完整间隔。

        **不要在这里设 30 秒之类的兜底下限**：以前写成 `max(30.0, 差值)`，
        于是在定点前 30 秒内计算时（比如 20:59:45 算 21:00），差值被抬到 30 秒，
        实际到 21:00:15 才跑 —— 用户看到的就是「不是设定的时间」。下限只该
        用来挡住「差值为 0 或负数」这种退化情形。
        """
        target = self._next_fire(after=after)
        if target is not None:
            return max(0.0, (target - datetime.now()).total_seconds())
        now = datetime.now()
        return max(0.0, (self._interval_target(after=now) - now).total_seconds())

    def _last_baseline(self, *, before: datetime) -> datetime:
        """间隔模式的「上一个基准时刻」，周期就是从这里开始数的。

        基准时刻（默认 0:00）决定周期落在哪些点上：配 0:00 + 120 分钟，就是
        0、2、4……22 点各一次。以前是从「插件启动的那一刻」起算，所以「每 60
        分钟」实际是「启动后每 60 分钟」，跟整点对不上 —— 用户看到的就是
        「不是设定的时间」，而且每次重启都会再漂一次。读不出基准就退回把
        before 当基准（等价于旧行为），不因为一个笔误把定时整个停掉。
        """
        parsed = _parse_hhmm(getattr(self.config.schedule, "base_time", ""))
        if parsed is None:
            return before
        hour, minute = parsed
        base = before.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if base > before:
            base = base - timedelta(days=1)
        return base

    def _interval_target(self, *, after: datetime) -> datetime:
        """间隔模式下 after 之后的下一个时间点（对齐基准时刻）。"""
        interval = max(60.0, float(self.config.schedule.interval_minutes) * 60.0)
        base = self._last_baseline(before=after)
        elapsed = (after - base).total_seconds()
        steps = int(elapsed // interval) + 1
        return base + timedelta(seconds=steps * interval)

    def _lead_seconds(self) -> float:
        """「提前开始」的秒数，只认 0~3600 秒；读不出来的值按 0 处理。"""
        raw = getattr(self.config.schedule, "lead_seconds", 0)
        try:
            seconds = int(raw)
        except (TypeError, ValueError):
            return 0.0
        if seconds <= 0:
            return 0.0
        return float(min(seconds, 3600))

    def _interval_wait(self, started_at: float) -> float:
        """间隔模式的下一次等待：从**上一轮开始跑的时刻**起算。

        若写成「跑完再等一个完整间隔」，实际周期就是「间隔 + 一轮耗时」：
        实测配 60 分钟却每隔 61 分钟跑一次（00:18 → 01:19 → 02:20，每轮约一分钟）。
        扣掉已经花掉的时间，周期才真的等于配置值；一轮要是跑超了一个间隔，
        就直接安排下一轮（下限 0），不追赶、不补跑。
        """
        interval = max(60.0, float(self.config.schedule.interval_minutes) * 60.0)
        return max(0.0, interval - (time.monotonic() - started_at))

    async def _schedule_loop(self) -> None:
        # 刚启动就先等一个完整间隔（设了指定时刻就等到那个点），别一开机就搬一轮。
        #
        # 关键：本轮的 ``target`` 必须在**睡之前**就定下来，睡醒后绝不重算。
        # 以前是睡醒后才第一次采样 now 去算 target，而 sleep 只保证至少睡够、
        # 醒来时往往已过定点一两毫秒 —— 那一毫秒让目标被顺延到明天，接着再被
        # skip 顶掉一天，于是两天才跑一次。
        fired: datetime | None = None
        # 间隔模式下「这一轮距离开跑还有多久（秒）」，在睡之前就算好。None 表示还
        # 没算过，下一轮该在哪个时刻开跑要重新按「基准时刻」推。
        #
        # 必须用变量把它带过一轮：以前是在循环**末尾**调 _interval_wait 算出
        # 「扣掉本轮耗时后还要等多久」，赋值给 wait 之后循环立刻回到顶部，又被
        # 顶部的完整间隔覆盖 —— 白算。于是实际周期 = 间隔 + 一轮耗时，实测配
        # 3600 秒却每隔约 3860 秒才跑一次，且误差随每轮耗时累积。
        pending: float | None = None
        while True:
            # 这一轮该在哪个绝对时刻发布；None 表示取完帖子就直接发。
            post_at: datetime | None = None
            lead = self._lead_seconds()
            fire = self._next_fire(after=fired)
            fixed = fire is not None
            if fixed:
                # 定点模式：直接对着那个时刻。提前量只是让「取帖」先开始，
                # 发布仍然卡在用户设的整点上。
                fired = fire
                # 切回定点后，间隔模式留下的「待跑时间」作废，免得跨模式生效。
                pending = None
                left = (fire - datetime.now()).total_seconds()
                if lead and left > lead:
                    # 先去贴吧把帖子取好（贴吧慢的时候这点时间正好用上），到了
                    # 到点再发出去 —— 这样「发」的时刻始终是准的，取帖快慢不影响。
                    wait = max(0.0, left - lead)
                    post_at = fire
                else:
                    wait = max(0.0, left)
            elif pending is not None:
                # 间隔模式，第二轮起：等待时间上一轮末尾就算好了（扣掉了本轮耗时），
                # 开跑后再过提前量就是发布时刻 —— 提前量只决定「什么时候发」，
                # 不改变开跑间隔，周期始终等于「隔多少分钟搬一次」。
                wait = pending
                pending = None
                if lead:
                    post_at = datetime.now() + timedelta(seconds=wait + lead)
            else:
                # 间隔模式，第一轮：按「基准时刻」推出下一个基准整点，再往前挪一个
                # 提前量当开跑时刻 —— 于是每一轮的发布时刻都精确落在基准网格上
                # （0:00 + 120 分钟就是 0、2、4……22 点整），既不改周期，也不让
                # 发布时刻随取帖快慢漂移。now 只采一次，免得 int(wait) 少 1 秒。
                now = datetime.now()
                fire = self._interval_target(after=now + timedelta(seconds=lead))
                wait = max(0.0, (fire - now).total_seconds() - lead)
                if lead:
                    post_at = fire
            self.ctx.logger.debug("[搬石] 下一次搬运在 %s 秒后", int(wait))
            try:
                await asyncio.sleep(wait)
            except asyncio.CancelledError:
                raise
            started_at = time.monotonic()
            # 免打扰时段：跳过这一轮，不补发。排期本身照旧往下走 —— 醒来时若还在
            # 时段内就整轮丢掉，等下一个网格点；醒来已在时段外就正常搬。
            if in_quiet_hours(self.config.schedule):
                self.ctx.logger.info(
                    "[搬石] 当前在免打扰时段（%s~%s），跳过本轮",
                    self.config.schedule.quiet_start,
                    self.config.schedule.quiet_end,
                )
                if not fixed:
                    pending = self._interval_wait(started_at)
                continue
            try:
                if post_at is not None:
                    self.ctx.logger.info(
                        "[搬石] 提前开跑，%s 再发", post_at.strftime("%m-%d %H:%M")
                    )
                summary = await self._run_round("定时", post_at=post_at)
                self.ctx.logger.info("[搬石] 定时一轮结束：%s", summary)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[搬石] 定时一轮失败：%s", exc, exc_info=True)
            # 定点模式按绝对时刻算，下一轮的 target 在循环开头重新求；间隔模式
            # 要扣掉这一轮已经花掉的时间，否则周期会越拖越长。
            if not fixed:
                pending = self._interval_wait(started_at)

    # -- 一轮搬运 ---------------------------------------------------------

    async def _run_round(
        self,
        trigger: str,
        streams: list[str] | None = None,
        *,
        only_forums: Sequence[str] | None = None,
        count: int = 0,
        only_kind: str = "",
        post_at: datetime | None = None,
        tool_options: bool = False,
    ) -> str:
        """搬一轮。streams 给了就只发这些会话（手动模式），否则按群配置发。

        only_forums / count / only_kind 是工具调用时临时改「逛哪些吧、每个吧
        搬几条、只要哪种帖」用的。tool_options 表示这一轮由模型调工具发起，
        发送相关选项（带不带原帖链接、长图最多几张图、只搬哪种帖）改读配置的
        「工具」一节，其余照「发送」一节。
        """
        # 不能写成 if self._lock.locked(): return —— 检查和获取之间会让出控制权，
        # 两个调用可能同时通过检查，于是并行搬两轮。直接尝试获取才是原子的。
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=0.01)
        except (asyncio.TimeoutError, TimeoutError):
            return _RoundSummary("上一轮还没跑完，这次跳过")
        try:
            cfg = self.config
            client = self._client
            store = self._store
            # 先装覆盖再判前置条件：下面任何一个提前 return 都不会走到 finally，
            # 漏装的话上一轮的「只发视频帖」会跟着漏到下一轮去。
            self._kind_override = only_kind or ""
            self._tool_round = bool(tool_options)
            # 每轮重新给「贴吧原页面」一次机会：宿主升级了 playwright、网络变好了，
            # 下一轮就该自己恢复成原页面，不必重启插件。
            self._page_mode_down = False
            try:
                if client is None or store is None:
                    return _RoundSummary("插件还没准备好，稍后再试", error=True)
                if only_forums:
                    forums = [
                        str(name).strip() for name in only_forums if str(name or "").strip()
                    ]
                else:
                    forums = resolve_forums(cfg.stone)
                if not forums:
                    return _RoundSummary("配置里没勾也没填要逛的吧", error=True)
                per_forum = int(count) if int(count) > 0 else int(cfg.stone.count_per_forum)
                return await self._run_locked(
                    cfg, client, store, forums, per_forum, streams, trigger, post_at
                )
            finally:
                self._kind_override = ""
                self._tool_round = False
        finally:
            self._lock.release()

    async def _run_locked(
        self,
        cfg: Any,
        client: Any,
        store: Any,
        forums: Sequence[str],
        per_forum: int,
        streams: list[str] | None,
        trigger: str,
        post_at: datetime | None = None,
    ) -> str:
        """_run_round 的主体：进来时一定已经拿住锁。"""
        # collected 里每项带上「属于哪个吧」，发送成功后才好按吧记账
        collected: list[tuple[Any, Any]] = []
        forum_of: dict[str, str] = {}
        # 长图没能渲出来、因而一条都没发出去的帖子。它们**不能**被记成
        # 「已搬过」—— 记了就等于永久丢帖（渲染失败多半只是临时抽风，
        # 下一轮多半就好了）。
        skipped_tids: set[str] = set()
        blocked = ""
        proto = getattr(self, "_proto", None)
        for forum in forums:
            threads, blocked = await self._load_threads(
                forum, client=client, proto=proto, pages=int(cfg.fetch.max_pages)
            )
            if blocked:
                break
            if not threads:
                continue
            want = int(per_forum) if int(per_forum) > 0 else int(cfg.stone.count_per_forum)
            picks = self._pick_threads(forum, threads, limit=per_forum)
            # picks 里是「候选」（筛选模式下会多取几倍，用来熬过列表页标记看走眼），
            # 这里才真正数到 want 条为止：复核通过的凑够数就停，剩下的候选连详情页
            # 都不用抓。计数按吧分别算，collected 是各吧汇总的。
            got = 0
            for thread in picks:
                if want > 0 and got >= want:
                    break
                detail, blocked = await self._load_detail(
                    thread,
                    client=client,
                    proto=proto,
                    max_floors=int(cfg.fetch.max_floors),
                    max_chars=int(cfg.fetch.max_chars),
                    max_comments=int(getattr(cfg.fetch, "max_comments", 3) or 0),
                )
                if detail is None:
                    # 这一帖彻底没拿到。被拦的话就到此为止，别再敲了。
                    if blocked:
                        break
                    continue
                # **先收下手里这帖，再看要不要停**：protobuf 翻到第 2 页才被拦时
                # detail 是有效的（网页版那份），先 break 会把刚抓到的东西扔掉。
                if not detail.floors:
                    self.ctx.logger.debug("[搬石] 帖子 %s 没有解析到楼层，跳过", thread.tid)
                elif not self._passes_filter(detail):
                    self.ctx.logger.debug(
                        "[搬石] 帖子 %s 不符合「%s」，跳过",
                        thread.tid,
                        self._filter_mode(),
                    )
                    # 筛选掉的和发送无关，现在就能记账，不用等投递
                    store.mark(forum, thread.tid, thread.title)
                else:
                    collected.append((thread, detail))
                    forum_of[thread.tid] = forum
                    got += 1
                if blocked:
                    break
            store.mark_run(forum)
            if blocked:
                break

        delivered = 0
        # 真正落到群里的帖子数（不是「挑了几条」）。摘要按这个说，见下面的说明。
        landed_posts = 0
        failed = 0
        host_error = ""
        # 真正收到帖子的会话。结束语只往这些会话补 —— 一句都没发出去的群
        # 再来一句「这是我今天发现的帖子」只会让人莫名其妙。
        ok_streams: list[str] = []
        if collected:
            if streams is not None:
                targets = streams
            else:
                targets, host_error = await self._target_streams()
            payloads = await self._prepare_payloads(collected)
            # 「提前开始」模式下帖子已经取好了，这里把剩下的时间补回来，
            # 好让发出去的时刻正好是用户设的那个点。
            await self._hold_until(post_at)
            for stream_id in targets:
                try:
                    outcome = await self._deliver(stream_id, collected, payloads)
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("[搬石] 往 %s 发送失败：%s", stream_id, exc)
                    failed += 1
                    continue
                delivered += 1
                for thread, _detail in outcome:
                    skipped_tids.add(thread.tid)
                # outcome 是「没发出去」的帖。反过来数一遍才是真正落到群里的条数。
                # 以前摘要直接写「本轮搬了 len(collected) 条」，于是长图一张都没渲出来、
                # 群里什么都没收到的那一轮，日志里照样写着「搬了 4 条，发到 1 个群」——
                # 用户看着这行字、群里看着空，谁都不知道是渲染挂了。这是本轮踩的坑，
                # 所以摘要必须按**真发出去的条数**说，不能按「挑了几条」说。
                landed = len(collected) - len(outcome)
                if landed > landed_posts:
                    landed_posts = landed
                if landed:
                    ok_streams.append(stream_id)
            # 只有「每个目标都收到了」才记「搬过了」。
            # 去重是帖子级的，任何一个群漏收都意味着它再也等不到这条 ——
            # 哪怕是永久丢帖。所以只要有一个目标失败就整条不记账，
            # 让下一轮重搬一次（收到过的群会重复看到，比收不到强）。
            #
            # 另外，长图没渲出来的帖子也**不能**记账：它压根没发到群里，
            # 记了就等于永久丢帖。这类帖留给下一轮重试。
            if targets and not failed:
                for thread, _detail in collected:
                    if thread.tid in skipped_tids:
                        continue
                    store.mark(forum_of.get(thread.tid, ""), thread.tid, thread.title)
        store.flush()

        is_error = False
        if blocked:
            summary = "被贴吧拦住了，本轮先停下：%s" % blocked
            is_error = True
        elif not collected:
            summary = "本轮没有新帖可搬（要么都搬过了，要么没够门槛）"
        else:
            # 「挑了几条」和「真发出去几条」是两回事。一条都没发出去的那一轮，
            # 挑的条数是 4、发出去的是 0，日志要是只写前者，看日志的人会以为一切正常，
            # 而群里其实什么也没有（这就是本轮云端踩的坑）。所以这里两条都写出来。
            #
            # 措辞只说到「没发出去」，不猜原因：以前这里写死「长图都没渲出来」，
            # 而实际是聊天记录被协议端拒收（长图明明都渲好了）—— 一句猜错的
            # 原因比不说更糟，会把排查引到完全错误的方向上。
            summary = "本轮挑了 %d 条" % len(collected)
            if landed_posts:
                summary = "本轮搬了 %d 条" % landed_posts
                if landed_posts < len(collected):
                    summary += "（另有 %d 条长图没渲出来，这轮没发）" % (
                        len(collected) - landed_posts
                    )
            else:
                summary += "，但一条也没发出去（原因见上面的日志）"
                is_error = True
            if delivered:
                # 说「群」而不是「会话」：这两个数以前是同一个，直到发现同一个群
                # 可能占两行会话记录（见 _target_streams 的去重说明）。那时摘要
                # 会写成「发到 2 个会话」——明明只发了一个群两条，读日志的人以为
                # 一切正常，bug 就这么藏了一个月。现在按群数说，对得上就是对的。
                summary += "，发到 %d 个群" % delivered
            if failed:
                # 有群没收到就不记账，下一轮会重搬；说清楚免得用户以为是重复发
                summary += "，%d 个群没发成功（这几条下一轮会重搬一次）" % failed
                is_error = True
            elif not delivered:
                if host_error:
                    summary += "，但没问到会话列表（主程序可能正忙，过会儿再试）"
                    is_error = True
                elif streams is None:
                    summary += "，但没有找到可发的群（检查群配置或机器人是否在群里说过话）"
                    is_error = True
        # 结束语：这一轮真把帖子发进过某个群，才补这一句。
        await self._say_closing(cfg, ok_streams)
        self.ctx.logger.info("[搬石] %s结束：%s", trigger, summary)
        return _RoundSummary(summary, error=is_error)

    async def _say_closing(self, cfg: Any, streams: Sequence[str]) -> None:
        """一轮结束后，在收到帖子的会话里补一句话（面板上的「结束语」）。

        留空则什么都不说。发送走闸门 —— 刚发完长图和视频，紧接着再说话最容易
        撞上频控。
        """
        if not streams:
            return
        line = str(getattr(cfg.send, "closing_text", "") or "").strip()
        if not line:
            return
        for stream_id in streams:
            try:
                await self._gate_of().run(
                    self.ctx.send.text, line, stream_id, storage_message=False
                )
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("[搬石] 结束语发不出去：%s", exc)

    async def _hold_until(self, post_at: datetime | None) -> None:
        """等到该发的那一刻再发。

        贴吧取帖快慢不定；「提前开始」就是先取好帖、再掐着点发，让发布时刻
        严格等于用户设的那个点。过了点就直接发，不补等。
        """
        if post_at is None:
            return
        left = (post_at - datetime.now()).total_seconds()
        if left <= 0:
            self.ctx.logger.info(
                "[搬石] 已经过了 %s，不再等，直接发", post_at.strftime("%H:%M")
            )
            return
        self.ctx.logger.info(
            "[搬石] 帖子备好了，等到 %s 再发（还有 %d 秒）",
            post_at.strftime("%H:%M"),
            int(left),
        )
        await asyncio.sleep(left)

    async def _load_threads(
        self,
        forum: str,
        *,
        client: Any,
        proto: Any,
        pages: int,
    ) -> tuple[list[Any], str]:
        """取一个吧的帖子列表，返回 (帖子, 被拦原因)。

        优先走 protobuf：只有它给点赞数，而「最少点赞数」这项筛选全靠它。
        protobuf 取不到（网络/结构变动）就退回 HTML 列表页 —— 那条路点赞数恒为 0，
        `_pick_threads` 见到「整页都是 0」会自动跳过点赞筛选，不会把整吧帖子误杀。
        """
        if proto is not None:
            try:
                threads = await proto.fetch_thread_list(forum=forum, pages=pages)
            except StoneBlockedError as exc:
                return [], str(exc)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning(
                    "[搬石] %s 的帖子列表（protobuf）没取到，退回网页版：%s", forum, exc
                )
            else:
                if threads:
                    return threads, ""
                self.ctx.logger.debug("[搬石] %s 的 protobuf 列表页是空的，退回网页版", forum)
        try:
            threads = await client.fetch_thread_list(forum, pages=pages)
        except StoneBlockedError as exc:
            return [], str(exc)
        except StoneTiebaError as exc:
            self.ctx.logger.warning("[搬石] %s 的帖子列表没取到：%s", forum, exc)
            return [], ""
        return list(threads), ""

    async def _load_detail(
        self,
        thread: Any,
        *,
        client: Any,
        proto: Any,
        max_floors: int,
        max_chars: int,
        max_comments: int,
    ) -> tuple[Any | None, str]:
        """取一个帖子的正文，返回 (详情, 被拦原因)。

        两条路各拿一半，缺一不可：

        - **HTML** 给 ``raw``（页面原文）—— 「贴吧原页面」长图就是把这份 HTML 交给
          渲染器截的，protobuf 给不了。
        - **protobuf** 给楼中楼正文、楼层点赞数，以及**真翻页** —— HTML 的 ``kz=``
          页面只有第一页（``pn=`` 对它无效），所以 ``max_floors`` 超过 30 时只有
          protobuf 这条能接着往下取。

        两边都失败才算这条帖子失败；只有一边成功就用手上有的那份。
        """
        detail = None
        html_error = ""
        try:
            detail = await client.fetch_thread_detail(
                thread,
                max_floors=max_floors,
                max_chars=max_chars,
                max_comments=max_comments,
            )
        except StoneBlockedError as exc:
            return None, str(exc)
        except StoneTiebaError as exc:
            html_error = str(exc)

        block_error = ""
        if proto is not None:
            try:
                proto_detail = await proto.fetch_thread_detail(
                    thread,
                    max_floors=max_floors,
                    max_chars=max_chars,
                    max_comments=max_comments,
                )
            except StoneBlockedError as exc:
                # **不要**在这里直接放弃手上那份。protobuf 翻到第 2 页才被拦时，
                # HTML 那边通常已经拿到了完整正文（还有长图要的 raw），
                # 直接 return None 等于把整帖丢掉、白抓一次。
                # 先把能发的发掉，再把「被拦了」这个事实交给调用方停掉整轮。
                block_error = str(exc)
                self.ctx.logger.warning(
                    "[搬石] 帖子 %s 的 protobuf 正文被反爬拦下（%s），用网页版已有的内容继续",
                    thread.tid,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning(
                    "[搬石] 帖子 %s 的正文（protobuf）没取到：%s", thread.tid, exc
                )
            else:
                detail = self._merge_detail(detail, proto_detail)

        if detail is None:
            self.ctx.logger.warning("[搬石] 帖子 %s 取正文失败：%s", thread.tid, html_error)
            return None, block_error
        return detail, block_error

    @staticmethod
    def _merge_detail(html_detail: Any, proto_detail: Any) -> Any:
        """把 protobuf 那份的楼中楼与点赞数补进 HTML 那份。

        HTML 是底子（它带着 ``raw``，长图靠它）；protobuf 只往上补两样：
        楼中楼（HTML 只渲染前几条，而且拿不到正文）和楼层点赞数（HTML 一个都没有）。
        按楼层号对齐 —— 两边的楼号都是 1 起算，对不上就按顺序兜底。
        """
        if html_detail is None:
            return proto_detail
        if proto_detail is None:
            return html_detail
        by_fn = {floor.fn: floor for floor in proto_detail.floors if floor.fn}
        for idx, floor in enumerate(html_detail.floors):
            other = by_fn.get(floor.fn)
            if other is None and idx < len(proto_detail.floors):
                other = proto_detail.floors[idx]
            if other is None:
                continue
            if not floor.comments and other.comments:
                floor.comments = list(other.comments)
            if other.comment_total > floor.comment_total:
                floor.comment_total = other.comment_total
            if not floor.agree:
                floor.agree = other.agree
            if not floor.text and other.text:
                floor.text = other.text
        # protobuf 多出来的楼层（HTML 只有第一页）接着补在后面，楼号已经自编对齐过
        if len(proto_detail.floors) > len(html_detail.floors):
            html_detail.floors.extend(proto_detail.floors[len(html_detail.floors) :])
        if not html_detail.info and proto_detail.info:
            html_detail.info = dict(proto_detail.info)
        return html_detail

    def _filter_mode(self) -> str:
        """当前生效的「只搬哪种帖」：工具调用时是它临时指定的，平时是配置值。

        没有临时指定时走 _send_options()，这样工具那一轮读的是「工具」节的
        thread_filter，定时/手动轮读「发送」节 —— 与 README 的说法一致。
        """
        override = str(getattr(self, "_kind_override", "") or "")
        if override:
            return override
        return str(getattr(self._send_options(), "thread_filter", _FILTER_ALL) or _FILTER_ALL)

    def _passes_filter(self, detail: Any) -> bool:
        """这条帖子符不符合「只搬哪种帖」（详情页兜底判断）。

        正常走的是 _pick_threads 里按列表页标记的预筛；这里只在拿不到
        列表标记（thread_type=0，比如老页码或结构变动）时按真解析结果兜底。
        """
        mode = self._filter_mode()
        if mode == _FILTER_ALL:
            return True
        has_video = any(getattr(f, "videos", None) for f in getattr(detail, "floors", []))
        if mode == _FILTER_VIDEO:
            return has_video
        if mode == _FILTER_TEXT:
            return not has_video
        return True

    def _pick_threads(self, forum: str, threads: Sequence[Any], limit: int = 0) -> list[Any]:
        """从列表里挑出这一轮要搬的帖子。limit > 0 时按它取，否则用配置里的条数。

        列表页的 ``data-thread-type`` 能先筛掉不合口味的帖子，省下详情页那次请求；
        但那是**预筛，不是定论**：贴吧偶尔把视频帖标成 0，真伪要等详情页解析出来
        才知道（见 :meth:`_passes_filter`）。所以「只搬视频帖」时多留一截候选，
        免得预筛看走眼就一条都挑不出来、后面的视频帖连被看一眼的机会都没有。
        """
        cfg = self.config.stone
        want = int(limit) if int(limit) > 0 else int(cfg.count_per_forum)
        mode = self._filter_mode()
        # 要发的条数就这么多，但用于「拿详情页复核」的候选允许超标。
        # 上限按候选数走，多取的部分只多花几次详情页请求，不会被发出去。
        if mode == _FILTER_ALL:
            budget = want
        else:
            budget = max(want, want * _FILTER_CANDIDATE_FACTOR)
        store = self._store
        keywords = [str(word).strip().lower() for word in cfg.block_keywords if str(word).strip()]
        min_agree = int(getattr(cfg, "min_agree", 0) or 0)
        # 这一页有没有读到过点赞数：全是 0 就当作没读到，别把解析失败当成「零赞」。
        page_has_agree = any(int(getattr(x, "agree", 0) or 0) > 0 for x in threads)
        picked: list[Any] = []
        # 收满 budget 就够了。这里**不**替 _run_locked 的复核收窄候选：「只要视频帖」
        # 时列表页标记偶尔失准（视频帖被标成 0），多留几倍正是为了在复核阶段捞回来；
        # 收窄成 want 会让预筛一失准就整轮挑不出视频帖。
        for thread in threads:
            if cfg.skip_top and bool(getattr(thread, "is_top", False)):
                continue
            if int(getattr(thread, "replies", 0) or 0) < int(cfg.min_replies):
                continue
            # 点赞数只有列表页内联 JSON 里有，读不到时字段就是 0。
            # 但 0 既可能是「真的零赞」也可能是「没解析到」；对置顶/公告这类帖子，
            # 当成零赞筛掉是错的 —— 所以只在**这一页确实读到过**点赞数据时才按它筛。
            if min_agree > 0 and page_has_agree:
                if int(getattr(thread, "agree", 0) or 0) < min_agree:
                    continue
            title = str(getattr(thread, "title", "") or "").lower()
            if any(word in title for word in keywords):
                continue
            # 这里就能按列表页的 data-thread-type 筛掉不合口味的帖子，
            # 不用先把详情页抓回来 —— 详情页一次请求比列表页贵得多。
            if mode != _FILTER_ALL and not self._type_matches(thread, mode):
                continue
            if store is not None and store.known(forum, thread.tid):
                continue
            picked.append(thread)
            if len(picked) >= budget:
                break
        return picked

    @staticmethod
    def _type_matches(thread: Any, mode: str) -> bool:
        """按列表页标记判断帖子类型。

        视频帖的标记是 data-thread-type="40"，普通帖是 "0"（另有 33/71 等
        其它类型）。所以「是不是视频」可以直接算出来，不存在「标记缺失」：
        只有拿不到 li 属性时 thread_type 才留 0，那和普通帖同义，按非视频算。
        """
        has_video = bool(getattr(thread, "has_video", False))
        if mode == _FILTER_VIDEO:
            return has_video
        if mode == _FILTER_TEXT:
            return not has_video
        return True

    async def _target_streams(self) -> tuple[list[str], str]:
        """按黑白名单挑出要发的会话。

        Returns:
            (会话列表, 出错原因)。出错原因是 "host" 表示没问着宿主（RPC 故障），
            空串表示问着了、就是没有可发的群 —— 两者要分清楚，
            否则宿主机临时抽风却提示用户「检查群配置」，纯属让人白查。
        """
        # 群名单在「定时」一节：它管的是发给谁，不是发出来长什么样。
        cfg = self.config.schedule
        mode = str(getattr(cfg, "group_mode", _GROUP_ALLOW) or _GROUP_ALLOW).strip().lower()
        mode = _GROUP_ALIASES.get(mode, mode)  # 老词一律归一到中文
        listed = {_clean_id(x) for x in getattr(cfg, "group_list", None) or []}
        listed.discard("")
        if mode == _GROUP_DENY:
            include: set[str] = set()
            exclude = listed
            allow_mode = False
        else:
            # 白名单：名单空着就是「哪个群都不发」。这是有意的 —— 默认装好插件
            # 不该往任何群里发东西，得用户自己把群号填进来才算数。
            include = listed
            exclude: set[str] = set()
            allow_mode = True
        try:
            resp = await self.ctx.chat.get_all_streams(platform="qq")
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[搬石] 取会话列表失败：%s", exc)
            return [], "host"
        streams = resp.get("streams") if isinstance(resp, dict) else resp
        if not isinstance(streams, list):
            self.ctx.logger.warning("[搬石] 会话列表形状不认识，本轮不发")
            return [], "host"
        # 同一个群在宿主的会话表里可能留下不止一行：一行规规矩矩带着群名和账号，
        # 另一行是没归好档的孤儿行（group_id 一样，其它字段空着）。宿主把这些行
        # 原样列出来，不去重的话这里会挑回两个 session_id，同一轮对同一个群把整批
        # 内容投递两遍 —— 用户看到的就是「一次连续发两次」。
        #
        # 光按群号只留一行还不够：**留哪一行是要挑的**。会话表按插入序返回，先出现的
        # 未必是能用的那行；万一留下的是孤儿行，消息发不出去，而因为群号在白名单里
        # 命中了，摘要照样写「发到 N 个群」，用户只看到少收一条、查不出原因。
        # 所以先按群号分组，每组挑一个「最像真的」的会话，挑不出来才退回第一个。
        groups: dict[str, list[dict[str, Any]]] = {}
        degenerated: list[str] = []
        seen: set[str] = set()
        for item in streams:
            if not isinstance(item, dict):
                continue
            if item.get("is_group_session") is False:
                continue
            group_id = _clean_id(item.get("group_id"))
            session_id = str(item.get("session_id") or "").strip()
            if not session_id:
                continue
            if group_id and group_id in exclude:
                continue
            if allow_mode and (not group_id or group_id not in include):
                # 白名单模式下要么认不出群号、要么不在名单里，一律不发 ——
                # 宁可不发，也不要发错群。注意这里**不能**先判 include 是否为空：
                # 白名单的空名单语义是「哪个群都不发」，不是「不限」。
                continue
            if not group_id:
                # 认不出群号：白名单模式下上面已经拦掉了，这里只可能是黑名单模式。
                # 没有群号可归组，只能按 session_id 去重 —— 宿主偶尔会把同一条会话
                # 列两遍，不去重就等于对同一个会话发两遍，正是要防的那种重复。
                if session_id in seen:
                    self.ctx.logger.info(
                        "[搬石] 会话 %s 在列表里出现多次，只发一条", session_id
                    )
                    continue
                seen.add(session_id)
                degenerated.append(session_id)
                continue
            groups.setdefault(group_id, []).append(item)

        out: list[str] = []
        taken: set[str] = set()
        for group_id, items in groups.items():
            chosen = _pick_live_session(items)
            if len(items) > 1:
                self.ctx.logger.info(
                    "[搬石] 群 %s 有 %d 条会话记录，只发一条（留下 session=%s）",
                    group_id,
                    len(items),
                    chosen.get("session_id"),
                )
            session_id = str(chosen.get("session_id") or "").strip()
            if not session_id:
                continue
            taken.add(session_id)
            out.append(session_id)
        # 认不出群号的那些会话放在最后收。这里**必须**跟已经选中的按会话 id 去重：
        # 缺 group_id 的行归不了组，只能走这条路，而宿主列出的行并不保证字段整齐 ——
        # 同一个 session_id 完全可能既有一行带群号（已进 out）、又有一行不带。少了
        # 这一步，同一个会话就会进 targets 两遍，一轮里对同一个群投递两次，用户看到
        # 的就是「一次连续发两次」。按 id 硬去重，任何来源都一样对待。
        for session_id in degenerated:
            if session_id in taken:
                self.ctx.logger.info(
                    "[搬石] 会话 %s 同一轮里出现了两次，只发一条", session_id
                )
                continue
            taken.add(session_id)
            out.append(session_id)
        return out, ""

    async def _prepare_payloads(
        self, collected: Sequence[tuple[Any, Any]]
    ) -> dict[str, dict[str, Any]]:
        """把每条帖子的图片下载好，只下这一轮真用得上的。

        - 自绘卡片 / 文本 + 图片：要「长图用的 data URI + 直接发图用的 base64」，按楼层顺序取前几张。
        - 贴吧原页面：要「页面里每个图片占位的 data URI」，按页面出现顺序取前几张 ——
          页面自己的图要靠脚本才加载得出来，截图时是灰框，所以只能我们下好嵌进去。
        - 视频：要 mp4 字节，走发视频那条通道；下不动或超限就只留链接。
        """
        cfg = self._send_options()
        client = self._client
        payloads: dict[str, dict[str, Any]] = {}
        if client is None:
            return payloads
        style = _card_style(cfg)
        # 正文图只服务长图：自绘卡片要把它们插进卡片里，原页面长图要把它们
        # 补进截图（手机版页面根本没带全图）。长图恒开，所以两项恒真。
        want_floors = True
        want_page = style == _CARD_STYLE_PAGE
        limit = int(cfg.max_images)
        max_width = int(cfg.image_max_width)
        quality = int(cfg.image_quality)
        for thread, detail in collected:
            uris: dict[str, str] = {}
            blobs: dict[str, str] = {}
            sizes: dict[str, tuple[int, int]] = {}
            page: dict[str, str] = {}
            if want_floors:
                thumb_of: dict[str, str] = {}
                order: list[str] = []
                for floor in detail.floors:
                    for url, thumb in zip(floor.images, floor.thumbs):
                        if url and url not in thumb_of:
                            thumb_of[url] = thumb
                            order.append(url)
                if limit > 0:
                    order = order[:limit]
                else:
                    order = []
                for url in order:
                    try:
                        raw = await client.download_image(
                            url, thumb=thumb_of.get(url, ""), referer=thread.mobile_url
                        )
                    except StoneBlockedError as exc:
                        self.ctx.logger.warning("[搬石] 取图被拦住，剩下的图不取了：%s", exc)
                        break
                    except Exception as exc:  # noqa: BLE001
                        self.ctx.logger.debug("[搬石] 取图失败 %s：%s", url, exc)
                        continue
                    if not raw:
                        continue
                    uri = image_data_uri(raw, max_width=max_width, quality=quality)
                    if not uri:
                        continue
                    uris[url] = uri
                    blobs[url] = uri.split(",", 1)[1] if "," in uri else ""
                    sizes[url] = _image_size(raw)
            if want_page:
                # 原页面模式不看 max_images：那个值是「文本+图片一次发几张」，
                # 拿到这里用就会让第 N 张往后全变空洞。这里按页面占位数全补，只受硬上限约束。
                page_raw = str(getattr(detail, "raw", "") or "")
                spots = placeholder_urls(page_raw)[:_PAGE_IMAGE_LIMIT]
                # 首楼多图时 DOM 只有 3 个格子，其余图只在 contentTotal 载荷里，
                # 所以要把它们也下下来 —— 否则长图里那层只有 3 张。
                have = {signed for signed, _u in spots}
                extra_spots: list[tuple[str, str]] = []
                for signed in content_total_images(page_raw):
                    if signed in have:
                        continue
                    have.add(signed)
                    direct = to_origin_image(_direct_image_url(signed)) or signed
                    extra_spots.append((signed, direct))
                spots = (spots + extra_spots)[:_PAGE_IMAGE_LIMIT]
                for signed, url in spots:
                    try:
                        raw = await client.download_image(
                            url, thumb=signed, referer=thread.mobile_url
                        )
                    except StoneBlockedError as exc:
                        self.ctx.logger.warning("[搬石] 补图被拦住，剩下的图不补了：%s", exc)
                        break
                    except Exception as exc:  # noqa: BLE001
                        self.ctx.logger.debug("[搬石] 补图失败 %s：%s", url, exc)
                        continue
                    if not raw:
                        continue
                    uri = image_data_uri(
                        raw,
                        max_width=min(max_width, _PAGE_IMAGE_MAX_WIDTH),
                        quality=quality,
                    )
                    if uri:
                        page[signed] = uri
            # 视频不再下载：发送走 videourl 段，只把直链交给协议端自己去取。
            # 以前在这里下整段视频再转 base64，会撞上 RPC 单帧 16MB 的上限
            # （base64 再胀 1/3，原视频过 12MB 就必失败），白下载还发不出去。
            payloads[thread.tid] = {
                "uris": uris,
                "blobs": blobs,
                "sizes": sizes,
                "page": page,
            }
        return payloads

    # -- 发送 -------------------------------------------------------------

    def _send_options(self) -> Any:
        """当前生效的发送选项。

        平时就是配置里的「发送」一节。麦麦自己调工具搬石时改用「工具」一节 ——
        那一节可以单独配（只发长图、不带链接之类），免得改工具的行为还得连手动
        搬的样式一起动。工具那一节没配到的项，回落到「发送」一节。
        """
        # getattr 兜底：_tool_round 由 on_load 建立，用 object.__new__ 造出来的
        # 测试桩没有它，缺了就当作「不是工具轮」。
        if getattr(self, "_tool_round", False):
            return _ToolOptions(self.config)
        return self.config.send

    def _forward_name(self) -> str:
        """合并转发里显示的发送者名字，配置留空时用默认值。"""
        name = str(getattr(self._send_options(), "forward_name", "") or "").strip()
        return name or _DEFAULT_FORWARD_NAME

    def _thread_header(self, thread: Any) -> str:
        forum = str(getattr(thread, "forum", "") or "").strip() or "贴吧"
        title = str(getattr(thread, "title", "") or "").strip() or "（无标题）"
        replies = int(getattr(thread, "replies", 0) or 0)
        head = "【%s】%s" % (forum, title)
        if bool(getattr(self._send_options(), "post_link", True)):
            return "%s\n%s · %d 回复" % (head, getattr(thread, "url", ""), replies)
        return "%s\n%d 回复" % (head, replies)

    async def _deliver(
        self,
        stream_id: str,
        collected: Sequence[tuple[Any, Any]],
        payloads: dict[str, dict[str, Any]],
    ) -> list[tuple[Any, Any]]:
        """把本轮内容发到某个群。返回**没发出去**的帖子。

        没发出去有两种：长图没渲出来，或者整条聊天记录没发成。两种都交回去
        让调用方排除记账 —— 这些帖一条都没到群里，记成「已搬过」就是永久丢帖。

        **内容只发一次，只在聊天记录里。** 转发不重试（`_FORWARD_RETRY_TIMES`），
        失败也不把帖子拆开单独发 —— 拆出来就会变成「聊天记录外面的一份」，
        正是 issue #1 抱怨的重复。
        """
        cfg = self._send_options()
        if cfg.forward:
            # 长图恒开，所以每次都要合成聊天记录；装不下的帖子交回 overflow。
            overflow: list[tuple[Any, Any]] = []
            nodes, overflow = await self._forward_nodes(collected, payloads)
            sent = False
            if nodes:
                # 转发是最贵的一次发送（几十 MB 的帧），但**恰恰不能重试**：
                # 它慢、容易超时，而超时几乎都意味着「其实已经发出去了」。
                # 重试一次，群里就多一条一模一样的聊天记录。
                sent = await self._gate_of().run(
                    self.ctx.send.forward,
                    nodes,
                    stream_id,
                    timeout_ms=_FORWARD_TIMEOUT_MS,
                    storage_message=False,
                    _retries=_FORWARD_RETRY_TIMES,
                )
            # 装不进聊天记录的帖子**不补发**（不单独发长图，也不退发文字）：
            # 它们没到群里，交回去让 _run_locked 不记账，下一轮重来。补发出来的
            # 第二条长图对刚看过整帖的人就是重复刷屏；而「装不下」如今只在内嵌
            # 兜底（长图落盘失败）时才可能发生 —— 长图走本地路径引用，帧里只有
            # 两百来字节，与图片多大无关。
            if not nodes:
                # 一条帖子都没能进记录（长图全没渲出来）：整轮不发。
                return list(overflow)
            if not sent:
                # 整条聊天记录没发出去 —— **不补发**，这一轮就当没搬。
                #
                # 内容必须只在聊天记录里出现一次。一旦失败就逐帖单独发长图，
                # 那几条就会以「聊天记录外面的一份」出现在群里 —— 正是 issue #1
                # 说的「部分内容会在聊天记录之外重复发送」。而且这种补发只可能
                # 在转发真的失败时触发，用户看到的是半截聊天记录加一堆散图，
                # 比什么都不发更乱。
                #
                # 这几帖不记账（交回去让 _run_locked 排除），下一轮会自动重来，
                # 所以不会永久丢帖。
                self.ctx.logger.warning(
                    "[搬石] 聊天记录没发出去，这 %d 条不单独补发，下一轮重搬"
                    "（协议端为什么拒收，看主程序日志里的 SendService 行）",
                    len(collected),
                )
                return list(collected)
            if overflow:
                self.ctx.logger.info(
                    "[搬石] 有 %d 条帖子没能进聊天记录，这一轮不补发，留到下一轮",
                    len(overflow),
                )
            return list(overflow)
        skipped = []
        for thread, detail in collected:
            ok = await self._send_cards(
                stream_id, thread, detail, payloads.get(thread.tid) or {}
            )
            if not ok:
                skipped.append((thread, detail))
        await self._send_videos(stream_id, collected, payloads)
        return skipped

    async def _send_videos(
        self,
        stream_id: str,
        collected: Sequence[tuple[Any, Any]],
        payloads: dict[str, dict[str, Any]],
    ) -> None:
        """把这条帖子的视频发出去，走 ``videourl`` 段。

        这里只把贴吧的 mp4 直链交给宿主，视频本体由协议端自己去取，**不经过
        插件与主程序之间的 RPC 通道**。这一点很关键：那条通道的单帧上限写死在
        ``src/plugin_runtime/transport/base.py`` 的 ``MAX_FRAME_SIZE = 16MB``，
        而视频要以 base64 传输会胀大约 1/3，于是原视频一旦超过 12MB 就会在发送
        时抛 ``[E_UNKNOWN] 帧大小 N 超过最大限制 16777216``——过去「大视频发不
        出去」的真正原因就在这里，跟 QQ 服务端的大小限制无关。改传 URL 之后帧
        里只有两百来个字符，实测 28.2MB 的视频照发不误。

        ``video.url`` 是带 ``tbau`` 签名的直链，签名按天过期；发送紧接着抓取
        进行，所以来得及。真过期了也只会退化成「补一条链接」，不会丢帖。
        """
        cfg = self._send_options()
        if not bool(getattr(cfg, "send_video", True)):
            return
        limit = self._video_limit_bytes()
        for thread, detail in collected:
            all_videos = self._thread_videos(detail)
            if not all_videos:
                continue
            # 逐条视频记，而不是整帖一个布尔：某一条失败时，它也必须出现在
            # 补发的链接里，否则它就无声无息地没了。
            missing: list[str] = []
            # 只发带直链的：没直链的段发出去也是个空壳。
            sendable = [video for video in all_videos if video.url]
            for video in sendable:
                if await self._video_too_big(video, thread, limit):
                    continue
                # 视频已经是降级到「发链接」的最后一步了，这里重试是纯赚：
                # 链接再发不出去，用户就完全不知道有这条视频。
                ok = await self._gate_of().run(
                    self.ctx.send.custom,
                    "videourl",
                    {"file": video.url},
                    stream_id,
                    timeout_ms=_VIDEO_SEND_TIMEOUT_MS,
                    storage_message=False,
                )
                if not ok:
                    self.ctx.logger.warning("[搬石] 视频没发出去，改成发链接：%s", video.url)
                    missing.append(video.url)
            # 没发成的那些补一条链接，免得用户不知道有视频
            if missing:
                await self._gate_of().run(
                    self.ctx.send.text,
                    "视频：" + chr(10).join(missing),
                    stream_id,
                    storage_message=False,
                )

    def _video_limit_bytes(self) -> int:
        """「视频大小上限」换成字节；0 表示不限制。"""
        mb = int(getattr(self._send_options(), "video_max_mb", 0) or 0)
        return mb * 1024 * 1024

    async def _video_too_big(self, video: Any, thread: Any, limit: int) -> bool:
        """这条视频是否超过上限（超了就整条跳过，连链接都不发）。

        上限的意义不是「QQ 收不收得了」——视频走 ``videourl`` 段由协议端自取，
        多大都不经过插件的 RPC 通道——而是**别让单次搬运拖太久**：协议端下载
        一个大视频会占住那一轮。

        问不到大小时（服务端不回长度、或探测请求失败）一律按「不大」处理：
        宁可偶尔搬久一点，也不要因为问不到就默默把视频全丢掉。

        反爬拦截在这里**不当异常抛**：抛出去会一路冒到 ``_deliver``，让这个群算
        「发送失败」，进而整批不记账 —— 下一轮重搬，群里重复看到同一批帖子。
        视频发不出去顶多少发一个视频，不值得把整轮的记账赔进去，所以按「不大」
        处理，照发（协议端自己去取直链，与贴吧的 IP 封锁无关）。
        """
        if limit <= 0:
            return False
        # getattr 兜底：用 object.__new__ 造的测试桩没有 _client。
        client = getattr(self, "_client", None)
        if client is None:
            return False
        try:
            size = await client.probe_video_size(video, referer=getattr(thread, "mobile_url", "") or "")
        except StoneBlockedError as exc:
            self.ctx.logger.warning("[搬石] 查视频大小时撞上反爬，这条照发：%s", exc)
            return False
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[搬石] 查视频大小失败 %s：%s", video.url, exc)
            return False
        if size is None:
            return False
        if size <= limit:
            return False
        self.ctx.logger.info(
            "[搬石] 视频 %.1f MB 超过上限 %d MB，跳过：%s",
            size / 1048576.0,
            limit // 1048576,
            video.url,
        )
        return True

    async def _card_images(
        self, thread: Any, detail: Any, payload: dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        """把一条帖子渲染成长图；返回 (base64 列表, 落盘路径列表)。

        两个列表一一对应：``paths[i]`` 是 ``images[i]`` 落盘后的 ``file://`` 路径，
        落盘失败时对应项是空串。转发节点优先用路径（帧里只有两百来字节、与图片
        体积无关），路径为空才退回 base64。

        **「贴吧原页面」渲不出来就自动改用自绘卡片**，而不是丢掉这帖。

        这条兜底原本是为了绕开宿主的一个坑：网络路由回调用了
        ``functools.partial(self._handle_network_route, allow_network=...)``，
        而 playwright ≤1.60 的 ``wrap_handler`` 把 ``allow_network`` 也算进位置
        参数个数，于是每个请求都抛 ``TypeError: got multiple values for argument
        'allow_network'`` —— 外链请求全部悬空，``load`` 永不触发，一路等到超时，
        用户看到的就是「一张图都没有」。自绘卡片的图全是 ``data:`` 内嵌，页面零
        外链，那个回调一次都不会被调用，所以完全绕得开。

        1.1.7 起原页面自己也零外链了（见 :meth:`_localize_page`），正常路径上已经
        踩不到那个坑，所以这条降级现在只是兜底：装饰图全下不动、宿主渲染器本身
        出问题、CSP 之外还有漏网请求时才会走到。

        判定是**按轮**做的（``_page_mode_down``）：一旦本轮确认原页面这条路走不通，
        后面几帖就不再白等一遍超时（每帖两档 x 一次超时就是好几分钟）；下一轮开头
        复位，宿主那边恢复之后自己就好了。
        """
        cfg = self._send_options()
        if _card_style(cfg) == _CARD_STYLE_PAGE and not getattr(
            self, "_page_mode_down", False
        ):
            raw_html = str(getattr(detail, "raw", "") or "")
            if raw_html:
                html = build_page_html(
                    raw_html,
                    images=payload.get("page") or {},
                    max_floors=int(self.config.fetch.max_floors),
                )
                image_b64 = await self._render_page(html, _page_render(cfg))
                if image_b64:
                    return [image_b64], [self._save_page_image(image_b64)]
                # 走到这里说明原页面这条路这一轮废了。**不丢帖**：标记降级，
                # 让下面自绘卡片那段接手。只在本轮第一次降级时记一条，免得
                # 每帖重复刷同一句话。
                self._page_mode_down = True
                if _page_render(cfg) == _PAGE_RENDER_RAW:
                    self.ctx.logger.warning(
                        "[搬石] 贴吧原页面渲不出来，本轮剩下的帖改用自绘卡片"
                        "（「原页面渲染方式」设成了「原生」，主程序的 Playwright 需要在 "
                        "1.61 以上；改回「零外链」即可，下一轮会自动重试原页面）"
                    )
                else:
                    self.ctx.logger.warning(
                        "[搬石] 贴吧原页面渲不出来，本轮剩下的帖改用自绘卡片"
                        "（装饰图下载失败或宿主渲染器异常时会出现，下一轮会自动重试原页面）"
                    )
            else:
                self.ctx.logger.warning("[搬石] 没留下页面原文，这条改用自绘卡片")
        uris: dict[str, str] = payload.get("uris") or {}
        sizes: dict[str, tuple[int, int]] = payload.get("sizes") or {}
        width = int(cfg.card_width)
        floors = list(detail.floors)
        floor_images = _select_floor_images(floors, uris, int(cfg.max_images))
        chunks = _plan_card_chunks(floors, sizes, width, floor_images)
        if not chunks:
            return [], []
        images: list[str] = []
        paths: list[str] = []
        for index, chunk in enumerate(chunks):
            segment = _seg_images(chunk, floors, floor_images, uris)
            html = build_thread_html(
                thread,
                chunk,
                images=segment,
                width=width,
                max_images=len(segment),
                part=index,
                parts=len(chunks),
            )
            # 自绘卡片比原页面短，但长帖切出来的分片一样可能超标。不过和原页面一样，
            # **不降档**：降档是再渲一遍，总时间只会更长，省下的那点体积也没意义 ——
            # 长图发送走本地路径，不占帧，真正的上限只有 RPC 帧的 12 MB 硬顶。
            # 同一档上给瞬时故障留一次重试，超时不重试（理由见 :meth:`_render_page`）。
            image_b64 = ""
            card_reason = ""
            for attempt in range(_RENDER_TRANSIENT_RETRY + 1):
                try:
                    result = await self._render_card_html(html, _PAGE_RENDER_SCALE)
                except Exception as exc:  # noqa: BLE001
                    card_reason = "%s: %s" % (type(exc).__name__, exc)
                    self.ctx.logger.warning(
                        "[搬石] 长图渲染抛异常（第 %d 次）：%s", attempt + 1, exc
                    )
                    if _looks_like_timeout(card_reason) or attempt >= (
                        _RENDER_TRANSIENT_RETRY
                    ):
                        break
                    await asyncio.sleep(_RENDER_RETRY_PAUSE_S)
                    continue
                candidate = ""
                if isinstance(result, dict):
                    candidate = str(result.get("image_base64") or "")
                if not candidate:
                    if isinstance(result, dict) and not card_reason:
                        card_reason = str(
                            result.get("error") or result.get("message") or result
                        )[:200]
                    if _looks_like_timeout(card_reason) or attempt >= (
                        _RENDER_TRANSIENT_RETRY
                    ):
                        break
                    await asyncio.sleep(_RENDER_RETRY_PAUSE_S)
                    continue
                raw_bytes = len(candidate) * 3 // 4
                if raw_bytes > _PAGE_IMAGE_HARD_LIMIT_BYTES:
                    self.ctx.logger.warning(
                        "[搬石] 卡片长图 %.1f MB 超过 RPC 帧上限 %.1f MB，这帖跳过",
                        raw_bytes / 1048576.0,
                        _PAGE_IMAGE_HARD_LIMIT_BYTES / 1048576.0,
                    )
                    card_reason = "体积超限"
                    break
                image_b64 = candidate
                break
            if not image_b64:
                # 见上：渲不出来就跳过这帖，不补任何文字。
                self.ctx.logger.warning(
                    "[搬石] 长图渲染失败或太大，这帖跳过%s",
                    ("（%s）" % card_reason) if card_reason else "",
                )
                return [], []
            images.append(image_b64)
            paths.append(self._save_page_image(image_b64))
        return images, paths

    async def _localize_page(self, html: str) -> str:
        """把整页 HTML 的外链全部收掉，变成零外链页面。

        为什么非做不可：宿主的网络路由回调是
        ``functools.partial(self._handle_network_route, allow_network=...)``，而
        playwright <=1.60 的 ``wrap_handler`` 把 ``allow_network`` 也算进位置参数
        个数，于是**每一个**外链请求都抛 ``TypeError: got multiple values for
        argument 'allow_network'``。请求全部悬空 -> ``load`` 永不触发 -> 一路等到
        超时 -> 一张图都没有。贴吧原页面有八十多个外链，必炸。

        零外链化之后页面一个请求都不发，那个回调一次都不会被调用，缺陷就绕开了。
        在 1.61 以上也不亏：实测同一帖原始版 load 要 0.95~2.27 秒、零外链版只要
        0.09~0.22 秒，出图字节差 0.3%，代价只是多花 0.8~1.6 秒预下载装饰图 ——
        而这笔下载本来也要发生，只是从浏览器挪到了插件里。

        下不动、超单图上限、超总预算的图一律换成 1x1 透明占位：**宁可少一个装饰图，
        也要让页面渲出来**。长图渲不出来整帖就没了，缺一个图标没人看得出来。
        """
        client = self._client
        if client is None or not html:
            return html
        styles_wanted, images_wanted = page_asset_urls(html)
        if not styles_wanted and not images_wanted:
            return html
        started = time.monotonic()
        sem = asyncio.Semaphore(_PAGE_ASSET_CONCURRENCY)

        async def _one(url: str, limit: int) -> tuple[str, bytes]:
            async with sem:
                try:
                    body = await client.fetch_asset(url, max_bytes=limit)
                except StoneBlockedError:
                    return url, b""
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.debug("[搬石] 取页面资源失败 %s：%s", url, exc)
                    return url, b""
            return url, (body or b"")

        styles: dict[str, str] = {}
        if styles_wanted:
            got = await asyncio.gather(
                *(_one(u, _PAGE_CSS_MAX_BYTES) for u in styles_wanted)
            )
            for url, body in got:
                if body:
                    styles[url] = body.decode("utf-8", "ignore")
        images: dict[str, str] = {}
        used = 0
        if images_wanted:
            got = await asyncio.gather(
                *(_one(u, _PAGE_ASSET_MAX_BYTES) for u in images_wanted)
            )
            for url, body in got:
                if not body:
                    continue
                if used + len(body) > _PAGE_ASSET_BUDGET_BYTES:
                    continue
                used += len(body)
                images[url] = "data:%s;base64,%s" % (
                    _mime_of_image(body),
                    base64.b64encode(body).decode("ascii"),
                )
        out = zero_net_html(html, styles=styles, images=images)
        self.ctx.logger.debug(
            "[搬石] 零外链化：样式 %d/%d 个 %.0f KB，图 %d/%d 个 %.0f KB，耗时 %.2fs，"
            "HTML %.2f MB",
            len(styles),
            len(styles_wanted),
            sum(len(v) for v in styles.values()) / 1024.0,
            len(images),
            len(images_wanted),
            used / 1024.0,
            time.monotonic() - started,
            len(out) / 1048576.0,
        )
        return out

    async def _render_page(self, html: str, mode: str = _PAGE_RENDER_ZERO) -> str:
        """把整页 HTML 交给宿主渲染器截成一张长图；失败返回空串。

        ``mode`` 是配置项 ``send.page_render``：

        * ``零外链``（默认）——页面先经 :meth:`_localize_page` 收成零外链（样式表内联、
          装饰图转 data:、盖 CSP），真正交给宿主的 HTML 一个请求都不发。这既是绕开
          playwright <=1.60 那个路由回调缺陷的唯一办法，在 1.61 以上也更快。
        * ``原生``——原始页面直接交给宿主浏览器，让它自己加载外链。只有 playwright
          >=1.61 才正常；<=1.60 会因为同一个缺陷每个请求都报错、最后超时。这条留着
          是给「想省掉那 0.8~1 秒预下载、且确认自己主程序够新」的人用的。

        **倍率固定，不降档。** 以前这里有一圈「渲出来太大就降档重渲」的循环，
        它建立在两个错误前提上：

        1. 以为长图要 base64 进帧、单张上限 6 MiB。实际上长图发送走的是
           **本地路径**（:meth:`_send_page_image` 的 ``custom("imageurl", {"file": …})``），
           帧里只有几百字节的字符串，发送侧根本没有体积上限。
        2. 以为 Chromium 全页截图有位图高度上限。实测 20000 px 的页面照样截得出来
           （414×20000 的 PNG 正常产出），高度也不是限制。

        真正存在的上限只有一条：渲染结果从宿主回插件的那一次 RPC，宿主
        ``transport/base.py`` 的 ``MAX_FRAME_SIZE`` 是 16 MiB，而结果里放的是
        base64（膨胀 4/3），于是**原图约 12 MB 是硬顶**，超了宿主直接抛 ValueError。
        按 ``max_floors`` 的上限 60 层估，最坏也就 3~4 MB，这条兜底实际上永远碰不到；
        留着它只是为了「万一撞上，别静默丢帖」。

        降档为什么删掉：降档是**再渲一遍**，总时间只会更长。实测倍率对时间的影响
        微乎其微（页高 7000 时 1.5x 合计 0.344s、1.0x 0.193s，只差 0.15s），而一整轮
        真实耗时 1.7~2.4 秒里，零外链化下载就占 0.8~1.07 秒 —— 真正慢的是网络，
        倍率省下的那点时间淹没在噪声里，却要拿一张肉眼可见更糊的图去换。

        **渲染失败里要分两类**：**超时**是真的等不到，原样重来只是再白等一个超时
        （云端出网才 3Mbps，慢网是常态，等不到就是等不到）；而浏览器刚崩、RPC 抖
        一下这类**瞬时故障**，宿主会在失败路径里 reset 掉浏览器实例，隔几秒重试一次
        往往就能过。所以给瞬时故障一次重试机会，超时不重试。

        另外**不能走 ``ctx.render.html2png``**。SDK 那个包装不收 ``timeout_ms``，RPC 侧
        一律按默认的 30 秒算（``runner/rpc_client.py``），而 ``render_timeout_ms`` 只传给
        宿主渲染器、管不了 RPC。插件给的单档耐心是 ``_PAGE_RENDER_TIMEOUT_MS`` 再加
        ``_RENDER_RPC_SLACK_MS`` 的 RPC 余量，比 30 秒长；走包装就会在宿主还在渲的时候
        这边先抛 ``[E_TIMEOUT] cap.call 超时`` 放弃 —— 白等一场。所以这里直接用
        ``call_host_method`` 把 RPC 超时也按渲染超时给足（这个私有通道不可用时退回
        ``ctx.render.html2png``，见 :meth:`_call_render_capability`）。
        """
        # 零外链模式下先变成零外链页面：宿主那个网络路由回调在 playwright <=1.60 上
        # 必炸，页面只要发一个请求就一路等到超时（详见 :meth:`_localize_page`）。
        # 原生模式跳过这一步，就是把原始页面直接交给浏览器。
        allow_network = mode == _PAGE_RENDER_RAW
        if not allow_network:
            html = await self._localize_page(html)
        scale = _PAGE_RENDER_SCALE
        last_reason = ""
        for attempt in range(_RENDER_TRANSIENT_RETRY + 1):
            started = time.monotonic()
            try:
                result = await self._render_html2png(
                    html, scale, _PAGE_RENDER_TIMEOUT_MS, allow_network=allow_network
                )
            except Exception as exc:  # noqa: BLE001
                last_reason = "%s: %s" % (type(exc).__name__, exc)
                self.ctx.logger.warning(
                    "[搬石] 原页面渲染抛异常（第 %d 次）：%s", attempt + 1, exc
                )
                if _looks_like_timeout(last_reason) or attempt >= _RENDER_TRANSIENT_RETRY:
                    break
                await asyncio.sleep(_RENDER_RETRY_PAUSE_S)
                continue
            elapsed = time.monotonic() - started
            image_b64 = ""
            if isinstance(result, dict):
                image_b64 = str(result.get("image_base64") or "")
            if image_b64:
                # base64 长度反推原始字节：base64 是 4 个字符存 3 字节。
                raw_bytes = len(image_b64) * 3 // 4
                if raw_bytes > _PAGE_IMAGE_HARD_LIMIT_BYTES:
                    # 12 MB 硬顶：再往下发就是宿主 RPC 抛 ValueError。按正常层数
                    # 碰不到这里（60 层最坏 3~4 MB），真撞上了只能放弃这帖。
                    self.ctx.logger.warning(
                        "[搬石] 原页面长图 %.1f MB 超过 RPC 帧上限 %.1f MB，这帖跳过",
                        raw_bytes / 1048576.0,
                        _PAGE_IMAGE_HARD_LIMIT_BYTES / 1048576.0,
                    )
                    return ""
                width = int(result.get("width") or 0)
                height = int(result.get("height") or 0)
                self.ctx.logger.info(
                    "[搬石] 原页面长图 %.2f MB（%sx，%dx%d，渲染 %.1fs）",
                    raw_bytes / 1048576.0,
                    scale,
                    width,
                    height,
                    elapsed,
                )
                return image_b64
            # 宿主没抛异常却没给图，说明它自己判失败了（超时/浏览器挂了/参数被拒）。
            # 以前的实现直接 continue，日志里只留下一句「降档后仍然太大」—— 明明一张图
            # 都没渲出来，却报「太大」，把排查方向带偏。这里把宿主的原话记下来。
            detail = ""
            if isinstance(result, dict):
                detail = str(result.get("error") or result.get("message") or result)[:200]
            else:
                detail = type(result).__name__ if result is not None else "None"
            last_reason = "%.1fs 无图 (%s)" % (elapsed, detail)
            self.ctx.logger.warning(
                "[搬石] 原页面渲染没拿到图（耗时 %.1fs）：%s", elapsed, detail
            )
            if _looks_like_timeout(detail) or attempt >= _RENDER_TRANSIENT_RETRY:
                break
            await asyncio.sleep(_RENDER_RETRY_PAUSE_S)
        self.ctx.logger.warning(
            "[搬石] 原页面长图没渲出来（%s），这帖跳过",
            last_reason or "未知",
        )
        return ""

    async def _call_render_capability(self, payload: dict[str, Any], timeout_ms: int) -> Any:
        """把一次渲染请求交给宿主，返回归一化后的结果。

        正常走 ``cap.call`` 裸调用。SDK 的 ``ctx.render.html2png`` 是薄包装，不收
        ``timeout_ms``，RPC 层一律按默认的 30 秒算（``runner/rpc_client.py``），而插件
        给的单档耐心比 30 秒长 —— 一超时就是这边先放弃、宿主其实还在渲，白等一场。
        ``cap.call`` 在 SDK 的 ``_ALLOWED_RAW_HOST_METHODS`` 里，是它明确放行的裸调用。

        但 ``_normalize_capability_result`` 带下划线，属于 SDK 内部接口，升级时可能改名
        或消失。所以这里先做能力探测：两个都在才走上面那条路，否则退回公开的
        ``ctx.render.html2png`` —— 那条路的 RPC 超时写死 30 秒，长页面可能渲不出来，
        但插件不会因为 SDK 换了个内部方法名就整条渲染链路失效。
        """
        raw_call = getattr(self.ctx, "call_host_method", None)
        normalize = getattr(self.ctx, "_normalize_capability_result", None)
        if callable(raw_call) and callable(normalize):
            result = await raw_call("cap.call", payload=payload, timeout_ms=timeout_ms)
            return normalize("render.html2png", result)
        fallback = getattr(getattr(self.ctx, "render", None), "html2png", None)
        if not callable(fallback):
            self.ctx.logger.warning("[搬石] 当前 SDK 既没有 cap.call 通道也没有渲染能力，长图渲不出来")
            return None
        self.ctx.logger.debug("[搬石] 退回落 ctx.render.html2png（RPC 超时固定 30 秒）")
        return await fallback(**dict(payload.get("args") or {}))

    async def _render_card_html(self, html: str, scale: float) -> Any:
        """渲染自绘卡片（选择器 ``#card``）。

        和 :meth:`_render_html2png` 同样要绕开 SDK 包装的 30 秒默认超时 ——
        长帖切出来的分片一样能渲染很久。两条路都收在
        :meth:`_call_render_capability` 里。
        """
        payload = {
            "capability": "render.html2png",
            "args": {
                "html": html,
                "selector": "#card",
                "viewport": {},
                "device_scale_factor": scale,
                "full_page": True,
                "omit_background": False,
                "wait_until": "load",
                "wait_for_selector": "",
                "wait_for_timeout_ms": 0,
                "render_timeout_ms": _CARD_RENDER_TIMEOUT_MS,
                "allow_network": False,
            },
        }
        return await self._call_render_capability(
            payload, _CARD_RENDER_TIMEOUT_MS + _RENDER_RPC_SLACK_MS
        )

    async def _render_html2png(
        self, html: str, scale: float, timeout_ms: int, *, allow_network: bool = True
    ) -> Any:
        """渲染 HTML，并且让 RPC 超时跟着渲染超时走（默认 30 秒根本不够）。

        ``ctx.render.html2png`` 是 SDK 的薄包装，参数里没有 ``timeout_ms``，于是不管
        你 ``render_timeout_ms`` 传多大，RPC 层都只等 30 秒。直接调 ``cap.call`` 才能
        把超时传下去。``cap.call`` 在 SDK 的 ``_ALLOWED_RAW_HOST_METHODS`` 里，合规。

        ``allow_network`` 跟着配置项 ``send.page_render`` 走：零外链模式下页面里本来
        就没有请求了，传 False 让宿主连路由回调都不挂；原生模式必须 True。
        """
        payload = {
            "capability": "render.html2png",
            "args": {
                "html": html,
                "selector": "body",
                "viewport": {
                    "width": _PAGE_VIEWPORT_WIDTH,
                    "height": _PAGE_VIEWPORT_HEIGHT,
                },
                "device_scale_factor": scale,
                "full_page": True,
                "omit_background": False,
                "wait_until": "load",
                "wait_for_selector": "",
                "wait_for_timeout_ms": 0,
                "render_timeout_ms": timeout_ms,
                "allow_network": bool(allow_network),
            },
        }
        # 插件自己再多等一会儿：宿主判超时会先返回错误，这里只是兜住 RPC 层，
        # 免得它比宿主更早放弃、把一个还在渲的页面扔掉。
        return await self._call_render_capability(payload, timeout_ms + _RENDER_RPC_SLACK_MS)

    async def _send_cards(
        self, stream_id: str, thread: Any, detail: Any, payload: dict[str, Any]
    ) -> bool:
        """发这一帖的长图。返回 False = 长图没渲出来，这帖没发出去。

        渲不出来时**什么都不发**：既不补纯文字提要，也不退回「逐层发图文」——
        两种兜底在群里都是刷屏，内容还不完整。调用方拿到 False 就别把这帖
        记成「已搬过」，下一轮它会重来（渲染失败多半只是临时抽风）。
        """
        images, paths = await self._card_images(thread, detail, payload)
        if not images:
            return False
        for index, image_b64 in enumerate(images):
            # 单独发长图时也优先用落盘路径：走 base64 的话一张图就要过一次
            # 16MB 的 RPC 帧，路径只有两百来字节。路径为空才退回内嵌。
            page = paths[index] if index < len(paths) else ""
            await self._send_page_image(stream_id, page, image_b64)
        return True

    async def _send_page_image(self, stream_id: str, page_uri: str, image_b64: str) -> None:
        """发一张长图：有落盘路径就走路径，否则退回内嵌 base64。"""
        if page_uri:
            # 路径发不出去时**不要**重试到底再降级：这里已经有降级路径（内嵌 base64），
            # 重试三次才换路等于白等好几秒。给一次机会，失败立刻降级。
            sent = await self._gate_of().run(
                self.ctx.send.custom,
                "imageurl",
                {"file": page_uri},
                stream_id,
                storage_message=False,
                timeout_ms=_VIDEO_SEND_TIMEOUT_MS,
            )
            if sent:
                return
            self.ctx.logger.warning("[搬石] 长图按本地路径发送没成功，改用内嵌图片")
        await self._gate_of().run(
            self.ctx.send.image, image_b64, stream_id, storage_message=False
        )

    async def _forward_nodes(
        self, collected: Sequence[tuple[Any, Any]], payloads: dict[str, dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], list[tuple[Any, Any]]]:
        """把本轮所有帖子打包成一条聊天记录：文字、图片、长图、视频都装进去。

        **要盯着帧大小**。整条聊天记录是**一个** RPC 帧，上限 16MB（宿主
        ``transport/base.py`` 的 ``MAX_FRAME_SIZE``），而图片是以 base64 塞进段里的，
        体积还因为宿主的归一化翻倍（详见 ``_FORWARD_FRAME_BUDGET_BYTES``）。实测 5 帖
        x 12 楼 x 2 张 200KB 的正文图就有 31MB，5 帖各带一张 2MB 长图也有 19.6MB ——
        都必然撞上限，整条聊天记录发不出去，用户看到的是「搬了半天什么也没有」。
        所以这里维护一个预算，快满了就收窄：先少放楼层，再放弃这条记录。

        返回 ``(nodes, overflow)``：``overflow`` 是**没能进这条聊天记录**的帖子。
        调用方**不补发**，把它们交回给 ``_run_locked`` 不记账、下一轮重来 ——
        转发成功的话 ``sent`` 是 True，没人会察觉内容被截掉了，若按「已搬过」记下，
        这几条就再也补不回来。

        长图装不进预算时**整帖**退出去（``deferred``），不要在聊天记录里再补一个
        只有标题的节点：标题节点前面已经发过，补出来的是重复内容，用户看到的
        是「同一帖出现两遍、其中一遍没正文」。
        """
# 长图进不了这条聊天记录的帖子整帖放进 deferred，交回给调用方。
        # **不能只把长图丢掉、再补一个提要节点** —— 标题节点上面已经
        # 发过了，补出来的那个只会是第二次标题，用户看到的就是「有的帖子重复
        # 发送，有的没内容」（``probe_stone213_forward_dup.py`` 复现的就是它）。
        deferred: list[tuple[Any, Any]] = []
        nodes: list[dict[str, Any]] = []
        used_bytes = 0
        position = -1
        # 帧里除了图片还有别的字段（昵称、楼号文字、JSON 结构），留出余量。
        budget = _FORWARD_FRAME_BUDGET_BYTES
        for position, (thread, detail) in enumerate(collected):
            first = len(nodes)
            payload = payloads.get(thread.tid) or {}
            header = {"type": "text", "data": self._thread_header(thread)}
            nodes.append(
                {
                    "nickname": self._forward_name(),
                    "user_id": "",
                    "segments": [header],
                }
            )
            header_cost = len(str(header.get("data") or "")) + _FORWARD_NODE_OVERHEAD
            used_bytes += header_cost
            images, paths = await self._card_images(thread, detail, payload)
            if not images:
                # 长图渲不出来。不能把标题节点孤零零留在聊天记录里 ——
                # 那就是一条只有标题、没有正文的刷屏节点。整帖退出这条
                # 记录，这一轮不发它（不记账，下一轮重来）。
                self.ctx.logger.warning(
                    "[搬石] 第 %s 帖的长图没能渲染出来，退出这条聊天记录", thread.tid
                )
                del nodes[first:]
                used_bytes -= header_cost
                deferred.append((thread, detail))
                continue
            if images:
                # 长图优先用「本地路径引用」：节点里只放一个 file:// 路径，
                # 帧里是两百来字节，**与图片有多大无关**。这样一张 6MB 的长图
                # 不再吃掉整条预算，后面的帖子也不必被踢出去单独发。
                # 落盘失败（paths 对应项为空串）才退回内嵌 base64。
                segments: list[dict[str, Any]] = []
                embedded_cost = 0
                for index, b64 in enumerate(images):
                    page = paths[index] if index < len(paths) else ""
                    if page:
                        segments.append(
                            {
                                "type": "dict",
                                "data": {
                                    "type": "imageurl",
                                    "data": {"file": page},
                                },
                            }
                        )
                        # 路径段的帧开销虽然只有两百来字节，但也得记账，
                        # 否则预算算的是「内嵌部分」而不是「整条节点」。
                        embedded_cost += _PATH_SEGMENT_COST
                    else:
                        segments.append({"type": "image", "content": b64})
                        embedded_cost += len(b64)
                if used_bytes + embedded_cost > budget:
                    # 只在内嵌兜底（长图落盘失败）时才会撞预算。这帖整帖退出
                    # 这条记录，调用方不补发，留到下一轮重试。
                    self.ctx.logger.info(
                        "[搬石] 第 %s 帖的长图装不下这条聊天记录了（%d 字节），这帖退出本轮",
                        thread.tid,
                        embedded_cost,
                    )
                    del nodes[first:]
                    used_bytes -= header_cost
                    deferred.append((thread, detail))
                    continue
                used_bytes += embedded_cost
                nodes.append(
                    {
                        "nickname": self._forward_name(),
                        "user_id": "",
                        "segments": segments,
                    }
                )
            # 长图之外不再往节点里塞逐层的文字和插图：那种节点在聊天记录里
            # 就是刷屏，而且长图已经把正文和插图都带上了。这里只挂视频。
            # 视频各自单独成节点，所以它占的字节也要记进预算。
            used_bytes += self._attach_videos(nodes, first, detail, payload)
            # 预算见底，再往后只会更糟：停在这里，剩下的帖子交回去。
            # 不能就这么算了 —— 这条聊天记录待会儿照样发得出去、`sent` 会是
            # True，没有任何地方会发现内容被截掉了；把它们记成「已搬过」就再也
            # 补不回来。交回去的帖子由调用方排除记账，下一轮重来。
            if used_bytes > budget:
                self.ctx.logger.info(
                    "[搬石] 这条聊天记录已经装到 %d 字节，后面 %d 条帖子退出本轮",
                    used_bytes,
                    len(collected) - position - 1,
                )
                return nodes, list(collected[position + 1 :]) + deferred
        return nodes, deferred

    def _attach_videos(
        self,
        nodes: list[dict[str, Any]],
        first: int,
        detail: Any,
        payload: dict[str, Any],
    ) -> int:
        """把这一帖的视频放进聊天记录，**每个视频独占一个节点**。

        视频段不能和文字、图片待在同一节点里。SnowLuma 网关对合并转发里的
        视频只认「独占一条消息」，同一节点里还有别的段它就整条拒收：

            对端动作返回失败: action=send_private_forward_msg
            message=message element "video" must be the only segment in a message

        而被拒的是**整条聊天记录**（宿主把整条转发当成一次发送），不是那一个
        节点 —— 群里一条都收不到，报错却只提到 video，极难从日志上看出来。
        所以视频一律单独开节点，一个视频一个节点。

        返回这些节点占掉的帧字节数，交给调用方记账。
        """
        segments = self._video_segments(detail, payload)
        if not segments or len(nodes) <= first:
            return 0
        cost = 0
        for segment in segments:
            nodes.append(
                {
                    "nickname": self._forward_name(),
                    "user_id": "",
                    "segments": [segment],
                }
            )
            cost += _PATH_SEGMENT_COST + _FORWARD_NODE_OVERHEAD
        return cost

    def _video_segments(self, detail: Any, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """构造合并转发节点里的视频消息段（走 URL，不用 base64）。

        转发节点里的段同样要「双层」写：宿主把非 file 的自定义段一律包成
        ``DictComponent(data=内层)`` 并丢掉外层 ``type``，适配器拿到的是
        ``{"type":"dict","data":内层}``，只有内层自带 ``type`` 才认得出。
        内层用 ``videourl``（带直链），而不是 ``video`` + base64——后者会让
        整个转发帧撞上 16MB 的 RPC 单帧上限。
        """
        del payload  # 视频不再预下载，用不上
        if not bool(getattr(self._send_options(), "send_video", True)):
            return []
        segments: list[dict[str, Any]] = []
        # 只发带直链的：没直链的段发出去也是个空壳。
        sendable = [video for video in self._thread_videos(detail) if video.url]
        for video in sendable:
            segments.append(
                {
                    "type": "video",
                    "data": {
                        "type": "videourl",
                        "data": {"file": video.url},
                    },
                }
            )
        return segments

    @staticmethod
    def _thread_videos(detail: Any) -> list[Any]:
        """按楼层顺序列出这一帖里的所有视频。"""
        return [v for floor in detail.floors for v in getattr(floor, "videos", [])]

    # -- 工具 -------------------------------------------------------------

    @Tool(
        "tieba_share",
        description=(
            "去贴吧搬帖子发到当前会话。用户说「搬个石」「搬点贴吧的帖子」"
            "「去XX吧搬一条」「搬个视频帖」时用它。只读不写：逛指定的吧"
            "（不填就用配置里勾好的），取最新的帖子、跳过已经搬过的，一次最多 5 条。"
            "默认搬 1 条；发出来的样子（图文、长图还是聊天记录）由插件配置决定，"
            "不用你操心。"
        ),
        # 宿主给组件的默认 RPC 超时只有 60 秒，而逛一个吧再取帖发帖常要一两分钟。
        # 不声明的话宿主会先判超时把结果丢掉，插件这边却还在跑。
        timeout_ms=300000,
        parameters=[
            ToolParameterInfo(
                name="forum",
                param_type=ToolParamType.STRING,
                description=(
                    "贴吧的吧名，只写吧名本身、不带末尾的「吧」字，"
                    "例如「搬石」「孙笑川」。留空就用配置里勾好的吧。"
                ),
                required=False,
            ),
            ToolParameterInfo(
                name="count",
                param_type=ToolParamType.INTEGER,
                description="搬几条，默认 1，最多 5。",
                required=False,
                default=1,
            ),
            ToolParameterInfo(
                name="thread_type",
                param_type=ToolParamType.STRING,
                description=(
                    "只搬哪种帖：any＝不限（默认，用配置里的设置）；"
                    "text＝只要图文帖；video＝只要视频帖。"
                    "用户说「搬个视频帖」「只要带图的」时用它。"
                ),
                required=False,
                enum_values=[_KIND_ANY, _KIND_TEXT, _KIND_VIDEO],
                default=_KIND_ANY,
            ),
        ],
    )
    async def handle_move_tool(self, **kwargs: Any) -> dict[str, Any]:
        """模型调用的搬石工具：只发当前会话，一次一两条。

        怎么发（图文 / 长图 / 聊天记录 / 带不带链接）一律照配置走 ——
        工具只决定「搬哪条、搬几条、只搬哪种帖」这三件事。
        """
        if not bool(getattr(self.config.plugin, "enabled", False)):
            return {"success": True, "content": "搬石插件没启用，先让人在插件设置里打开。"}
        tool_cfg = getattr(self.config, "tool", None)
        if tool_cfg is not None and not bool(getattr(tool_cfg, "enabled", True)):
            return {"success": True, "content": "搬石工具被关掉了，让人在插件设置 →「工具」里打开。"}
        # 配置里没有「工具」一节时（老版本 config.toml 没这一节，宿主不会自动补），
        # tool_cfg 会是 None，下面几个 getattr 全都变成「没配」。不能让模型因此报错，
        # 也不能让「工具箱一次最多几条」这类默认值凭空消失：统一回落到这里的默认值。
        if tool_cfg is None:
            tool_cfg = getattr(self.config, "send", None)
        stream_id = str(kwargs.get("stream_id") or "").strip()
        if not stream_id:
            return {"success": False, "content": "拿不到当前会话，发不出去。"}

        forum = str(kwargs.get("forum") or "").strip()
        # 贴吧的 kw 要的是吧名本身，模型经常会顺手带上末尾的「吧」字，这里削掉
        if len(forum) > 1 and forum.endswith("吧"):
            forum = forum[:-1]
        allowed_forums = [str(x).strip() for x in (getattr(tool_cfg, "forums", None) or [])]
        allowed_forums = [x for x in allowed_forums if x]
        if forum and allowed_forums:
            # 面板上限定了吧就别让模型乱逛：比对时忽略末尾的「吧」字
            if forum not in {x[:-1] if x.endswith("吧") and len(x) > 1 else x for x in allowed_forums}:
                return {
                    "success": True,
                    "content": "这个吧不在允许范围里，能搬的是：" + "、".join(allowed_forums) + "。",
                }
        if forum and not _FORUM_NAME_RE.fullmatch(forum):
            return {
                "success": False,
                "content": "吧名看着不对，只写吧名本身（比如「搬石」），别带链接或符号。",
            }

        try:
            want = int(kwargs.get("count") or 0)
        except (TypeError, ValueError):
            want = 0
        cap = int(getattr(tool_cfg, "max_count", _TOOL_MAX_COUNT) or _TOOL_MAX_COUNT)
        cap = max(1, min(cap, _TOOL_MAX_COUNT))
        want = 1 if want <= 0 else min(want, cap)

        kind = _thread_kind(kwargs.get("thread_type"))
        # 「不限」直接留空，让 _pick_threads 走配置里那个值，别用工具的默认把配置顶掉
        only_kind = "" if kind == _FILTER_ALL else kind
        if not forum and allowed_forums:
            # 模型没说逛哪个吧：只逛允许的那些
            only_forums_arg = allowed_forums
        else:
            only_forums_arg = [forum] if forum else None

        # 同 _cmd_move：也派到后台去跑，结果由插件自己发到会话里。
        # 这里不能同步等 —— 贴吧一轮动辄一两分钟，等它跑完再回话，
        # 模型那句回复就晚了，宿主那边也可能先判超时。
        if self._busy or self._lock.locked():
            return {"success": True, "content": "上一轮还在搬，等它跑完再试。"}
        self._busy = True
        self._spawn_job(
            self._manual_round(
                stream_id,
                only_forums=only_forums_arg,
                count=want,
                only_kind=only_kind,
                trigger="工具",
                tool_options=True,
            )
        )
        # 不在会话里播报「开始搬了」：成功是常态，只有出错才需要说话。
        return {
            "success": True,
            "content": "已开始搬运，帖子与结束语由插件直接发送；成功时不再另行通知，只有出错才会说明。",
        }

    # -- 命令 -------------------------------------------------------------

    @Command(
        "搬石",
        description=(
            "麦麦贴吧搬石：把贴吧的帖子搬到群里。"
            "/搬石 按配置搬一轮 | /搬视频 只搬视频帖 | /搬石 状态 | "
            "/搬石 重置 [吧名] | /搬石 帮助（维护命令要管理员，在插件设置的「权限」里配）"
        ),
        # 命令必须出现在整段文本最前面。宿主用 search() 匹配，不锚定的话
        # 「这个 /搬石 插件不错」这种闲聊里的提及也会被当成命令。
        # 引用回复时宿主把被引用原文拼在最前，正文命令因此不再处于开头 ——
        # 那是想要的效果（引用的命令不该重跑），handler 里的引用守卫再兜一层。
        #
        # 字符类里的几处讲究，动之前先看：
        #   [^\S\n] 代替 \s —— \s 含换行，会让 rest 吞掉下一行；
        #   sub/rest 都排除 ] ） ) 】 」 』 —— 闭括号不是子命令的一部分。
        #     （早先 sub 只排除 ]，尾部带括号时正则引擎会选「让 sub 不参与、
        #      整段塞进 rest」这条路径，于是 sub 变空 → 被当成裸命令 →
        #      管理员直接搬一轮。引用场景下这就是「想查状态结果搬了一轮」。）
        #   \Z 代替 $ —— $ 认末尾那个换行之前的位置，会跨行匹配。
        #
        # 这里刻意用 public 而不是 operator：宿主的 operator 拦截发生在
        # 插件 handler 之前（src/chat/message_receive/bot.py:295），判据只有
        # [plugin] permission 和隐藏的 command_permissions，插件配置里那份
        # 「管理员」名单根本没机会被读到 —— 用户在插件面板填了 QQ 号却被告知
        # 「你没有权限使用此命令。」，就是这个原因。改成 public 由下面的
        # _may_move / _may_maintain 自己判，名单语义才和面板一致。
        pattern=r"^[/／]搬石(?:[^\S\n]+(?P<sub>[^\s/／\]）)】」』]+))?(?:[^\S\n]+(?P<rest>[^\n\]）)】」』]*?))?[^\S\n]*[\]）)】」』]?\Z",
        permission="public",
    )
    async def handle_stone(self, **kwargs: Any) -> tuple[bool, str, int]:
        matched = kwargs.get("matched_groups") or {}
        sub = str(matched.get("sub") or "").strip()
        rest = str(matched.get("rest") or "").strip()
        stream_id = str(kwargs.get("stream_id") or "")
        if not stream_id:
            return False, "缺少聊天流 ID", 1
        # 引用一条写着「/搬石 …」的消息时，宿主会把被引用原文拼进这次文本，
        # 于是命令被"带"进来又执行一遍。检测到命令来自引用部分就放行不处理。
        full_text = _command_text(kwargs)
        quoted = full_text and (
            _quoted_command(full_text, "/搬石") or _quoted_command(full_text, "／搬石")
        )
        if quoted:
            self.ctx.logger.info("[搬石] 命令出现在被引用内容里，跳过：%r", full_text[:80])
            return True, "引用内容里的命令，已跳过", 1
        # 同一条消息只处理一次：宿主超时后重投、或引用带出同一条命令时，
        # 不能让同一句话再搬一轮。
        message_id = _command_message_id(kwargs)
        if message_id:
            if message_id in self._seen_message_ids:
                self.ctx.logger.info("[搬石] 同一条消息重复进入命令链，跳过：%s", message_id)
                return True, "重复消息，已跳过", 1
            self._seen_message_ids.append(message_id)
        platform = str(kwargs.get("platform") or "")
        user_id = str(kwargs.get("user_id") or "")
        local_operator = kwargs.get("is_local_operator") is True
        can_move = await self._may_move(
            platform, user_id, stream_id, local_operator, str(kwargs.get("group_id") or "")
        )
        # 维护权限**不能**写成 ``can_move or await self._may_maintain(...)``：
        # can_move 为真时 ``or`` 直接短路，_may_maintain 根本不会被调用，于是
        # 「可搬石的群」里的任何群成员都被当成管理员 —— 能看状态、能清空全站
        # 搬运记录、能解除反爬冷却。两条权限本来就是分开的（见 _may_maintain
        # 的说明：可搬石的群只给搬运权限），这里必须单独问一次。
        is_admin = await self._may_maintain(
            platform, user_id, stream_id, local_operator
        )
        low = sub.lower()
        if not sub:
            if can_move:
                return await self._cmd_move(stream_id)
            await self.ctx.send.text(_MOVE_DENIED, stream_id, storage_message=False)
            return True, "没有权限", 1
        if low in ("帮助",):
            await self.ctx.send.text(_HELP_TEXT, stream_id, storage_message=False)
            return True, "用法说明", 1
        if low in ("状态",):
            if not is_admin:
                await self.ctx.send.text(_MAINTENANCE_DENIED, stream_id, storage_message=False)
                return True, "维护命令需要管理员权限", 1
            await self.ctx.send.text(self._status_text(), stream_id, storage_message=False)
            return True, "状态", 1
        if low in ("重置",):
            if not is_admin:
                await self.ctx.send.text(_MAINTENANCE_DENIED, stream_id, storage_message=False)
                return True, "维护命令需要管理员权限", 1
            # 「/搬石 重置 冷却」单独用来解除反爬冷却：记录照旧留着，
            # 否则用户想把冷却清掉就只能连搬运记录一起清。
            if rest in ("冷却", "反爬"):
                if self._client is not None:
                    self._client.reset_block()
                await self.ctx.send.text(
                    "反爬冷却已解除，下次搬石会重新请求贴吧。",
                    stream_id,
                    storage_message=False,
                )
                return True, "冷却已解除", 1
            count = self._store.reset(rest) if self._store is not None else 0
            if self._store is not None:
                self._store.flush()
            await self.ctx.send.text(
                "已清空 %d 条搬运记录%s，下次会重新搬。" % (count, ("（%s）" % rest) if rest else ""),
                stream_id,
                storage_message=False,
            )
            return True, "记录已重置", 1
        await self.ctx.send.text(
            "不认识的子命令，打「/搬石 帮助」看看。", stream_id, storage_message=False
        )
        return True, "未知子命令", 1

    @Command(
        "搬视频",
        description=(
            "麦麦贴吧搬石：只搬视频帖，不看配置里的「只搬哪种帖」。"
            "/搬视频 立刻搬一轮视频帖（要管理员，在插件设置的「权限」里配）"
        ),
        # 同一个正则套路，只把命令名换成「搬视频」，后缀仍收进 rest 备用。
        pattern=r"^[/／]搬视频[^\S\n]*(?P<rest>[^\n\]）)】」』]*?)[^\S\n]*[\]）)】」』]?\Z",
        # 同上：public + handler 内自判，避免宿主抢先拦截。
        permission="public",
    )
    async def handle_video(self, **kwargs: Any) -> tuple[bool, str, int]:
        """「/搬视频」：只搬视频帖。

        用户说「举个例子 /搬视频 就是只搬视频内容，/搬石 就是按照配置内容来」，
        所以这条命令不读配置里的「只搬哪种帖」，一律按视频筛。
        """
        guard = self._command_guard(kwargs, "/搬视频")
        if guard is not None:
            return guard
        stream_id = str(kwargs.get("stream_id") or "")
        if not stream_id:
            return False, "缺少聊天流 ID", 1
        can_move = await self._may_move(
            str(kwargs.get("platform") or ""),
            str(kwargs.get("user_id") or ""),
            stream_id,
            kwargs.get("is_local_operator") is True,
            str(kwargs.get("group_id") or ""),
        )
        if not can_move:
            await self.ctx.send.text(_MOVE_DENIED, stream_id, storage_message=False)
            return True, "维护命令需要管理员权限", 1
        return await self._cmd_move(stream_id, only_kind=_FILTER_VIDEO)

    def _command_guard(self, kwargs: dict[str, Any], command: str) -> tuple[bool, str, int] | None:
        """命令入口的两道公共守卫：引用不算数、同一条消息只处理一次。

        返回 None 表示放行；返回三元组就是直接交给宿主的应答。
        """
        full_text = _command_text(kwargs)
        if full_text and (
            _quoted_command(full_text, command) or _quoted_command(full_text, command.replace("/", "／"))
        ):
            self.ctx.logger.info("[搬石] 命令出现在被引用内容里，跳过：%r", full_text[:80])
            return True, "引用内容里的命令，已跳过", 1
        message_id = _command_message_id(kwargs)
        if message_id:
            if message_id in self._seen_message_ids:
                self.ctx.logger.info("[搬石] 同一条消息重复进入命令链，跳过：%s", message_id)
                return True, "重复消息，已跳过", 1
            self._seen_message_ids.append(message_id)
        return None

    async def _cmd_move(self, stream_id: str, *, only_kind: str = "") -> tuple[bool, str, int]:
        """把这一轮丢到后台跑，自己立刻返回。

        宿主给命令 60 秒，而一轮搬石常常超过它。同步等的话宿主会判超时并把结果
        丢掉，可插件这边还在跑 —— 搬运照常发生，用户却什么也看不到。所以命令
        本身只负责应答，搬运交给后台任务。

        **成功时不说话**：帖子（和配置里的结束语）由后台任务直接发进群，再补一
        句「开始搬了」「搬了 N 条」都是刷屏。只有出错时后台任务才会发一条说明。

        only_kind 非空时只搬那一种帖子（「/搬视频」用）。
        """
        if self._busy or self._lock.locked():
            await self.ctx.send.text(
                "上一轮还在搬，等它跑完再来。", stream_id, storage_message=False
            )
            return True, "上一轮还没跑完", 1
        self._busy = True
        self._spawn_job(self._manual_round(stream_id, only_kind=only_kind))
        return True, "已开始搬运", 1

    async def _manual_round(
        self,
        stream_id: str,
        *,
        only_forums: list[str] | None = None,
        count: int = 0,
        only_kind: str = "",
        trigger: str = "手动",
        tool_options: bool = False,
    ) -> None:
        """后台跑的那一轮：**只有出错**才往群里发一句说明。

        命令入口只传 stream_id；模型工具入口还会带上吧名、条数和只要哪种帖。

        成功时不吭声：帖子和结束语已经由 ``_run_locked`` 直接发进群了，再补一句
        「本轮搬了 N 条」除了刷屏没有任何信息量。出错则必须说 —— 否则用户只看到
        「这轮什么都没发生」，还以为是没够门槛。
        """
        try:
            summary = await self._run_round(
                trigger,
                streams=[stream_id],
                only_forums=only_forums,
                count=count,
                only_kind=only_kind,
                tool_options=tool_options,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[搬石] 手动轮次异常：%s", exc, exc_info=True)
            # 报错信息写完整：异常类型 + 原始消息一个字都不删。直发到群里
            # 一长条报错会刷屏，所以下面用聊天记录包起来 —— 正因为包起来了，
            # 才有余地把它写全，不必为了短而截断。
            summary = _RoundSummary(
                "这轮搬石出错了。\n\n%s: %s\n\n完整堆栈在主程序日志里。"
                % (type(exc).__name__, exc),
                error=True,
            )
        if not getattr(summary, "error", False):
            self.ctx.logger.info("[搬石] 手动一轮结束：%s", summary)
            return
        try:
            await self._send_report(stream_id, summary)
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[搬石] 手动结果发不出去：%s", exc)

    async def _send_report(self, stream_id: str, text: str) -> None:
        """把一条说明**打包成聊天记录**发进群，装不下才退回直发。

        为什么要包起来：一轮失败的说明可能有好几行（异常类型、原始消息、
        被贴吧拦住的原因……），直发就是一大坨文字顶在群里刷屏，而且发出去就
        收不回来。聊天记录折叠成一条，点开才看得见，群里只多一行。

        也正因为包起来了，报错信息可以写完整、不用截断 —— 直发时为了少刷屏
        不得不压缩成一句话，反而看不出到底哪一步错了。

        聊天记录发不出去（帧太大、宿主抖动）就退回直发：宁可刷屏，也不能让
        用户以为「这轮什么都没发生」。这条兜底只走一次 —— 转发本身不重试
        （``_FORWARD_RETRY_TIMES``），否则「没拿到回执」就会被放大成好几条。
        """
        forward = getattr(self.ctx.send, "forward", None)
        if forward is not None:
            nodes = [
                {
                    "nickname": self._forward_name(),
                    "user_id": "",
                    "segments": [{"type": "text", "data": str(text)}],
                }
            ]
            ok = await self._gate_of().run(
                forward,
                nodes,
                stream_id,
                timeout_ms=_FORWARD_TIMEOUT_MS,
                storage_message=False,
                _retries=_FORWARD_RETRY_TIMES,
            )
            if ok:
                return
            self.ctx.logger.warning("[搬石] 说明的聊天记录没发出去，改成直发")
        await self._gate_of().run(
            self.ctx.send.text, str(text), stream_id, storage_message=False
        )

    def _status_text(self) -> str:
        cfg = self.config
        lines = ["麦麦贴吧搬石 · 状态"]
        lines.append(
            "版本 %s｜自动搬石 %s｜呈现 %s"
            % (
                _PLUGIN_VERSION,
                "开" if cfg.schedule.auto_enabled else "关",
                self._mode_label(),
            )
        )
        forums = resolve_forums(cfg.stone)
        lines.append("要逛的吧：" + ("、".join(forums) if forums else "（没配）"))
        if cfg.schedule.daily_times:
            lines.append("定时：每天 " + "、".join(str(x) for x in cfg.schedule.daily_times))
        else:
            base = str(getattr(cfg.schedule, "base_time", "") or "").strip() or "0:00"
            lines.append(
                "定时：从 %s 起每 %d 分钟" % (base, int(cfg.schedule.interval_minutes))
            )
        if self._lead_seconds():
            lines.append("提前 %d 秒开搬，到点再发" % int(self._lead_seconds()))
        bad = _bad_daily_times(cfg.schedule.daily_times)
        if bad:
            # 认不出来的时刻会被丢掉，剩下几个照跑；不说的话用户只会觉得「怎么没按我设的来」
            lines.append(
                "⚠ 这几个时刻没认出来、不会生效：" + "、".join(bad) + "（写成 9:30 这样就行）"
            )
        stats = self._store.stats() if self._store is not None else {}
        if not stats:
            lines.append("还没有搬过任何帖子。")
        else:
            lines.append("搬运记录：")
            for forum, info in sorted(stats.items()):
                lines.append(
                    "· %s：最近 %d 条在档，累计 %d 条，最后 %s"
                    % (
                        forum,
                        int(info.get("recent") or 0),
                        int(info.get("total") or 0),
                        str(info.get("last_run") or "（没记）"),
                    )
                )
        if self._client is not None:
            left = self._client.blocked_remaining()
            if left > 0:
                lines.append("反爬冷却中，还剩 %d 秒。" % int(left))
        lines.append("权限：" + self._admin_label())
        return "\n".join(lines)

    def _admin_label(self) -> str:
        """状态里回显谁能用命令 —— 免得「说管理员能用却没地方配」。"""
        section = getattr(self.config, "admin", None)
        names = sorted(_admin_ids(getattr(section, "admins", None)))
        groups = sorted(_group_set(getattr(section, "allow_groups", None)))
        parts: list[str] = []
        parts.append("管理员 " + "、".join(names) if names else "管理员（还没填）")
        if groups:
            parts.append("可搬石的群 " + "、".join(groups))
        parts.append("主程序终端")
        return "；".join(parts)
    async def _may_move(
        self,
        platform: str,
        user_id: str,
        stream_id: str,
        local_operator: bool,
        group_id: str = "",
    ) -> bool:
        """能不能用「搬石 / 搬视频」这类立刻搬运的命令。

        管理员、「可搬石的群」里的任何人、主程序终端，以及两张名单都留空时
        回落到宿主 ``[plugin] permission`` 名单。

        ``group_id`` 是**群号**，由宿主单独给（``invoke_args["group_id"]``）。
        不能拿 ``stream_id`` 当群号用 —— 那是宿主按 platform/account/群号算出来的
        md5 会话 id，和面板里填的群号永远对不上，「可搬石的群」会整条失效。
        只有宿主没给 group_id 时（例如私聊）才退回从 stream_id 里抠数字。
        """
        if local_operator:
            return True
        section = getattr(self.config, "admin", None)
        uid = _clean_id(user_id)
        if uid and uid in _admin_ids(getattr(section, "admins", None)):
            return True
        group = _clean_id(group_id) or _clean_id(stream_id)
        if group and group in _group_set(getattr(section, "allow_groups", None)):
            return True
        return await self._host_permission_allows(platform, user_id)

    async def _may_maintain(
        self, platform: str, user_id: str, stream_id: str, local_operator: bool
    ) -> bool:
        """维护类命令（状态 / 重置）能不能执行。

        只认管理员名单与主程序终端；「可搬石的群」只给搬运权限，不给维护权限。
        """
        if local_operator:
            return True
        section = getattr(self.config, "admin", None)
        uid = _clean_id(user_id)
        if uid and uid in _admin_ids(getattr(section, "admins", None)):
            return True
        # 管理员名单填了人就按名单说了算；没填时才看宿主名单。
        if _admin_ids(getattr(section, "admins", None)):
            return False
        return await self._host_permission_allows(platform, user_id)

    async def _host_permission_allows(self, platform: str, user_id: str) -> bool:
        """回落到宿主 ``[plugin] permission``。两张名单填了任意一张就不看它。
        """
        section = getattr(self.config, "admin", None)
        if _admin_ids(getattr(section, "admins", None)) or _group_set(
            getattr(section, "allow_groups", None)
        ):
            return False
        scoped = _scoped_user_id(platform, user_id)
        if not scoped:
            return False
        try:
            configured = await self.ctx.config.get("plugin.permission", [])
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[搬石] 读 [plugin] permission 失败：%s", exc)
            return False
        if not isinstance(configured, (list, tuple, set, frozenset)):
            return False
        return scoped in _permission_set(configured)

def create_plugin() -> StonePlugin:
    return StonePlugin()
