#!/usr/bin/env python3
"""Decode LX Music custom sources from user_api.json into music-source/."""
import base64
import hashlib
import json
import os
import re
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = ROOT / "music-source"


def default_user_api_path() -> Path:
    """Resolve LX Music Desktop's user_api.json for the current OS/user."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        root = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return root / "lx-music-desktop" / "LxDatas" / "user_api.json"

    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "lx-music-desktop"
            / "LxDatas"
            / "user_api.json"
        )

    xdg = os.environ.get("XDG_CONFIG_HOME")
    config_root = Path(xdg) if xdg else Path.home() / ".config"
    return config_root / "lx-music-desktop" / "LxDatas" / "user_api.json"

# Match config.json script filenames where possible.
FILENAME_BY_NAME = {
    "野花🌷": "野花音源.js",
    "野花": "野花音源.js",
    "野草🌾": "野草音源.js",
    "野草": "野草音源.js",
    "[独家音源]": "[独家音源] v4.0.js",
}


def safe_filename(name: str, version: str = "") -> str:
    mapped = FILENAME_BY_NAME.get(name.strip())
    if mapped:
        return mapped
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", name.strip()) or "custom_source"
    if version and version not in cleaned:
        return f"{cleaned} v{version}.js"
    if not cleaned.endswith(".js"):
        return f"{cleaned}.js"
    return cleaned


def inflate_script(script: str) -> str:
    if script.startswith("gz_"):
        raw = zlib.decompress(base64.b64decode(script[3:]))
        return raw.decode("utf-8")
    return script


def main():
    user_api_path = Path(sys.argv[1]) if len(sys.argv) > 1 else default_user_api_path()
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUT_DIR
    if not user_api_path.is_file():
        print(f"user_api.json not found: {user_api_path}")
        print("Pass the path explicitly: python scripts/decode_lx_scripts.py <user_api.json> [out_dir]")
        sys.exit(1)
    out_dir.mkdir(parents=True, exist_ok=True)

    with open(user_api_path, encoding="utf-8") as f:
        data = json.load(f)

    print(f"source: {user_api_path}")
    print(f"output: {out_dir}\n")

    for api in data.get("userApis", []):
        name = str(api.get("name") or "unknown")
        version = str(api.get("version") or "").strip()
        script = api.get("script") or ""
        if not script:
            print(f"[SKIP] {name}: empty script")
            continue

        try:
            text = inflate_script(script)
        except Exception as exc:
            print(f"[FAIL] {name}: decode error: {exc}")
            continue

        filename = safe_filename(name, version)
        out_path = out_dir / filename
        raw = text.encode("utf-8")
        out_path.write_bytes(raw)
        md5 = hashlib.md5(raw).hexdigest()
        print(f"[OK] {name}")
        print(f"     -> {out_path}")
        print(f"     version={version or '-'}  bytes={len(text.encode('utf-8'))}  md5={md5}")
        print()


if __name__ == "__main__":
    main()
