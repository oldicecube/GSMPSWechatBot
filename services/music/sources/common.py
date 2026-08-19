import json
import re
from collections.abc import Mapping
from urllib.parse import quote, urlparse

import requests

from ..models import ResolvedAudio, SongInfo
from ..source import MusicSourceError


def normalize_base_url(base_url: str, default: str) -> str:
    value = str(base_url or "").strip() or default
    return value.rstrip("/")


def source_identifier(song: SongInfo, source: str) -> str:
    """Map LX Music's platform key to the ID expected by a source API."""
    info = song.to_music_info()
    field_by_source = {
        "kw": "songmid",
        "wy": "songmid",
        "tx": "songmid",
        "kg": "hash",
        "mg": "copyrightId",
    }
    field = field_by_source.get(source)
    if not field:
        raise MusicSourceError(source, f"不支持的平台源：{source}")
    value = info.get(field)
    if value in (None, ""):
        raise MusicSourceError(source, f"歌曲信息缺少 {field}")
    return str(value)


def tagged_request_key(source: str, identifier: str, quality: str) -> str:
    """Reproduce the tag used by the flower/grass LX scripts."""
    path = f"/url/{source}/{identifier}/{quality}"
    matches = re.findall(r"(?:\d\w)+", path)
    serialized = json.dumps(
        matches,
        ensure_ascii=False,
        indent=1,
        separators=(",", ": "),
    )
    return serialized.encode("utf-8").hex()


def resolve_json_url(
    *,
    source_id: str,
    source_name: str,
    url: str,
    headers: Mapping[str, str],
    timeout: float,
    request_get=None,
) -> ResolvedAudio:
    request_get = request_get or requests.get
    try:
        response = request_get(url, headers=dict(headers), timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except MusicSourceError:
        raise
    except Exception as exc:
        raise MusicSourceError(source_id, f"请求失败：{exc}") from exc

    if not isinstance(payload, Mapping):
        raise MusicSourceError(source_id, "接口返回不是 JSON 对象")
    if payload.get("code") != 0:
        message = str(payload.get("msg") or "接口返回失败")
        raise MusicSourceError(source_id, message)

    audio_url = payload.get("data")
    parsed = urlparse(str(audio_url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise MusicSourceError(source_id, "接口未返回有效播放地址")

    return ResolvedAudio(
        url=str(audio_url),
        source_id=source_id,
        source_name=source_name,
    )


def encode_path_segment(value: str) -> str:
    return quote(str(value), safe="")
