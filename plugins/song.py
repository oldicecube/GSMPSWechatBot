# -*- coding: utf-8 -*-
"""点歌插件：从网易云搜索歌曲，按音源优先级解析并发送语音。

支持两种用法：
    /song <歌曲名>
    /song <歌曲名>
    <起始秒> <终止秒>

第二行参数（起始秒 终止秒）不填时，默认取前 59.5 秒；秒数支持双精度浮点数，
非法秒数按 0 处理。终止秒超出歌曲时长时截止到歌曲时长；起止差超过 59.5 秒时
终止秒默认取 起始秒+59.5。

语音文件统一下载到 core.wechat_sender.file_down.VOICE_TEMP_DIR
（%TEMP%\\WechatRobot\\voice），由发送组件（wechat_hook_server）在发送完成后删除。
"""

import logging
import threading

from services.music.resolver import (
    MusicConfigError,
    MusicResolutionError,
    MusicResolver,
)
from services.music.source import MusicSourceError
from services.music.sources import build_sources
from core.sender import send
from core.wechat_sender.file_down import download_voice_file

COMMAND = "/song"
ALIASES = ["/music"]

# 微信语音最长约 60 秒；歌曲语音默认只取前 59.5 秒，给录音留少量余量
DEFAULT_SPAN_SECONDS = 59.5

_music_resolver = None
_netease_source = None
_music_init_error = None
_music_init_lock = threading.RLock()
_logger = logging.getLogger(__name__)


def init(config):
    """初始化音乐源解析器；旧配置缺少 music 时使用默认链路。"""
    global _music_resolver, _netease_source, _music_init_error

    with _music_init_lock:
        try:
            music_config = (config or {}).get("music", {}) or {}
            sources, source_order = build_sources(music_config)
            _music_resolver = MusicResolver.from_config(
                config or {},
                sources=sources,
                source_order=source_order,
            )
            _netease_source = sources["netease"]
            _music_init_error = None
        except Exception as exc:
            _music_resolver = None
            _netease_source = None
            _music_init_error = exc
            raise


def _get_music_components():
    with _music_init_lock:
        if _music_init_error is not None:
            raise _music_init_error
        if _music_resolver is None or _netease_source is None:
            init({})
        return _music_resolver, _netease_source


def _get_target(context):
    """获取目标群聊，与其他插件保持一致。"""
    if not context:
        return "文件传输助手"
    return context.get("group") or context.get("user") or "文件传输助手"


def _parse_seconds(token, default):
    """把秒数字符串解析为 double 浮点数；非法输入返回 default。

    default=None 表示“该参数缺失”，用于终止秒：缺失时由调用方回退为 起始秒+59.5。
    负数视为非法：default 非 None 时按 0 处理，为 None 时按缺失处理。
    """
    try:
        value = float(str(token).strip())
    except (TypeError, ValueError):
        return default
    if value < 0:
        return 0.0 if default is not None else None
    return value


def _parse_song_args(content):
    """解析 /song 内容为 (歌曲名, 起始秒, 终止秒)。

    格式：第一行歌曲名；第二行（回车后）为 "<起始秒> <终止秒>"，均支持小数。
    规则：
      - 第二行不填 → 起始=0，终止=59.5（默认取前 59.5 秒）
      - 只填起始 → 终止=起始+59.5
      - 非法秒数 → 起始按 0、终止视为缺失
    """
    text = str(content or "")
    lines = text.split("\n")
    keyword = lines[0].strip()
    param_line = "\n".join(lines[1:]).strip()

    start = 0.0
    end = DEFAULT_SPAN_SECONDS
    tokens = param_line.split()
    if tokens:
        start = _parse_seconds(tokens[0], 0.0)
        end = start + DEFAULT_SPAN_SECONDS
    if len(tokens) >= 2:
        parsed_end = _parse_seconds(tokens[1], None)
        if parsed_end is not None:
            end = parsed_end
    return keyword, start, end


