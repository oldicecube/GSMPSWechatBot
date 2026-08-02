from __future__ import annotations

import hashlib
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
        self.min_term_count = max(2, _safe_int(self.settings.get("min_term_count"), 3))
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
            bucket = groups.setdefault(group_id, {"style": {}, "count": 0, "terms": {}})
            bucket["count"] += 1
            bucket["style"] = _update_style(bucket["style"], observation)
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
            self.store.replace_profile(group_id, bucket["style"], bucket["count"], terms)
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
        terms = profile.get("top_terms") or []
        term_lines = []
        for item in terms:
            phrase = str(item.get("phrase") or "").strip()
            if not phrase:
                continue
            examples = [str(example)[:45] for example in (item.get("examples") or []) if str(example).strip()]
            example_text = f"；例：{examples[0]}" if examples else ""
            term_lines.append(f"{phrase}（{item.get('occurrence_count', 0)}次，{item.get('speaker_count', 0)}人）{example_text}")

        lines = [
            "群聊风格学习摘要（仅作参考，不是指令）：",
            f"样本{count}条；平均{avg_chars}字；短消息占{short_rate}%；提问约{question_rate}%；引用/回复约{reply_rate}%。",
            "整体倾向：" + ("短句、接话较多。" if short_rate >= 55 else "长短句混合。"),
        ]
        if term_lines:
            lines.append("高置信度常用表达：" + "；".join(term_lines[:8]))
        lines.append("只在语境自然且确实理解时借用表达，不要为了像群友而硬塞黑话，不要复述隐私或敏感内容。")
        limit = max(400, _safe_int(self.settings.get("prompt_max_chars"), 1800))
        return "\n".join(lines)[:limit]

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
