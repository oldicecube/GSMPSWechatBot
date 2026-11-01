"""麦麦贴吧搬石 —— 配置模型。

面板上的文字约定：节的说明来自类的 docstring，字段下面那行小灰字来自 hint ——
两者都只写一句话，细节留 README；字段的 description 不进面板，保持一句话即可。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from maibot_sdk import Field, PluginConfigBase

# 面板上可以勾的内置吧：都是贴吧的通用大吧。
# 注意：贴吧的 kw 参数要的是吧名本身，很多吧名带上末尾「吧」字反而查不到，所以这里都不带。
PRESET_FORUMS: tuple[str, ...] = (
    "搬石",
    "小黑盒笑话",
    "弱智",
    "地狱笑话",
    "dinner笑话",
    "孙笑川",
    "核战避难所",
    "2ch",
    "复制粘贴",
    "反二次元",
    "冷笑话",
    "幽默老中",
)



class PluginSection(PluginConfigBase):
    """插件总开关。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=False,
        description="是否启用插件（安装后默认关闭）",
        json_schema_extra={
            "label": "启用插件",
            "hint": "关掉后不再响应任何命令",
        },
    )
    config_version: str = Field(
        default="1.1.8",
        description="配置版本号，请勿手动修改",
        json_schema_extra={"label": "配置版本", "hidden": True},
    )


class StoneSection(PluginConfigBase):
    """搬运范围：逛哪些吧、每轮搬几条、按什么条件筛选。"""

    __ui_label__ = "搬石"
    __ui_icon__ = "hammer"
    __ui_order__ = 1

    preset_forums: list[
        Literal[
            "搬石",
            "小黑盒笑话",
            "弱智",
            "地狱笑话",
            "dinner笑话",
            "孙笑川",
            "核战避难所",
            "2ch",
            "复制粘贴",
            "反二次元",
            "冷笑话",
            "幽默老中",
        ]
    ] = Field(
        default_factory=lambda: ["搬石"],
        description="内置吧，可勾选多个",
        json_schema_extra={
            "label": "内置吧（可多选）",
            "hint": "勾选即搬运；没列出的吧写到下一项",
            "x-widget": "select",
        },
    )
    forums: list[str] = Field(
        default_factory=list,
        description="自己填的吧（一行一个）",
        json_schema_extra={
            "label": "自己加的吧",
            "hint": "一行一个吧名；末尾带不带「吧」字均可",
        },
    )
    count_per_forum: int = Field(
        default=3,
        ge=1,
        le=20,
        description="每轮从每个吧搬几条新帖",
        json_schema_extra={
            "label": "每个吧搬几条",
            "hint": "每个吧取最后回复最新的几条",
        },
    )
    min_replies: int = Field(
        default=0,
        ge=0,
        le=1000,
        description="只搬回复数不少于多少的帖子",
        json_schema_extra={"label": "最少回复数", "hint": "回复数低于此值的帖子不搬；0 = 不限"},
    )
    min_agree: int = Field(
        default=10,
        ge=0,
        le=100000,
        description="只搬点赞数不少于多少的帖子",
        json_schema_extra={"label": "最少点赞数", "hint": "点赞低于此值的帖子不搬；0 = 不限"},
    )
    skip_top: bool = Field(
        default=True,
        description="跳过置顶帖",
        json_schema_extra={"label": "跳过置顶帖", "hint": "吧内置顶帖一般是公告，通常保持开启"},
    )
    block_keywords: list[str] = Field(
        default_factory=lambda: ["水贴", "水楼", "+3"],
        description="标题里含这些词就不搬（一行一个）",
        json_schema_extra={"label": "标题屏蔽词", "hint": "标题含这些词的帖子不搬；留空 = 不屏蔽"},
    )
    keep_records: int = Field(
        default=500,
        ge=50,
        le=5000,
        description="每个吧保留多少条搬运记录用于去重",
        json_schema_extra={"label": "去重记录条数", "hint": "每个吧保留的去重记录条数；超出后丢弃最早的记录"},
    )


