"""麦麦贴吧搬石 —— 贴吧客户端接口（protobuf）的字段表与解析。

贴吧手机版走的是 `tiebac.baidu.com` 上的两个客户端接口，收发都是 protobuf：

  * 列表：`POST /c/f/frs/page?cmd=301001`
  * 楼层：`POST /c/f/pb/page?cmd=302001`

相比抓 HTML（旧路线），这条路有三个好处，都是实测出来的，不是推测：

1. **点赞数真的拿得到** —— HTML 详情页里根本没有 `agree_num`（实测 3 个样本
   命中数都是 0），列表页的内联 JSON 里才有；而 protobuf 的 `ThreadInfo.agree`
   和 `Post.agree` 两个层级都有，跟 HTML 列表页给的数字逐一相同。
2. **能翻页** —— HTML 的 `kz=` 页面加 `pn=` 完全无效（实测 pn=1/2/99 返回同一批
   楼层），protobuf 的 `pn` 是真的页码，`total_page` 也给了。
3. **不需要登录** —— 连楼中楼（`with_floor=1`）都不用带 BDUSS。网上流传的
   「取楼中楼必须登录」是拿 aiotieba 的通用注释当结论，实测无 BDUSS 一样拿得到。

字段号全部来自对真实响应的逐字段比对，**不是从 .proto 文件抄的**：那些 .proto
在 aiotieba 仓库里，字段号对不上时以实测为准。响应里出现过的字段在下面都有注释，
没确认过的字段一律不猜 —— 读不到就是默认值，宁可少显示也不要显示错的。
"""

from __future__ import annotations

from typing import Any

from st_proto import Message, ProtobufError

# --- 请求字段号 ---------------------------------------------------------------

# CommonReq（两个接口共用）

# PbPageReqIdl：楼层页请求
PAGE_CMD = 302001
PAGE_URL = "http://tiebac.baidu.com/c/f/pb/page"
REQ_PAGE_DATA = 1        # PbPageReqIdl.data
REQ_PAGE_COMMON = 25     # DataReq.common
REQ_PAGE_COMMON_TYPE = 1       # CommonReq._client_type
REQ_PAGE_COMMON_VERSION = 2    # CommonReq._client_version
REQ_PAGE_KZ = 4          # DataReq.kz（帖子 id，int64）
REQ_PAGE_LZ = 5          # DataReq.lz（只看楼主）
REQ_PAGE_SORT = 6        # DataReq.r（楼层排序）
REQ_PAGE_PID = 7         # DataReq.pid
REQ_PAGE_WITH_FLOOR = 8  # DataReq.with_floor（1 = 带楼中楼）
REQ_PAGE_FLOOR_RN = 9    # DataReq.floor_rn（每层带几条楼中楼）
REQ_PAGE_RN = 13         # DataReq.rn（一页几层）
REQ_PAGE_PN = 18         # DataReq.pn（第几页）
REQ_PAGE_FLOOR_SORT = 74 # DataReq.floor_sort_type（1 = 楼中楼按热度）

# FrsPageReqIdl：列表页请求
FRS_CMD = 301001
FRS_URL = "http://tiebac.baidu.com/c/f/frs/page"
REQ_FRS_DATA = 1         # FrsPageReqIdl.data
REQ_FRS_KW = 1           # DataReq.kw（吧名，不带「吧」字）
REQ_FRS_RN = 2           # DataReq.rn
REQ_FRS_RN_NEED = 3      # DataReq.rn_need
REQ_FRS_IS_GOOD = 4      # DataReq.is_good（1 = 只要精品帖）
REQ_FRS_PN = 15          # DataReq.pn（第几页）
REQ_FRS_COMMON = 39      # DataReq.common
REQ_FRS_SORT = 47        # DataReq.sort_type（0=回复时间 1=发布时间；**2 会返回 0 条**）

# --- 响应字段号 ---------------------------------------------------------------

# 最外层：error / data
RES_ERROR = 1            # Error（errno 在 1）
RES_DATA = 2
ERROR_ERRNO = 1          # Error.errno，0 才算成功
ERROR_MSG = 2            # Error.errmsg

