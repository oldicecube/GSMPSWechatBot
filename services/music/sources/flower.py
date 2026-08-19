from .common import (
    encode_path_segment,
    normalize_base_url,
    resolve_json_url,
    source_identifier,
    tagged_request_key,
)


class FlowerSource:
    source_id = "flower"
    name = "野花"
    default_base_url = "http://97.64.37.235/flower/v1"

    def __init__(
        self,
        base_url=None,
        request_get=None,
        lx_version="2.0.0",
        source_version="1",
    ):
        self.base_url = normalize_base_url(base_url, self.default_base_url)
        self.request_get = request_get
        self.lx_version = str(lx_version)
        self.source_version = str(source_version)

    def resolve(self, song, quality, timeout):
        platform = str(song.source or "wy")
        identifier = source_identifier(song, platform)
        url = (
            f"{self.base_url}/url/{encode_path_segment(platform)}/"
            f"{encode_path_segment(identifier)}/{encode_path_segment(quality)}"
        )
        headers = {
            "User-Agent": "lx-music/desktop",
            "ver": self.lx_version,
            "source-ver": self.source_version,
            "tag": tagged_request_key(platform, identifier, quality),
        }
        return resolve_json_url(
            source_id=self.source_id,
            source_name=self.name,
            url=url,
            headers=headers,
            timeout=timeout,
            request_get=self.request_get,
        )
