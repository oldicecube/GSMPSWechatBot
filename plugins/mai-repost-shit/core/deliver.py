"""按真实会话来源投递：QQ 合并转发，微信说明文字与 MP4 文件。

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

from pathlib import Path
from typing import Any, Dict, List

from .judge import Candidate


def _receipt(result: Any, method: str) -> Dict[str, Any]:
    """Keep an unresolved platform/RPC receipt distinct from a rejected send."""
    sent = isinstance(result, dict) and result.get('sent') is True
    report = {'sent': sent, 'method': method, 'detail': result}
    if not sent and isinstance(result, dict) and (
        result.get('unknown') is True or result.get('queued') is True
        or any(marker in str(result.get('error') or '').lower()
               for marker in ('e_timeout', 'timed out', '超时'))
    ):
        report['unknown'] = True
    return report


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
        from utils.plugin_stream import stream_for
        stream = await stream_for(self._ctx, stream_id)
        if not stream:
            return {'sent': False, 'reason': 'unknown destination platform/account'}
        if stream['platform'] == 'qq':
            return await self._send_upstream_forward(stream_id, candidate, video_path, cfg, stream)
        # wxSend cannot send QQ merged-forward or VideoComponent messages.
        # One SDK call preserves native text/file fields and prevalidation.
        # Local paths also avoid base64 overflowing the Runner RPC frame.
        segments = []
        if cfg.include_header_text:
            segments.append({'type':'text','data':candidate.header_text()})
        segments.append({'type':'file','data':{'name':video_path.name,'mime_type':'video/mp4',
                                             'url':str(video_path.resolve())}})
        try:
            result = await self._ctx.send.hybrid(segments,stream_id,return_details=True,timeout_ms=180000)
        except Exception as exc:
            self._logger.error(f'微信视频回执未确认: {type(exc).__name__}: {exc}')
            return {'sent':False,'unknown':True,'method':'wechat_file','error':type(exc).__name__}
        return _receipt(result, 'wechat_file')

    async def _send_upstream_forward(self, stream_id: str, candidate: Candidate, video_path: Path, cfg: Any, stream: dict) -> Dict[str, Any]:
        """QQ native merged-forward with the destination adapter's own account."""
        bot_qq = str(stream['account_id'])
        bot_name = str(await self._ctx.config.get("bot.nickname", "") or "麦麦")
        try:
            size_bytes = video_path.stat().st_size
        except OSError:
            size_bytes = 0
        size_mb = size_bytes / 1024 / 1024
        # Local NapCat shares this host; paths keep media out of the 16 MB RPC frame.
        messages = build_forward_messages(
            candidate, video_path.resolve(), bot_qq, bot_name, cfg.include_header_text
        )
        try:
            result = await self._ctx.send.forward(messages, stream_id, return_details=True, timeout_ms=180000)
        except Exception as exc:  # noqa: BLE001
            self._logger.error(f"投递异常（合并转发调用失败，视频 {size_mb:.1f}MB）: {exc!r}")
            return {"sent": False, "unknown": True, "method": "forward", "errors": [repr(exc)]}

        report = _receipt(result, 'forward')
        if report['sent'] or report.get('unknown'):
            return report

        # 失败不往群里说话（群里只会看到搬出来的视频），但日志要写清楚便于排查：
        # 宿主的 result 来自平台批量回执，实测体积过大时 NapCat 会判 FAILED 且 error 为空。
        self._logger.error(
            f"投递失败（合并转发未成功，视频 {size_mb:.1f}MB）：{result}。"
            "请核对实际适配器路由和发送日志；此回执不能单独证明是文件大小问题。"
        )
        return report


async def append_context(ctx: Any, stream_id: str, candidate: Candidate, logger: Any) -> None:
    """把搬运记录写进聊天流上下文，让 bot 之后能接上这个话题。"""
    text = (
        f"[我刚刚在群里搬了一条B站视频] {candidate.title}"
        f"（UP：{candidate.author}，播放 {candidate.play:,}；"
        + ('Story标签推荐' if candidate.channel == 'story' else f'评论区艾特率 {candidate.at_ratio:.0%}') + '）'
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
