from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager


DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "llm_learning.sqlite3",
)


class ProfileStore:
    """Small SQLite store for derived, non-conversational group statistics."""

    def __init__(self, path: str | None = None):
        self.path = os.path.abspath(path or DEFAULT_DB_PATH)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._batch_local = threading.local()
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _managed_connection(self):
        existing = getattr(self._batch_local, "connection", None)
        if existing is not None:
            yield existing
            return
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self):
        with self._managed_connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS group_profiles (
                    group_id TEXT PRIMARY KEY,
                    updated_at INTEGER NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0,
                    style_json TEXT NOT NULL DEFAULT '{}',
                    top_terms_json TEXT NOT NULL DEFAULT '[]',
                    source_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS slang_terms (
                    group_id TEXT NOT NULL,
                    normalized_phrase TEXT NOT NULL,
                    phrase TEXT NOT NULL,
                    occurrence_count INTEGER NOT NULL DEFAULT 0,
                    speaker_count INTEGER NOT NULL DEFAULT 0,
                    first_seen INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    safe_to_use INTEGER NOT NULL DEFAULT 0,
                    examples_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY (group_id, normalized_phrase)
                );
                CREATE TABLE IF NOT EXISTS term_speakers (
                    group_id TEXT NOT NULL,
                    normalized_phrase TEXT NOT NULL,
                    speaker TEXT NOT NULL,
                    PRIMARY KEY (group_id, normalized_phrase, speaker)
                );
                CREATE TABLE IF NOT EXISTS message_fingerprints (
                    group_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    seen_at INTEGER NOT NULL,
                    PRIMARY KEY (group_id, fingerprint)
                );
                CREATE INDEX IF NOT EXISTS idx_slang_prompt
                    ON slang_terms(group_id, safe_to_use, confidence DESC, occurrence_count DESC);
                """
            )

    def reset_group(self, group_id: str):
        with self._managed_connection() as connection:
            connection.execute("DELETE FROM group_profiles WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM slang_terms WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM term_speakers WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM message_fingerprints WHERE group_id = ?", (group_id,))

    def replace_profile(self, group_id: str, style: dict, message_count: int, terms: list[dict]):
        """Write one already-aggregated offline profile in a single transaction."""
        now = int(time.time())
        with self._managed_connection() as connection:
            connection.execute("DELETE FROM group_profiles WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM slang_terms WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM term_speakers WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM message_fingerprints WHERE group_id = ?", (group_id,))
            safe_terms = [item for item in terms if item.get("safe_to_use")]
            connection.execute(
                "INSERT INTO group_profiles(group_id, updated_at, message_count, style_json, top_terms_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    group_id,
                    now,
                    int(message_count),
                    json.dumps(style or {}, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(safe_terms[:24], ensure_ascii=False, separators=(",", ":")),
                ),
            )
            connection.executemany(
                "INSERT INTO slang_terms(" 
                "group_id, normalized_phrase, phrase, occurrence_count, speaker_count, "
                "first_seen, last_seen, confidence, safe_to_use, examples_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        group_id, item["normalized_phrase"], item["phrase"],
                        int(item["occurrence_count"]), int(item["speaker_count"]),
                        int(item.get("first_seen", now)), int(item.get("last_seen", now)),
                        float(item["confidence"]), int(bool(item["safe_to_use"])),
                        json.dumps(item.get("examples") or [], ensure_ascii=False, separators=(",", ":")),
                    )
                    for item in terms
                ],
            )
            connection.executemany(
                "INSERT OR IGNORE INTO term_speakers(group_id, normalized_phrase, speaker) VALUES (?, ?, ?)",
                [
                    (group_id, item["normalized_phrase"], str(speaker))
                    for item in terms
                    for speaker in item.get("speakers") or []
                ],
            )

    def record_message(self, observation: dict, terms: list[dict], min_term_count: int = 3):
        group_id = str(observation.get("group_id") or "unknown").strip() or "unknown"
        fingerprint = str(observation.get("fingerprint") or "").strip()
        if not fingerprint:
            return False
        now = int(observation.get("timestamp") or time.time())
        content = str(observation.get("content") or "").strip()
        speaker = str(observation.get("speaker") or "unknown").strip() or "unknown"

        with self._managed_connection() as connection:
            inserted = connection.execute(
                "INSERT OR IGNORE INTO message_fingerprints(group_id, fingerprint, seen_at) VALUES (?, ?, ?)",
                (group_id, fingerprint, now),
            ).rowcount
            if not inserted:
                return False

            profile = connection.execute(
                "SELECT style_json, message_count FROM group_profiles WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            style = _load_json(profile["style_json"], {}) if profile else {}
            style = _update_style(style, observation)
            count = int(profile["message_count"] if profile else 0) + 1
            connection.execute(
                """
                INSERT INTO group_profiles(group_id, updated_at, message_count, style_json, top_terms_json)
                VALUES (?, ?, ?, ?, '[]')
                ON CONFLICT(group_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    message_count=excluded.message_count,
                    style_json=excluded.style_json
                """,
                (group_id, now, count, json.dumps(style, ensure_ascii=False, separators=(",", ":"))),
            )

            for term in terms:
                normalized = str(term.get("normalized_phrase") or "").strip()
                phrase = str(term.get("phrase") or normalized).strip()
                if not normalized or not phrase:
                    continue
                existing = connection.execute(
                    "SELECT occurrence_count, first_seen, examples_json FROM slang_terms "
                    "WHERE group_id = ? AND normalized_phrase = ?",
                    (group_id, normalized),
                ).fetchone()
                occurrence = int(existing["occurrence_count"] if existing else 0) + 1
                first_seen = int(existing["first_seen"] if existing else now)
                examples = _load_json(existing["examples_json"], []) if existing else []
                if content and content not in examples:
                    examples = (examples + [content[:160]])[-3:]
                connection.execute(
                    """
                    INSERT INTO slang_terms(
                        group_id, normalized_phrase, phrase, occurrence_count, speaker_count,
                        first_seen, last_seen, confidence, safe_to_use, examples_json
                    ) VALUES (?, ?, ?, ?, 0, ?, ?, 0, 0, ?)
                    ON CONFLICT(group_id, normalized_phrase) DO UPDATE SET
                        phrase=excluded.phrase,
                        occurrence_count=excluded.occurrence_count,
                        last_seen=excluded.last_seen,
                        examples_json=excluded.examples_json
                    """,
                    (
                        group_id, normalized, phrase, occurrence, first_seen, now,
                        json.dumps(examples, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO term_speakers(group_id, normalized_phrase, speaker) VALUES (?, ?, ?)",
                    (group_id, normalized, speaker),
                )
                speaker_count = connection.execute(
                    "SELECT COUNT(*) FROM term_speakers WHERE group_id = ? AND normalized_phrase = ?",
                    (group_id, normalized),
                ).fetchone()[0]
                confidence, safe = _term_confidence(
                    occurrence, int(speaker_count), min_term_count
                )
                connection.execute(
                    "UPDATE slang_terms SET speaker_count = ?, confidence = ?, safe_to_use = ? "
                    "WHERE group_id = ? AND normalized_phrase = ?",
                    (speaker_count, confidence, int(safe), group_id, normalized),
                )

            self._refresh_top_terms(connection, group_id)
        return True

    def record_messages(self, items: list[tuple[dict, list[dict], int]]) -> int:
        """Record a small batch with one transaction and one connection."""
        if not items:
            return 0
        with self._managed_connection() as connection:
            self._batch_local.connection = connection
            try:
                count = 0
                for observation, terms, minimum in items:
                    if self.record_message(observation, terms, minimum):
                        count += 1
                return count
            finally:
                self._batch_local.connection = None

    def _refresh_top_terms(self, connection, group_id: str):
        rows = connection.execute(
            "SELECT phrase, occurrence_count, speaker_count, confidence FROM slang_terms "
            "WHERE group_id = ? AND safe_to_use = 1 ORDER BY confidence DESC, occurrence_count DESC LIMIT 24",
            (group_id,),
        ).fetchall()
        terms = [dict(row) for row in rows]
        connection.execute(
            "UPDATE group_profiles SET top_terms_json = ? WHERE group_id = ?",
            (json.dumps(terms, ensure_ascii=False, separators=(",", ":")), group_id),
        )

    def get_profile(self, group_id: str, max_terms: int = 12) -> dict:
        group_id = str(group_id or "unknown").strip() or "unknown"
        with self._managed_connection() as connection:
            profile = connection.execute(
                "SELECT * FROM group_profiles WHERE group_id = ?", (group_id,)
            ).fetchone()
            if not profile:
                return {}
            terms = connection.execute(
                "SELECT phrase, occurrence_count, speaker_count, confidence, examples_json "
                "FROM slang_terms WHERE group_id = ? AND safe_to_use = 1 "
                "ORDER BY confidence DESC, occurrence_count DESC LIMIT ?",
                (group_id, max(1, int(max_terms))),
            ).fetchall()
            result = dict(profile)
            result["style"] = _load_json(result.pop("style_json"), {})
            result["top_terms"] = [
                {
                    "phrase": row["phrase"],
                    "occurrence_count": row["occurrence_count"],
                    "speaker_count": row["speaker_count"],
                    "confidence": row["confidence"],
                    "examples": _load_json(row["examples_json"], [])[:2],
                }
                for row in terms
            ]
            return result


def _load_json(value, default):
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(default)) else default
    except Exception:
        return default


def _update_style(style: dict, observation: dict) -> dict:
    content = str(observation.get("content") or "").strip()
    chars = len(content)
    style = dict(style or {})
    style["total_chars"] = int(style.get("total_chars", 0)) + chars
    style["short_messages"] = int(style.get("short_messages", 0)) + int(chars <= 12)
    style["medium_messages"] = int(style.get("medium_messages", 0)) + int(13 <= chars <= 45)
    style["long_messages"] = int(style.get("long_messages", 0)) + int(chars > 45)
    style["question_messages"] = int(style.get("question_messages", 0)) + int("?" in content or "？" in content)
    style["exclamation_messages"] = int(style.get("exclamation_messages", 0)) + int("!" in content or "！" in content)
    style["emoji_messages"] = int(style.get("emoji_messages", 0)) + int(any(ord(char) > 0x1F000 for char in content))
    style["reply_messages"] = int(style.get("reply_messages", 0)) + int(bool(observation.get("reply_to")))
    style["command_messages"] = int(style.get("command_messages", 0)) + int(content.startswith("/"))
    style["media_messages"] = int(style.get("media_messages", 0)) + int(bool(observation.get("is_media")))
    style["speaker_count"] = max(int(style.get("speaker_count", 0)), int(observation.get("speaker_count", 0) or 0))
    return style


def _term_confidence(occurrence: int, speakers: int, minimum: int):
    count_score = min(1.0, occurrence / max(minimum * 3, 1))
    speaker_score = min(1.0, speakers / 3)
    confidence = round(min(0.97, 0.28 + count_score * 0.42 + speaker_score * 0.27), 3)
    safe = occurrence >= minimum and (speakers >= 2 or occurrence >= minimum + 2)
    return confidence, safe
