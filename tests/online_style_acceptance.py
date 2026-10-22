"""Live UUAPI Grok-4.7 style-card acceptance; no Bot start, no WeChat send, no production DB writes."""
import copy
import hashlib
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from llm.config import get_llm_config
from llm.core.llm_service import LLMService
from llm.core.response_parser import parse_llm_response
from llm.learning.learner import StyleLearner
from llm.learning.message_units import build_learning_units
from llm.prompt.prompt_builder import build_batch_user_messages, build_reply_style_learning_messages, build_system_prompt
from llm.provider.deepseek_provider import OpenAICompatibleClient

ROOT = Path(__file__).resolve().parents[1]
CFG = get_llm_config()
ENTRY = next(e for e in CFG["apis"] if e.get("name") == "WechatLLM-Grok0.08-3" and e.get("model") == "grok-4.7")


def load_anonymized_context():
    paths = list((ROOT / "data" / "groups").glob("*/group_messages.json"))
    if not paths:
        raise RuntimeError("no current group export found")
    path = max(paths, key=lambda item: item.stat().st_size)
    raw = json.loads(path.read_text(encoding="utf-8"))
    name_map = {}
    records = []
    for item in raw:
        if not isinstance(item, dict) or item.get("is_bot") or item.get("role") == "assistant":
            continue
        content = str(item.get("content") or "").strip()
        if not content or content.startswith("/"):
            continue
        source = str(item.get("wxid") or item.get("sender_wxid") or item.get("nickname") or "human")
        if source not in name_map:
            name_map[source] = f"human_{len(name_map) + 1}"
        copied = {
            "message_id": f"live-human-{len(records) + 1}",
            "source_id": f"live-human-{len(records) + 1}",
            "nickname": name_map[source],
            "wxid": name_map[source],
            "speaker": name_map[source],
            "speaker_name": name_map[source],
            "content": content,
            "timestamp": item.get("timestamp") or 0,
            "role": "user",
            "is_bot": False,
            "source_kind": "user_incoming",
            "source_origin": "wechat_inbound",
            "learnable": True,
            "prompt_visible": True,
        }
        records.append(copied)
    if len(records) < 12:
        raise RuntimeError(f"only {len(records)} usable human records")
    return path.parent.name, records[-24:]


def make_service():
    service = LLMService.__new__(LLMService)
    service.config = copy.deepcopy(CFG)
    service.provider = SimpleNamespace(supports_images=False)
    service.style_learner = None
    service.long_term_memory = SimpleNamespace(enabled=False)
    service._history_cache_breakpoints = {}
    import threading
    service._history_cache_breakpoints_lock = threading.RLock()
    return service


def message_text(message):
    value = getattr(message, "content", message)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text") or item.get("content")
            if text:
                parts.append(str(text))
        return "".join(parts)
    return str(value or "")


def main():
    group_name, records = load_anonymized_context()
    client = OpenAICompatibleClient(ENTRY)
    with tempfile.TemporaryDirectory(prefix="wechat-style-live-") as folder:
        learner = StyleLearner({"learning": {"enabled": True, "db_path": str(Path(folder) / "learning.sqlite3"), "style_card_max_chars": 900}}, start_worker=False)
        observations = []
        for item in records:
            observations.append({
                "group_id": "live-anonymized-group",
                "speaker": item["speaker"],
                "speaker_name": item["speaker_name"],
                "content": item["content"],
                "timestamp": item["timestamp"],
                "message_id": item["source_id"],
                "is_bot": False,
                "source_kind": "user_incoming",
                "source_origin": "wechat_inbound",
                "learnable": True,
                "prompt_visible": True,
            })
        learner.ingest_many(observations)
        style_payload = learner.get_cycle_style_payload("live-anonymized-group")
        style_data = {
            "human_messages": records,
            "style_learning_payload": style_payload,
            "existing_style_card": style_payload.get("existing_card") if isinstance(style_payload, dict) else {},
        }
        style_system = build_system_prompt(CFG.get("prompt"), identity=CFG.get("identity"), prefixes=CFG.get("prefixes")) + "\n\nTRUSTED INTERNAL OVERRIDE: maintenance style-learning task; return only abstract style_card JSON, never a chat reply, messages, slang, expressions, or concrete examples."
        style_messages = [{"role": "system", "content": style_system}]
        style_messages += build_reply_style_learning_messages(style_data)
        style_raw = message_text(client.chat(style_messages, tools=[], thinking={}, timeout=120))
        try:
            style_result = json.loads(style_raw)
        except json.JSONDecodeError:
            style_result = {"raw": style_raw}
        card = (style_result.get("style_card") if isinstance(style_result, dict) else {}) or {}
        applied = learner.apply_style_card("live-anonymized-group", card, len(records))
        style_profile = learner.get_prompt_context("live-anonymized-group")

        service = make_service()
        prompt_records = records[-8:]
        current = dict(prompt_records[-1])
        current["directed_to_bot"] = True
        current["is_at_bot"] = True
        current["content"] = current["content"]
        reply_data = {
            "group_messages": prompt_records,
            "batch_messages": [current],
            "force_reply": True,
            "conversation_priority": "MUST_REPLY",
            "trigger_source": "prefix",
            "style_profile": style_profile,
            "identity": CFG.get("identity") or {},
            "prefixes": CFG.get("prefixes") or [],
            "llm_config": CFG,
            "slang_context": "[]",
            "expression_list": [],
            "current_message": current,
        }
        reply_messages = [{"role": "system", "content": build_system_prompt(CFG.get("prompt"), identity=CFG.get("identity"), prefixes=CFG.get("prefixes"))}]
        reply_messages += build_batch_user_messages(reply_data)
        reply_raw = message_text(client.chat(reply_messages, tools=[], thinking={}, timeout=120))
        reply_parsed = parse_llm_response(reply_raw, [])

        report = {
            "endpoint": ENTRY.get("base_url"),
            "endpoint_name": ENTRY.get("name"),
            "model": ENTRY.get("model"),
            "source_group_hash": hashlib.sha256(group_name.encode("utf-8")).hexdigest()[:16],
            "human_records_used": len(records),
            "style_card_applied_to_temporary_db": applied,
            "style_card": card,
            "normalized_style_profile": style_profile,
            "style_request_messages": style_messages,
            "style_response_raw": style_raw,
            "reply_request_messages": reply_messages,
            "reply_response_raw": reply_raw,
            "reply_response_after_delivery_normalization": reply_parsed,
            "style_usage": dict(client.last_usage),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        learner.close()
    client._client.close()


if __name__ == "__main__":
    main()
