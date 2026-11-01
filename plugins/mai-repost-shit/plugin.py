"""mai repost shit —— 由 planner 跟随群聊或定时搬运「评论区全是艾特」的B站视频。

设计要点（都是实测结论，改之前先读 docs/NOTES.md）：
  * 判据只有一条：评论含 @ 比例 ≥ 阈值，且评论样本数达标（否则比值是噪声）。
  * 只搬一条/轮：planner 模式观察消息密度后交核心决策；scheduled 模式保留随机时刻。
  * 不转码：免登录 playurl 最高就是 720P，直接按档位下载即可，本机也不需要 ffmpeg。
  * 微信投递用说明文字 + 原生 MP4 文件，本地路径避免大体积 base64 RPC。
  * 除了命令，还注册了一个 Tool 让群友用自然语言点播（「搬个史」「来点鬼畜的」）。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from maibot_sdk import API, Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .core.bili_api import BiliClient
from .core.defaults import (
    DEFAULT_BLACKLIST,
    DEFAULT_KEYWORDS,
    DEFAULT_PRETTY_BLOCK_WORDS,
    DEFAULT_PRETTY_KEYWORDS,
    DEFAULT_PRETTY_REAL_BLOCK,
    DEFAULT_PRETTY_TYPENAME,
)
from .core.judge import Judge
from .core.pipeline import Pipeline
from .core.pretty import PrettyPicker
from .core.speaker import SpeakerCache, normalize_qq
from .core.state import StateStore
from .core.activity import summarize, eligible, is_hot, low_activity_eligible
from .core.sources import discover, planner_instruction

TOOL_REPOST = "repost_shi"
TOOL_PRETTY = "repost_pretty"
# 入站钩子：群聊里 Tool 回调拿不到发言者 QQ，只能在这里按会话缓存真实发言者
RECEIVE_HOOK = "chat.receive.after_process"
# 回复后处理钩子：把 response 置空即可取消这次回复（宿主会记「返回了空回复」并不发送）
REPLY_HOOK = "maisaka.reply.before_post_process"

# 口语触发的命令正则。宿主用 re.compile(pattern).search(text) 匹配、**非锚定**，
# 所以「憨憨，给我来点好看的」这种带称呼的说法也能命中。
# 为什么做成命令而不是只靠 LLM 工具：实测群里说「来点好康的/涩的/好看的」时，
# 模型只是聊天式回绝（「好看的没有了」），根本不调用工具；命令链是确定性的，
# 而且 intercept 置 1 之后消息不进 LLM、机器人也不会回话。
_ASK_WORDS = r"(?:来点|来几个|再来点|整点|搞点|发点|给点|找点|搬点|搬个|搬几个|搬几)"
_SHI_WORDS = r"(?:史|屎|抽象|鬼畜|答辩|难绷|乐子)"
_PRETTY_WORDS = r"(?:涩|(?<!颜)色|黄|好看|好康|二次元|纸片人|mmd)"
_GAP = r"[^，。！？!?,、]{0,6}"
SHI_TRIGGER_PATTERN = rf"(?i){_ASK_WORDS}{_GAP}{_SHI_WORDS}"
PRETTY_TRIGGER_PATTERN = rf"(?i){_ASK_WORDS}{_GAP}{_PRETTY_WORDS}"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用插件（同时控制定时搬运）")
    config_version: str = Field(default="1.3.0", description="配置版本")


class TargetSectionConfig(PluginConfigBase):
    """投递目标与频率。"""

    __ui_label__ = "投递"
    __ui_icon__ = "send"
    __ui_order__ = 1

    groups: List[str] = Field(default_factory=list, description="微信 chatroom ID 或 qq:账号:连接:group:群号；不按群名猜测")
    mode: Literal["scheduled", "planner"] = Field(default="scheduled", description="scheduled 定时直接搬；planner 观察群聊后交由核心决定")
    daily_times: int = Field(default=3, description="每天搬几次")
    active_hours: List[int] = Field(default_factory=lambda: [9, 23], description="允许发送的时间范围 [起, 止]（24 小时制）")
    catch_up_minutes: int = Field(default=30, description="错过计划时刻后多久内仍补发，超时则跳过")
    include_header_text: bool = Field(default=True, description="聊天记录里视频上方是否带标题/数据说明")
    source_channels: List[str] = Field(default_factory=lambda: ['bilibili', 'qq_forward', 'tieba'],
                                       description='统一 Planner 可选择的来源；未安装的来源自动跳过')


class PlannerSectionConfig(PluginConfigBase):
    coordinator: bool = Field(default=False, description='通过自主生活插件统一决定、计入每日分享限额；不可用则不主动搬')
    window_seconds: int = Field(default=600, ge=60, le=3600, description="群聊活跃度统计窗口")
    min_messages: int = Field(default=6, ge=1, description="窗口内最少真人消息")
    min_users: int = Field(default=2, ge=1, description="窗口内最少不同发言者")
    min_shares: int = Field(default=2, ge=1, description="最少图像、视频、B站链接或搬屎话题信号，是否为整活由planner判断")
    fresh_seconds: int = Field(default=120, ge=10, description="最近分享超过此秒数不主动跟随")
    hot_messages: int = Field(default=20, ge=1, description="密集消息阈值")
    hot_shares: int = Field(default=4, ge=1, description="密集分享阈值")
    normal_gap_seconds: int = Field(default=600, ge=60, description="普通活跃时最短决策间隔")
    hot_gap_seconds: int = Field(default=180, ge=60, description="密集分享时最短决策与工具间隔")
    low_activity_gap_seconds: int = Field(default=600, ge=60, description="低活跃补位的最短决策间隔")
    repost_daily_limit: int = Field(default=48, ge=1, description="搬屎专用每日上限")
    repost_gap_seconds: int = Field(default=900, ge=60, description="搬屎专用最短间隔")
    repost_action_limit: int = Field(default=48, ge=1, description="搬屎专用每日动作上限")
    ignored_user_ids: List[str] = Field(default_factory=list, description="不计入群友活跃度的其他机器人")
    low_activity_enabled: bool = Field(default=True, description="低活跃群聊也允许由 Planner 判断是否补一条")
    low_activity_min_messages: int = Field(default=2, ge=1, description="低活跃机会所需真人消息数")
    low_activity_idle_seconds: int = Field(default=180, ge=30, description="低活跃机会要求距最近消息至少多少秒")


class SourceSectionConfig(PluginConfigBase):
    """候选检索。"""

    __ui_label__ = "检索"
    __ui_icon__ = "search"
    __ui_order__ = 2

    keywords: List[str] = Field(default_factory=lambda: list(DEFAULT_KEYWORDS), description="关键词池，每轮随机抽几个")
    keywords_per_run: int = Field(default=8, description="每轮用几个关键词搜")
    orders: List[str] = Field(default_factory=lambda: ["totalrank", "pubdate"], description="搜索排序方式")
    page_max: int = Field(default=3, description="每个关键词随机翻到第几页（上限）")
    window_hours: List[int] = Field(default_factory=lambda: [72, 720], description="发布时间窗 [最近, 最远]（小时）")
    play_min: int = Field(default=50000, description="播放量下限")
    play_max: int = Field(default=300000, description="播放量上限")
    min_video_review: int = Field(default=40, description="评论数下限（搜索接口自带，用来保证艾特率有分母）")
    max_video_review: int = Field(
        default=600,
        description="评论数上限（0 表示不限）。实测评论越多艾特率越低（40~150 条中位 33%、600+ 只 10%），"
        "所以把解说/剧集类大热门筛掉",
    )
    max_check_per_run: int = Field(
        default=16,
        description="每轮最多实际检查几条候选（每条约 4 个请求）。"
        "实测能过 50% 艾特率的候选约占一成，检查越多越容易出片",
    )


class JudgeSectionConfig(PluginConfigBase):
    """艾特率判据。"""

    __ui_label__ = "判据"
    __ui_icon__ = "filter"
    __ui_order__ = 3

    at_ratio_threshold: float = Field(default=0.5, description="评论含 @ 比例阈值")
    min_comments: int = Field(default=30, description="评论样本下限，不足则判样本不足")
    comment_pages: int = Field(default=3, description="取几页评论（每页 20 条）")


class DownloadSectionConfig(PluginConfigBase):
    """下载与体积控制。"""

    __ui_label__ = "下载"
    __ui_icon__ = "download"
    __ui_order__ = 4

    max_duration_seconds: int = Field(default=240, description="视频时长上限（秒）")
    max_size_mb: int = Field(
        default=10,
        description="体积上限（MB）。超限会自动降一档清晰度；实测 37MB 的视频三条投递路线都会被"
        "平台判失败，所以别放太大",
    )
    quality_preference: List[int] = Field(
        default_factory=lambda: [64, 32, 16], description="清晰度优先级（64=720P, 32=480P, 16=360P）"
    )
    inline_video_max_mb: int = Field(
        default=10,
        description="视频内联发送的体积上限（MB）：不超过就带 base64 发，后端读不到本地文件也能发"
        "（实测 SnowLuma 后端跑在别的文件系统、给路径会 EACCES）。"
        "上限受插件↔宿主 RPC 帧 16MB 约束，base64 膨胀 4/3，所以别超过 11",
    )
    keep_files: int = Field(default=2, description="本地保留最近几个视频文件")


class FilterSectionConfig(PluginConfigBase):
    """屏蔽词与内容安全。"""

    __ui_label__ = "屏蔽"
    __ui_icon__ = "ban"
    __ui_order__ = 5

    title_blacklist: List[str] = Field(
        default_factory=lambda: list(DEFAULT_BLACKLIST), description="标题屏蔽词（子串匹配，命中即跳过）"
    )
    tag_blacklist: List[str] = Field(
        default_factory=list, description="标签屏蔽词（子串匹配，命中即跳过）"
    )
    up_blacklist: List[str] = Field(
        default_factory=list, description="UP 主屏蔽词（子串匹配，命中即跳过）"
    )


class ToolSectionConfig(PluginConfigBase):
    """自然语言点播（LLM Tool）。"""

    __ui_label__ = "自然语言"
    __ui_icon__ = "message-square"
    __ui_order__ = 6

    enabled: bool = Field(default=True, description="是否允许用自然语言叫它搬史")
    permission: str = Field(
        default="anyone",
        description="谁能触发：anyone=群成员都可以 / whitelist=只有下白名单里的 QQ 能触发。"
        "注意工具回调拿不到发言者 QQ，白名单是借助入站钩子按会话回查的",
    )
    whitelist: List[str] = Field(default_factory=list, description="permission=whitelist 时允许触发的 QQ 号列表")
    speaker_cache_ttl_seconds: int = Field(default=600, description="发言者缓存有效期（秒），超时后白名单模式会拒绝")
    min_gap_seconds: int = Field(
        default=60,
        description="同一个群两次点播的最小间隔（秒）：只挡贴脸连点，不设每日次数上限；0 表示完全不挡",
    )
    max_count: int = Field(default=3, description="单次点播最多搬几条")
    suppress_reply: bool = Field(
        default=True,
        description="点播后是否吞掉机器人这次的回话（只调用搬史工具、不在群里多说话）",
    )
    suppress_reply_window_seconds: int = Field(
        default=120, description="吞回话的有效窗口（秒）：点播后这段时间内的第一条回复会被取消"
    )


class PrettySectionConfig(PluginConfigBase):
    """「来点好看的」频道：短时长 + 高收藏率 + 只二次元。"""

    __ui_label__ = "来点好看的"
    __ui_icon__ = "sparkles"
    __ui_order__ = 7

    enabled: bool = Field(default=True, description="是否允许用自然语言点播「好看的」（二次元向）")
    keywords: List[str] = Field(
        default_factory=lambda: list(DEFAULT_PRETTY_KEYWORDS), description="关键词池，每轮随机抽几个"
    )
    keywords_per_run: int = Field(default=6, description="每轮用几个关键词搜")
    orders: List[str] = Field(default_factory=lambda: ["totalrank", "pubdate"], description="搜索排序方式")
    page_max: int = Field(default=3, description="每个关键词随机翻到第几页（上限）")
    window_hours: List[int] = Field(default_factory=lambda: [72, 720], description="发布时间窗 [最近, 最远]（小时）")
    play_min: int = Field(default=10000, description="播放量下限")
    play_max: int = Field(default=500000, description="播放量上限")
    max_duration_seconds: int = Field(default=120, description="时长上限（秒）——「时长不长」就是这个")
    favorite_rate_metric: str = Field(
        default="fav_like",
        description="收藏率用哪个分母：fav_like = 收藏/点赞（默认）、fav_play = 收藏/播放。"
        "实测收藏/播放最高只到 40%、73 条里没有一条到 50%（收藏不可能比播放多一半）；"
        "而收藏/点赞中位就有 52%、≥50% 的有 39 条，所以门槛要设在点赞上",
    )
    min_favorite_rate: float = Field(
        default=0.5,
        description="收藏率下限。按 fav_like 口径：≥50% 约留一半候选，≥80% 约留三成，"
        "≥100%（收藏比点赞还多）约留两成",
    )
    typename_whitelist: List[str] = Field(
        default_factory=lambda: list(DEFAULT_PRETTY_TYPENAME),
        description="子分区白名单（子串匹配）：只放二次元向分区，真人向（仿妆cos/颜值/舞蹈综合/特摄）不在里面",
    )
    real_blacklist: List[str] = Field(
        default_factory=lambda: list(DEFAULT_PRETTY_REAL_BLOCK),
        description="真人向屏蔽词：标题或标签命中即跳过",
    )
    block_words: List[str] = Field(
        default_factory=lambda: list(DEFAULT_PRETTY_BLOCK_WORDS),
        description="教程/讲解类屏蔽词：标题或标签命中即跳过（只想要好看的画面，不想要教学）",
    )
    min_gap_seconds: int = Field(
        default=60,
        description="同一个群两次点播的最小间隔（秒）：只挡贴脸连点，不设每日次数上限；0 表示完全不挡",
    )


class RetrySectionConfig(PluginConfigBase):
    """没找到就继续找。"""

    __ui_label__ = "重试"
    __ui_icon__ = "rotate-cw"
    __ui_order__ = 8

    rounds: int = Field(
        default=3,
        description="一轮没找到符合条件的候选时，继续换关键词/翻页再找，最多试几轮",
    )
    gap_seconds: int = Field(default=15, description="两次尝试之间的间隔（秒），0 表示不等待")


class ShitpostConfig(PluginConfigBase):
    """插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    target: TargetSectionConfig = Field(default_factory=TargetSectionConfig)
    planner: PlannerSectionConfig = Field(default_factory=PlannerSectionConfig)
    source: SourceSectionConfig = Field(default_factory=SourceSectionConfig)
    judge: JudgeSectionConfig = Field(default_factory=JudgeSectionConfig)
    download: DownloadSectionConfig = Field(default_factory=DownloadSectionConfig)
    filters: FilterSectionConfig = Field(default_factory=FilterSectionConfig)
    tool: ToolSectionConfig = Field(default_factory=ToolSectionConfig)
    pretty: PrettySectionConfig = Field(default_factory=PrettySectionConfig)
    retry: RetrySectionConfig = Field(default_factory=RetrySectionConfig)


