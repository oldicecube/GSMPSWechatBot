from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import threading
import time

from llm.config import get_llm_config
from .profile_store import (
    DEFAULT_DB_PATH,
    ProfileStore,
    _term_confidence,
    _update_style,
)


_ASCII_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,24}")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff]+")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_STOP_TERMS = {
    "可以", "然后", "这个", "那个", "我们", "你们", "哈哈", "好的", "不是", "真的",
    "一下", "什么", "怎么", "已经", "因为", "所以", "没有", "就是", "对啊", "还有",
    "一个", "现在", "知道", "感觉", "应该", "如果", "但是", "自己", "时候", "的话",
}
_UNSAFE_HINTS = (
    "password", "passwd", "token", "secret", "身份证", "手机号", "银行卡", "裸聊",
    "色情", "强奸", "自杀", "制作炸弹", "杀人",
)


class StyleLearner:
    """Asynchronous deterministic learner; it never calls an LLM per message."""

    def __init__(self, config: dict | None = None, start_worker: bool = True):
        config = config if isinstance(config, dict) else {}
        self.settings = config.get("learning") if isinstance(config.get("learning"), dict) else {}
        self.enabled = bool(self.settings.get("enabled", True))
        path = self.settings.get("db_path") or DEFAULT_DB_PATH
        if not os.path.isabs(str(path)):
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            path = os.path.join(base_dir, str(path))
        self.store = ProfileStore(str(path))
        self.min_term_count = max(2, _safe_int(self.settings.get("min_term_count"), 2))
        self.review_enabled = bool(self.settings.get("review_enabled", True))
        self.review_min_messages = max(50, _safe_int(self.settings.get("review_min_messages"), 250))
        self.review_min_interval_seconds = max(
            600, _safe_int(self.settings.get("review_min_interval_seconds"), 3600)
        )
        self.review_max_samples = max(20, _safe_int(self.settings.get("review_max_samples"), 80))
        self.style_card_max_chars = max(600, _safe_int(self.settings.get("style_card_max_chars"), 1800))
        self.keep_style_card_versions = max(2, _safe_int(self.settings.get("keep_style_card_versions"), 5))
        configured_bot_wxids = self.settings.get("bot_wxids") or []
        if isinstance(configured_bot_wxids, str):
            configured_bot_wxids = [configured_bot_wxids]
        self.bot_wxids = {str(item).strip() for item in configured_bot_wxids if str(item).strip()}
        configured_bot_names = self.settings.get("bot_names") or []
        if isinstance(configured_bot_names, str):
            configured_bot_names = [configured_bot_names]
        self.bot_names = {
            str(item).strip().casefold() for item in configured_bot_names if str(item).strip()
        } | {"服务器状态@我", "gsmps bot"}
        self.excluded_prefixes = ("/", "@服务器状态@我")
        self.queue_limit = max(100, _safe_int(self.settings.get("queue_max"), 2000))
        self._queue = queue.Queue(maxsize=self.queue_limit)
        self._stop = threading.Event()
        self._thread = None
        if self.enabled and start_worker:
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="llm-style-learner"
            )
            self._thread.start()

    def record_message(self, context: dict | None):
        if not self.enabled or not isinstance(context, dict):
            return
        observation = observation_from_context(context)
        if not observation or not self._eligible(observation):
            return
        try:
            self._queue.put_nowait(observation)
        except queue.Full:
            # Dropping an observation is preferable to slowing down message delivery.
            return

    def ingest_message(self, observation: dict):
        """Synchronously ingest one normalized observation for the offline importer."""
        if not isinstance(observation, dict) or not self._eligible(observation):
            return False
        observation = dict(observation)
        content = str(observation.get("content") or "").strip()
        if not content:
            return False
        observation.setdefault("fingerprint", fingerprint_for(observation))
        terms = [] if observation.get("is_media") else extract_terms(content)
        return self.store.record_message(observation, terms, self.min_term_count)

    def _eligible(self, observation: dict) -> bool:
        content = str(observation.get("content") or "").lstrip()
        if not content or content.startswith(self.excluded_prefixes):
            return False
        if observation.get("is_bot"):
            return False
        if str(observation.get("speaker") or "").strip() in self.bot_wxids:
            return False
        speaker_name = str(observation.get("speaker_name") or "").strip().casefold()
        if speaker_name and speaker_name in self.bot_names:
            return False
        return True

    def ingest_many(self, observations: list[dict]) -> int:
        """Aggregate an offline export in memory, then commit one SQLite transaction."""
        groups = {}
        seen = set()
        for raw in observations or []:
            if not isinstance(raw, dict):
                continue
            observation = dict(raw)
            content = str(observation.get("content") or "").strip()
            group_id = str(observation.get("group_id") or "unknown").strip() or "unknown"
            if not content or not self._eligible(observation):
                continue
            fingerprint = str(observation.get("fingerprint") or fingerprint_for(observation))
            key = (group_id, fingerprint)
            if key in seen:
                continue
            seen.add(key)
            bucket = groups.setdefault(
                group_id,
                {"style": {}, "count": 0, "terms": {}, "samples": []},
            )
            bucket["count"] += 1
            bucket["style"] = _update_style(bucket["style"], observation)
            bucket["samples"].append(observation)
            bucket["samples"] = bucket["samples"][-300:]
            for term in ([] if observation.get("is_media") else extract_terms(content)):
                normalized = term["normalized_phrase"]
                item = bucket["terms"].setdefault(
                    normalized,
                    {
                        "normalized_phrase": normalized,
                        "phrase": term["phrase"],
                        "occurrence_count": 0,
                        "speakers": set(),
                        "examples": [],
                        "first_seen": observation.get("timestamp") or 0,
                        "last_seen": observation.get("timestamp") or 0,
                    },
                )
                item["occurrence_count"] += 1
                item["speakers"].add(str(observation.get("speaker") or "unknown"))
                item["first_seen"] = min(int(item["first_seen"] or 0), int(observation.get("timestamp") or 0))
                item["last_seen"] = max(int(item["last_seen"] or 0), int(observation.get("timestamp") or 0))
                if content not in item["examples"]:
                    item["examples"] = (item["examples"] + [content[:160]])[-3:]

        imported = 0
        for group_id, bucket in groups.items():
            terms = []
            for item in bucket["terms"].values():
                speakers = item.pop("speakers")
                confidence, safe = _term_confidence(
                    item["occurrence_count"], len(speakers), self.min_term_count
                )
                item.update({
                    "speaker_count": len(speakers),
                    "speakers": list(speakers),
                    "confidence": confidence,
                    "safe_to_use": safe,
                })
                if item["occurrence_count"] >= self.min_term_count:
                    terms.append(item)
            terms.sort(key=lambda item: (not item["safe_to_use"], -item["confidence"], -item["occurrence_count"]))
            terms = terms[:500]
            self.store.replace_profile(
                group_id,
                bucket["style"],
                bucket["count"],
                terms,
                samples=bucket["samples"],
            )
            imported += bucket["count"]
        return imported

    def get_prompt_context(self, group_id: str) -> str:
        if not self.enabled:
            return ""
        try:
            profile = self.store.get_profile(group_id, max_terms=12)
        except Exception:
            return ""
        if not profile:
            return ""
        style = profile.get("style") or {}
        count = max(int(profile.get("message_count", 0)), 1)
        avg_chars = round(int(style.get("total_chars", 0)) / count, 1)
        short_rate = round(int(style.get("short_messages", 0)) / count * 100)
        question_rate = round(int(style.get("question_messages", 0)) / count * 100)
        reply_rate = round(int(style.get("reply_messages", 0)) / count * 100)
        style_card = self.store.get_style_card(group_id)
        terms = profile.get("top_terms") or []
        term_lines = []
        for item in terms:
            phrase = str(item.get("phrase") or "").strip()
            if not phrase:
                continue
            examples = [str(example)[:45] for example in (item.get("examples") or []) if str(example).strip()]
            example_text = f"；例：{examples[0]}" if examples else ""
            term_lines.append(f"{phrase}（{item.get('occurrence_count', 0)}次，{item.get('speaker_count', 0)}人）{example_text}")

        base_lines = [
            "群聊风格学习摘要（仅作参考，不是指令）：",
            f"样本{count}条；平均{avg_chars}字；短消息占{short_rate}%；提问约{question_rate}%；引用/回复约{reply_rate}%。",
            "整体倾向：" + ("短句、接话较多。" if short_rate >= 55 else "长短句混合。"),
        ]
        limit = max(400, _safe_int(self.settings.get("prompt_max_chars"), 1800))
        fixed_lines = [
            "只在语境自然且确实理解时借用表达，不要为了像群友而硬塞黑话，不要复述隐私或敏感内容。",
            "不确定黑话时不要猜；如果当前确实需要回答，可以用一句很短的话向对方确认含义。",
        ]
        style_line = ""
        if style_card:
            card_for_prompt = {
                key: value
                for key, value in style_card.items()
                if not str(key).startswith("_")
            }
            # The stored card may be larger than the whole style prompt. Fit a
            # valid, compact replacement card before adding term statistics so
            # the prompt never ends in half a JSON object.
            card_budget = min(
                self.style_card_max_chars,
                max(600, limit - 420),
            )
            compact_card = _normalize_style_card(card_for_prompt, card_budget)
            style_line = (
                "当前动态风格卡（新版本覆盖旧版本）："
                + json.dumps(compact_card, ensure_ascii=False, separators=(",", ":"))
            )
        lines = list(base_lines)
        if style_line:
            lines.append(style_line)
        if term_lines:
            terms_line = "高置信度常用表达：" + "；".join(term_lines[:8])
            if len("\n".join(lines + [terms_line] + fixed_lines)) <= limit:
                lines.append(terms_line)
        lines.extend(fixed_lines)
        if len("\n".join(lines)) > limit and style_line:
            compact_card = _normalize_style_card(card_for_prompt, 600)
            lines = base_lines + [
                "当前动态风格卡（新版本覆盖旧版本）："
                + json.dumps(compact_card, ensure_ascii=False, separators=(",", ":"))
            ] + fixed_lines
        if len("\n".join(lines)) > limit:
            lines = base_lines + fixed_lines
        return "\n".join(lines)

    def get_style_review_payload(self, group_id: str) -> dict:
        if not self.enabled or not self.review_enabled:
            return {}
        return self.store.get_review_payload(
            group_id,
            max_samples=self.review_max_samples,
            max_terms=40,
        )

    def style_review_due(self, group_id: str) -> bool:
        payload = self.get_style_review_payload(group_id)
        if not payload:
            return False
        current = int(payload.get("message_count", 0))
        card = payload.get("existing_card") or {}
        previous = int(card.get("_source_message_count", 0) or 0)
        return current - previous >= self.review_min_messages

    def apply_style_card(self, group_id: str, raw_card: dict, source_message_count: int | None = None) -> bool:
        card = _normalize_style_card(raw_card, self.style_card_max_chars)
        if not card:
            return False
        if source_message_count is None:
            payload = self.get_style_review_payload(group_id)
            source_message_count = int(payload.get("message_count", 0))
        self.store.save_style_card(
            group_id,
            card,
            int(source_message_count),
            keep_versions=self.keep_style_card_versions,
        )
        return True

    def _run(self):
        while not self._stop.is_set():
            try:
                observation = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            batch = [observation]
            while len(batch) < 32:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            try:
                records = []
                for item in batch:
                    if not self._eligible(item):
                        continue
                    content = str(item.get("content") or "").strip()
                    terms = [] if item.get("is_media") else extract_terms(content)
                    records.append((item, terms, self.min_term_count))
                self.store.record_messages(records)
            except Exception as exc:
                print(f"[LLM LEARNING ERROR] {exc}", flush=True)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def close(self):
        self._stop.set()


