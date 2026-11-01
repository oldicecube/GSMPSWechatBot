"""Isolated plugin compatibility checks. No chat messages or production reloads."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime
import importlib
import json
import logging
import os
from pathlib import Path
import shutil
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
ROWS = []


def check(name, passed, detail=""):
    ROWS.append(dict(name=name, passed=bool(passed), detail=detail))
    print(f"PLUGIN {'PASS' if passed else 'FAIL'} {name}: {detail}", flush=True)


def load_package(path, name):
    package = ModuleType(name)
    package.__path__ = [str(path)]
    sys.modules[name] = package
    return importlib.import_module(name + ".plugin")


async def run(source, instance, live_sources):
    modules = {}
    components = {}
    defaults = {}
    folders = ["mai-life", "hot-topics", "idle_proactive_chat", "cateye_mai_personality_swap"]
    for i, folder in enumerate(folders):
        mod = load_package(source / folder, f"acceptance_plugin_{i}")
        plugin = mod.create_plugin()
        plugin.set_plugin_config({})
        modules[folder] = (mod, plugin)
        components[folder] = plugin.get_components()
        defaults[folder] = plugin.get_default_config()
        check(folder + ".default_config_and_components", bool(components[folder]))
    names = [str(c.get("name")) for group in components.values() for c in group]
    check("no_duplicate_component_names", len(names) == len(set(names)), names)

    hot_mod, hot = modules["hot-topics"]
    chat = SimpleNamespace(get_stream_by_group_id=AsyncMock(return_value={"session_id": "wechat-group-stream"}),
                           get_stream_by_user_id=AsyncMock(return_value={"session_id": "wechat-private-stream"}),
                           get_all_streams=AsyncMock(return_value=[{"session_id": "all-stream", "group_id": "123@chatroom"}]))
    hot._set_context(SimpleNamespace(chat=chat, logger=logging.getLogger("plugin_test")))
    hot.config.ambient.group_ids = ["123@chatroom", "wechat:group:456@chatroom", "123456"]
    hot.config.ambient.private_ids = ["wxid_friend", "wechat:private:wxid_other"]
    streams, _ = await hot._resolve_target_streams()
    check("hot.wechat_and_qq_platform_routing", chat.get_stream_by_group_id.await_args_list == [
        __import__("unittest.mock").mock.call("123@chatroom", platform="wechat"),
        __import__("unittest.mock").mock.call("456@chatroom", platform="wechat"),
        __import__("unittest.mock").mock.call("123456", platform="qq")])
    check("hot.private_ids_and_session_id", set(streams) == {"wechat-group-stream", "wechat-private-stream"}
          and all(c.kwargs["platform"] == "wechat" for c in chat.get_stream_by_user_id.await_args_list))
    hot.config.ambient.apply_to_all = True
    check("hot.all_platform_stream_discovery", (await hot._resolve_target_streams())[0] == ["all-stream"]
          and chat.get_all_streams.await_args.args == ("all_platforms",))
    check("hot.helper_import_is_namespaced", "hot_core" not in sys.modules)

    persona_mod, persona = modules["cateye_mai_personality_swap"]
    normalize = persona_mod.plain_id
    check("persona.wechat_identity_supported", normalize("wxid_friend") == normalize("wechat:wxid_friend") == "wechat:wxid_friend")
    check("persona.cross_platform_identity_separation", normalize("wechat:123") != normalize("qq:123")
          and normalize("wechat:wxid_friend", platform="qq") == "" and normalize("qq:123", platform="wechat") == "")
    check("persona.qq_backward_compatibility", normalize(123) == normalize("qq:123") == "123" and normalize("invalid text") == "")
    persona._host_admin_ids = AsyncMock(return_value=["wechat:wxid_admin"])
    check("persona.wechat_admin_permission", await persona._is_admin_call(is_local_operator=False, group_id="", user_id="wxid_admin", platform="wechat"))
    check("persona.qq_cannot_impersonate_wechat_admin", not await persona._is_admin_call(is_local_operator=False, group_id="", user_id="wxid_admin", platform="qq"))
    persona._set_context(SimpleNamespace(config=SimpleNamespace(get=AsyncMock(return_value=["wechat:wxid_bot"])), logger=logging.getLogger("plugin_test")))
    persona._read_official = AsyncMock(side_effect=["测试麦麦", "123456"])
    await persona._warm_bot_name()
    check("persona.wechat_bot_identity", persona.script_bot_user_id == "wxid_bot")
    script = importlib.import_module(persona_mod.__package__ + ".script_host")
    ctx = script.ScriptContext(SimpleNamespace(_backend=persona), "message", bot_user_id="wxid_bot",
                               raw_segments=[{"type": "at", "data": {"target_user_id": "wxid_bot"}}])
    check("persona.native_wechat_at_component", ctx.is_at_me)

    life_mod, life = modules["mai-life"]
    policy = [{"platform": "wechat", "group_id": "123@chatroom", "user_id": "wxid_excluded"}]
    excluded = {"platform": "wechat", "message_info": {"user_info": {"user_id": "wxid_excluded"}, "group_info": {"group_id": "123@chatroom"}}}
    allowed = deepcopy(excluded)
    allowed["message_info"]["user_info"]["user_id"] = "wxid_allowed"
    check("life.learning_blacklist_scope", not life._learning_message_allowed(excluded, policy)
          and life._learning_message_allowed(allowed, policy))
    other_group = deepcopy(excluded)
    other_group["message_info"]["group_info"]["group_id"] = "other@chatroom"
    check("life.blacklist_does_not_mute_other_groups", life._learning_message_allowed(other_group, policy))
    unlearnable = deepcopy(allowed)
    unlearnable["message_info"]["additional_config"] = {"wechat": {"learnable": False}}
    check("life.message_learning_metadata", not life._learning_message_allowed(unlearnable, policy))
    life.config.generation.include_history = True
    life._set_context(SimpleNamespace(config=SimpleNamespace(get=AsyncMock(return_value=policy)),
        message=SimpleNamespace(get_by_time_in_chat=AsyncMock(return_value=[excluded, allowed]), build_readable=AsyncMock(return_value="allowed history"))))
    check("life.blacklist_filtered_before_readable_and_llm", await life._resolve_history(["synthetic-stream"]) != ""
          and life.ctx.message.build_readable.await_args.args[0] == [allowed])
    life.ctx.config.get = AsyncMock(side_effect=RuntimeError("policy unavailable"))
    life.ctx.message.build_readable.reset_mock()
    check("life.policy_failure_skips_learning_history", await life._resolve_history(["synthetic-stream"]) == ""
          and not life.ctx.message.build_readable.called)

    life.config.plugin.enabled = True
    life.config.schedule.enabled = True
    life.config.schedule.allowed_streams = ["wechat:group:123@chatroom"]
    life._cached_streams = AsyncMock(return_value=[{"session_id": "synthetic-stream", "platform": "wechat", "group_id": "123@chatroom", "chat_type": "group"}])
    store = importlib.import_module(life_mod.__package__ + ".store")
    now = datetime(2026, 10, 29, 12, 30)
    life._now = lambda: now
    life._load_today = lambda now: store.LifeDocument("2026-10-29", now.isoformat(), activities=[store.Activity("test", "12:00", "13:00", "HOOK_SCHEDULE_SENTINEL")])
    life._load_yesterday = lambda now: None
    life._set_context(SimpleNamespace(logger=logging.getLogger("plugin_test")))
    injector = importlib.import_module(life_mod.__package__ + ".injector")
    system = injector.build_system_item("官方身份\n测试麦麦的行为风格：OLD_BEHAVIOR\n以上测试麦麦的行为风格可以帮助你更好地决策")
    payload = {"session_id": "synthetic-stream", "items": [system], "item_schema_version": 1, "tool_definitions": [{"name": "test"}], "sentinel": "metadata"}
    original = deepcopy(payload)
    result = await life.inject_schedule_handler(**payload)
    preset_type = importlib.import_module(persona_mod.__package__ + ".preset_store").PersonaPreset
    persona._active_preset_for_stream = AsyncMock(return_value=preset_type("synthetic", behavior="HOOK_PERSONA_SENTINEL"))
    result = await persona.on_planner_before_request(**result["modified_kwargs"])
    modified = result["modified_kwargs"]
    text = json.dumps(modified, ensure_ascii=False)
    check("combined_hooks.life_context_and_personality_both_retained", "HOOK_SCHEDULE_SENTINEL" in text and "HOOK_PERSONA_SENTINEL" in text and "OLD_BEHAVIOR" not in text)
    check("combined_hooks.metadata_and_original_payload_preserved", modified["sentinel"] == "metadata"
          and modified["tool_definitions"] == payload["tool_definitions"] and payload == original)

    # A real Host capability response is encoded/decoded, rather than passing Python models directly.
    from src.plugin_runtime.capabilities.core import RuntimeCoreCapabilityMixin
    from src.plugin_runtime.protocol.codec import MsgPackCodec
    host = RuntimeCoreCapabilityMixin()
    wire = await host._cap_config_get("diagnostic", "config.get", {"key": "learning.excluded_users", "default": []})
    decoded = MsgPackCodec().decode(MsgPackCodec().encode(wire))
    check("host.learning_config_rpc_returns_dicts", decoded["success"] and all(isinstance(r, dict) for r in decoded["value"]))

    from src.llm_models import quota_cooldown
    from src.llm_models.exceptions import RespNotOkException
    from unittest.mock import patch
    provider = SimpleNamespace(base_url="https://fixture.invalid/v1", api_key="old-credential")
    model = SimpleNamespace(model_identifier="fixture")
    quota = RespNotOkException(429, "PROMOTION_MODEL_LIMIT_REACHED")
    check("quota.terminal_and_transient_429_distinguished", quota_cooldown.explicit_quota_exhaustion(quota)
          and not quota_cooldown.explicit_quota_exhaustion(RespNotOkException(429, "temporary rate limit")))
    with patch.object(quota_cooldown.time, "monotonic", return_value=100.0):
        quota_cooldown.mark_exhausted(provider, model)
        check("quota.exhausted_route_blocked", bool(quota_cooldown.blocked_reason(provider, model)))
        fresh = SimpleNamespace(base_url=provider.base_url, api_key="new-credential")
        check("quota.new_credential_unblocked", not quota_cooldown.blocked_reason(fresh, model))
    with patch.object(quota_cooldown.time, "monotonic", return_value=701.0):
        check("quota.route_reprobed_after_cooldown", not quota_cooldown.blocked_reason(provider, model))

    if live_sources:
        async def fetch(key):
            try:
                items = await asyncio.to_thread(hot_mod.hot_core.fetch_platform, key)
                check("hot.live_source." + key, bool(items), {"items": len(items)})
            except Exception as exc:
                check("hot.live_source." + key, False, str(exc))
        await asyncio.gather(fetch("bili"), fetch("douyin"))
    return dict(checks=ROWS, defaults=defaults, components=components, passed=all(r["passed"] for r in ROWS), instance=str(instance))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default="plugins")
    parser.add_argument("--live-sources", action="store_true")
    args = parser.parse_args()
    source = (ROOT / args.source).resolve()
    instance = ROOT / "temp/plugin-compatibility" / datetime.now().strftime("%Y%m%d-%H%M%S")
    (instance / "config").mkdir(parents=True)
    for name in ["bot_config.toml", "model_config.toml"]:
        shutil.copy2(ROOT / "config" / name, instance / "config" / name)
    os.environ["MAIBOT_INSTANCE_DIR"] = str(instance)
    os.chdir(instance)
    result = asyncio.run(run(source, instance, args.live_sources))
    output = ROOT / "docs/maibot-wechat-acceptance/plugin-compatibility-20261029.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
