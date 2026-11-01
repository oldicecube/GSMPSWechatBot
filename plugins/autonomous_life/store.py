"""Small durable journal; transactions reserve side effects before RPC.

Unknown delivery is terminal, never retried after restart. Budget is charged on
attempt rather than receipt. Data are per real stream, never a system prompt.
"""
import hashlib
import json
from pathlib import Path
import sqlite3
from contextlib import contextmanager


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,day TEXT,stamp REAL,stream TEXT,kind TEXT,state TEXT,data TEXT);
            CREATE INDEX IF NOT EXISTS event_budget ON events(day,kind);
            CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,stream TEXT,title TEXT,status TEXT,note TEXT,code TEXT,updated REAL);
            CREATE TABLE IF NOT EXISTS interests(stream TEXT,topic TEXT,updated REAL,PRIMARY KEY(stream,topic));
            CREATE TABLE IF NOT EXISTS shares(fingerprint TEXT PRIMARY KEY,day TEXT,stamp REAL,stream TEXT,person TEXT,state TEXT);
            CREATE TABLE IF NOT EXISTS repost_shares(fingerprint TEXT PRIMARY KEY,day TEXT,stamp REAL,stream TEXT,state TEXT);
            CREATE INDEX IF NOT EXISTS repost_budget ON repost_shares(stream,day,stamp);
            CREATE TABLE IF NOT EXISTS progress_updates(fingerprint TEXT PRIMARY KEY,stream TEXT,project TEXT,stamp REAL,payload TEXT,state TEXT);
            ''')
            db.execute("UPDATE events SET state='interrupted' WHERE state='running'")
            db.execute("UPDATE shares SET state='unknown' WHERE state='reserved'")
            db.execute("UPDATE repost_shares SET state='unknown' WHERE state='reserved'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def seed_bootstrap(self, request_id, stamp, jobs):
        """Consume one boot guide globally, including the original group set, atomically."""
        marker = 'bootstrap_consumed:' + request_id
        added = False
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM meta WHERE key=?', (marker,)).fetchone():
                return False
            for group, job in jobs.items():
                name = 'project_assignment:' + group
                row = db.execute('SELECT value FROM meta WHERE key=?', (name,)).fetchone()
                old = json.loads(row[0]) if row else None
                if old and old.get('request_id') == request_id:
                    # Upgrade a previously registered task without restarting its work.
                    job = {**old, 'group': group, 'guidance_pending': not old.get('researched', False)
                           and old.get('state') == 'queued'}
                else:
                    added = True
                db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (name, json.dumps(job, ensure_ascii=False)))
            db.execute('INSERT INTO meta VALUES(?,?)', (marker, json.dumps(dict(at=stamp, groups=list(jobs)))))
        return added

    def queue_progress(self, fingerprint, stream, project, stamp, payload):
        data = json.dumps(payload, ensure_ascii=False)
        if len(data) > 24000:
            return False
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM progress_updates WHERE fingerprint=?', (fingerprint,)).fetchone():
                return False
            # Keep the latest meaningful update for each project, not a backlog of old milestones.
            db.execute("UPDATE progress_updates SET state='superseded' WHERE stream=? AND project=? AND state='pending'", (stream, project))
            db.execute('INSERT INTO progress_updates VALUES(?,?,?,?,?,?)', (fingerprint, stream, project, stamp, data, 'pending'))
            db.execute("UPDATE progress_updates SET state='superseded' WHERE stream=? AND state='pending' AND fingerprint NOT IN "
                       "(SELECT fingerprint FROM progress_updates WHERE stream=? AND state='pending' ORDER BY stamp DESC LIMIT 8)", (stream, stream))
        return True

    def pending_progress(self, stream, stamp):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM progress_updates WHERE stream=? AND state='pending' AND stamp>? ORDER BY stamp DESC LIMIT 8", (stream, stamp - 3*86400))]

    def progress_state(self, fingerprint, state):
        with self.db() as db:
            db.execute('UPDATE progress_updates SET state=? WHERE fingerprint=?', (state, fingerprint))

    def share_state(self, fingerprint):
        with self.db() as db:
            row = db.execute('SELECT state FROM shares WHERE fingerprint=?', (fingerprint,)).fetchone()
        return row[0] if row else None

    def count(self, day, kind):
        with self.db() as db:
            return self._count(db, day, kind)

    @staticmethod
    def _count(db, day, kind):
        rows = db.execute('SELECT data FROM events WHERE day=? AND kind=?', (day, kind)).fetchall()
        if kind != 'action':
            return len(rows)
        # Older journals charged private thoughts as real actions. Interpret them
        # correctly without deleting history or resetting real side-effect limits.
        total = 0
        for row in rows:
            try:
                data = json.loads(row[0])
                action = data.get('action') or (data.get('decision') or {}).get('action')
            except (ValueError, TypeError, AttributeError):
                action = None
            total += action != 'think'
        return total

    def reserve(self, day, stamp, stream, kind, limit, data=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if self._count(db, day, kind) >= limit:
                return None
            cur = db.execute('INSERT INTO events(day,stamp,stream,kind,state,data) VALUES(?,?,?,?,?,?)',
                             (day, stamp, stream, kind, 'running', json.dumps(data or {}, ensure_ascii=False)))
            return cur.lastrowid

    def finish(self, event, result, state='done'):
        payload = json.dumps(result, ensure_ascii=False)
        if len(payload) > 24000:
            payload = json.dumps({'truncated_data': payload[:23500]}, ensure_ascii=False)
        with self.db() as db:
            db.execute('UPDATE events SET state=?,data=? WHERE id=?',
                       (state, payload, event))

    def snapshot(self, stream):
        with self.db() as db:
            projects = [dict(r) for r in db.execute('SELECT * FROM projects WHERE stream=? ORDER BY updated DESC LIMIT 8', (stream,))]
            notes = [dict(r) for r in db.execute("SELECT stamp,kind,state,data FROM events WHERE stream=? AND kind IN ('action','thought') ORDER BY id DESC LIMIT 8", (stream,))]
            topics = [r[0] for r in db.execute('SELECT topic FROM interests WHERE stream=? ORDER BY updated DESC LIMIT 12', (stream,))]
        return dict(projects=projects, experiences=notes, interests=topics)

    def update_project(self, stream, project, stamp):
        # IDs are data, never filesystem paths. Max 16 projects including abandoned.
        if not isinstance(project, dict):
            return
        title = str(project.get('title', ''))[:120].strip()
        if not title:
            return
        key = hashlib.sha256((stream + title).encode()).hexdigest()[:20]
        status = project.get('status', 'active')
        if status not in {'active', 'paused', 'done', 'abandoned'}:
            raise ValueError('invalid project status')
        with self.db() as db:
            old = db.execute('SELECT code FROM projects WHERE id=?', (key,)).fetchone()
            code = project.get('code', old[0] if old else '')
            db.execute('INSERT OR REPLACE INTO projects VALUES(?,?,?,?,?,?,?)',
                       (key, stream, title, status, str(project.get('note', ''))[:2000], str(code)[:16000], stamp))
            db.execute('DELETE FROM projects WHERE stream=? AND id NOT IN (SELECT id FROM projects WHERE stream=? ORDER BY updated DESC LIMIT 16)', (stream, stream))

    def activity_budget(self, day, stream, stamp, action_limit, target, share_limit):
        """Observed counts, not model guesses; private thoughts never become work."""
        with self.db() as db:
            actions = self._count(db, day, 'action')
            attempted, sent = db.execute("SELECT count(*),coalesce(sum(state='sent'),0) FROM shares WHERE day=?", (day,)).fetchone()
            last_sent = db.execute("SELECT max(stamp) FROM shares WHERE state='sent'").fetchone()[0]
            rows = db.execute("SELECT data FROM events WHERE stream=? AND kind IN ('action','thought') ORDER BY id DESC LIMIT 24", (stream,)).fetchall()
        streak = 0
        for row in rows:
            try:
                data = json.loads(row[0])
                action = data.get('action') or (data.get('decision') or {}).get('action')
            except (TypeError, ValueError, AttributeError):
                break
            if action != 'think':
                break
            streak += 1
        return dict(actions_done=actions, actions_remaining=max(0,action_limit-actions), target_actions=target,
            shares_limit=share_limit, share_attempts_today=attempted, shares_sent_today=sent,
            shares_remaining=max(0,share_limit-attempted), consecutive_private_thoughts=streak,
            seconds_since_last_share=max(0,stamp-last_sent) if last_sent is not None else None,
            public_reply_required=False)

    def learn(self, stream, topics, stamp):
        with self.db() as db:
            for topic in topics[:3]:
                topic = str(topic)[:80].strip()
                if topic:
                    db.execute('INSERT OR REPLACE INTO interests VALUES(?,?,?)', (stream, topic, stamp))
            db.execute('DELETE FROM interests WHERE stream=? AND topic NOT IN (SELECT topic FROM interests WHERE stream=? ORDER BY updated DESC LIMIT 12)', (stream, stream))

    def reserve_share(self, day, stamp, stream, fingerprint, person, limit, gap, person_gap):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM shares WHERE day=?', (day,)).fetchone()[0] >= limit:
                return False
            if db.execute('SELECT 1 FROM shares WHERE stamp>?', (stamp - gap,)).fetchone():
                return False
            if person and db.execute('SELECT 1 FROM shares WHERE person=? AND stamp>?', (person, stamp - person_gap)).fetchone():
                return False
            if db.execute('SELECT 1 FROM shares WHERE fingerprint=?', (fingerprint,)).fetchone():
                return False
            db.execute('INSERT INTO shares VALUES(?,?,?,?,?,?)', (fingerprint, day, stamp, stream, person, 'reserved',))
        return True

    def reserve_repost(self, day, stamp, stream, fingerprint, limit, gap):
        """Independent per-destination media budget; ordinary life keeps its own limits."""
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT count(*) FROM repost_shares WHERE stream=? AND day=?', (stream, day)).fetchone()[0] >= limit:
                return False
            if db.execute('SELECT 1 FROM repost_shares WHERE stream=? AND stamp>?', (stream, stamp-gap)).fetchone():
                return False
            if db.execute('SELECT 1 FROM repost_shares WHERE fingerprint=?', (fingerprint,)).fetchone():
                return False
            db.execute('INSERT INTO repost_shares VALUES(?,?,?,?,?)', (fingerprint, day, stamp, stream, 'reserved'))
        return True

    def repost_result(self, fingerprint, state):
        with self.db() as db:
            db.execute('UPDATE repost_shares SET state=? WHERE fingerprint=?', (state, fingerprint))

    def share_result(self, fingerprint, state):
        with self.db() as db:
            db.execute('UPDATE shares SET state=? WHERE fingerprint=?', (state, fingerprint))

    def can_share(self, day, stamp, fingerprint, person, limit, gap, person_gap):
        """Read-only preflight; reserve_share still rechecks atomically before RPC."""
        with self.db() as db:
            return (db.execute('SELECT COUNT(*) FROM shares WHERE day=?', (day,)).fetchone()[0] < limit
                and not db.execute('SELECT 1 FROM shares WHERE stamp>?', (stamp-gap,)).fetchone()
                and not (person and db.execute('SELECT 1 FROM shares WHERE person=? AND stamp>?', (person,stamp-person_gap)).fetchone())
                and not db.execute('SELECT 1 FROM shares WHERE fingerprint=?', (fingerprint,)).fetchone())

    def prune(self, stamp):
        with self.db() as db:
            db.execute('DELETE FROM events WHERE stamp<?', (stamp - 90 * 86400,))
            db.execute('DELETE FROM shares WHERE stamp<?', (stamp - 90 * 86400,))
            db.execute('DELETE FROM repost_shares WHERE stamp<?', (stamp - 90 * 86400,))
            db.execute('DELETE FROM progress_updates WHERE stamp<?', (stamp - 3 * 86400,))
