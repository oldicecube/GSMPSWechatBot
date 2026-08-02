"""LLM 内置主动回复状态机。

状态顺序为：idle -> attention -> working -> attention -> idle。
工作期按 10 秒批次判断；关注期每次随机 2--5 分钟检查最近 10 条消息，
连续三次没有值得回复的内容才回到空闲期。
"""

from __future__ import annotations

import random
import threading
import time
from urllib.parse import urlparse

from core.group_policy import is_allowed_group, normalize_target_groups
from llm.web_tools import AUTO_REPLY_URL_HOSTS, is_url_only


def _is_supported_url_only(content):
    if not is_url_only(content):
        return False
    hostname = (urlparse(str(content).strip()).hostname or "").rstrip(".").lower()
    return any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in AUTO_REPLY_URL_HOSTS
    )


class ProactiveReplyManager:
    def __init__(self, config):
        self.config = config if isinstance(config, dict) else {}
        self.settings = self.config.get("auto_reply") or self.config.get("random_reply") or {}
        self.target_groups = normalize_target_groups(self.config.get("target_groups"))
        self.allowed_groups = normalize_target_groups(self.settings.get("allowed_groups"))
        self.enabled = bool(self.settings.get("enabled", False))
        self.states = {}
        self.lock = threading.Lock()
        self.callback = None
        self.clock = time.time
        self.randint = random.randint
        self.initial_idle_end = self.clock() + self._random_seconds(self._idle_bounds())

        if self.enabled:
            threading.Thread(
                target=self._timer_loop,
                daemon=True,
                name="llm-proactive-reply-timer",
            ).start()

    def set_batch_callback(self, callback):
        self.callback = callback if callable(callback) else None

    def _int_setting(self, name, default, minimum=1):
        try:
            return max(minimum, int(self.settings.get(name, default)))
        except (TypeError, ValueError):
            return default

    def _idle_bounds(self):
        minimum = self._int_setting("idle_min_seconds", 20 * 60)
        maximum = self._int_setting("idle_max_seconds", 40 * 60)
        return minimum, max(minimum, maximum)

    def _work_bounds(self):
        minimum = self._int_setting("work_min_seconds", 2 * 60)
        maximum = self._int_setting("work_max_seconds", 5 * 60)
        return minimum, max(minimum, maximum)

    def _attention_bounds(self):
        minimum = self._int_setting("attention_min_seconds", 2 * 60)
        maximum = self._int_setting("attention_max_seconds", 5 * 60)
        return minimum, max(minimum, maximum)

    def _batch_interval(self):
        return self._int_setting("batch_interval_seconds", 10)

    def _attention_message_limit(self):
        return self._int_setting("attention_message_limit", 10)

    def _attention_no_reply_limit(self):
        return self._int_setting("attention_no_reply_limit", 3)

    def _work_extend_threshold(self):
        return self._int_setting("work_extend_threshold_seconds", 3 * 60)

    def _work_extend_seconds(self):
        return self._int_setting("work_extend_seconds", 3 * 60)

    def _random_seconds(self, bounds):
        return self.randint(bounds[0], bounds[1])

    def _new_idle_state(self, now):
        return {
            "phase": "idle",
            "phase_end": now + self._random_seconds(self._idle_bounds()),
            "next_batch_at": None,
            "next_attention_at": None,
            "messages": [],
            "recent_messages": [],
            "last_context": None,
            "attention_misses": 0,
            "checking": False,
            "flushing": False,
            "force_reply_pending": False,
            "trigger_source_pending": "interval",
        }

    def _new_work_state(self, now):
        return {
            "phase": "working",
            "phase_end": now + self._random_seconds(self._work_bounds()),
            "next_batch_at": now + self._batch_interval(),
            "next_attention_at": None,
            "messages": [],
            "recent_messages": [],
            "last_context": None,
            "attention_misses": 0,
            "checking": False,
            "flushing": False,
            "force_reply_pending": False,
            "trigger_source_pending": "interval",
        }

    def _new_attention_state(self, now, recent_messages=None):
        return {
            "phase": "attention",
            "phase_end": None,
            "next_batch_at": None,
            "next_attention_at": now + self._random_seconds(self._attention_bounds()),
            "messages": [],
            "recent_messages": list(recent_messages or []),
            "last_context": None,
            "attention_misses": 0,
            "checking": False,
            "flushing": False,
            "force_reply_pending": False,
            "trigger_source_pending": "attention",
        }

    def _initial_state(self, now):
        if now >= self.initial_idle_end:
            return self._new_attention_state(now)
        state = self._new_idle_state(now)
        state["phase_end"] = self.initial_idle_end
        return state

    def _group_key(self, context):
        return str(
            context.get("group")
            or context.get("sessionId")
            or context.get("user")
            or "unknown"
        ).strip() or "unknown"

    def _advance_state_locked(self, state, now):
        if state["phase"] == "idle" and now >= state["phase_end"]:
            recent = state.get("recent_messages") or []
            state.update(self._new_attention_state(now, recent))
            print("[LLM PROACTIVE] idle ended, attention period started", flush=True)
        elif state["phase"] == "working" and now >= state["phase_end"]:
            recent = state.get("recent_messages") or []
            state.update(self._new_attention_state(now, recent))
            print("[LLM PROACTIVE] work ended, attention period started", flush=True)

    def _allowed_group(self, context):
        groups = self.allowed_groups or self.target_groups
        return is_allowed_group(context, groups)

    def _is_mention(self, context):
        if context.get("prefix_used") or context.get("is_at") or context.get("is_mentioned"):
            return True

        raw = context.get("raw") or {}
        if isinstance(raw, dict):
            if raw.get("prefix_used") or raw.get("is_at") or raw.get("is_mentioned"):
                return True
            source = raw.get("_raw") or {}
            if isinstance(source, dict) and (
                source.get("is_at") or source.get("isAt") or source.get("is_mentioned")
                or source.get("isMentioned") or source.get("atBot")
            ):
                return True
        return False

    def _message_content(self, context):
        if context.get("content") is not None:
            return str(context.get("content") or "").strip()
        raw = context.get("raw") or {}
        if isinstance(raw, dict) and raw.get("content") is not None:
            return str(raw.get("content") or "").strip()
        return str(context.get("content") or "").strip()

    def _eligible(self, context):
        if not self.enabled or not context.get("is_group"):
            return False
        if not self._allowed_group(context):
            return False
        content = self._message_content(context)
        if not content:
            return False

        if self.settings.get("exclude_commands", False):
            routed = str(context.get("content") or "").strip()
            if content.startswith("/") or routed.startswith("/"):
                return False
        if self.settings.get("exclude_media", False) and (
            (content.startswith("[") and content.endswith("]"))
            or context.get("is_picture")
            or context.get("is_emoji")
            or context.get("is_voice")
        ):
            return False
        return True

    def _serialize_message(self, context):
        raw = context.get("raw") or {}
        source = raw.get("_raw") if isinstance(raw, dict) else {}
        if not isinstance(source, dict):
            source = {}

        return {
            "message_id": raw.get("messageKey") or source.get("messageKey") or "",
            "local_id": raw.get("localId") or source.get("localId"),
            "server_id": (
                raw.get("serverId") or raw.get("svrid") or raw.get("rawid")
                or source.get("serverId") or source.get("rawid")
            ),
            "session_id": context.get("sessionId") or raw.get("sessionId") or "",
            "timestamp": raw.get("_ts") or self.clock(),
            "group": context.get("group") or raw.get("group") or "",
            "sender_nickname": context.get("user") or raw.get("user") or "未知用户",
            "sender_wxid": context.get("wxid") or raw.get("wxid") or "",
            "content": self._message_content(context),
            "is_url_only": _is_supported_url_only(self._message_content(context)),
            "is_at_bot": self._is_mention(context),
            "prefix_used": bool(context.get("prefix_used")),
            "is_command": str(context.get("content") or "").strip().startswith("/"),
            "message_type": raw.get("type") or source.get("messageType") or "text",
        }

    def _remember_locked(self, state, context, message):
        state["last_context"] = dict(context)
        recent = state.setdefault("recent_messages", [])
        recent.append(message)
        state["recent_messages"] = recent[-self._attention_message_limit():]

    def _directive(self, batch, context, force_reply, trigger_source, attention_check=False):
        return {
            "forward_batch_to_llm": True,
            "batch_messages": batch,
            "force_reply": bool(force_reply),
            "trigger_source": trigger_source,
            "attention_check": bool(attention_check),
            "group_id": self._group_key(context),
        }

    def handle_message(self, context):
        if not self._eligible(context):
            return None

        group_id = self._group_key(context)
        now = self.clock()
        mention = self._is_mention(context)
        message = self._serialize_message(context)

        with self.lock:
            state = self.states.setdefault(group_id, self._initial_state(now))
            self._advance_state_locked(state, now)
            self._remember_locked(state, context, message)

            # Every message received during a work period goes into the same
            # ten-second batch.  Mentions and supported URL-only messages set
            # a batch-level force flag instead of causing a separate request.
            if state["phase"] != "working" and (mention or message.get("is_url_only")):
                recent = list(state.get("recent_messages") or [])
                state.clear()
                state.update(self._new_work_state(now))
                state["phase_end"] = now + self._work_extend_seconds()
                state["recent_messages"] = recent

            if state["phase"] != "working":
                return None

            state["messages"].append(message)
            if mention or message.get("is_url_only"):
                state["force_reply_pending"] = True
                if mention:
                    state["trigger_source_pending"] = "mention"
                elif state.get("trigger_source_pending") != "mention":
                    state["trigger_source_pending"] = "url_only"

            # The timer owns flushing.  Keeping this path enqueue-only means
            # messages arriving around the same ten-second boundary are still
            # judged together instead of racing into separate LLM requests.
            return None

    def _attention_miss_locked(self, state, now):
        state["checking"] = False
        state["attention_misses"] += 1
        if state["attention_misses"] >= self._attention_no_reply_limit():
            recent = state.get("recent_messages") or []
            state.clear()
            state.update(self._new_idle_state(now))
            state["recent_messages"] = recent
            print("[LLM PROACTIVE] attention ended after three no-reply checks", flush=True)
        else:
            state["next_attention_at"] = now + self._random_seconds(self._attention_bounds())

    def on_llm_result(self, context, result, attention_check=False):
        group_id = self._group_key(context)
        now = self.clock()
        with self.lock:
            state = self.states.get(group_id)
            if not state:
                return

            if attention_check:
                if state["phase"] != "attention":
                    return
                if (
                    isinstance(result, dict)
                    and result.get("_llm_ok")
                    and result.get("should_reply")
                    and result.get("messages")
                ):
                    recent = state.get("recent_messages") or []
                    state.clear()
                    state.update(self._new_work_state(now))
                    state["recent_messages"] = recent
                    state["last_context"] = dict(context)
                    print("[LLM PROACTIVE] attention found a reply-worthy message, work resumed", flush=True)
                else:
                    self._attention_miss_locked(state, now)
                return

            if (
                state["phase"] == "working"
                and isinstance(result, dict)
                and result.get("_llm_ok")
                and result.get("should_reply")
                and result.get("messages")
                and state["phase_end"] - now < self._work_extend_threshold()
            ):
                state["phase_end"] = now + self._work_extend_seconds()
                print("[LLM PROACTIVE] reply extended work period", flush=True)

    def _timer_loop(self):
        while self.enabled:
            now = self.clock()
            pending = []
            with self.lock:
                for group_id, state in self.states.items():
                    self._advance_state_locked(state, now)

                    if (
                        state["phase"] == "attention"
                        and not state.get("checking")
                        and state.get("next_attention_at") is not None
                        and now >= state["next_attention_at"]
                    ):
                        batch = list(state.get("recent_messages") or [])[-self._attention_message_limit():]
                        context = dict(state.get("last_context") or {})
                        if batch and self.callback is not None and context:
                            state["checking"] = True
                            pending.append((group_id, context, batch, True, False, "attention"))
                        else:
                            self._attention_miss_locked(state, now)

                    if (
                        state["phase"] == "working"
                        and state.get("messages")
                        and state.get("next_batch_at") is not None
                        and now >= state["next_batch_at"]
                        and not state.get("flushing")
                        and self.callback is not None
                    ):
                        batch = list(state["messages"])
                        context = dict(state.get("last_context") or {})
                        if context:
                            state["messages"] = []
                            force_reply = bool(state.get("force_reply_pending"))
                            trigger_source = state.get("trigger_source_pending") or "interval"
                            state["force_reply_pending"] = False
                            state["trigger_source_pending"] = "interval"
                            state["next_batch_at"] = now + self._batch_interval()
                            state["flushing"] = True
                            pending.append((group_id, context, batch, False, force_reply, trigger_source))

            for group_id, context, batch, attention_check, force_reply, trigger_source in pending:
                threading.Thread(
                    target=self._run_callback,
                    args=(group_id, context, batch, attention_check, force_reply, trigger_source),
                    daemon=True,
                    name="llm-proactive-reply-flush",
                ).start()
            time.sleep(1)

    def _run_callback(self, group_id, context, batch, attention_check,
                      force_reply=False, trigger_source="interval"):
        try:
            self.callback(
                context,
                batch,
                force_reply if not attention_check else False,
                "attention" if attention_check else trigger_source,
                attention_check,
            )
        finally:
            if not attention_check:
                with self.lock:
                    state = self.states.get(group_id)
                    if state:
                        state["flushing"] = False
            else:
                with self.lock:
                    state = self.states.get(group_id)
                    if state and state.get("checking"):
                        self._attention_miss_locked(state, self.clock())
