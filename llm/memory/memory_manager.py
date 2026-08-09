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
        self.max_history_chars = int(
            llm_config.get("max_history_chars", llm_config.get("max_history", 5000))
        )
        self.history_expire_ms = int(llm_config.get("history_expire_ms", 600000))
        self.group_message_limit_chars = int(
            llm_config.get(
                "group_message_limit_chars",
                llm_config.get("group_message_limit", 2000),
            )
        )
        # Compatibility aliases for callers outside this module.
        self.max_history = self.max_history_chars
        self.group_message_limit = self.group_message_limit_chars

    @staticmethod
    def estimate_tokens(value) -> int:
        """Conservative tokenizer-free estimate used only for context safety."""
        text = str(value or "")
        cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
        non_cjk = len(text) - cjk
        return cjk + max(0, (non_cjk + 3) // 4)

    def add_llm_message(self, group_id, nickname, content):
        """Compatibility wrapper; all new conversation messages use group history."""
        return self.add_group_message(
            group_id,
            nickname,
            content,
            is_bot=True,
        )

    def get_llm_history(self, group_id):
        group_dir = self._ensure_group_dir(group_id)
        history_path = os.path.join(group_dir, "llm_history.json")

        return []

    def add_group_message(self, group_id, nickname, content, message_id=None,
                          local_id=None, server_id=None, session_id=None,
                          is_bot=False, role=None, batch_index=None,
                          prefix_used=False, is_at_bot=False,
                          is_mentioned=False, message_type=None):
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
            if is_bot or prefix_used or is_at_bot or is_mentioned:
                item["prefix_used"] = bool(prefix_used)
                item["is_at_bot"] = bool(is_at_bot)
                item["is_mentioned"] = bool(is_mentioned)
            if message_type not in (None, ""):
                item["message_type"] = str(message_type)
            if batch_index is not None:
                item["batch_index"] = int(batch_index)

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

            self._write_json(messages_path, messages)
            return True

    def get_group_messages(self, group_id):
        with _GROUP_FILE_LOCK:
            group_dir = self._ensure_group_dir(group_id)
            messages_path = os.path.join(group_dir, "group_messages.json")
            return self._load_json(messages_path, default=[])

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

    def _clear_expired_history(self, history):
        if not history:
            return []
        if self.history_expire_ms <= 0:
            return history

        now_ms = int(time.time() * 1000)
        latest_timestamp = max(int(item.get("timestamp", 0)) for item in history)
        latest_timestamp_ms = latest_timestamp * 1000

        if now_ms - latest_timestamp_ms > self.history_expire_ms:
            return []

        return history

    @staticmethod
    def _trim_by_chars(messages, limit):
        """Keep newest messages until the character budget is reached.

        The budget counts message ``content`` only. The newest complete
        message that crosses the boundary is retained, so forwarding never
        splits a message and the total is just over the configured limit.
        """
        if not isinstance(messages, list) or limit <= 0:
            return messages

        selected = []
        total_chars = 0
        for item in reversed(messages):
            if not isinstance(item, dict):
                continue
            content_chars = len(str(item.get("content") or ""))
            selected.append(item)
            total_chars += content_chars
            if total_chars >= limit:
                break

        selected.reverse()
        return selected

    @staticmethod
    def trim_messages_by_chars(messages, limit):
        return MemoryManager._trim_by_chars(messages, limit)
