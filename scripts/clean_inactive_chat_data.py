"""Backup, audit and prune confirmed inactive chat scopes; preserve active aliases."""
from pathlib import Path
from datetime import datetime
import argparse
import json
import shutil
import sqlite3
from contextlib import closing

ROOT = Path(__file__).resolve().parents[1]


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def clean(root, apply=False):
    root = Path(root).resolve()
    config = json.loads((root / 'config.json').read_text(encoding='utf8'))
    targets = config.get('target_group') or []
    targets = {targets} if isinstance(targets, str) else set(targets)
    if not targets:
        raise ValueError('No verified configured target groups; refusing cleanup')
    report = {'target_groups': sorted(targets), 'changes': [], 'archives': []}
    databases = [root / 'data' / n for n in ('MaiBot.db', 'llm_learning.sqlite3', 'memory.sqlite3', 'bot.sqlite3')]
    active_aliases = set(targets)
    active_sessions = set()
    expression_aliases = {}
    with closing(sqlite3.connect('file:' + str(databases[0]) + '?mode=ro', uri=True)) as connection:
        for sid, platform, gid, name in connection.execute('SELECT session_id,platform,group_id,group_name FROM chat_sessions'):
            if platform == 'wechat' and name in targets:
                active_aliases.add(gid)
                active_sessions.add(sid)
                expression_aliases.update({name: sid, gid: sid})
    # Do not infer unknown ID -> group mappings or touch unrelated private chats.
    stale = set()
    folder = root / 'data/groups'
    if folder.exists():
        stale.update(p.name for p in folder.iterdir() if p.is_dir() and p.name not in active_aliases)
    stale.update({'unknown', 'group', 's', ''})
    backup = root / 'data/backups' / ('inactive-chat-cleanup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    if apply:
        backup.mkdir(parents=True)
        for path in databases:
            if path.exists():
                with closing(sqlite3.connect(str(path))) as source, closing(sqlite3.connect(str(backup / path.name))) as snapshot:
                    source.backup(snapshot)
        report['backup'] = str(backup)
    for path in databases:
        if not path.exists():
            continue
        with closing(sqlite3.connect(str(path), timeout=30)) as connection:
            connection.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
            tables = [r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                cols = {r[1] for r in connection.execute('PRAGMA table_info(' + quoted(table) + ')')}
                column = 'group_id' if 'group_id' in cols else 'session_id' if table == 'conversation_messages' else None
                if not column:
                    continue
                values = {r[0] for r in connection.execute('SELECT DISTINCT ' + quoted(column) + ' FROM ' + quoted(table))}
                # Non-target group names can only be removed if independently
                # confirmed by archived group folders, not by format heuristics.
                for value in values & stale:
                    if value in active_aliases or value in active_sessions:
                        continue
                    if path.name == 'MaiBot.db' and column == 'group_id' and value == '':
                        continue  # a real native private chat has no group ID
                    clause = quoted(column) + ' = ?'
                    count = connection.execute('SELECT COUNT(*) FROM ' + quoted(table) + ' WHERE ' + clause, (value,)).fetchone()[0]
                    if count:
                        report['changes'].append(dict(database=path.name, table=table, scope=value, removed=count))
                        if apply:
                            connection.execute('DELETE FROM ' + quoted(table) + ' WHERE ' + clause, (value,))
            if path.name == 'MaiBot.db':
                for alias, sid in expression_aliases.items():
                    count = connection.execute('SELECT COUNT(*) FROM expressions WHERE session_id=?', (alias,)).fetchone()[0]
                    if count:
                        report['changes'].append(dict(database=path.name, table='expressions', scope=alias, normalized=count))
                        if apply:
                            connection.execute('UPDATE expressions SET session_id=? WHERE session_id=?', (sid, alias))
                if apply:
                    # Preserve review state, source and example evidence; merge
                    # only identical records after canonical session migration.
                    duplicates = connection.execute('SELECT session_id,situation,style,content_list,checked,modified_by,COUNT(*) FROM expressions GROUP BY session_id,situation,style,content_list,checked,modified_by HAVING COUNT(*)>1').fetchall()
                    removed = 0
                    for sid, situation, style, content, checked, modified, count in duplicates:
                        if sid not in active_sessions:
                            continue
                        ids = connection.execute('SELECT id FROM expressions WHERE session_id=? AND situation=? AND style=? AND content_list=? AND checked=? AND modified_by IS ? ORDER BY count DESC,id', (sid, situation, style, content, checked, modified)).fetchall()
                        for item in ids[1:]:
                            connection.execute('DELETE FROM expressions WHERE id=?', item)
                            removed += 1
                    report['changes'].append(dict(database=path.name, table='expressions', duplicates_removed=removed))
            connection.commit() if apply else connection.rollback()
    for name in sorted(stale - {''}):
        source = (folder / name).resolve()
        if not source.is_dir():
            continue
        if not source.is_relative_to(folder.resolve()) or name in active_aliases:
            raise ValueError('Unsafe archive path')
        report['archives'].append(str(source))
        if apply:
            target = backup / 'groups' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
    if apply:
        (backup / 'audit.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--apply', action='store_true')
    print(json.dumps(clean(ROOT, parser.parse_args().apply), ensure_ascii=False, indent=2))
