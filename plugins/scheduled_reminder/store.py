"""User-scoped durable reminders; scheduling never sends platform messages."""
from __future__ import annotations

from datetime import datetime
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import time
from zoneinfo import ZoneInfo


class ReminderStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    platform TEXT NOT NULL, group_id TEXT NOT NULL,
                    stream_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                    account_id TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
                    owner_name TEXT NOT NULL, source_message_id TEXT NOT NULL,
                    title TEXT NOT NULL, due_at REAL NOT NULL, remind_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, host_task_id TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reminders_due ON reminders(status,due_at,retry_at);
                CREATE INDEX IF NOT EXISTS reminders_owner ON reminders(platform,group_id,owner_id,status);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def scope(request):
        return request['platform'], request['group_id'], request['user_id']

    def manage(self, action, request, *, title='', remind_at='', event_id=None, timezone='Asia/Shanghai'):
        if action == 'list':
            with self.connect() as db:
                rows = db.execute("SELECT id,title,remind_at,status,host_task_id,last_error FROM reminders "
                    "WHERE platform=? AND group_id=? AND owner_id=? AND status IN ('pending','submitting','uncertain') "
                    "ORDER BY due_at,id LIMIT 20", self.scope(request)).fetchall()
            return dict(success=True, action='list', events=[dict(r) for r in rows])
        if action == 'cancel':
            if type(event_id) is not int or event_id < 1:
                raise ValueError('取消提醒需要查询返回的整数 event_id')
            with self.connect() as db:
                cursor = db.execute("UPDATE reminders SET status='cancelled',updated_at=? WHERE id=? "
                    "AND platform=? AND group_id=? AND owner_id=? AND status='pending'",
                    (time.time(), event_id, *self.scope(request)))
            if not cursor.rowcount:
                raise ValueError('当前群中没有属于你且可以取消的待处理提醒')
            return dict(success=True, action='cancelled', event_id=event_id)
        if action != 'schedule':
            raise ValueError('action 只能是 schedule、list、cancel')
        title = ' '.join(str(title or '').split())
        if not title or len(title) > 240:
            raise ValueError('title 应为 1–240 个字符的提醒内容')
        try:
            when = datetime.fromisoformat(str(remind_at).strip().replace('Z', '+00:00'))
        except ValueError as exc:
            raise ValueError('remind_at 必须是包含时区的 ISO-8601 日期时间') from exc
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError('remind_at 必须包含明确时区，例如 +08:00')
        now = time.time()
        due = when.timestamp()
        if not now + 10 <= due <= now + 366 * 86400:
            raise ValueError('提醒时间必须在 10 秒至 366 天之后；时间不明确时先向用户确认')
        local = when.astimezone(ZoneInfo(timezone)).isoformat(timespec='seconds')
        key = hashlib.sha256(repr((*self.scope(request),title.casefold(),int(due // 60))).encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute("SELECT * FROM reminders WHERE dedupe_key=?", (key,)).fetchone()
            if existing and existing['status'] in {'pending', 'submitting', 'uncertain'}:
                return dict(success=True, action='scheduled', event_id=existing['id'], title=existing['title'],
                            remind_at=existing['remind_at'], status=existing['status'], deduplicated=True)
            if existing:
                key += ':' + str(time.time_ns())
            count = db.execute("SELECT count(*) FROM reminders WHERE platform=? AND group_id=? AND owner_id=? "
                "AND status IN ('pending','submitting','uncertain')", self.scope(request)).fetchone()[0]
            if count >= 20:
                raise ValueError('每个用户在每个群最多保留 20 条活动提醒')
            cursor = db.execute("INSERT INTO reminders (dedupe_key,platform,group_id,stream_id,owner_id,owner_name,"
                "source_message_id,title,due_at,remind_at,created_at,updated_at,account_id,scope) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (key, request['platform'], request['group_id'], request['stream_id'], request['user_id'],
                 request.get('user_name',''), request['message_id'], title, due, local, now, now,
                 request.get('account_id',''), request.get('scope','')))
            return dict(success=True, action='scheduled', event_id=cursor.lastrowid, title=title,
                        remind_at=local, status='pending', deduplicated=False,
                        delivery='到期后提交 MaiBot 处理；提交成功不代表微信消息已送达')

    def recover(self):
        # Host may already have accepted the RPC before the Runner died.
        with self.connect() as db:
            db.execute("UPDATE reminders SET status='uncertain',last_error='runner_interrupted_submission',updated_at=? "
                       "WHERE status='submitting'", (time.time(),))

    def claim_due(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute("SELECT * FROM reminders WHERE status='pending' AND due_at<=? AND retry_at<=? "
                              "ORDER BY due_at,id LIMIT 1", (now, now)).fetchall()
            for row in rows:
                db.execute("UPDATE reminders SET status='submitting',attempts=attempts+1,updated_at=? WHERE id=?",
                           (now, row['id']))
            return [dict(row) | {'attempts':row['attempts']+1} for row in rows]

    def finish(self, event, status, *, error='', task_id=''):
        if status == 'pending' and event['attempts'] >= 5:
            status = 'failed'
        retry_at = time.time() + min(300, 5 * 2 ** min(event['attempts']-1, 6))
        with self.connect() as db:
            db.execute("UPDATE reminders SET status=?,last_error=?,host_task_id=?,retry_at=?,updated_at=? "
                       "WHERE id=? AND status='submitting'",
                       (status, str(error)[:500], task_id, retry_at, time.time(), event['id']))
