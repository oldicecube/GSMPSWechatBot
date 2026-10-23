"""Opt-in live business acceptance, synthetic evidence and isolated SQLite only.

Run: python -X utf8 scripts/business_prompt_acceptance.py
No main import, WeChat delivery, production writes, or paid endpoint fallback.
Reports are written after each step, including HTTP failures and parser rejection.
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
import time
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import queue  # Load stdlib queue before project modules change sys.path.
from llm.config import get_llm_config
from llm.core.llm_service import LLMService
from llm.core.response_parser import load_json_lenient, parse_proactive_response
from llm.learning import StyleLearner
from llm.learning.message_units import build_learning_units
from llm.memory import LongTermMemory
from llm.prompt.prompt_builder import (
    build_batch_user_messages, build_memory_curation_messages,
    build_reply_style_learning_messages, build_system_prompt,
)
from llm.provider.deepseek_provider import OpenAICompatibleClient
from llm.web_tools import MEMORY_LOOKUP_TOOL

GROUP = "acceptance-synthetic-group"


def record(index, content, speaker="human-a", **fields):
    return dict(message_id=f"s{index}", source_id=f"s{index}", nickname=speaker,
                wxid=speaker, sender_wxid=speaker, content=content,
                timestamp=1792700000 + index * 40, role="user", is_bot=False,
                learnable=True, prompt_visible=True, source_kind="user_incoming", **fields)


EVIDENCE = [
    record(1, "我喜欢合作建造。"),
    record(2, "我们把测试环境重启以后恢复正常叫回春了。"),
    record(3, "测试环境重启后恢复了，回春了。", "human-b"),
    record(4, "测试环境又回春了。", "human-c"),
    record(5, "修复成功，这下能安心了。"),
    record(6, "任务完成，这下能安心了。", "human-b"),
    record(7, "群里已经约定：任务甲等待成员确认，尚未完成。"),
    record(8, "有人直接问我时我先答；他们聊得顺就不插话。", "human-c"),
    record(9, "部署的问题解决了吗？"),
    record(10, "解决了。", "human-b"),
    record(11, "那就先这样，晚点再聊。", "human-c"),
    record(12, "好。"),
]


class Acceptance:
    def __init__(self, report_path):
        self.cfg = get_llm_config()
        self.entry = copy.deepcopy(next(e for e in self.cfg["apis"] if e["model"] == "kilo-auto/free"))
        self.entry.update(max_tokens=2304, json_mode=False)
        self.client = OpenAICompatibleClient(self.entry)
        self.identity = self.cfg.get("identity") or {}
        self.prefixes = self.cfg.get("prefixes") or []
        self.system = build_system_prompt(self.cfg.get("prompt"), self.identity, self.prefixes)
        self.path = report_path
        self.report = dict(started_at=datetime.now().astimezone().isoformat(),
                           synthetic=True, production_db_writes=False, wechat_send=False,
                           endpoint=self.entry["base_url"], model=self.entry["model"],
                           evidence=EVIDENCE, local_checks=[], rounds=[])
        self.current_http = []
        self.client._client._client.event_hooks.setdefault("response", []).append(
            lambda response: self.current_http.append(response.status_code))
        self.returned_model = None
        create = self.client._client.chat.completions.create

        def capture_model(**kwargs):
            response = create(**kwargs)
            self.returned_model = getattr(response, "model", None)
            return response

        self.client._client.chat.completions.create = capture_model

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def check(self, name, ok, **details):
        self.report["local_checks"].append(dict(name=name, passed=bool(ok), **details))
        self.save()

    def call(self, name, messages, tools=None, max_tokens=1536):
        row = dict(name=name, input=copy.deepcopy(messages), tools=copy.deepcopy(tools or []),
                   parameters=dict(max_tokens=max_tokens, thinking={}, timeout=40),
                   started_at=datetime.now().astimezone().isoformat())
        self.report["rounds"].append(row)
        self.current_http.clear()
        self.returned_model = None
        self.save()  # Preserve the exact input even if the process is interrupted.
        started = time.monotonic()
        try:
            message = self.client._chat_once(messages, tools=tools or [], thinking={"max_tokens": max_tokens},
                                             prompt_cache_key="business-acceptance-synthetic", timeout=40)
            row.update(transport_ok=True, returned_model=self.returned_model,
                       output=str(getattr(message, "content", "") or ""), usage=dict(self.client.last_usage),
                       tool_calls=[dict(id=c.id, name=c.function.name, arguments=c.function.arguments)
                                   for c in getattr(message, "tool_calls", None) or []])
        except Exception as exc:
            row.update(transport_ok=False, error_type=type(exc).__name__,
                       status=getattr(exc, "status_code", None), error=str(exc)[:1200])
        row.update(http_statuses=list(self.current_http), elapsed_seconds=round(time.monotonic() - started, 3))
        self.save()
        print(json.dumps({k: v for k, v in row.items() if k not in {"input", "tools"}}, ensure_ascii=False), flush=True)
        return row

    def data(self, current, history=None, force=True, **resources):
        return dict(identity=self.identity, prefixes=self.prefixes, llm_config=self.cfg,
                    current_message=current, group_messages=history or [], batch_messages=[dict(current, batch_index=1)],
                    force_reply=force, conversation_priority="MUST_REPLY" if force else "MAY_REPLY",
                    trigger_source="acceptance", emoji_list=[], **resources)

    def reply(self, name, current, history=None, force=True, **resources):
        data = self.data(current, history, force, **resources)
        messages = [{"role": "system", "content": self.system}] + build_batch_user_messages(data)
        row = self.call(name, messages)
        if row.get("transport_ok"):
            row["production_parsed"] = parse_proactive_response(row["output"], [], force_reply=force)
            row["parser_accepted"] = row["production_parsed"].get("_valid", True) is not False
            row["behavior"] = "manual_review_required"
            self.save()
        return row

    def run(self):
        with tempfile.TemporaryDirectory(prefix="wechat-acceptance-") as temporary:
            learner = StyleLearner({"learning": {"enabled": True, "db_path": str(Path(temporary) / "learning.db")}},
                                   start_worker=False)
            memory = LongTermMemory({"memory": {"enabled": True, "db_path": str(Path(temporary) / "memory.db")}})
            service = LLMService.__new__(LLMService)
            service.config = self.cfg
            service.style_learner = learner
            service.long_term_memory = memory
            service.memory_manager = None
            service._cycle_context_cache = {}
            service._cycle_context_cache_lock = threading.RLock()
            observations = [dict(r, group_id=GROUP, speaker=r["wxid"], speaker_name=r["nickname"],
                                 fingerprint=r["message_id"]) for r in EVIDENCE]
            ignored_bot = dict(observations[0], fingerprint="bot", content="BOT_ONLY_PHRASE", is_bot=True,
                               source_kind="llm_outbound", learnable=False)
            imported = learner.ingest_many(observations + [ignored_bot])
            self.check("human_learning_only", imported == len(EVIDENCE), imported=imported)
            for r in EVIDENCE:
                memory.record_message(dict(r, group=GROUP, messageKey=r["message_id"]))

            units = build_learning_units(EVIDENCE)
            learning_input = [{"role": "system", "content": self.system}] + build_memory_curation_messages({
                "cycle_messages": EVIDENCE, "learning_units": units,
                "identity": self.identity, "prefixes": self.prefixes,
                "memory_state": memory.get_group_memory_state(GROUP),
                "style_learning_payload": learner.get_cycle_style_payload(GROUP),
            })
            learned = self.call("cycle_learning", learning_input, max_tokens=3072)
            if learned.get("transport_ok"):
                parsed = load_json_lenient(learned["output"])
                if not isinstance(parsed, dict):
                    parsed = {}
                learned["action_schema_present"] = any(k in parsed for k in ("expression_actions", "slang_actions", "memory_actions"))
                expression_records = [dict(r, source_id=r["message_id"]) for r in EVIDENCE]
                grounded_expressions = []
                learned["expression_grounding"] = []
                for action in parsed.get("expression_actions") or []:
                    grounded, reason = service._ground_expression_action_in_cycle(action, expression_records)
                    learned["expression_grounding"].append(dict(reason=reason, action=action))
                    if grounded is not None:
                        grounded_expressions.append(grounded)
                learned["expressions_saved"] = learner.apply_expression_actions(
                    GROUP, grounded_expressions, valid_source_ids=[r["message_id"] for r in EVIDENCE])
                learned["slang_writes"] = []
                for action in parsed.get("slang_actions") or []:
                    grounded, reason = service._ground_slang_action_in_cycle(action, units)
                    saved = False
                    if grounded and str(action.get("action")) in {"add", "update"}:
                        saved = learner.store.apply_slang_scenario(dict(grounded, group_id=GROUP, status="active",
                                                                       speakers=grounded.get("source_speakers") or []))
                    learned["slang_writes"].append(dict(grounding=reason, saved=saved, action=action))
                # Keep only actions grounded to this synthetic human group.
                actions = parsed.get("memory_actions") or []
                facts = []
                for action in actions:
                    kind = str(action.get("memory_type") or "")
                    if kind in {"short", "medium", "long", "person_fact", "person_profile"}:
                        facts.append(action)
                learned["memory_write_result"] = memory.apply_memory_actions(GROUP, facts)
                self.save()

            style_messages = [{"role": "system", "content": self.system + (
                "\n\nTRUSTED INTERNAL OVERRIDE: this request is a maintenance-learning pass, not a chat turn. "
                "Return only the abstract style_card JSON requested by the user task. Never return messages, slang, expressions, or concrete examples.")
            }] + build_reply_style_learning_messages(dict(human_messages=EVIDENCE,
                                                          style_learning_payload=learner.get_cycle_style_payload(GROUP)))
            style = self.call("style_learning", style_messages, max_tokens=1800)
            if style.get("transport_ok"):
                style_data = load_json_lenient(style["output"])
                card = style_data.get("style_card", {}) if isinstance(style_data, dict) else {}
                style["card_applied"] = learner.apply_style_card(GROUP, card, len(EVIDENCE))
                style["stored_card"] = learner.store.get_style_card(GROUP)
                self.save()

            # Reopen storage: tests persistence rather than in-memory prompt injection.
            reloaded = StyleLearner({"learning": {"enabled": True, "db_path": learner.store.path}}, start_worker=False)
            self.report["persisted_expressions"] = reloaded.get_expressions(GROUP)
            self.report["persisted_slang"] = reloaded.store.lookup_slang(GROUP, "", 20)
            self.report["persisted_memory"] = memory.get_group_memory_state(GROUP)
            self.report["persisted_person_profile"] = memory.get_person_profile_context(GROUP, "human-a", "喜欢")
            self.save()

            self.reply("direct_fact", record(30, "你算一下，7乘以8等于多少？", addressed_to="bot"))
            self.reply("other_addressee", record(31, "你周末有空吗？", reply_to_other=True, directed_to_other=True,
                                                 reply_target="human-b"), force=False)
            self.reply("closed_topic", record(32, "好，问题都处理完了，先这样吧。"),
                       [record(29, "已经处理完，暂时没有别的问题了。", "human-b")], force=False)
            matched = record(33, "测试环境修好了，回春了，这下能安心了！", directed_to_bot=True)
            slang = reloaded.store.get_context_slang(GROUP, [matched], max_chars=1500)
            expressions = reloaded.get_context_expressions(GROUP, [matched])
            reuse = self.reply("learned_reuse", matched, slang_context=slang,
                               expression_list=expressions, style_profile=reloaded.get_prompt_context(GROUP))
            reuse["resource_source"] = "LLM-learned temporary database; never manually seeded"
            reuse["recalled_slang"] = slang
            reuse["recalled_expressions"] = expressions
            self.save()
            self.reply(
                "multi_point_reply",
                record(38, "请告诉我任务甲现在的状态，再说说部署问题怎么样了，最后给群里一句收尾。", addressed_to_bot=True),
                [record(37, "大家刚刚都在等结果。", "human-b")],
                force=True,
                slang_context=slang,
                expression_list=expressions,
                style_profile=reloaded.get_prompt_context(GROUP),
            )
            low_info = record(37, "回春了就好，安心。", directed_to_bot=False)
            low_info_slang = reloaded.store.get_context_slang(GROUP, [low_info], max_chars=1500)
            low_info_expressions = reloaded.get_context_expressions(GROUP, [low_info])
            self.reply(
                "low_info_banter",
                low_info,
                [record(33, "问题解决了，大家先歇会儿。", "human-b")],
                force=False,
                slang_context=low_info_slang,
                expression_list=low_info_expressions,
                style_profile=reloaded.get_prompt_context(GROUP),
            )
            self.report["rounds"][-1]["recalled_slang"] = low_info_slang
            self.report["rounds"][-1]["recalled_expressions"] = low_info_expressions
            self.save()
            self.reply("technical_no_forced_slang", record(34, "把十进制15转换成二进制。", directed_to_bot=True),
                       slang_context=slang, expression_list=expressions, style_profile=reloaded.get_prompt_context(GROUP))
            state, _ = service._reply_memory_state_if_needed(GROUP, "之前任务甲进展如何？")
            self.reply("memory_recall", record(35, "之前任务甲进展如何？", directed_to_bot=True), **{
                k: state.get(k, "") for k in ("short_memory", "medium_memory", "long_memory")})

            # Native tool round-trip: only real production memory lookup, no canned tool return.
            person_current = record(36, "human-a之前说过喜欢哪种玩法？请查记忆后回答。", directed_to_bot=True)
            person_data = self.data(person_current)
            person_data["batch_messages"][0]["batch_index"] = 1
            person_messages = [{"role": "system", "content": self.system}] + build_batch_user_messages(person_data)
            lookup = self.call("person_lookup_initial", person_messages, tools=[MEMORY_LOOKUP_TOOL])
            if lookup.get("transport_ok") and lookup.get("tool_calls"):
                conversation = person_messages + [dict(role="assistant", content=lookup.get("output") or None,
                    tool_calls=[dict(id=c["id"], type="function", function=dict(name=c["name"], arguments=c["arguments"]))
                                for c in lookup["tool_calls"]])]
                results = []
                for call in lookup["tool_calls"]:
                    if call["name"] != "lookup_group_memory":
                        result = {"ok": False, "error": "tool not provided"}
                    else:
                        args = json.loads(call["arguments"])
                        subject = str(args.get("subject_id") or "")
                        query = str(args.get("query") or "")
                        result = dict(ok=True, group_id=GROUP,
                                      person_profile=memory.get_person_profile_context(GROUP, subject, query),
                                      extra_memory=memory.get_context(GROUP, subject, query))
                    results.append(dict(call=call, result=result))
                    conversation.append(dict(role="tool", tool_call_id=call["id"],
                                             content=json.dumps(result, ensure_ascii=False)))
                lookup["tool_execution"] = results
                self.save()
                final = self.call("person_lookup_final", conversation)
                if final.get("transport_ok"):
                    final["production_parsed"] = parse_proactive_response(final["output"], [])
                    self.save()
            learner.close()
            reloaded.close()
        self.report["finished_at"] = datetime.now().astimezone().isoformat()
        self.save()
        self.client._client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=ROOT / "output/business_acceptance_20261023/report.json")
    args = parser.parse_args()
    Acceptance(args.report).run()
