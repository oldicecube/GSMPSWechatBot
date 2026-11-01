"""Native style regression using real prompt builders and isolated instance state.

Optional live samples use synthetic messages; never start the bot or send to chat.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def run(live_model=""):
    instance = Path(tempfile.mkdtemp(prefix="native-style-", dir=ROOT / "temp"))
    os.environ["MAIBOT_INSTANCE_DIR"] = str(instance)
    (instance / "config").mkdir()
    for name in ("bot_config.toml", "model_config.toml"):
        shutil.copy2(ROOT / "config" / name, instance / "config" / name)
    report = dict(instance=str(instance), production_restart=False, physical_messages=False,
                  checks=[], live_samples=[])

    def check(name, condition):
        report["checks"].append(dict(name=name, passed=bool(condition)))
        print(name, bool(condition), flush=True)

    from llm.core.native_prompt_policy import native_styles, profile_prompt
    from llm.core import maibot_native_config as mapper
    styles = native_styles()
    doc = tomllib.loads((ROOT / "config/bot_config.toml").read_text(encoding="utf8"))
    legacy = json.loads((ROOT / "config.json").read_text(encoding="utf8"))
    persona = doc["personality"]["personality"]
    baseline = json.loads((ROOT / "docs/maibot-wechat-acceptance/native-style-config-20261030.json").read_text(encoding="utf8"))
    check("custom_persona_unchanged", hashlib.sha256(persona.encode()).hexdigest() == baseline["custom_persona_sha256_before"])
    for field, expected in styles.items():
        section = doc["chat"]["reply_style"] if field in ("group_chat_prompt", "private_chat_prompts") else doc["personality"]
        check("production_native_" + field, section[field] == expected)
    check("group_profiles_no_style_reinjection", all(not profile_prompt(legacy["llm"], p)
          for p in legacy["llm"].get("group_profiles", {}).values()))
    check("shell_scoped_identity_and_boundaries_ignored", profile_prompt(legacy["llm"], dict(
        identity=dict(name="测试身份", style="STYLE_SENTINEL"), behavior_boundaries=["测试边界"],
        reply_style="STYLE_SENTINEL")) == {})

    # Exercise future startup mapping with deliberately stale style overrides.
    edited = json.loads(json.dumps(legacy))
    edited["llm"].setdefault("identity", {})["style"] = "STYLE_SENTINEL"
    for field in ("reply_style", "behavior_style", "group_prompt", "private_prompt"):
        edited["llm"][field] = "STYLE_SENTINEL"
    with patch.object(mapper, "_read_legacy", return_value=edited):
        mapper.sync_native_config()
    mapped = tomllib.loads((instance / "config/bot_config.toml").read_text(encoding="utf8"))
    check("restart_mapping_preserves_persona", mapped["personality"]["personality"] == persona)
    check("restart_mapping_blocks_stale_styles", "STYLE_SENTINEL" not in json.dumps(mapped, ensure_ascii=False))
    check("restart_mapping_preserves_timing", mapped["chat"]["reply_timing"] == doc["chat"]["reply_timing"])
    # Restore real model configuration for optional samples; mapping is isolated.
    shutil.copy2(ROOT / "config/model_config.toml", instance / "config/model_config.toml")

    from src.config.config import global_config
    from src.config.official_configs import PersonalityConfig, ChatReplyStyleConfig, ChatReplyTimingConfig
    from src.maisaka.chat_loop_service import MaisakaChatLoopService
    from src.chat.replyer.maisaka_generator import MaisakaReplyGenerator
    native_persona = PersonalityConfig()
    native_attention = ChatReplyStyleConfig()
    native_timing = ChatReplyTimingConfig()
    scene = "善于用人类的角度思考问题，聊天比较日常。"
    check("restored_guidance_is_native", scene in native_persona.personality)
    check("reply_style_only_native_guidance_added", styles["reply_style"] == scene + native_persona.reply_style)
    check("planner_only_native_identity_removed", styles["behavior_style"] == scene + native_persona.behavior_style.removeprefix(native_persona.personality))
    check("reply_timing_equals_native", global_config.chat.reply_timing.model_dump() == native_timing.model_dump())
    check("group_attention_equals_native", global_config.chat.reply_style.group_chat_prompt == native_attention.group_chat_prompt)
    check("private_attention_equals_native", global_config.chat.reply_style.private_chat_prompts == native_attention.private_chat_prompts)
    check("alternate_style_probability_equals_native", global_config.personality.multiple_probability == native_persona.multiple_probability)
    custom_prompts = ROOT / "data/custom_prompts"
    check("no_custom_planner_replyer_templates", not custom_prompts.exists() or not any(
        p.stem in {"maisaka_chat", "maisaka_chat_focus", "maisaka_chat_passive", "maisaka_replyer"}
        for p in custom_prompts.rglob("*.prompt")))

    prompts = {}
    for group in (True, False):
        label = "group" if group else "private"
        planner = MaisakaChatLoopService(session_id="synthetic-style", is_group_chat=group)
        replyer = MaisakaReplyGenerator(chat_stream=SimpleNamespace(session_id="synthetic-style"))
        planner_text = planner._build_chat_system_prompt()
        with patch("src.chat.replyer.maisaka_generator_base.get_chat_type_and_target_info", return_value=(group, "synthetic")), \
             patch.object(replyer, "_get_chat_prompt_for_chat", return_value=""):
            replyer_text = replyer._build_system_prompt(None, "")
        attention = styles["group_chat_prompt" if group else "private_chat_prompts"].strip()
        for phase, text in (("planner", planner_text), ("replyer", replyer_text)):
            name = label + "_" + phase
            check(name + "_full_custom_persona", persona in text)
            check(name + "_native_daily_guidance_once", text.count(scene) == 1)
            check(name + "_native_attention", attention in text)
            check(name + "_no_default_identity", "女大学生" not in text and "一反常态，表现出锐利攻击性" not in text)
            check(name + "_no_fallback_or_sentinel", "STYLE_SENTINEL" not in text and "你是一个友好的 AI 助手" not in text)
            prompts[name] = text
    # Save exact synthetic system prompts, with no private group conversation.
    (instance / "assembled-prompts.json").write_text(json.dumps(prompts, ensure_ascii=False, indent=2), encoding="utf8")
    report["assembled_system_prompts"] = str(instance / "assembled-prompts.json")

    if live_model:
        from src.common.data_models.llm_service_data_models import LLMGenerationOptions
        from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType
        from src.services.llm_service import LLMServiceClient
        client = LLMServiceClient("replyer", request_type="maisaka.acceptance.synthetic.style")
        for scene_name, message in (
            ("casual", "小林：下班了，脑子已经变成一块石头，今天啥也不想干。"),
            ("playful", "小林：刚进服务器就掉岩浆了，麦麦快笑我一下。"),
            ("curiosity", "小林：我想用红石造个会翻白眼的小装置，你觉得咋整？"),
        ):
            row = dict(scene=scene_name, input=message, model=live_model)
            try:
                def context_factory(_client):
                    return [ContextItemBuilder().set_role(RoleType.System).add_text_content(prompts["group_replyer"]).build(),
                            ContextItemBuilder().set_role(RoleType.User).add_text_content(
                                "以下是虚构群聊，仅用于回复风格验收：\n" + message + "\n请自然回复这一条。").build()]
                result = await asyncio.wait_for(client.generate_response_with_context(
                    context_factory, LLMGenerationOptions(model_name=live_model, max_tokens=4096)), timeout=110)
                row.update(response=result.response, nonempty=bool(result.response.strip()))
            except Exception as exc:
                row.update(error=type(exc).__name__ + ": " + str(exc)[:300], nonempty=False)
            report["live_samples"].append(row)
            print("live_sample", scene_name, row, flush=True)
    report["passed"] = all(c["passed"] for c in report["checks"])
    report["passed_scope"] = "deterministic configuration and actual native prompt assembly checks only"
    report["live_samples_nonempty"] = (all(s["nonempty"] for s in report["live_samples"])
                                          if report["live_samples"] else None)
    report["limitations"] = ["Synthetic prompt assembly and optional model samples do not certify subjective style in every live conversation.",
                              "No real WeChat message was sent and the production process was not restarted."]
    path = ROOT / "docs/maibot-wechat-acceptance/native-style-20261030.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print("REPORT", path, report["passed"], flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-model", default="")
    args = parser.parse_args()
    raise SystemExit(0 if asyncio.run(run(args.live_model)) else 1)
