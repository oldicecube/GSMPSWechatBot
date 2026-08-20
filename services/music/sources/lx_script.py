import logging
from pathlib import Path

from ..models import ResolvedAudio, SongInfo
from ..script_discovery import DEFAULT_PLATFORM_ORDER
from ..source import MusicSourceError
from .lx_daemon_client import (
    LxDaemonError,
    LxDaemonPool,
    _find_node_executable,
    register_pool,
)

_logger = logging.getLogger(__name__)


def _build_platform_attempts(
    platform_order: tuple[str, ...],
    supported_platforms: list[str],
    preferred_platform: str | None,
) -> list[str]:
    supported = [str(item) for item in supported_platforms if str(item).strip()]
    attempts: list[str] = []

    if preferred_platform:
        preferred = str(preferred_platform).strip()
        if preferred and (not supported or preferred in supported):
            attempts.append(preferred)

    for platform in platform_order:
        if platform in attempts:
            continue
        if supported and platform not in supported:
            continue
        attempts.append(platform)

    if not attempts:
        if supported:
            return supported
        return list(platform_order) or [preferred_platform or "wy"]
    return attempts


class LxScriptSource:
    """Resolve music URLs via a long-lived LX script daemon."""

    def __init__(
        self,
        source_id: str,
        script_path: Path,
        *,
        name: str | None = None,
        daemon_pool: LxDaemonPool | None = None,
        platform_order: tuple[str, ...] = DEFAULT_PLATFORM_ORDER,
    ):
        self.source_id = str(source_id)
        self.name = str(name or source_id)
        self.script_path = Path(script_path)
        self.daemon_pool = daemon_pool
        self.platform_order = tuple(platform_order) or DEFAULT_PLATFORM_ORDER
        if self.daemon_pool is None:
            raise MusicSourceError(self.source_id, "LX 守护进程池未配置")
        if not self.script_path.is_file():
            raise MusicSourceError(self.source_id, f"脚本不存在：{self.script_path}")

    @classmethod
    def from_discovered(
        cls,
        discovered,
        *,
        music_config=None,
        daemon_pool: LxDaemonPool | None = None,
    ):
        music_config = music_config if isinstance(music_config, dict) else {}
        if daemon_pool is None:
            daemon_pool = register_pool(LxDaemonPool.from_config(music_config))
        _find_node_executable(music_config.get("node_executable"))
        from ..script_discovery import resolve_platform_order

        return cls(
            discovered.source_id,
            discovered.script_path,
            name=discovered.name,
            daemon_pool=daemon_pool,
            platform_order=resolve_platform_order(music_config),
        )

    def resolve(self, song: SongInfo, quality: str, timeout: float) -> ResolvedAudio:
        preferred_platform = str(song.source or "wy")
        per_platform_timeout = max(float(timeout) / max(len(self.platform_order), 1), 3.0)

        _logger.info(
            "[LX SCRIPT] start source=%s script=%s song=%s quality=%s",
            self.source_id,
            self.script_path.name,
            song.song_id,
            quality,
        )

        try:
            daemon = self.daemon_pool.get_daemon(self.source_id, self.script_path)
            supported_platforms = daemon.get_supported_platforms()
            attempts = _build_platform_attempts(
                self.platform_order,
                supported_platforms,
                preferred_platform,
            )
        except LxDaemonError as exc:
            _logger.warning(
                "[LX SCRIPT] failed source=%s script=%s error=%s",
                self.source_id,
                self.script_path.name,
                exc.reason,
            )
            raise MusicSourceError(self.source_id, exc.reason) from exc

        errors: list[str] = []
        for platform in attempts:
            music_info = song.to_lx_music_info()
            music_info["source"] = platform
            try:
                url = daemon.music_url(
                    platform,
                    quality,
                    music_info,
                    per_platform_timeout,
                )
            except LxDaemonError as exc:
                errors.append(f"{platform}: {exc.reason}")
                _logger.warning(
                    "[LX SCRIPT] failed source=%s platform=%s reason=%s",
                    self.source_id,
                    platform,
                    exc.reason,
                )
                continue

            _logger.info(
                "[LX SCRIPT] success source=%s platform=%s url=%s",
                self.source_id,
                platform,
                url[:120],
            )
            return ResolvedAudio(
                url=url,
                source_id=self.source_id,
                source_name=self.name,
            )

        detail = "；".join(errors) or "所有平台均解析失败"
        raise MusicSourceError(self.source_id, detail)
