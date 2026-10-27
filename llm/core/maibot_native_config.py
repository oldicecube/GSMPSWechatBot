"""Load the native MaiBot configuration from the legacy WeChat LLM config.

This runs before importing ``src.config.config``.  MaiBot therefore sees a
native ``config/bot_config.toml`` and ``config/model_config.toml`` generated
from the repository's existing ``config.json``; no path outside this repo is
used at runtime.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import tomlkit


ROOT = Path(__file__).resolve().parents[2]
BOT_CONFIG = ROOT / "config" / "bot_config.toml"
MODEL_CONFIG = ROOT / "config" / "model_config.toml"
LEGACY_CONFIG = ROOT / "config.json"


def _slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "")).strip("_").lower()
    return value or "model"


def _read_legacy() -> dict[str, Any]:
    try:
        return json.loads(LEGACY_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_toml(path: Path, document: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(document), encoding="utf-8")


def sync_native_config() -> dict[str, Any]:
    legacy = _read_legacy()
    llm = legacy.get("llm") if isinstance(legacy.get("llm"), dict) else {}
    identity = llm.get("identity") if isinstance(llm.get("identity"), dict) else {}
    apis = llm.get("apis") if isinstance(llm.get("apis"), list) else []

    try:
        bot_doc = tomlkit.parse(BOT_CONFIG.read_text(encoding="utf-8"))
    except Exception:
        bot_doc = tomlkit.document()

    bot = bot_doc.setdefault("bot", tomlkit.table())
    personality = bot_doc.setdefault("personality", tomlkit.table())
    chat = bot_doc.setdefault("chat", tomlkit.table())
    reply_style = chat.setdefault("reply_style", tomlkit.table())
    bot["nickname"] = str(llm.get("assistant_nickname") or identity.get("name") or "MaiBot")
    personality["personality"] = "。".join(str(identity.get(k) or "").strip() for k in ("name", "role", "style") if identity.get(k))
    personality["behavior_style"] = str(llm.get("behavior_style") or personality.get("behavior_style") or "")
    personality["reply_style"] = str(llm.get("reply_style") or personality.get("reply_style") or "")
    reply_style["group_chat_prompt"] = str(llm.get("group_prompt") or reply_style.get("group_chat_prompt") or "")
    reply_style["private_chat_prompts"] = str(llm.get("private_prompt") or reply_style.get("private_chat_prompts") or "")
    _write_toml(BOT_CONFIG, bot_doc)

    model_doc = tomlkit.document()
    inner = tomlkit.table(); inner.add("version", "1.17.24"); model_doc.add("inner", inner)
    models = tomlkit.aot()
    providers = tomlkit.aot()
    model_names: list[str] = []
    for index, raw in enumerate(apis):
        if not isinstance(raw, dict):
            continue
        api_name = str(raw.get("name") or f"legacy_api_{index + 1}")
        provider_name = f"WechatBot_{_slug(api_name)}"
        model_name = f"wechat_{_slug(raw.get('model') or api_name)}"
        model_names.append(model_name)
        provider = tomlkit.table()
        provider.update({
            "name": provider_name,
            "base_url": str(raw.get("base_url") or ""),
            "api_key": str(raw.get("api_key") or ""),
            "client_type": str(raw.get("protocol") or "openai"),
            "auth_type": "none" if str(raw.get("auth_mode") or "").lower() == "none" else "bearer",
            "timeout": int(raw.get("timeout_seconds") or 90),
            "max_retry": 1,
            "retry_interval": 1,
        })
        providers.append(provider)
        model = tomlkit.table()
        model.update({
            "model_type": "llm",
            "model_identifier": str(raw.get("model") or model_name),
            "name": model_name,
            "api_provider": provider_name,
            "price_in": 0.0,
            "price_out": 0.0,
            "cache": bool(raw.get("cache", True)),
            "visual": bool(raw.get("supports_images", False)),
        })
        if isinstance(raw.get("max_tokens"), int):
            model["max_tokens"] = int(raw["max_tokens"])
        extra = tomlkit.table()
        if isinstance(raw.get("thinking_params"), dict):
            extra.update(raw["thinking_params"])
        if extra:
            model.add("extra_params", extra)
        models.append(model)

    if not model_names:
        model_names = ["wechat_free_kilo_auto"]
    model_doc.add("models", models)
    model_doc.add("api_providers", providers)
    task_defaults = {
        "planner": (model_names[0], 1024, 0.7),
        "replyer": (model_names[0], 4096, 0.8),
        "utils": (model_names[0], 1024, 0.5),
        "learner": (model_names[0], 1024, 0.7),
        "memory": (model_names[0], 2048, 0.3),
        "vlm": (model_names[0], 2048, 0.7),
    }
    task_root = tomlkit.table()
    for task, (name, max_tokens, temperature) in task_defaults.items():
        section = tomlkit.table()
        section.update({"model_list": list(model_names), "max_tokens": max_tokens, "temperature": temperature, "selection_strategy": "sequential", "hard_timeout": 90.0})
        task_root.add(task, section)
    model_doc.add("model_task_config", task_root)
    _write_toml(MODEL_CONFIG, model_doc)
    return {"models": model_names, "providers": len(providers), "bot_config": str(BOT_CONFIG), "model_config": str(MODEL_CONFIG)}
