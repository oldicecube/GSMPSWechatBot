# -*- coding: utf-8 -*-
"""点歌插件：从音乐源（网易云音乐）搜索歌曲，并将第一首歌的音源作为语音发送。

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

import requests

from core.sender import send
from core.wechat_sender.file_down import download_voice_file

COMMAND = "/song"
ALIASES = ["/music"]

SEARCH_URL = "https://music.163.com/api/search/get/web"
STREAM_URL = "https://music.163.com/song/media/outer/url"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://music.163.com/",
}
TIMEOUT = 20

# 微信语音最长约 60 秒；歌曲语音默认只取前 59.5 秒，给录音留少量余量
DEFAULT_SPAN_SECONDS = 59.5


def init(config):
    """插件初始化（无特殊配置）。"""
    pass


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
    """搜索网易云音乐，返回第一首歌的信息 dict；未找到返回 None。"""
    params = {
        "csrf_token": "",
        "s": keyword,
        "type": 1,
        "offset": 0,
        "total": True,
        "limit": 1,
    }
    resp = requests.get(SEARCH_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    result = data.get("result") or {}
    songs = result.get("songs") or []
    if not songs:
        return None
    song = songs[0]
    artists = song.get("artists") or []
    album = song.get("album") or {}
    duration_ms = song.get("duration")
    try:
        duration_sec = float(duration_ms) / 1000.0 if duration_ms else 0.0
    except (TypeError, ValueError):
        duration_sec = 0.0
    return {
        "id": song.get("id"),
        "name": song.get("name") or "未知歌曲",
        "artist": artists[0].get("name") if artists else "未知歌手",
        "album": album.get("name") or "",
        "duration": duration_sec,
    }


def _resolve_stream_url(song_id):
    """通过 outer/url 接口获取真实 mp3 地址（302 跳转）。"""
    resp = requests.get(
        f"{STREAM_URL}?id={song_id}.mp3",
        headers=HEADERS,
        allow_redirects=False,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    location = resp.headers.get("Location")
    if not location:
        raise RuntimeError("未获取到音源地址")
    return location


def handle(content, context):
    if content is None:
        return None

    keyword, start, end = _parse_song_args(content)
    if not keyword:
        return "用法：/song <歌曲名>，或 /song <歌曲名>\n<起始秒> <终止秒>，例如：\n/song 晴天\n30 40"

    try:
        song = _search_song(keyword)
    except Exception as e:
        return f"歌曲搜索失败：{e}"

    if not song:
        return f"未找到与「{keyword}」相关的歌曲"

    # 应用终止秒约束（歌曲时长 / 59.5 秒上限）
    song_duration = song.get("duration") or 0.0
    start, end = _apply_caps(start, end, song_duration or None)
    if song_duration > 0 and start >= song_duration:
        return f"起始秒超出歌曲时长：{start:.2f}s"

    try:
        stream_url = _resolve_stream_url(song["id"])
        # 下载到统一语音临时目录，发送完成后由发送组件删除
        local_path = download_voice_file(stream_url, prefix="song_%s" % song["id"])
    except Exception as e:
        return f"音源下载失败：{e}"

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