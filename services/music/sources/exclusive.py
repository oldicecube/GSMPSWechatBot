from .common import (
    encode_path_segment,
    normalize_base_url,
    resolve_json_url,
)
from ..source import MusicSourceError


class ExclusiveSource:
    source_id = "exclusive"
    name = "独家音源"
    default_base_url = "https://88.lxmusic.xn--fiqs8s/lxmusicv4"

    def __init__(self, base_url=None, api_key="", request_get=None):
        self.base_url = normalize_base_url(base_url, self.default_base_url)
        self.api_key = str(api_key or "").strip()
        self.request_get = request_get

    def resolve(self, song, quality, timeout):
        if not self.api_key:
            raise MusicSourceError(self.source_id, "未配置 api_key")

        info = song.to_music_info()
        identifier = info.get("hash") or info.get("songmid")
        if identifier in (None, ""):
            raise MusicSourceError(self.source_id, "歌曲信息缺少 hash 或 songmid")

        platform = str(song.source or "wy")
        url = (
            f"{self.base_url}/url/{encode_path_segment(platform)}/"
            f"{encode_path_segment(identifier)}/{encode_path_segment(quality)}"
        )
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "lx-music/desktop",
            "X-Request-Key": self.api_key,
            "follow_max": "5",
        }
        return resolve_json_url(
            source_id=self.source_id,
            source_name=self.name,
            url=url,
            headers=headers,
            timeout=timeout,
            request_get=self.request_get,
        )
