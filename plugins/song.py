# -*- coding: utf-8 -*-
"""鐐规瓕鎻掍欢锛氫粠缃戞槗浜戞悳绱㈡瓕鏇诧紝鎸夐煶婧愪紭鍏堢骇瑙ｆ瀽骞跺彂閫佽闊炽€?

鏀寔涓ょ鐢ㄦ硶锛?
    /song <姝屾洸鍚?
    /song <姝屾洸鍚?
    <璧峰绉? <缁堟绉?

绗簩琛屽弬鏁帮紙璧峰绉?缁堟绉掞級涓嶅～鏃讹紝榛樿鍙栧墠 59.5 绉掞紱绉掓暟鏀寔鍙岀簿搴︽诞鐐规暟锛?
闈炴硶绉掓暟鎸?0 澶勭悊銆傜粓姝㈢瓒呭嚭姝屾洸鏃堕暱鏃舵埅姝㈠埌姝屾洸鏃堕暱锛涜捣姝㈠樊瓒呰繃 59.5 绉掓椂
缁堟绉掗粯璁ゅ彇 璧峰绉?59.5銆?

璇煶鏂囦欢缁熶竴涓嬭浇鍒?core.wechat_sender.file_down.VOICE_TEMP_DIR
锛?TEMP%\\WechatRobot\\voice锛夛紝鐢卞彂閫佺粍浠讹紙wechat_hook_server锛夊湪鍙戦€佸畬鎴愬悗鍒犻櫎銆?
"""

# GSMPS WebUI metadata only; execution remains in this shell module.
WEBUI_CONFIG = {'version': 1, 'fields': {}}


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

# 寰俊璇煶鏈€闀跨害 60 绉掞紱姝屾洸璇煶榛樿鍙彇鍓?59.5 绉掞紝缁欏綍闊崇暀灏戦噺浣欓噺
DEFAULT_SPAN_SECONDS = 59.5

_music_resolver = None
_netease_source = None
_music_init_error = None
_music_init_lock = threading.RLock()
_logger = logging.getLogger(__name__)


def init(config):
    """Initialize the configured music resolver."""
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
    """Return the target group or user for the send operation."""
    if not context:
        return "鏂囦欢浼犺緭鍔╂墜"
    return context.get("group") or context.get("user") or "鏂囦欢浼犺緭鍔╂墜"


def _parse_seconds(token, default):
    """Parse a non-negative floating-point seconds value."""
    try:
        value = float(str(token).strip())
    except (TypeError, ValueError):
        return default
    if value < 0:
        return 0.0 if default is not None else None
    return value


def _parse_song_args(content):
    """Parse ``/song`` arguments and detect the raw-file mode.

    The first line is the search phrase. Any standalone ``mp3`` token on later
    lines selects raw-file delivery and makes all time arguments irrelevant.
    """
    lines = str(content or "").splitlines()
    keyword = lines[0].strip() if lines else ""
    param_text = "\n".join(lines[1:]).strip()
    tokens = param_text.split()
    mp3_mode = any(token.casefold() == "mp3" for token in tokens)
    if mp3_mode:
        return keyword, 0.0, DEFAULT_SPAN_SECONDS, True

    start = 0.0
    end = DEFAULT_SPAN_SECONDS
    if tokens:
        start = _parse_seconds(tokens[0], 0.0)
        end = start + DEFAULT_SPAN_SECONDS
    if len(tokens) >= 2:
        parsed_end = _parse_seconds(tokens[1], None)
        if parsed_end is not None:
            end = parsed_end
    return keyword, start, end, False

def _apply_caps(start, end, song_duration=None):
    """Apply the 59.5-second voice duration and song-length limits."""
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
    """Search the configured music source and return the first result."""
    resolver, netease_source = _get_music_components()
    return netease_source.search(
        keyword,
        timeout=resolver.resolve_timeout_seconds,
    )


def handle(content, context):
    """Search for a song and send either a voice clip or the original file."""
    if content is None:
        return None

    keyword, start, end, mp3_mode = _parse_song_args(content)
    if not keyword:
        return (
            "???/song <???>\n"
            "? /song <???>\n<???> <???>\n"
            "???? /song <???>\nmp3 ?????????"
        )

    try:
        resolver, _ = _get_music_components()
    except MusicConfigError as exc:
        return f"????????{exc}"
    except Exception as exc:
        return f"?????????{exc}"

    try:
        song = _search_song(keyword)
    except MusicSourceError as exc:
        return f"???????{exc.reason}"
    except Exception as exc:
        return f"???????{exc}"

    if not song:
        return f"?????{keyword}??????"

    song_duration = song.duration or 0.0
    if not mp3_mode:
        start, end = _apply_caps(start, end, song_duration or None)
        if song_duration > 0 and start >= song_duration:
            return f"??????????{start:.2f}s"

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
                local_path = download_voice_file(
                    candidate.url,
                    prefix="song_%s" % song.song_id,
                    headers=candidate.download_headers,
                    timeout=resolver.download_timeout_seconds,
                    source_id=candidate.source_id,
                )
                break
            except Exception as exc:
                download_errors.append(f"{candidate.source_name}: {exc}")
                _logger.warning("[MUSIC DOWNLOAD ERROR] %s: %s", candidate.source_id, exc)
    except MusicResolutionError as exc:
        resolution_error = exc
        resolution_errors = [f"{source_id}: {reason}" for source_id, reason in exc.errors]
        for source_id, reason in exc.errors:
            _logger.warning("[MUSIC RESOLVE] failed source=%s reason=%s", source_id, reason)

    if not local_path:
        if not download_errors and resolution_error is not None:
            return f"???????{resolution_error}"
        if resolution_errors and not download_errors:
            return "???????" + "; ".join(resolution_errors)
        if resolution_error is not None:
            download_errors.append(f"?????{resolution_error}")
        return "???????" + ("; ".join(download_errors) or "??????")

    target = _get_target(context)
    if mp3_mode:
        ok, err = send(target=target, file_path=local_path, mode="wechat_file")
        if not ok:
            return f"???????????{err}"
        return None

    ok, err = send(
        target=target,
        file_path=local_path,
        mode="wechat_voice",
        duration=end - start,
        voice_start=start,
    )
    if not ok:
        return f"???????{err}"
    return None
