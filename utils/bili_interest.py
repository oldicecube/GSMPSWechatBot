"""Shared, transactional Story tag weights for native repost and shell /bv."""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time

MODES = {'replace', 'update', 'add', 'merge'}


def parse_report(text):
    if not isinstance(text, str) or len(text.encode('utf8')) > 60000:
        raise ValueError('标签报告最多60KB')
    values = {}
    for number, line in enumerate(text.lstrip('\ufeff').splitlines(), 1):
        row = re.match(r'^\s*\d+\s+(.+?)\s+(\d+(?:\.\d+)?)\s*$', line)
        if not row:
            if re.match(r'^\s*\d+\s+', line):
                raise ValueError(f'标签报告第{number}行格式或权重无效')
            continue
        name, weight = row.group(1).strip(), float(row.group(2))
        if not name or len(name) > 120 or not math.isfinite(weight) or not 0 <= weight <= 1000000:
            raise ValueError('标签名或权重超出限制')
        values[name] = values.get(name, 0) + weight
    if not values or len(values) > 1000:
        raise ValueError('未找到有效的“排名 标签 出现视频数”记录，最多1000个标签')
    return values


def default_path():
    root = Path(os.environ.get('MAIBOT_INSTANCE_DIR') or Path(__file__).resolve().parents[1])
    return root / 'data/plugins/github.erhan111119.mai-repost-shit/story.sqlite3'


class TagStore:
    def __init__(self, path=None):
        self.path = Path(path or default_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS tags(name TEXT PRIMARY KEY,weight REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS parsed(event TEXT PRIMARY KEY,bvid TEXT,stamp REAL);
                CREATE TABLE IF NOT EXISTS story_seen(aid TEXT PRIMARY KEY,stamp REAL,state TEXT);''')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def tags(self):
        with self.db() as db:
            return dict(db.execute('SELECT name,weight FROM tags ORDER BY weight DESC,name'))

    def get(self, key, default=None):
        with self.db() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def seed(self, values):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM meta WHERE key='preset_seeded'").fetchone():
                return
            db.executemany('INSERT OR IGNORE INTO tags VALUES(?,?)', values.items())
            db.execute("INSERT INTO meta VALUES('preset_seeded','true')")

    def import_tags(self, values, mode):
        if mode not in MODES:
            raise ValueError('请选择覆盖、部分覆盖、新增或合并')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if mode == 'replace':
                db.execute('DELETE FROM tags')
            for name, weight in values.items():
                if mode == 'add':
                    db.execute('INSERT OR IGNORE INTO tags VALUES(?,?)', (name, weight))
                elif mode == 'merge':
                    db.execute('INSERT INTO tags VALUES(?,?) ON CONFLICT(name) DO UPDATE SET weight=min(1000000,tags.weight+excluded.weight)', (name, weight))
                else:
                    db.execute('INSERT OR REPLACE INTO tags VALUES(?,?)', (name, weight))

    def edit(self, name, weight, delete=False):
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise ValueError('标签名不能为空且最多120字')
        weight = float(weight)
        if not math.isfinite(weight) or not 0 <= weight <= 1000000:
            raise ValueError('权重应在0到1000000之间')
        with self.db() as db:
            if delete:
                db.execute('DELETE FROM tags WHERE name=?', (name.strip(),))
            else:
                db.execute('INSERT OR REPLACE INTO tags VALUES(?,?)', (name.strip(), weight))

    def record_parse(self, bvid, tags, event):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            cur = db.execute('INSERT OR IGNORE INTO parsed VALUES(?,?,?)', (event, bvid, time.time()))
            if not cur.rowcount:
                return False
            for name in set(str(tag).strip() for tag in tags):
                if name and len(name) <= 120:
                    db.execute('INSERT INTO tags VALUES(?,1) ON CONFLICT(name) DO UPDATE SET weight=min(1000000,weight+1)', (name,))
            db.execute('DELETE FROM parsed WHERE stamp<?', (time.time()-90*86400,))
        return True

    def seen(self, aid):
        with self.db() as db:
            return db.execute('SELECT 1 FROM story_seen WHERE aid=? AND stamp>?', (str(aid), time.time()-7*86400)).fetchone() is not None

    def mark(self, aid, state):
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO story_seen VALUES(?,?,?)', (str(aid), time.time(), state))
            db.execute('DELETE FROM story_seen WHERE stamp<?', (time.time()-7*86400,))
