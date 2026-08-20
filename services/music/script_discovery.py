import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SCRIPT_DIR = PROJECT_ROOT / "music-source"
SOURCE_ORDER_FILE = "source_order.txt"

# Only used when resolving legacy ids inside config.json `music.source_order`.
LEGACY_SOURCE_ALIASES = {
    "flower": "野花音源.js",
    "exclusive": "[独家音源] v4.0.js",
    "grass": "野草音源.js",
}

DEFAULT_PLATFORM_ORDER = ("wy", "kg", "kw", "tx", "mg")
SCRIPT_HEADER_LINE = re.compile(r"^\s?\*\s?@(\w+)\s(.+)$")


@dataclass(frozen=True)
class DiscoveredScript:
    source_id: str
    script_path: Path
    name: str
    version: str = "1"


def parse_script_header(text: str) -> dict[str, str]:
    meta = {
        "name": "",
        "description": "",
        "author": "",
        "homepage": "",
        "version": "1",
        "updateUrl": "",
    }
    match = re.match(r"^/\*[\s\S]+?\*/", text or "")
    if not match:
        return meta
    for line in match.group(0).splitlines():
        result = SCRIPT_HEADER_LINE.match(line)
        if not result:
            continue
        key = result.group(1).lower()
        if key in meta:
            meta[key] = result.group(2).strip()
    if not meta["version"]:
        meta["version"] = "1"
    return meta


def make_source_id(script_path: Path, display_name: str) -> str:
    stem = script_path.stem.strip()
    cleaned = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", stem).strip("_")
    if cleaned:
        return cleaned
    digest = hashlib.md5(str(script_path.resolve()).encode("utf-8")).hexdigest()[:8]
    fallback = re.sub(r"[^\w\u4e00-\u9fff_]+", "_", display_name).strip("_")
    if fallback:
        return fallback
    return f"script_{digest}"


def load_script_dir_order(script_dir: Path) -> list[str]:
    """Read optional music-source/source_order.txt (filename or source_id per line)."""
    order_file = script_dir / SOURCE_ORDER_FILE
    if not order_file.is_file():
        return []
    entries: list[str] = []
    for line in order_file.read_text(encoding="utf-8").splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        entries.append(value)
    return entries


def sort_discovered(
    scripts: list[DiscoveredScript],
    script_dir: Path | None = None,
) -> list[DiscoveredScript]:
    """Order scripts by source_order.txt, else alphabetically by filename."""
    directory = script_dir or DEFAULT_SCRIPT_DIR
    file_order = load_script_dir_order(directory)
    priority = {entry: index for index, entry in enumerate(file_order)}

    def sort_key(item: DiscoveredScript) -> tuple[int, str]:
        rank = priority.get(
            item.script_path.name,
            priority.get(item.source_id, len(file_order)),
        )
        return (rank, item.script_path.name.lower())

    return sorted(scripts, key=sort_key)


def discover_scripts(script_dir: Path | str | None = None) -> list[DiscoveredScript]:
    directory = Path(script_dir or DEFAULT_SCRIPT_DIR)
    if not directory.is_absolute():
        directory = (PROJECT_ROOT / directory).resolve()
    if not directory.is_dir():
        return []

    discovered: list[DiscoveredScript] = []
    used_ids: dict[str, Path] = {}
    for script_path in sorted(directory.glob("*.js")):
        try:
            text = script_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not text.lstrip().startswith("/*"):
            continue
        meta = parse_script_header(text)
        display_name = meta.get("name") or script_path.stem
        source_id = make_source_id(script_path, display_name)
        if source_id in used_ids and used_ids[source_id] != script_path:
            digest = hashlib.md5(str(script_path.resolve()).encode("utf-8")).hexdigest()[:6]
            source_id = f"{source_id}_{digest}"
        used_ids[source_id] = script_path
        discovered.append(
            DiscoveredScript(
                source_id=source_id,
                script_path=script_path.resolve(),
                name=display_name,
                version=str(meta.get("version") or "1"),
            )
        )
    return sort_discovered(discovered, directory)


def build_source_order(
    discovered: list[DiscoveredScript],
    music_config=None,
) -> list[str]:
    music_config = music_config if isinstance(music_config, dict) else {}
    script_dir = resolve_script_dir(music_config)
    by_id = {item.source_id: item for item in discovered}
    by_file = {item.script_path.name: item for item in discovered}
    configured = music_config.get("source_order")

    if not configured:
        order = [item.source_id for item in sort_discovered(discovered, script_dir)]
        if music_config.get("allow_netease_fallback", True):
            order.append("netease")
        return order

    order: list[str] = []
    seen: set[str] = set()
    for item in configured:
        if item == "netease":
            if "netease" not in seen:
                order.append("netease")
                seen.add("netease")
            continue
        if item in by_id and item not in seen:
            order.append(item)
            seen.add(item)
            continue
        alias_file = LEGACY_SOURCE_ALIASES.get(item)
        if alias_file and alias_file in by_file:
            source_id = by_file[alias_file].source_id
            if source_id not in seen:
                order.append(source_id)
                seen.add(source_id)

    for script in sort_discovered(discovered, script_dir):
        if script.source_id in seen:
            continue
        if "netease" in order:
            netease_index = order.index("netease")
            order.insert(netease_index, script.source_id)
        else:
            order.append(script.source_id)
        seen.add(script.source_id)

    if music_config.get("allow_netease_fallback", True) and "netease" not in seen:
        order.append("netease")
    return order


def resolve_script_dir(music_config=None) -> Path:
    music_config = music_config if isinstance(music_config, dict) else {}
    script_dir = Path(music_config.get("script_dir") or DEFAULT_SCRIPT_DIR)
    if not script_dir.is_absolute():
        script_dir = (PROJECT_ROOT / script_dir).resolve()
    return script_dir


def resolve_platform_order(music_config=None) -> tuple[str, ...]:
    music_config = music_config if isinstance(music_config, dict) else {}
    configured = music_config.get("platform_order")
    if not configured:
        return DEFAULT_PLATFORM_ORDER
    if not isinstance(configured, list):
        return DEFAULT_PLATFORM_ORDER
    platforms = [str(item).strip() for item in configured if str(item).strip()]
    return tuple(platforms) if platforms else DEFAULT_PLATFORM_ORDER
