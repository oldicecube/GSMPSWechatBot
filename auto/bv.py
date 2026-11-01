"""Low-cost automatic Bilibili BV and b23.tv share-link detector.

The detector only performs local boundary-checked identifier/link scans. Network work,
queueing, download, send, and cleanup stay in plugins.bv.
"""
from __future__ import annotations

# GSMPS WebUI metadata only; execution remains in this shell module.
WEBUI_CONFIG = {'version': 1, 'fields': {}}


import re

from plugins import bv as implementation

MATCH_RAW_MESSAGE = True
INTERCEPT_LLM = True
FALLBACK_ONLY = False
AUTO_COMMAND_CONSUMES = True
AUTO_COMMANDS = {"/bv", "/BV"}

# Standard BV token used by the existing downloader: BV + ten base62 chars.
# Boundary checks prevent matching a longer identifier or an ordinary word.
_FAST_BV_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])BV[0-9A-Za-z]{10}(?![A-Za-z0-9])"
)


def init(config):
    # The command module and this auto wrapper share the same FIFO/worker.
    implementation.init(config)


def _content(context):
    context = context if isinstance(context, dict) else {}
    command = str(context.get("command") or "").strip()
    args = str(context.get("args") or "").strip()
    if command.startswith("/"):
        return f"{command} {args}".strip()
    value = context.get("content")
    if value:
        return str(value).strip()
    raw = context.get("raw")
    if isinstance(raw, dict):
        return str(raw.get("content") or raw.get("text") or "").strip()
    return ""


def handle_auto(context):
    text = _content(context)
    if not text or not (_FAST_BV_RE.search(text) or implementation.bilibili.extract_short_link(text)):
        return None
    # Reuse the existing FIFO implementation. It performs the same fast scan,
    # queue limit check, background download, send, and finally cleanup.
    return implementation.handle(text, context)
