"""Synchronize transport identity before loading native MaiBot configuration.

Native TOML/WebUI always owns persona, nickname, behavior and expression.
Legacy model import never imports conversational settings from config.json.
"""

from __future__ import annotations

import json
import re
import os
from pathlib import Path
from typing import Any

import tomlkit
from utils.wechat_identity import canonical_wechat_user_id


ROOT = Path(os.environ.get("MAIBOT_INSTANCE_DIR") or Path(__file__).resolve().parents[2])
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
    apis = llm.get("apis") if isinstance(llm.get("apis"), list) else []
    apis = [a for a in apis if isinstance(a, dict) and a.get("enabled", True)]
    apis.sort(key=lambda a: int(a.get("priority", 100)))

    try:
        bot_doc = tomlkit.parse(BOT_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError('Native bot configuration is missing or invalid; refusing to rebuild persona from shell settings') from exc

    bot = bot_doc.setdefault("bot", tomlkit.table())
    account = str((legacy.get("weflow") or {}).get("myWxid") or "wechatbot")
    # Route/account IDs retain their directory suffix for existing session IDs;
    # the core's self/mention checks need the actual WeChat user ID first.
    accounts = list(dict.fromkeys([canonical_wechat_user_id(account), account]))
    bot["platforms"] = [p for p in bot.get("platforms", []) if not str(p).startswith("wechat:")] + [f"wechat:{value}" for value in accounts]
    # Remove only retired bridge-owned prompt entries; preserve native WebUI edits.
    prompts = bot_doc.get('chat', {}).get('reply_style', {}).get('chat_prompts')
    if prompts is not None:
        for index in reversed(range(len(prompts))):
            if str(prompts[index].get('prompt', '')).startswith('[WechatBot chat profile]\n'):
                del prompts[index]
    _write_toml(BOT_CONFIG, bot_doc)

    try:
        previous_models = tomlkit.parse(MODEL_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous_models = {}
    if llm.get('model_config_source') == 'maibot':
        # Once imported, WebUI/model_config.toml owns endpoints, task routing,
        # credentials and order. Never resurrect deleted endpoints on restart.
        if not previous_models:
            raise ValueError('Native model configuration is missing or invalid; refusing to restore the retired shell pool')
        pending_path = MODEL_CONFIG.parent / 'gsmps_model_import_pending.json'
        if pending_path.exists():
            # One-time import deferred for the still-running older config validator.
            # Merge the current file so intervening WebUI edits remain authoritative.
            from scripts.adopt_maibot_model_pool import adopt
            pending = json.loads(pending_path.read_text(encoding='utf8'))
            previous_models = adopt({'llm': {'apis': pending['apis']}}, previous_models.unwrap())
            temporary = MODEL_CONFIG.with_suffix('.tmp')
            temporary.write_text(tomlkit.dumps(previous_models), encoding='utf8')
            os.replace(temporary, MODEL_CONFIG)
            pending_path.unlink()
        return {'models': [m['name'] for m in previous_models.get('models', [])],
                'providers': len(previous_models.get('api_providers', [])),
                'bot_config': str(BOT_CONFIG), 'model_config': str(MODEL_CONFIG),
                'model_config_source': 'maibot'}
    model_doc = tomlkit.document()
    inner = tomlkit.table(); inner.add("version", "1.17.24"); model_doc.add("inner", inner)
    models = tomlkit.aot()
    providers = tomlkit.aot()
    model_names: list[str] = []
    visual_names: list[str] = []
    for index, raw in enumerate(apis):
        if not isinstance(raw, dict):
            continue
        api_name = str(raw.get("name") or f"legacy_api_{index + 1}")
        provider_name = f"WechatBot_{_slug(api_name)}"
        model_name = f"wechat_{_slug(raw.get('model') or api_name)}"
        model_names.append(model_name)
        if raw.get("supports_images", False):
            visual_names.append(model_name)
        provider = tomlkit.table()
        provider.update({
            "name": provider_name,
            "base_url": str(raw.get("base_url") or ""),
            "api_key": str(raw.get("api_key") or ""),
            "client_type": str(raw.get("protocol") or "openai"),
            "auth_type": str(raw.get("auth_mode") or "bearer").lower(),
            "timeout": int(raw.get("timeout_seconds") or 90),
            "max_retry": 1,
            "retry_interval": 1,
            "request_min_interval": float(raw.get("request_min_interval") or 0.0),
            "max_images_per_request": int(raw.get("max_images_per_request") or 0),
        })
        for field in ("default_headers", "default_query", "auth_header_name", "auth_header_prefix", "auth_query_name"):
            if field in raw:
                provider[field] = raw[field]
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

    # Keep independently configured native embedding/search/audio models and tasks.
    for old_model in previous_models.get("models", []):
        if not str(old_model.get("name", "")).startswith("wechat_"):
            models.append(old_model)
    known_providers = {p["name"] for p in providers}
    for old_provider in previous_models.get("api_providers", []):
        if not str(old_provider.get("name", "")).startswith("WechatBot_") and old_provider["name"] not in known_providers:
            providers.append(old_provider)
    if not model_names:
        raise ValueError("No enabled WeChat LLM APIs configured")
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
    task_root = previous_models.get("model_task_config", tomlkit.table())
    for task, (name, max_tokens, temperature) in task_defaults.items():
        section = tomlkit.table()
        section.update({"model_list": list(visual_names if task == "vlm" else model_names), "max_tokens": max_tokens, "temperature": temperature, "selection_strategy": "sequential", "hard_timeout": 90.0})
        task_root[task] = section
    model_doc.add("model_task_config", task_root)
    _write_toml(MODEL_CONFIG, model_doc)
    return {"models": model_names, "providers": len(providers), "bot_config": str(BOT_CONFIG), "model_config": str(MODEL_CONFIG)}