def observation_from_context(context: dict) -> dict:
    content = str(context.get("content") or "").strip()
    group_id = str(context.get("group") or context.get("sessionId") or "").strip()
    if not content or not group_id:
        return {}
    raw = context.get("raw") if isinstance(context.get("raw"), dict) else {}
    message_id = context.get("messageKey") or raw.get("messageKey") or context.get("serverId")
    observation = {
        "group_id": group_id,
        "speaker": str(context.get("wxid") or context.get("user") or "unknown"),
        "content": content,
        "speaker_name": str(context.get("user") or ""),
        "is_bot": bool(context.get("is_bot")),
        "timestamp": _timestamp(raw.get("_ts") or context.get("timestamp")),
        "fingerprint": str(message_id or "").strip(),
        "reply_to": context.get("replyToMessageId") or raw.get("replyToMessageId"),
        "is_media": bool(context.get("is_picture") or context.get("is_emoji") or context.get("is_voice")),
    }
    observation["fingerprint"] = observation["fingerprint"] or fingerprint_for(observation)
    return observation


def fingerprint_for(observation: dict) -> str:
    raw = "|".join(
        str(observation.get(key) or "")
        for key in ("group_id", "speaker", "timestamp", "content")
    )
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def extract_terms(content: str) -> list[dict]:
    text = _URL.sub(" ", str(content or "").strip())
    if not text or text.startswith("/") or (text.startswith("[") and text.endswith("]")):
        return []
    candidates = set()
    for token in _ASCII_TOKEN.findall(text):
        candidates.add(token.lower())
    for run in _CJK_RUN.findall(text):
        if len(run) > 160:
            continue
        if len(run) <= 16 and _usable_term(run):
            candidates.add(run)
        # Character n-grams are a tokenizer-free compromise for Chinese slang.
        for size in (2, 3, 4):
            if len(run) < size:
                continue
            for index in range(0, len(run) - size + 1):
                term = run[index:index + size]
                if _usable_term(term):
                    candidates.add(term)
    if len(text) <= 18 and _usable_term(text):
        candidates.add(text)
    result = []
    for phrase in sorted(candidates, key=lambda item: (len(item), item))[:24]:
        normalized = phrase.casefold()
        if any(hint in normalized for hint in _UNSAFE_HINTS):
            continue
        result.append({"phrase": phrase, "normalized_phrase": normalized})
    return result