def resolve_forums(section: StoneSection) -> list[str]:
    """把面板上勾的内置吧和自己填的吧合成一份：去空、去重、保持顺序。"""

    merged: list[str] = []
    seen: set[str] = set()
    chosen = list(getattr(section, "preset_forums", None) or [])
    chosen += list(getattr(section, "forums", None) or [])
    for raw in chosen:
        name = str(raw or "").strip()
        if not name:
            continue
        # 「搬石吧」和「搬石」指向同一个吧，只留先出现的那个（内置吧排在前面）
        key = name[:-1] if len(name) > 1 and name.endswith("吧") else name
        if key in seen:
            continue
        seen.add(key)
        merged.append(name)
    return merged


class ScheduleSection(PluginConfigBase):
    """自动搬运的排期，以及投递到哪些群。"""

    __ui_label__ = "定时"
    __ui_icon__ = "clock"
    __ui_order__ = 2

    auto_enabled: bool = Field(
        default=False,
        description="是否按下面的时间自动搬石",
        json_schema_extra={
            "label": "自动搬石",
            "hint": "与手动 /搬石 相互独立",
        },
    )
    interval_minutes: int = Field(
        default=120,
        ge=10,
        le=1440,
        description="每隔多少分钟搬一次",
        json_schema_extra={
            "label": "隔多少分钟搬一次",
            "hint": "两次自动搬运的间隔分钟数；填了「每天定点」后本项失效",
        },
    )
    base_time: str = Field(
        default="0:00",
        description="间隔计时的起点",
        json_schema_extra={
            "label": "基准时刻",
            "hint": "间隔从这一刻往后数，如 0:00 + 120 分钟 = 0、2、4…22 点",
        },
    )
    lead_seconds: int = Field(
        default=30,
        ge=0,
        le=3600,
        description="提前多少秒开始准备",
        json_schema_extra={
            "label": "提前开始（秒）",
            "hint": "提前多少秒开始取帖，到时间点后再发送",
        },
    )
    # 用 Literal 而不是自己往 json_schema_extra 里塞 choices：
    # SDK 的 _extract_select_choices 只认 Literal 注解
    # （maibot_sdk/config.py:446），手写的 choices 面板根本不看。
    group_mode: Literal["白名单", "黑名单"] = Field(
        default="白名单",
        description="群名单模式：白名单 = 只发名单里的群，黑名单 = 名单里的群不发",
        json_schema_extra={
            "label": "群名单模式",
            "hint": "白名单：只在下面列的群里发；黑名单：下面列的群不发。默认白名单且名单为空 = 哪个群都不发",
            "x-widget": "select",
        },
    )
    group_list: list[str] = Field(
        default_factory=list,
        description="群名单（填群号，一行一个）",
        json_schema_extra={
            "label": "群名单",
            "hint": "白名单：只有这些群会收到；黑名单：这些群不收。填群号，一行一个",
        },
    )
    daily_times: list[str] = Field(
        default_factory=list,
        description="每天几点搬（24 小时制，一行一个）",
        json_schema_extra={
            "label": "每天定点",
            "hint": "每天几点搬，如 09:30、21:00，一行一个",
        },
    )
    quiet_start: str = Field(
        default="23:00",
        description="免打扰开始时刻",
        json_schema_extra={
            "label": "免打扰开始",
            "hint": "格式 HH:MM；留空 = 不启用免打扰。默认 23:00~08:00 深夜不发",
        },
    )
    quiet_end: str = Field(
        default="08:00",
        description="免打扰结束时刻",
        json_schema_extra={
            "label": "免打扰结束",
            "hint": "格式 HH:MM；支持跨午夜。默认 23:00~08:00 深夜不发",
        },
    )



