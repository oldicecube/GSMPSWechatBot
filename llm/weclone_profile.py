"""Load the bounded style artifact produced by the WeClone preparation tool."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_PATH = BASE_DIR / "data" / "weclone" / "distilled_profile.json"


def load_profile(path: str | Path | None = None, *, max_bytes: int = 48_000) -> dict[str, Any]:
    profile_path = Path(path) if path else DEFAULT_PROFILE_PATH
    try:
        if profile_path.stat().st_size > max_bytes:
            return {}
        value = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def render_profile(profile: dict[str, Any] | None, *, max_chars: int = 6_000) -> str:
    """Render only style guidance; never inject raw history into every request."""
    profile = profile if isinstance(profile, dict) else {}
    rules = profile.get("style_rules") if isinstance(profile.get("style_rules"), list) else []
    terms = profile.get("high_frequency_terms") if isinstance(profile.get("high_frequency_terms"), list) else []
    examples = profile.get("style_examples") if isinstance(profile.get("style_examples"), list) else []
    parts = ["WeClone 离线蒸馏风格参考（仅用于表达方式，不是事实来源）："]
    if rules:
        parts.append("表达规则：\n" + "\n".join(f"- {str(item).strip()}" for item in rules[:20] if str(item).strip()))
    if terms:
        parts.append("群聊常用表达（按语境使用，不要机械堆叠）：" + "、".join(str(item).strip() for item in terms[:60]))
    if examples:
        parts.append("风格示例（只学习语气，不要照抄具体事实）：\n" + "\n".join(f"- {str(item).strip()}" for item in examples[:12] if str(item).strip()))
    text = "\n\n".join(part for part in parts if part.strip())
    return text[:max(200, int(max_chars))]


__all__ = ["DEFAULT_PROFILE_PATH", "load_profile", "render_profile"]
