"""Remove exact legacy duplicates only after validating the native image.

Dry run by default. Archives affected rows before a short transaction; keeps
native registration, bans, descriptions and usage untouched. No bot restart.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[1]


def repair_legacy_emoji_rows(database: Path, instance_root: Path, apply=False):
    root = instance_root.resolve()
    with sqlite3.connect(database, timeout=10) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        old = connection.execute("SELECT * FROM images WHERE image_type = 'emoji'").fetchall()
        native = connection.execute("SELECT * FROM images WHERE image_type = 'EMOJI'").fetchall()
        references = []
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            quoted = table.replace('"', '""')
            for fk in connection.execute(f'PRAGMA foreign_key_list("{quoted}")'):
                if fk[2] == 'images':
                    references.append((quoted, fk[3].replace('"', '""')))
        removed, skipped, snapshots = [], [], []
        for row in old:
            candidates = [n for n in native if n['image_hash'] == row['image_hash']]
            if len(candidates) != 1:
                skipped.append(row['id'])
                continue
            target = candidates[0]
            path = Path(target['full_path'])
            path = (path if path.is_absolute() else root / path).resolve()
            if not path.is_relative_to(root) or not path.is_file() or target['no_file_flag']:
                skipped.append(row['id'])
                continue
            if hashlib.sha256(path.read_bytes()).hexdigest() != row['image_hash']:
                skipped.append(row['id'])
                continue
            if any(connection.execute(f'SELECT 1 FROM "{table}" WHERE "{column}"=? LIMIT 1',
                                      (row['id'],)).fetchone() for table, column in references):
                skipped.append(row['id'])
                continue
            snapshots.append({'legacy': dict(row), 'native': dict(target)})
            removed.append(row['id'])
        archive = None
        if apply and snapshots:
            folder = root / 'data' / 'backups'
            folder.mkdir(parents=True, exist_ok=True)
            archive = folder / ('legacy-emoji-rows-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.json')
            archive.write_text(json.dumps(snapshots, ensure_ascii=False, indent=2), encoding='utf-8')
            connection.executemany("DELETE FROM images WHERE id=? AND image_type='emoji'",
                                   [(identifier,) for identifier in removed])
        if apply:
            connection.commit()
        else:
            connection.rollback()
        return {'applied': apply, 'removed_duplicate_ids': removed, 'skipped_ids': skipped,
                'archive': str(archive) if archive else None}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(repair_legacy_emoji_rows(ROOT / 'data' / 'MaiBot.db', ROOT, args.apply), ensure_ascii=False))