def _apply_caps(start, end, song_duration=None):
    """应用时长约束：
      - 终止秒超出歌曲时长 → 截止到歌曲时长
      - 起止差超过 59.5 秒 → 终止=起始+59.5
      - 终止<=起始（非法区间）→ 终止=起始+59.5（再受歌曲时长约束）
    """
    start = max(0.0, float(start))
    end = max(0.0, float(end))
    if song_duration and song_duration > 0:
        end = min(end, song_duration)
    if end - start > DEFAULT_SPAN_SECONDS:
        end = start + DEFAULT_SPAN_SECONDS
        if song_duration and song_duration > 0:
            end = min(end, song_duration)
    if end <= start:
        end = start + DEFAULT_SPAN_SECONDS
        if song_duration and song_duration > 0:
            end = min(end, song_duration)
    if end < start:
        end = start
    return start, end


def _search_song(keyword):
    """搜索网易云第一首结果，返回统一的 SongInfo。"""
    resolver, netease_source = _get_music_components()
    return netease_source.search(
        keyword,
        timeout=resolver.resolve_timeout_seconds,
    )


def handle(content, context):
    if content is None:
        return None

    keyword, start, end = _parse_song_args(content)
    if not keyword:
        return "用法：/song <歌曲名>，或 /song <歌曲名>\n<起始秒> <终止秒>，例如：\n/song 晴天\n30 40"

    try:
        resolver, _ = _get_music_components()
    except MusicConfigError as e:
        return f"音乐源配置错误：{e}"
    except Exception as e:
        return f"音乐源初始化失败：{e}"

    try:
        song = _search_song(keyword)
    except MusicSourceError as e:
        return f"歌曲搜索失败：{e.reason}"
    except Exception as e:
        return f"歌曲搜索失败：{e}"

    if not song:
        return f"未找到与「{keyword}」相关的歌曲"

    # 应用终止秒约束（歌曲时长 / 59.5 秒上限）
    song_duration = song.duration or 0.0
    start, end = _apply_caps(start, end, song_duration or None)
    if song_duration > 0 and start >= song_duration:
        return f"起始秒超出歌曲时长：{start:.2f}s"

    local_path = None
    download_errors = []
    resolution_errors = []
    resolution_error = None
    try:
        for candidate in resolver.iter_candidates(song):
            _logger.info(
                "[MUSIC RESOLVE] success source=%s url=%s",
                candidate.source_id,
                candidate.url[:120],
            )
            try:
                # 下载到统一语音临时目录，发送完成后由发送组件删除。
                local_path = download_voice_file(
                    candidate.url,
                    prefix="song_%s" % song.song_id,
                    headers=candidate.download_headers,
                    timeout=resolver.download_timeout_seconds,
                    source_id=candidate.source_id,
                )
                break
            except Exception as e:
                download_errors.append(f"{candidate.source_name}: {e}")
                print(
                    f"[MUSIC DOWNLOAD ERROR] {candidate.source_id}: {e}",
                    flush=True,
                )
    except MusicResolutionError as e:
        resolution_error = e
        resolution_errors = [
            f"{source_id}: {reason}" for source_id, reason in e.errors
        ]
        for source_id, reason in e.errors:
            _logger.warning(
                "[MUSIC RESOLVE] failed source=%s reason=%s",
                source_id,
                reason,
            )

    if not local_path:
        if not download_errors and resolution_error is not None:
            return f"音源解析失败：{resolution_error}"
        if resolution_errors and not download_errors:
            return f"音源解析失败：{'；'.join(resolution_errors)}"
        if resolution_error is not None:
            download_errors.append(f"解析阶段：{resolution_error}")
        return f"音源下载失败：{'；'.join(download_errors) or '没有可用音源'}"

    target = _get_target(context)
    # duration=终止-起始；voice_start=起始秒；发送组件从该秒开始注入，按住时长=duration
    ok, err = send(
        target=target,
        file_path=local_path,
        mode="wechat_voice",
        duration=end - start,
        voice_start=start,
    )
    if not ok:
        return f"语音发送失败：{err}"

    # 语音本身即为回复，无需额外文本
    return None