@dataclass
class RunConfig:
    """一次搬运所需的扁平配置，供 core 层的模块使用。"""

    groups: List[str]
    include_header_text: bool
    keywords: List[str]
    keywords_per_run: int
    orders: List[str]
    page_max: int
    window_hours: List[int]
    play_min: int
    play_max: int
    min_video_review: int
    max_video_review: int
    max_check_per_run: int
    at_ratio_threshold: float
    min_comments: int
    comment_pages: int
    max_duration_seconds: int
    max_size_mb: int
    quality_preference: List[int]
    inline_video_max_mb: int
    keep_files: int
    title_blacklist: List[str]
    tag_blacklist: List[str]
    up_blacklist: List[str]
    # 「来点好看的」频道
    pretty_keywords: List[str]
    pretty_keywords_per_run: int
    pretty_orders: List[str]
    pretty_page_max: int
    pretty_window_hours: List[int]
    pretty_play_min: int
    pretty_play_max: int
    pretty_max_duration_seconds: int
    pretty_rate_metric: str
    pretty_min_favorite_rate: float
    pretty_typename_whitelist: List[str]
    retry_rounds: int
    retry_gap_seconds: int
    pretty_real_blacklist: List[str]
    pretty_block_words: List[str]


class BilibiliShitpostPlugin(MaiBotPlugin):
    """定时搬运B站「艾特率超标」视频的插件。"""

    config_model = ShitpostConfig

    def __init__(self) -> None:
        super().__init__()
        self._client: Optional[BiliClient] = None
        self._store: Optional[StateStore] = None
        self._pipeline: Optional[Pipeline] = None
        self._loop_task: Optional[asyncio.Task[None]] = None
        self._wake = asyncio.Event()
        self._running = False
        # 点播任务在创建时同步置位，避免连续两次点播都通过重入检查
        self._manual_busy = False
        self._activity: Dict[str, dict] = {}
        # 群聊里工具回调拿不到发言者 QQ，靠入站钩子按会话缓存，供白名单校验
        self._speakers: Optional[SpeakerCache] = None
        # 会话 -> 吞回话的截止时间：点播后这段时间内的第一条回复会被取消
        self._reply_suppress: Dict[str, float] = {}
        self._reply_suppress_source: Dict[str, str] = {}
        self._source_status: Dict[str, Dict[str, Any]] = {}

    # ===== 生命周期 =====

    async def on_load(self) -> None:
        """装载状态、建客户端、起定时循环。"""
        data_dir = self.ctx.paths.data_dir
        work_dir = self.ctx.paths.runtime_dir / "clips"
        data_dir.mkdir(parents=True, exist_ok=True)
        work_dir.mkdir(parents=True, exist_ok=True)

        self._store = StateStore(data_dir / "state.json", self.ctx.logger)
        self._store.load()
        self._source_status = discover()
        self.ctx.logger.info('统一搬屎来源状态: %s', self._source_status)
        self._client = BiliClient(self.ctx.logger)
        self._speakers = SpeakerCache(self.config.tool.speaker_cache_ttl_seconds, self.ctx.logger)
        self._pipeline = Pipeline(
            ctx=self.ctx,
            client=self._client,
            judge=Judge(self._client, self._run_config(), self.ctx.logger),
            store=self._store,
            logger=self.ctx.logger,
            work_dir=work_dir,
            pretty=PrettyPicker(self._client, self._store, self.ctx.logger),
        )
        self._loop_task = asyncio.create_task(self._schedule_loop(), name="mai-repost-shit-scheduler")
        mode = self._permission_mode()
        self.ctx.logger.info(
            f"搬史机已装载：目标群 {self.config.target.groups or '（未配置）'}，"
            f"模式 {self.config.target.mode}，定时模式每天 {self.config.target.daily_times} 次，播放区间 "
            f"{self.config.source.play_min}~{self.config.source.play_max}，"
            f"投递方式 微信说明文字与 MP4 文件"
        )
        self.ctx.logger.info(f"自然语言点播权限：{self._describe_permission(mode)}")
        self.ctx.logger.info(
            "频道：搬史（评论区艾特率"
            f" ≥ {self.config.judge.at_ratio_threshold:.0%}）"
            f" + 来点好看的（收藏率 ≥ {self.config.pretty.min_favorite_rate:.0%}"
            f"（{'收藏/点赞' if self.config.pretty.favorite_rate_metric == 'fav_like' else '收藏/播放'}）、"
            f"时长 ≤ {self.config.pretty.max_duration_seconds}s、只二次元，"
            f"{'开' if self.config.pretty.enabled else '关'}）"
        )
        # 权限模式单独喊一遍：默认全员可触发是个有安全含义的默认值，不能只混在装载行里
        if mode == "anyone" and self.config.plugin.enabled and self.config.tool.enabled:
            self.ctx.logger.warning(
                "自然语言点播当前为「全员可触发」：群里任何人说一句就能让它检索并往群里发视频。"
                '要收紧就把 [tool].permission 设为 "whitelist" 并填 [tool].whitelist，'
                "或把 [tool].enabled 设为 false 只留管理员命令 /搬史。"
            )
        if self.config.plugin.enabled:
            await self._check_targets()

    async def _check_targets(self) -> None:
        """装载时把目标解析一遍：解析不到就当场告警，别等定时轮到点才静默跳过。

        注意两点：
          - 聊天流要等该群/该人产生过消息才存在，所以这里只能告警、不能禁用功能；
          - 这是**诊断**步骤，任何异常都必须被吞掉——on_load 抛异常会导致整个插件加载失败。
        """
        if self._pipeline is None:
            return
        try:
            resolved: List[str] = []
            failed: List[str] = []
            for target in self._run_config().groups:
                result = await self._pipeline.resolve_target(target)
                if result is None:
                    failed.append(target)
                else:
                    resolved.append(f"{target}（{result[1]}）")
            if resolved:
                self.ctx.logger.info(f"目标解析成功：{'、'.join(resolved)}")
            if failed:
                self.ctx.logger.warning(
                    f"目标解析不到聊天流：{'、'.join(failed)}"
                    "——定时搬运到这些目标时会跳过。群号写错了？或者该群/该人还没产生过消息？"
                    "（目标既可以是群号，也可以是 QQ 号发私聊）"
                )
        except Exception as exc:  # noqa: BLE001 - 诊断失败不能拖垮加载
            self.ctx.logger.warning(f"目标自检失败（不影响插件加载）: {exc!r}")

    async def on_unload(self) -> None:
        """停循环、落盘、关连接。宿主只给 5 秒，全部操作都要快。"""
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        self._loop_task = None
        if self._store is not None:
            await self._store.save()
        if self._client is not None:
            await self._client.aclose()

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载：判据的阈值要跟着换。"""
        self.ctx.logger.info(f"配置已更新（scope={scope} version={version}），下一轮生效")
        if self._client is not None and self._pipeline is not None:
            self._pipeline.set_judge(
                Judge(self._client, self._run_config(), self.ctx.logger),
                PrettyPicker(self._client, self._store, self.ctx.logger) if self._store else None,
            )

    # ===== 配置快照 =====

    def _run_config(self) -> RunConfig:
        """把分层配置拍平成 core 层要的一次性配置。"""
        cfg = self.config
        window = list(cfg.source.window_hours)
        while len(window) < 2:
            window.append(720)
        return RunConfig(
            groups=[str(g).strip() for g in cfg.target.groups if str(g).strip()],
            include_header_text=cfg.target.include_header_text,
            keywords=list(cfg.source.keywords),
            keywords_per_run=cfg.source.keywords_per_run,
            orders=list(cfg.source.orders),
            page_max=cfg.source.page_max,
            window_hours=window[:2],
            play_min=cfg.source.play_min,
            play_max=cfg.source.play_max,
            min_video_review=cfg.source.min_video_review,
            max_video_review=cfg.source.max_video_review,
            max_check_per_run=cfg.source.max_check_per_run,
            at_ratio_threshold=cfg.judge.at_ratio_threshold,
            min_comments=cfg.judge.min_comments,
            comment_pages=cfg.judge.comment_pages,
            max_duration_seconds=cfg.download.max_duration_seconds,
            max_size_mb=cfg.download.max_size_mb,
            quality_preference=list(cfg.download.quality_preference),
            inline_video_max_mb=cfg.download.inline_video_max_mb,
            keep_files=cfg.download.keep_files,
            title_blacklist=list(cfg.filters.title_blacklist),
            tag_blacklist=list(cfg.filters.tag_blacklist),
            up_blacklist=list(cfg.filters.up_blacklist),
            pretty_keywords=list(cfg.pretty.keywords),
            pretty_keywords_per_run=cfg.pretty.keywords_per_run,
            pretty_orders=list(cfg.pretty.orders),
            pretty_page_max=cfg.pretty.page_max,
            pretty_window_hours=list(cfg.pretty.window_hours)[:2] or [72, 720],
            pretty_play_min=cfg.pretty.play_min,
            pretty_play_max=cfg.pretty.play_max,
            pretty_max_duration_seconds=cfg.pretty.max_duration_seconds,
            pretty_rate_metric=str(cfg.pretty.favorite_rate_metric or "fav_like"),
            pretty_min_favorite_rate=cfg.pretty.min_favorite_rate,
            pretty_typename_whitelist=list(cfg.pretty.typename_whitelist),
            retry_rounds=cfg.retry.rounds,
            retry_gap_seconds=cfg.retry.gap_seconds,
            pretty_real_blacklist=list(cfg.pretty.real_blacklist),
            pretty_block_words=list(cfg.pretty.block_words),
        )

    # ===== 发言者缓存与点播权限 =====

    @HookHandler(
        RECEIVE_HOOK,
        name="repost_shi_cache_speaker",
        description="缓存各会话最近一位真人发言者，供自然语言点播做白名单校验",
        mode=HookMode.BLOCKING,
        order=HookOrder.EARLY,
        error_policy=ErrorPolicy.SKIP,  # 缓存失败绝不影响入站处理
    )
    async def cache_speaker(self, message: Dict[str, Any] | None = None, **kwargs: Any) -> None:
        """只读观察：记录发言者后原样放行（返回 None）。"""
        del kwargs
        try:
            if self._speakers is None or not isinstance(message, dict):
                return None
            session_id = str(message.get("session_id") or "").strip()
            user_info = (message.get("message_info") or {}).get("user_info") or {}
            speaker = normalize_qq(user_info.get("user_id"))
            if not session_id or not speaker:
                return None  # 系统/通知类消息没有发言者，跳过
            self._speakers.remember(
                session_id,
                speaker,
                str(user_info.get("user_cardname") or user_info.get("user_nickname") or ""),
            )
            # 顺手记下"这个群/这个人当前在用的聊天流"，发送时优先用它（见 _resolve_stream）
            if self._store is not None:
                group_info = (message.get("message_info") or {}).get("group_info") or {}
                group_id = str(group_info.get("group_id") or "").strip()
                if group_id:
                    self._store.remember_live_session(f"group:{group_id}", session_id)
                else:
                    self._store.remember_live_session(f"user:{speaker}", session_id)
        except Exception as exc:  # noqa: BLE001 - 缓存失败不影响入站
            self.ctx.logger.debug(f"缓存发言者失败（已忽略）: {exc!r}")
        return None

    # ===== 点播后不回话 =====

    @HookHandler(
        REPLY_HOOK,
        name="repost_shi_suppress_reply",
        description="点播搬史后吞掉机器人这一次的回话（只让工具把视频发出去）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        error_policy=ErrorPolicy.SKIP,  # 抑制失败不该影响正常回复链路
    )
    async def suppress_reply_after_tool(self, **kwargs: Any) -> Dict[str, Any]:
        """把这一次的回复文本置空以取消发送。

        为什么要用钩子而不是只靠提示词：模型经常在调完工具后顺口说一句「搬着呢」
        「刚给你下了几个」，而用户要的是**群里只出现视频**。宿主对空回复的处理是
        「不发送并记为 failure」，所以置空就能可靠地吞掉这次回话。
        只吞「点播之后的第一条」，避免把随后正常的聊天回复也吃掉。
        """
        try:
            if not self.config.tool.suppress_reply:
                return {"action": "continue"}
            session_id = str(kwargs.get("session_id") or "")
            deadline = self._reply_suppress.get(session_id)
            if not deadline:
                return {"action": "continue"}
            if time.time() > deadline:
                self._reply_suppress.pop(session_id, None)
                self._reply_suppress_source.pop(session_id, None)
                return {"action": "continue"}
            if str(kwargs.get('reply_message_id') or '') != self._reply_suppress_source.get(session_id):
                return {'action':'continue'}
            self._reply_suppress.pop(session_id, None)
            self._reply_suppress_source.pop(session_id, None)
            self.ctx.logger.info("点播搬史后吞掉本次回话（只发视频，不在群里多说话）")
            modified = dict(kwargs)
            modified["response"] = ""
            return {"action": "continue", "modified_kwargs": modified}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug(f"吞回话失败（已忽略）: {exc!r}")
            return {"action": "continue"}

    def _permission_mode(self) -> str:
        """规范化权限模式，未知值一律按最严格的 whitelist 处理。"""
        mode = str(self.config.tool.permission or "anyone").strip().lower()
        return mode if mode in {"anyone", "whitelist"} else "whitelist"

    def _describe_permission(self, mode: str) -> str:
        if mode == "anyone":
            return "全员可触发（仅按群冷却限流）"
        count = len([q for q in (normalize_qq(x) for x in self.config.tool.whitelist) if q])
        return f"白名单（{count} 个 QQ）" if count else "白名单（名单为空，实际无人可触发）"

    def _permission_denied_reason(self, stream_id: str) -> str:
        """返回拒绝原因；允许则返回空串。"""
        mode = self._permission_mode()
        if mode == "anyone":
            return ""
        allowed = {q for q in (normalize_qq(x) for x in self.config.tool.whitelist) if q}
        if not allowed:
            self.ctx.logger.warning("点播权限设为 whitelist 但白名单为空，已拒绝本次触发")
            return "这个功能当前只对白名单开放，但白名单还没配"
        speaker = self._speakers.resolve(stream_id) if self._speakers is not None else None
        if speaker is None:
            # 工具回调拿不到发言者，只能靠入站钩子的缓存；拿不到就拒绝，不能放过
            return "没认出你是谁（缓存里没有你的发言），先在群里说句话再叫我"
        if speaker["user_id"] not in allowed:
            # 缓存的是"该会话最近发言者"，可能不是本次调用者：拒绝的同时丢掉这条缓存，
            # 逼下一次触发重新经过入站钩子，缩短旧条目可能被蹭到的窗口
            if self._speakers is not None:
                self._speakers.forget(stream_id)
            return f"这个功能只对白名单开放（{speaker['user_id']} 不在名单里）"
        return ""

    # ===== 定时循环 =====

    async def _schedule_loop(self) -> None:
        """每分钟醒一次看是否到点；被 cancel 时立刻退出，配合宿主 5 秒卸载预算。"""
        while True:
            try:
                self._wake.clear()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=60)
            except asyncio.CancelledError:
                break
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.error(f"定时搬运异常: {exc!r}", exc_info=True)

    async def _tick(self) -> None:
        """到点就跑一轮。"""
        if self._pipeline is None or self._store is None:
            return
        cfg = self.config
        if not cfg.plugin.enabled:
            return
        if cfg.target.mode == "planner":
            await self._planner_tick()
            return
        now = datetime.now()
        store = self._store
        store.ensure_plan(
            now,
            times_per_day=cfg.target.daily_times,
            hour_start=int(cfg.target.active_hours[0]),
            hour_end=int(cfg.target.active_hours[1]),
        )
        due = store.next_due(now)
        if due is None:
            await store.save()
            return
        if now.timestamp() < due:
            await store.save()
            return
        # 过期太久（比如插件刚被重启）就跳过，避免开服瞬间补发三条
        if now.timestamp() - due > max(1, cfg.target.catch_up_minutes) * 60:
            store.mark_plan_done(due)
            await store.save()
            self.ctx.logger.info("错过的发送时刻已跳过（超出补发窗口）")
            return
        store.mark_plan_done(due)
        await store.save()
        await self._run_and_report(self._run_config())

    async def _planner_tick(self) -> None:
        """只向核心提交跟玩机会；不搜索、不下载、不直接投递。包括外壳已解析后仅入库的消息。"""
        # Plugin registration order is not stable: tieba-share may load after
        # this coordinator. Refresh source capabilities on each planner tick
        # instead of permanently caching an on-load unavailable result.
        try:
            self._source_status = discover()
        except Exception as exc:
            self.ctx.logger.debug('??????????: %s', type(exc).__name__)
        now = datetime.now()
        start, end = self.config.target.active_hours[:2]
        active = start <= now.hour < end if start < end else now.hour >= start or now.hour < end
        if not active or self._manual_busy or self._running or not self.config.tool.enabled:
            return
        cfg = self.config.planner
        for group_id in self.config.target.groups:
            from utils.platform_target import is_group_target
            if not is_group_target(group_id):
                continue
            try:
                resolved = await self._pipeline._resolve_stream(group_id)
                if not resolved or resolved[1] != '群':
                    continue
                stream_id = resolved[0]
                payload = await self.ctx.call_capability('message.get_recent', chat_id=stream_id,
                    hours=cfg.window_seconds / 3600, limit=200, filter_mai=True, include_binary_data=False)
                messages = payload.get('messages', []) if isinstance(payload, dict) else payload
                if not isinstance(messages, list):
                    continue
                stamp = now.timestamp()
                stats = summarize(messages, now=stamp, window=cfg.window_seconds, group_id=group_id,
                                  ignored_users=cfg.ignored_user_ids)
                self._activity[stream_id] = stats
                normal_mode = eligible(stats, cfg, stamp)
                quiet_mode = low_activity_eligible(stats, cfg, stamp)
                if not normal_mode and not quiet_mode:
                    continue
                gap = (cfg.hot_gap_seconds if normal_mode and is_hot(stats, cfg) else cfg.normal_gap_seconds if normal_mode else cfg.low_activity_gap_seconds)
                state = self._store.planner_state(stream_id)
                # 正常跟玩按“最新分享”去重；低活跃补位按时间间隔运行，
                # 否则安静群的 newest_share=0 会让它从未有机会搬第一条。
                if stamp - state.get('last_intent', 0) < gap:
                    continue
                if normal_mode and stats['newest_share'] <= state.get('newest_share', 0):
                    continue
                if self._store.manual_block_reason(stream_id, gap):
                    continue
                # Reserve before RPC: a timeout may still have queued the task. New shares are required for another intent.
                self._store.record_planner(
                    stream_id,
                    stamp,
                    stats['newest_share'] if normal_mode else stats.get('newest', 0),
                )
                await self._store.save()
                target_platform = 'qq' if str(group_id).startswith('qq:') else 'wechat'
                available_sources = [
                    source for source in self.config.target.source_channels
                    if self._source_status.get(source, {}).get('available')
                    and (source != 'qq_forward' or target_platform == 'qq')
                ]
                source_hint = (
                    available_sources[int(stamp // max(60, cfg.repost_gap_seconds)) % len(available_sources)]
                    if available_sources else 'bilibili'
                )
                if cfg.coordinator:
                    result = await self.ctx.api.call(
                        'autonomous_life.repost_opportunity',
                        group_id=group_id,
                        low_activity=quiet_mode and not normal_mode,
                        force=True,
                        daily_limit=cfg.repost_daily_limit,
                        gap_seconds=cfg.repost_gap_seconds,
                        action_limit=cfg.repost_action_limit,
                        source=source_hint,
                    )
                    coordinated = bool(result.get('success')) if isinstance(result, dict) else False
                    self.ctx.logger.info('搬屎机会交给自主生活限额调度: %s', coordinated)
                    if coordinated:
                        continue
                    # A failed/退避 autonomous coordinator must not swallow a
                    # real repost opportunity. Fall through to the existing
                    # density Planner path, which still observes its own
                    # cooldown and share limits.
                    self.ctx.logger.warning('自主生活未接管搬屎机会，回退到搬屎 Planner 路径: %s', result)
                intent = (
                    f"最近{cfg.window_seconds // 60}分钟，群里有{stats['users']}位群友发了{stats['messages']}条消息，"
                    f"其中{stats['shares']}条涉及图像/视频分享或搬屎话题，{stats['bili']}条带B站链接或BV号。"
                    "这是群聊活跃度观察，不是任何人的点播请求。请看真实近期聊天，判断是否适合跟着群友一起整活。"
                    "先判断这些分享是否为抽象搞笑整活，普通照片、工作截图不代表在发屎。"
                    + ("当前是低活跃补位机会：群里没有足够的新分享，只有少量真人消息；如果不打扰，"
                     "可以从相关兴趣中搬一条轻量内容。" if quiet_mode and not normal_mode else
                     "如果群友持续发屎或使用B站解析，气氛合适时可以更积极地调用repost_shi搬一条新的相关视频；")
                    + "也可以只聊天或保持安静。群里在认真讨论、反感刷屏或话题已经过去时不要搬。"
                    + "不要重复群友刚分享的视频，不要自动触发其他机器人的命令，不要把统计说明发到群里。"
                    + planner_instruction(self._source_status)
                    + f"本轮建议优先来源：{source_hint}；若该来源执行失败，再记录失败，不要假装已搬。"
                    + "决定搬运时只调用一次可用来源工具，count=1，不额外发送开场白。"
                )
                result = await self.ctx.maisaka.proactive.trigger(stream_id, intent,
                    reason='群内视频分享活跃，交由planner判断是否跟玩', priority='normal', metadata=stats)
                if not isinstance(result, dict) or result.get('success') is False or result.get('queued') is not True:
                    self.ctx.logger.warning(f"搬屎跟玩机会未被核心接收：{group_id}，{result!r}")
                    continue
                self.ctx.logger.info(f"搬屎跟玩机会已交给planner：{group_id}，消息{stats['messages']}，分享{stats['shares']}")
            except Exception as exc:
                self.ctx.logger.warning(f"搬屎活跃度检查失败（未直接搬运）: {exc!r}")

    def _tool_gap(self, stream_id: str) -> int:
        cfg = self.config.planner
        stats = self._activity.get(stream_id) or {}
        if (self.config.target.mode == 'planner' and eligible(stats, cfg, time.time())
                and is_hot(stats, cfg)):
            return min(self.config.tool.min_gap_seconds, cfg.hot_gap_seconds)
        return self.config.tool.min_gap_seconds

    @API('repost.autonomous_one', description='只消费统一限额调度的一次性授权，搬一条视频', public=True, timeout_ms=180000)
    async def autonomous_one(self, group_id: str = '', query: str = '', grant: str = '', **kwargs):
        if (not self.config.plugin.enabled or not self.config.planner.coordinator
                or group_id not in self.config.target.groups or self._running or self._manual_busy):
            return {'success': False, 'reason': 'disabled, disallowed or busy'}
        authorized = await self.ctx.api.call('autonomous_life.consume_repost_grant', grant=grant, group_id=group_id)
        grant_result = authorized.get('result', authorized) if isinstance(authorized, dict) else {}
        if not isinstance(grant_result, dict) or grant_result.get('success') is not True:
            return {'success': False, 'reason': 'no valid single-use share grant'}
        resolved = await self._pipeline._resolve_stream(group_id)
        if not resolved or resolved[1] != '群':
            return {'success': False, 'reason': 'no real group stream'}
        source = str(kwargs.get('source') or '').strip().lower()
        if source in {'tieba', 'qq_forward'}:
            external = await self._run_external_source(source, group_id, resolved[0], query)
            return {'success': True, **external}
        report = await self._run_and_report(self._run_config(), override_stream=resolved[0], extra_keywords=[query] if query else [])
        return {'success': True, **report}

    async def _run_external_source(self, source: str, group_id: str, stream_id: str, query: str = '') -> Dict[str, Any]:
        """调用已注册的贴吧/QQ 原样转发工具，并把结果纳入统一搬屎回执。"""
        from src.plugin_runtime.component_query import component_query_service

        if source == 'tieba':
            tool_name = 'tieba_share'
            args = {'stream_id': stream_id, 'chat_id': stream_id, 'count': 1, 'thread_type': 'any'}
            if query:
                args['forum'] = query.removesuffix('吧')[:80]
        else:
            tool_name = 'cateye_forward_messages'
            payload = await self.ctx.call_capability(
                'message.get_recent', chat_id=stream_id, hours=12, limit=6,
                filter_mai=True, include_binary_data=False,
            )
            rows = payload.get('messages', []) if isinstance(payload, dict) else payload
            ids = [str(row.get('message_id') or '') for row in (rows or []) if isinstance(row, dict)]
            ids = [value for value in ids if value][-6:]
            if not ids:
                return {'sent': False, 'source': source, 'reason': '没有可打包的近期 QQ 消息'}
            args = {
                'stream_id': stream_id, 'chat_id': stream_id,
                'message_ids': ids, 'target_stream_id': stream_id, 'merge': True,
            }
        executor = component_query_service.get_tool_executor(tool_name)
        if executor is None:
            return {'sent': False, 'source': source, 'reason': f'{tool_name} 未注册'}
        try:
            result = await executor(args)
        except Exception as exc:
            self.ctx.logger.warning('统一搬屎外部来源执行失败 source=%s: %s', source, type(exc).__name__)
            return {'sent': False, 'source': source, 'reason': type(exc).__name__}
        if not isinstance(result, dict):
            result = {'content': str(result)}
        sent = bool(result.get('sent') or result.get('success'))
        self.ctx.logger.info('统一搬屎外部来源完成 source=%s sent=%s target=%s', source, sent, group_id)
        return {'sent': sent, 'source': source, 'detail': result}

    async def _run_and_report(
        self,
        cfg: RunConfig,
        override_stream: str = "",
        extra_keywords: Sequence[str] = (),
        channel: str = "shi",
    ) -> Dict[str, Any]:
        """跑一轮并汇报结果，带重入保护。"""
        if self._pipeline is None:
            return {"sent": False, "reason": "插件尚未就绪"}
        if self._running:
            return {"sent": False, "reason": "上一轮还在进行中"}
        self._running = True
        try:
            report = await self._pipeline.run_once(
                cfg,
                override_stream=override_stream,
                extra_keywords=extra_keywords,
                channel=channel,
            )
        finally:
            self._running = False
        if self._store is not None:
            await self._store.save()
        if report.get("sent"):
            candidate = report.get("candidate") or {}
            self.ctx.logger.info(f"本轮已搬：{candidate.get('title')}")
        else:
            self.ctx.logger.info(f"本轮未搬：{report.get('reason')}")
        return report

    # ===== 口语触发（命令链：确定性、不经过模型、不回话）=====

    @Command(
        "repost_by_words",
        description="口语搬史：群里说「来点史/搬点屎/来点抽象的」就自动搬一条（不走模型、机器人不回话）",
        pattern=SHI_TRIGGER_PATTERN,
    )
    async def cmd_repost_by_words(
        self, text: str = "", stream_id: str = "", user_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """按口语触发搬史。返回 intercept=1，消息不再进 LLM。"""
        del text, kwargs
        return await self._trigger_by_words("shi", stream_id, user_id)

    @Command(
        "pretty_by_words",
        description="口语点播好看的：群里说「来点涩的/来点黄的/来点好看的/来点好康的」就自动搬一条（不走模型、不回话）",
        pattern=PRETTY_TRIGGER_PATTERN,
    )
    async def cmd_pretty_by_words(
        self, text: str = "", stream_id: str = "", user_id: str = "", **kwargs: Any
    ) -> Tuple[bool, str, int]:
        """按口语触发好看频道。返回 intercept=1，消息不再进 LLM。"""
        del text, kwargs
        return await self._trigger_by_words("pretty", stream_id, user_id)

    def _permission_denied_for_user(self, user_id: str) -> str:
        """用真实 QQ 号判权限（命令链能拿到真实发言者，比工具那条缓存路径更准）。"""
        mode = self._permission_mode()
        if mode == "anyone":
            return ""
        allowed = {q for q in (normalize_qq(x) for x in self.config.tool.whitelist) if q}
        if not allowed:
            return "白名单还没配"
        speaker = normalize_qq(user_id)
        if not speaker:
            return "没认出你是谁"
        if speaker not in allowed:
            return f"只对白名单开放（{speaker} 不在名单里）"
        return ""

    async def _trigger_by_words(self, channel: str, stream_id: str, user_id: str) -> Tuple[bool, str, int]:
        """口语触发的公共实现（两个频道共用）。"""
        # 命令链的返回值不会发到群里（宿主只写日志），所以这里的文字只是给日志看的
        if not self.config.plugin.enabled:
            return False, "插件未启用", 0
        if not stream_id:
            return False, "拿不到当前会话", 0
        if channel == "pretty" and not self.config.pretty.enabled:
            return False, "好看频道未启用", 0
        denied = self._permission_denied_for_user(user_id)
        if denied:
            return False, f"权限不足：{denied}", 1
        if self._store is None:
            return False, "插件未就绪", 0
        if self._manual_busy or self._running:
            return False, "正在搬上一条", 1

        key = f"{stream_id}#pretty" if channel == "pretty" else stream_id
        gap = (
            self.config.pretty.min_gap_seconds if channel == "pretty" else self.config.tool.min_gap_seconds
        )
        block = self._store.manual_block_reason(key, min_gap_seconds=gap)
        if block:
            return False, f"被拒绝：{block}", 1

        self._store.record_manual(key)
        await self._store.save()
        self._manual_busy = True
        task = asyncio.create_task(
            self._manual_share(stream_id, 1, [], channel=channel), name=f"mai-repost-{channel}-words"
        )
        task.add_done_callback(self._on_manual_done)
        # intercept=1：消息不进 LLM，机器人不会为这条消息说话
        return True, f"已触发{channel}频道搬运", 1

    # ===== 自然语言点播（LLM Tool，模型主动调用时走这条）=====

    @Tool(
        TOOL_REPOST,
        brief_description=(
            "从B站搬一条「史」到当前群：专挑评论区艾特率超过 50% 的抽象视频，"
            "下载成 720p 后以说明文字和 MP4 文件的形式发出来。"
            "当有人想搬史、要来点抽象的、想看乐子、点名要某类整活/鬼畜视频时调用。"
            "群友持续分享视频或使用B站解析时，也可根据近期聊天气氛自主跟玩；不需要先有人点播。"
            "调用后立刻返回，视频会在后台下载并自动以说明文字和 MP4 文件发出。"
            "**调用之后不要再调用 reply、也不要回任何文字**——群里只会看到搬出来的视频；"
            "不要追问「要不要」「确定吗」，也不要应一声「搬着呢」，插件会吞掉这次回话。"
        ),
        parameters=[
            ToolParameterInfo(
                name="topic",
                param_type=ToolParamType.STRING,
                description="对方点名的口味或主题，例如「鬼畜」「抽象」「答辩」「游戏」；没点名就留空",
                required=False,
            ),
            ToolParameterInfo(
                name="count",
                param_type=ToolParamType.INTEGER,
                description="要几条，默认 1；对方说「来几个」「多来点」时给 2 或 3",
                required=False,
                default=1,
            ),
        ],
        visibility="visible",
        chat_scope="all",
    )
    async def tool_repost(self, topic: str = "", count: int = 1, **kwargs: Any) -> Dict[str, Any]:
        """群友用自然语言点播一条（或几条）史。"""
        request = kwargs.get('_request_context') or {}
        stream_id = str(request.get('stream_id') or '') if request.get('platform') in {'wechat', 'qq'} else ''
        if not stream_id:
            return self._tool_result("拿不到当前会话，没法搬。")
        if not self.config.plugin.enabled:
            return self._tool_result("搬史功能没开（插件配置里 [plugin].enabled 还是 false）。")
        if not self.config.tool.enabled:
            return self._tool_result("自然语言点播被关掉了，要用的话让管理员开 [tool].enabled。")
        # 权限：命令是 operator 专属，工具默认全员可用但可收紧成白名单
        denied = self._permission_denied_for_user(str(request.get('user_id') or ''))
        if denied:
            return self._tool_result(f"先不搬：{denied}。管理员可以用 /搬史 指令搬。")
        if self._store is None:
            return self._tool_result("插件还没就绪，稍后再试。")
        if self._manual_busy or self._running:
            return self._tool_result("正在搬上一条，等它发出来再说。")

        limit = max(1, self.config.tool.max_count)
        wanted = max(1, min(int(count or 1), limit))
        # 不限次数、不设冷却，只在"刚搬完又来一条"时拒绝（避免贴脸连点）
        block = self._store.manual_block_reason(stream_id, min_gap_seconds=self._tool_gap(stream_id))
        if block:
            return self._tool_result(f"先不搬：{block}。")

        self._store.record_manual(stream_id)
        await self._store.save()
        extra = [topic.strip()] if topic and topic.strip() else []
        self._manual_busy = True
        task = asyncio.create_task(self._manual_share(stream_id, wanted, extra), name="mai-repost-shit-manual")
        task.add_done_callback(self._on_manual_done)
        # 点播后吞掉机器人这一次的回话：只把视频发出去，不在群里多说话
        if self.config.tool.suppress_reply:
            self._reply_suppress_source[stream_id] = str(request.get('message_id') or '')
            self._reply_suppress[stream_id] = time.time() + max(
                10, self.config.tool.suppress_reply_window_seconds
            )
        return self._tool_result(
            "已提交搬运，视频会在后台下载并以说明文字和 MP4 文件自动发到这个群，约一两分钟。"
            "不要再调用 reply，也不要回复任何文字。"
        )

    @Tool(
        TOOL_PRETTY,
        brief_description=(
            "从B站搬一条「好看的」二次元视频到当前群：只要纸片人（MMD/手书/MAD/AMV/插画/动画），"
            "不要真人出镜；挑时长 2 分钟以内、收藏率高的。"
            "当有人说「来点涩的/来点色的/来点黄的/来点好看的/来点好康的/来点二次元」这类话时调用。"
            "**调用之后不要再调用 reply、也不要回任何文字**——插件会吞掉这次回话，群里只出现视频。"
            "不要追问「要哪种」，不要应一声「找着呢」。"
        ),
        parameters=[
            ToolParameterInfo(
                name="topic",
                param_type=ToolParamType.STRING,
                description="对方点名的类型，例如「MMD」「手书」「插画」「壁纸」「初音」；没点名就留空",
                required=False,
            ),
            ToolParameterInfo(
                name="count",
                param_type=ToolParamType.INTEGER,
                description="要几条，默认 1；对方说「来几个」时给 2 或 3",
                required=False,
                default=1,
            ),
        ],
        visibility="visible",
        chat_scope="all",
    )
    async def tool_pretty(self, topic: str = "", count: int = 1, **kwargs: Any) -> Dict[str, Any]:
        """用自然语言点播「好看的」二次元视频。"""
        tunnel = "pretty"
        request = kwargs.get('_request_context') or {}
        stream_id = str(request.get('stream_id') or '') if request.get('platform') in {'wechat', 'qq'} else ''
        if not stream_id:
            return self._tool_result("拿不到当前会话，没法找。", tunnel)
        if not self.config.plugin.enabled:
            return self._tool_result("搬视频功能没开（插件配置里 [plugin].enabled 还是 false）。", tunnel)
        if not self.config.pretty.enabled:
            return self._tool_result("「好看的」频道被关掉了，要用的话让管理员开 [pretty].enabled。", tunnel)
        denied = self._permission_denied_for_user(str(request.get('user_id') or ''))
        if denied:
            return self._tool_result(f"先不搬：{denied}。管理员可以用 /搬好看 指令。", tunnel)
        if self._store is None:
            return self._tool_result("插件还没就绪，稍后再试。", tunnel)
        if self._manual_busy or self._running:
            return self._tool_result("正在搬上一条，等它发出来再说。", tunnel)

        limit = max(1, self.config.tool.max_count)
        wanted = max(1, min(int(count or 1), limit))
        # 不限次数、不设冷却；两个频道的最小间隔分开记
        block = self._store.manual_block_reason(
            f"{stream_id}#pretty", min_gap_seconds=self.config.pretty.min_gap_seconds
        )
        if block:
            return self._tool_result(f"先不搬：{block}。", tunnel)

        self._store.record_manual(f"{stream_id}#pretty")
        await self._store.save()
        extra = [topic.strip()] if topic and topic.strip() else []
        self._manual_busy = True
        task = asyncio.create_task(
            self._manual_share(stream_id, wanted, extra, channel=tunnel),
            name="mai-repost-shit-pretty",
        )
        task.add_done_callback(self._on_manual_done)
        if self.config.tool.suppress_reply:
            self._reply_suppress_source[stream_id] = str(request.get('message_id') or '')
            self._reply_suppress[stream_id] = time.time() + max(
                10, self.config.tool.suppress_reply_window_seconds
            )
        return self._tool_result(
            "已提交，视频会在后台下载并以说明文字和 MP4 文件自动发到这个群。"
            "不要再调用 reply，也不要回复任何文字。",
            tunnel,
        )

    async def _manual_share(
        self, stream_id: str, wanted: int, extra_keywords: List[str], channel: str = "shi"
    ) -> None:
        """后台跑完手动点播：每条都是完整的检索→判据→下载→投递。"""
        try:
            cfg = self._run_config()
            for index in range(wanted):
                report = await self._run_and_report(
                    cfg, override_stream=stream_id, extra_keywords=extra_keywords, channel=channel
                )
                if report.get("sent"):
                    continue
                reason = str(report.get("reason") or "没找到合适的")
                # 第一条就没搬成才回话；后续条数失败保持安静，避免刷屏
                if index == 0:
                    with contextlib.suppress(Exception):
                        await self.ctx.send.text(f"这次没搬成：{reason}", stream_id)
                break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.error(f"点播搬运异常: {exc!r}", exc_info=True)

    def _on_manual_done(self, task: asyncio.Task[None]) -> None:
        """收尾：清忙标志，并把异常取出来记日志（否则会被静默吞掉）。"""
        self._manual_busy = False
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self.ctx.logger.error(f"点播任务失败: {exc!r}")

    @staticmethod
    def _tool_result(content: str, tool_name: str = TOOL_REPOST) -> Dict[str, Any]:
        """构造 Tool 返回值（内容会进入模型上下文）。"""
        return {"name": tool_name, "content": content}

    # ===== 命令 =====

    @Command(
        "shitpost_now",
        description="立刻按配置搬一条史到指定群（需管理员）",
        pattern=r"^/搬史$",
        permission="operator",
    )
    async def cmd_share_now(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, int]:
        """手动触发一次搬运（仍走配置里的目标群）。"""
        del kwargs
        if not self.config.plugin.enabled:
            return False, "插件未启用，请先在配置里打开", 0
        if not self.config.target.groups:
            return False, "还没有配置目标群号", 0
        cfg = self._run_config()
        report = await self._run_and_report(cfg)
        if report.get("sent"):
            candidate = report.get("candidate") or {}
            return True, f"已搬：{candidate.get('title')}", 1
        return False, f"没搬成：{report.get('reason')}", 0

    @Command(
        "shitpost_here",
        description="立刻搬一条史发到当前会话，用来试效果（需管理员）",
        pattern=r"^/搬史测试$",
        permission="operator",
    )
    async def cmd_share_here(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, int]:
        """把一条视频发到当前会话，便于在不打扰群的情况下验证投递链路。"""
        del kwargs
        if not stream_id:
            return False, "拿不到当前会话 ID", 0
        cfg = self._run_config()
        report = await self._run_and_report(cfg, override_stream=stream_id)
        if report.get("sent"):
            candidate = report.get("candidate") or {}
            return True, f"已发到本会话：{candidate.get('title')}", 1
        return False, f"没成功：{report.get('reason')}", 0

    @staticmethod
    def _mask_groups(groups: List[str]) -> str:
        """群号打码：/搬史状态 任何会话都能调，配置里的群号不必暴露完整。

        只保留首 3 位与末 2 位（群号本来就是群里可见的，打码是防止被顺手抄走）。
        """
        masked: List[str] = []
        for raw in groups:
            value = str(raw).strip()
            if not value:
                continue
            masked.append(value if len(value) <= 5 else f"{value[:3]}****{value[-2:]}")
        return "、".join(masked) if masked else "（未配置）"

    @staticmethod
    def _sanitize_bvid(bvid: str) -> str:
        """文件名只允许字母数字，避免上游返回值里混进路径字符。"""
        safe = "".join(ch for ch in str(bvid) if ch.isalnum())
        return safe or "video"

    @Command(
        "pretty_here",
        description="立刻搬一条「好看的」二次元视频到当前会话（需管理员）",
        pattern=r"^/搬好看$",
        permission="operator",
    )
    async def cmd_pretty_here(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, int]:
        """把一条好看的二次元视频发到当前会话。"""
        del kwargs
        if not stream_id:
            return False, "拿不到当前会话 ID", 0
        if not self.config.pretty.enabled:
            return False, "「好看的」频道未启用", 0
        report = await self._run_and_report(
            self._run_config(), override_stream=stream_id, channel="pretty"
        )
        if report.get("sent"):
            candidate = report.get("candidate") or {}
            return True, f"已搬：{candidate.get('title')}", 1
        return False, f"没搬成：{report.get('reason')}", 0

    @Command(
        "shitpost_status",
        description="查看今日搬运计划与最近记录",
        pattern=r"^/搬史状态$",
    )
    async def cmd_status(self, stream_id: str = "", **kwargs: Any) -> Tuple[bool, str, int]:
        """回报计划、配额与最近搬过什么。"""
        del kwargs
        if self._store is None:
            return False, "插件尚未就绪", 0
        now = datetime.now()
        if self.config.target.mode == 'scheduled':
            self._store.ensure_plan(now, self.config.target.daily_times, *self.config.target.active_hours[:2])
        plan = self._store.plan_summary(now)
        recent = self._store.recent_sent(5)
        lines = [
            ("按群聊活跃度交由planner决定，固定时刻不直接搬运" if self.config.target.mode == 'planner' else
             f"今日计划 {plan['done']}/{plan['total']} 次，时刻 {', '.join(plan['times']) or '无'}"),
            f"时间窗 {self.config.source.window_hours} 小时，播放 "
            f"{self.config.source.play_min}~{self.config.source.play_max}",
            f"判据：艾特率 ≥ {self.config.judge.at_ratio_threshold:.0%}"
            f" 且评论 ≥ {self.config.judge.min_comments} 条",
            f"目标群 {self._mask_groups(self.config.target.groups)}",
            "投递方式 微信说明文字与 MP4 文件；失败只记本地日志，不在群里说话",
            f"自然语言点播：{'开' if self.config.tool.enabled else '关'}"
            f"（{self._describe_permission(self._permission_mode())}，"
            f"不限次数，最小间隔 {self.config.tool.min_gap_seconds} 秒）",
        ]
        if recent:
            lines.append("最近搬过：")
            for record in reversed(recent):
                when = datetime.fromtimestamp(record.get("sent_at", 0)).strftime("%m-%d %H:%M")
                if str(record.get("channel") or "shi") == "pretty":
                    label = "收藏/点赞" if str(record.get("favorite_metric") or "fav_like") == "fav_like" else "收藏/播放"
                    metric = f"收藏率 {float(record.get('favorite_ratio') or 0):.0%}（{label}）"
                else:
                    metric = f"艾特率 {float(record.get('at_ratio') or 0):.0%}"
                lines.append(f"· {when} {str(record.get('title') or '')[:24]}（{metric}）")
        text = "\n".join(lines)
        if stream_id:
            await self.ctx.send.text(text, stream_id)
        return True, text, 1


def create_plugin() -> BilibiliShitpostPlugin:
    """插件工厂。"""
    return BilibiliShitpostPlugin()