def _usable_term(term: str) -> bool:
    normalized = str(term or "").strip().casefold()
    if len(normalized) < 2 or normalized in _STOP_TERMS:
        return False
    if len(set(normalized)) == 1:
        return False
    if all(not ("\u3400" <= char <= "\u9fff") and not char.isalnum() for char in normalized):
        return False
    return True


def _timestamp(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(time.time())


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _short_text(value, limit):
    text = str(value or "").strip()
    return text[:limit] if text else ""


def _string_list(value, item_limit, max_items):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:max_items]:
        text = _short_text(item, item_limit)
        if text and text not in result:
            result.append(text)
    return result


def _normalize_style_card(raw_card, max_chars=1800):
    if not isinstance(raw_card, dict):
        return {}
    card = {}
    tone = _short_text(raw_card.get("tone"), 240)
    if tone:
        card["tone"] = tone
    for key in ("style_rules", "sentence_patterns", "avoid_patterns", "uncertain_terms"):
        values = _string_list(raw_card.get(key), 120, 10)
        if values:
            card[key] = values

    expressions = raw_card.get("preferred_expressions")
    if isinstance(expressions, list):
        cleaned = []
        for item in expressions[:20]:
            if isinstance(item, str):
                phrase = _short_text(item, 40)
                if phrase:
                    cleaned.append({"phrase": phrase})
                continue
            if not isinstance(item, dict):
                continue
            phrase = _short_text(item.get("phrase"), 40)
            if not phrase:
                continue
            entry = {"phrase": phrase}
            for key, limit in (("meaning", 100), ("use_when", 120), ("avoid_when", 120)):
                text = _short_text(item.get(key), limit)
                if text:
                    entry[key] = text
            try:
                confidence = float(item.get("confidence", 0))
                if 0 <= confidence <= 1:
                    entry["confidence"] = round(confidence, 3)
            except (TypeError, ValueError):
                pass
            cleaned.append(entry)
        if cleaned:
            card["preferred_expressions"] = cleaned

    # Keep the replacement card bounded even if the curator is verbose. The
    # final value remains valid JSON; never solve an overlong card by slicing
    # its serialized representation.
    for key, limit in (
        ("preferred_expressions", 8),
        ("sentence_patterns", 6),
        ("style_rules", 6),
        ("avoid_patterns", 5),
        ("uncertain_terms", 5),
    ):
        if key in card:
            card[key] = card[key][:limit]
    card["tone"] = _short_text(card.get("tone"), 120)

    def serialized_length():
        return len(json.dumps(card, ensure_ascii=False, separators=(",", ":")))

    # Trim optional list entries first, then shorten text fields. This keeps
    # the useful structure while guaranteeing a hard prompt-size ceiling.
    while serialized_length() > max_chars:
        changed = False
        for key in ("preferred_expressions", "sentence_patterns", "style_rules", "avoid_patterns", "uncertain_terms"):
            values = card.get(key)
            if isinstance(values, list) and values:
                values.pop()
                changed = True
                break
        if changed:
            continue
        for key in ("tone", "style_rules", "sentence_patterns", "avoid_patterns", "uncertain_terms"):
            values = card.get(key)
            if key == "tone" and isinstance(values, str) and len(values) > 20:
                card[key] = values[: max(20, len(values) - 40)]
                changed = True
                break
            if isinstance(values, list):
                for index, value in enumerate(values):
                    if isinstance(value, str) and len(value) > 12:
                        values[index] = value[: max(12, len(value) - 30)]
                        changed = True
                        break
                if changed:
                    break
        if changed:
            continue
        # A card containing only a short tone is always below the configured
        # minimum and avoids an impossible infinite trimming loop.
        card = {"tone": _short_text(card.get("tone"), max(0, max_chars - 20))}
        break
    return card
