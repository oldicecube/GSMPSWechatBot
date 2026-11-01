"""搬运流水线：检索 → 预筛 → 艾特率判定 → 下载 → 投递 → 记账。

一次 run 只搬一条。候选漏斗的顺序是刻意的：
先用搜索接口自带的 video_review（评论数）和屏蔽词做零成本过滤，
再对少量候选花请求算艾特率，最后只对通过的那一条下载视频。
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .bili_api import BiliClient, BiliError
from .deliver import Deliverer, append_context
from .judge import Candidate, Judge
from .pretty import PrettyPicker
from .state import StateStore


class Pipeline:
    """一次搬运流程。"""

    def __init__(
        self,
        ctx: Any,
        client: BiliClient,
        judge: Judge,
        store: StateStore,
        logger: Any,
        work_dir: Path,
        pretty: Optional[PrettyPicker] = None,
    ) -> None:
        self._ctx = ctx
        self._client = client
        self._judge = judge
        self._pretty = pretty
        self._store = store
        self._logger = logger
        self._work_dir = work_dir
        self._deliverer = Deliverer(ctx, logger)

    def set_judge(self, judge: Judge, pretty: Optional[PrettyPicker] = None) -> None:
        """热重载配置后换掉判据（阈值可能变了）。"""
        self._judge = judge
        if pretty is not None:
            self._pretty = pretty

    # ===== 检索 =====

    async def _collect_candidates(self, cfg: Any, extra_keywords: Sequence[str] = ()) -> List[Dict[str, Any]]:
        """跑几个关键词，收集落在播放区间、评论数够、没查过的候选。

        extra_keywords 来自自然语言请求里的主题词（比如「来点鬼畜的」），
        它们优先占用本轮的关键词名额，保证点播的口味真的被搜到。
        """
        pool = [k.strip() for k in extra_keywords if k and k.strip()]
        rest = [k for k in cfg.keywords if k.strip() and k not in pool]
        random.shuffle(rest)
        keywords = pool + rest
        now = int(time.time())
        begin = now - int(cfg.window_hours[1]) * 3600
        end = now - int(cfg.window_hours[0]) * 3600
        found: Dict[str, Dict[str, Any]] = {}
        for keyword in keywords[: max(1, cfg.keywords_per_run)]:
            order = random.choice(list(cfg.orders) or ["totalrank"])
            page = random.randint(1, max(1, cfg.page_max))
            try:
                items = await self._client.search_videos(
                    keyword=keyword,
                    order=order,
                    page=page,
                    window_begin=begin,
                    window_end=end,
                    play_min=cfg.play_min,
                    play_max=cfg.play_max,
                    video_review_min=cfg.min_video_review,
                    video_review_max=int(getattr(cfg, "max_video_review", 0) or 0),
                )
            except BiliError as exc:
                self._logger.warning(f"搜索「{keyword}」失败: {exc}")
                continue
            for item in items:
                bvid = str(item.get("bvid") or "")
                if bvid and bvid not in found:
                    found[bvid] = item
            self._logger.debug(f"搜索「{keyword}」order={order} page={page} 命中 {len(items)} 条")
        return list(found.values())

    async def _pick(self, cfg: Any, extra_keywords: Sequence[str] = ()) -> Optional[Candidate]:
        """从候选池里挑出第一条通过判据的。"""
        raw = await self._collect_candidates(cfg, extra_keywords)
        fresh: List[Dict[str, Any]] = []
        for item in raw:
            bvid = str(item.get("bvid") or "")
            if not bvid or self._store.is_checked(bvid) or self._store.is_sent(bvid):
                continue
            # 先做零成本的预筛（时长/屏蔽词），再排序——否则排序会把长合集排到前面，
            # 白白占掉检查名额又被时长上限否掉
            reason = self._judge.prefilter(item)
            if reason:
                self._store.mark_checked(bvid)
                self._logger.info(f"跳过 {bvid}：{reason}")
                continue
            fresh.append(item)
        self._logger.info(f"本轮候选 {len(raw)} 条，可检查 {len(fresh)} 条")
        # 随机排序，不要按评论数降序：实测评论数越多艾特率越低
        # （今天的分档采样：40~150 条评论中位 33%、600+ 只 10%），
        # 按降序挑等于每轮专挑最差的一批先看。
        random.shuffle(fresh)
        checked = 0
        for item in fresh:
            if checked >= cfg.max_check_per_run:
                self._logger.info(f"本轮检查数达上限 {cfg.max_check_per_run}，留到下一轮")
                break
            bvid = str(item.get("bvid") or "")
            checked += 1
            candidate = await self._judge.evaluate(item)
            self._store.mark_checked(bvid)
            if candidate.reject_reason:
                self._logger.info(f"跳过 {bvid}（{candidate.title[:20]}）：{candidate.reject_reason}")
                continue
            self._logger.info(
                f"命中 {bvid} {candidate.title[:28]} 艾特率={candidate.at_ratio:.0%}"
                f"（{candidate.at_count}/{candidate.comment_count}）播放={candidate.play}"
            )
            return candidate
        return None

    # ===== 下载 =====

    async def _download(self, candidate: Candidate, cfg: Any) -> Optional[Path]:
        """按清晰度优先级选一档下载，并保证体积在上限内。

        两个要点（都是实测踩出来的）：
          1. playurl 会直接给出分段体积 `size`，所以**下载前**就能判断会不会超限，
             不用先下完再放弃；
          2. 体积超限时**自动往下降一档**（720P→480P→360P），而不是直接放弃这一条——
             长一点的视频用低一档照样能发，比丢掉候选划算。
        """
        max_bytes = int(cfg.max_size_mb) * 1024 * 1024
        chosen: Optional[Tuple[Dict[str, Any], int]] = None
        tried: List[str] = []
        for qn in list(cfg.quality_preference):
            try:
                info = await self._client.playurl(candidate.bvid, candidate.cid, [qn])
            except BiliError as exc:
                self._logger.warning(f"{candidate.bvid} 取播放地址（qn={qn}）失败: {exc}")
                continue
            segments = info.get("segments") or []
            if not segments:
                tried.append(f"qn={qn} 无分段")
                continue
            if len(segments) > 1:
                # 多分段要拼接，本机没有可用的 ffmpeg CLI，直接放弃这一条
                self._logger.info(f"{candidate.bvid} 是多分段视频（{len(segments)} 段），跳过")
                return None
            size = int(segments[0].get("size") or 0)
            quality = int(info.get("quality") or 0)
            if size and size > max_bytes:
                tried.append(f"qn={quality} 约 {size / 1024 / 1024:.1f}MB 超限")
                continue
            chosen = (segments[0], quality)
            if qn != list(cfg.quality_preference)[0]:
                self._logger.info(
                    f"{candidate.bvid} 高清晰度体积超限，降到 qn={quality}"
                    f"（约 {size / 1024 / 1024:.1f}MB）"
                )
            break
        if chosen is None:
            reason = "；".join(tried) or "没有可用清晰度"
            self._logger.info(f"{candidate.bvid} 放弃下载：{reason}（上限 {cfg.max_size_mb}MB）")
            return None

        segment, quality = chosen
        self._work_dir.mkdir(parents=True, exist_ok=True)
        # 文件名只保留字母数字：bvid 来自上游接口，不拿它直接拼路径
        safe_bvid = "".join(ch for ch in candidate.bvid if ch.isalnum()) or "video"
        safe_quality = "".join(ch for ch in str(quality) if ch.isdigit()) or "q"
        dest = self._work_dir / f"{safe_bvid}_{safe_quality}.mp4"
        try:
            written = await self._client.download(str(segment.get("url") or ""), dest, max_bytes)
        except BiliError as exc:
            self._logger.warning(f"{candidate.bvid} 下载失败: {exc}")
            dest.unlink(missing_ok=True)
            return None
        self._logger.info(
            f"{candidate.bvid} 下载完成 {written / 1024 / 1024:.1f}MB（qn={quality}）→ {dest.name}"
        )
        return dest

    # ===== 主流程 =====

    async def run_once(
        self,
        cfg: Any,
        override_stream: str = "",
        extra_keywords: Sequence[str] = (),
        channel: str = "shi",
    ) -> Dict[str, Any]:
        """跑一轮：挑一条、下下来、发出去。返回本轮报告。

        override_stream 非空时只发到该会话（命令与自然语言触发用），
        否则发到配置里的所有群。extra_keywords 是点播的口味词。
        channel 决定用哪套判据：shi = 评论区艾特率、pretty = 收藏率+短时长+只二次元。
        """
        report: Dict[str, Any] = {"sent": False, "reason": "", "candidate": None, "targets": [], "attempts": 0}
        # 一轮没找到就继续找：每次换关键词/翻页重新搜，试到 rounds 轮为止
        rounds = max(1, int(getattr(cfg, "retry_rounds", 1) or 1))
        gap = max(0, int(getattr(cfg, "retry_gap_seconds", 0) or 0))
        candidate: Optional[Candidate] = None
        for attempt in range(1, rounds + 1):
            report["attempts"] = attempt
            if channel == "pretty":
                if self._pretty is None:
                    report["reason"] = "好看频道未初始化"
                    return report
                candidate = await self._pretty.pick(cfg, extra_keywords)
                if candidate is not None:
                    # 好看频道没有"花请求的检查"这一步，挑出来就标记，避免下载失败时每轮都挑同一条
                    self._store.mark_checked(candidate.bvid)
            else:
                candidate = await self._pick(cfg, extra_keywords)
            if candidate is not None:
                break
            if attempt < rounds:
                self._logger.info(f"第 {attempt}/{rounds} 轮没找到符合条件的，继续找…")
                if gap:
                    await asyncio.sleep(gap)
        if candidate is None:
            report["reason"] = f"找了 {report['attempts']} 轮都没找到符合条件的候选"
            return report

        video_path = await self._download(candidate, cfg)
        if video_path is None:
            report["reason"] = "候选下载失败"
            report["candidate"] = self._candidate_brief(candidate)
            return report

        if override_stream:
            targets: List[Tuple[str, str]] = [(override_stream, "override")]
        else:
            targets = [(group_id, group_id) for group_id in cfg.groups]

        for stream_id, label in targets:
            target_label = label
            if not override_stream:
                resolved = await self._resolve_stream(stream_id)
                if resolved is None:
                    self._logger.warning(
                        f"目标 {label} 解析不到聊天流，跳过（群号写错了？该群/该人还没产生过消息？）"
                    )
                    report["targets"].append({"group": label, "sent": False, "reason": "no_stream"})
                    continue
                stream_id, kind = resolved
                target_label = f"{label}（{kind}）"
            result = await self._deliverer.send(stream_id, candidate, video_path, cfg)
            if result.get('unknown'):
                report['unknown'] = True
            # 只看 sent：投递失败时 method 仍是 "forward"，用它判断会把失败误报成成功
            sent = bool(result.get("sent"))
            report["targets"].append({"group": target_label, "sent": sent, "method": result.get("method")})
            if sent:
                await append_context(self._ctx, stream_id, candidate, self._logger)
            else:
                self._logger.error(f"{target_label} 投递失败: {result}")

        if any(item.get("sent") for item in report["targets"]):
            report["sent"] = True
            self._store.record_sent(
                {
                    "bvid": candidate.bvid,
                    "title": candidate.title,
                    "author": candidate.author,
                    "play": candidate.play,
                    "at_ratio": round(candidate.at_ratio, 3),
                    "comment_count": candidate.comment_count,
                    # 好看频道不看艾特率，记录里存收藏率与口径，状态命令按频道显示对应指标
                    "channel": candidate.channel,
                    "favorite_ratio": round(candidate.favorite_ratio, 4),
                    "favorite_metric": candidate.favorite_metric,
                    "sent_at": int(time.time()),
                    "groups": [item["group"] for item in report["targets"] if item.get("sent")],
                }
            )
            self._cleanup_old_videos(cfg.keep_files)
        else:
            report["reason"] = "投递失败"
        report["candidate"] = self._candidate_brief(candidate)
        return report

    async def resolve_target(self, target: str) -> Optional[Tuple[str, str]]:
        """公开的解析入口（装载自检也会用）。"""
        return await self._resolve_stream(target)

    async def _resolve_stream(self, target: str) -> Optional[Tuple[str, str]]:
        """把配置里的目标解析成真实聊天流。

        先当群号找，找不到再当 QQ 号找私聊——配置里填一个 QQ 号是很自然的事
        （实测用户就把主人的 QQ 填进了 groups 以为是发给他），
        只支持群号会导致定时任务静默空转。

        **优先用入站消息学到的活动聊天流**：宿主的 chat.get_stream_by_group_id 是
        next(...) 取第一个匹配，同一群有多条历史会话时会返回过期的那条，
        发送路由会落到 legacy 旧链并失败（实测：定时发送全挂、手动发送正常）。
        """
        if str(target).startswith('qq:'):
            from utils.platform_target import resolve_configured_stream, parse_target
            stream = await resolve_configured_stream(self._ctx.chat, target)
            if not isinstance(stream, dict):
                return None
            sid = stream.get('session_id') or stream.get('stream_id')
            return (str(sid), '群' if parse_target(target)['kind']=='group' else '私聊') if sid else None
        for prefix in ("group", "user"):
            live = self._store.live_session(f"{prefix}:{target}")
            if live:
                kind = "群" if prefix == "group" else "私聊"
                self._logger.info(f"目标 {target} 使用最近活动聊天流（{kind}）")
                return live, kind

        # 缓存里没有（比如刚重载完还没收到该群消息）：枚举同目标的所有会话，
        # 用"最近有没有消息"挑出真正在用的那条
        probed = await self._probe_by_recent_messages(target)
        if probed:
            return probed

        lookups = (
            ("群", self._ctx.chat.get_stream_by_group_id),
            ("私聊", self._ctx.chat.get_stream_by_user_id),
        )
        for kind, getter in lookups:
            try:
                stream = await getter(str(target), platform="wechat")
            except Exception as exc:  # noqa: BLE001
                self._logger.warning(f"解析目标 {target}（按{kind}）异常: {exc!r}")
                continue
            if isinstance(stream, dict):
                session_id = str(stream.get("session_id") or stream.get("stream_id") or "")
                if session_id:
                    return session_id, kind
        return None

    async def _probe_by_recent_messages(self, target: str) -> Optional[Tuple[str, str]]:
        """在同目标的候选会话里，挑出最近有消息的那条。

        宿主的 chat.get_stream_by_group_id 用 next(...) 取第一个匹配，同一群有多条历史会话时
        会返回过期的那条（发送路由落到 legacy 旧链、必然失败）。这里用"有没有最近消息"来区分。
        """
        try:
            payload = await self._ctx.chat.get_all_streams(platform="wechat")
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(f"列举聊天流失败（回退到宿主查询）: {exc!r}")
            return None
        streams = payload.get("streams") if isinstance(payload, dict) else payload
        if not isinstance(streams, list):
            return None
        candidates: List[Tuple[str, str]] = []
        for item in streams:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("session_id") or "")
            if not sid:
                continue
            if str(item.get("group_id") or "") == target:
                candidates.append((sid, "群"))
            elif not item.get("group_id") and str(item.get("user_id") or "") == target:
                candidates.append((sid, "私聊"))
        if len(candidates) <= 1:
            return None  # 只有一条就交给宿主查询，不必多花请求
        best: Optional[Tuple[str, str]] = None
        best_stamp = ""
        for sid, kind in candidates:
            try:
                recent = await self._ctx.message.get_recent(chat_id=sid, limit=1)
            except Exception:  # noqa: BLE001
                continue
            items = recent.get("messages") if isinstance(recent, dict) else recent
            if not isinstance(items, list) or not items:
                continue
            first = items[0] if isinstance(items[0], dict) else {}
            stamp = str(first.get("timestamp") or first.get("time") or "")
            if best is None or stamp > best_stamp:
                best, best_stamp = (sid, kind), stamp
        if best:
            self._logger.info(f"目标 {target} 按最近消息定位到聊天流（{best[1]}）")
            if self._store is not None:
                self._store.remember_live_session(f"{'group' if best[1] == '群' else 'user'}:{target}", best[0])
        return best

    def _cleanup_old_videos(self, keep: int) -> None:
        """只保留最近几个视频文件，其余删掉，避免插件数据目录无限膨胀。"""
        files = sorted(self._work_dir.glob("*.mp4"), key=lambda path: path.stat().st_mtime, reverse=True)
        for path in files[max(1, keep):]:
            path.unlink(missing_ok=True)

    @staticmethod
    def _candidate_brief(candidate: Candidate) -> Dict[str, Any]:
        data = asdict(candidate)
        data.pop("tags", None)
        return data