# FrsPageResIdl.DataRes
FRS_FORUM = 2            # forum
FRS_FORUM_NAME = 2       # Forum.name
FRS_FORUM_MEMBERS = 9    # Forum.member_num
FRS_PAGE = 4             # page（Page 消息）
FRS_THREAD_LIST = 7      # thread_list（重复）
FRS_USER_LIST = 17       # user_list（重复）

# ThreadInfo
TH_ID = 1
TH_TITLE = 3
TH_REPLY_NUM = 4
TH_VIEW_NUM = 5
TH_IS_TOP = 9
TH_IS_GOOD = 10
TH_THREAD_TYPE = 26      # 40 = 视频帖
TH_AUTHOR = 18           # 作者（内嵌 User，实测**可能为空**，要用 author_id 去 user_list 里找）
TH_CREATE_TIME = 45
TH_AUTHOR_ID = 56
TH_VIDEO_INFO = 79
TH_AGREE = 126           # Agree 消息
TH_SHARE_NUM = 135

# PbPageResIdl.DataRes
PAGE_FORUM = 2
PAGE_PAGE = 3            # page（Page 消息）
PAGE_POST_LIST = 6       # post_list（重复）
PAGE_THREAD = 8          # thread（这个帖子的 ThreadInfo）
PAGE_USER_LIST = 13      # user_list（重复）

# Post（一层楼）
POST_ID = 1              # 楼层 id（不是楼层号！）
POST_FLOOR = 3           # 楼层号 —— **实测几乎全是 0，不能用作楼层号**
POST_TIME = 4
POST_TITLE = 2           # 标题 / 摘要（首楼是标题，其余楼层是「回复：标题」）—— **不是正文**
POST_CONTENT = 5         # 重复的 PbContent —— **这才是正文**
POST_SUB_POST_NUMBER = 13 # 楼中楼总数
POST_SUB_POST_LIST = 15  # 楼中楼（重复 SubPost）
POST_SIGNATURE = 21
POST_AUTHOR_ID = 19      # 作者 user_id —— **这才是拿昵称的正路**
POST_AGREE = 37          # Agree
POST_TID = 46

# SubPost / SubPostList（楼中楼）
SUB_PID = 1              # SubPost.pid
SUB_LIST = 2             # SubPost.sub_post_list（重复 SubPostList）
SUBL_ID = 1              # SubPostList.id
SUBL_SHELL = 2           # **内嵌 protobuf**（不是 PbContent 列表），见 comment_text
SUBL_TIME = 3
SUBL_AUTHOR_ID = 4       # 作者 user_id
SUBL_TITLE = 5           # 实测恒为空
SUBL_FLOOR = 6           # 实测恒为 0
SUBL_AGREE = 9           # Agree —— **实测该字段不存在**，楼中楼没有点赞数

# 楼中楼那层内嵌壳（SUBL_SHELL 解开后）
SHELL_TYPE = 1           # 0=文本
SHELL_TEXT = 2           # 正文（**直接是字符串**，不是 PbContent）

# PbContent（正文里的一段）
PC_TYPE = 1              # 0=文本(proto3 省零值) 2=表情 3=图片，实测分布 {0:28, 2:12, 3:2}
PC_TEXT = 2
PC_LINK = 3
PC_SRC = 4               # 缩略图
PC_BSIZE = 5
PC_CDN_SRC = 8           # 720 宽那档
PC_BIG_CDN_SRC = 9       # 大图
PC_C = 11
PC_EMOJI_NAME = 11      # 表情中文名（如「阴险」）—— field2 只是 image_emoticonNN
PC_VOICE_MD5 = 12
PC_DURING_TIME = 13
PC_UID = 15
PC_WIDTH = 18
PC_HEIGHT = 19
PC_ORIGIN_SRC = 25       # 原图（最清晰，见 pick_image_url）
PC_ORIGIN_SIZE = 27
PC_COUNT = 28

# Agree
AGREE_NUM = 1
AGREE_DISAGREE = 4

