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
from llm.learning.message_units import build_learning_units
from llm.learning.profile_store import _is_generic_slang_phrase
from llm.conversation_pulse import ConversationPulse
from llm.proactive_reply import ProactiveReplyManager
from llm.prompt import (
    build_batch_user_messages,
    build_context_compression_messages,
    build_memory_curation_messages,
    build_system_prompt,
    build_user_messages,
    group_message_identity,
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
    IMAGE_MESSAGE_TOOL,
    BEHAVIOR_LOOKUP_TOOL,
    TIABA_HOT_TOOL,
    WEB_FETCH_TOOL,
    execute_tool,
    fetch_tieba_hot_post,
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
        # Per group/flow anchor for the immutable history record that is emitted
        # as the Responses cache checkpoint. It intentionally lives only for the
        # process lifetime: after restart one request writes a fresh cache, while
        # later incremental requests preserve and reuse the same boundary.
        self._history_cache_breakpoints = {}
        self._history_cache_breakpoints_lock = threading.RLock()
        # Idle content is fetched into a small shared cache only after a real
        # cycle boundary. It is not a global send quota: each group still
        # independently decides whether it has earned a share opportunity.
        self._idle_content_prefetch_lock = threading.RLock()
        self._idle_content_prefetch_running = False
        self._idle_content_prefetch_started_at = 0.0
        self._balance_warning_sent = False
        self.assistant_nickname = self.config.get("assistant_nickname", "LLM")
        self.emoji_list = []

        try:
            build_emoji_index(self.config.get("emoji_dir"))
            self.emoji_list = get_emoji_list()
        except Exception:
            self.emoji_list = []

        self._proactive_callback = None
        # The default proactive manager is created before the learner below.
        # Keep the attribute available so manager construction is safe during startup.
        self.style_learner = None
        self._proactive_managers = {}
        self.proactive_reply = self._get_proactive_manager("")
        self._conversation_pulses = {}
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
            for manager in self._proactive_managers.values():
                manager.set_reply_timing_reader(
                    self.style_learner.get_response_learning
                )
                manager.set_activity_profile_reader(
                    self.style_learner.get_activity_profile
                )

    def _group_profile(self, group_id):
        profiles = self.config.get("group_profiles") or {}
        if not isinstance(profiles, dict):
            return {}
        key = str(group_id or "").strip()
        profile = profiles.get(key)
        if not isinstance(profile, dict):
            profile = profiles.get("default")
        return dict(profile) if isinstance(profile, dict) else {}

    def _effective_group_config(self, group_id):
        """Return the global LLM config with only group-local persona/timing overrides."""
        effective = dict(self.config)
        profile = self._group_profile(group_id)
        for key in (
            "identity", "reply_style", "behavior_style", "group_prompt",
            "private_prompt", "prefixes", "prompt", "proactive",
        ):
            if key in profile:
                value = profile.get(key)
                effective[key] = dict(value) if isinstance(value, dict) else value
        if "auto_reply" in profile:
            effective["auto_reply"] = dict(profile.get("auto_reply") or {})
        return effective

    @staticmethod
    def _persona_guidance(profile):
        profile = profile if isinstance(profile, dict) else {}
        parts = []
        labels = (
            ("Reply style", "reply_style"),
            ("Behavior guidance", "behavior_style"),
            ("Group context", "group_prompt"),
            ("Private context", "private_prompt"),
        )
        for label, key in labels:
            value = str(profile.get(key) or "").strip()
            if value:
                parts.append(f"{label}: {value}")
        return "\n".join(parts)

    def _get_proactive_manager(self, group_id):
        key = str(group_id or "").strip() or "__default__"
        manager = self._proactive_managers.get(key)
        if manager is not None:
            return manager
        effective = self._effective_group_config(group_id)
        if str(group_id or "").strip():
            auto_reply = dict(effective.get("auto_reply") or {})
            auto_reply["allowed_groups"] = [str(group_id).strip()]
            effective["auto_reply"] = auto_reply
            effective["target_groups"] = [str(group_id).strip()]
        manager = ProactiveReplyManager(effective)
        if self._proactive_callback:
            manager.set_batch_callback(self._proactive_callback)
        if self.style_learner:
            manager.set_reply_timing_reader(self.style_learner.get_response_learning)
            manager.set_activity_profile_reader(self.style_learner.get_activity_profile)
        self._proactive_managers[key] = manager
        return manager

    def _conversation_pulse_for(self, group_id):
        key = str(group_id or "").strip() or "__default__"
        pulse = self._conversation_pulses.get(key)
        if pulse is None:
            profile = self._group_profile(group_id)
            settings = profile.get("auto_reply") if isinstance(profile, dict) else None
            pulse = ConversationPulse(settings or self.config.get("auto_reply") or {})
            self._conversation_pulses[key] = pulse
        return pulse

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
    def _extract_local_slang_candidates(learning_units, known_phrases=None, max_candidates=30, max_messages=150):
        """Find frequent candidates from complete, boundary-verified learning units.

        A raw WeChat delivery may split one sentence into several events. Only
        units explicitly marked ``candidate_allowed`` are considered here, so a
        dangling half sentence can remain curation context without becoming a
        slang candidate. Longest-first dedup prevents a full phrase from creating fake substring terms.
        """
        units = [
            item for item in (learning_units or [])
            if isinstance(item, dict) and item.get("candidate_allowed") and item.get("complete")
        ]
        if not units:
            return []
        if len(units) > max(1, int(max_messages or 1)):
            units = units[-max(1, int(max_messages or 1)):]
        known = {
            LLMService._slang_evidence_key(phrase)
            for phrase in (known_phrases or [])
            if len(LLMService._slang_evidence_key(phrase)) >= 2
        }
        counts = {}
        for index, item in enumerate(units):
            content = str(item.get("content") or "").strip()
            if (
                not content or content.startswith("/")
                or re.search(r"https?://", content, flags=re.IGNORECASE)
                or content.isdigit()
            ):
                continue
            normalized = LLMService._slang_evidence_key(content)
            if len(normalized) < 2:
                continue
            source_ids = [str(source).strip() for source in (item.get("source_ids") or []) if str(source).strip()]
            source_id = source_ids[0] if source_ids else f"_unit{index}"
            speaker = str(item.get("speaker_id") or "").strip()[:120]
            for size in range(2, min(6, len(normalized)) + 1):
                for offset in range(0, len(normalized) - size + 1):
                    gram = normalized[offset:offset + size]
                    if gram.isdigit() or len(set(gram)) == 1:
                        continue
                    entry = counts.setdefault(gram, {"messages": set(), "speakers": set(), "source_ids": set()})
                    entry["messages"].add(source_id)
                    entry["source_ids"].update(source_ids or [source_id])
                    if speaker:
                        entry["speakers"].add(speaker)

        def usable(gram):
            return not (
                _is_generic_slang_phrase(gram)
                or gram in known
                or any(len(existing) > len(gram) and gram in existing for existing in known)
            )

        passing = [
            gram for gram, entry in counts.items()
            if (len(entry["messages"]) >= 2 or len(entry["speakers"]) >= 2) and usable(gram)
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
                "source_ids": sorted(counts[gram]["source_ids"])[:8],
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

    def _ground_slang_action_in_cycle(self, action, learning_units):
        """Ground slang writes only in complete, local learning units.

        ``cycle_messages`` still give the curator conversational context, but
        incomplete units never qualify as phrase evidence. This prevents a
        model from learning an accidental prefix or a phrase created by joining
        two unrelated deliveries.
        """
        if not isinstance(action, dict):
            return None, "invalid_action"
        phrase = str(action.get("phrase") or action.get("normalized_phrase") or "").strip()
        phrase_key = self._slang_evidence_key(phrase)
        if len(phrase_key) < 2:
            return None, "missing_phrase"
        sources, matches = {}, []
        for unit in learning_units or []:
            if not isinstance(unit, dict) or not unit.get("complete") or not unit.get("candidate_allowed"):
                continue
            content = str(unit.get("content") or "").strip()
            if not content or phrase_key not in self._slang_evidence_key(content):
                continue
            speaker = str(unit.get("speaker_id") or "").strip()[:120]
            for source_id in unit.get("source_ids") or []:
                source_id = str(source_id).strip()
                if not source_id:
                    continue
                sources[source_id] = {"content": content, "speaker": speaker}
                matches.append(source_id)
        matches = list(dict.fromkeys(matches))
        requested = action.get("source_ids")
        if isinstance(requested, str):
            requested = [requested]
        requested = list(dict.fromkeys(str(item).strip() for item in (requested or []) if str(item).strip()))[:8]
        if requested:
            source_ids = [source_id for source_id in requested if source_id in matches]
            if not source_ids:
                return None, "source_not_in_complete_learning_unit"
        elif matches:
            source_ids = matches[:3]
        else:
            return None, "phrase_not_in_complete_learning_unit"
        source_speakers = list(dict.fromkeys(
            sources[source_id]["speaker"] for source_id in source_ids
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
        """Recall literal hits first, then a tiny scene-matched safe pool.

        This stays fully local (keywords/scenes/examples/meaning) rather than
        introducing a vector dependency.  A scene candidate is only a reference:
        the model may use none of them, and only active-use slang is eligible.
        """
        if not self.style_learner:
            return "[]"
        try:
            store = self.style_learner.store
            literal = store.get_context_slang(group_id, messages, max_items=6, max_chars=900)
            topic = " ".join(
                str(item.get("content") or "")
                for item in (messages or [])[-12:]
                if isinstance(item, dict) and not item.get("is_bot") and item.get("role") != "assistant"
            )
            intent = "question" if "?" in topic or "?" in topic else "statement"
            scene = store.get_slang_scene_candidates(
                group_id, topic=topic, intent=intent, max_items=2, max_chars=360
            ) if topic.strip() else []
            seen = {str(item.get("normalized_phrase") or item.get("phrase") or "") for item in literal if isinstance(item, dict)}
            candidates = list(literal)
            for item in scene:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("normalized_phrase") or item.get("phrase") or "")
                if not key or key in seen:
                    continue
                enriched = dict(item)
                enriched["recall"] = "scene"
                candidates.append(enriched)
                seen.add(key)
        except Exception:
            candidates = []
        return json.dumps(candidates[:8], ensure_ascii=False, separators=(",", ":"))

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
        # A second selector request is costly and makes the per-reply prompt
        # less predictable. The local pool already ranks by situation, recent
        # use and frequency; keep an optional selector for installations that
        # explicitly want it, but default to the deterministic local path.
        selector_enabled = bool((self.config.get("learning") or {}).get("expression_selector_enabled", False))
        selected = None
        if selector_enabled and len(pool) > inject_limit:
            selected = self._select_expression_candidates(group_id, messages or [], pool, inject_limit)
        result = selected if selected is not None else pool[:inject_limit]
        if result:
            try:
                self.style_learner.record_expression_selection(
                    group_id,
                    [item.get("situation") for item in result if isinstance(item, dict)],
                )
            except Exception:
                pass
        return result

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

    def _clear_history_cache_breakpoints(self, group_id, namespaces=None):
        """Forget cache anchors whose stable pre-history context was replaced.

        A cycle curation can change long/medium/short memory, and context
        compression can replace the stored transcript. In either case an old
        anchor is no longer the longest reusable prefix. The next request writes
        one fresh anchor at that cycle's first history tail.
        """
        group_key = str(group_id or "global")
        wanted = {
            str(value or "").strip().lower()
            for value in (namespaces or [])
            if str(value or "").strip()
        }
        with self._history_cache_breakpoints_lock:
            for key in list(self._history_cache_breakpoints):
                flow, stored_group = key
                if stored_group != group_key:
                    continue
                if wanted and flow not in wanted:
                    continue
                self._history_cache_breakpoints.pop(key, None)

    def _history_cache_breakpoint_id(self, group_id, messages, namespace, cycle_id=None):
        """Return the immutable history tail selected for this group cycle.

        The first request in a cycle anchors its full stable memory + accumulated
        history prefix. Later requests append records after that anchor, so
        Responses automatic caching sees a byte-identical prefix. A named new
        cycle deliberately gets a new anchor because cycle-scoped memory/style
        state may have changed before the next request.
        """
        records = [item for item in (messages or []) if isinstance(item, dict)]
        if not records:
            return ""
        flow = str(namespace or "chat").strip().lower() or "chat"
        key = (flow, str(group_id or "global"))
        current_cycle = str(cycle_id or "").strip()
        available = {group_message_identity(item) for item in records}
        with self._history_cache_breakpoints_lock:
            current = self._history_cache_breakpoints.get(key)
            # Backwards-compatible handling for anchors stored by older process
            # code. New entries carry both id and cycle id.
            if isinstance(current, str):
                current = {"id": current, "cycle_id": ""}
            if isinstance(current, dict):
                current_id = str(current.get("id") or "")
                previous_cycle = str(current.get("cycle_id") or "")
                if current_id in available and (not current_cycle or previous_cycle == current_cycle):
                    return current_id
            selected = group_message_identity(records[-1])
            self._history_cache_breakpoints[key] = {
                "id": selected,
                "cycle_id": current_cycle,
            }
            return selected

    @staticmethod
    def _responses_prompt_cache_key(group_id, namespace):
        """Return a non-sensitive stable cache-routing key for Responses APIs."""
        flow = re.sub(r"[^a-z0-9_-]+", "-", str(namespace or "chat").lower()).strip("-") or "chat"
        digest = hashlib.sha256(f"{flow}|{group_id or 'global'}".encode("utf-8")).hexdigest()[:20]
        return f"wechat:{flow}:{digest}"

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
            effective_config = self._effective_group_config(group_id)
            response = self._complete_with_tools(
                [
                    {"role": "system", "content": build_system_prompt(
                        effective_config.get("prompt") or {},
                        identity=effective_config.get("identity") or {},
                        prefixes=effective_config.get("prefixes") or [],
                        persona_guidance=self._persona_guidance(effective_config),
                    )},
                ] + build_context_compression_messages({
                    "identity": effective_config.get("identity") or {},
                    "prefixes": effective_config.get("prefixes") or [],
                    "group_messages": messages,
                    **state,
                }),
                current_session_id=session_id,
                memory_group_id=group_id,
                cache_namespace="context-compression",
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
            self._clear_history_cache_breakpoints(group_id)
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
        effective_config = self._effective_group_config(group_id)
        system_prompt = build_system_prompt(
            effective_config.get("prompt") or {},
            identity=effective_config.get("identity") or {},
            prefixes=effective_config.get("prefixes") or [],
            persona_guidance=self._persona_guidance(effective_config),
        )
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
                cache_namespace="memory-curation",
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

    @staticmethod
    def _learning_cursor_key(item, index=0):
        """Return a conservative ordering key for persisted cycle progress."""
        item = item if isinstance(item, dict) else {}
        try:
            timestamp = int(float(item.get("timestamp") or 0))
        except (TypeError, ValueError):
            timestamp = 0
        source_id = str(item.get("source_id") or "").strip()
        if not source_id:
            source_id = group_message_identity(item)
        return timestamp, source_id or f"unidentified-{index}"

    @classmethod
    def _messages_after_learning_cursor(cls, messages, cursor):
        """Keep only unseen cycle evidence while retaining unorderable events.

        Timestamps are the primary cursor because WeFlow IDs are not guaranteed
        to be globally sortable. For a same-timestamp tie we use the stable
        source id when available; unknown timestamps are kept rather than
        risking loss of valid learning material.
        """
        cursor = cursor if isinstance(cursor, dict) else {}
        try:
            cursor_timestamp = int(cursor.get("cursor_timestamp") or 0)
        except (TypeError, ValueError):
            cursor_timestamp = 0
        cursor_source = str(cursor.get("cursor_source_id") or "").strip()
        if cursor_timestamp <= 0:
            return [item for item in (messages or []) if isinstance(item, dict)]
        filtered = []
        for index, item in enumerate(messages or []):
            if not isinstance(item, dict):
                continue
            timestamp, source_id = cls._learning_cursor_key(item, index)
            if timestamp <= 0 or timestamp > cursor_timestamp:
                filtered.append(item)
                continue
            if timestamp < cursor_timestamp:
                continue
            if not cursor_source:
                filtered.append(item)
                continue
            if source_id == cursor_source:
                continue
            # Numeric source IDs are normally monotonic. Other IDs may be
            # opaque, so retain them at a shared timestamp to avoid loss.
            if source_id.isdigit() and cursor_source.isdigit():
                if int(source_id) > int(cursor_source):
                    filtered.append(item)
            elif not source_id.startswith("unidentified-"):
                filtered.append(item)
        return filtered

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
        context = context if isinstance(context, dict) else {}
        cycle_messages = [item for item in (cycle_messages or []) if isinstance(item, dict)]
        curation_outbox_ids = []
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
        # Keep the incoming cycle separate from recovered outbox work. A failed
        # curation may be retried together with later cycles, but group-rhythm
        # statistics must count each named cycle exactly once rather than count
        # every recovered message again under the newest cycle ID.
        activity_cycle_messages = list(cycle_messages)
        if self.style_learner and cycle_messages:
            # The outbox remains the recovery source for provider failures;
            # the cursor simply suppresses already-curated history when an
            # upstream cycle callback is replayed.
            cursor = self.style_learner.get_learning_cursor(group_id)
            before_cursor_filter = len(cycle_messages)
            cycle_messages = self._messages_after_learning_cursor(cycle_messages, cursor)
            if len(cycle_messages) < before_cursor_filter:
                print(
                    f"[LLM CYCLE CURATION] group={group_id} cursor_filtered="
                    f"{before_cursor_filter - len(cycle_messages)}",
                    flush=True,
                )
        # Persist the full cycle before asking an LLM.  The next cycle can
        # reclaim this job after a provider error or process restart instead of
        # dropping the material.  Old jobs are merged only at this boundary;
        # they are never used to stitch incomplete message fragments together.
        if self.style_learner and cycle_messages:
            cycle_id = str(context.get("cycle_id") or context.get("_cycle_id") or "").strip()
            identity = cycle_id or hashlib.sha1(
                json.dumps(cycle_messages, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8", errors="ignore")
            ).hexdigest()[:24]
            payload = {
                "context": {key: context.get(key) for key in ("cycle_id", "_cycle_id", "_cycle_end_at", "sessionId", "wxid")},
                "cycle_messages": cycle_messages,
            }
            self.style_learner.store.enqueue_outbox(
                event_key=f"cycle_curation:{group_id}:{identity}",
                group_id=group_id,
                kind="cycle_curation",
                payload=payload,
            )
            pending_jobs = self.style_learner.store.claim_outbox(
                kinds=["cycle_curation"], group_id=group_id, limit=3, lease_seconds=600
            )
            combined, seen = [], set()
            for job in pending_jobs:
                try:
                    job_payload = json.loads(job.get("payload_json") or "{}")
                    job_messages = job_payload.get("cycle_messages") if isinstance(job_payload, dict) else []
                    if not isinstance(job_messages, list):
                        job_messages = []
                    for index, message in enumerate(job_messages):
                        if not isinstance(message, dict):
                            continue
                        source = next((str(message.get(key) or "").strip() for key in ("message_id", "local_id", "server_id", "source_id") if str(message.get(key) or "").strip()), "")
                        dedupe = source or hashlib.sha1(json.dumps(message, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8", errors="ignore")).hexdigest()
                        if dedupe in seen:
                            continue
                        seen.add(dedupe)
                        combined.append(message)
                    curation_outbox_ids.append(int(job["id"]))
                except Exception as exc:
                    self.style_learner.store.fail_outbox(job["id"], repr(exc), max_attempts=8, retry_delay=30)
            if combined:
                cycle_messages = sorted(combined, key=lambda item: int(item.get("timestamp") or 0))
        if not cycle_messages:
            return False
        if self.style_learner:
            # This is local, cycle-bound learning. Do it before provider
            # initialization/backoff so a failed curation request cannot drop
            # the group's observed rhythm. The store makes named cycles idempotent.
            try:
                self.style_learner.update_cycle_activity(
                    group_id,
                    activity_cycle_messages,
                    cycle_end_at=context.get("_cycle_end_at"),
                    cycle_id=context.get("cycle_id") or context.get("_cycle_id"),
                )
            except Exception as exc:
                print(f"[LLM ACTIVITY PROFILE ERROR] group={group_id} {exc}", flush=True)
        if time.time() < self._memory_curator_backoff_until:
            print(f"[LLM CYCLE CURATION] backoff group={group_id}", flush=True)
            return False
        if not self._memory_curator_lock.acquire(blocking=False):
            return False
        try:
            if self.style_learner:
                self.style_learner.resolve_slang_usage_feedback(group_id)
            if self.long_term_memory and self.long_term_memory.enabled:
                # Legacy state or abnormal writes can leave short memory over budget.
                # If the hard limit changes the stable memory head, invalidate the
                # history anchor so the next Responses call builds a byte-stable prefix.
                truncated = self.long_term_memory.enforce_short_memory_budget(group_id)
                if truncated:
                    self._clear_history_cache_breakpoints(group_id)
            self._ensure_provider()
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
            learning_units = build_learning_units(prompt_cycle_messages)
            complete_learning_units = [
                item for item in learning_units
                if item.get("complete") and item.get("candidate_allowed")
            ]
            local_slang_candidates = self._extract_local_slang_candidates(
                complete_learning_units,
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
            effective_config = self._effective_group_config(group_id)
            prompt_data = {
                "identity": effective_config.get("identity") or {},
                "prefixes": effective_config.get("prefixes") or [],
                "cycle_messages": prompt_cycle_messages,
                # These units are the *only* valid evidence source for new
                # slang. Raw cycle messages remain available as context for
                # memory/style decisions, including unresolved fragments.
                "learning_units": learning_units,
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
                        {"role": "system", "content": build_system_prompt(
                        effective_config.get("prompt") or {},
                        identity=effective_config.get("identity") or {},
                        prefixes=effective_config.get("prefixes") or [],
                        persona_guidance=self._persona_guidance(effective_config),
                    )},
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
                action, reason = self._ground_slang_action_in_cycle(raw_action, learning_units)
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
                self._cycle_context_cache.pop(group_id, None)
            # A successful cycle curation is the explicit stable-prefix boundary.
            # New requests in the next cycle create fresh history anchors after
            # any memory compaction or updates have been applied.
            self._clear_history_cache_breakpoints(
                group_id,
                namespaces=("direct-reply", "proactive-reply"),
            )
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
            if self.style_learner:
                for outbox_id in curation_outbox_ids:
                    self.style_learner.store.ack_outbox(outbox_id)
                last_item = max(
                    user_cycle_messages,
                    key=self._learning_cursor_key,
                    default={},
                )
                if last_item:
                    cursor_timestamp, cursor_source = self._learning_cursor_key(last_item)
                    self.style_learner.advance_learning_cursor(
                        group_id,
                        timestamp=cursor_timestamp,
                        source_id=cursor_source,
                        cycle_id=context.get("cycle_id") or context.get("_cycle_id") or "",
                    )
            # Fetching only fills a source-backed cache for future eligible
            # idle windows. It is asynchronous and never sends a group message.
            self._schedule_idle_content_prefetch(group_id)
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
                for outbox_id in curation_outbox_ids:
                    # Curation is LLM-dependent. Preserve the cycle until it
                    # succeeds instead of eventually turning a provider outage
                    # into a permanently dead learning task.
                    self.style_learner.store.fail_outbox(
                        outbox_id, str(exc), max_attempts=None, retry_delay=30
                    )
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
        self._proactive_callback = callback if callable(callback) else None
        for manager in self._proactive_managers.values():
            manager.set_batch_callback(self._proactive_callback)

    def handle_proactive_message(self, context):
        group_id = (context or {}).get("group") or (context or {}).get("sessionId") or ""
        return self._get_proactive_manager(group_id).handle_message(context)

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
        group_id = ""
        if isinstance(context, dict):
            group_id = context.get("group") or context.get("group_id") or context.get("target") or ""
        manager = self._get_proactive_manager(group_id)
        manager.on_llm_result(
            context,
            result,
            attention_check=attention_check,
        )
        # The reply was generated for a real group batch.  Remember its sender
        # briefly so a natural follow-up is never rejected by autonomous
        # frequency scoring before the model can see the context.
        if not attention_check:
            manager.record_conversation_reply(context, result)

    def _get_active_style_switch(self, group_id):
        """Read the current cycle's LLM-chosen style selection for injection."""
        try:
            return self._get_proactive_manager(group_id).get_active_style_switch(group_id)
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
        manager = self._get_proactive_manager(group_id)
        if manager is None or not isinstance(switch, dict):
            return False
        learning = self.config.get("learning") or {}
        if not bool(learning.get("style_switch_enabled", True)):
            return False
        action = str(switch.get("action") or "set").lower()
        if action == "clear":
            return self._get_proactive_manager(group_id).set_active_style_switch(group_id, cycle_id, None)
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
        return self._get_proactive_manager(group_id).set_active_style_switch(group_id, cycle_id, selection)

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
            # Inject a tiny locally-ranked pool. It appears after immutable
            # history in the prompt, therefore a changing expression choice
            # cannot invalidate the cycle's cached memory/history prefix.
            expression_list = self._get_context_expressions(
                group_id,
                group_messages,
                max_items=max(1, min(int((self.config.get("learning") or {}).get("expression_prompt_max_items", 3) or 3), 4)),
            )
            memory_state = self._get_group_memory_state(group_id)
            style_context = self.style_learner.get_prompt_context(group_id) if self.style_learner else ""
            emoji_list = list(self.emoji_list)
            style_profile = (
                self.style_learner.get_prompt_context(group_id)
                if self.style_learner else ""
            )
            active_style_switch = self._get_active_style_switch(group_id)

            effective_config = self._effective_group_config(group_id)
            system_prompt = build_system_prompt(
                effective_config.get("prompt") or {},
                identity=effective_config.get("identity") or {},
                prefixes=effective_config.get("prefixes") or [],
                persona_guidance=self._persona_guidance(effective_config),
            )
            prompt_data = {
                "group_messages": group_messages,
                "history_cache_breakpoint_id": self._history_cache_breakpoint_id(
                    group_id,
                    group_messages,
                    "direct-reply",
                    cycle_id=current_message.get("_cycle_id"),
                ),
                "emoji_list": emoji_list,
                "identity": effective_config.get("identity") or {},
                "llm_config": effective_config or {},
                "prefixes": effective_config.get("prefixes") or [],
                "prompt": effective_config.get("prompt") or {},
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
                cache_namespace="direct-reply",
            )
            parsed = parse_llm_response(response_text, emoji_list)
            style_switch = parsed.get("style_switch")
            if isinstance(style_switch, dict):
                self._apply_style_switch(group_id, current_message.get("_cycle_id"), style_switch)
            parsed.pop("style_switch", None)
            if force_reply and not parsed.get("messages") and not parsed.get("animation"):
                fallback_message = str(
                    (effective_config.get("prompt") or {}).get("fallback_message")
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

    def _idle_content_cache_settings(self):
        learning = self.config.get("learning") or {}
        try:
            max_age = max(15 * 60, int(learning.get("idle_content_cache_max_age_seconds", 12 * 3600)))
        except (TypeError, ValueError):
            max_age = 12 * 3600
        try:
            prefetch_interval = max(5 * 60, int(learning.get("idle_content_prefetch_min_interval_seconds", 30 * 60)))
        except (TypeError, ValueError):
            prefetch_interval = 30 * 60
        try:
            min_items = max(1, min(12, int(learning.get("idle_content_prefetch_min_items", 6))))
        except (TypeError, ValueError):
            min_items = 6
        return max_age, prefetch_interval, min_items

    def _cache_idle_content_posts(self, payload, *, source="tieba"):
        """Persist de-duplicated, source-backed idle-content candidates."""
        if not self.style_learner or not isinstance(payload, dict):
            return 0
        posts = payload.get("posts") or payload.get("records") or []
        if not isinstance(posts, list):
            return 0
        cached = 0
        for post in posts:
            if not isinstance(post, dict):
                continue
            title = str(post.get("title") or "").strip()
            excerpt = str(post.get("excerpt") or post.get("content") or "").strip()
            if not (title or excerpt):
                continue
            digest = hashlib.sha1(
                (title + "\n" + excerpt).casefold().encode("utf-8", errors="ignore")
            ).hexdigest()
            if self.style_learner.cache_idle_content(
                content_hash=digest,
                source=source,
                title=title,
                content=excerpt or title,
                url=str(post.get("url") or ""),
                metadata={"reply_count": post.get("reply_count")},
            ):
                cached += 1
        return cached

    def _prefetch_idle_content(self):
        """Fetch a small candidate pool outside reply/curation critical paths."""
        try:
            result = fetch_tieba_hot_post(max_items=6, timeout=10.0)
            cached = self._cache_idle_content_posts(result, source="tieba")
            if cached:
                print(f"[LLM IDLE CONTENT CACHE] refreshed={cached}", flush=True)
            elif isinstance(result, dict) and not result.get("ok", True):
                print(f"[LLM IDLE CONTENT CACHE] refresh skipped: {str(result.get('error') or 'unavailable')[:160]}", flush=True)
        except Exception as exc:
            print(f"[LLM IDLE CONTENT CACHE] refresh failed: {exc}", flush=True)
        finally:
            with self._idle_content_prefetch_lock:
                self._idle_content_prefetch_running = False

    def _schedule_idle_content_prefetch(self, group_id=None):
        """Refresh after a cycle without adding a learner timer or LLM call."""
        effective_auto_reply = self._effective_group_config(group_id).get("auto_reply") or {}
        if not self.style_learner or not bool(effective_auto_reply.get("adaptive_idle_content_enabled", True)):
            return False
        max_age, interval, min_items = self._idle_content_cache_settings()
        try:
            fresh = self.style_learner.get_idle_content_pool(
                limit=min_items, max_age_seconds=max_age
            )
        except Exception:
            fresh = []
        now = time.time()
        with self._idle_content_prefetch_lock:
            if self._idle_content_prefetch_running or len(fresh) >= min_items:
                return False
            if now - self._idle_content_prefetch_started_at < interval:
                return False
            self._idle_content_prefetch_running = True
            self._idle_content_prefetch_started_at = now
        threading.Thread(
            target=self._prefetch_idle_content,
            daemon=True,
            name="llm-idle-content-prefetch",
        ).start()
        return True

    @staticmethod
    def _idle_content_plan_is_allowed(plan, candidates) -> bool:
        """A share must name one candidate injected into this exact request."""
        if not isinstance(plan, dict) or str(plan.get("action") or "").strip().lower() != "share":
            return False
        selected = str(plan.get("content_hash") or "").strip()
        if not selected:
            return False
        allowed = {
            str(item.get("content_hash") or "").strip()
            for item in (candidates or [])
            if isinstance(item, dict) and str(item.get("content_hash") or "").strip()
        }
        return selected in allowed

    def _idle_content_candidates(self, group_id, limit=4):
        """Return only compact, fresh, de-duplicated content for an idle planner."""
        if not self.style_learner or not group_id:
            return []
        max_age, _, _ = self._idle_content_cache_settings()
        rows = self.style_learner.get_idle_content_pool(
            limit=limit,
            max_age_seconds=max_age,
        )
        candidates = []
        for row in rows:
            content_hash = str(row.get("content_hash") or "").strip()
            content = str(row.get("content") or "").strip()
            if not content_hash or not content:
                continue
            candidates.append({
                "content_hash": content_hash[:80],
                "title": str(row.get("title") or "")[:180],
                "excerpt": content[:320],
                "source": str(row.get("source") or "")[:40],
            })
        return candidates

    def _complete_with_tools(self, messages, allowed_hosts=None, current_session_id=None, memory_group_id=None, allow_memory_update=False, memory_observations=None, tieba_opportunity=False, behavior_lookup_enabled=False, cache_namespace="generic"):
        """Run DeepSeek's assistant/tool/assistant loop and return final JSON."""
        tools = []
        if self.config.get("web_fetch_enabled", True):
            tools.append(WEB_FETCH_TOOL)
            if tieba_opportunity:
                tools.append(TIABA_HOT_TOOL)
        if self.config.get("original_message_enabled", True) and current_session_id:
            tools.append(ORIGINAL_MESSAGE_TOOL)
        if (
            self.config.get("image_tool_enabled", True)
            and current_session_id
            and bool(getattr(self.provider, "supports_images", False))
        ):
            tools.append(IMAGE_MESSAGE_TOOL)
        if memory_group_id and self.style_learner:
            tools.append(SLANG_LOOKUP_TOOL)
            tools.append(SLANG_SIMILAR_LOOKUP_TOOL)
            tools.append(EXPRESSION_LOOKUP_TOOL)
            if behavior_lookup_enabled:
                tools.append(BEHAVIOR_LOOKUP_TOOL)
        if memory_group_id and self.long_term_memory and self.long_term_memory.enabled:
            tools.append(MEMORY_LOOKUP_TOOL)
        prompt_cache_key = self._responses_prompt_cache_key(memory_group_id, cache_namespace)
        if not tools:
            return self.provider.send(messages, prompt_cache_key=prompt_cache_key)

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
        image_calls_used = 0
        image_references = []
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
        try:
            image_max_calls = int(self.config.get("image_tool_max_calls", 1) or 1)
        except (TypeError, ValueError):
            image_max_calls = 1
        image_max_calls = max(1, min(image_max_calls, 3))
        while True:
            if time.monotonic() >= loop_deadline:
                print("[LLM TOOL LOOP TIMEOUT] final response skipped after deadline", flush=True)
                return ""
            assistant_message = self.provider.send_chat(
                conversation,
                tools=tools,
                prompt_cache_key=prompt_cache_key,
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
                if name == IMAGE_MESSAGE_TOOL["function"]["name"]:
                    if image_calls_used >= image_max_calls:
                        conversation.append({
                            "role": "tool",
                            "tool_call_id": getattr(tool_call, "id", ""),
                            "content": '{"ok":false,"error":"image lookup limit reached"}',
                        })
                        tool_calls_used += 1
                        continue
                    image_calls_used += 1
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
                    image_max_bytes=self.config.get("image_tool_max_bytes", 8 * 1024 * 1024),
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
                if name == TIABA_HOT_TOOL["function"]["name"]:
                    try:
                        self._cache_idle_content_posts(load_json_lenient(content), source="tieba")
                    except Exception:
                        pass

                safe_content = content
                if name == IMAGE_MESSAGE_TOOL["function"]["name"]:
                    try:
                        image_payload = json.loads(content)
                    except (TypeError, ValueError):
                        image_payload = None
                    if isinstance(image_payload, dict):
                        image_data_url = str(image_payload.pop("_image_data_url", "") or "")
                        if image_data_url.startswith("data:image/"):
                            image_references.append(image_data_url)
                        safe_content = json.dumps(image_payload, ensure_ascii=False)

                conversation.append({
                    "role": "tool",
                    "tool_call_id": getattr(tool_call, "id", ""),
                    "content": safe_content,
                })
                tool_calls_used += 1

            if image_references:
                image_blocks = [{
                    "type": "text",
                    "text": "Image tool returned image content. Refer to it directly when answering.",
                }]
                image_blocks.extend(
                    {"type": "image_url", "image_url": {"url": image_url}}
                    for image_url in image_references[:3]
                )
                conversation.append({"role": "user", "content": image_blocks})
                image_references = []

            if tool_calls_used >= max_calls:
                # Do not leave the model in an unbounded tool-call loop. The
                # tool error (or the last successful result) is still included
                # in the context for one final JSON-only response.
                final_message = self.provider.send_chat(
                    conversation,
                    prompt_cache_key=prompt_cache_key,
                )
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
        idle_content_opportunity=False,
        idle_content_probability=0.0,
        idle_content_reason="",
        conversation_priority="MAY_REPLY",
    ):
        """Judge a ten-second message batch for proactive group replies."""
        conversation_priority = str(conversation_priority or "MAY_REPLY").upper()
        if conversation_priority not in {"MUST_REPLY", "SHOULD_REPLY", "UNCERTAIN_DIRECT", "MAY_REPLY", "DO_NOT_REPLY"}:
            conversation_priority = "MAY_REPLY"
        frequency_exempt = conversation_priority in {"MUST_REPLY", "SHOULD_REPLY", "UNCERTAIN_DIRECT"}
        effective_force_reply = bool(force_reply or conversation_priority == "MUST_REPLY")
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
            idle_content_opportunity = bool(idle_content_opportunity)
            if not batch_messages and not (proactive_web_opportunity or idle_content_opportunity):
                return _finish(result_to_return)

            attention_budget = int(
                (self._effective_group_config(group_id).get("auto_reply") or {}).get(
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
            # MUST_REPLY survives the later batch/url normalization.  It is an
            # explicit addressing signal, not an autonomous-frequency decision.
            effective_force_reply = bool(
                force_reply or url_only_present or conversation_priority == "MUST_REPLY"
            )
            result_to_return["should_reply"] = effective_force_reply

            # Local scheduling only decides whether this batch deserves the
            # one normal proactive LLM call. It does not author a reply: once
            # planned, the model receives the complete context and may still
            # stay silent or select an earlier topic.
            auto_settings = self._effective_group_config(group_id).get("auto_reply") or {}
            trigger_mode = str(auto_settings.get("reply_trigger_mode", "conversation_pulse") or "conversation_pulse").lower()
            if (
                not attention_check
                and not effective_force_reply
                and not frequency_exempt
                and not proactive_web_opportunity
                and not idle_content_opportunity
                and trigger_mode in {"reply_necessity", "conversation_pulse"}
            ):
                history = self.memory_manager.get_group_messages(group_id)
                timing_learning = self._get_proactive_manager(group_id).get_reply_timing_learning(group_id)
                pulse_decision, conversation_pulse = self._conversation_pulse_for(group_id).decide(
                    group_id,
                    batch_messages,
                    history,
                    timing_learning,
                    self._get_proactive_manager(group_id).seconds_since_llm_reply(group_id),
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
            no_llm_reply_seconds = self._get_proactive_manager(group_id).seconds_since_llm_reply(group_id)
            attention_boost = self._get_proactive_manager(group_id).attention_boost_active(group_id)
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
            # Inject a very small deterministic mix of context hits and
            # high-frequency group expressions. The model may ignore it; this
            # is a reference, not a forced stylistic template.
            expression_list = self._get_context_expressions(
                group_id, prompt_group_messages or batch_messages
            )

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

            idle_content_candidates = (
                self._idle_content_candidates(group_id)
                if idle_content_opportunity else []
            )
            idle_content_shadow_mode = bool((self.config.get("learning") or {}).get("idle_content_shadow_mode", True))
            effective_config = self._effective_group_config(group_id)
            prompt_config = effective_config.get("prompt") or {}
            system_prompt = build_system_prompt(
                prompt_config,
                identity=effective_config.get("identity") or {},
                prefixes=effective_config.get("prefixes") or [],
                persona_guidance=self._persona_guidance(effective_config),
            )
            prompt_data = {
                "batch_messages": batch_messages,
                "group_messages": prompt_group_messages,
                "history_cache_breakpoint_id": self._history_cache_breakpoint_id(
                    group_id,
                    prompt_group_messages,
                    "proactive-reply",
                    cycle_id=cycle_id,
                ),
                "force_reply": effective_force_reply,
                "conversation_priority": conversation_priority,
                "frequency_exempt": frequency_exempt,
                "trigger_source": trigger_source,
                "attention_check": attention_check,
                "no_llm_reply_seconds": no_llm_reply_seconds,
                "attention_boost": attention_boost,
                "nonsense_opportunity": bool(attention_check and nonsense_opportunity),
                "slang_emotional_opportunity": bool(attention_check and slang_emotional_opportunity),
                "slang_emotional_candidates": slang_emotional_candidates,
                "tieba_opportunity": bool(
                    (attention_check and tieba_opportunity)
                    or proactive_web_opportunity
                    or idle_content_opportunity
                ),
                "proactive_web_opportunity": proactive_web_opportunity,
                "idle_content_opportunity": idle_content_opportunity,
                "idle_content_probability": idle_content_probability,
                "idle_content_reason": idle_content_reason,
                "idle_content_candidates": idle_content_candidates,
                "idle_content_shadow_mode": idle_content_shadow_mode,
                "conversation_pulse": conversation_pulse,
                "slang_usage_guidance": (
                    self.style_learner.get_slang_usage_guidance(group_id)
                    if self.style_learner else ""
                ),
                **cycle_context,
                **memory_state,
                "emoji_list": self.emoji_list,
                "identity": effective_config.get("identity") or {},
                "llm_config": effective_config or {},
                "prefixes": effective_config.get("prefixes") or [],
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
                f"force={effective_force_reply} priority={conversation_priority} attention={attention_check} trigger={trigger_source}",
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
                    (attention_check and tieba_opportunity)
                    or proactive_web_opportunity
                    or idle_content_opportunity
                ),
                behavior_lookup_enabled=bool(not attention_check),
                cache_namespace="proactive-reply",
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
            idle_plan = parsed.get("share_idle_content") if isinstance(parsed.get("share_idle_content"), dict) else {}
            if idle_content_opportunity and idle_plan.get("action") == "share":
                content_hash = str(idle_plan.get("content_hash") or "").strip()
                # Sharing is constrained to a cached, de-duplicated candidate
                # injected into this exact request. A model must not turn an
                # idle opportunity into fabricated or uncached content.
                if not self._idle_content_plan_is_allowed(idle_plan, idle_content_candidates):
                    parsed["messages"] = []
                    parsed["animation"] = None
                    parsed["should_reply"] = False
                    parsed["reply_to"] = []
                    parsed["share_idle_content"] = {"action": "skip"}
                    parsed["_idle_content_rejected"] = True
                    print(
                        f"[LLM IDLE CONTENT REJECTED] group={group_id} reason=uncached_candidate "
                        f"content={content_hash or 'missing'}",
                        flush=True,
                    )
                elif idle_content_shadow_mode:
                    # Shadow mode observes planner choices without consuming a
                    # candidate or allowing an idle-content post into the sender
                    # path. Normal replies are unaffected unless the model
                    # explicitly selected a valid cached share.
                    parsed["messages"] = []
                    parsed["animation"] = None
                    parsed["should_reply"] = False
                    parsed["reply_to"] = []
                    parsed["_idle_content_shadow"] = True
                    print(f"[LLM IDLE CONTENT SHADOW] group={group_id} content={content_hash}", flush=True)
                elif self.style_learner:
                    self.style_learner.mark_idle_content_used(content_hash)

            parsed["_llm_ok"] = llm_ok
            result_to_return = parsed
            self._balance_warning_sent = False
        except Exception as exc:
            print(f"[LLM BATCH ERROR] {exc}")
            if is_insufficient_balance_error(exc):
                result_to_return = self._balance_response()

        result_to_return = _finish(result_to_return)
        if (
            idle_content_opportunity
            and self.style_learner
            and isinstance(result_to_return.get("share_idle_content"), dict)
            and result_to_return["share_idle_content"].get("action") == "share"
            and not result_to_return.get("_idle_content_shadow")
            and result_to_return.get("should_reply")
            and result_to_return.get("messages")
        ):
            # Count only an actual planned share, never an unrelated reply that
            # happened to occur during an idle opportunity.
            self.style_learner.record_idle_content_attempt(group_id)
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
                self._get_proactive_manager(group_id).record_llm_reply(group_id)
            self._maybe_compress_context(group_id)
        except Exception:
            return