class SendSection(PluginConfigBase):
    """定时搬运与手动指令的发送形态。"""

    __ui_label__ = "发送"
    __ui_icon__ = "send"
    __ui_order__ = 3

    card_style: Literal["自绘卡片", "贴吧原页面"] = Field(
        default="贴吧原页面",
        description="长图用哪种样式",
        json_schema_extra={
            "label": "长图样式",
            "hint": "贴吧原页面 = 按手机版页面截图；自绘卡片 = 插件自己排版",
            "x-widget": "select",
        },
    )
    page_render: Literal["零外链", "原生"] = Field(
        default="零外链",
        description="贴吧原页面的渲染方式",
        json_schema_extra={
            "label": "原页面渲染方式",
            "hint": "零外链 = 先内嵌好资源再渲染，任何主程序版本都能出图；"
            "原生 = 浏览器自己加载外链，要 1.61 以上",
            "x-widget": "select",
        },
    )
    forward: bool = Field(
        default=True,
        description="用合并转发打包成聊天记录",
        json_schema_extra={"label": "打包成聊天记录", "hint": "关掉则逐帖单独发一张长图"},
    )
    forward_name: str = Field(
        default="？！石！？",
        description="聊天记录里显示的名字",
        json_schema_extra={
            "label": "聊天记录名字",
            "hint": "聊天记录里显示的发件人名字",
        },
    )
    send_video: bool = Field(
        default=True,
        description="帖子里的视频直接发出来",
        json_schema_extra={
            "label": "发视频",
            "hint": "超过上限的整条跳过；取不到直链时退发链接",
        },
    )
    video_max_mb: int = Field(
        default=30,
        ge=0,
        le=500,
        description="视频文件大小上限（MB）",
        json_schema_extra={
            "label": "视频大小上限",
            "hint": "单位 MB；超过后整条视频跳过，不发链接；0 = 不限",
        },
    )
    post_link: bool = Field(
        default=True,
        description="每条帖子带上原帖链接",
        json_schema_extra={
            "label": "带原帖链接",
            "hint": "关掉后聊天记录的标题下面不再带原帖地址",
        },
    )
    thread_filter: Literal["不限", "只发图文帖", "只发视频帖"] = Field(
        default="不限",
        description="这一轮只搬哪种帖子",
        json_schema_extra={
            "label": "只搬哪种帖",
            "hint": "按列表页的类型标记筛",
            "x-widget": "select",
        },
    )
    max_images: int = Field(
        default=9,
        ge=0,
        le=30,
        description="长图里最多放几张正文图",
        json_schema_extra={"label": "长图里最多几张图", "hint": "长图会先插入若干张正文图；0 = 长图里不放图"},
    )
    image_max_width: int = Field(
        default=1080,
        ge=320,
        le=2560,
        description="图片超过这个宽度就等比缩小",
        json_schema_extra={"label": "图片最大宽度", "hint": "超过后按比例缩小"},
    )
    image_quality: int = Field(
        default=85,
        ge=40,
        le=100,
        description="图片压缩质量（JPEG）",
        json_schema_extra={"label": "图片压缩质量", "hint": "建议 80~90；数值越大越清晰，体积也越大"},
    )
    card_width: int = Field(
        default=830,
        ge=600,
        le=1200,
        description="长图宽度（像素）",
        json_schema_extra={"label": "长图宽度", "hint": "只对「自绘卡片」有效"},
    )
    closing_text: str = Field(
        default="主……主人，这……这是我今天发……发……发现的…有趣贴子♥",
        description="一轮搬运结束后补的一句话",
        json_schema_extra={
            "label": "结束语",
            "hint": "帖子发完后由麦麦在同一个群里补一句；留空则不说",
        },
    )


