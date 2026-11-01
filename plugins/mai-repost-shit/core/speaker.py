"""按会话缓存最近一位真人发言者。

为什么需要它：LLM 工具（Tool）在群聊里回调时拿到的 `user_id` 恒为空，
身份校验做不了。宿主的入站钩子 `chat.receive.after_process` 才带真实发言者，
所以这里用钩子把「会话 -> 最近发言者」记下来，工具触发时再回查。

取舍：缓存是「最近一位发言者」，不是「触发这次工具调用的那个人」。
两者在绝大多数场景一致（点播的那条消息刚进过入站钩子），
但同一群里多人抢话时可能错位，所以白名单模式下宁可拒绝也不能放过。
"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional

# 只保留最近的若干个会话，避免长时间运行内存累积
_MAX_SESSIONS = 500


class SpeakerCache:
    """会话 -> 最近发言者（带 TTL）。"""

    def __init__(self, ttl_seconds: int, logger: Any) -> None:
        self._ttl = max(30, ttl_seconds)
        self._logger = logger
        self._entries: Dict[str, Dict[str, Any]] = {}

    def remember(self, session_id: str, user_id: str, nickname: str) -> None:
        """记下某个会话的最近发言者。"""
        if not session_id or not user_id:
            return
        self._entries[session_id] = {
            "user_id": str(user_id),
            "nickname": str(nickname or ""),
            "at": time.time(),
        }
        self.purge()

    def resolve(self, session_id: str) -> Optional[Dict[str, str]]:
        """回查某会话的最近发言者；过期或没有则返回 None。"""
        if not session_id:
            return None
        entry = self._entries.get(session_id)
        if entry is None:
            return None
        if time.time() - float(entry.get("at") or 0) > self._ttl:
            self._entries.pop(session_id, None)
            return None
        return {"user_id": entry["user_id"], "nickname": entry.get("nickname", "")}

    def forget(self, session_id: str) -> None:
        """丢掉某会话的缓存条目。

        白名单校验失败时调用：钩子被 skip 掉可能留下旧条目，
        丢掉它可以让下一次触发必须重新经过入站钩子，减少被蹭的可能。
        """
        self._entries.pop(session_id, None)

    def purge(self) -> None:
        """清掉过期条目；条目过多时再淘汰最旧的一些。"""
        deadline = time.time() - self._ttl
        for key in [k for k, v in self._entries.items() if float(v.get("at") or 0) < deadline]:
            self._entries.pop(key, None)
        if len(self._entries) > _MAX_SESSIONS:
            ordered = sorted(self._entries.items(), key=lambda item: float(item[1].get("at") or 0))
            for key, _ in ordered[: len(self._entries) - _MAX_SESSIONS]:
                self._entries.pop(key, None)

    def size(self) -> int:
        return len(self._entries)


def normalize_qq(value: Any) -> str:
    """Wechat port: accept exact wxid identifiers and legacy numeric IDs."""
    text = str(value or "").strip()
    import re
    if not (re.fullmatch(r'wxid_[A-Za-z0-9_-]{1,100}',text) or
            text.isdigit() and text.strip('0')):
        return ""
    return text
