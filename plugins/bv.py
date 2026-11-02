# -*- coding: utf-8 -*-
"""Command plugin for downloading one Bilibili BV video.

Usage: /bv <BVxxxxxxxxxx, Bilibili video URL or b23.tv share link>
The queue is FIFO with a configurable capacity. Every downloaded file is
written below the bot workspace temp directory and deleted in finally.
"""

import os
import hashlib
import queue
from contextvars import copy_context
import re
import threading
import time
from uuid import uuid4

from core.sender import send
from services import bilibili

COMMAND = "/bv"
ALIASES = ["/BV"]

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp", "bilibili")
QUEUE_LIMIT = 5
_BVID_RE = re.compile(r"(?i)(?<![A-Za-z0-9])BV[0-9A-Za-z]{10}(?![A-Za-z0-9])")
_CONFIG = {}
_QUEUE = queue.Queue(maxsize=QUEUE_LIMIT)
_WORKER = None
_WORKER_LOCK = threading.Lock()
_INFLIGHT = set()
_INFLIGHT_LOCK = threading.RLock()


def init(config):
    global _CONFIG, _WORKER, QUEUE_LIMIT
    _CONFIG = config if isinstance(config, dict) else {}
    QUEUE_LIMIT = max(1, min(20, int((_CONFIG.get('bilibili') or {}).get('queue_limit', 5))))
    _QUEUE.maxsize = QUEUE_LIMIT
    bilibili.configure(_CONFIG)
    os.makedirs(TEMP_DIR, exist_ok=True)
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker_loop, daemon=True, name="bilibili-bv-worker")
            _WORKER.start()
    print(f"[PLUGIN] /bv Bilibili FIFO worker ready (capacity={QUEUE_LIMIT})", flush=True)


def _block():
    value = (_CONFIG or {}).get("bilibili")
    return value if isinstance(value, dict) else {}


def _extract_bvid(text):
    match = _BVID_RE.search(str(text or ""))
    if not match:
        return ""
    raw = match.group(0)
    return "BV" + raw[2:]


def _max_bytes():
    try:
        return int(float(_block().get("max_size_mb", 40) or 40) * 1024 * 1024)
    except (TypeError, ValueError):
        return 40 * 1024 * 1024


def _target(context):
    return str((context or {}).get("group") or (context or {}).get("user") or "").strip()


def _send_text(target, text):
    text = str(text or "").strip()
    if not text:
        return False
    ok, error = send(target=target, content=text, mode="wechat_text")
    if not ok:
        print(f"[BV] text send failed target={target} error={error}", flush=True)
    return bool(ok)


def _failure_text(error):
    return f"B站视频处理失败：{error}"


def handle(content, context):
    context = context if isinstance(context, dict) else {}
    text = str(content or "").strip()
    bvid = bilibili.extract_short_link(text) or _extract_bvid(text)
    if not bvid:
        return "用法：/bv BVxxxxxxxxxx、B站视频链接或 https://b23.tv/短码（也可直接发送短链）"

    target = _target(context)
    if not target:
        return "找不到发送目标"
    inflight_key = (str(context.get('platform') or 'wechat'), target, bvid)

    with _INFLIGHT_LOCK:
        if inflight_key in _INFLIGHT:
            return f"{bvid} 已在队列中或正在处理，请勿重复提交"
        # Capacity includes the currently running job plus queued jobs.
        if len(_INFLIGHT) >= QUEUE_LIMIT or _QUEUE.full():
            return f"B站视频下载队列已满（上限 {QUEUE_LIMIT}），请稍后再试"
        _INFLIGHT.add(inflight_key)
        try:
            position = len(_INFLIGHT)
            event = str(context.get('messageKey') or context.get('serverId') or uuid4().hex)
            event = ':'.join([str(context.get('platform') or 'wechat'), target, event])
            _QUEUE.put_nowait((target, text, bvid, copy_context(), inflight_key, event))
        except queue.Full:
            _INFLIGHT.discard(inflight_key)
            return f"B站视频下载队列已满（上限 {QUEUE_LIMIT}），请稍后再试"

    return f"已加入B站视频队列（第 {position}/{QUEUE_LIMIT}）：{bvid}"


def _worker_loop():
    while True:
        job = _QUEUE.get()
        target, text, bvid = job[:3]
        job_context = job[3] if len(job) > 3 else copy_context()
        try:
            job_context.run(_run_job, target, text, bvid, job[5] if len(job)>5 else uuid4().hex)
        except Exception as exc:
            print(f"[BV] unexpected job error bvid={bvid}: {exc}", flush=True)
            job_context.run(_send_text, target, _failure_text(exc))
        finally:
            with _INFLIGHT_LOCK:
                _INFLIGHT.discard(job[4] if len(job)>4 else bvid)
            _QUEUE.task_done()