class FetchSection(PluginConfigBase):
    """抓取细节，通常保持默认即可。"""

    __ui_label__ = "抓取"
    __ui_icon__ = "sliders-horizontal"
    __ui_order__ = 5

    max_pages: int = Field(
        default=1,
        ge=1,
        le=5,
        description="列表翻几页",
        json_schema_extra={"label": "列表翻几页", "hint": "每页 30 条；翻得越多越容易被贴吧拦截"},
    )
    max_floors: int = Field(
        default=30,
        ge=1,
        le=60,
        description="每条帖子最多搬几层",
        json_schema_extra={"label": "每帖最多几层", "hint": "每帖最多取几层；免登录只给首页 30 层"},
    )
    max_chars: int = Field(
        default=800,
        ge=0,
        le=5000,
        description="单层正文最多留几个字",
        json_schema_extra={"label": "单层最多留几个字", "hint": "单层文字最多留几个字；0 = 不截断"},
    )
    max_comments: int = Field(
        default=3,
        ge=0,
        le=20,
        description="每层最多带几条楼中楼",
        json_schema_extra={
            "label": "每层带几条评论",
            "hint": "每层正文下最多挂几条评论（楼主回复优先）；0 = 不带评论",
        },
    )
    delay_seconds: float = Field(
        default=3.0,
        ge=0.0,
        le=30.0,
        description="两次请求之间歇多少秒",
        json_schema_extra={"label": "请求间隔（秒）", "hint": "两次请求之间的间隔秒数；设为 0 会显著提高被拦截的概率"},
    )
    timeout: int = Field(
        default=30,
        ge=5,
        le=120,
        description="单次 HTTP 请求超时（秒）",
        json_schema_extra={"label": "请求超时（秒）", "hint": "单次请求超时秒数；网络较慢时可适当调大"},
    )
    proxy: str = Field(
        default="",
        description="访问贴吧时用的 HTTP 代理地址",
        json_schema_extra={"label": "（高级）代理", "hint": "留空为直连；例如 http://127.0.0.1:7890"},
    )
    user_agent: str = Field(
        default="",
        description="自定义 User-Agent",
        json_schema_extra={"label": "（高级）User-Agent", "hint": "留空时使用内置手机 UA"},
    )


class AdminSection(PluginConfigBase):
    """谁可以使用本插件的指令。"""

    __ui_label__ = "权限"
    __ui_icon__ = "shield"
    __ui_order__ = 6

    admins: list[str] = Field(
        default_factory=list,
        description="管理员的 QQ 号（一行一个）",
        json_schema_extra={
            "label": "管理员",
            "hint": "能用全部命令的 QQ 号，一行一个；本项与下一项都留空时，沿用宿主插件权限名单",
        },
    )
    allow_groups: list[str] = Field(
        default_factory=list,
        description="能用搬石指令的群（一行一个群号）",
        json_schema_extra={
            "label": "可搬石的群",
            "hint": "这些群里任何人都能用 /搬石、/搬视频；维护命令仍只限管理员。一行一个群号",
        },
    )


class ToolSection(PluginConfigBase):
    """麦麦自主搬运时使用的参数，与上一节的发送形态相互独立。"""

    __ui_label__ = "工具"
    __ui_icon__ = "wrench"
    __ui_order__ = 4

    enabled: bool = Field(
        default=False,
        description="是否让麦麦能自己搬石",
        json_schema_extra={
            "label": "让麦麦自己搬",
            "hint": "开启后麦麦可在聊天里自己搬石",
        },
    )
    forums: list[str] = Field(
        default_factory=list,
        description="限定麦麦只能搬这些吧（一行一个）",
        json_schema_extra={
            "label": "限定吧",
            "hint": "限定麦麦只能搬这些吧；留空 = 不限",
        },
    )
    max_count: int = Field(
        default=5,
        ge=1,
        le=10,
        description="麦麦一次最多搬几条",
        json_schema_extra={
            "label": "一次最多几条",
            "hint": "麦麦一次能搬的上限；实际条数由麦麦自行决定，不会超过此值",
        },
    )
    post_link: bool = Field(
        default=False,
        description="工具搬石时带原帖链接",
        json_schema_extra={"label": "带原帖链接", "hint": "关掉后聊天记录的标题下面不再带原帖地址"},
    )
    max_images: int = Field(
        default=9,
        ge=0,
        le=30,
        description="工具搬石时长图里最多几张图",
        json_schema_extra={"label": "长图里最多几张图", "hint": "长图会先插入若干张正文图；0 = 长图里不放图"},
    )
    thread_filter: Literal["不限", "只发图文帖", "只发视频帖"] = Field(
        default="不限",
        description="工具搬石时只搬哪种帖",
        json_schema_extra={
            "label": "只搬哪种帖",
            "hint": "麦麦指定要视频帖时以此为准",
            "x-widget": "select",
        },
    )


