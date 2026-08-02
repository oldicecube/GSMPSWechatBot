import json
import os
import time

from llm.config import get_llm_config


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GROUPS_DIR = os.path.join(BASE_DIR, "data", "groups")


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

    def add_llm_message(self, group_id, nickname, content):
        group_dir = self._ensure_group_dir(group_id)
        history_path = os.path.join(group_dir, "llm_history.json")

        history = self._load_json(history_path, default=[])

        history.append({
            "nickname": str(nickname or ""),
            "timestamp": int(time.time()),
            "content": str(content or "")
        })

        history = self._trim_by_chars(history, self.max_history_chars)

        self._write_json(history_path, history)

    def get_llm_history(self, group_id):
        group_dir = self._ensure_group_dir(group_id)
        history_path = os.path.join(group_dir, "llm_history.json")

        history = self._load_json(history_path, default=[])
        history = self._clear_expired_history(history)

        history = self._trim_by_chars(history, self.max_history_chars)

        return history

    def add_group_message(self, group_id, nickname, content, message_id=None,
                          local_id=None, server_id=None, session_id=None):
        group_dir = self._ensure_group_dir(group_id)
        messages_path = os.path.join(group_dir, "group_messages.json")

        messages = self._load_json(messages_path, default=[])
        item = {
            "nickname": str(nickname or ""),
            "timestamp": int(time.time()),
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
        messages.append(item)

        messages = self._trim_by_chars(messages, self.group_message_limit_chars)

        self._write_json(messages_path, messages)

    def get_group_messages(self, group_id):
        group_dir = self._ensure_group_dir(group_id)
        messages_path = os.path.join(group_dir, "group_messages.json")

        messages = self._load_json(messages_path, default=[])

        messages = self._trim_by_chars(messages, self.group_message_limit_chars)

        return messages

    def _ensure_group_dir(self, group_id):
        safe_group_id = str(group_id or "unknown")
        group_dir = os.path.join(GROUPS_DIR, safe_group_id)
        os.makedirs(group_dir, exist_ok=True)
        return group_dir

    def _load_json(self, path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return list(default) if isinstance(default, list) else dict(default)

    def _write_json(self, path, data):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

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
