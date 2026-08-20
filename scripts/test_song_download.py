#!/usr/bin/env python3
"""Quick integration test for a specific song download."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.music.models import SongInfo
from services.music.resolver import MusicResolver
from services.music.sources import build_sources


def load_config():
    path = os.path.join(ROOT, "config.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main():
    keyword = sys.argv[1] if len(sys.argv) > 1 else "ヒトリゴト"
    config = load_config()
    music_config = config.get("music", {})
    sources, source_order = build_sources(music_config)
    resolver = MusicResolver.from_config(
        config,
        sources=sources,
        source_order=source_order,
    )

    netease = sources["netease"]
    song = netease.search(keyword, timeout=resolver.resolve_timeout_seconds)
    if not song:
        print(f"未找到歌曲: {keyword}")
        return 1
    print(f"搜索到: {song.name} - {song.artist} (id={song.song_id})")

    for source_id in resolver.source_order:
        print(f"\n--- 尝试音源: {source_id} ---")
        try:
            result = resolver._resolve_source(source_id, song)
            print(f"  解析成功: {result.url[:120]}...")
        except Exception as e:
            print(f"  解析失败: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
