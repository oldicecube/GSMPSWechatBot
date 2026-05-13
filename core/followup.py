import threading
import time


_LOCK = threading.Lock()
# 改为按 (session_id, wxid) 为键
_FOLLOWUPS = {}


def register(session_id, target, ttl=60, payload=None, wxid=None):
    """
    注册一个 follow-up 状态
    
    每个 (session_id, wxid) 组合可以独立维护一个状态，
    这样可以支持同一会话中多个用户的并发处理
    """
    if not session_id or not target:
        return

    key = (session_id, wxid)
    
    with _LOCK:
        _FOLLOWUPS[key] = {
            "target": target,
            "expire": time.time() + ttl,
            "payload": payload or {},
            "wxid": wxid
        }


def peek(session_id, wxid=None):
    """查看 follow-up 状态，但不消费"""
    if not session_id:
        return None

    key = (session_id, wxid)
    
    with _LOCK:
        state = _FOLLOWUPS.get(key)
        if not state:
            return None

        if time.time() > state.get("expire", 0):
            _FOLLOWUPS.pop(key, None)
            return None

        return {
            "target": state.get("target"),
            "expire": state.get("expire"),
            "payload": dict(state.get("payload") or {}),
            "wxid": state.get("wxid")
        }


def consume(session_id, wxid=None):
    """消费一个 follow-up 状态，删除它"""
    if not session_id:
        return None

    key = (session_id, wxid)
    
    with _LOCK:
        state = _FOLLOWUPS.get(key)
        if not state:
            return None

        if time.time() > state.get("expire", 0):
            _FOLLOWUPS.pop(key, None)
            return None

        _FOLLOWUPS.pop(key, None)
        return {
            "target": state.get("target"),
            "expire": state.get("expire"),
            "payload": dict(state.get("payload") or {}),
            "wxid": state.get("wxid")
        }


def clear(session_id, wxid=None):
    """清除指定的 follow-up 状态"""
    if not session_id:
        return

    key = (session_id, wxid)
    
    with _LOCK:
        _FOLLOWUPS.pop(key, None)


def clear_all_by_session(session_id):
    """清除某个 session 下的所有 follow-up 状态"""
    if not session_id:
        return
    
    with _LOCK:
        keys_to_remove = [
            key for key in _FOLLOWUPS.keys()
            if key[0] == session_id
        ]
        for key in keys_to_remove:
            _FOLLOWUPS.pop(key, None)
