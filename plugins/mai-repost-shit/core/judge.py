"""候选判定：把搜索结果收敛成「值得搬的一条」。

判据只有一条硬指标——评论区含 @ 的比例。实测这是「屎」的真信号：
鬼畜区 5万~9万播放样本 4/4 命中，中位 64.4%，而对照区只有个位数百分比。
但比值必须有分母，所以同时要求采到的评论数达到下限，否则一律判为样本不足。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .bili_api import BiliClient, BiliError

_DURATION_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


def parse_duration(text: str) -> int:
    """把搜索接口的 "1:23" / "12:34" 时长文本解析成秒。"""
    match = _DURATION_RE.match((text or "").strip())
    if not match:
        return 0
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds)


def _hit_blacklist(text: str, words: Sequence[str]) -> Optional[str]:
    lowered = (text or "").lower()
    for word in words:
        word = (word or "").strip()
        if word and word.lower() in lowered:
            return word
    return None


@dataclass
class Candidate:
    """一条通过判据、可以进入下载与发送环节的候选。"""

    bvid: str
    aid: int
    cid: int
    title: str
    author: str
    author_mid: int
    play: int
    like: int
    favorite: int
    duration: int
    typename: str
    tags: List[str] = field(default_factory=list)
    at_ratio: float = 0.0
    at_count: int = 0
    comment_count: int = 0
    reject_reason: str = ""
    # 频道：shi = 搬史（按评论区艾特率），pretty = 来点好看的（按收藏率、短时长、只二次元）
    channel: str = "shi"
    # 好看频道：算好的收藏率与它用的口径（fav_like = 收藏/点赞、fav_play = 收藏/播放）
    favorite_ratio: float = 0.0
    favorite_metric: str = "fav_like"

    def header_text(self) -> str:
        """合并转发里视频上方的那段说明（按频道给不同的数据口径）。"""
        minutes, seconds = divmod(self.duration, 60)
        common = (
            f"【{self.title}】\n"
            f"UP：{self.author}　分区：{self.typename}\n"
            f"播放 {self.play:,}　点赞 {self.like:,}　收藏 {self.favorite:,}\n"
            f"时长 {minutes}分{seconds}秒　"
        )
        if self.channel == "pretty":
            label = "收藏/点赞" if self.favorite_metric == "fav_like" else "收藏/播放"
            tail = f"收藏率 {self.favorite_ratio:.0%}（{label}）\n"
        else:
            tail = f"评论区艾特率 {self.at_ratio:.0%}（{self.at_count}/{self.comment_count}）\n"
        return f"{common}{tail}https://www.bilibili.com/video/{self.bvid}"


class Judge:
    """候选筛选器。"""

    def __init__(self, client: BiliClient, cfg: Any, logger: Any) -> None:
        self._client = client
        self._cfg = cfg
        self._logger = logger

    def prefilter(self, item: Dict[str, Any]) -> Optional[str]:
        """不花请求就能否掉的候选，返回拒绝原因。

        只做三类判断：付费/合作视频、时长超上限、以及用户自己配的屏蔽词。
        不再做任何内容安全过滤——判据就是评论区艾特率，搬的就是屎。
        """
        if int(item.get("is_pay") or 0) or int(item.get("is_union_video") or 0):
            return "付费/合作视频"
        if parse_duration(str(item.get("duration") or "")) > self._cfg.max_duration_seconds:
            return "时长超上限"
        title = str(item.get("title") or "")
        hit = _hit_blacklist(title, self._cfg.title_blacklist)
        if hit:
            return f"标题命中屏蔽词「{hit}」"
        hit = _hit_blacklist(str(item.get("tag") or ""), self._cfg.tag_blacklist)
        if hit:
            return f"标签命中屏蔽词「{hit}」"
        hit = _hit_blacklist(str(item.get("author") or ""), self._cfg.up_blacklist)
        if hit:
            return f"UP 命中屏蔽词「{hit}」"
        return None

    async def evaluate(self, item: Dict[str, Any]) -> Candidate:
        """对一个候选做完整判定：拉详情 → 取评论 → 算艾特率。"""
        bvid = str(item.get("bvid") or "")
        candidate = Candidate(
            bvid=bvid,
            aid=int(item.get("aid") or 0),
            cid=0,
            title=str(item.get("title") or "").replace('<em class="keyword">', "").replace("</em>", ""),
            author=str(item.get("author") or ""),
            author_mid=int(item.get("mid") or 0),
            play=int(item.get("play") or 0),
            like=int(item.get("like") or 0),
            favorite=int(item.get("favorites") or 0),
            duration=parse_duration(str(item.get("duration") or "")),
            typename=str(item.get("typename") or ""),
            tags=[t for t in str(item.get("tag") or "").split(",") if t],
        )
        try:
            detail = await self._client.video_detail(bvid)
        except BiliError as exc:
            candidate.reject_reason = f"取详情失败: {exc}"
            return candidate
        candidate.cid = int(detail.get("cid") or 0)
        candidate.aid = int(detail.get("aid") or candidate.aid)
        candidate.duration = int(detail.get("duration") or candidate.duration)
        stat = detail.get("stat") or {}
        candidate.play = int(stat.get("view") or candidate.play)
        candidate.like = int(stat.get("like") or candidate.like)
        candidate.favorite = int(stat.get("favorite") or candidate.favorite)
        candidate.typename = str(detail.get("tname") or candidate.typename)
        candidate.author = str((detail.get("owner") or {}).get("name") or candidate.author)
        candidate.title = str(detail.get("title") or candidate.title)
        if not candidate.cid or not candidate.aid:
            candidate.reject_reason = "缺少 cid/aid"
            return candidate
        if candidate.duration > self._cfg.max_duration_seconds:
            candidate.reject_reason = "时长超上限"
            return candidate

        try:
            messages = await self._client.fetch_comments(candidate.aid, self._cfg.comment_pages)
        except BiliError as exc:
            candidate.reject_reason = f"取评论失败: {exc}"
            return candidate
        ratio, hit, total = BiliClient.at_ratio(messages)
        candidate.at_ratio, candidate.at_count, candidate.comment_count = ratio, hit, total

        # 分母不够时比值没有统计意义，直接判样本不足，不要把噪声当信号
        if total < self._cfg.min_comments:
            candidate.reject_reason = f"评论样本不足（{total} < {self._cfg.min_comments}）"
            return candidate
        if ratio < self._cfg.at_ratio_threshold:
            candidate.reject_reason = f"艾特率不够（{ratio:.0%} < {self._cfg.at_ratio_threshold:.0%}）"
            return candidate
        return candidate