def _run_job(target, text, bvid, interest_event=''):
    os.makedirs(TEMP_DIR, exist_ok=True)
    file_id = bvid if _BVID_RE.fullmatch(bvid) else 'short_' + hashlib.sha256(bvid.encode()).hexdigest()[:16]
    path = os.path.join(TEMP_DIR, f"{file_id}_{int(time.time() * 1000)}.mp4")
    try:
        print(f"[BV] resolving bvid={bvid} target={target}", flush=True)
        plan = bilibili.resolve(text, max_bytes=_max_bytes())
        info = plan["info"]
        stream = plan["stream"]
        title = str(info.get("title") or bvid).strip()
        print(f"[BV] downloading bvid={bvid} title={title!r} path={path}", flush=True)
        bilibili.download(stream, path, max_bytes=_max_bytes())
        ok, error = send(target=target, file_path=path, mode="wechat_file")
        if not ok:
            print(f"[BV] video send failed bvid={bvid} error={error}", flush=True)
            _send_text(target, _failure_text(error))
            return
        _send_text(target, f"解析成功！{title}")
        try:
            from utils.bili_interest import default_path, TagStore
            if default_path().is_file():
                store = TagStore()
                if store.get('parse_linked_enabled', False):
                    tags = bilibili.fetch_video_tags(info)
                    store.record_parse(str(info.get('bvid') or bvid), tags, interest_event or uuid4().hex)
        except Exception as exc:
            # Tag learning is optional and never reverses a successful video send.
            print(f'[BV] Story tag learning failed: {type(exc).__name__}', flush=True)
        print(f"[BV] sent bvid={bvid} title={title!r}", flush=True)
    except bilibili.BilibiliError as exc:
        print(f"[BV] failed bvid={bvid}: {exc}", flush=True)
        _send_text(target, _failure_text(exc))
    except Exception as exc:
        print(f"[BV] unexpected failure bvid={bvid}: {exc}", flush=True)
        _send_text(target, _failure_text(exc))
    finally:
        bilibili.delete_file(path)
        print(f"[BV] cleaned path={path}", flush=True)

WEBUI_CONFIG = {'version': 1, 'fields': {
    'enabled': {'type': 'bool', 'default': True, 'label': '允许下载视频', 'config_path': 'bilibili.enabled', 'reload': 'restart'},
    'max_size_mb': {'type': 'float', 'default': 40, 'min': 1, 'max': 200, 'label': '文件大小上限（MB）', 'config_path': 'bilibili.max_size_mb', 'reload': 'restart'},
    'max_duration_seconds': {'type': 'int', 'default': 300, 'min': 10, 'max': 3600, 'label': '视频时长上限（秒）', 'config_path': 'bilibili.max_duration_seconds', 'reload': 'restart'},
    'prefer_quality': {'type': 'int', 'default': 64, 'choices': [6, 16, 32, 64, 80], 'label': '首选画质代码', 'config_path': 'bilibili.prefer_quality', 'reload': 'restart'},
    'fallback_quality': {'type': 'int', 'default': 32, 'choices': [6, 16, 32, 64, 80], 'label': '回退画质代码', 'config_path': 'bilibili.fallback_quality', 'reload': 'restart'},
    'timeout_seconds': {'type': 'float', 'default': 30, 'min': 5, 'max': 120, 'label': '请求超时（秒）', 'config_path': 'bilibili.timeout_seconds', 'reload': 'restart'},
    'queue_limit': {'type': 'int', 'default': 5, 'min': 1, 'max': 20, 'label': '下载任务上限（含正在运行）', 'config_path': 'bilibili.queue_limit', 'reload': 'restart'},
}}

WEBUI_CONFIG_SCHEMA = {
    "title": "B站媒体",
    "fields": [
        {"key": "enabled", "type": "boolean", "label": "启用"},
        {"key": "max_size_mb", "type": "number", "label": "最大文件大小 MB"},
        {"key": "max_duration_seconds", "type": "number", "label": "最大时长"},
        {"key": "prefer_quality", "type": "number", "label": "优先画质"},
        {"key": "fallback_quality", "type": "number", "label": "回退画质"},
        {"key": "timeout_seconds", "type": "number", "label": "超时秒数"},
        {"key": "queue_limit", "type": "number", "label": "队列上限"},
    ],
}
