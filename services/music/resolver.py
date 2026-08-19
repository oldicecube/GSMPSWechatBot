from collections.abc import Mapping
import logging
from typing import Iterable
from urllib.parse import urlparse

from .models import ResolvedAudio, SongInfo
from .source import MusicSourceError

DEFAULT_QUALITY = "128k"
VALID_QUALITIES = frozenset(("128k", "320k", "flac", "flac24bit"))
DEFAULT_RESOLVE_TIMEOUT_SECONDS = 20.0
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 60.0

_logger = logging.getLogger(__name__)


class MusicConfigError(ValueError):
    """Invalid music configuration."""


class MusicResolutionError(RuntimeError):
    """Raised when all configured music sources fail."""

    def __init__(self, attempted_sources, errors):
        self.attempted_sources = list(attempted_sources)
        self.errors = list(errors)
        message = "；".join(
            f"{source_id}: {reason}" for source_id, reason in self.errors
        )
        super().__init__(message or "没有可用的音乐源")


class MusicResolver:
    def __init__(
        self,
        source_order: Iterable[str] = (),
        quality: str = DEFAULT_QUALITY,
        resolve_timeout_seconds: float = DEFAULT_RESOLVE_TIMEOUT_SECONDS,
        download_timeout_seconds: float = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        allow_netease_fallback: bool = True,
        sources=None,
    ):
        self.source_order = tuple(source_order)
        self.quality = str(quality)
        self.resolve_timeout_seconds = float(resolve_timeout_seconds)
        self.download_timeout_seconds = float(download_timeout_seconds)
        self.allow_netease_fallback = bool(allow_netease_fallback)
        self.sources = dict(sources or {})
        self._validate()

    @classmethod
    def from_config(cls, config=None, sources=None, source_order=None):
        config = config if isinstance(config, Mapping) else {}
        music_config = config.get("music", {})
        if music_config is None:
            music_config = {}
        if not isinstance(music_config, Mapping):
            raise MusicConfigError("music 必须是对象")

        built_order = None
        if sources is None:
            from .sources import build_sources

            sources, built_order = build_sources(music_config)

        quality = music_config.get("quality", DEFAULT_QUALITY)
        resolve_timeout = music_config.get(
            "resolve_timeout_seconds",
            DEFAULT_RESOLVE_TIMEOUT_SECONDS,
        )
        download_timeout = music_config.get(
            "download_timeout_seconds",
            DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
        )
        allow_fallback = music_config.get("allow_netease_fallback", True)
        resolved_order = (
            source_order
            or music_config.get("source_order")
            or built_order
            or tuple(sources.keys())
        )

        return cls(
            source_order=resolved_order,
            quality=quality,
            resolve_timeout_seconds=resolve_timeout,
            download_timeout_seconds=download_timeout,
            allow_netease_fallback=allow_fallback,
            sources=sources,
        )

    def resolve(self, song: SongInfo) -> ResolvedAudio:
        return next(self.iter_candidates(song))

    def iter_candidates(self, song: SongInfo):
        """Yield usable sources lazily in configured priority order."""
        attempted_sources = []
        errors = []
        yielded = False

        for source_id in self.source_order:
            if source_id == "netease" and not self.allow_netease_fallback:
                continue
            if source_id not in self.sources:
                continue

            attempted_sources.append(source_id)
            try:
                result = self._resolve_source(source_id, song)
                yielded = True
                yield result
            except MusicSourceError as exc:
                _logger.warning(
                    "[MUSIC RESOLVE] failed source=%s reason=%s",
                    source_id,
                    exc.reason,
                )
                errors.append((source_id, exc.reason))
            except Exception as exc:
                reason = str(exc) or exc.__class__.__name__
                _logger.warning(
                    "[MUSIC RESOLVE] failed source=%s reason=%s",
                    source_id,
                    reason,
                )
                errors.append((source_id, reason))

        if not yielded:
            raise MusicResolutionError(attempted_sources, errors)

    def resolve_candidates(self, song: SongInfo):
        """Resolve every usable source so download failures can also fall back."""
        return list(self.iter_candidates(song))

    def _resolve_source(self, source_id, song):
        source = self.sources.get(source_id)
        if source is None:
            raise MusicSourceError(source_id, "音源适配器未注册")
        result = source.resolve(
            song,
            self.quality,
            timeout=self.resolve_timeout_seconds,
        )
        self._validate_result(source_id, result)
        return result

    def _validate(self):
        if not self.source_order:
            raise MusicConfigError("music.source_order 不能为空")

        if len(set(self.source_order)) != len(self.source_order):
            raise MusicConfigError("music.source_order 不能包含重复音源")

        unknown = [
            source_id
            for source_id in self.source_order
            if source_id not in self.sources
        ]
        if unknown:
            raise MusicConfigError(f"未知音乐源：{', '.join(unknown)}")

        if self.quality not in VALID_QUALITIES:
            raise MusicConfigError(f"不支持的音乐质量：{self.quality}")

        if self.resolve_timeout_seconds <= 0:
            raise MusicConfigError("music.resolve_timeout_seconds 必须大于 0")
        if self.download_timeout_seconds <= 0:
            raise MusicConfigError("music.download_timeout_seconds 必须大于 0")

    @staticmethod
    def _validate_result(source_id, result):
        if not isinstance(result, ResolvedAudio):
            raise MusicSourceError(source_id, "适配器返回值不是 ResolvedAudio")
        parsed = urlparse(result.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MusicSourceError(source_id, "适配器返回了无效 URL")
