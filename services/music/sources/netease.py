from collections.abc import Mapping
from urllib.parse import quote, urlparse

import requests

from ..models import ResolvedAudio, SongInfo
from ..source import MusicSourceError


DEFAULT_SEARCH_URL = "https://music.163.com/api/search/get/web"
DEFAULT_STREAM_URL = "https://music.163.com/song/media/outer/url"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
}


class NeteaseSource:
    source_id = "netease"
    name = "网易云音乐"

    def __init__(
        self,
        search_url=DEFAULT_SEARCH_URL,
        stream_url=DEFAULT_STREAM_URL,
        request_get=None,
    ):
        self.search_url = str(search_url or DEFAULT_SEARCH_URL).rstrip("/")
        self.stream_url = str(stream_url or DEFAULT_STREAM_URL).rstrip("/")
        self.request_get = request_get

    def search(self, keyword, timeout):
        request_get = self.request_get or requests.get
        params = {
            "csrf_token": "",
            "s": keyword,
            "type": 1,
            "offset": 0,
            "total": True,
            "limit": 1,
        }
        try:
            response = request_get(
                self.search_url,
                params=params,
                headers=dict(DEFAULT_HEADERS),
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise MusicSourceError(self.source_id, f"搜索请求失败：{exc}") from exc

        if not isinstance(payload, Mapping):
            raise MusicSourceError(self.source_id, "搜索接口返回不是 JSON 对象")
        songs = ((payload.get("result") or {}).get("songs") or [])
        if not songs:
            return None

        song = songs[0] or {}
        artists = song.get("artists") or []
        album = song.get("album") or {}
        duration_ms = song.get("duration")
        try:
            duration = float(duration_ms) / 1000.0 if duration_ms else 0.0
        except (TypeError, ValueError):
            duration = 0.0

        song_id = song.get("id")
        if song_id in (None, ""):
            raise MusicSourceError(self.source_id, "搜索结果缺少歌曲 ID")
        return SongInfo(
            song_id=str(song_id),
            name=str(song.get("name") or "未知歌曲"),
            artist=str(
                (artists[0] or {}).get("name")
                if artists
                else "未知歌手"
            ),
            album=str(album.get("name") or ""),
            duration=duration,
            source="wy",
        )

    def resolve(self, song, quality, timeout):
        del quality
        request_get = self.request_get or requests.get
        url = f"{self.stream_url}?id={quote(str(song.song_id))}.mp3"
        try:
            response = request_get(
                url,
                headers=dict(DEFAULT_HEADERS),
                allow_redirects=False,
                timeout=timeout,
            )
            response.raise_for_status()
        except Exception as exc:
            raise MusicSourceError(self.source_id, f"音源请求失败：{exc}") from exc

        location = (getattr(response, "headers", {}) or {}).get("Location")
        if not location:
            raise MusicSourceError(self.source_id, "未获取到音源地址")
        parsed = urlparse(str(location))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MusicSourceError(self.source_id, "网易云返回了无效音源地址")
        if parsed.path.rstrip("/").endswith("/404"):
            raise MusicSourceError(self.source_id, "该歌曲在网易云无可用音源")
        return ResolvedAudio(
            url=str(location),
            source_id=self.source_id,
            source_name=self.name,
            download_headers=dict(DEFAULT_HEADERS),
        )
