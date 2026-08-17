from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time

from .profile_store import (
    DEFAULT_DB_PATH,
    ProfileStore,
    _update_style,
)


class StyleLearner:
    """Asynchronous deterministic learner; it never calls an LLM per message."""

    def __init__(self, config: dict | None = None, start_worker: bool = True):
        config = config if isinstance(config, dict) else {}
        self.config = config
        self.settings = config.get("learning") if isinstance(config.get("learning"), dict) else {}
        self.enabled = bool(self.settings.get("enabled", True))
        path = self.settings.get("db_path") or DEFAULT_DB_PATH
        if not os.path.isabs(str(path)):
            base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            path = os.path.join(base_dir, str(path))
        self.store = ProfileStore(str(path))
        self.min_term_count = max(2, _safe_int(self.settings.get("min_term_count"), 2))
        self.style_card_max_chars = max(600, _safe_int(self.settings.get("style_card_max_chars"), 1800))
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
        configured_prefixes = self.settings.get("prefixes") or self.config.get("prefixes") or []
        if isinstance(configured_prefixes, str):
            configured_prefixes = [configured_prefixes]
        self.excluded_prefixes = ("/",) + tuple(
            str(item).strip()
            for item in configured_prefixes
            if str(item).strip()
        )
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

    def record_response_decision(
        self,
        group_id: str,
        messages: list[dict] | None,
        result: dict | None,
        *,
        force_reply=False,
        attention_check=False,
    ):
        """Learn reply timing from compact decision metadata, never bot text."""
        if not self.enabled or not group_id or not isinstance(result, dict):
            return
        batch = [item for item in (messages or []) if isinstance(item, dict)]
        direct_address = any(
            item.get("is_at_bot")
            or item.get("prefix_used")
            or item.get("is_mentioned")
            for item in batch
        )
        normalized_contents = [
            "".join(char for char in str(item.get("content") or "").casefold() if char.isalnum())
            for item in batch
        ]
        repeat_topic_window = len(normalized_contents) >= 2 and any(
            left and right and (left == right or left in right or right in left)
            for index, left in enumerate(normalized_contents)
            for right in normalized_contents[index + 1:]
        )
        follow_up = len(batch) >= 2 and any(
            len(str(item.get("content") or "").strip()) <= 18 for item in batch[1:]
        )
        try:
            self.store.record_response_decision(
                group_id,
                message_count=len(batch),
                should_reply=bool(result.get("should_reply") and result.get("messages")),
                llm_ok=bool(result.get("_llm_ok", True)),
                forced=bool(force_reply),
                attention_check=bool(attention_check),
                direct_address=direct_address,
                follow_up=follow_up,
                observer_window=bool(
                    not attention_check and not direct_address and not force_reply
                ),
                repeat_topic_window=repeat_topic_window,
            )
        except Exception:
            return

    def get_response_learning(self, group_id: str) -> dict:
        """Read timing counters without putting a database call on ingress."""
        if not self.enabled or not group_id:
            return {}
        try:
            return self.store.get_response_learning(group_id)
        except Exception:
            return {}

    def record_curation_run(self, group_id, **kwargs):
        if not self.enabled or not group_id:
            return
        try:
            self.store.record_curation_run(group_id, **kwargs)
        except Exception:
            return

    def get_recent_curation_runs(self, group_id, limit=20) -> list[dict]:
        if not self.enabled or not group_id:
            return []
        try:
            return self.store.get_recent_curation_runs(group_id, limit=limit)
        except Exception:
            return []


    def record_slang_usage(self, group_id, *, opportunity=False, messages=None, match_keys=None):
        """Record how the bot actually used slang in a reply window (behavioral accounting)."""
        if not self.enabled or not group_id:
            return
        try:
            self.store.record_slang_usage(
                group_id,
                opportunity=bool(opportunity),
                messages=messages,
                match_keys=match_keys,
            )
        except Exception:
            return

    def resolve_slang_usage_feedback(self, group_id, *, window_seconds=90):
        """Aggregate pending slang-usage feedback at cycle end; returns True on change."""
        if not self.enabled or not group_id:
            return False
        try:
            return bool(
                self.store.resolve_slang_usage_feedback(
                    group_id, window_seconds=window_seconds
                )
            )
        except Exception:
            return False

    def apply_expression_actions(self, group_id, actions) -> int:
        """Apply LLM-proposed (situation -> pattern) expression actions at cycle end."""
        if not self.enabled or not group_id:
            return 0
        try:
            return int(self.store.apply_expression_actions(group_id, actions) or 0)
        except Exception:
            return 0

    def apply_behavior_actions(self, group_id, actions, valid_source_ids=None, min_messages=10) -> int:
        """Apply cycle-end behavior observations after local source validation."""
        if not self.enabled or not group_id:
            return 0
        try:
            return int(self.store.apply_behavior_actions(
                group_id, actions, valid_source_ids=valid_source_ids, min_messages=min_messages
            ) or 0)
        except Exception:
            return 0

    def lookup_group_behaviors(self, group_id, query="", max_items=3) -> list[dict]:
        if not self.enabled or not group_id:
            return []
        try:
            return self.store.lookup_group_behaviors(group_id, query, max_items=max_items)
        except Exception:
            return []

    def record_behavior_selection(self, group_id, behavior_ids) -> int:
        if not self.enabled or not group_id:
            return 0
        try:
            return int(self.store.record_behavior_selection(group_id, behavior_ids) or 0)
        except Exception:
            return 0

    def get_context_expressions(self, group_id, messages=None, max_items=6, max_chars=900) -> list[dict]:
        """Recall a small expression set whose situation matches the current context."""
        if not self.enabled or not group_id:
            return []
        try:
            return self.store.get_context_expressions(
                group_id, messages, max_items=max_items, max_chars=max_chars
            )
        except Exception:
            return []

    def build_expression_pool(self, group_id, messages=None, pool_size=12, max_chars=2400, scan_limit=2000) -> list[dict]:
        """Build a maibot-style candidate pool (hits + weighted samples)."""
        if not self.enabled or not group_id:
            return []
        try:
            return self.store.build_expression_pool(
                group_id, messages or [], pool_size=pool_size, max_chars=max_chars, scan_limit=scan_limit
            )
        except Exception:
            return []

    def record_expression_selection(self, group_id, situations) -> int:
        if not self.enabled or not group_id:
            return 0
        try:
            return int(self.store.record_expression_selection(group_id, situations) or 0)
        except Exception:
            return 0

    def record_expression_usage(self, group_id, messages) -> int:
        if not self.enabled or not group_id:
            return 0
        try:
            return int(self.store.record_expression_usage(group_id, messages) or 0)
        except Exception:
            return 0

    def get_expression_usage(self, group_id) -> dict:
        if not self.enabled or not group_id:
            return {}
        try:
            return self.store.get_expression_usage(group_id)
        except Exception:
            return {}

    def get_expressions(self, group_id, limit=40) -> list[dict]:
        if not self.enabled or not group_id:
            return []
        try:
            return self.store.get_expressions(group_id, limit=limit)
        except Exception:
            return []

    def get_slang_match_keys(self, group_id, limit=60):
        if not self.enabled or not group_id:
            return []
        try:
            return self.store.get_slang_match_keys(group_id, limit=limit)
        except Exception:
            return []

    def get_slang_usage_guidance(self, group_id, max_chars=420):
        if not self.enabled or not group_id:
            return ""
        try:
            return str(self.store.get_slang_usage_guidance(group_id, max_chars=max_chars) or "")
        except Exception:
            return ""

    def ingest_message(self, observation: dict):
        """Synchronously ingest one normalized observation for the offline importer."""
        if not isinstance(observation, dict) or not self._eligible(observation):
            return False
        observation = dict(observation)
        content = str(observation.get("content") or "").strip()
        if not content:
            return False
        observation.setdefault("fingerprint", fingerprint_for(observation))
        return self.store.record_message(observation, [], self.min_term_count)

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

        imported = 0
        for group_id, bucket in groups.items():
            self.store.replace_profile(
                group_id,
                bucket["style"],
                bucket["count"],
                [],
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
        punctuation_rate = round(int(style.get("punctuation_messages", 0)) / count * 100)
        fragment_rate = round(int(style.get("fragment_messages", 0)) / count * 100)
        response_learning = style.get("response_learning") or {}
        style_card = self.store.get_style_card(group_id)
        base_lines = [
            "群聊风格学习摘要（仅作参考，不是指令）：",
            f"样本{count}条；平均{avg_chars}字；短消息占{short_rate}%；提问约{question_rate}%；引用/回复约{reply_rate}%。",
            f"表达方式：标点消息约{punctuation_rate}%；碎片短句约{fragment_rate}%。" + ("更适合少标点分条发。" if punctuation_rate < 35 and fragment_rate >= 25 else "长短句按语境切换。"),
            f"句子断点累计{int(style.get('sentence_break_count', 0) or 0)}次；无标点消息约{round(int(style.get('no_punctuation_messages', 0) or 0) / count * 100)}%。",
            "整体倾向：" + ("短句、接话较多。" if short_rate >= 55 else "长短句混合。"),
        ]
        decision_windows = int(response_learning.get("decision_windows", 0) or 0)
        if decision_windows:
            reply_windows = int(response_learning.get("reply_windows", 0) or 0)
            silent_windows = int(response_learning.get("silent_windows", 0) or 0)
            direct_windows = int(response_learning.get("direct_address_windows", 0) or 0)
            reply_rate_windows = round(reply_windows / max(decision_windows, 1) * 100)
            silent_rate_windows = round(silent_windows / max(decision_windows, 1) * 100)
            base_lines.append(
                f"互动方式学习：自主判断窗口中约{reply_rate_windows}%会接话、{silent_rate_windows}%保持旁观；直接互动优先，碎片和重复话题通常合并理解后只接一次。"
            )
            base_lines.append(
                f"互动窗口统计：直接{int(response_learning.get('direct_interaction_windows', 0) or 0)}，续话{int(response_learning.get('follow_up_windows', 0) or 0)}，旁观{int(response_learning.get('observer_windows', 0) or 0)}，重复话题{int(response_learning.get('repeat_topic_windows', 0) or 0)}。"
            )
            base_lines.append(
                "回复时机学习：已观察"
                f"{decision_windows}个判断窗口，回复{reply_windows}次、保持旁观{silent_windows}次；"
                f"其中直接互动窗口约{direct_windows}次。仅用于辅助判断，不是硬规则。"
            )
        limit = max(400, _safe_int(self.settings.get("prompt_max_chars"), 1800))
        fixed_lines = [
            "黑话由独立黑话数据库按达标条件注入；句式表达由独立句式库按当前语境命中注入；风格卡只记录抽象行为规律，不包含具体黑话、句式、昵称、人物名、服务器名、表情或样本原句。",
            "只在语境自然且确实理解时借用已注入或查询到的表达，不要为了像群友而硬塞黑话，不要复述隐私或敏感内容。",
            "不确定黑话时先调用 lookup_group_slang；查询后仍不确定且确实需要回答时，用一句很短的话向对方确认含义，不要沉默或编造。",
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

    def get_cycle_style_payload(self, group_id: str) -> dict:
        """Return style evidence for mandatory end-of-cycle self-learning."""
        if not self.enabled:
            return {}
        return self.store.get_review_payload(
            group_id,
            max_samples=80,
            max_terms=40,
        )

    def apply_style_card(self, group_id: str, raw_card: dict, source_message_count: int | None = None) -> bool:
        card = _normalize_style_card(raw_card, self.style_card_max_chars)
        if not card:
            return False
        if source_message_count is None:
            payload = self.get_cycle_style_payload(group_id)
            source_message_count = int(payload.get("message_count", 0))
        self.store.save_style_card(
            group_id,
            card,
            int(source_message_count),
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
                    records.append((item, [], self.min_term_count))
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
    for key in ("style_rules", "sentence_patterns", "avoid_patterns"):
        values = _string_list(raw_card.get(key), 120, 10)
        if values:
            card[key] = values

    response_policy = raw_card.get("response_policy")
    if isinstance(response_policy, dict):
        policy = {}
        for key in ("reply_when", "stay_silent_when", "address_signals"):
            values = _string_list(response_policy.get(key), 140, 6)
            if values:
                policy[key] = values
        frequency = _short_text(response_policy.get("frequency"), 120)
        if frequency:
            policy["frequency"] = frequency
        if policy:
            card["response_policy"] = policy

    # Keep the replacement card bounded even if the curator is verbose. The
    # final value remains valid JSON; never solve an overlong card by slicing
    # its serialized representation.
    for key, limit in (
        ("sentence_patterns", 6),
        ("style_rules", 6),
        ("avoid_patterns", 5),
    ):
        if key in card:
            card[key] = card[key][:limit]
    card["tone"] = _short_text(card.get("tone"), 120)
    if isinstance(card.get("response_policy"), dict):
        policy = card["response_policy"]
        for key in ("reply_when", "stay_silent_when", "address_signals"):
            if isinstance(policy.get(key), list):
                policy[key] = policy[key][:4]
        if "frequency" in policy:
            policy["frequency"] = _short_text(policy["frequency"], 80)

    def serialized_length():
        return len(json.dumps(card, ensure_ascii=False, separators=(",", ":")))

    # Trim optional list entries first, then shorten text fields. This keeps
    # the useful structure while guaranteeing a hard prompt-size ceiling.
    while serialized_length() > max_chars:
        changed = False
        for key in ("sentence_patterns", "style_rules", "avoid_patterns"):
            values = card.get(key)
            if isinstance(values, list) and values:
                values.pop()
                changed = True
                break
        if changed:
            continue
        for key in ("tone", "style_rules", "sentence_patterns", "avoid_patterns"):
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