class StoneConfig(PluginConfigBase):
    """麦麦贴吧搬石 —— 顶层配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    stone: StoneSection = Field(default_factory=StoneSection)
    admin: AdminSection = Field(default_factory=AdminSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    send: SendSection = Field(default_factory=SendSection)
    tool: ToolSection = Field(default_factory=ToolSection)
    fetch: FetchSection = Field(default_factory=FetchSection)


# 面板默认值改过、但老配置里那个键还留着旧值的，在这里按「旧值 → 新值」补一次。
# 为什么要这层：宿主升级配置时是按「新默认当骨架、旧值盖上去」，而空列表和三个词的列表
# 都是 list，类型检查放行，于是老配置里的 block_keywords = [] 会一直把新默认盖掉；
# 用户自己填过的词当然要留，所以只在「旧值恰好等于旧默认」时才视为没动过、替换成新默认。
# 字符串字段同理（quiet_start / quiet_end 这类）。
_DEFAULT_MIGRATIONS: dict[tuple[str, str], tuple[object, object]] = {
    ("stone", "block_keywords"): ([], list(StoneSection.model_fields["block_keywords"].default_factory())),
    ("schedule", "quiet_start"): ("", "23:00"),
    ("schedule", "quiet_end"): ("", "08:00"),
}


# 群名单从「一份白名单 + 一份黑名单」改成了「模式 + 一份名单」。老配置要能平滑
# 搬过来，否则用户升级完发现「明明配了群，却一个都不发」。两条旧名单在旧代码里
# 是**同时生效**的（先剔黑名单、再看白名单），所以合并时要按老语义等价折算：
# 只填白名单 → 白名单模式；只填黑名单 → 黑名单模式；两边都填过 → 白名单模式、
# 名单取「白名单 − 黑名单」。**两边都填过时不能只认黑名单** —— 那会把「只发这
# 几个群」变成「除了这几个全发」，方向正好反了，而且一声不响。
_GROUP_MOVES: dict[str, str] = {
    "include_groups": "白名单",
    "exclude_groups": "黑名单",
}

# group_mode 的取值换过两次：allow/deny → whitelist/blacklist → 中文。
# 中间那一步没发布过，但本机调试时写进过 config.toml，
# 所以三种叫法都得认。不搬就会掉进 else 分支 —— 那等于把用户的
# 黑名单悄悄变成白名单，从「只发这几个群」变成「除了这几个全发」，
# 方向正好反了，必须搬。
_GROUP_MODE_WORDS: dict[str, str] = {
    "allow": "白名单",
    "deny": "黑名单",
    "whitelist": "白名单",
    "blacklist": "黑名单",
}


def migrate_config(raw: dict) -> bool:
    """把老配置里「没动过、但默认值已经变了」的字段换成新默认，返回是否改过。

    顺带把换过节、挪过位置的字段搬到新位置；用户填的值原样跟随。
    """

    changed = False
    # 群名单：老配置是「一份白名单 + 一份黑名单」两份**同时生效**：
    # 先剔掉黑名单里的群，再看白名单 —— 白名单空着就是谁都发，填了就只发
    # 名单里的。现在合并成一个「模式 + 一份名单」的开关，搬家要按老语义
    # 等价折算：
    #   只填了白名单 → 白名单模式，名单照搬
    #   只填了黑名单 → 黑名单模式，名单照搬
    #   两边都填过   → 白名单模式，名单 = 白名单 − 黑名单（老语义是取交集）
    #
    # 两边都填过时**不能**只认黑名单：那等于把「只发这几个群」变成「除了这几个
    # 全发」，方向正好反了，而且不报错，用户只会看到一堆群莫名其妙收到消息。
    include_items: list[str] = []
    exclude_items: list[str] = []
    for block_name in ("send", "schedule"):
        block = raw.get(block_name)
        if not isinstance(block, dict):
            continue
        for field in _GROUP_MOVES:
            if field not in block:
                continue
            value = block.pop(field)
            changed = True
            items = (
                [str(x).strip() for x in value if str(x or "").strip()]
                if isinstance(value, list)
                else []
            )
            if field == "include_groups":
                include_items.extend(items)
            else:
                exclude_items.extend(items)
    if include_items or exclude_items:
        schedule_block = raw.get("schedule")
        if not isinstance(schedule_block, dict):
            raw["schedule"] = schedule_block = {}
        if include_items:
            banned = set(exclude_items)
            keep: list[str] = []
            for item in include_items:
                if item not in banned and item not in keep:
                    keep.append(item)
            mode, listed = "白名单", keep
        else:
            mode, listed = "黑名单", list(dict.fromkeys(exclude_items))
        if not schedule_block.get("group_list"):
            schedule_block["group_list"] = listed
        if not str(schedule_block.get("group_mode") or "").strip():
            schedule_block["group_mode"] = mode
        changed = True
    # group_mode 的取值换过词：allow/deny → whitelist/blacklist → 中文。
    # 认得出旧词才改，用户填的中文原样留着。
    schedule_block = raw.get("schedule")
    if isinstance(schedule_block, dict):
        word = str(schedule_block.get("group_mode") or "").strip().lower()
        if word in _GROUP_MODE_WORDS:
            schedule_block["group_mode"] = _GROUP_MODE_WORDS[word]
            changed = True
    for (section, field), (old_default, new_default) in _DEFAULT_MIGRATIONS.items():
        block = raw.get(section)
        if not isinstance(block, dict) or field not in block:
            continue
        if block[field] != old_default:
            # 用户自己填过，保持原样
            continue
        block[field] = list(new_default) if isinstance(new_default, list) else new_default
        changed = True
    return changed


def _parse_hhmm(value: str) -> tuple[int, int] | None:
    """把 HH:MM 解析成 (时, 分)；兼容 9:00 这类没补零的写法，解析不了返回 None。"""

    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        return None
    head, tail = parts[0].strip(), parts[1].strip()
    if not head.isdigit() or not tail.isdigit():
        return None
    hour, minute = int(head), int(tail)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour, minute


def in_quiet_hours(section: "ScheduleSection", now: "datetime | None" = None) -> bool:
    """当前是否落在免打扰时段里。两端都填了才生效，支持跨午夜。"""

    start = _parse_hhmm(getattr(section, "quiet_start", ""))
    end = _parse_hhmm(getattr(section, "quiet_end", ""))
    if start is None or end is None:
        return False
    if start == end:
        # 首尾相同视为「全天免打扰」太反直觉，按不启用处理
        return False
    current = now or datetime.now()
    now_minutes = current.hour * 60 + current.minute
    start_minutes = start[0] * 60 + start[1]
    end_minutes = end[0] * 60 + end[1]
    if start_minutes < end_minutes:
        return start_minutes <= now_minutes < end_minutes
    # 跨午夜，如 23:00 ~ 08:00
    return now_minutes >= start_minutes or now_minutes < end_minutes
