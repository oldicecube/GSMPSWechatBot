from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlparse


@dataclass(frozen=True)
class SongInfo:
    """Normalized song metadata used by every music source adapter."""

    song_id: str
    name: str
    artist: str
    album: str = ""
    duration: float = 0.0
    source: str = "wy"

    def to_music_info(self):
        """Return the subset of LX Music's musicInfo shape used by adapters."""
        return {
            "songmid": self.song_id,
            "name": self.name,
            "singer": self.artist,
            "albumName": self.album,
            "interval": self.duration,
            "source": self.source,
        }

    def to_lx_music_info(self):
        """Return LX Music desktop-compatible musicInfo for custom source scripts."""
        interval = None
        if self.duration and self.duration > 0:
            total = int(self.duration)
            minutes = total // 60
            seconds = total % 60
            interval = f"{minutes:02d}:{seconds:02d}"
        return {
            "id": self.song_id,
            "songmid": self.song_id,
            "name": self.name,
            "singer": self.artist,
            "source": self.source,
            "interval": interval,
            "meta": {
                "songId": self.song_id,
                "albumName": self.album,
                "picUrl": None,
                "qualitys": [],
                "_qualitys": {},
            },
        }


@dataclass(frozen=True)
class ResolvedAudio:
    """A validated remote audio URL and the headers needed to download it."""

    url: str
    source_id: str
    source_name: str
    download_headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        parsed = urlparse(str(self.url))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("音源返回的 URL 必须是 HTTP(S) 地址")
        if not str(self.source_id).strip():
            raise ValueError("音源 ID 不能为空")
        if not str(self.source_name).strip():
            raise ValueError("音源名称不能为空")
