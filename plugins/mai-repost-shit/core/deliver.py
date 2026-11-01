"""投递：把视频以「聊天记录」（合并转发）形式发进群。

**只有这一种投递方式**：转发节点 = 一段说明文字 + 视频本体。不走「直接发视频」或
「按文件发」——那两种会在群里多出消息，也与"搬史"想要的观感不符。

**依赖宿主内部行为的说明（重要）**
宿主没有 VideoComponent，转发节点里的视频段只能以「未知类型 → DictComponent」的形态落到
适配器，再由适配器的 `_build_dict_component_segment` 认领成视频段。也就是说：

    {"type": "video", "file": "C:/x.mp4"}   ← 顶层 file 键，宿主会原样透传

这个写法不在公开 API 里，是照宿主的归一化/装配代码试出来的。宿主一旦改动
`_normalize_plugin_segments` / `_component_from_dict` 的处理，视频会变成空文本段或
`[unsupported:...]` 占位——而且**不会报错**，只会安静地发出一条没有视频的消息。

失败时**只在本地记日志，不往群里发任何提示**（群里只看得到搬出来的视频）。
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any, Dict, List

from .judge import Candidate


def build_forward_messages(
    candidate: Candidate,
    video_path: Path,
    bot_qq: str,
    bot_name: str,
    include_header: bool = True,
    video_base64: str = "",
) -> List[Dict[str, Any]]:
    """构造合并转发节点列表。

    **文字与视频必须分成两个节点**：实测经 SnowLuma 适配器发送时，对端会拒绝
    「文字 + 视频」同一个节点的转发：

        对端动作返回失败: action=send_private_forward_msg
        message=message element "video" must be the only segment in a message

    也就是说转发里的 video 段必须独占一个节点。分成两个节点后 NapCat 与 SnowLuma 都能发。
    """
    nodes: List[Dict[str, Any]] = []
    if include_header:
        nodes.append(
            {
                "user_id": bot_qq,
                "nickname": bot_name,
                "segments": [{"type": "text", "content": candidate.header_text()}],
            }
        )
    # 视频优先内联 base64：后端（如 SnowLuma）可能跑在别的文件系统/容器里，
    # 给它一个本机路径它读不到（实测报 EACCES，realpath /root/maibot/...）；
    # base64 把数据放进消息本身，与后端在哪台机器无关。
    if video_base64:
        video_segment: Dict[str, Any] = {"type": "video", "binary_data_base64": video_base64}
    else:
        video_segment = {"type": "video", "file": video_path.as_posix()}
    nodes.append({"user_id": bot_qq, "nickname": bot_name, "segments": [video_segment]})
    return nodes


class Deliverer:
    """把一条候选以合并转发的形式发出去。"""

    def __init__(self, ctx: Any, logger: Any) -> None:
        self._ctx = ctx
        self._logger = logger

    async def send(self, stream_id: str, candidate: Candidate, video_path: Path, cfg: Any) -> Dict[str, Any]:
        """发送一条候选；返回 {"sent": bool, "method": "forward", ...}。"""
        # wxSend cannot send QQ merged-forward or VideoComponent messages.
        # One SDK call preserves native text/file fields and prevalidation.
        # Local paths also avoid base64 overflowing the Runner RPC frame.
        segments = []
        if cfg.include_header_text:
            segments.append({'type':'text','data':candidate.header_text()})
        segments.append({'type':'file','data':{'name':video_path.name,'mime_type':'video/mp4',
                                             'url':str(video_path.resolve())}})
        try:
            result = await self._ctx.send.hybrid(segments,stream_id,return_details=True)
        except Exception as exc:
            self._logger.error(f'微信视频投递失败: {type(exc).__name__}')
            return {'sent':False,'method':'wechat_file','error':type(exc).__name__}
        return {'sent':bool(isinstance(result,dict) and result.get('sent')),
                'method':'wechat_file','detail':result}

    async def _send_upstream_forward(self, stream_id: str, candidate: Candidate, video_path: Path, cfg: Any) -> Dict[str, Any]:
        """Original upstream QQ delivery retained for reference; unused by this port."""
        bot_qq = str(await self._ctx.config.get("bot.qq_account", "") or "")
        bot_name = str(await self._ctx.config.get("bot.nickname", "") or "麦麦")
        try:
            size_bytes = video_path.stat().st_size
        except OSError:
            size_bytes = 0
        size_mb = size_bytes / 1024 / 1024
        # 体积在内联预算内就带 base64 发（后端无需读本地文件），超预算才退回路径引用
        inline_limit = int(getattr(cfg, "inline_video_max_mb", 0) or 0) * 1024 * 1024
        video_base64 = ""
        if inline_limit and 0 < size_bytes <= inline_limit:
            try:
                video_base64 = base64.b64encode(await asyncio.to_thread(video_path.read_bytes)).decode("ascii")
            except OSError as exc:
                self._logger.warning(f"读取视频用于内联失败，退回路径引用: {exc!r}")
        if not video_base64:
            self._logger.warning(
                f"视频 {size_mb:.1f}MB 超过内联预算（{getattr(cfg, 'inline_video_max_mb', 0)}MB），"
                "改用本地路径引用——若后端不在同一台机器/容器里会读不到而发送失败"
            )
        messages = build_forward_messages(
            candidate, video_path, bot_qq, bot_name, cfg.include_header_text, video_base64
        )
        try:
            result = await self._ctx.send.forward(messages, stream_id, return_details=True)
        except Exception as exc:  # noqa: BLE001
            self._logger.error(f"投递异常（合并转发调用失败，视频 {size_mb:.1f}MB）: {exc!r}")
            return {"sent": False, "method": "forward", "errors": [repr(exc)]}

        if isinstance(result, dict) and result.get("sent"):
            return {"sent": True, "method": "forward", "detail": result}

        # 失败不往群里说话（群里只会看到搬出来的视频），但日志要写清楚便于排查：
        # 宿主的 result 来自平台批量回执，实测体积过大时 NapCat 会判 FAILED 且 error 为空。
        self._logger.error(
            f"投递失败（合并转发未成功，视频 {size_mb:.1f}MB）：{result}。"
            "体积过大时 NapCat 会上传失败，可调小 [download].max_size_mb"
        )
        return {"sent": False, "method": "forward", "detail": result}


async def append_context(ctx: Any, stream_id: str, candidate: Candidate, logger: Any) -> None:
    """把搬运记录写进聊天流上下文，让 bot 之后能接上这个话题。"""
    text = (
        f"[我刚刚在群里搬了一条B站视频] {candidate.title}"
        f"（UP：{candidate.author}，播放 {candidate.play:,}，评论区艾特率 {candidate.at_ratio:.0%}）"
        f" https://www.bilibili.com/video/{candidate.bvid}"
    )
    try:
        result = await ctx.maisaka.context.append(
            stream_id,
            [{"type": "text", "content": text}],
            visible_text=text,
            source_kind="bilibili_share",
        )
        if not (isinstance(result, dict) and result.get("success")):
            logger.warning(f"搬运记录注入上下文失败: {result}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"搬运记录注入上下文异常: {exc!r}")
