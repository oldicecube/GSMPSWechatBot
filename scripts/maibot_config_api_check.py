"""Read-only production evidence plus isolated real plugin LLM capability probes.

The SDK serializes requests into an in-process adapter to the actual Host handler.
This checks SDK payload -> Host routing -> real provider, not Runner IPC transport.
No runtime is bootstrapped and no chat/send capability is exposed.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import shutil
import sys
import time
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def text_parts(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "text" and isinstance(item, str):
                yield item
            elif key not in {"api_provider", "provider_request", "response"}:
                yield from text_parts(item)
    elif isinstance(value, list):
        for item in value:
            yield from text_parts(item)


def inspect_production():
    legacy = json.loads((ROOT / "config.json").read_text(encoding="utf8"))["llm"]
    bot = tomllib.loads((ROOT / "config/bot_config.toml").read_text(encoding="utf8"))
    models = tomllib.loads((ROOT / "config/model_config.toml").read_text(encoding="utf8"))
    rules = legacy.get("identity", {}).get("rules", [])
    native_persona = bot.get("personality", {}).get("personality", "")
    corrupt = []

    def scan(value, path):
        if isinstance(value, dict):
            for k, v in value.items():
                if k not in {"apis", "api_key"}:
                    scan(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                scan(v, f"{path}[{i}]")
        elif isinstance(value, str) and "????" in value:
            corrupt.append(path)

    scan(legacy, "llm")
    prompt_checks, failures = [], []
    for p in (ROOT / "logs/maisaka_prompt").rglob("*.json"):
        try:
            doc = json.loads(p.read_text(encoding="utf8"))
        except (ValueError, OSError):
            continue
        req, meta = doc.get("request", {}), doc.get("metadata", {})
        if req.get("task_name") in {"planner", "replyer"}:
            text = "\n".join(text_parts(doc.get("request_items", [])))
            prompt_checks.append({
                "file": str(p.relative_to(ROOT)), "created_at": meta.get("created_at"),
                "task": req.get("task_name"), "status": meta.get("status"),
                "persona_present": legacy.get("identity", {}).get("role", "!missing!") in text,
                "behavior_present": legacy.get("behavior_style", "!missing!") in text,
                "identity_rules_present": [i for i, rule in enumerate(rules) if rule in text],
                "identity_rules_count": len(rules),
                "profile_marker_present": "[WechatBot chat profile]" in text,
                "corrupt_question_run_present": "????" in text,
                "prompt_sha256": hashlib.sha256(text.encode()).hexdigest(),
            })
        for attempt in doc.get("generation_attempts", []):
            if attempt.get("error"):
                failures.append({"file": str(p.relative_to(ROOT)), "created_at": meta.get("created_at"),
                                 "task": req.get("task_name"), "model": attempt.get("model"),
                                 "error": attempt["error"]})
    features = {k: bot.get(k) for k in ["capabilities", "plugin_runtime", "learning", "voice"]}
    features["memory"] = {k: bot.get("a_memorix", {}).get(k, {}) for k in ["plugin", "integration"]}
    features["mcp_server_count"] = len(bot.get("mcp", {}).get("servers", []))
    features["third_party_manifest_paths"] = [str(p.relative_to(ROOT)) for p in (ROOT / "plugins").glob("*/_manifest.json")]
    snapshots = []
    trace = ROOT / "logs/llm_runtime.jsonl"
    if trace.exists():
        for line in trace.read_text(encoding="utf8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("event") == "maibot_native_config_loaded":
                snapshots.append(row)
    snapshot = snapshots[-1] if snapshots else None
    if snapshot:
        for field, config_key in [("persona", "personality"), ("behavior", "behavior_style"), ("reply_style", "reply_style")]:
            value = bot.get("personality", {}).get(config_key, "")
            snapshot[f"{field}_matches_disk"] = snapshot.get(f"{field}_sha256") == hashlib.sha256(value.encode()).hexdigest()
    return {
        "checked_at": datetime.now().isoformat(), "host_version": tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf8"))["project"]["version"],
        "sdk_version": importlib.metadata.version("maibot-plugin-sdk"),
        "native_identity_rule_coverage": {"present": sum(rule in native_persona for rule in rules), "total": len(rules)},
        "corrupt_legacy_fields": corrupt, "features": features,
        "task_routes": models.get("model_task_config", {}),
        "recent_prompt_evidence": sorted(prompt_checks, key=lambda r: r.get("created_at") or "", reverse=True)[:12],
        "recent_provider_failures": sorted(failures, key=lambda r: r.get("created_at") or "", reverse=True)[:20],
        "running_core_config_snapshot": snapshot,
    }


async def probe_apis(report, save, timeout):
    from maibot_sdk.context import PluginContext
    from src.common.database.database import initialize_database
    from src.config.config import config_manager
    from src.plugin_runtime.capabilities.core import RuntimeCoreCapabilityMixin

    initialize_database()
    handler = RuntimeCoreCapabilityMixin()
    routes = []

    async def rpc(method, plugin_id, payload, timeout_ms=None):
        assert method == "cap.call"
        capability = payload["capability"]
        assert capability in {"llm.generate", "llm.generate_with_tools"}
        args = payload.get("args", {})
        routes.append({"capability": capability, "task_name": args.get("task_name"), "model": args.get("model")})
        fn = handler._cap_llm_generate_with_tools if capability.endswith("with_tools") else handler._cap_llm_generate
        return await fn(plugin_id, capability, args)

    ctx = PluginContext("wechat.config-api-diagnostic", rpc_call=rpc)
    report["api_probe_scope"] = "Installed SDK serialization -> in-process real Host capability -> configured provider. Production config unchanged; no Runner IPC or WeChat sends."
    report["api_probes"] = []
    configs = config_manager.get_model_config()
    candidates = list(configs.model_task_config.utils.model_list)
    for name in candidates:
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(ctx.llm.generate(
                "Connection check. Reply with exactly OK.", task_name="utils", model=name,
            ), timeout=timeout)
            row = {"model": name, "success": bool(result.get("success")),
                   "response": str(result.get("response") or result.get("content") or "")[:160],
                   "error": str(result.get("error") or "")[:1600]}
        except Exception as exc:
            row = {"model": name, "success": False, "error": f"{type(exc).__name__}: {exc}"}
        row["seconds"] = round(time.monotonic() - started, 2)
        report["api_probes"].append(row)
        report["sdk_payload_routes"] = routes
        save()
        print(json.dumps(row, ensure_ascii=True), flush=True)

    working = next((r["model"] for r in report["api_probes"] if r["success"]), None)
    if working:
        for task in ["planner", "replyer", "memory"]:
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(ctx.llm.generate(
                    "Reply with exactly OK.", task_name=task, model=working,
                ), timeout=timeout)
                row = {"task": task, "model": working, "success": bool(result.get("success")),
                       "error": str(result.get("error") or "")[:1600]}
            except Exception as exc:
                row = {"task": task, "success": False, "error": f"{type(exc).__name__}: {exc}"}
            row["seconds"] = round(time.monotonic() - started, 2)
            report.setdefault("task_probes", []).append(row)
            save()
            print(json.dumps(row, ensure_ascii=True), flush=True)

        from PIL import Image
        image = io.BytesIO()
        Image.new("RGB", (64, 64), "red").save(image, format="PNG")
        image_url = "data:image/png;base64," + base64.b64encode(image.getvalue()).decode()
        mode_calls = {
            "tools": lambda: ctx.llm.generate_with_tools(
                "Call diagnostic_ping with value ok. This is a dry run; no tool will be executed.",
                [{"type": "function", "function": {"name": "diagnostic_ping", "description": "A dry-run diagnostic tool.",
                  "parameters": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}}}],
                task_name="planner", model=working,
            ),
            "vision": lambda: ctx.llm.generate(
                [{"role": "user", "content": [{"type": "text", "text": "What color is this image? One word."},
                  {"type": "image_url", "image_url": {"url": image_url}}]}],
                task_name="vlm", model=working,
            ),
        }
        for mode, call in mode_calls.items():
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(call(), timeout=timeout)
                calls = result.get("tool_calls") or []
                row = {"mode": mode, "model": working, "success": bool(result.get("success")),
                       "tool_calls": calls, "response": str(result.get("response") or "")[:160],
                       "error": str(result.get("error") or "")[:1600]}
                if mode == "tools":
                    row["requested_tool_returned"] = any(c.get("function", {}).get("name") == "diagnostic_ping" for c in calls)
            except Exception as exc:
                row = {"mode": mode, "success": False, "error": f"{type(exc).__name__}: {exc}"}
            row["seconds"] = round(time.monotonic() - started, 2)
            report.setdefault("mode_probes", []).append(row)
            save()
            print(json.dumps(row, ensure_ascii=True), flush=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--timeout", type=float, default=55)
    parser.add_argument("--output", default="docs/maibot-wechat-acceptance/config-api-check-20261029.json")
    args = parser.parse_args()
    report = inspect_production()
    output = (ROOT / args.output).resolve()

    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")

    save()
    if args.live:
        instance = ROOT / "temp/config-api-check" / datetime.now().strftime("%Y%m%d-%H%M%S")
        (instance / "config").mkdir(parents=True)
        for name in ["bot_config.toml", "model_config.toml"]:
            shutil.copy2(ROOT / "config" / name, instance / "config" / name)
        os.environ["MAIBOT_INSTANCE_DIR"] = str(instance)
        os.chdir(instance)
        report["isolated_instance"] = str(instance)
        save()
        asyncio.run(probe_apis(report, save, args.timeout))
    print(json.dumps({"report": str(output), "identity_rules": report["native_identity_rule_coverage"],
                      "corrupt_fields": report["corrupt_legacy_fields"]}, ensure_ascii=True), flush=True)


if __name__ == "__main__":
    main()
