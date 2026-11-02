"""Durable leisure directions, bounded native subagent runs and consumable reports."""
from contextlib import contextmanager
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import time
from uuid import uuid4
from zoneinfo import ZoneInfo


class FunJournal:
    def __init__(self, path=None):
        root = Path(os.environ.get('MAIBOT_INSTANCE_DIR') or Path(__file__).resolve().parents[1])
        self.path = Path(path or root/'data/plugins/wechatbot.autonomous-life/fun.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS directions(id TEXT PRIMARY KEY,stream TEXT,title TEXT,mode TEXT,state TEXT,objective TEXT,checkpoint TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,direction TEXT,stream TEXT,day TEXT,state TEXT,task TEXT,report TEXT,report_state TEXT,started REAL,finished REAL);
            CREATE TABLE IF NOT EXISTS steps(id INTEGER PRIMARY KEY,run TEXT,tool TEXT,code TEXT,result TEXT,stamp REAL);
            CREATE INDEX IF NOT EXISTS fun_pending ON runs(stream,report_state);
            ''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10); db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self, stream):
        with self.db() as db:
            directions = [dict(row) for row in db.execute('SELECT * FROM directions WHERE stream=? ORDER BY updated DESC LIMIT 8', (stream,))]
            reports = [dict(row) for row in db.execute("SELECT id,direction,state,report,finished FROM runs WHERE stream=? AND report_state='pending' ORDER BY finished DESC LIMIT 3", (stream,))]
            running = [dict(row) for row in db.execute("SELECT id,direction,task,started FROM runs WHERE stream=? AND state='running'", (stream,))]
        return {'directions': directions, 'reports': reports, 'running': running}

    def record_step(self, run, tool, arguments, result):
        """Store actual code/outputs separately; none of this is auto-posted."""
        code = str((arguments or {}).get('code') or '')[:16000] if tool in {'sandbox_program', 'restricted_python'} else ''
        raw = result.structured_content
        payload = {'success': result.success, 'error': result.error_message[:1000],
                   'data': raw if isinstance(raw, (dict, list)) else result.get_history_content()[:8000]}
        encoded = json.dumps(payload, ensure_ascii=False, default=str)
        if len(encoded.encode('utf8')) > 64000:
            encoded = json.dumps({'success': result.success, 'excerpt': result.get_history_content()[:8000]}, ensure_ascii=False)
        with self.db() as db:
            db.execute('INSERT INTO steps(run,tool,code,result,stamp) VALUES(?,?,?,?,?)', (run, tool, code, encoded, time.time()))
            db.execute('DELETE FROM steps WHERE id NOT IN (SELECT id FROM steps ORDER BY id DESC LIMIT 1000)')

    def continuation_material(self, direction):
        with self.db() as db:
            row = db.execute('SELECT steps.code,steps.result FROM steps JOIN runs ON runs.id=steps.run WHERE runs.direction=? AND steps.code<>? ORDER BY steps.id DESC LIMIT 1', (direction, '')).fetchone()
        return ('\n之前实际运行的代码：\n'+row['code']+'\n上次真实工具结果：\n'+row['result'][:4000]) if row else ''

    def import_legacy_projects(self, stream, projects):
        """Keep old unfinished directions, without asserting old plans were executed."""
        import hashlib
        with self.db() as db:
            for project in projects[:8]:
                title = str(project.get('title') or '').strip()
                if not title:
                    continue
                key = 'imported-'+hashlib.sha256((stream+title).encode()).hexdigest()[:24]
                state = project.get('status') if project.get('status') in {'paused', 'abandoned'} else 'paused'
                db.execute('INSERT OR IGNORE INTO directions VALUES(?,?,?,?,?,?,?,?)',
                    (key, stream, title[:120], 'code' if project.get('code') else 'hot', state,
                     '旧系统保留方向，先核验已有成果再决定继续', str(project.get('note') or '')[:2000],
                     float(project.get('updated') or 0)))

    def reserve(self, stream, title, mode, objective, direction='', limit=12):
        if mode not in {'hot', 'video', 'code'} or not title.strip() or not objective.strip():
            raise ValueError('方向需要标题、任务和 hot/video/code 类型')
        day = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        run = 'fun-'+uuid4().hex
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM runs WHERE day=?', (day,)).fetchone()[0] >= limit:
                raise ValueError('今日自主乐子任务已达上限')
            if db.execute("SELECT 1 FROM runs WHERE stream=? AND state='running'", (stream,)).fetchone():
                raise ValueError('本群已有方向在运行，可先搁置再换方向')
            if direction:
                row = db.execute('SELECT * FROM directions WHERE id=? AND stream=?', (direction, stream)).fetchone()
                if row is None or row['state'] == 'abandoned':
                    raise ValueError('方向不存在、不属于本群或已放弃')
                title, mode = row['title'], row['mode']
                checkpoint = row['checkpoint']
                db.execute("UPDATE directions SET state='active',objective=?,updated=? WHERE id=?", (objective[:3000], time.time(), direction))
            else:
                direction, checkpoint = uuid4().hex, ''
                db.execute('INSERT INTO directions VALUES(?,?,?,?,?,?,?,?)', (direction, stream, title[:120], mode, 'active', objective[:3000], '', time.time()))
            task = f'方向：{title}\n本轮任务：{objective[:3000]}\n上次进展（事实记录）：{checkpoint[:2000]}'
            db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?)', (run, direction, stream, day, 'running', task, '', '', time.time(), None))
        return {'id': run, 'direction': direction, 'task': task, 'mode': mode}

    def finish(self, run, state, report):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM runs WHERE id=?', (run,)).fetchone()
            if not row or row['state'] != 'running':
                return False
            db.execute("UPDATE runs SET state=?,report=?,report_state='pending',finished=? WHERE id=?", (state, str(report)[:2400], time.time(), run))
            db.execute('UPDATE directions SET checkpoint=?,updated=? WHERE id=?', (str(report)[:2000], time.time(), row['direction']))
        return True

    def transition(self, stream, direction, action):
        state = {'pause': 'paused', 'abandon': 'abandoned'}[action]
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM directions WHERE stream=? AND id=?', (stream, direction)).fetchone()
            if not row:
                raise ValueError('方向不属于本群')
            running = [r[0] for r in db.execute("SELECT id FROM runs WHERE direction=? AND state='running'", (direction,))]
            db.execute('UPDATE directions SET state=?,updated=? WHERE id=?', (state, time.time(), direction))
            db.execute("UPDATE runs SET state='stopped',finished=? WHERE direction=? AND state='running'", (time.time(), direction))
        return running

    def consume(self, stream, report):
        with self.db() as db:
            return bool(db.execute("UPDATE runs SET report_state='consumed' WHERE id=? AND stream=? AND report_state='pending'", (report, stream)).rowcount)
