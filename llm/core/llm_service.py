from llm.config import get_llm_config
import difflib
import hashlib
import json
import re
import threading
import time

from llm.core.response_parser import (
    load_json_lenient,
    build_balance_error_response,
    FALLBACK_RESPONSE,
    build_error_response,
    is_insufficient_balance_error,
    parse_proactive_response,
    parse_llm_response,
)
from llm.memory import LongTermMemory, MemoryManager
from llm.learning import StyleLearner
from llm.learning.profile_store import _is_generic_slang_phrase
from llm.conversation_pulse import ConversationPulse
from llm.proactive_reply import ProactiveReplyManager
from llm.prompt import (
    build_batch_user_messages,
    build_context_compression_messages,
    build_memory_curation_messages,
    build_system_prompt,
    build_user_messages,
)
from llm.provider import DeepSeekProvider
from llm.provider.deepseek_provider import ProviderShuttingDown
from llm.security import build_emoji_index, get_emoji_list
from llm.web_tools import (
    AUTO_REPLY_URL_HOSTS,
    EXPRESSION_LOOKUP_TOOL,
    MEMORY_LOOKUP_TOOL,
    SLANG_LOOKUP_TOOL,
    SLANG_SIMILAR_LOOKUP_TOOL,
    ORIGINAL_MESSAGE_TOOL,
    BEHAVIOR_LOOKUP_TOOL,
    TIABA_HOT_TOOL,
    WEB_FETCH_TOOL,
    execute_tool,
)


