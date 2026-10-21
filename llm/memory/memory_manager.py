import json
import os
import tempfile
import threading
import time

from llm.config import get_llm_config


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GROUPS_DIR = os.path.join(BASE_DIR, "data", "groups")

# MemoryManager is instantiated by both Worker and LLMService. Keep the lock at
# module scope so those instances serialize access to the same JSON files.
_GROUP_FILE_LOCK = threading.RLock()


class MemoryManager:
    def __init__(self):
        llm_config = get_llm_config()
        # The canonical transcript is bounded by record count. Prompt token
        # fitting and context compression handle per-request size.
        self.group_message_max_records = max(
            100, int(llm_config.get("group_message_max_records", 5000) or 5000)
        )

    @staticmethod
    def estimate_tokens(value) -> int:
        """Conservative tokenizer-free estimate used only for context safety."""
        text = str(value or "")
        cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
        non_cjk = len(text) - cjk
        return cjk + max(0, (non_cjk + 3) // 4)

    def add_llm_message(self, group_id, nickname, content):
        """Store a delivered LLM reply with an explicit source boundary."""
        return self.add_group_message(
            group_id,
            nickname,
            content,
            is_bot=True,
            role="assistant",
            source_kind="llm_assistant",
            source_origin="llm_service_delivery",
            learnable=False,
            prompt_visible=True,
        )

    def get_llm_history(self, group_id):
        group_dir = self._ensure_group_dir(group_id)
        history_path = os.path.join(group_dir, "llm_history.json")

        return []

    def add_group_message(self, group_id, nickname, content, message_id=None,
                          local_id=None, server_id=None, session_id=None,
                          is_bot=False, role=None, batch_index=None,
                          prefix_used=False, is_at_bot=False,
                          is_mentioned=False, message_type=None,
                          wxid=None, subject_id=None, directed_to_bot=False,
                          directed_to_other=False, reply_target=None, address_confidence=0.0,
                          reply_to_bot=False, reply_to_other=False, mentions=None,
                          quoted_message=None, quoted_message_id=None, quoted_sender=None,
                          quoted_sender_wxid=None, quoted_content=None,
                          quoted_message_type=None, quoted_timestamp=None,
                          quoted_available=None, quoted_role=None,
                          quoted_is_bot=None, source_kind="user_incoming",
                          source_origin="wechat_inbound", learnable=True,
                          prompt_visible=True):
        with _GROUP_FILE_LOCK:
            group_dir = self._ensure_group_dir(group_id)
            messages_path = os.path.join(group_dir, "group_messages.json")

            messages = self._load_json(messages_path, default=[])
            item = {
                "nickname": str(nickname or ""),
                # Keep sub-second precision so cycle cleanup can distinguish
                # messages arriving during curation from the cycle boundary.
                "timestamp": time.time(),
                "content": str(content or "")
            }
            stable_subject = str(subject_id or wxid or "").strip()
            if stable_subject:
                item["subject_id"] = stable_subject
                item["wxid"] = str(wxid or stable_subject).strip()
            for key, value in (
                ("message_id", message_id),
                ("local_id", local_id),
                ("server_id", server_id),
                ("session_id", session_id),
            ):
                if value not in (None, ""):
                    item[key] = value
            item["is_bot"] = bool(is_bot)
            item["role"] = str(role or ("assistant" if is_bot else "user"))
            item["source_kind"] = str(source_kind or ("llm_assistant" if is_bot else "user_incoming"))
            item["source_origin"] = str(source_origin or ("llm_service_delivery" if is_bot else "wechat_inbound"))
            item["learnable"] = bool(learnable)
            item["prompt_visible"] = bool(prompt_visible)
            if is_bot or prefix_used or is_at_bot or is_mentioned:
                item["prefix_used"] = bool(prefix_used)
                item["is_at_bot"] = bool(is_at_bot)
                item["is_mentioned"] = bool(is_mentioned)
            if message_type not in (None, ""):
                item["message_type"] = str(message_type)
            item["directed_to_bot"] = bool(directed_to_bot)
            item["directed_to_other"] = bool(directed_to_other)
            item["reply_to_bot"] = bool(reply_to_bot)
            item["reply_to_other"] = bool(reply_to_other)
            if isinstance(mentions, (list, tuple, set)) and mentions:
                item["mentions"] = [str(value) for value in mentions if str(value).strip()][:20]
            if reply_target not in (None, ""):
                item["reply_target"] = str(reply_target)
            try:
                item["address_confidence"] = round(float(address_confidence or 0.0), 3)
            except (TypeError, ValueError):
                item["address_confidence"] = 0.0
            if batch_index is not None:
                item["batch_index"] = int(batch_index)

            # Reply metadata is deliberately stored separately from the current
            # message body.  Omit empty fields to keep old transcripts compact
            # and preserve compatibility with pre-reply JSON files.
            quoted = quoted_message if isinstance(quoted_message, dict) else {}
            quote_values = {
                "quoted_message_id": quoted_message_id or quoted.get("message_id"),
                "quoted_sender": quoted_sender or quoted.get("sender"),
                "quoted_sender_wxid": quoted_sender_wxid or quoted.get("sender_wxid"),
                "quoted_content": quoted_content if quoted_content is not None else quoted.get("content"),
                "quoted_message_type": quoted_message_type or quoted.get("message_type"),
                "quoted_timestamp": quoted_timestamp or quoted.get("timestamp"),
                "quoted_role": quoted_role or quoted.get("role"),
                "quoted_is_bot": quoted_is_bot if quoted_is_bot is not None else quoted.get("is_bot"),
            }
            for key, value in quote_values.items():
                if value not in (None, "", [], {}):
                    item[key] = value
            if quoted_available is not None or quoted:
                item["quoted_available"] = bool(
                    quoted_available if quoted_available is not None else quoted.get("available")
                )

            identity = next(
                (
                    (key, str(item.get(key)))
                    for key in ("message_id", "local_id", "server_id")
                    if item.get(key) not in (None, "", 0, "0")
                ),
                None,
            )
            if identity:
                for previous in messages:
                    if isinstance(previous, dict) and str(previous.get(identity[0])) == identity[1]:
                        return False
            messages.append(item)
            messages = self._bound_group_messages(messages)

            self._write_json(messages_path, messages)
            return True

    def _bound_group_messages(self, messages):
        """Keep a latest context summary plus the newest bounded transcript."""
        limit = int(getattr(self, "group_message_max_records", 5000) or 5000)
        items = [item for item in (messages or []) if isinstance(item, dict)]
        if limit <= 0 or len(items) <= limit:
            return items
        summary = next(
            (
                item for item in reversed(items)
                if str(item.get("role") or "") == "context_summary"
            ),
            None,
        )
        if summary is None:
            return items[-limit:]
        tail_limit = max(0, limit - 1)
        tail = items[-tail_limit:] if tail_limit else []
        if tail and tail[0] is summary:
            return tail
        return [summary] + [item for item in tail if item is not summary]

    def get_group_messages(self, group_id):
        with _GROUP_FILE_LOCK:
            group_dir = self._ensure_group_dir(group_id)
            messages_path = os.path.join(group_dir, "group_messages.json")
            return self._load_json(messages_path, default=[])

    def find_group_message(self, group_id, identifiers):
        """Find one historical message by any provider message identifier.

        WeFlow payloads are inconsistent about whether a reply points to the
        message key, local id, or server id.  Search all three while retaining
        compatibility with old JSON records that contain only one of them.
        """
        if isinstance(identifiers, dict):
            values = []
            for key in ("message_id", "messageId", "messageKey", "id", "local_id", "localId", "server_id", "serverId", "svrid"):
                if identifiers.get(key) not in (None, "", 0, "0"):
                    values.append(str(identifiers[key]).strip())
        elif isinstance(identifiers, (list, tuple, set)):
            values = [str(value).strip() for value in identifiers if value not in (None, "")]
        else:
            values = [str(identifiers).strip()] if identifiers not in (None, "") else []
        values = {value for value in values if value}
        if not values:
            return None
        with _GROUP_FILE_LOCK:
            for item in self.get_group_messages(group_id):
                if not isinstance(item, dict):
                    continue
                for key in ("message_id", "local_id", "server_id"):
                    if str(item.get(key) or "").strip() in values:
                        return dict(item)
        return None

    @staticmethod
    def resolve_quoted_message(group_id, quoted, memory_manager):
        """Backfill a normalized quote from transcript history when possible."""
        if not isinstance(quoted, dict):
            return None
        result = dict(quoted)
        nested = result.get("quoted_message") if isinstance(result.get("quoted_message"), dict) else {}
        identifiers = []
        for source in (result, nested):
            for key in (
                "message_id", "messageKey", "messageId", "id",
                "local_id", "localId", "server_id", "serverId", "svrid",
            ):
                value = source.get(key)
                if value not in (None, "", 0, "0"):
                    identifiers.append(value)
        identifiers = list(dict.fromkeys(str(value).strip() for value in identifiers if str(value).strip()))
        historical = memory_manager.find_group_message(group_id, identifiers)
        if historical:
            result["message_id"] = result.get("message_id") or historical.get("message_id") or historical.get("local_id") or historical.get("server_id") or ""
            result["sender"] = result.get("sender") or historical.get("nickname") or ""
            result["sender_wxid"] = result.get("sender_wxid") or historical.get("wxid") or ""
            result["content"] = result.get("content") or historical.get("content") or ""
            result["message_type"] = result.get("message_type") or historical.get("message_type") or ""
            result["timestamp"] = result.get("timestamp") or historical.get("timestamp") or ""
            historical_is_bot = historical.get("is_bot")
            historical_is_bot = (
                historical_is_bot.strip().lower() in {"1", "true", "yes", "on"}
                if isinstance(historical_is_bot, str)
                else bool(historical_is_bot)
            )
            result_is_bot = result.get("is_bot")
            result_is_bot = (
                result_is_bot.strip().lower() in {"1", "true", "yes", "on"}
                if isinstance(result_is_bot, str)
                else bool(result_is_bot)
            )
            result["role"] = result.get("role") or historical.get("role") or ("assistant" if historical_is_bot else "user")
            result["is_bot"] = result_is_bot or historical_is_bot
        result["available"] = bool(str(result.get("content") or "").strip())
        return result

    def replace_group_messages(self, group_id, messages, preserve_unseen_from=None):
        """Replace the transcript without losing messages written meanwhile.

        Context compression takes time because it calls the LLM outside this
        lock. When a snapshot is supplied, retain current records that were
        not part of that snapshot and append them after the compacted result.
        """
        with _GROUP_FILE_LOCK:
            group_dir = self._ensure_group_dir(group_id)
            messages_path = os.path.join(group_dir, "group_messages.json")
            clean = [item for item in (messages or []) if isinstance(item, dict)]
            if preserve_unseen_from is not None:
                snapshot_keys = {
                    self._message_key(item)
                    for item in (preserve_unseen_from or [])
                    if isinstance(item, dict)
                }
                current = self._load_json(messages_path, default=[])
                clean.extend(
                    item for item in current
                    if isinstance(item, dict) and self._message_key(item) not in snapshot_keys
                )
            self._write_json(messages_path, clean)
            return clean

    def prune_group_messages_through(self, group_id, boundary):
        """Atomically keep only messages newer than a cycle boundary."""
        try:
            boundary = float(boundary)
        except (TypeError, ValueError):
            boundary = 0
        with _GROUP_FILE_LOCK:
            group_dir = self._ensure_group_dir(group_id)
            messages_path = os.path.join(group_dir, "group_messages.json")
            current = self._load_json(messages_path, default=[])
            preserved = [
                item for item in current
                if isinstance(item, dict) and self._message_timestamp(item) > boundary
            ]
            self._write_json(messages_path, preserved)
            return preserved

    def group_context_tokens(self, group_id) -> int:
        return self.estimate_tokens(self.get_group_messages(group_id))

    def _ensure_group_dir(self, group_id):
        safe_group_id = str(group_id or "unknown")
        group_dir = os.path.join(GROUPS_DIR, safe_group_id)
        os.makedirs(group_dir, exist_ok=True)
        return group_dir

    @staticmethod
    def _message_timestamp(item):
        try:
            return float(item.get("timestamp") or 0)
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _message_key(cls, item):
        for key in ("message_id", "local_id", "server_id"):
            value = item.get(key)
            if value not in (None, "", 0, "0"):
                return ("id", key, str(value))
        return (
            "content",
            cls._message_timestamp(item),
            str(item.get("nickname") or ""),
            str(item.get("content") or ""),
            str(item.get("role") or ""),
        )

    def _load_json(self, path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return list(default) if isinstance(default, list) else dict(default)

    def _write_json(self, path, data):
        directory = os.path.dirname(path)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=directory,
                prefix=".group_messages.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temp_path = file.name
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
        except Exception:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise
