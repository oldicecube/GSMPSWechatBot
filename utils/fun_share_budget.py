"""Native leisure replies share the original global autonomous-life quota."""
from datetime import datetime
import hashlib
import os
from pathlib import Path
import sqlite3
import time
import tomllib
from zoneinfo import ZoneInfo


def reserve_for_turn(tool_ctx):
    anchor = getattr(tool_ctx.engine, '_tool_request_message', None)
    if anchor is None or not getattr(anchor, 'message_info', None):
        return True
    mid = str(getattr(anchor, 'message_id', '') or '')
    if anchor.message_info.additional_config.get('proactive_target') is not True or not (
            mid.startswith('fun-') or mid.startswith('proactive:wechatbot.autonomous-life:')):
        return True
    if getattr(tool_ctx.runtime, '_fun_share_granted_trigger', None) == mid:
        return True
    root = Path(os.environ.get('MAIBOT_INSTANCE_DIR') or Path(__file__).resolve().parents[1])
    try:
        cfg = tomllib.loads((root/'plugins/autonomous_life/config.toml').read_text(encoding='utf8')).get('life') or {}
    except FileNotFoundError:
        cfg = {}
    limit = min(4, max(0, int(cfg.get('daily_shares', 4))))
    gap = max(1800, int(cfg.get('share_gap_seconds', 7200)))
    day = datetime.now(ZoneInfo(str(cfg.get('timezone') or 'Asia/Shanghai'))).date().isoformat()
    path = root/'data/plugins/wechatbot.autonomous-life/life.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    try:
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS shares(fingerprint TEXT PRIMARY KEY,day TEXT,stamp REAL,stream TEXT,person TEXT,state TEXT)')
            db.execute('BEGIN IMMEDIATE')
            fingerprint = hashlib.sha256(('native-fun:'+mid).encode()).hexdigest()
            if db.execute('SELECT count(*) FROM shares WHERE day=?', (day,)).fetchone()[0] >= limit:
                return False
            if db.execute('SELECT 1 FROM shares WHERE stamp>? OR fingerprint=?', (time.time()-gap, fingerprint)).fetchone():
                return False
            # Reserve before side effects. Unknown delivery remains charged,
            # including after restart; no new Store instance resets live jobs.
            db.execute('INSERT INTO shares VALUES(?,?,?,?,?,?)',
                (fingerprint, day, time.time(), tool_ctx.runtime.session_id, '', 'unknown'))
        tool_ctx.runtime._fun_share_granted_trigger = mid
        return True
    finally:
        db.close()


def record_result(tool_ctx, state):
    anchor = getattr(tool_ctx.engine, '_tool_request_message', None)
    mid = str(getattr(anchor, 'message_id', '') or '')
    if not mid or getattr(tool_ctx.runtime, '_fun_share_granted_trigger', None) != mid:
        return
    root = Path(os.environ.get('MAIBOT_INSTANCE_DIR') or Path(__file__).resolve().parents[1])
    db = sqlite3.connect(root/'data/plugins/wechatbot.autonomous-life/life.sqlite3', timeout=10)
    try:
        with db:
            db.execute("UPDATE shares SET state=? WHERE fingerprint=? AND state<>'sent'",
                       (state, hashlib.sha256(('native-fun:'+mid).encode()).hexdigest()))
    finally:
        db.close()