# User（在顶层 user_list 里，靠 id 关联）
USER_ID = 2
USER_NAME = 3            # 用户名（登录名）
USER_NAME_SHOW = 4       # 显示名（昵称）—— 优先用它
USER_PORTRAIT = 5
USER_LEVEL = 23

# Page —— 实测 field1=每页条数(30) field3=当前页 field9=总页数 field12=总条数(90)
PAGE_RN = 1
PAGE_CURRENT = 3
PAGE_TOTAL = 9
PAGE_TOTAL_COUNT = 12

# 正文片段类型（PbContent.type）—— 实测取值只有 0/2/3
PC_TYPE_TEXT = 0         # proto3 省略零值，文本段通常连 field1 都不写
PC_TYPE_EMOJI = 2        # field2 是英文名 image_emoticonNN，field11 才是中文名
PC_TYPE_IMAGE = 3
PC_TYPE_AT = 4           # 实测样本里没出现，保留兼容
PC_TYPE_VIDEO = 5        # 实测样本里没出现，保留兼容

# 客户端标识：这两个值是从真实 App 抓包里抄下来的固定串，换掉会被服务端拒。
CLIENT_TYPE = 2
CLIENT_VERSION = "12.64.1.1"
PROTO_UA = "miku/39"
PROTO_BOUNDARY = "-*_r1999"
from st_proto import (
    Message,
    ProtobufError,
    encode_bytes,
    encode_message,
    encode_uint,
)

def build_frs_request(
    forum: str, *, page: int = 1, rn: int = 30, sort: int = 0, is_good: bool = False
) -> bytes:
    """打包一个「取某吧帖子列表」的请求。

    Args:
        forum: 吧名，**不带末尾的「吧」字**（带「吧」实测返回 0 条）。
        page: 第几页，从 1 开始。
        rn: 一页几条。
        sort: 0=按回复时间、1=按发布时间。**别用 2（热门）**：实测返回 0 条，
            贴吧的热门只在有热门分区的吧生效，这里没有可用的判据。
        is_good: 只要精品帖。
    """
    data = encode_bytes(REQ_FRS_KW, forum)
    data += encode_uint(REQ_FRS_RN, rn)
    data += encode_uint(REQ_FRS_RN_NEED, rn)
    data += encode_uint(REQ_FRS_IS_GOOD, 1 if is_good else 0)
    data += encode_uint(REQ_FRS_PN, max(1, int(page)))
    data += encode_message(REQ_FRS_COMMON, _common())
    data += encode_uint(REQ_FRS_SORT, int(sort))
    return encode_message(REQ_FRS_DATA, data)


def build_page_request(
    tid: str,
    *,
    page: int = 1,
    rn: int = 30,
    with_floor: bool = False,
    floor_rn: int = 0,
    sort: int = 0,
    only_author: bool = False,
) -> bytes:
    """打包一个「取某帖楼层」的请求。

    Args:
        tid: 帖子 id。
        page: 第几页（从 1 开始）。protobuf 的 pn 是真页码，跟 HTML 那条死路不同。
        rn: 一页几层。
        with_floor: 要不要带楼中楼。**不带 BDUSS 也能拿**，别被网上「必须登录」的说法劝退。
        floor_rn: 每层带几条楼中楼。只在 with_floor 为真时有意义。
        sort: 楼层排序，0 = 按时间。
        only_author: 只看楼主。
    """
    data = encode_message(REQ_PAGE_COMMON, _common())
    data += encode_uint(REQ_PAGE_KZ, int(tid))
    data += encode_uint(REQ_PAGE_LZ, 1 if only_author else 0)
    data += encode_uint(REQ_PAGE_SORT, int(sort))
    data += encode_uint(REQ_PAGE_WITH_FLOOR, 1 if with_floor else 0)
    data += encode_uint(REQ_PAGE_FLOOR_RN, max(0, int(floor_rn)))
    data += encode_uint(REQ_PAGE_RN, rn)
    data += encode_uint(REQ_PAGE_PN, max(1, int(page)))
    # 楼中楼按热度排，服务端原生支持。实测不带这个参数也返回楼中楼，
    # 但顺序是时间序；带上才可能把高赞的顶上来。拿不到高赞数时也无害。
    data += encode_uint(REQ_PAGE_FLOOR_SORT, 1)
    return encode_message(REQ_PAGE_DATA, data)