class LLMService:
    def __init__(self):
        try:
            self.config = get_llm_config()
        except Exception:
            self.config = {"enabled": False}

        try:
            self.memory_manager = MemoryManager()
        except Exception:
            self.memory_manager = None
        try:
            self.long_term_memory = LongTermMemory(self.config)
        except Exception as exc:
            print(f"[LLM MEMORY INIT ERROR] {exc}", flush=True)
            self.long_term_memory = None

        self.provider = None
        self._memory_curator_lock = threading.Lock()
        self._context_compression_lock = threading.Lock()
        self._memory_curator_backoff_until = 0
        self._cycle_context_cache = {}
        self._cycle_context_cache_lock = threading.RLock()
        self._proactive_context_cache = {}
        self._balance_warning_sent = False
        self.assistant_nickname = self.config.get("assistant_nickname", "LLM")
        self.emoji_list = []

        try:
            build_emoji_index(self.config.get("emoji_dir"))
            self.emoji_list = get_emoji_list()
        except Exception:
            self.emoji_list = []

        self.proactive_reply = ProactiveReplyManager(self.config)
        self.conversation_pulse = ConversationPulse(self.config.get("auto_reply") or {})
        try:
            learning_config = dict(self.config)
            learning_config["learning"] = dict(self.config.get("learning") or {})
            learning_config["learning"]["bot_wxids"] = self.config.get("bot_wxids") or []
            learning_config["learning"]["prefixes"] = self.config.get("prefixes") or []
            self.style_learner = StyleLearner(learning_config, start_worker=False)
        except Exception as exc:
            print(f"[LLM LEARNING INIT ERROR] {exc}", flush=True)
            self.style_learner = None
        if self.style_learner:
            self.proactive_reply.set_reply_timing_reader(
                self.style_learner.get_response_learning
            )

    def _get_memory_context(self, group_id, wxid, query):
        if self.long_term_memory is None:
            return ""
        try:
            return self.long_term_memory.get_context(
                group_id,
                wxid,
                query,
                max_chars=(self.config.get("memory") or {}).get("context_max_chars", 0),
            )
        except Exception:
            return ""

    def _get_person_profile_context(self, group_id, wxid, query=""):
        if not self.long_term_memory or not self.long_term_memory.enabled:
            return ""
        try:
            return self.long_term_memory.get_person_profile_context(group_id, wxid, query)
        except Exception:
            return ""

    def _get_group_memory_state(self, group_id):
        if not self.long_term_memory or not self.long_term_memory.enabled:
            return {"short_memory": "", "medium_memory": "", "long_memory": ""}
        try:
            state = self.long_term_memory.get_group_memory_state(group_id)
            # Short memory is for facts/topics, not a second slang source.
            # Keep historical storage intact, but never feed known slang back
            # through this channel; reply/cycle slang comes from the dedicated
            # slang library and the current cycle evidence only.
            short_memory = str(state.get("short_memory") or "")
            if short_memory and self.style_learner:
                phrases = self._known_slang_phrases(group_id)
                if phrases:
                    state = dict(state)
                    state["short_memory"] = self._strip_phrases(short_memory, phrases)
            return state
        except Exception:
            return {"short_memory": "", "medium_memory": "", "long_memory": ""}

    def _known_slang_phrases(self, group_id):
        if not self.style_learner:
            return []
        try:
            rows = self.style_learner.store.lookup_slang(group_id, "", 200)
        except Exception:
            return []
        return sorted({
            str(item.get("phrase") or "").strip()
            for item in rows if isinstance(item, dict) and len(str(item.get("phrase") or "").strip()) >= 2
        }, key=len, reverse=True)

    @staticmethod
    def _strip_phrases(text, phrases):
        result = str(text or "")
        for phrase in phrases or []:
            if phrase:
                result = re.sub(re.escape(str(phrase)), "", result, flags=re.IGNORECASE)
        return re.sub(r"[ \t]{2,}", " ", result).strip()

    @staticmethod
    def _slang_evidence_key(value):
        return "".join(char for char in str(value or "").casefold() if char.isalnum())

    @staticmethod
    def _extract_local_slang_candidates(cycle_messages, known_phrases=None, max_candidates=30, max_messages=150):
        """Find frequent phrase candidates from this cycle's non-bot messages.

        Candidate n-grams (2-6 chars) must appear in at least 2 distinct
        messages or be spoken by at least 2 distinct speakers. Longest-first
        dedup drops shorter substrings of an accepted candidate, so "吓哭了"
        suppresses fake fragments like "吓哭"/"哭了". These are review-only
        suggestions: the LLM still decides whether they enter the library.
        """
        if not cycle_messages:
            return []
        # Bound cost: only the most recent messages matter for slang evidence.
        if len(cycle_messages) > max(1, int(max_messages or 1)):
            cycle_messages = list(cycle_messages)[-max(1, int(max_messages or 1)):]
        known = set()
        for phrase in known_phrases or []:
            key = LLMService._slang_evidence_key(phrase)
            if len(key) >= 2:
                known.add(key)
        records = []
        seen_messages = set()
        for index, item in enumerate(cycle_messages):
            if not isinstance(item, dict) or item.get("is_bot") or str(item.get("role") or "") == "assistant":
                continue
            source_id = str(item.get("source_id") or "").strip() or f"_m{index}"
            if source_id in seen_messages:
                continue
            content = str(item.get("content") or "").strip()
            if (
                not content
                or content.startswith("/")
                or re.search(r"https?://", content, flags=re.IGNORECASE)
                or content.isdigit()
            ):
                continue
            normalized = LLMService._slang_evidence_key(content)
            if len(normalized) < 2:
                continue
            speaker = str(
                item.get("wxid") or item.get("sender_id") or item.get("sender")
                or item.get("speaker") or item.get("nickname") or ""
            ).strip()[:120]
            records.append((source_id, normalized, speaker))
            seen_messages.add(source_id)
        if len(records) < 2:
            return []
        counts = {}
        for source_id, normalized, speaker in records:
            text_len = len(normalized)
            for size in range(2, min(6, text_len) + 1):
                for start in range(0, text_len - size + 1):
                    gram = normalized[start:start + size]
                    if gram.isdigit() or len(set(gram)) == 1:
                        continue
                    entry = counts.setdefault(gram, {"messages": set(), "speakers": set()})
                    entry["messages"].add(source_id)
                    if speaker:
                        entry["speakers"].add(speaker)

        def _usable(gram):
            if _is_generic_slang_phrase(gram):
                return False
            if gram in known:
                return False
            if any(len(k) > len(gram) and gram in k for k in known):
                return False
            return True

        passing = [
            gram for gram, entry in counts.items()
            if (len(entry["messages"]) >= 2 or len(entry["speakers"]) >= 2) and _usable(gram)
        ]
        passing.sort(key=lambda gram: (-len(gram), gram))
        accepted = []
        for gram in passing:
            if any(gram in longer for longer in accepted):
                continue
            accepted.append(gram)
            if len(accepted) >= max(1, int(max_candidates or 1)):
                break
        return [
            {
                "phrase": gram,
                "normalized_phrase": gram,
                "message_count": len(counts[gram]["messages"]),
                "speaker_count": len(counts[gram]["speakers"]),
            }
            for gram in accepted
        ]

    @staticmethod
    def _block_fragment_slang_indices(grounded_actions, known_phrases=None):
        """Return indices of grounded slang writes that are fragments of another.

        When the LLM proposes both a longer phrase and one of its substrings in
        the same cycle (e.g. "吓哭了" plus "吓哭" / "哭了"), the longest phrase
        wins and shorter fragments are rejected. Established slang already in
        the library is never suppressed; keep/delete are never fragments.
        """
        if not grounded_actions:
            return set()
        known = {LLMService._slang_evidence_key(p) for p in known_phrases or [] if p}
        new_writes = []
        for index, action in enumerate(grounded_actions):
            if not isinstance(action, dict):
                continue
            operation = str(action.get("action") or "").lower()
            if operation not in {"add", "update"}:
                continue
            normalized = str(action.get("normalized_phrase") or action.get("phrase") or "").strip().casefold()
            key = LLMService._slang_evidence_key(normalized)
            if not key or key in known:
                continue
            new_writes.append((index, key))
        new_writes.sort(key=lambda item: (-len(item[1]), item[1]))
        blocked = set()
        longest_keys = []
        for index, key in new_writes:
            if any(key in longer_key for longer_key in longest_keys):
                blocked.add(index)
                continue
            longest_keys.append(key)
        return blocked

    def _ground_slang_action_in_cycle(self, action, cycle_messages):
        """Accept slang writes only when their phrase appears in current user context."""
        if not isinstance(action, dict):
            return None, "invalid_action"
        phrase = str(action.get("phrase") or action.get("normalized_phrase") or "").strip()
        phrase_key = self._slang_evidence_key(phrase)
        if len(phrase_key) < 2:
            return None, "missing_phrase"
        sources = {}
        matches = []
        for item in cycle_messages or []:
            if not isinstance(item, dict) or item.get("is_bot") or str(item.get("role") or "") == "assistant":
                continue
            source_id = str(item.get("source_id") or "").strip()
            content = str(item.get("content") or "").strip()
            if not source_id or not content:
                continue
            sources[source_id] = {
                "content": content,
                "speaker": str(
                    item.get("wxid") or item.get("sender_id") or item.get("sender")
                    or item.get("speaker") or item.get("nickname") or ""
                ).strip()[:120],
            }
            if phrase_key in self._slang_evidence_key(content):
                matches.append(source_id)
        requested = action.get("source_ids")
        if isinstance(requested, str):
            requested = [requested]
        requested = list(dict.fromkeys(
            str(item).strip() for item in (requested or []) if str(item).strip()
        ))[:8]
        if requested:
            requested = [item for item in requested if item in sources]
            source_ids = [item for item in requested if item in matches]
            if not source_ids:
                if matches:
                    # Cited source ids are invalid or do not contain the exact
                    # phrase. Fall back to locally verified occurrences so a
                    # grounded phrase is not dropped just because provenance
                    # ids drifted; the phrase still must appear in this cycle.
                    print(
                        f"[SLANG GROUND FALLBACK] phrase={phrase} requested={requested} "
                        f"local_matches={len(matches)}",
                        flush=True,
                    )
                    source_ids = matches[:3]
                else:
                    return None, (
                        "unknown_source" if not requested
                        else "source_does_not_contain_phrase"
                    )
        elif matches:
            # Backward-compatible local grounding for an otherwise valid
            # response from a model that did not yet emit source_ids.
            source_ids = matches[:3]
        else:
            return None, "phrase_not_in_cycle_context"
        source_speakers = list(dict.fromkeys(
            sources[source_id]["speaker"]
            for source_id in source_ids
            if sources.get(source_id, {}).get("speaker")
        ))[:20]
        return dict(action, phrase=phrase, source_ids=source_ids, source_speakers=source_speakers), "ok"

    def _remove_slang_from_short_memory_actions(self, actions, phrases):
        cleaned = []
        removed = 0
        for action in actions or []:
            if not isinstance(action, dict):
                continue
            item = dict(action)
            memory_type = str(item.get("memory_type") or item.get("kind") or "").strip().lower()
            operation = str(item.get("action") or "").strip().lower()
            if memory_type != "short" or operation not in {"append", "add", "replace", "update", "set"}:
                cleaned.append(item)
                continue
            original = str(item.get("content") or "")
            filtered = self._strip_phrases(original, phrases)
            if filtered != original:
                removed += 1
            if not filtered:
                continue
            item["content"] = filtered
            cleaned.append(item)
        return cleaned, removed

    def _get_cycle_context(self, group_id, cycle_id, wxid, query):
        key = str(group_id or "unknown")
        cycle_id = str(cycle_id or "direct")
        if cycle_id != "direct":
            with self._cycle_context_cache_lock:
                cached = self._cycle_context_cache.get(key)
                if cached and cached.get("cycle_id") == cycle_id:
                    return dict(cached)
        context = {
            "cycle_id": cycle_id,
            # Select slang from the current context for each request. The full
            # qualified table must not become a cached prompt prefix.
            "slang_context": "[]",
            "slang_taxonomy_context": self._get_slang_taxonomy_context(group_id),
            "person_profile_context": self._get_person_profile_context(group_id, wxid, query),
            "extra_memory_context": self._get_memory_context(group_id, wxid, query),
        }
        if cycle_id != "direct":
            with self._cycle_context_cache_lock:
                self._cycle_context_cache[key] = dict(context)
        return context

    def _get_context_slang_context(self, group_id, messages):
        if not self.style_learner:
            return "[]"
        try:
            candidates = self.style_learner.store.get_context_slang(
                group_id,
                messages,
                max_items=8,
                max_chars=1200,
            )
        except Exception:
            candidates = []
        return json.dumps(candidates, ensure_ascii=False, separators=(",", ":"))

    def _fit_group_messages_for_prompt(self, messages):
        """Bound only the prompt copy of history; never discard stored records."""
        items = [item for item in (messages or []) if isinstance(item, dict)]
        if not items:
            return []
        try:
            context_limit = int(self.config.get("context_window_tokens", 200000) or 200000)
        except (TypeError, ValueError):
            context_limit = 200000
        try:
            compression_target = int(
                self.config.get("context_compression_target_tokens", 160000) or 160000
            )
        except (TypeError, ValueError):
            compression_target = 160000
        # Leave room for the system prompt, dynamic memory/style sections,
        # tool schemas, tool results, and the completion itself.
        budget = max(5000, min(compression_target, context_limit - 16000))

        def item_tokens(item):
            return MemoryManager.estimate_tokens(str(item.get("content") or ""))

        if sum(item_tokens(item) for item in items) <= budget:
            return list(items)

        summary_indexes = {
            index for index, item in enumerate(items)
            if str(item.get("role") or "") == "context_summary"
        }
        selected = []
        used = 0
        for index in sorted(summary_indexes):
            item = items[index]
            cost = item_tokens(item)
            if used + cost <= budget:
                selected.append((index, item))
                used += cost

        for index in range(len(items) - 1, -1, -1):
            if index in summary_indexes:
                continue
            item = items[index]
            cost = item_tokens(item)
            if used + cost <= budget:
                selected.append((index, item))
                used += cost
                continue
            if not selected or index == len(items) - 1:
                remaining = max(1, budget - used)
                content = str(item.get("content") or "")
                if content:
                    low, high = 0, len(content)
                    while low < high:
                        middle = (low + high + 1) // 2
                        if MemoryManager.estimate_tokens(content[:middle]) <= remaining:
                            low = middle
                        else:
                            high = middle - 1
                    clipped = dict(item)
                    clipped["content"] = content[:low].rstrip() + " …[截断]"
                    selected.append((index, clipped))
                break

        selected.sort(key=lambda pair: pair[0])
        result = [item for _, item in selected]
        print(
            f"[LLM CONTEXT FIT] before={len(items)} after={len(result)} "
            f"budget_tokens={budget}",
            flush=True,
        )
        return result

    def _get_context_expressions(self, group_id, messages, max_items=None) -> list:
        """Build a maibot-style expression pool and (optionally) have a light
        selector LLM pick the few that best fit the current context."""
        if not self.style_learner:
            return []
        try:
            learning = self.config.get("learning") or {}
            pool_size = int(learning.get("expression_pool_size", 12) or 12)
            scan_limit = int(learning.get("expression_recall_scan_limit", 2000) or 2000)
            pool = self.style_learner.build_expression_pool(
                group_id,
                messages or [],
                pool_size=pool_size,
                max_chars=2400,
                scan_limit=scan_limit,
            )
        except Exception:
            pool = []
        if not pool:
            return []
        inject_limit = max_items or int(
            (self.config.get("learning") or {}).get("expression_selector_max_items", 4) or 4
        )
        selector_enabled = bool((self.config.get("learning") or {}).get("expression_selector_enabled", True))
        if selector_enabled and len(pool) > inject_limit:
            selected = self._select_expression_candidates(group_id, messages or [], pool, inject_limit)
            if selected is not None:
                return selected
        return pool[:inject_limit]

    def _select_expression_candidates(self, group_id, messages, pool, max_items=4):
        """maibot-style lightweight selector: pick the few expressions that best
        fit the current context. Returns None on any failure so callers fall
        back to the plain top-k pool."""
        if not pool or not max_items:
            return None
        try:
            learning = self.config.get("learning") or {}
            max_chars = int(learning.get("expression_selector_max_chars", 1400) or 1400)
            context_lines = []
            for item in (messages or [])[-15:]:
                if not isinstance(item, dict) or item.get("is_bot") or item.get("role") == "assistant":
                    continue
                speaker = str(item.get("nickname") or item.get("speaker_name") or "群友")
                text = str(item.get("content") or "").strip()
                if text:
                    context_lines.append(f"{speaker}: {text}")
            context_text = "\n".join(context_lines)[-max_chars:]
            candidate_lines = []
            for index, item in enumerate(pool[:24]):
                situation = str(item.get("situation") or "").strip()
                pattern = str(item.get("pattern") or "").strip()
                candidate_lines.append(
                    f"{index}: 情景={situation} | 表达={pattern} | 频次={int(item.get('count') or 0)}"
                )
            if not candidate_lines:
                return None
            prompt = (
                "请分析下面聊天语境的情绪、话题类型和表达匹配度，从候选句式表达中选出最适合当前语境的，"
                f"最多 {max_items} 个。\n\n聊天语境：\n{context_text}\n\n候选句式表达：\n"
                + "\n".join(candidate_lines)
                + '\n\n只输出 JSON：{"selected_ids":[...]}，不要输出其他内容。'
            )
            if self.provider is None:
                self._ensure_provider()
            response = self.provider.send([
                {"role": "system", "content": "你是句式选择器，只做筛选，不生成聊天回复。"},
                {"role": "user", "content": prompt},
            ])
            data = load_json_lenient(response)
            ids = data.get("selected_ids") if isinstance(data, dict) else None
            if not isinstance(ids, list):
                return None
            selected = []
            seen = set()
            for value in ids:
                if not str(value).isdigit():
                    continue
                index = int(value)
                if index < 0 or index >= len(pool) or index in seen:
                    continue
                seen.add(index)
                selected.append(pool[index])
                if len(selected) >= max_items:
                    break
            return selected
        except Exception:
            return None

    def _evaluate_expression_actions(self, group_id, actions):
        """Optional LLM suitability filter for add/update expression actions
        (maibot expression_evaluation style). Returns None on failure so the
        caller keeps the original actions."""
        candidates = [
            item for item in (actions or [])
            if isinstance(item, dict) and str(item.get("action") or "").lower() in {"add", "update"}
        ]
        if not candidates:
            return actions
        try:
            learning = self.config.get("learning") or {}
            max_items = int(learning.get("expression_eval_max_items", 6) or 6)
            candidates = candidates[:max_items]
            lines = []
            for index, item in enumerate(candidates):
                situation = str(item.get("situation") or "").strip()
                pattern = str(item.get("pattern") or "").strip()
                lines.append(f"{index}: 场景={situation} | 表达={pattern}")
            prompt = (
                "请评估以下每个候选句式表达是否适合作为群聊表达习惯（适合=短句、有群体色彩、无隐私/命令/身份信息、"
                "不涉及具体人名）。\n\n"
                + "\n".join(lines)
                + '\n\n只输出 JSON：{"results":[{"index":0,"suitable":true/false,"reason":"..."}]}，不要输出其他内容。'
            )
            if self.provider is None:
                self._ensure_provider()
            response = self.provider.send([
                {"role": "system", "content": "你是表达习惯评估器，只评估，不生成聊天回复。"},
                {"role": "user", "content": prompt},
            ])
            data = load_json_lenient(response)
            results = data.get("results") if isinstance(data, dict) else None
            if not isinstance(results, list):
                return None
            unsuitable = set()
            for entry in results:
                if not isinstance(entry, dict):
                    continue
                try:
                    index = int(entry.get("index"))
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(candidates) and entry.get("suitable") is False:
                    unsuitable.add(index)
            kept = [item for index, item in enumerate(candidates) if index not in unsuitable]
            others = [
                item for item in (actions or [])
                if not (isinstance(item, dict) and str(item.get("action") or "").lower() in {"add", "update"})
            ]
            return kept + others
        except Exception:
            return None

    def _get_slang_taxonomy_context(self, group_id):
        if not self.style_learner:
            return "{}"
        try:
            taxonomy = self.style_learner.store.get_slang_taxonomy(group_id)
        except Exception:
            taxonomy = {"types": [], "emotions": [], "emotion_intensities": []}
        return json.dumps(taxonomy, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _scene_topic_key(batch_messages):
        text = " ".join(
            str(item.get("content") or "")
            for item in (batch_messages or [])
            if isinstance(item, dict)
        ).casefold()
        tokens = re.findall(r"[a-z0-9_]{2,}|[\u3400-\u9fff]{2,}", text)
        ignored = {"这个", "那个", "现在", "然后", "就是", "可以", "什么", "怎么"}
        tokens = sorted({token for token in tokens if token not in ignored})[:24]
        intent = "question" if "?" in text or "？" in text else "statement"
        tone = "excited" if any(mark in text for mark in ("!", "！")) else "plain"
        return hashlib.sha1(("|".join(tokens) + "|" + intent + "|" + tone).encode("utf-8")).hexdigest()

    def _get_slang_scene_context(self, group_id, batch_messages):
        # Keep the legacy helper aligned with the current prompt contract:
        # only safe slang literally matched in the current user context may
        # be injected. Other records remain available through the tool.
        return self._get_context_slang_context(group_id, batch_messages)

    def _ensure_provider(self):
        if self.provider is None:
            self.provider = DeepSeekProvider()
        return self.provider

    def _compression_decision(self, stored_tokens, cache_scope="full"):
        """缓存成本感知的上下文压缩决策。

        缓存命中:未命中 = 1:ratio（默认 5.428）。命中价 1：稳定前缀（system +
        缓存头 + 聊天上下文）按命中价计费，非常便宜；未命中价 ratio：只有断点后
        的新增内容与一次性重建按未命中价计费。因此“保持大上下文”通常比“压缩”更省，
        压缩只应在以下情况发生：
          1) 硬上限：上下文即将超过模型窗口（安全优先，必须压缩）；
          2) 成本划算：压缩的一次性成本（压缩调用 + 被替换前缀的一次重建）能在
             cache_break_even_horizon 次请求内被回本。
        返回 (should_compress, reason)。
        """
        try:
            context_limit = int(self.config.get("context_window_tokens", 200000) or 200000)
        except (TypeError, ValueError):
            context_limit = 200000
        try:
            target = int(self.config.get("context_compression_target_tokens", 160000) or 160000)
        except (TypeError, ValueError):
            target = 160000
        try:
            ratio = float(self.config.get("cache_cost_ratio", 5.428) or 5.428)
        except (TypeError, ValueError):
            ratio = 5.428
        try:
            hit_rate = float(self.config.get("cache_hit_rate", 0.85) or 0.85)
        except (TypeError, ValueError):
            hit_rate = 0.85
        try:
            horizon = int(self.config.get("cache_break_even_horizon", 40) or 40)
        except (TypeError, ValueError):
            horizon = 40
        ratio = max(1.0, ratio)
        hit_rate = min(0.99, max(0.0, hit_rate))
        # 缓存范围决定“历史是否按命中价计费”：
        #   full   —— 完整前缀缓存（DeepSeek/OpenAI 兼容自动缓存、responses+prompt_cache_key），
        #              历史按命中价计费，保持大上下文很便宜；
        #   system —— 服务端只复用 system 时，历史仍按未命中价计费，压缩更划算；
        #   none   —— 完全不缓存。
        if str(cache_scope or "full").strip().lower() != "full":
            hit_rate = 0.0
        hit_price, miss_price = 1.0, ratio
        headroom = 16000
        # 提示词内历史可携带上限（与 _fit_group_messages_for_prompt 一致）。
        fit_budget = max(5000, min(target, context_limit - headroom))
        # 模型窗口硬上限（安全边界）。
        hard_limit = context_limit

        if stored_tokens <= fit_budget:
            return False, f"fits-budget({stored_tokens}<={fit_budget})"
        if stored_tokens >= hard_limit:
            return True, f"hard-limit({stored_tokens}>={hard_limit})"
        replaced = max(0, stored_tokens - target)
        if replaced <= 0:
            return False, "nothing-to-save"
        # 一次性成本：压缩调用本身（整段按未命中价）+ 被替换前缀的一次重建（未命中价）。
        one_time = (stored_tokens + replaced) * miss_price
        # 每次请求省下的费用：被移除的 token 原本按“有效价”（命中价为主）计费。
        effective_price = hit_rate * hit_price + (1 - hit_rate) * miss_price
        per_request_saving = replaced * effective_price
        breakeven = one_time / max(per_request_saving, 1e-9)
        if breakeven <= horizon:
            return True, f"cost-effective(breakeven={breakeven:.1f}<={horizon})"
        return False, f"keep-cached(breakeven={breakeven:.1f}>{horizon})"

    def _current_cache_scope(self):
        """读取当前生效 API 的缓存范围；provider 未就绪时按 full（最保守）处理。"""
        try:
            if self.provider is not None and hasattr(self.provider, "current_cache_scope"):
                return self.provider.current_cache_scope()
        except Exception:
            pass
        return "full"

    def _maybe_compress_context(self, group_id, session_id=None):
        if not self.memory_manager:
            return False
        stored_tokens = self.memory_manager.group_context_tokens(group_id)
        should_compress, reason = self._compression_decision(
            stored_tokens,
            cache_scope=self._current_cache_scope(),
        )
        if not should_compress:
            return False
        if not self._context_compression_lock.acquire(blocking=False):
            return False
        try:
            messages = self.memory_manager.get_group_messages(group_id)
            state = self._get_group_memory_state(group_id)
            response = self._complete_with_tools(
                [
                    {"role": "system", "content": build_system_prompt(self.config.get("prompt") or {}, identity=self.config.get("identity") or {}, prefixes=self.config.get("prefixes") or [])},
                ] + build_context_compression_messages({
                    "identity": self.config.get("identity") or {},
                    "prefixes": self.config.get("prefixes") or [],
                    "group_messages": messages,
                    **state,
                }),
                current_session_id=session_id,
                memory_group_id=group_id,
            )
            data = load_json_lenient(response)
            summary = str(data.get("summary") or "").strip()
            recent = [item for item in (data.get("recent_messages") or []) if isinstance(item, dict)]
            if not summary or not recent:
                print(f"[LLM CONTEXT COMPRESSION] invalid result group={group_id}", flush=True)
                return False
            compact = [{
                "nickname": "context_summary",
                "timestamp": int(time.time()),
                "content": summary,
                "role": "context_summary",
                "is_bot": False,
            }]
            for item in recent:
                compact.append({
                    "nickname": str(item.get("nickname") or "未知用户"),
                    "timestamp": item.get("timestamp") or int(time.time()),
                    "content": str(item.get("content") or ""),
                    "role": str(item.get("role") or ("assistant" if item.get("is_bot") else "user")),
                    "is_bot": bool(item.get("is_bot")),
                    "prefix_used": bool(item.get("prefix_used")),
                    "is_at_bot": bool(item.get("is_at_bot")),
                    "is_mentioned": bool(item.get("is_mentioned")),
                    **({"message_id": item["message_id"]} if item.get("message_id") else {}),
                })
            self.memory_manager.replace_group_messages(
                group_id,
                compact,
                preserve_unseen_from=messages,
            )
            print(
                f"[LLM CONTEXT COMPRESSION] group={group_id} before={len(messages)} after={len(compact)} "
                f"tokens={stored_tokens} reason={reason}",
                flush=True,
            )
            return True
        except Exception as exc:
            print(f"[LLM CONTEXT COMPRESSION ERROR] group={group_id} {exc}", flush=True)
            return False
        finally:
            self._context_compression_lock.release()

    @staticmethod
    def _should_retry_curation_in_batches(exc) -> bool:
        text = f"{type(exc).__name__} {exc}".casefold()
        return any(marker in text for marker in (
            "timeout", "timed out", "context length", "maximum context",
            "too many tokens", "request too large", "payload too large", "413",
            "prompt is too long", "jsondecodeerror", "expecting value",
            "empty response", "no response", "整理返回格式无效",
        ))

    @staticmethod
    def _curation_retry_reason(exc) -> str:
        text = f"{type(exc).__name__} {exc}".casefold()
        if any(marker in text for marker in ("context length", "maximum context", "too many tokens", "request too large", "payload too large", "413", "prompt is too long")):
            return "context_limit"
        if "timeout" in text or "timed out" in text:
            return "timeout"
        return "empty_or_invalid_response"

    @staticmethod
    def _split_curation_messages(messages, max_items=16, max_chars=6000):
        chunks = []
        current = []
        current_chars = 0
        for item in messages or []:
            if not isinstance(item, dict):
                continue
            item_chars = len(str(item.get("content") or ""))
            if current and (len(current) >= max_items or current_chars + item_chars > max_chars):
                chunks.append(current)
                current = []
                current_chars = 0
            current.append(item)
            current_chars += item_chars
        if current:
            chunks.append(current)
        return chunks

    @staticmethod
    def _split_curation_records(value, max_items=32, max_chars=6000):
        """Split JSON record lists so a context-limit retry is genuinely smaller."""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return [[value]] if value.strip() else [[]]
        if not isinstance(value, list):
            return [[value]] if value else [[]]

        chunks = []
        current = []
        current_chars = 0
        for item in value:
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if current and (
                len(current) >= max_items
                or current_chars + len(encoded) > max_chars
            ):
                chunks.append(current)
                current = []
                current_chars = 0
            current.append(item)
            current_chars += len(encoded)
        if current:
            chunks.append(current)
        return chunks or [[]]

    def _curate_cycle_in_batches(self, prompt_data, context, group_id, cycle_messages):
        # prompt_data may carry validated source_id annotations used by
        # behavior_actions; keep them when a large cycle is split.
        message_chunks = self._split_curation_messages(
            prompt_data.get("cycle_messages") or cycle_messages
        )
        if not message_chunks:
            message_chunks = [[]]
        slang_chunks = self._split_curation_records(
            prompt_data.get("slang_context") or [],
            max_items=24,
            max_chars=5000,
        )
        memory_chunks = self._split_curation_records(
            prompt_data.get("extra_memory_context") or [],
            max_items=24,
            max_chars=5000,
        )
        batch_count = max(len(message_chunks), len(slang_chunks), len(memory_chunks))
        if not batch_count:
            raise RuntimeError("无法拆分本轮整理消息")
        print(
            f"[LLM CURATION RETRY] group={group_id} batches={batch_count} "
            f"message_batches={len(message_chunks)} slang_batches={len(slang_chunks)} "
            f"memory_batches={len(memory_chunks)}",
            flush=True,
        )
        merged = {
            "memory_actions": [],
            "slang_actions": [],
            "expression_actions": [],
            "behavior_actions": [],
            "style_action": {"action": "keep"},
        }
        system_prompt = build_system_prompt(self.config.get("prompt") or {}, identity=self.config.get("identity") or {}, prefixes=self.config.get("prefixes") or [])
        for index in range(batch_count):
            message_chunk = message_chunks[min(index, len(message_chunks) - 1)]
            slang_chunk = slang_chunks[min(index, len(slang_chunks) - 1)]
            memory_chunk = memory_chunks[min(index, len(memory_chunks) - 1)]
            batch_data = dict(prompt_data)
            batch_data["cycle_messages"] = message_chunk
            batch_data["slang_context"] = json.dumps(
                slang_chunk, ensure_ascii=False, separators=(",", ":")
            )
            batch_data["extra_memory_context"] = json.dumps(
                memory_chunk, ensure_ascii=False, separators=(",", ":")
            )
            print(
                f"[LLM CURATION BATCH] group={group_id} batch={index + 1}/{batch_count} "
                f"items={len(message_chunk)} chars={sum(len(str(item.get('content') or '')) for item in message_chunk)} "
                f"slang={len(slang_chunk)} memory={len(memory_chunk)}",
                flush=True,
            )
            response = self._complete_with_tools(
                [
                    {"role": "system", "content": system_prompt},
                ] + build_memory_curation_messages(batch_data),
                current_session_id=context.get("sessionId"),
                memory_group_id=group_id,
            )
            data = load_json_lenient(response)
            if not isinstance(data, dict) or any(
                key not in data for key in ("memory_actions", "slang_actions", "style_action")
            ):
                raise RuntimeError(f"整理批次 {index + 1} 返回格式无效")
            if isinstance(data.get("expression_actions"), list):
                merged["expression_actions"].extend(data["expression_actions"])
            if isinstance(data.get("memory_actions"), list):
                merged["memory_actions"].extend(data["memory_actions"])
            if isinstance(data.get("slang_actions"), list):
                merged["slang_actions"].extend(data["slang_actions"])
            if isinstance(data.get("behavior_actions"), list):
                merged["behavior_actions"].extend(data["behavior_actions"])
            if isinstance(data.get("style_action"), dict):
                if str(data["style_action"].get("action") or "keep").lower() == "replace":
                    merged["style_action"] = data["style_action"]
        return merged

    def curate_cycle(
        self,
        group_id,
        context=None,
        cycle_messages=None,
    ):
        """Let one LLM decide style, memory, and slang changes at cycle end."""
        if (
            not self.long_term_memory or not self.long_term_memory.enabled
        ) and not self.style_learner:
            return False
        group_id = str(group_id or "unknown")
        cycle_messages = [item for item in (cycle_messages or []) if isinstance(item, dict)]
        if cycle_messages and self.memory_manager:
            # The proactive state machine snapshots inbound messages. Bot
            # replies are persisted in the canonical transcript, so merge
            # those assistant messages back into this cycle's evidence.
            timestamp_values = [
                int(item.get("timestamp") or 0)
                for item in cycle_messages
                if str(item.get("timestamp") or "0").isdigit()
            ]
            first_timestamp = min(timestamp_values) if timestamp_values else 0
            known_ids = {
                str(item.get(key))
                for item in cycle_messages
                for key in ("message_id", "local_id", "server_id")
                if item.get(key) not in (None, "", 0, "0")
            }
            for item in self.memory_manager.get_group_messages(group_id):
                if not isinstance(item, dict):
                    continue
                if not (item.get("is_bot") or str(item.get("role") or "") == "assistant"):
                    continue
                timestamp = int(item.get("timestamp") or 0)
                identity = next(
                    (
                        str(item.get(key))
                        for key in ("message_id", "local_id", "server_id")
                        if item.get(key) not in (None, "", 0, "0")
                    ),
                    "",
                )
                if timestamp >= first_timestamp and (not identity or identity not in known_ids):
                    cycle_messages.append(item)
            cycle_messages.sort(key=lambda item: int(item.get("timestamp") or 0))
        if not cycle_messages:
            return False
        if time.time() < self._memory_curator_backoff_until:
            print(f"[LLM CYCLE CURATION] backoff group={group_id}", flush=True)
            return False
        if not self._memory_curator_lock.acquire(blocking=False):
            return False
        try:
            if self.style_learner:
                self.style_learner.resolve_slang_usage_feedback(group_id)
            if self.long_term_memory and self.long_term_memory.enabled:
                # 整理前先执行短期记忆硬上限：保证整理请求读到的
                # short_memory 不超过上限，并让历史超限数据在本轮落库收敛。
                self.long_term_memory.enforce_short_memory_budget(group_id)
            self._ensure_provider()
            context = context if isinstance(context, dict) else {}
            wxid = str(context.get("wxid") or "")
            query = "\n".join(str(item.get("content") or "") for item in cycle_messages)
            cycle_context = self._get_cycle_context(group_id, str(context.get("cycle_id") or "direct"), wxid, query)
            existing_records = (
                self.long_term_memory.get_memory_records(group_id)
                if self.long_term_memory and self.long_term_memory.enabled else []
            )
            if self.style_learner:
                slang_records = self.style_learner.store.lookup_slang(group_id, "", 200)
                pending_slang_candidates = self.style_learner.store.get_pending_slang_candidates(
                    group_id, limit=20
                )
            else:
                slang_records = []
                pending_slang_candidates = []
            state = self._get_group_memory_state(group_id)
            style_context = self.style_learner.get_prompt_context(group_id) if self.style_learner else ""
            style_payload = (
                self.style_learner.get_cycle_style_payload(group_id)
                if self.style_learner else {}
            )
            pending_rows = []
            if self.long_term_memory and self.long_term_memory.enabled:
                pending = self.long_term_memory.pending_candidates(group_id=group_id, limit=40)
                for item in pending:
                    pending_rows.append({
                        "id": int(item.get("id")),
                        "kind": str(item.get("kind") or ""),
                        "group_id": str(item.get("group_id") or group_id),
                        "subject_id": str(item.get("subject_id") or ""),
                        "content": str(item.get("content") or "")[:180],
                    })
            prompt_cycle_messages = []
            for index, item in enumerate(cycle_messages):
                copied = dict(item)
                source_id = next(
                    (
                        str(copied.get(key)).strip()
                        for key in ("message_id", "local_id", "server_id")
                        if copied.get(key) not in (None, "", 0, "0")
                    ),
                    "",
                )
                copied["source_id"] = source_id or f"cycle-message-{index}"
                prompt_cycle_messages.append(copied)
            local_slang_candidates = self._extract_local_slang_candidates(
                prompt_cycle_messages,
                known_phrases=[
                    str(item.get("phrase") or "")
                    for item in slang_records
                    if isinstance(item, dict)
                ],
            )
            if local_slang_candidates:
                print(
                    f"[LLM CYCLE CURATION] local_slang_candidates group={group_id} "
                    f"count={len(local_slang_candidates)} phrases="
                    f"{[item['phrase'] for item in local_slang_candidates]}",
                    flush=True,
                )
            prompt_data = {
                "identity": self.config.get("identity") or {},
                "prefixes": self.config.get("prefixes") or [],
                "cycle_messages": prompt_cycle_messages,
                "memory_state": state,
                **cycle_context,
                "slang_context": json.dumps(slang_records, ensure_ascii=False, separators=(",", ":")),
                "pending_slang_candidates": pending_slang_candidates,
                "local_slang_candidates": local_slang_candidates,
                "extra_memory_context": json.dumps(existing_records, ensure_ascii=False, separators=(",", ":")),
                "style_profile": style_context,
                "style_learning_payload": style_payload,
                "pending_candidates": pending_rows,
            }
            try:
                response = self._complete_with_tools(
                    [
                        {"role": "system", "content": build_system_prompt(self.config.get("prompt") or {}, identity=self.config.get("identity") or {}, prefixes=self.config.get("prefixes") or [])},
                    ] + build_memory_curation_messages(prompt_data),
                    current_session_id=context.get("sessionId"),
                    memory_group_id=group_id,
                )
                data = load_json_lenient(response)
                if not isinstance(data, dict) or any(
                    key not in data for key in ("memory_actions", "slang_actions", "style_action")
                ):
                    raise RuntimeError("整理返回格式无效")
                if not isinstance(data.get("expression_actions"), list):
                    data["expression_actions"] = []
            except Exception as exc:
                if not self._should_retry_curation_in_batches(exc):
                    raise
                print(
                    f"[LLM CURATION RETRY TRIGGER] group={group_id} "
                    f"reason={self._curation_retry_reason(exc)} error={type(exc).__name__}",
                    flush=True,
                )
                data = self._curate_cycle_in_batches(
                    prompt_data,
                    context,
                    group_id,
                    cycle_messages,
                )
            memory_actions = data.get("memory_actions") if isinstance(data, dict) else []
            slang_actions = data.get("slang_actions") if isinstance(data, dict) else []
            expression_actions = data.get("expression_actions") if isinstance(data, dict) else []
            behavior_actions = data.get("behavior_actions") if isinstance(data, dict) else []
            if (
                bool((self.config.get("learning") or {}).get("expression_eval_enabled", False))
                and expression_actions
            ):
                evaluated = self._evaluate_expression_actions(group_id, expression_actions)
                if evaluated is not None:
                    expression_actions = evaluated
            processed = 0
            candidate_ids = data.get("candidate_ids") if isinstance(data, dict) and isinstance(data.get("candidate_ids"), list) else []
            valid_ids = {int(item.get("id")) for item in pending_rows if item.get("id")}
            if not candidate_ids and pending_rows:
                # A valid response without candidate_ids means the LLM reviewed
                # the pending evidence and found nothing worth keeping.
                candidate_ids = sorted(valid_ids)
            selected = [int(item) for item in candidate_ids if str(item).isdigit() and int(item) in valid_ids]
            if selected and self.long_term_memory and self.long_term_memory.enabled:
                self.long_term_memory.mark_candidates_processed(selected)
                processed = len(selected)
            slang_applied = 0
            slang_phrases = self._known_slang_phrases(group_id)
            slang_stats = {
                "received": 0,
                "grounded": 0,
                "kept": 0,
                "deferred": 0,
                "rejected": {},
            }
            grounded_slang = []
            for raw_action in slang_actions or []:
                slang_stats["received"] += 1
                if not isinstance(raw_action, dict):
                    slang_stats["rejected"]["invalid_action"] = slang_stats["rejected"].get("invalid_action", 0) + 1
                    continue
                action_group = str(raw_action.get("group_id") or group_id)
                if action_group != group_id or not self.style_learner:
                    slang_stats["rejected"]["wrong_group_or_disabled"] = slang_stats["rejected"].get("wrong_group_or_disabled", 0) + 1
                    continue
                action, reason = self._ground_slang_action_in_cycle(raw_action, prompt_cycle_messages)
                if action is None:
                    slang_stats["rejected"][reason] = slang_stats["rejected"].get(reason, 0) + 1
                    continue
                slang_stats["grounded"] += 1
                normalized = str(action.get("normalized_phrase") or action.get("phrase") or "").strip().casefold()
                if not normalized:
                    slang_stats["rejected"]["missing_normalized_phrase"] = slang_stats["rejected"].get("missing_normalized_phrase", 0) + 1
                    continue
                grounded_slang.append(action)
            # Fragmentation guard: if the LLM proposes both a longer phrase and
            # one of its substrings in the same cycle (e.g. "吓哭了" plus
            # "吓哭"/"哭了"), keep the longest phrase and reject the fragments.
            fragment_blocked = self._block_fragment_slang_indices(grounded_slang, known_phrases=slang_phrases)
            for index, action in enumerate(grounded_slang):
                normalized = str(action.get("normalized_phrase") or action.get("phrase") or "").strip().casefold()
                if index in fragment_blocked:
                    slang_stats["rejected"]["substring_of_longer_new_slang"] = (
                        slang_stats["rejected"].get("substring_of_longer_new_slang", 0) + 1
                    )
                    continue
                operation = str(action.get("action") or "").lower()
                if operation == "keep":
                    slang_stats["kept"] += 1
                    continue
                if operation == "delete":
                    slang_applied += int(self.style_learner.store.remove_slang(group_id, normalized))
                    self.style_learner.store.remove_pending_slang_candidate(group_id, normalized)
                    continue
                action["group_id"] = group_id
                action["normalized_phrase"] = normalized
                if str(action.get("similarity_decision") or "").strip().lower() == "uncertain":
                    if operation in {"add", "update"} and self.style_learner.store.defer_slang_candidate(action):
                        slang_stats["deferred"] += 1
                    else:
                        slang_stats["rejected"]["uncertain_not_deferred"] = (
                            slang_stats["rejected"].get("uncertain_not_deferred", 0) + 1
                        )
                    continue
                action["status"] = action.get("status") or "active"
                action["min_term_count"] = self.style_learner.min_term_count
                resolved = self.style_learner.store.resolve_slang_write(action)
                if not resolved:
                    slang_stats["rejected"]["similarity_resolution"] = slang_stats["rejected"].get("similarity_resolution", 0) + 1
                    continue
                if self.style_learner.store.apply_slang_scenario(resolved):
                    slang_applied += 1
                    slang_phrases.append(str(resolved.get("phrase") or action.get("phrase") or "").strip())
                    self.style_learner.store.remove_pending_slang_candidate(group_id, normalized)
                else:
                    slang_stats["rejected"]["store_rejected"] = slang_stats["rejected"].get("store_rejected", 0) + 1

            # Keep slang out of short memory. Apply memory actions only after
            # current-cycle slang has been resolved, so both new and existing
            # known phrases are removed from short-memory writes.
            memory_actions, short_slang_removed = self._remove_slang_from_short_memory_actions(
                memory_actions, slang_phrases
            )
            memory_result = (
                self.long_term_memory.apply_memory_actions(group_id, memory_actions or [])
                if self.long_term_memory and self.long_term_memory.enabled
                else {"updated": 0, "deleted": 0, "added": 0}
            )
            expression_applied = 0
            if self.style_learner:
                expression_applied = int(
                    self.style_learner.apply_expression_actions(group_id, expression_actions or []) or 0
                )
            behavior_applied = 0
            if self.style_learner and isinstance(behavior_actions, list):
                all_source_ids = [str(item.get("source_id") or "").strip() for item in prompt_cycle_messages]
                user_source_ids = [
                    str(item.get("source_id") or "").strip()
                    for item in prompt_cycle_messages
                    if not item.get("is_bot") and str(item.get("role") or "") != "assistant"
                ]
                behavior_applied = int(self.style_learner.apply_behavior_actions(
                    group_id,
                    behavior_actions,
                    valid_source_ids=(all_source_ids if len(user_source_ids) >= 10 else []),
                    min_messages=10,
                ) or 0)
            style_applied = False
            style_action = data.get("style_action") if isinstance(data, dict) else None
            if (
                isinstance(style_action, dict)
                and str(style_action.get("action") or "keep").lower() == "replace"
                and self.style_learner
            ):
                card = style_action.get("card")
                if isinstance(card, dict):
                    style_applied = bool(self.style_learner.apply_style_card(
                        group_id,
                        card,
                        source_message_count=int(style_payload.get("message_count", 0) or 0),
                    ))
            context_cleared = False
            cycle_end_at = context.get("_cycle_end_at")
            if cycle_messages and cycle_end_at is not None and self.memory_manager:
                try:
                    boundary = float(cycle_end_at)
                except (TypeError, ValueError):
                    boundary = 0
                self.memory_manager.prune_group_messages_through(group_id, boundary)
                context_cleared = True
            with self._cycle_context_cache_lock:
                self._proactive_context_cache.pop(group_id, None)
            with self._cycle_context_cache_lock:
                self._cycle_context_cache.pop(group_id, None)
            user_cycle_messages = [
                item for item in prompt_cycle_messages
                if not item.get("is_bot") and str(item.get("role") or "") != "assistant"
            ]
            if self.style_learner:
                self.style_learner.record_curation_run(
                    group_id,
                    cycle_id=context.get("cycle_id") or context.get("_cycle_id") or "",
                    cycle_message_count=len(prompt_cycle_messages),
                    user_message_count=len(user_cycle_messages),
                    outcome="success",
                    slang_action_count=slang_stats["received"],
                    slang_grounded_count=slang_stats["grounded"],
                    slang_applied_count=slang_applied,
                    slang_rejected=slang_stats["rejected"],
                )
            print(
                f"[LLM CYCLE CURATION] group={group_id} memory={memory_result} slang={slang_applied} "
                f"expressions={expression_applied} behaviors={behavior_applied} style={style_applied} "
                f"candidates={processed}/{len(pending_rows)} context_cleared={context_cleared} "
                f"slang_actions={slang_stats['received']} grounded={slang_stats['grounded']} "
                f"deferred={slang_stats['deferred']} "
                f"short_slang_removed={short_slang_removed} rejected={slang_stats['rejected']}",
                flush=True,
            )
            return True
        except Exception as exc:
            error_text = str(exc).casefold()
            if (
                is_insufficient_balance_error(exc)
                or "embedding" in error_text
                or "hashing-v1" in error_text
            ):
                self._memory_curator_backoff_until = time.time() + 600
            if self.style_learner:
                self.style_learner.record_curation_run(
                    group_id,
                    cycle_id=(context or {}).get("cycle_id") if isinstance(context, dict) else "",
                    cycle_message_count=len(cycle_messages or []),
                    user_message_count=sum(
                        1 for item in (cycle_messages or [])
                        if isinstance(item, dict) and not item.get("is_bot")
                        and str(item.get("role") or "") != "assistant"
                    ),
                    outcome="error",
                    error_text=str(exc),
                )
            print(f"[LLM CYCLE CURATION ERROR] group={group_id} {exc}", flush=True)
            return False
        finally:
            self._memory_curator_lock.release()

    def set_proactive_callback(self, callback):
        self.proactive_reply.set_batch_callback(callback)

    def handle_proactive_message(self, context):
        return self.proactive_reply.handle_message(context)

    @staticmethod
    def _attention_batch(group_id, stored_messages, token_budget=1000):
        """Convert the newest complete messages within the attention budget."""
        try:
            token_budget = max(1, int(token_budget))
        except (TypeError, ValueError):
            token_budget = 1000
        result = []
        used_tokens = 0
        selected = []
        for item in reversed(stored_messages or []):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            message_tokens = MemoryManager.estimate_tokens(content)
            if selected and used_tokens + message_tokens > token_budget:
                break
            selected.append(item)
            used_tokens += message_tokens
        selected.reverse()
        for index, item in enumerate(selected):
            content = str(item.get("content") or "").strip()
            timestamp = item.get("timestamp") or ""
            result.append({
                "message_id": item.get("message_id") or f"history-{timestamp}-{index}",
                "local_id": item.get("local_id"),
                "server_id": item.get("server_id"),
                "session_id": item.get("session_id") or group_id,
                "timestamp": timestamp,
                "group": group_id,
                "sender_nickname": item.get("nickname") or "未知用户",
                "nickname": item.get("nickname") or "未知用户",
                "sender_wxid": item.get("wxid") or "",
                "content": content,
                "is_bot": bool(item.get("is_bot")),
                "role": item.get("role") or ("assistant" if item.get("is_bot") else "user"),
                "is_at_bot": bool(item.get("is_at_bot")),
                "is_mentioned": bool(item.get("is_mentioned")),
                "prefix_used": bool(item.get("prefix_used")),
                "is_command": bool(item.get("is_command")),
                "is_url_only": bool(item.get("is_url_only")),
                "message_type": item.get("message_type") or "text",
            })
        return result

    def _cache_proactive_context(self, group_id, cycle_id):
        if not self.memory_manager or not cycle_id:
            return
        with self._cycle_context_cache_lock:
            self._proactive_context_cache[str(group_id)] = {
                "cycle_id": str(cycle_id),
                "messages": list(self.memory_manager.get_group_messages(group_id)),
            }

    def _attention_prompt_context(
        self,
        group_id,
        cycle_id,
        attention_source,
        latest_messages,
    ):
        """Use a fresh 1000-token tail, optionally attached to work context."""
        if str(attention_source or "idle") != "working":
            return latest_messages
        with self._cycle_context_cache_lock:
            cached = self._proactive_context_cache.get(str(group_id))
        if not cached or str(cached.get("cycle_id") or "") != str(cycle_id or ""):
            return latest_messages
        combined = list(cached.get("messages") or [])

        def identity(item):
            for key in ("message_id", "local_id", "server_id"):
                value = item.get(key) if isinstance(item, dict) else None
                if value in (None, "", 0, "0"):
                    continue
                if key == "message_id" and str(value).startswith("history-"):
                    continue
                return f"{key}:{value}"
            return f"content:{item.get('timestamp')}:{item.get('nickname')}:{item.get('content')}"

        seen = set()
        for item in combined:
            if not isinstance(item, dict):
                continue
            seen.add(identity(item))
        for item in latest_messages or []:
            if not isinstance(item, dict):
                continue
            item_identity = identity(item)
            if item_identity not in seen:
                combined.append(item)
                seen.add(item_identity)
        return combined

    def on_proactive_result(self, context, result, attention_check=False):
        self.proactive_reply.on_llm_result(
            context,
            result,
            attention_check=attention_check,
        )

    def _get_active_style_switch(self, group_id):
        """Read the current cycle's LLM-chosen style selection for injection."""
        try:
            return self.proactive_reply.get_active_style_switch(group_id)
        except Exception:
            return None

    def _apply_style_switch(self, group_id, cycle_id, switch):
        """Validate and apply the optional style_switch reply field.

        Only grounded values are accepted: the situation must exist in the
        active expression library and the type/emotion must exist in the slang
        taxonomy, so the model cannot invent entries that were never learned.
        Any failure is swallowed so a bad switch never breaks the reply flow.
        """
        try:
            return self._apply_style_switch_inner(group_id, cycle_id, switch)
        except Exception:
            return False

    def _apply_style_switch_inner(self, group_id, cycle_id, switch):
        if self.proactive_reply is None or not isinstance(switch, dict):
            return False
        learning = self.config.get("learning") or {}
        if not bool(learning.get("style_switch_enabled", True)):
            return False
        action = str(switch.get("action") or "set").lower()
        if action == "clear":
            return self.proactive_reply.set_active_style_switch(group_id, cycle_id, None)
        if action == "keep":
            return False
        scene = str(switch.get("scene") or "").strip()[:40]
        situation = str(switch.get("situation") or "").strip()[:160]
        slang_type = str(switch.get("slang_type") or "").strip()[:40]
        emotion = str(switch.get("emotion") or "").strip()[:40]
        pattern = str(switch.get("pattern") or "").strip()[:240]
        if not (scene or situation or slang_type or emotion or pattern):
            return False
        selection = {
            "scene": scene,
            "situation": situation,
            "slang_type": slang_type,
            "emotion": emotion,
            "pattern": pattern,
        }
        if self.style_learner:
            valid_situation = True
            if situation:
                valid_situation = False
                try:
                    expressions = self.style_learner.store.get_expressions(group_id, limit=200)
                    normalized = situation.casefold()
                    valid_situation = any(
                        normalized == str(item.get("situation") or "").casefold()
                        or normalized in str(item.get("situation") or "").casefold()
                        for item in expressions
                    )
                except Exception:
                    valid_situation = False
            valid_type = True
            if slang_type:
                valid_type = False
                try:
                    taxonomy = self.style_learner.store.get_slang_taxonomy(group_id)
                    known_types = {str(value).casefold() for value in (taxonomy.get("types") or [])}
                    valid_type = slang_type.casefold() in known_types
                except Exception:
                    valid_type = False
            valid_emotion = True
            if emotion:
                valid_emotion = False
                try:
                    taxonomy = self.style_learner.store.get_slang_taxonomy(group_id)
                    known_emotions = {str(value).casefold() for value in (taxonomy.get("emotions") or [])}
                    valid_emotion = emotion.casefold() in known_emotions
                except Exception:
                    valid_emotion = False
            if not (valid_situation and valid_type and valid_emotion):
                return False
        return self.proactive_reply.set_active_style_switch(group_id, cycle_id, selection)

    def _balance_response(self):
        if self._balance_warning_sent:
            result = build_balance_error_response()
            result["messages"] = []
            return result
        self._balance_warning_sent = True
        return build_balance_error_response()

    def handle_message(self, group_id, nickname, content, wxid="", session_id=None,
                       message_context=None, force_reply=False):
        result_to_return = dict(FALLBACK_RESPONSE)

        try:
            if not self.config.get("enabled"):
                return build_error_response("LLM转发失败：LLM 功能未启用")

            if self.memory_manager is None:
                return build_error_response("LLM转发失败：记忆模块不可用")

            # 初始化或重新初始化 provider（如果失败过）
            if self.provider is None:
                try:
                    self.provider = DeepSeekProvider()
                except Exception as init_err:
                    if is_insufficient_balance_error(init_err):
                        return self._balance_response()
                    if isinstance(init_err, ProviderShuttingDown):
                        print("[LLM] 解释器退出中，跳过 LLM 客户端初始化", flush=True)
                        return {"messages": [], "animation": None}
                    return build_error_response(f"LLM转发失败：LLM 客户端初始化失败 - {init_err}")

            current_message = message_context or {}
            self.memory_manager.add_group_message(
                group_id,
                nickname,
                content,
                message_id=current_message.get("messageKey"),
                local_id=current_message.get("localId"),
                server_id=current_message.get("serverId") or current_message.get("svrid") or current_message.get("rawid"),
                session_id=session_id,
                is_bot=False,
                role="user",
                prefix_used=bool(current_message.get("prefix_used")),
                is_at_bot=bool(current_message.get("is_at") or current_message.get("is_at_bot")),
                is_mentioned=bool(current_message.get("is_mentioned")),
                message_type=current_message.get("type") or current_message.get("message_type"),
            )
            self._maybe_compress_context(group_id, session_id)
            group_messages = self._fit_group_messages_for_prompt(
                self.memory_manager.get_group_messages(group_id)
            )
            cycle_context = self._get_cycle_context(group_id, "direct", wxid, content)
            cycle_context["slang_context"] = self._get_context_slang_context(
                group_id,
                group_messages,
            )
            # Style expressions are queried by the main LLM on demand through
            # lookup_group_expressions, alongside slang and memory tools.
            expression_list = []
            memory_state = self._get_group_memory_state(group_id)
            style_context = self.style_learner.get_prompt_context(group_id) if self.style_learner else ""
            emoji_list = list(self.emoji_list)
            style_profile = (
                self.style_learner.get_prompt_context(group_id)
                if self.style_learner else ""
            )
            active_style_switch = self._get_active_style_switch(group_id)

            system_prompt = build_system_prompt(self.config.get("prompt") or {}, identity=self.config.get("identity") or {}, prefixes=self.config.get("prefixes") or [])
            prompt_data = {
                "group_messages": group_messages,
                "emoji_list": emoji_list,
                "identity": self.config.get("identity") or {},
                "llm_config": self.config or {},
                "prefixes": self.config.get("prefixes") or [],
                "prompt": self.config.get("prompt") or {},
                "sender_wxid": wxid,
                "current_message": current_message,
                "current_state": "直接回复模式",
                "force_reply": bool(force_reply),
                **cycle_context,
                **memory_state,
                "style_profile": style_profile,
                "expression_list": expression_list,
                "active_style_switch": active_style_switch,
            }

            messages = [
                {"role": "system", "content": system_prompt},
            ] + build_user_messages(prompt_data)

            print(
                f"[LLM INPUT] {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"group={group_id} mode=direct count=1 chars={len(str(content or ''))} "
                f"force={bool(force_reply)} prefix={bool((message_context or {}).get('prefix_used'))}",
                flush=True,
            )
            response_text = self._complete_with_tools(
                messages,
                current_session_id=session_id,
                memory_group_id=group_id,
            )
            parsed = parse_llm_response(response_text, emoji_list)
            style_switch = parsed.get("style_switch")
            if isinstance(style_switch, dict):
                self._apply_style_switch(group_id, current_message.get("_cycle_id"), style_switch)
            parsed.pop("style_switch", None)
            if force_reply and not parsed.get("messages") and not parsed.get("animation"):
                fallback_message = str(
                    (self.config.get("prompt") or {}).get("fallback_message")
                    or "我在"
                ).strip() or "我在"
                parsed["messages"] = [fallback_message]
            result_to_return = parsed
            self._balance_warning_sent = False
        except Exception as e:
            if is_insufficient_balance_error(e):
                result_to_return = self._balance_response()
            elif isinstance(e, ProviderShuttingDown):
                print("[LLM] 解释器退出中，本次消息不回复", flush=True)
                result_to_return = {"messages": [], "animation": None}
            else:
                result_to_return = build_error_response(f"LLM转发失败：{e}")

        if not result_to_return.get("_balance_error"):
            self._store_assistant_messages(group_id, result_to_return)
            self._cache_proactive_context(group_id, current_message.get("_cycle_id"))
            if self.style_learner:
                self.style_learner.record_expression_usage(
                    group_id,
                    result_to_return.get("messages") or [],
                )
        return result_to_return

    def _lookup_group_behaviors_for_reply(self, group_id, query="", max_items=3):
        if not self.style_learner or not group_id:
            return {"ok": False, "error": "当前上下文不支持行为模式查询"}
        records = self.style_learner.lookup_group_behaviors(
            group_id, query, max_items=min(int(max_items or 3), 3)
        )
        self.style_learner.record_behavior_selection(
            group_id, [item.get("id") for item in records if isinstance(item, dict)]
        )
        return {"ok": True, "group_id": group_id, "records": records}

    def _complete_with_tools(self, messages, allowed_hosts=None, current_session_id=None, memory_group_id=None, allow_memory_update=False, memory_observations=None, tieba_opportunity=False, behavior_lookup_enabled=False):
        """Run DeepSeek's assistant/tool/assistant loop and return final JSON."""
        tools = []
        if self.config.get("web_fetch_enabled", True):
            tools.append(WEB_FETCH_TOOL)
            if tieba_opportunity:
                tools.append(TIABA_HOT_TOOL)
        if self.config.get("original_message_enabled", True) and current_session_id:
            tools.append(ORIGINAL_MESSAGE_TOOL)
        if memory_group_id and self.style_learner:
            tools.append(SLANG_LOOKUP_TOOL)
            tools.append(SLANG_SIMILAR_LOOKUP_TOOL)
            tools.append(EXPRESSION_LOOKUP_TOOL)
            if behavior_lookup_enabled:
                tools.append(BEHAVIOR_LOOKUP_TOOL)
        if memory_group_id and self.long_term_memory and self.long_term_memory.enabled:
            tools.append(MEMORY_LOOKUP_TOOL)
        if not tools:
            return self.provider.send(messages)

        try:
            max_calls = int(self.config.get("web_fetch_max_calls", 3) or 3)
        except (TypeError, ValueError):
            max_calls = 3
        max_calls = max(1, min(max_calls, 5))

        try:
            timeout = float(self.config.get("web_fetch_timeout_seconds", 15) or 15)
        except (TypeError, ValueError):
            timeout = 15.0
        try:
            max_chars = int(self.config.get("web_fetch_max_chars", 24000) or 24000)
        except (TypeError, ValueError):
            max_chars = 24000

        conversation = list(messages)
        tool_calls_used = 0
        original_calls_used = 0
        try:
            loop_timeout = float(self.config.get("tool_loop_timeout_seconds", 45) or 45)
        except (TypeError, ValueError):
            loop_timeout = 45.0
        loop_deadline = time.monotonic() + max(10.0, min(loop_timeout, 120.0))
        try:
            original_max_calls = int(self.config.get("original_message_max_calls", 2) or 2)
        except (TypeError, ValueError):
            original_max_calls = 2
        original_max_calls = max(1, min(original_max_calls, 4))
        while True:
            if time.monotonic() >= loop_deadline:
                print("[LLM TOOL LOOP TIMEOUT] final response skipped after deadline", flush=True)
                return ""
            assistant_message = self.provider.send_chat(
                conversation,
                tools=tools,
            )
            tool_calls = getattr(assistant_message, "tool_calls", None) or []
            if not tool_calls:
                return str(getattr(assistant_message, "content", None) or "")

            conversation.append(self._assistant_message_dict(assistant_message))
            for tool_call in tool_calls:
                if tool_calls_used >= max_calls:
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": getattr(tool_call, "id", ""),
                        "content": '{"ok":false,"error":"工具调用次数已达到上限"}',
                    })
                    continue

                function = getattr(tool_call, "function", None)
                name = str(getattr(function, "name", "") or "")
                if name == ORIGINAL_MESSAGE_TOOL["function"]["name"]:
                    if original_calls_used >= original_max_calls:
                        conversation.append({
                            "role": "tool",
                            "tool_call_id": getattr(tool_call, "id", ""),
                            "content": '{"ok":false,"error":"原始消息工具调用次数已达上限"}',
                        })
                        tool_calls_used += 1
                        continue
                    original_calls_used += 1
                arguments = getattr(function, "arguments", "{}")
                content = execute_tool(
                    name,
                    arguments,
                    timeout=timeout,
                    max_chars=max_chars,
                    allowed_hosts=allowed_hosts,
                    current_session_id=current_session_id,
                    api_base=self.config.get("weflow_api_base"),
                    api_token=self.config.get("weflow_api_token"),
                    original_timeout=self.config.get("original_message_timeout_seconds", 8),
                    original_max_chars=self.config.get("original_message_max_chars", 16000),
                    slang_lookup=(
                        lambda query, max_items=20: {
                            "ok": True,
                            "group_id": memory_group_id,
                            "records": self.style_learner.store.lookup_slang(
                                memory_group_id, query, min(int(max_items or 20), 50)
                            ),
                        }
                    ) if self.style_learner and memory_group_id else None,
                    slang_similar_lookup=(
                        lambda phrase, max_items=8: {
                            "ok": True,
                            "group_id": memory_group_id,
                            "records": self.style_learner.store.find_similar_slang(
                                memory_group_id, phrase, min(int(max_items or 8), 20)
                            ),
                        }
                    ) if self.style_learner and memory_group_id else None,
                    expression_lookup=(
                        lambda query, max_items=6: {
                            "ok": True,
                            "group_id": memory_group_id,
                            "records": self.style_learner.store.get_context_expressions(
                                memory_group_id,
                                [{"content": str(query or "")[:240], "is_bot": False}],
                                max_items=min(int(max_items or 6), 12),
                                max_chars=1400,
                            ),
                        }
                    ) if self.style_learner and memory_group_id else None,
                    behavior_lookup=(
                        lambda query, max_items=3: self._lookup_group_behaviors_for_reply(
                            memory_group_id, query, max_items
                        )
                    ) if self.style_learner and memory_group_id and behavior_lookup_enabled else None,
                    memory_lookup=(
                        lambda query, subject_id="": {
                            "ok": True,
                            "group_id": memory_group_id,
                            "person_profile": self._get_person_profile_context(
                                memory_group_id, subject_id, query
                            ),
                            "extra_memory": self._get_memory_context(
                                memory_group_id, subject_id, query
                            ),
                        }
                    ) if self.long_term_memory and memory_group_id else None,
                )
                conversation.append({
                    "role": "tool",
                    "tool_call_id": getattr(tool_call, "id", ""),
                    "content": content,
                })
                tool_calls_used += 1

            if tool_calls_used >= max_calls:
                # Do not leave the model in an unbounded tool-call loop. The
                # tool error (or the last successful result) is still included
                # in the context for one final JSON-only response.
                final_message = self.provider.send_chat(conversation)
                return str(getattr(final_message, "content", None) or "")

    def handle_batch_message(
        self,
        group_id,
        messages,
        force_reply=False,
        trigger_source="interval",
        attention_check=False,
        session_id=None,
        allow_memory_update=False,
        cycle_id=None,
        attention_source="idle",
        nonsense_opportunity=False,
        slang_emotional_opportunity=False,
        tieba_opportunity=False,
        proactive_web_opportunity=False,
    ):
        """Judge a ten-second message batch for proactive group replies."""
        effective_force_reply = bool(force_reply)
        result_to_return = {
            "messages": [],
            "animation": None,
            "should_reply": effective_force_reply,
            "reply_to": [],
            "_llm_ok": False,
        }
        batch_messages = []
        conversation_pulse = {}
        fallback_message = str(
            (self.config.get("prompt") or {}).get("fallback_message") or "我在，有什么事？"
        ).strip() or "我在，有什么事？"

        def _finish(result):
            if (
                effective_force_reply
                and not result.get("messages")
                and not result.get("_balance_error")
            ):
                result["messages"] = [fallback_message]
                result["should_reply"] = True
            return result

        try:
            if not self.config.get("enabled"):
                return _finish(result_to_return)

            if self.memory_manager is None:
                return _finish(result_to_return)

            batch_messages = [item for item in (messages or []) if isinstance(item, dict)]
            proactive_web_opportunity = bool(proactive_web_opportunity)
            if not batch_messages and not proactive_web_opportunity:
                return _finish(result_to_return)

            attention_budget = int(
                (self.config.get("auto_reply") or {}).get(
                    "attention_context_token_budget", 1000
                ) or 1000
            )
            if attention_check:
                stored_messages = self.memory_manager.get_group_messages(group_id)
                latest_messages = self._attention_batch(
                    group_id,
                    stored_messages,
                    attention_budget,
                )
                if latest_messages:
                    batch_messages = latest_messages

            url_only_present = any(
                bool(item.get("is_url_only")) for item in batch_messages
            )
            effective_force_reply = bool(force_reply or url_only_present)
            result_to_return["should_reply"] = effective_force_reply

            # Local scheduling only decides whether this batch deserves the
            # one normal proactive LLM call. It does not author a reply: once
            # planned, the model receives the complete context and may still
            # stay silent or select an earlier topic.
            auto_settings = self.config.get("auto_reply") or {}
            trigger_mode = str(auto_settings.get("reply_trigger_mode", "conversation_pulse") or "conversation_pulse").lower()
            if (
                not attention_check
                and not effective_force_reply
                and not proactive_web_opportunity
                and trigger_mode in {"reply_necessity", "conversation_pulse"}
            ):
                history = self.memory_manager.get_group_messages(group_id)
                timing_learning = self.proactive_reply.get_reply_timing_learning(group_id)
                pulse_decision, conversation_pulse = self.conversation_pulse.decide(
                    group_id,
                    batch_messages,
                    history,
                    timing_learning,
                    self.proactive_reply.seconds_since_llm_reply(group_id),
                )
                if pulse_decision != "plan":
                    result_to_return = {
                        "messages": [],
                        "animation": None,
                        "should_reply": False,
                        "reply_to": [],
                        "_llm_ok": True,
                        "_local_gate": f"conversation_pulse:{pulse_decision}",
                    }
                    if self.style_learner:
                        self.style_learner.record_response_decision(
                            group_id,
                            batch_messages,
                            result_to_return,
                            force_reply=False,
                            attention_check=False,
                        )
                    print(
                        f"[LLM LOCAL GATE] working batch {pulse_decision} group={group_id} "
                        f"score={conversation_pulse.get('score')} density={conversation_pulse.get('density_60s')} "
                        f"reason={conversation_pulse.get('reason')}",
                        flush=True,
                    )
                    return result_to_return

            if self.provider is None:
                self.provider = DeepSeekProvider()

            if not attention_check:
                for index, item in enumerate(batch_messages):
                    self.memory_manager.add_group_message(
                        group_id,
                        item.get("sender_nickname") or "未知用户",
                        item.get("content") or "",
                        message_id=item.get("message_id"),
                        local_id=item.get("local_id"),
                        server_id=item.get("server_id"),
                        session_id=item.get("session_id") or session_id,
                        is_bot=False,
                        role="user",
                        batch_index=index,
                        prefix_used=bool(item.get("prefix_used")),
                        is_at_bot=bool(item.get("is_at_bot")),
                        is_mentioned=bool(item.get("is_mentioned")),
                        message_type=item.get("message_type"),
                    )

            memory_query = "\n".join(
                str(item.get("content") or "")
                for item in batch_messages
                if isinstance(item, dict)
            )
            sender_wxid = str(batch_messages[0].get("sender_wxid") or "") if batch_messages else ""
            cycle_context = self._get_cycle_context(group_id, cycle_id, sender_wxid, memory_query)
            memory_state = self._get_group_memory_state(group_id)
            style_context = self.style_learner.get_prompt_context(group_id) if self.style_learner else ""
            active_style_switch = self._get_active_style_switch(group_id)
            no_llm_reply_seconds = self.proactive_reply.seconds_since_llm_reply(group_id)
            attention_boost = self.proactive_reply.attention_boost_active(group_id)
            self._maybe_compress_context(group_id, session_id)
            latest_attention_messages = self._attention_batch(
                group_id,
                self.memory_manager.get_group_messages(group_id),
                attention_budget,
            ) if attention_check else None
            prompt_group_messages = (
                self._attention_prompt_context(
                    group_id,
                    cycle_id,
                    attention_source,
                    latest_attention_messages,
                )
                if attention_check
                else self.memory_manager.get_group_messages(group_id)
            )
            prompt_group_messages = self._fit_group_messages_for_prompt(prompt_group_messages)
            cycle_context["slang_context"] = self._get_context_slang_context(
                group_id,
                prompt_group_messages or batch_messages,
            )
            # Style expressions are queried by the main LLM on demand through
            # lookup_group_expressions, alongside slang and memory tools.
            expression_list = []

            slang_emotional_candidates = []
            if attention_check and slang_emotional_opportunity and self.style_learner:
                try:
                    slang_emotional_candidates = self.style_learner.store.get_slang_emotional_candidates(
                        group_id,
                        prompt_group_messages or batch_messages,
                        rotation=bool((self.config.get("learning") or {}).get("slang_emotional_pool_rotation", True)),
                    )
                except Exception:
                    slang_emotional_candidates = []

            prompt_config = self.config.get("prompt") or {}
            system_prompt = build_system_prompt(prompt_config, identity=self.config.get("identity") or {}, prefixes=self.config.get("prefixes") or [])
            prompt_data = {
                "batch_messages": batch_messages,
                "group_messages": prompt_group_messages,
                "force_reply": effective_force_reply,
                "trigger_source": trigger_source,
                "attention_check": attention_check,
                "no_llm_reply_seconds": no_llm_reply_seconds,
                "attention_boost": attention_boost,
                "nonsense_opportunity": bool(attention_check and nonsense_opportunity),
                "slang_emotional_opportunity": bool(attention_check and slang_emotional_opportunity),
                "slang_emotional_candidates": slang_emotional_candidates,
                "tieba_opportunity": bool(
                    (attention_check and tieba_opportunity) or proactive_web_opportunity
                ),
                "proactive_web_opportunity": proactive_web_opportunity,
                "conversation_pulse": conversation_pulse,
                "slang_usage_guidance": (
                    self.style_learner.get_slang_usage_guidance(group_id)
                    if self.style_learner else ""
                ),
                **cycle_context,
                **memory_state,
                "emoji_list": self.emoji_list,
                "identity": self.config.get("identity") or {},
                "llm_config": self.config or {},
                "prefixes": self.config.get("prefixes") or [],
                "style_profile": style_context,
                "expression_list": expression_list,
                "active_style_switch": active_style_switch,
            }
            batch_char_count = sum(
                len(str(item.get("content") or ""))
                for item in batch_messages
                if isinstance(item, dict)
            )
            print(
                f"[LLM INPUT] {time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"group={group_id} mode=proactive count={len(batch_messages)} chars={batch_char_count} "
                f"force={effective_force_reply} attention={attention_check} trigger={trigger_source}",
                flush=True,
            )
            response_text = self._complete_with_tools(
                [
                    {"role": "system", "content": system_prompt},
                ] + build_batch_user_messages(prompt_data),
                allowed_hosts=AUTO_REPLY_URL_HOSTS if url_only_present else None,
                current_session_id=session_id,
                memory_group_id=group_id,
                allow_memory_update=False,
                memory_observations=None,
                tieba_opportunity=bool(
                    (attention_check and tieba_opportunity) or proactive_web_opportunity
                ),
                behavior_lookup_enabled=bool(not attention_check),
            )
            parsed = parse_proactive_response(
                response_text,
                self.emoji_list,
                force_reply=effective_force_reply,
            )
            llm_ok = bool(parsed.pop("_valid", True))
            style_switch = parsed.get("style_switch")
            if isinstance(style_switch, dict):
                self._apply_style_switch(group_id, cycle_id, style_switch)
            parsed.pop("style_switch", None)
            parsed = self._suppress_recent_proactive_repeats(
                group_id, parsed, effective_force_reply
            )

            parsed["_llm_ok"] = llm_ok
            result_to_return = parsed
            self._balance_warning_sent = False
        except Exception as exc:
            print(f"[LLM BATCH ERROR] {exc}")
            if is_insufficient_balance_error(exc):
                result_to_return = self._balance_response()

        result_to_return = _finish(result_to_return)
        if self.style_learner and not result_to_return.get("_balance_error"):
            self.style_learner.record_response_decision(
                group_id,
                batch_messages,
                result_to_return,
                force_reply=effective_force_reply,
                attention_check=attention_check,
            )
            self.style_learner.record_slang_usage(
                group_id,
                opportunity=bool(attention_check and slang_emotional_opportunity),
                messages=result_to_return.get("messages") or [],
                match_keys=self.style_learner.get_slang_match_keys(group_id),
            )
            self.style_learner.record_expression_usage(
                group_id,
                result_to_return.get("messages") or [],
            )
        if not result_to_return.get("_balance_error"):
            self._store_assistant_messages(group_id, result_to_return)
            if not attention_check:
                self._cache_proactive_context(group_id, cycle_id)
        return result_to_return

    @staticmethod
    def _reply_similarity_key(text):
        value = str(text or "").strip()
        return "".join(char for char in value if char.isalnum())

    @classmethod
    def _is_recent_similar_reply(cls, current, previous):
        current_key = cls._reply_similarity_key(current)
        previous_key = cls._reply_similarity_key(previous)
        if not current_key or not previous_key:
            return str(current or "").strip() == str(previous or "").strip()
        if current_key == previous_key:
            return True
        if min(len(current_key), len(previous_key)) < 5:
            return False
        return difflib.SequenceMatcher(None, current_key, previous_key).ratio() >= 0.9

    def _suppress_recent_proactive_repeats(self, group_id, result, force_reply):
        if force_reply or self.memory_manager is None:
            return result
        messages = [str(item or "").strip() for item in (result.get("messages") or []) if str(item or "").strip()]
        if not messages:
            return result
        history = self.memory_manager.get_group_messages(group_id)
        recent_bot_messages = [
            str(item.get("content") or "").strip()
            for item in (history or [])
            if isinstance(item, dict)
            and (
                item.get("is_bot")
                or str(item.get("role") or "").strip() == "assistant"
                or str(item.get("nickname") or "").strip() == self.assistant_nickname
            )
            and str(item.get("content") or "").strip()
        ][-8:]
        if not recent_bot_messages:
            return result
        kept = [
            item for item in messages
            if not any(self._is_recent_similar_reply(item, previous) for previous in recent_bot_messages)
        ]
        if len(kept) == len(messages):
            return result
        result["messages"] = kept
        if not kept and not result.get("animation"):
            result["should_reply"] = False
            result["reply_to"] = []
        print("[LLM BATCH] suppressed recent similar reply", flush=True)
        return result

    @staticmethod
    def _assistant_message_dict(message):
        """Serialize an SDK message in the format required by the next call."""
        if hasattr(message, "model_dump"):
            raw = message.model_dump(exclude_none=False)
            result = dict(raw) if isinstance(raw, dict) else {"role": "assistant"}
            result["role"] = "assistant"
            if result.get("tool_calls"):
                result["tool_calls"] = [
                    LLMService._tool_call_dict(item)
                    for item in result["tool_calls"]
                ]
            return result

        result = {"role": "assistant", "content": getattr(message, "content", None)}
        content = getattr(message, "content", None)
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            result["tool_calls"] = [LLMService._tool_call_dict(item) for item in tool_calls]
        return result

    @staticmethod
    def _tool_call_dict(tool_call):
        """Convert SDK/Pydantic tool-call objects to DeepSeek's JSON shape."""
        if isinstance(tool_call, dict):
            item = dict(tool_call)
            function = item.get("function") or {}
            if not isinstance(function, dict):
                function = {
                    "name": getattr(function, "name", ""),
                    "arguments": getattr(function, "arguments", "{}"),
                }
            item["type"] = item.get("type") or "function"
            item["function"] = {
                "name": str(function.get("name") or ""),
                "arguments": function.get("arguments")
                if isinstance(function.get("arguments"), str)
                else json.dumps(function.get("arguments") or {}, ensure_ascii=False, separators=(",", ":")),
            }
            return item
        function = getattr(tool_call, "function", None)
        arguments = getattr(function, "arguments", "{}")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments or {}, ensure_ascii=False, separators=(",", ":"))
        return {
            "id": str(getattr(tool_call, "id", "") or ""),
            "type": "function",
            "function": {
                "name": str(getattr(function, "name", "") or ""),
                "arguments": arguments,
            },
        }

    def _store_assistant_messages(self, group_id, result):
        if self.memory_manager is None:
            return

        try:
            messages = result.get("messages") or []
            sent_reply = bool(messages or result.get("animation"))
            for item in messages:
                content = str(item or "").strip()
                if not content:
                    continue

                self.memory_manager.add_llm_message(
                    group_id,
                    self.assistant_nickname,
                    content
                )
            if sent_reply:
                self.proactive_reply.record_llm_reply(group_id)
            self._maybe_compress_context(group_id)
        except Exception:
            return
