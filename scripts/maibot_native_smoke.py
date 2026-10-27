"""Verify that Wechat Bot loads the vendored MaiBot core without D:\\maibot."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm.core import LLMService


def main() -> None:
    service = LLMService()
    assert (ROOT / "src" / "maisaka" / "runtime.py").exists()
    assert (ROOT / "config" / "bot_config.toml").exists()
    assert (ROOT / "config" / "model_config.toml").exists()
    assert service._native_runtime._thread is not None
    assert service._native_runtime._thread.name == "maibot-native-core"
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    assert config.get("llm", {}).get("enabled") is True
    print({
        "status": "passed",
        "source": str(ROOT / "src"),
        "runtime_thread": service._native_runtime._thread.name,
        "native_models": service._native_config_summary["models"],
    })


if __name__ == "__main__":
    main()