def _common() -> bytes:
    """两个接口共用的 CommonReq 包头。版本串是写死的，改了服务端会拒。"""
    return encode_uint(REQ_PAGE_COMMON_TYPE, CLIENT_TYPE) + encode_bytes(
        REQ_PAGE_COMMON_VERSION, CLIENT_VERSION
    )


def multipart_body(payload: bytes) -> tuple[bytes, str]:
    """把请求体包成 multipart/form-data，返回 (字节, Content-Type)。

    客户端就是 multipart 发这个 protobuf 的：字段名叫 ``data``、文件名叫 ``file``、
    boundary 是固定的 ``-*_r1999``。三样都对不上服务端就当没收到，实测过。
    """
    boundary = PROTO_BOUNDARY
    head = (
        "--%s\r\n"
        'Content-Disposition: form-data; name="data"; filename="file"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ) % boundary
    tail = "\r\n--%s--\r\n" % boundary
    body = head.encode("utf-8") + payload + tail.encode("utf-8")
    return body, "multipart/form-data; boundary=%s" % boundary


def proto_headers() -> dict[str, str]:
    """客户端接口要的请求头。少一个都会被当成普通网页请求。"""
    return {
        "User-Agent": PROTO_UA,
        "x_bd_data_type": "protobuf",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


# --- 响应解析 -----------------------------------------------------------------


def _errno(msg: Message) -> tuple[int, str]:
    """读最外层的 error。返回 (errno, 错误消息)。"""
    err = msg.message(RES_ERROR)
    return err.uint(ERROR_ERRNO), err.text(ERROR_MSG)


def _user_map(msg: Message, field: int) -> "dict[int, Message]":
    """把顶层 user_list 摊成 ``{user_id: User}``。

    这是整条路线最关键的一步：``Post`` 消息里**没有**作者昵称字段（实测 field 23
    根本不存在），昵称全在 user_list 里，靠 ``author_id`` 关联。不做这层映射，
    所有楼层都会没有作者名。
    """
    out: "dict[int, Message]" = {}
    for user in msg.messages(field):
        uid = user.uint(USER_ID)
        if uid:
            out[uid] = user
    return out


def _display_name(user: Message) -> str:
    """昵称：优先 name_show（用户自己起的显示名），退回 name（登录名）。"""
    return (user.text(USER_NAME_SHOW) or user.text(USER_NAME)).strip()


def _agree_num(msg: Message, field: int) -> int:
    """读 Agree 消息里的同意数。Agree 缺席或字段缺失都给 0。"""
    if not msg.has(field):
        return 0
    return msg.message(field).uint(AGREE_NUM)


def _page_info(msg: Message, field: int) -> tuple[int, int, int]:
    """读 Page：返回 (当前页, 总页数, 总条数)。缺字段给 0。

    字段号是实测出来的，跟一开始按 .proto 猜的完全不同：
    ``field1`` 是每页条数（恒为请求的 rn），``field3`` 才是当前页，
    ``field9`` 是总页数，``field12`` 是总条数。
    """
    page = msg.message(field)
    return (
        page.uint(PAGE_CURRENT),
        page.uint(PAGE_TOTAL),
        page.uint(PAGE_TOTAL_COUNT),
    )

def parse_frs_response(raw: bytes, *, forum: str = "") -> tuple[list[dict[str, Any]], str]:
    """解析列表页响应，返回 (帖子字典列表, 错误说明)。

    出任何问题都返回空列表 + 一句为什么，不抛异常：调用方是每小时跑一次的定时任务，
    一次解析失败应该只让这一轮少发几条，不该让整个插件循环崩掉。
    """
    try:
        msg = Message.load(raw)
    except ProtobufError as exc:
        return [], "响应不是 protobuf：%s" % exc
    errno, errmsg = _errno(msg)
    if errno:
        return [], "errno=%s %s" % (errno, errmsg or "（无错误信息）")
    data = msg.message(RES_DATA)
    if not data:
        return [], "响应里没有 data"
    users = _user_map(data, FRS_USER_LIST)
    out: list[dict[str, Any]] = []
    # 逐条 try：函数 docstring 承诺「不抛异常」，但下面每一项都要读十来个字段，
    # 字段形状一变（比如某个 text 字段突然变成嵌套 message）就会在**中间**炸掉，
    # 结果是这一页前面已经解析好的帖子也一起丢掉、整轮少发。单条坏了就跳过它。
    for th in data.messages(FRS_THREAD_LIST):
        try:
            tid = str(th.uint(TH_ID) or "")
            if not tid:
                continue
            author_id = th.uint(TH_AUTHOR_ID)
            name = _display_name(users.get(author_id, Message()))
            if not name:
                # 列表页的 thread_list 里也可能自带 author，兜底用一下
                name = _display_name(th.message(TH_AUTHOR))
            out.append(
                {
                    "tid": tid,
                    "title": th.text(TH_TITLE),
                    "forum": forum,
                    "is_top": bool(th.uint(TH_IS_TOP)),
                    "is_good": bool(th.uint(TH_IS_GOOD)),
                    "replies": th.uint(TH_REPLY_NUM),
                    "views": th.uint(TH_VIEW_NUM),
                    "agree": _agree_num(th, TH_AGREE),
                    "thread_type": th.uint(TH_THREAD_TYPE),
                    "author_id": author_id,
                    "author": name,
                    "create_time": th.uint(TH_CREATE_TIME),
                    "has_video": bool(th.has(TH_VIDEO_INFO)),
                }
            )
        except Exception:  # noqa: BLE001
            continue
    return out, ""


def parse_page_response(
    raw: bytes, *, tid: str = "", with_floor: bool = False, floor_number_base: int = 0
) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    """解析楼层页响应，返回 (楼层字典列表, 帖子信息, 错误说明)。

    Args:
        floor_number_base: 楼层号起始值。**必须要**，因为响应里的 ``floor`` 字段实测
            几乎全是 0（第 1 页 31 条里只有第一条给了 1，第 2 页 12 条全是 0）。
            这里按返回顺序从 base+1 开始自编；翻页时由调用方把 base 累加。
        with_floor: 是否解析楼中楼。响应里没有就是没有，这个参数只影响要不要去找。
    """
    try:
        msg = Message.load(raw)
    except ProtobufError as exc:
        return [], {}, "响应不是 protobuf：%s" % exc
    errno, errmsg = _errno(msg)
    if errno:
        return [], {}, "errno=%s %s" % (errno, errmsg or "（无错误信息）")
    data = msg.message(RES_DATA)
    if not data:
        return [], {}, "响应里没有 data"
    users = _user_map(data, PAGE_USER_LIST)
    current, total, offset = _page_info(data, PAGE_PAGE)
    info: dict[str, Any] = {"page": current, "total_page": total, "offset": offset}
    thread = data.message(PAGE_THREAD)
    if thread:
        info["tid"] = str(thread.uint(TH_ID) or tid or "")
        info["title"] = thread.text(TH_TITLE)
        info["replies"] = thread.uint(TH_REPLY_NUM)
        info["views"] = thread.uint(TH_VIEW_NUM)
        info["agree"] = _agree_num(thread, TH_AGREE)
        info["is_good"] = bool(thread.uint(TH_IS_GOOD))
        info["thread_type"] = thread.uint(TH_THREAD_TYPE)
    floors: list[dict[str, Any]] = []
    # 同样逐条 try：一层楼坏了不该让整页楼层都没了。注意 idx 必须照样递增 ——
    # 楼层号是 base + idx + 1 现编的，跳过一条后后面的号会整体前移，但这是
    # 唯一可行的取舍（没有可信的真实楼层号），且跳过的那层本来就不显示。
    for idx, post in enumerate(data.messages(PAGE_POST_LIST)):
        try:
            author_id = post.uint(POST_AUTHOR_ID)
            user = users.get(author_id, Message())
            name = _display_name(user)
            # 楼层号自编：响应里的 floor 不可信（几乎全是 0），但第一条如果是 1
            # 就说明服务端认得，用它的更保险 —— 自编只是为了填补剩下的。
            seq = floor_number_base + idx + 1
            real = post.uint(POST_FLOOR)
            floors.append(
                {
                    "fn": real or seq,
                    "pid": str(post.uint(POST_ID) or ""),
                    "author": name,
                    "author_id": author_id,
                    "level": user.uint(USER_LEVEL),
                    "portrait": user.text(USER_PORTRAIT),
                    "time": post.uint(POST_TIME),
                    "agree": _agree_num(post, POST_AGREE),
                    "content": post.messages(POST_CONTENT),
                    "comment_total": post.uint(POST_SUB_POST_NUMBER),
                    "comments": _parse_comments(post, users) if with_floor else [],
                }
            )
        except Exception:  # noqa: BLE001
            continue
    return floors, info, ""


def comment_text(shell_raw: bytes) -> str:
    """把楼中楼的正文取出来。

    ``SubPostList.field2`` **不是** PbContent 列表，而是一段内嵌 protobuf，
    形状跟 PbContent 很像但语义不同 —— 解开后只有两个字段：
    ``field1`` 是类型（0=文本），``field2`` 直接就是字符串正文。
    实测例：``08 00 12 07 e5 9b 9e e5 a4 8d 20`` = ``field1=0`` + ``field2="回复 "``。

    所以这里不能复用 :func:`content_text`：那一层没有 PbContent 的图片/表情字段。
    楼中楼里也没有点赞数（``SUBL_AGREE=9`` 实测不存在），故只取正文。
    """
    if not shell_raw:
        return ""
    try:
        shell = Message.load(shell_raw)
    except ProtobufError:
        return ""
    return shell.text(SHELL_TEXT).strip()


def _parse_comments(post: Message, users: "dict[int, Message]") -> list[dict[str, Any]]:
    """解析一层楼里的楼中楼。

    结构是 ``Post.sub_post_list[] -> SubPost.sub_post_list[] -> SubPostList``，
    中间那层 SubPost 只是个壳（带一个 pid），真正的内容在 SubPostList 里。
    空壳（sub_post_list 为空）直接跳过 —— 贴吧经常发一个空 SubPost 占位。

    实测的接口限制：**「回复某人」的被回复人名字接口不给**，正文里只剩一个
    字面上的「回复 」。对比网页版是有 ``<a href="/home/main?un=...">`` 的。
    这里照原样保留，不做任何猜测性补全。
    """
    out: list[dict[str, Any]] = []
    for wrapper in post.messages(POST_SUB_POST_LIST):
        for sub in wrapper.messages(SUB_LIST):
            # 单条楼中楼坏了就跳过：它只是「评论区的补充」，不该连累整层楼。
            try:
                text = comment_text(sub.raw(SUBL_SHELL))
                if not text:
                    continue
                author_id = sub.uint(SUBL_AUTHOR_ID)
                user = users.get(author_id, Message())
                out.append(
                    {
                        "pid": str(sub.uint(SUBL_ID) or ""),
                        "author": _display_name(user),
                        "author_id": author_id,
                        "level": user.uint(USER_LEVEL),
                        "time": sub.uint(SUBL_TIME),
                        "text": text,
                    }
                )
            except Exception:  # noqa: BLE001
                continue
    return out


def content_text(parts: "list[Message]") -> str:
    """把 PbContent 列表拼成纯文本。

    ``type`` 的实测取值只有 0/2/3 三种：0 是纯文本（proto3 省略零值，所以文本段
    连 field1 都不写，读出来就是默认 0）、2 是表情、3 是图片。表情的中文名在
    ``field11``，``field2`` 只是 ``image_emoticonNN`` 这种英文名，所以彩蛋式地优先
    用中文名。图片留 ``[图]``、视频留 ``[视频]``，真实地址由
    :func:`content_images` 单独取，这里不重复输出。
    """
    chunks: list[str] = []
    for part in parts:
        kind = part.uint(PC_TYPE)
        if kind in (PC_TYPE_TEXT, PC_TYPE_AT):
            chunks.append(part.text(PC_TEXT))
        elif kind == PC_TYPE_EMOJI:
            name = part.text(PC_EMOJI_NAME).strip() or part.text(PC_TEXT).strip()
            chunks.append("[%s]" % name if name else "[表情]")
        elif kind == PC_TYPE_IMAGE:
            chunks.append("[图]")
        elif kind == PC_TYPE_VIDEO:
            chunks.append("[视频]")
    return "".join(chunks).strip()


def pick_image_url(part: Message) -> str:
    """从一段图片 PbContent 里挑最清晰的那个地址。

    四档实测：``origin_src`` 最清晰（1040×1280 263KB），``big_cdn_src`` 次之，
    ``cdn_src`` 是 720 宽那档，``src`` 常常是空的。所以按
    **origin → big_cdn → cdn → src** 的顺序挑第一个非空的。
    """
    for field in (PC_ORIGIN_SRC, PC_BIG_CDN_SRC, PC_CDN_SRC, PC_SRC):
        url = part.text(field).strip()
        if url:
            return _absolute(url)
    return ""


def _absolute(url: str) -> str:
    """protobuf 里的图片地址有时是 ``//host/path``，补上协议。"""
    if url.startswith("//"):
        return "https:" + url
    return url


def content_images(parts: "list[Message]") -> "list[str]":
    """取一层楼里所有图片的最清晰地址（保持正文顺序）。"""
    out: "list[str]" = []
    for part in parts:
        if part.uint(PC_TYPE) != PC_TYPE_IMAGE:
            continue
        url = pick_image_url(part)
        if url and url not in out:
            out.append(url)
    return out


def looks_like_proto(raw: bytes) -> bool:
    """粗判一段字节像不像 protobuf。

    客户端接口返回的不是 protobuf 时，往往是 HTML 错误页（反爬拦截、接口改版），
    也可能是一段 JSON、纯文本，或者干脆是空的。直接丢给 parse 会抛异常，而那种
    异常信息（「字段号 0」之类）看不出真实原因。

    判据（注意顺序，顺序错了会把合法 protobuf 判成文本）：

    1. 空响应不算；去掉开头的 UTF-8 BOM。
    2. 原始首字节是 ``<`` / ``{`` / ``[`` 的，是 HTML/XML/JSON，不算。
    3. 跳过前导空白（错误页前面常有几个换行）后，如果变成了空、或者变成
       上面那三种开头，也不算 —— 这一步只用来识别**文本**，不用来改判 protobuf。
    4. 最后才看 tag 合法性：低 3 位必须是合法 wire type（0/1/2/5），
       高 5 位字段号不能为 0。

    **第 4 步必须用原始首字节，不能先 lstrip 再判**：``\x0a`` / ``\x0d`` /
    ``\x09`` 既是换行/回车/TAB，也是 field 1 的 wire2/wire5/wire1 —— 而 field 1
    恰恰是 protobuf 最常见的第一字段。先 lstrip 就会把这类响应的首字节吃掉，
    再把后面的内容当成 tag，于是合法响应被判成「不像 protobuf」，整条 protobuf
    路线被静默跳过（调用方会退回 HTML 路线，表面只是少了个点赞数）。
    """
    head = raw or b""
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    if not head:
        return False
    if head[0] in (0x3C, 0x7B, 0x5B):  # < { [
        return False
    # 只为识别「前面带空白的文本页」，不参与下面的 tag 判定
    stripped = head.lstrip(b" \t\r\n")
    if not stripped:
        return False
    if stripped[0] in (0x3C, 0x7B, 0x5B):
        return False
    first = head[0]
    wire = first & 0x07
    if wire not in (0, 1, 2, 5):
        return False
    return (first >> 3) != 0
