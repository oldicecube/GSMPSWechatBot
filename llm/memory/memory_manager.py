import json
import os
import time

from llm.config import get_llm_config


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GROUPS_DIR = os.path.join(BASE_DIR, "data", "groups")


class MemoryManager:
    def __init__(self):
        llm_config = get_llm_config()
        self.max_history = int(llm_config.get("max_history", 100))
        self.history_expire_ms = int(llm_config.get("history_expire_ms", 600000))
        self.group_message_limit = int(llm_config.get("group_message_limit", 20))

    def add_llm_message(self, group_id, nickname, content):
        group_dir = self._ensure_group_dir(group_id)
        history_path = os.path.join(group_dir, "llm_history.json")

        history = self._load_json(history_path, default=[])
        history = self._normalize_llm_history(history)
        history = self._clear_expired_history(history)

        history.append({
            "nickname": str(nickname or ""),
            "timestamp": int(time.time()),
            "content": str(content or "")
        })

        if self.max_history > 0:
            history = history[-self.max_history:]

        self._write_json(history_path, history)

    def get_llm_history(self, group_id):
        group_dir = self._ensure_group_dir(group_id)
        history_path = os.path.join(group_dir, "llm_history.json")

        history = self._load_json(history_path, default=[])
        history = self._normalize_llm_history(history)
        history = self._clear_expired_history(history)
        self._write_json(history_path, history)
        return history

    def add_group_message(self, group_id, nickname, content):
        group_dir = self._ensure_group_dir(group_id)
        messages_path = os.path.join(group_dir, "group_messages.json")

        messages = self._load_json(messages_path, default=[])
        messages = self._normalize_group_messages(messages)
        messages.append({
            "nickname": str(nickname or ""),
            "timestamp": int(time.time()),
            "content": str(content or "")
        })

        if self.group_message_limit > 0:
            messages = messages[-self.group_message_limit:]

        self._write_json(messages_path, messages)

    def get_group_messages(self, group_id):
        group_dir = self._ensure_group_dir(group_id)
        messages_path = os.path.join(group_dir, "group_messages.json")

        messages = self._load_json(messages_path, default=[])
        messages = self._normalize_group_messages(messages)

        if self.group_message_limit > 0:
            messages = messages[-self.group_message_limit:]
            self._write_json(messages_path, messages)

        return messages

    def _ensure_group_dir(self, group_id):
        safe_group_id = str(group_id or "unknown")
        group_dir = os.path.join(GROUPS_DIR, safe_group_id)
        os.makedirs(group_dir, exist_ok=True)
        return group_dir

    def _load_json(self, path, default):
        try:
            if not os.path.exists(path):
                return self._clone_default(default)

            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return self._clone_default(default)

    def _write_json(self, path, data):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            return

    def _normalize_llm_history(self, history):
        if not isinstance(history, list):
            return []

        normalized = []
        for item in history:
            if not isinstance(item, dict):
                continue

            try:
                timestamp = int(item.get("timestamp", 0))
            except Exception:
                timestamp = 0

            normalized.append({
                "nickname": str(item.get("nickname") or ""),
                "timestamp": timestamp,
                "content": str(item.get("content") or "")
            })

        if self.max_history > 0:
            normalized = normalized[-self.max_history:]

        return normalized

    def _normalize_group_messages(self, messages):
        if not isinstance(messages, list):
            return []

        normalized = []
        for item in messages:
            if isinstance(item, dict):
                try:
                    timestamp = int(item.get("timestamp", 0))
                except Exception:
                    timestamp = 0

                normalized.append({
                    "nickname": str(item.get("nickname") or ""),
                    "timestamp": timestamp,
                    "content": str(item.get("content") or "")
                })
                continue

            # 兼容旧版纯字符串数组
            normalized.append({
                "nickname": "",
                "timestamp": 0,
                "content": str(item or "")
            })

        if self.group_message_limit > 0:
            normalized = normalized[-self.group_message_limit:]
        return normalized

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

    def _clone_default(self, default):
        if isinstance(default, list):
            return list(default)
        if isinstance(default, dict):
            return dict(default)
        return default
