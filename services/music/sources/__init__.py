from collections.abc import Mapping

from ..script_discovery import (
    build_source_order,
    discover_scripts,
    resolve_script_dir,
)
from .exclusive import ExclusiveSource
from .flower import FlowerSource
from .grass import GrassSource
from .lx_daemon_client import LxDaemonPool, register_pool, shutdown_all_pools
from .lx_script import LxScriptSource
from .netease import NeteaseSource


def _source_config(config, source_id):
    source_configs = config.get("sources", {})
    if not isinstance(source_configs, Mapping):
        return {}
    value = source_configs.get(source_id, {})
    return value if isinstance(value, Mapping) else {}


def _use_python_adapter(source_id, source_config, music_config):
    if source_config.get("base_url") or source_config.get("api_key"):
        return True
    if music_config.get("mode") == "python":
        return source_id in {"flower", "exclusive", "grass"}
    return False


def build_sources(music_config=None, request_get=None, daemon_pool=None):
    config = music_config if isinstance(music_config, Mapping) else {}
    script_dir = resolve_script_dir(config)
    if config.get("mode") == "python":
        discovered = []
    else:
        discovered = discover_scripts(script_dir)
    source_order = build_source_order(discovered, config)

    shutdown_all_pools()
    if daemon_pool is None and discovered:
        daemon_pool = register_pool(LxDaemonPool.from_config(config))

    sources: dict[str, object] = {}
    for item in discovered:
        sources[item.source_id] = LxScriptSource.from_discovered(
            item,
            music_config=config,
            daemon_pool=daemon_pool,
        )

    flower_config = _source_config(config, "flower")
    exclusive_config = _source_config(config, "exclusive")
    grass_config = _source_config(config, "grass")
    netease_config = _source_config(config, "netease")

    if _use_python_adapter("flower", flower_config, config):
        sources["flower"] = FlowerSource(
            base_url=flower_config.get("base_url"),
            request_get=request_get,
            lx_version=flower_config.get("lx_version", "2.0.0"),
            source_version=flower_config.get("source_version", "1"),
        )
    if _use_python_adapter("exclusive", exclusive_config, config):
        sources["exclusive"] = ExclusiveSource(
            base_url=exclusive_config.get("base_url"),
            api_key=(
                exclusive_config.get("api_key")
                or exclusive_config.get("request_key")
                or ""
            ),
            request_get=request_get,
        )
    if _use_python_adapter("grass", grass_config, config):
        sources["grass"] = GrassSource(
            base_url=grass_config.get("base_url"),
            request_get=request_get,
            lx_version=grass_config.get("lx_version", "2.0.0"),
            source_version=grass_config.get("source_version", "1"),
        )

    sources["netease"] = NeteaseSource(
        search_url=netease_config.get("search_url"),
        stream_url=netease_config.get("stream_url"),
        request_get=request_get,
    )

    return sources, source_order


__all__ = [
    "ExclusiveSource",
    "FlowerSource",
    "GrassSource",
    "LxScriptSource",
    "NeteaseSource",
    "build_sources",
]
