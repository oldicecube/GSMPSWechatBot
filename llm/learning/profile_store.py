from __future__ import annotations

import json
import os
import random
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from functools import wraps



SLANG_SCENE_LABELS = {
    "general", "通用", "接梗", "自嘲", "惊讶", "赞同", "吐槽", "MC/游戏",
    "技术讨论", "反问", "结束话题", "uncertain", "其他",
}
_SLANG_UNSAFE_MARKERS = (
    "password", "passwd", "token", "secret", "credential", "诈骗",
    "色情", "裸聊", "自杀", "爆炸", "毒品", "个人信息",
)

# Expression patterns are sentence styles, not credential strings: "token" /
# "炸了" are common tech-chat words, so this set is deliberately narrower and
# focused on privacy/safety content rather than technical terms.
_EXPRESSION_UNSAFE_MARKERS = (
    "password", "passwd", "secret", "credential",
    "密码", "密碼", "身份证", "身分證", "银行卡", "銀行卡",
    "电话", "電話", "邮箱", "郵箱", "住址", "个人信息", "個人信息",
    "诈骗", "騙", "色情", "裸聊", "裸照", "自杀", "自殺", "毒品", "爆炸",
)

# Explicit general-language/system terms are never injected as group slang.
# Keep this conservative: frequency alone must not remove expressions such as
# "串子" or "吓哭了" that may have a group-specific meaning.
_GENERIC_SLANG_WORDS = {"还是", "服务", "服务器", "状态", "直接", "问题", "这么", "群里", "这样", "都是", "这是", "东西", "不会", "不要", "有点", "我是", "不能", "其实", "给我", "出来", "个人", "有人", "我的", "你的", "为什么", "可能", "不过", "游戏", "可以", "然后", "这个", "那个", "我们", "你们", "哈哈", "好的", "不是", "真的", "一下", "什么", "怎么", "已经", "因为", "所以", "没有", "就是", "对啊", "还有", "一个", "现在", "知道", "感觉", "应该", "如果", "但是", "自己", "时候", "的话", "年老", "务器", "器状态", "拍了", "了拍", "说话", "看看", "需要", "开始", "结束", "地方", "事情", "一样", "一样的"}


def _is_generic_slang_phrase(value: str) -> bool:
    return str(value or "").strip().casefold() in _GENERIC_SLANG_WORDS


def _maintenance_serialized(method):
    """Serialize learner writes and maintenance in this process."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._maintenance_lock:
            return method(self, *args, **kwargs)
    return wrapped


def _clean_slang_type(value) -> str:
    value = str(value or "通用").strip()[:40]
    return value or "通用"


def _clean_emotion(value) -> str:
    return str(value or "").strip()[:40]


def _normalize_emotion_intensity(value) -> float:
    try:
        return round(max(0.0, min(1.0, float(value))), 3)
    except (TypeError, ValueError):
        return 0.0


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
        self._maintenance_lock = threading.RLock()
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
            # Additive expression library (maibot-style situation -> pattern).
            # Must exist even when the rest of the schema is already ready.
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS style_expressions (
                    group_id TEXT NOT NULL,
                    situation TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    examples_json TEXT NOT NULL DEFAULT '[]',
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    last_seen INTEGER NOT NULL DEFAULT 0,
                    last_active_time INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, situation)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_style_expressions_prompt "
                "ON style_expressions(group_id, status, count DESC)"
            )
            for table, column, definition in (
                ("style_expressions", "last_active_time", "INTEGER NOT NULL DEFAULT 0"),
            ):
                columns = {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if column not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            if self._schema_is_ready(connection):
                # These tables belonged to the removed overlap/dedup pipeline.
                connection.execute("DROP TABLE IF EXISTS slang_relations")
                connection.execute("DROP TABLE IF EXISTS slang_exclusions")
                return
            had_scenario_table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='slang_scenarios'"
            ).fetchone() is not None
            connection.executescript(
                """
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
                    slang_type TEXT NOT NULL DEFAULT '通用',
                    emotion TEXT NOT NULL DEFAULT '',
                    emotion_intensity REAL NOT NULL DEFAULT 0,
                    llm_confidence REAL NOT NULL DEFAULT 0,
                    local_confidence REAL NOT NULL DEFAULT 0,
                    algorithm_confidence REAL NOT NULL DEFAULT 0,
                    overlap_ratio REAL NOT NULL DEFAULT 0,
                    independent_occurrence_count INTEGER NOT NULL DEFAULT 0,
                    shared_subphrase_count INTEGER NOT NULL DEFAULT 0,
                    covered_by_json TEXT NOT NULL DEFAULT '[]',
                    overlap_updated_at INTEGER NOT NULL DEFAULT 0,
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
                CREATE TABLE IF NOT EXISTS learning_samples (
                    group_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    timestamp INTEGER NOT NULL,
                    speaker TEXT NOT NULL,
                    speaker_name TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    PRIMARY KEY (group_id, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS style_cards (
                    group_id TEXT PRIMARY KEY,
                    version INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    source_message_count INTEGER NOT NULL DEFAULT 0,
                    card_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS style_expressions (
                    group_id TEXT NOT NULL,
                    situation TEXT NOT NULL,
                    pattern TEXT NOT NULL,
                    count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'active',
                    keywords_json TEXT NOT NULL DEFAULT '[]',
                    examples_json TEXT NOT NULL DEFAULT '[]',
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    last_seen INTEGER NOT NULL DEFAULT 0,
                    last_active_time INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, situation)
                );
                CREATE INDEX IF NOT EXISTS idx_style_expressions_prompt
                    ON style_expressions(group_id, status, count DESC);
                CREATE TABLE IF NOT EXISTS slang_scenarios (
                    group_id TEXT NOT NULL,
                    normalized_phrase TEXT NOT NULL,
                    phrase TEXT NOT NULL,
                    meaning TEXT NOT NULL DEFAULT '',
                    scenes_json TEXT NOT NULL DEFAULT '[]',
                    avoid_scenes_json TEXT NOT NULL DEFAULT '[]',
                    examples_json TEXT NOT NULL DEFAULT '[]',
                    confidence REAL NOT NULL DEFAULT 0,
                    speaker_count INTEGER NOT NULL DEFAULT 0,
                    occurrence_count INTEGER NOT NULL DEFAULT 0,
                    last_seen INTEGER NOT NULL DEFAULT 0,
                    safe_to_use INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'candidate',
                    slang_type TEXT NOT NULL DEFAULT '通用',
                    emotion TEXT NOT NULL DEFAULT '',
                    emotion_intensity REAL NOT NULL DEFAULT 0,
                    llm_confidence REAL NOT NULL DEFAULT 0,
                    algorithm_confidence REAL NOT NULL DEFAULT 0,
                    overlap_ratio REAL NOT NULL DEFAULT 0,
                    independent_occurrence_count INTEGER NOT NULL DEFAULT 0,
                    shared_subphrase_count INTEGER NOT NULL DEFAULT 0,
                    covered_by_json TEXT NOT NULL DEFAULT '[]',
                    overlap_updated_at INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (group_id, normalized_phrase)
                );
                CREATE TABLE IF NOT EXISTS maintenance_runs (
                    group_id TEXT NOT NULL,
                    task TEXT NOT NULL,
                    run_date TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (group_id, task, run_date)
                );
                CREATE INDEX IF NOT EXISTS idx_slang_prompt
                    ON slang_terms(group_id, safe_to_use, confidence DESC, occurrence_count DESC);
                CREATE INDEX IF NOT EXISTS idx_slang_scenario_prompt
                    ON slang_scenarios(group_id, status, safe_to_use, confidence DESC, occurrence_count DESC);
                """
            )
            for table, column, definition in (
                ("slang_terms", "slang_type", "TEXT NOT NULL DEFAULT '通用'"),
                ("slang_terms", "emotion", "TEXT NOT NULL DEFAULT ''"),
                ("slang_terms", "emotion_intensity", "REAL NOT NULL DEFAULT 0"),
                ("slang_scenarios", "slang_type", "TEXT NOT NULL DEFAULT '通用'"),
                ("slang_scenarios", "emotion", "TEXT NOT NULL DEFAULT ''"),
                ("slang_scenarios", "emotion_intensity", "REAL NOT NULL DEFAULT 0"),
                ("slang_terms", "llm_confidence", "REAL NOT NULL DEFAULT 0"),
                ("slang_terms", "local_confidence", "REAL NOT NULL DEFAULT 0"),
                ("slang_terms", "algorithm_confidence", "REAL NOT NULL DEFAULT 0"),
                ("slang_terms", "overlap_ratio", "REAL NOT NULL DEFAULT 0"),
                ("slang_terms", "independent_occurrence_count", "INTEGER NOT NULL DEFAULT 0"),
                ("slang_terms", "shared_subphrase_count", "INTEGER NOT NULL DEFAULT 0"),
                ("slang_terms", "covered_by_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("slang_terms", "overlap_updated_at", "INTEGER NOT NULL DEFAULT 0"),
                ("slang_scenarios", "llm_confidence", "REAL NOT NULL DEFAULT 0"),
                ("slang_scenarios", "algorithm_confidence", "REAL NOT NULL DEFAULT 0"),
                ("slang_scenarios", "overlap_ratio", "REAL NOT NULL DEFAULT 0"),
                ("slang_scenarios", "independent_occurrence_count", "INTEGER NOT NULL DEFAULT 0"),
                ("slang_scenarios", "shared_subphrase_count", "INTEGER NOT NULL DEFAULT 0"),
                ("slang_scenarios", "covered_by_json", "TEXT NOT NULL DEFAULT '[]'"),
                ("slang_scenarios", "overlap_updated_at", "INTEGER NOT NULL DEFAULT 0"),
            ):
                columns = {
                    str(row[1])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if column not in columns:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            # Historical style-card snapshots are never read by the runtime.
            connection.execute("DROP TABLE IF EXISTS style_card_versions")
            if not had_scenario_table:
                legacy_terms = connection.execute(
                    "SELECT group_id, normalized_phrase, phrase, occurrence_count, "
                    "speaker_count, confidence, last_seen, examples_json, safe_to_use, "
                    "slang_type, emotion, emotion_intensity "
                    "FROM slang_terms"
                ).fetchall()
                for legacy in legacy_terms:
                    self._sync_slang_scenario_candidate_locked(
                        connection,
                        legacy["group_id"],
                        {
                            "normalized_phrase": legacy["normalized_phrase"],
                            "phrase": legacy["phrase"],
                            "occurrence_count": legacy["occurrence_count"],
                            "speaker_count": legacy["speaker_count"],
                            "confidence": legacy["confidence"],
                            "safe_to_use": legacy["safe_to_use"],
                            "last_seen": legacy["last_seen"],
                            "slang_type": legacy["slang_type"],
                            "emotion": legacy["emotion"],
                            "emotion_intensity": legacy["emotion_intensity"],
                        },
                        int(legacy["last_seen"] or time.time()),
                    )
                print(
                    f"[LLM LEARNING MIGRATION] ensured slang_scenarios in {self.path}; "
                    f"backfilled={len(legacy_terms)}",
                    flush=True,
                )

            elif connection.execute("SELECT 1 FROM slang_scenarios LIMIT 1").fetchone() is None:
                legacy_terms = connection.execute(
                    "SELECT t.group_id, t.normalized_phrase, t.phrase, t.occurrence_count, "
                    "t.speaker_count, t.confidence, t.last_seen, t.safe_to_use, "
                    "t.slang_type, t.emotion, t.emotion_intensity "
                    "FROM slang_terms t LEFT JOIN slang_scenarios s "
                    "ON s.group_id=t.group_id AND s.normalized_phrase=t.normalized_phrase "
                    "WHERE s.normalized_phrase IS NULL"
                ).fetchall()
                for legacy in legacy_terms:
                    self._sync_slang_scenario_candidate_locked(
                        connection,
                        legacy["group_id"],
                        {
                            "normalized_phrase": legacy["normalized_phrase"],
                            "phrase": legacy["phrase"],
                            "occurrence_count": legacy["occurrence_count"],
                            "speaker_count": legacy["speaker_count"],
                            "confidence": legacy["confidence"],
                            "safe_to_use": legacy["safe_to_use"],
                            "last_seen": legacy["last_seen"],
                            "slang_type": legacy["slang_type"],
                            "emotion": legacy["emotion"],
                            "emotion_intensity": legacy["emotion_intensity"],
                        },
                        int(legacy["last_seen"] or time.time()),
                    )
                if legacy_terms:
                    print(
                        f"[LLM LEARNING MIGRATION] backfilled missing slang scenarios={len(legacy_terms)} "
                        f"in {self.path}",
                        flush=True,
                    )

    @staticmethod
    def _schema_is_ready(connection) -> bool:
        required_tables = {
            "group_profiles", "slang_terms", "term_speakers", "message_fingerprints",
            "learning_samples", "style_cards", "slang_scenarios", "maintenance_runs",
            "style_expressions",
        }
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        if not required_tables.issubset({str(row[0]) for row in rows}):
            return False
        required_columns = {
            "slang_terms": {
                "slang_type", "emotion", "emotion_intensity", "llm_confidence",
                "local_confidence", "algorithm_confidence", "overlap_ratio",
                "independent_occurrence_count", "shared_subphrase_count", "covered_by_json",
                "overlap_updated_at", "examples_json",
            },
            "slang_scenarios": {
                "slang_type", "emotion", "emotion_intensity", "llm_confidence",
                "algorithm_confidence", "overlap_ratio", "independent_occurrence_count",
                "shared_subphrase_count", "covered_by_json", "overlap_updated_at",
            },
        }
        for table, columns in required_columns.items():
            actual = {
                str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if not columns.issubset(actual):
                return False
        return True

    @_maintenance_serialized
    def reset_group(self, group_id: str):
        with self._managed_connection() as connection:
            connection.execute("DELETE FROM group_profiles WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM slang_terms WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM slang_scenarios WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM term_speakers WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM message_fingerprints WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM learning_samples WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM style_cards WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM style_expressions WHERE group_id = ?", (group_id,))

    @_maintenance_serialized
    def replace_profile(
        self,
        group_id: str,
        style: dict,
        message_count: int,
        terms: list[dict],
        samples: list[dict] | None = None,
    ):
        """Write one already-aggregated offline profile in a single transaction."""
        now = int(time.time())
        with self._managed_connection() as connection:
            connection.execute("DELETE FROM group_profiles WHERE group_id = ?", (group_id,))
            connection.execute("DELETE FROM message_fingerprints WHERE group_id = ?", (group_id,))
            connection.executemany(
                "INSERT OR REPLACE INTO learning_samples("
                "group_id, fingerprint, timestamp, speaker, speaker_name, content) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        group_id,
                        str(item.get("fingerprint") or f"offline-{index}"),
                        int(item.get("timestamp") or now),
                        str(item.get("speaker") or "unknown"),
                        str(item.get("speaker_name") or "")[:80],
                        str(item.get("content") or "")[:500],
                    )
                    for index, item in enumerate((samples or [])[-300:])
                ],
            )
            # Offline learner imports may update style/sample data only. Slang
            # rows must be created or changed through an LLM curation action.
            safe_terms = []
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

    def record_response_decision(
        self,
        group_id: str,
        *,
        message_count=0,
        should_reply=False,
        llm_ok=True,
        forced=False,
        attention_check=False,
        direct_address=False,
        follow_up=False,
        observer_window=False,
        repeat_topic_window=False,
    ):
        """Persist reply-timing outcomes without storing bot message text."""
        group_id = str(group_id or "unknown").strip() or "unknown"
        now = int(time.time())
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT style_json FROM group_profiles WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            style = _load_json(row["style_json"], {}) if row else {}
            learning = style.get("response_learning")
            if not isinstance(learning, dict):
                learning = {}

            def increment(name, amount=1):
                learning[name] = int(learning.get(name, 0) or 0) + int(amount)

            increment("decision_windows")
            increment("messages_seen", max(0, int(message_count or 0)))
            if forced:
                increment("forced_windows")
            if attention_check:
                increment("attention_windows")
            if direct_address:
                increment("direct_address_windows")
                increment("direct_interaction_windows")
            if follow_up:
                increment("follow_up_windows")
            if observer_window:
                increment("observer_windows")
            if repeat_topic_window:
                increment("repeat_topic_windows")
            if llm_ok:
                if should_reply:
                    increment("reply_windows")
                    if attention_check:
                        increment("attention_reply_windows")
                else:
                    increment("silent_windows")
            else:
                increment("failed_windows")
            style["response_learning"] = learning
            serialized = json.dumps(style, ensure_ascii=False, separators=(",", ":"))

            if row:
                connection.execute(
                    "UPDATE group_profiles SET updated_at=?, style_json=? WHERE group_id=?",
                    (now, serialized, group_id),
                )
            else:
                connection.execute(
                    "INSERT INTO group_profiles(group_id, updated_at, message_count, style_json, top_terms_json) "
                    "VALUES (?, ?, 0, ?, '[]')",
                    (group_id, now, serialized),
                )


    def get_slang_match_keys(self, group_id: str, limit: int = 60) -> list[dict]:
        """Return safe active slang rows (phrase + examples) for usage detection."""
        group_id = str(group_id or "unknown").strip() or "unknown"
        limit = max(1, min(int(limit or 60), 100))
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT t.phrase, t.normalized_phrase, COALESCE(s.emotion, '') AS emotion, "
                "COALESCE(s.examples_json, t.examples_json) AS examples_json "
                "FROM slang_terms t JOIN slang_scenarios s ON s.group_id=t.group_id "
                "AND s.normalized_phrase=t.normalized_phrase "
                "WHERE t.group_id=? AND t.safe_to_use=1 AND s.safe_to_use=1 AND s.status='active' "
                "ORDER BY t.confidence DESC, t.occurrence_count DESC LIMIT ?",
                (group_id, limit),
            ).fetchall()
        return [
            {
                "phrase": row["phrase"],
                "normalized_phrase": row["normalized_phrase"],
                "emotion": row["emotion"] or "",
                "examples": _load_json(row["examples_json"], []),
            }
            for row in rows
        ]

    def record_slang_usage(self, group_id, *, opportunity=False, messages=None, match_keys=None):
        """Record whether a slang-emotional opportunity was used (slang-only / embedded / missed).

        Pure behavioral accounting: local matching decides the mode from the bot's own
        reply text; the LLM never reports confidence or mode.
        """
        group_id = str(group_id or "unknown").strip() or "unknown"
        if not group_id:
            return
        now = int(time.time())
        messages = [str(item or "").strip() for item in (messages or []) if str(item or "").strip()]
        key_meta = []
        for key in (match_keys or []):
            if not isinstance(key, dict):
                continue
            phrase = str(key.get("phrase") or "").strip()
            emotion = str(key.get("emotion") or "").strip()
            for value in (key.get("examples") or [])[:3]:
                normalized = _normalize_match_text(value)
                if normalized:
                    key_meta.append((normalized, phrase, emotion))
            normalized = _normalize_match_text(phrase)
            if normalized:
                key_meta.append((normalized, phrase, emotion))
        records = []
        used_any = False
        for text in messages:
            normalized_text = _normalize_match_text(text)
            if not normalized_text:
                continue
            matched = [item for item in key_meta if item[0] and item[0] in normalized_text]
            if not matched:
                continue
            longest = max(matched, key=lambda item: len(item[0]))
            excess = len(normalized_text) - len(longest[0])
            if excess <= 1 or (len(normalized_text) <= 12 and excess <= 3):
                mode = "slang_only"
            else:
                mode = "embedded"
            records.append({
                "mode": mode,
                "phrase": longest[1],
                "emotion": longest[2],
                "ts": now,
            })
            used_any = True
        if opportunity and not used_any:
            records.append({"mode": "missed", "phrase": "", "emotion": "", "ts": now})
        if not records:
            return
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT style_json FROM group_profiles WHERE group_id = ?", (group_id,)
            ).fetchone()
            style = _load_json(row["style_json"], {}) if row else {}
            learning = style.get("slang_usage_learning")
            if not isinstance(learning, dict):
                learning = {}

            def increment(name, amount=1):
                learning[name] = int(learning.get(name, 0) or 0) + int(amount)

            if opportunity:
                increment("opportunities")
            pending = learning.get("pending")
            if not isinstance(pending, list):
                pending = []
            for rec in records:
                mode = rec["mode"]
                if mode == "slang_only":
                    increment("slang_only_used")
                elif mode == "embedded":
                    increment("embedded_used")
                elif mode == "missed":
                    increment("missed")
                if mode in {"slang_only", "embedded"}:
                    emotion = rec["emotion"]
                    if emotion:
                        bucket = learning.setdefault("by_emotion", {}).setdefault(emotion, {})
                        bucket["opportunities"] = int(bucket.get("opportunities", 0) or 0) + (1 if opportunity else 0)
                        bucket["used"] = int(bucket.get("used", 0) or 0) + 1
                    phrase = rec["phrase"]
                    if phrase:
                        bucket = learning.setdefault("by_phrase", {}).setdefault(phrase, {})
                        bucket["used"] = int(bucket.get("used", 0) or 0) + 1
                pending.append(rec)
            recent = learning.get("recent_ts")
            if not isinstance(recent, list):
                recent = []
            recent.append(now)
            learning["recent_ts"] = recent[-20:]
            learning["pending"] = pending[-50:]
            style["slang_usage_learning"] = learning
            serialized = json.dumps(style, ensure_ascii=False, separators=(",", ":"))
            if row:
                connection.execute(
                    "UPDATE group_profiles SET updated_at=?, style_json=? WHERE group_id=?",
                    (now, serialized, group_id),
                )
            else:
                connection.execute(
                    "INSERT INTO group_profiles(group_id, updated_at, message_count, style_json, top_terms_json) "
                    "VALUES (?, ?, 0, ?, '[]')",
                    (group_id, now, serialized),
                )

    def resolve_slang_usage_feedback(self, group_id, *, window_seconds=90, now=None):
        """Resolve pending slang-usage records with observed follow-up engagement."""
        group_id = str(group_id or "unknown").strip() or "unknown"
        now = int(now or time.time())
        try:
            window = max(15, min(int(window_seconds or 90), 600))
        except (TypeError, ValueError):
            window = 90
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT style_json FROM group_profiles WHERE group_id = ?", (group_id,)
            ).fetchone()
            if not row:
                return False
            style = _load_json(row["style_json"], {})
            learning = style.get("slang_usage_learning")
            if not isinstance(learning, dict):
                return False
            pending = learning.get("pending")
            if not isinstance(pending, list) or not pending:
                return False
            unresolved = []
            changed = False
            for rec in pending:
                try:
                    ts = int(rec.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0
                if not ts or now - ts < window:
                    unresolved.append(rec)
                    continue
                mode = str(rec.get("mode") or "")
                phrase = str(rec.get("phrase") or "")
                emotion = str(rec.get("emotion") or "")
                engaged = connection.execute(
                    "SELECT 1 FROM learning_samples WHERE group_id=? AND timestamp > ? AND timestamp <= ? LIMIT 1",
                    (group_id, ts, ts + window),
                ).fetchone() is not None
                changed = True

                def increment(name, amount=1):
                    learning[name] = int(learning.get(name, 0) or 0) + int(amount)

                if mode == "slang_only":
                    if engaged:
                        increment("slang_only_success")
                    if emotion:
                        bucket = learning.setdefault("by_emotion", {}).setdefault(emotion, {})
                        bucket["success"] = int(bucket.get("success", 0) or 0) + int(engaged)
                elif mode == "embedded":
                    if engaged:
                        increment("embedded_success")
                    if emotion:
                        bucket = learning.setdefault("by_emotion", {}).setdefault(emotion, {})
                        bucket["success"] = int(bucket.get("success", 0) or 0) + int(engaged)
                if mode in {"slang_only", "embedded"} and phrase:
                    bucket = learning.setdefault("by_phrase", {}).setdefault(phrase, {})
                    bucket["success"] = int(bucket.get("success", 0) or 0) + int(engaged)
            learning["pending"] = unresolved
            style["slang_usage_learning"] = learning
            if changed:
                connection.execute(
                    "UPDATE group_profiles SET updated_at=?, style_json=? WHERE group_id=?",
                    (now, json.dumps(style, ensure_ascii=False, separators=(",", ":")), group_id),
                )
            return changed

    def get_slang_usage_learning(self, group_id: str) -> dict:
        group_id = str(group_id or "unknown").strip() or "unknown"
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT style_json FROM group_profiles WHERE group_id = ?", (group_id,)
            ).fetchone()
        style = _load_json(row["style_json"], {}) if row else {}
        learning = style.get("slang_usage_learning")
        return dict(learning) if isinstance(learning, dict) else {}

    def get_slang_usage_guidance(self, group_id: str, max_chars: int = 420) -> str:
        learning = self.get_slang_usage_learning(group_id)
        if not learning:
            return ""
        opportunities = int(learning.get("opportunities", 0) or 0)
        used_only = int(learning.get("slang_only_used", 0) or 0)
        embedded = int(learning.get("embedded_used", 0) or 0)
        missed = int(learning.get("missed", 0) or 0)
        if opportunities < 3 and (used_only + embedded) < 2:
            return ""
        only_success = int(learning.get("slang_only_success", 0) or 0)
        embed_success = int(learning.get("embedded_success", 0) or 0)
        only_rate = only_success / max(used_only, 1)
        embed_rate = embed_success / max(embedded, 1)

        def acceptance(rate):
            if rate >= 0.5:
                return "高"
            if rate >= 0.25:
                return "中"
            return "低"

        lines = ["黑话使用学习（本地统计，仅供参考，不是硬规则）："]
        lines.append(
            f"情绪黑话机会{opportunities}次：纯黑话情绪回复{used_only}次（接受度{acceptance(only_rate)}），"
            f"嵌入普通回复{embedded}次（接受度{acceptance(embed_rate)}），错过{missed}次。"
        )
        if used_only >= 2:
            if only_rate >= 0.5:
                lines.append("群友对纯黑话情绪回复接受度高：机会命中时可用一条黑话短句，不加解释。")
            elif only_rate >= 0.25:
                lines.append("群友对纯黑话情绪回复接受度一般：仅在语境非常自然时用一条黑话短句。")
            else:
                lines.append("群友对纯黑话情绪回复接受度低：机会命中时也尽量少用纯黑话，优先嵌入。")
        if embedded >= 2:
            if embed_rate >= 0.5:
                lines.append("黑话嵌入普通回复接受度高：自然话题中可适度嵌入一条黑话。")
            elif embed_rate >= 0.25:
                lines.append("黑话嵌入接受度一般：嵌入前先确认黑话与语境贴合。")
            else:
                lines.append("黑话嵌入接受度低：减少黑话插入，只在极自然时使用。")
        by_emotion = learning.get("by_emotion") or {}
        if isinstance(by_emotion, dict):
            ranked = sorted(
                (
                    (str(k), int(v.get("used", 0) or 0), int(v.get("success", 0) or 0))
                    for k, v in by_emotion.items()
                    if isinstance(v, dict)
                ),
                key=lambda item: (-item[2], -item[1]),
            )
            if ranked:
                top = ranked[0]
                if top[1] >= 2 and top[2] >= 1:
                    lines.append(f"表达「{top[0]}」类的黑话反馈最好，可优先考虑该类型。")
        recent = learning.get("recent_ts") or []
        if isinstance(recent, list) and len(recent) >= 3:
            recent = sorted(int(item or 0) for item in recent if item)
            span = max(1, recent[-1] - recent[0])
            density = len(recent) * 3600.0 / max(span, 1)
            if density >= 3.0:
                lines.append("近期黑话使用偏密集，适当降低插入频率，避免刷屏感。")
        text = "\n".join(lines)
        return text[:max_chars]

    @_maintenance_serialized
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

            connection.execute(
                "INSERT OR REPLACE INTO learning_samples("
                "group_id, fingerprint, timestamp, speaker, speaker_name, content) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    group_id,
                    fingerprint,
                    now,
                    speaker,
                    str(observation.get("speaker_name") or "")[:80],
                    content[:500],
                ),
            )
            connection.execute(
                "DELETE FROM learning_samples WHERE group_id = ? AND rowid NOT IN ("
                "SELECT rowid FROM learning_samples WHERE group_id = ? "
                "ORDER BY timestamp DESC LIMIT 300)",
                (group_id, group_id),
            )

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

            # The local learner records samples and style only. It must never
            # turn extracted n-grams into slang database rows.
            # Slang persistence is exclusively handled by daily LLM curation.
            self._refresh_top_terms(connection, group_id)
            return True
            terms = []
            for term in terms:
                normalized = str(term.get("normalized_phrase") or "").strip()
                phrase = str(term.get("phrase") or normalized).strip()
                if not normalized or not phrase:
                    continue
                if _is_generic_slang_phrase(normalized):
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
                    "UPDATE slang_terms SET speaker_count = ?, confidence = ?, local_confidence = ?, safe_to_use = ? "
                    "WHERE group_id = ? AND normalized_phrase = ?",
                    (speaker_count, confidence, confidence, int(safe), group_id, normalized),
                )
                self._sync_slang_scenario_candidate_locked(
                    connection,
                    group_id,
                    {
                        "normalized_phrase": normalized,
                        "phrase": phrase,
                        "occurrence_count": occurrence,
                        "speaker_count": speaker_count,
                        "confidence": confidence,
                        "safe_to_use": safe,
                        "last_seen": now,
                        "examples": examples,
                    },
                    now,
                )

            self._refresh_top_terms(connection, group_id)
        return True

    @_maintenance_serialized
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

    def _sync_slang_scenario_candidate_locked(self, connection, group_id, item, now):
        normalized = str(item.get("normalized_phrase") or "").strip().casefold()[:80]
        phrase = str(item.get("phrase") or normalized).strip()[:80]
        if not normalized or not phrase:
            return
        existing = connection.execute(
            "SELECT meaning, scenes_json, avoid_scenes_json, examples_json, status, confidence, "
            "slang_type, emotion, emotion_intensity "
            "FROM slang_scenarios WHERE group_id = ? AND normalized_phrase = ?",
            (group_id, normalized),
        ).fetchone()
        status = str(existing["status"] if existing else "candidate")
        minimum = max(2, int(item.get("min_term_count") or 2))
        occurrence = max(0, int(item.get("occurrence_count", 0) or 0))
        speakers = max(0, int(item.get("speaker_count", 0) or 0))
        local_confidence, local_safe = _term_confidence(occurrence, speakers, minimum)
        confidence = float(existing["confidence"] if existing else local_confidence)
        slang_type = _clean_slang_type(
            item.get("slang_type") or item.get("type")
            or (existing["slang_type"] if existing else "通用")
        )
        emotion = _clean_emotion(item.get("emotion") or (existing["emotion"] if existing else ""))
        emotion_intensity = _normalize_emotion_intensity(
            item.get("emotion_intensity")
            if item.get("emotion_intensity") not in (None, "")
            else (existing["emotion_intensity"] if existing else 0)
        )
        connection.execute(
            "INSERT INTO slang_scenarios("
            "group_id, normalized_phrase, phrase, meaning, scenes_json, avoid_scenes_json, "
            "examples_json, confidence, speaker_count, occurrence_count, last_seen, safe_to_use, status, "
            "slang_type, emotion, emotion_intensity, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(group_id, normalized_phrase) DO UPDATE SET phrase=excluded.phrase, "
            "speaker_count=excluded.speaker_count, occurrence_count=excluded.occurrence_count, "
            "last_seen=excluded.last_seen, slang_type=excluded.slang_type, emotion=excluded.emotion, "
            "emotion_intensity=excluded.emotion_intensity, updated_at=excluded.updated_at",
            (
                group_id, normalized, phrase,
                str(existing["meaning"] if existing else ""),
                str(existing["scenes_json"] if existing else "[]"),
                str(existing["avoid_scenes_json"] if existing else "[]"),
                str(existing["examples_json"] if existing else "[]"),
                confidence,
                int(item.get("speaker_count", 0) or 0),
                int(item.get("occurrence_count", 0) or 0),
                int(item.get("last_seen", now) or now),
                int(local_safe if status == "active" else 0),
                status,
                slang_type,
                emotion,
                emotion_intensity,
                now,
            ),
        )

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
            scenario_rows = connection.execute(
                "SELECT normalized_phrase, meaning, scenes_json, avoid_scenes_json, status, "
                "slang_type, emotion, emotion_intensity "
                "FROM slang_scenarios WHERE group_id = ? AND safe_to_use = 1 AND status = 'active'",
                (group_id,),
            ).fetchall()
            scenario_map = {row["normalized_phrase"]: row for row in scenario_rows}
            for item in result["top_terms"]:
                row = scenario_map.get(str(item["phrase"]).casefold())
                if row:
                    item["meaning"] = row["meaning"]
                    item["scenes"] = _load_json(row["scenes_json"], [])
                    item["avoid_scenes"] = _load_json(row["avoid_scenes_json"], [])
                    item["slang_type"] = row["slang_type"]
                    item["emotion"] = row["emotion"]
                    item["emotion_intensity"] = row["emotion_intensity"]
            return result

    def get_slang_review_candidates(self, max_items=40, min_occurrences=2):
        """Return only low-cost, group-scoped candidates for one batch review."""
        limit = max(1, min(int(max_items or 40), 200))
        minimum = max(2, int(min_occurrences or 2))
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT s.group_id, s.normalized_phrase, s.phrase, s.occurrence_count, "
                "s.speaker_count, s.confidence, s.last_seen, s.status, s.meaning, "
                "s.scenes_json, s.avoid_scenes_json, s.examples_json, s.slang_type, "
                "s.emotion, s.emotion_intensity "
                "FROM slang_scenarios s WHERE s.occurrence_count >= ? "
                "AND s.status IN ('candidate', 'uncertain', 'active') "
                "ORDER BY s.group_id, s.occurrence_count DESC, s.confidence DESC LIMIT ?",
                (minimum, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["scenes"] = _load_json(item.pop("scenes_json"), [])
            item["avoid_scenes"] = _load_json(item.pop("avoid_scenes_json"), [])
            item["examples"] = _load_json(item.pop("examples_json"), [])[:3]
            result.append(item)
        return result

    @_maintenance_serialized
    def apply_slang_scenario(self, item: dict) -> bool:
        """Apply one bounded, model-reviewed scenario without trusting its counters."""
        if not isinstance(item, dict):
            return False
        group_id = str(item.get("group_id") or "").strip()[:200]
        normalized = str(item.get("normalized_phrase") or "").strip().casefold()[:80]
        phrase = str(item.get("phrase") or normalized).strip()[:80]
        if not group_id or not normalized or not phrase:
            return False
        meaning = str(item.get("meaning") or "").strip()[:180]
        scenes = _clean_scene_list(item.get("scenes"), 6)
        avoid_scenes = _clean_scene_list(item.get("avoid_scenes"), 6, fallback_uncertain=False)
        examples = _clean_text_list(item.get("examples"), 3, 160)
        slang_type = _clean_slang_type(item.get("slang_type") or item.get("type"))
        emotion = _clean_emotion(item.get("emotion"))
        emotion_intensity = _normalize_emotion_intensity(item.get("emotion_intensity"))
        if item.get("status"):
            requested_status = item.get("status")
        elif item.get("is_slang") is True:
            requested_status = "active"
        elif item.get("is_slang") is False:
            requested_status = "inactive"
        else:
            requested_status = "uncertain"
        status = str(requested_status).strip().lower()
        if status not in {"active", "uncertain", "inactive", "candidate"}:
            status = "uncertain"
        if _is_generic_slang_phrase(normalized):
            # A model cannot promote an unambiguous ordinary word back into
            # the directly injected slang set.
            status = "inactive"
        if any(marker in (meaning + " " + " ".join(scenes)).casefold() for marker in _SLANG_UNSAFE_MARKERS):
            status = "inactive"
        if not meaning or not scenes or scenes == ["uncertain"]:
            status = "uncertain" if status == "active" else status
        # The model only decides whether a term enters the library (status,
        # meaning, scenes, examples); it has no confidence vote. The raw model
        # value is kept as informational llm_confidence for observability only;
        # every gate below uses the local frequency-based confidence.
        try:
            llm_confidence = max(0.0, min(1.0, float(item.get("confidence", 0))))
        except (TypeError, ValueError):
            llm_confidence = 0.0
        now = int(time.time())
        with self._managed_connection() as connection:
            current_scenario = connection.execute(
                "SELECT meaning, scenes_json, avoid_scenes_json, examples_json, confidence, status, "
                "slang_type, emotion, emotion_intensity "
                "FROM slang_scenarios WHERE group_id = ? AND normalized_phrase = ?",
                (group_id, normalized),
            ).fetchone()
            source = connection.execute(
                "SELECT phrase, occurrence_count, speaker_count, first_seen, confidence, last_seen, examples_json "
                "FROM slang_terms WHERE group_id = ? AND normalized_phrase = ?",
                (group_id, normalized),
            ).fetchone()
            try:
                occurrence_delta = max(1, min(100, int(item.get("occurrence_delta") or 1)))
            except (TypeError, ValueError):
                occurrence_delta = 1
            speakers = list(dict.fromkeys(
                str(value).strip()[:120]
                for value in (item.get("speakers") or [])
                if str(value).strip()
            ))[:20]
            incoming_examples = _clean_text_list(item.get("examples"), 3, 160)
            old_examples = _load_json(source["examples_json"], []) if source else []
            examples = _clean_text_list(old_examples + incoming_examples, 3, 160)
            if source:
                occurrence = int(source["occurrence_count"] or 0) + occurrence_delta
                first_seen = int(source["first_seen"] or now)
                last_seen = max(int(source["last_seen"] or now), now)
            else:
                occurrence = occurrence_delta
                first_seen = now
                last_seen = now
            for speaker in speakers:
                connection.execute(
                    "INSERT OR IGNORE INTO term_speakers(group_id, normalized_phrase, speaker) VALUES (?, ?, ?)",
                    (group_id, normalized, speaker),
                )
            speaker_count = connection.execute(
                "SELECT COUNT(*) FROM term_speakers WHERE group_id=? AND normalized_phrase=?",
                (group_id, normalized),
            ).fetchone()[0]
            minimum = max(2, int(item.get("min_term_count") or 2))
            local_confidence, local_safe = _term_confidence(
                occurrence, int(speaker_count), minimum
            )
            if source:
                connection.execute(
                    "UPDATE slang_terms SET phrase=?, occurrence_count=?, speaker_count=?, first_seen=?, "
                    "last_seen=?, confidence=?, local_confidence=?, safe_to_use=?, examples_json=? "
                    "WHERE group_id=? AND normalized_phrase=?",
                    (
                        phrase or str(source["phrase"] or normalized), occurrence, int(speaker_count),
                        first_seen, last_seen, local_confidence, local_confidence,
                        int(local_safe), json.dumps(examples, ensure_ascii=False, separators=(",", ":")),
                        group_id, normalized,
                    ),
                )
            else:
                connection.execute(
                    "INSERT INTO slang_terms(group_id, normalized_phrase, phrase, occurrence_count, speaker_count, "
                    "first_seen, last_seen, confidence, safe_to_use, examples_json, slang_type, emotion, "
                    "emotion_intensity, llm_confidence, local_confidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        group_id, normalized, phrase, occurrence, int(speaker_count), first_seen, last_seen,
                        local_confidence, int(local_safe), json.dumps(examples, ensure_ascii=False, separators=(",", ":")),
                        slang_type, emotion, emotion_intensity, llm_confidence, local_confidence,
                    ),
                )
            source = connection.execute(
                "SELECT phrase, occurrence_count, speaker_count, first_seen, confidence, last_seen, examples_json "
                "FROM slang_terms WHERE group_id = ? AND normalized_phrase = ?",
                (group_id, normalized),
            ).fetchone()
            old_scenes = _load_json(current_scenario["scenes_json"], []) if current_scenario else []
            old_avoid_scenes = _load_json(current_scenario["avoid_scenes_json"], []) if current_scenario else []
            old_examples = _load_json(current_scenario["examples_json"], []) if current_scenario else []
            if current_scenario:
                slang_type = _clean_slang_type(slang_type or current_scenario["slang_type"])
                emotion = _clean_emotion(emotion or current_scenario["emotion"])
                if item.get("emotion_intensity") in (None, ""):
                    emotion_intensity = _normalize_emotion_intensity(current_scenario["emotion_intensity"])
            if current_scenario and status == "active":
                previous_active = str(current_scenario["status"]) == "active"
                scenes = _clean_scene_list((old_scenes if previous_active else []) + scenes, 6)
                avoid_scenes = _clean_scene_list(
                    (old_avoid_scenes if previous_active else []) + avoid_scenes,
                    6,
                    fallback_uncertain=False,
                )
                examples = _clean_text_list((old_examples if previous_active else []) + examples, 3, 160)
                meaning = meaning or str(current_scenario["meaning"] or "")
                # Scenario confidence is always recomputed from local counters below.
            elif current_scenario and str(current_scenario["status"]) == "active" and status != "active":
                # Uncertain follow-up evidence must not permanently add a new
                # scene to an already active phrase.
                meaning = str(current_scenario["meaning"] or meaning)
                scenes = _clean_scene_list(old_scenes, 6)
                avoid_scenes = _clean_scene_list(old_avoid_scenes, 6, fallback_uncertain=False)
                examples = _clean_text_list(old_examples, 3, 160)
                status = "active"
                # Scenario confidence is always recomputed from local counters below.
            safe = int(status == "active" and bool(meaning) and scenes != ["uncertain"] and local_safe)
            connection.execute(
                "INSERT INTO slang_scenarios("
                "group_id, normalized_phrase, phrase, meaning, scenes_json, avoid_scenes_json, "
                "examples_json, confidence, speaker_count, occurrence_count, last_seen, "
                "safe_to_use, status, slang_type, emotion, emotion_intensity, llm_confidence, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(group_id, normalized_phrase) DO UPDATE SET phrase=excluded.phrase, "
                "meaning=excluded.meaning, scenes_json=excluded.scenes_json, "
                "avoid_scenes_json=excluded.avoid_scenes_json, examples_json=excluded.examples_json, "
                "confidence=excluded.confidence, speaker_count=excluded.speaker_count, "
                "occurrence_count=excluded.occurrence_count, last_seen=excluded.last_seen, "
                "safe_to_use=excluded.safe_to_use, status=excluded.status, slang_type=excluded.slang_type, "
                "emotion=excluded.emotion, emotion_intensity=excluded.emotion_intensity, "
                "llm_confidence=excluded.llm_confidence, "
                "updated_at=excluded.updated_at",
                (
                    group_id, normalized, str(source["phrase"] or phrase), meaning,
                    json.dumps(scenes, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(avoid_scenes, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(examples, ensure_ascii=False, separators=(",", ":")),
                    local_confidence, int(source["speaker_count"]), int(source["occurrence_count"]),
                    int(source["last_seen"] or now), safe, status,
                    slang_type, emotion, emotion_intensity, llm_confidence, now,
                ),
            )
            connection.execute(
                "UPDATE slang_terms SET slang_type=?, emotion=?, emotion_intensity=?, llm_confidence=? "
                "WHERE group_id=? AND normalized_phrase=?",
                (slang_type, emotion, emotion_intensity, llm_confidence, group_id, normalized),
            )
        return True

    def get_slang_scene_candidates(self, group_id, topic="", intent="", max_items=8, max_chars=900):
        """Retrieve a small safe candidate set; the full slang table never enters a prompt."""
        group_id = str(group_id or "unknown").strip() or "unknown"
        limit = max(1, min(int(max_items or 8), 24))
        budget = max(240, min(int(max_chars or 900), 3000))
        query_tokens = _topic_tokens(f"{topic} {intent}")
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT phrase, normalized_phrase, meaning, scenes_json, avoid_scenes_json, "
                "examples_json, confidence, speaker_count, occurrence_count, last_seen, "
                "slang_type, emotion, emotion_intensity "
                "FROM slang_scenarios WHERE group_id = ? AND safe_to_use = 1 AND status = 'active' "
                "ORDER BY confidence DESC, occurrence_count DESC, last_seen DESC LIMIT 80",
                (group_id,),
            ).fetchall()
        ranked = []
        for row in rows:
            scenes = _load_json(row["scenes_json"], [])
            meaning = str(row["meaning"] or "")
            searchable = _topic_tokens(" ".join([row["phrase"], meaning] + scenes))
            overlap = len(query_tokens & searchable)
            if query_tokens and overlap == 0:
                continue
            score = overlap * 4 + float(row["confidence"]) * 2 + min(2.0, int(row["occurrence_count"]) / 10)
            ranked.append((score, row, scenes))
        ranked.sort(key=lambda entry: entry[0], reverse=True)
        result = []
        used = 0
        for _, row, scenes in ranked:
            item = {
                "phrase": row["phrase"],
                "meaning": str(row["meaning"] or "")[:120],
                "scenes": scenes[:4],
                "avoid_scenes": _load_json(row["avoid_scenes_json"], [])[:3],
                "examples": _load_json(row["examples_json"], [])[:1],
                "confidence": round(float(row["confidence"]), 3),
                "slang_type": row["slang_type"],
                "emotion": row["emotion"],
                "emotion_intensity": row["emotion_intensity"],
            }
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if used + len(encoded) + 1 > budget:
                continue
            result.append(item)
            used += len(encoded) + 1
            if len(result) >= limit:
                break
        return result

    def get_prompt_slang(self, group_id: str) -> list[dict]:
        """Return every currently qualified expression, independent of topic scenes."""
        group_id = str(group_id or "unknown").strip() or "unknown"
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT t.phrase, t.normalized_phrase, COALESCE(s.meaning, '') AS meaning, "
                "COALESCE(s.examples_json, t.examples_json) AS examples_json, "
                "s.slang_type, s.emotion, s.emotion_intensity, "
                "t.confidence, t.occurrence_count, t.speaker_count FROM slang_terms t "
                "JOIN slang_scenarios s ON s.group_id=t.group_id AND "
                "s.normalized_phrase=t.normalized_phrase WHERE t.group_id=? AND t.safe_to_use=1 "
                "AND s.safe_to_use=1 AND s.status='active' "
                "ORDER BY t.normalized_phrase ASC",
                (group_id,),
            ).fetchall()
        return [
            {
                "phrase": row["phrase"],
                "normalized_phrase": row["normalized_phrase"],
                "meaning": row["meaning"],
                "examples": _load_json(row["examples_json"], [])[:2],
                "confidence": round(float(row["confidence"] or 0), 3),
                "occurrence_count": int(row["occurrence_count"] or 0),
                "speaker_count": int(row["speaker_count"] or 0),
                "slang_type": row["slang_type"],
                "emotion": row["emotion"],
                "emotion_intensity": row["emotion_intensity"],
            }
            for row in rows
            if not _is_generic_slang_phrase(row["normalized_phrase"])
        ]

    def get_slang_taxonomy(self, group_id: str | None = None) -> dict:
        """Return deduplicated type/emotion/intensity values for prompt guidance."""
        params = []
        where = ""
        if group_id:
            where = " WHERE group_id=?"
            params.append(str(group_id))
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT slang_type, emotion, emotion_intensity FROM slang_terms" + where
                + " UNION ALL SELECT slang_type, emotion, emotion_intensity FROM slang_scenarios" + where,
                params + params,
            ).fetchall()
        return {
            "types": sorted({_clean_slang_type(row["slang_type"]) for row in rows}),
            "emotions": sorted({
                value for value in (_clean_emotion(row["emotion"]) for row in rows) if value
            }),
            "emotion_intensities": sorted({
                _normalize_emotion_intensity(row["emotion_intensity"])
                for row in rows
                if row["emotion_intensity"] not in (None, "")
            }),
        }

    def get_context_slang(self, group_id: str, messages, max_items=8, max_chars=1200) -> list[dict]:
        """Return only qualified slang that is literally present in current user context."""
        group_id = str(group_id or "unknown").strip() or "unknown"
        limit = max(1, min(int(max_items or 8), 16))
        budget = max(300, min(int(max_chars or 1200), 3000))
        context_texts = []
        for index, item in enumerate(messages or []):
            if not isinstance(item, dict) or item.get("is_bot") or item.get("role") == "assistant":
                continue
            text = _normalize_match_text(item.get("content"))
            if text:
                # Keep the original transcript index. The assistant messages
                # are excluded from matching, but their positions still
                # matter when the prompt explains where a candidate matched.
                context_texts.append((index, text))
        if not context_texts:
            return []

        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT t.phrase, t.normalized_phrase, COALESCE(s.meaning, '') AS meaning, "
                "s.scenes_json, s.avoid_scenes_json, COALESCE(s.examples_json, t.examples_json) AS examples_json, "
                "s.slang_type, s.emotion, s.emotion_intensity, "
                "t.confidence, t.occurrence_count, t.speaker_count, s.last_seen "
                "FROM slang_terms t JOIN slang_scenarios s ON s.group_id=t.group_id "
                "AND s.normalized_phrase=t.normalized_phrase "
                "WHERE t.group_id=? AND t.safe_to_use=1 AND s.safe_to_use=1 AND s.status='active' "
                "ORDER BY t.confidence DESC, t.occurrence_count DESC, s.last_seen DESC",
                (group_id,),
            ).fetchall()

        ranked = []
        for row in rows:
            if _is_generic_slang_phrase(row["normalized_phrase"]):
                continue
            phrase_key = _normalize_match_text(row["phrase"])
            match_keys = [
                key for key in (
                    phrase_key,
                    *(_normalize_match_text(example) for example in _load_json(row["examples_json"], [])[:3]),
                )
                if key
            ]
            if not match_keys:
                continue
            matched_indexes = [
                index for index, text in context_texts
                if any(key in text for key in match_keys)
            ]
            if not matched_indexes:
                continue
            score = (
                len(phrase_key) * 5
                + len(matched_indexes) * 3
                + float(row["confidence"] or 0) * 4
                + min(3.0, int(row["occurrence_count"] or 0) / 10)
            )
            ranked.append((score, row, matched_indexes))
        ranked.sort(key=lambda item: (-item[0], -len(_normalize_match_text(item[1]["phrase"])), item[1]["normalized_phrase"]))

        result = []
        used = 0
        for _, row, matched_indexes in ranked:
            item = {
                "phrase": row["phrase"],
                "normalized_phrase": row["normalized_phrase"],
                "meaning": str(row["meaning"] or "")[:160],
                "scenes": _load_json(row["scenes_json"], [])[:4],
                "avoid_scenes": _load_json(row["avoid_scenes_json"], [])[:3],
                "examples": _load_json(row["examples_json"], [])[:2],
                "confidence": round(float(row["confidence"] or 0), 3),
                "matched_context_indexes": matched_indexes[:4],
                "slang_type": row["slang_type"],
                "emotion": row["emotion"],
                "emotion_intensity": row["emotion_intensity"],
            }
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if used + len(encoded) + 1 > budget:
                continue
            result.append(item)
            used += len(encoded) + 1
            if len(result) >= limit:
                break
        return result

    def get_slang_emotional_candidates(self, group_id, messages=None, max_items=3, max_chars=600, rotation=True):
        """Return a small safe slang pool for the proactive emotional window.

        Used when an attention pull triggers the escalating n*10% slang
        emotional opportunity: instead of letting the model fish blindly, we
        pre-select a few safe active terms ordered by local confidence.
        """
        group_id = str(group_id or "unknown").strip() or "unknown"
        limit = max(1, min(int(max_items or 3), 6))
        budget = max(300, min(int(max_chars or 600), 1500))
        del messages
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT t.phrase, t.normalized_phrase, COALESCE(s.meaning, '') AS meaning, "
                "COALESCE(s.examples_json, t.examples_json) AS examples_json, "
                "s.slang_type, s.emotion, s.emotion_intensity, "
                "t.confidence, t.occurrence_count, t.speaker_count, s.last_seen "
                "FROM slang_terms t JOIN slang_scenarios s ON s.group_id=t.group_id "
                "AND s.normalized_phrase=t.normalized_phrase "
                "WHERE t.group_id=? AND t.safe_to_use=1 AND s.safe_to_use=1 AND s.status='active' "
                "ORDER BY t.confidence DESC, t.occurrence_count DESC, s.last_seen DESC",
                (group_id,),
            ).fetchall()
        ranked = []
        for row in rows:
            if _is_generic_slang_phrase(row["normalized_phrase"]):
                continue
            if not str(row["phrase"] or "").strip():
                continue
            try:
                intensity = float(row["emotion_intensity"] or 0)
            except (TypeError, ValueError):
                intensity = 0.0
            score = (
                float(row["confidence"] or 0) * 4
                + min(3.0, int(row["occurrence_count"] or 0) / 10)
                + min(1.0, intensity)
            )
            ranked.append((score, row))
        ranked.sort(key=lambda entry: (-entry[0], -float(entry[1]["confidence"] or 0)))
        pick_pool = self._rotate_emotional_pool(ranked, limit) if rotation else ranked
        result = []
        used = 0
        for _, row in pick_pool:
            item = {
                "phrase": row["phrase"],
                "normalized_phrase": row["normalized_phrase"],
                "meaning": str(row["meaning"] or "")[:140],
                "examples": _load_json(row["examples_json"], [])[:2],
                "confidence": round(float(row["confidence"] or 0), 3),
                "occurrence_count": int(row["occurrence_count"] or 0),
                "emotion": row["emotion"],
                "emotion_intensity": row["emotion_intensity"],
            }
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if used + len(encoded) + 1 > budget:
                continue
            result.append(item)
            used += len(encoded) + 1
            if len(result) >= limit:
                break
        return result

    def lookup_slang(self, group_id: str, query="", max_items=20) -> list[dict]:
        """Let the model inspect non-injected slang from the current group only."""
        group_id = str(group_id or "unknown").strip() or "unknown"
        query = str(query or "").strip().casefold()
        # The interactive tool is capped at 50 by LLMService; maintenance
        # calls may inspect a larger bounded set before splitting the prompt.
        limit = max(1, min(int(max_items or 20), 200))
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT normalized_phrase, phrase, meaning, scenes_json, avoid_scenes_json, "
                "examples_json, confidence, occurrence_count, speaker_count, status, safe_to_use, "
                "slang_type, emotion, emotion_intensity "
                "FROM slang_scenarios WHERE group_id=? ORDER BY occurrence_count DESC, confidence DESC LIMIT 200",
                (group_id,),
            ).fetchall()
        result = []
        for row in rows:
            searchable = " ".join([
                str(row["normalized_phrase"] or ""), str(row["phrase"] or ""),
                str(row["meaning"] or ""), str(row["scenes_json"] or ""),
            ]).casefold()
            if query and query not in searchable:
                continue
            item = dict(row)
            item["scenes"] = _load_json(item.pop("scenes_json"), [])
            item["avoid_scenes"] = _load_json(item.pop("avoid_scenes_json"), [])
            item["examples"] = _load_json(item.pop("examples_json"), [])[:3]
            item["emotion_intensity"] = _normalize_emotion_intensity(item.get("emotion_intensity"))
            result.append(item)
            if len(result) >= limit:
                break
        return result

    def find_similar_slang(self, group_id: str, phrase: str, max_items=8) -> list[dict]:
        """Find exact or textually similar slang before an LLM write.

        This is deliberately lexical and bounded. It is a collision check, not
        a replacement for the LLM's semantic decision and does not mutate data.
        """
        group_id = str(group_id or "unknown").strip() or "unknown"
        normalized = _normalize_match_text(phrase)
        if len(normalized) < 2:
            return []
        limit = max(1, min(int(max_items or 8), 20))
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT s.normalized_phrase, s.phrase, s.meaning, s.scenes_json, "
                "s.examples_json, s.confidence, s.status, s.safe_to_use, "
                "s.slang_type, s.emotion, s.emotion_intensity "
                "FROM slang_scenarios s WHERE s.group_id=? "
                "ORDER BY s.confidence DESC, s.occurrence_count DESC LIMIT 500",
                (group_id,),
            ).fetchall()
        ranked = []
        for row in rows:
            other = _normalize_match_text(row["normalized_phrase"] or row["phrase"])
            if not other:
                continue
            if other == normalized:
                score, match_type = 1.0, "exact"
            elif normalized in other or other in normalized:
                shared = min(len(normalized), len(other))
                score = 0.55 + 0.4 * shared / max(len(normalized), len(other))
                match_type = "contained"
            else:
                shared = _longest_shared_span(normalized, other)
                if shared < 2:
                    continue
                score = shared / max(len(normalized), len(other))
                if score < 0.45:
                    continue
                match_type = "shared_text"
            item = dict(row)
            item["normalized_phrase"] = other
            item["scenes"] = _load_json(item.pop("scenes_json"), [])[:4]
            item["examples"] = _load_json(item.pop("examples_json"), [])[:3]
            item["similarity"] = round(score, 3)
            item["match_type"] = match_type
            ranked.append(item)
        ranked.sort(key=lambda item: (-item["similarity"], item["normalized_phrase"]))
        return ranked[:limit]

    def resolve_slang_write(self, item: dict) -> dict | None:
        """Require an explicit LLM decision after the similarity lookup."""
        if not isinstance(item, dict):
            return None
        normalized = _normalize_match_text(item.get("normalized_phrase"))
        if not normalized:
            return None
        decision = str(item.get("similarity_decision") or "").strip().lower()
        similar = self.find_similar_slang(item.get("group_id"), normalized, max_items=8)
        if not similar:
            if decision != "new_distinct":
                return None
            return dict(item, normalized_phrase=normalized)
        exact = next(
            (row for row in similar if row.get("match_type") == "exact"),
            None,
        )
        if exact:
            canonical = _normalize_match_text(item.get("canonical_normalized_phrase"))
            if decision != "reuse_existing" or canonical != exact.get("normalized_phrase"):
                return None
            return dict(item, normalized_phrase=canonical, canonical_normalized_phrase=canonical)
        if decision == "reuse_existing":
            canonical = _normalize_match_text(item.get("canonical_normalized_phrase"))
            allowed = {str(row.get("normalized_phrase") or "") for row in similar}
            if canonical not in allowed:
                return None
            return dict(item, normalized_phrase=canonical, canonical_normalized_phrase=canonical)
        if decision == "new_distinct":
            return dict(item, normalized_phrase=normalized)
        return None

    @_maintenance_serialized
    def remove_slang(self, group_id: str, normalized_phrase: str) -> bool:
        group_id = str(group_id or "").strip()
        normalized_phrase = str(normalized_phrase or "").strip().casefold()
        if not group_id or not normalized_phrase:
            return False
        with self._managed_connection() as connection:
            connection.execute(
                "DELETE FROM slang_scenarios WHERE group_id=? AND normalized_phrase=?",
                (group_id, normalized_phrase),
            )
            connection.execute(
                "DELETE FROM slang_terms WHERE group_id=? AND normalized_phrase=?",
                (group_id, normalized_phrase),
            )
            connection.execute(
                "DELETE FROM term_speakers WHERE group_id=? AND normalized_phrase=?",
                (group_id, normalized_phrase),
            )
            self._refresh_top_terms(connection, group_id)
        return True

    @_maintenance_serialized
    def purge_generic_slang(self, group_id: str | None = None) -> int:
        """Remove only the explicitly known general-language terms."""
        removed = 0
        with self._managed_connection() as connection:
            if group_id:
                rows = connection.execute(
                    "SELECT group_id, normalized_phrase FROM slang_terms WHERE group_id=?",
                    (str(group_id),),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT group_id, normalized_phrase FROM slang_terms"
                ).fetchall()
            for row in rows:
                if not _is_generic_slang_phrase(row["normalized_phrase"]):
                    continue
                group_id = row["group_id"]
                normalized = row["normalized_phrase"]
                connection.execute(
                    "DELETE FROM slang_scenarios WHERE group_id=? AND normalized_phrase=?",
                    (group_id, normalized),
                )
                connection.execute(
                    "DELETE FROM slang_terms WHERE group_id=? AND normalized_phrase=?",
                    (group_id, normalized),
                )
                connection.execute(
                    "DELETE FROM term_speakers WHERE group_id=? AND normalized_phrase=?",
                    (group_id, normalized),
                )
                removed += 1
            for group_id in {row["group_id"] for row in rows}:
                self._refresh_top_terms(connection, group_id)
        return removed

    def get_style_card(self, group_id: str) -> dict:
        with self._managed_connection() as connection:
            return self._get_style_card_connection(connection, group_id)

    @staticmethod
    def _get_style_card_connection(connection, group_id: str) -> dict:
        row = connection.execute(
            "SELECT version, updated_at, source_message_count, card_json "
            "FROM style_cards WHERE group_id = ?",
            (str(group_id or "unknown"),),
        ).fetchone()
        if not row:
            return {}
        card = _load_json(row["card_json"], {})
        card["_version"] = int(row["version"])
        card["_updated_at"] = int(row["updated_at"])
        card["_source_message_count"] = int(row["source_message_count"])
        return card

    def get_review_payload(self, group_id: str, max_samples: int = 80, max_terms: int = 80) -> dict:
        group_id = str(group_id or "unknown").strip() or "unknown"
        with self._managed_connection() as connection:
            profile = connection.execute(
                "SELECT message_count, style_json FROM group_profiles WHERE group_id = ?",
                (group_id,),
            ).fetchone()
            if not profile:
                return {}
            terms = connection.execute(
                "SELECT phrase, occurrence_count, speaker_count, confidence, safe_to_use, examples_json "
                "FROM slang_terms WHERE group_id = ? ORDER BY occurrence_count DESC LIMIT ?",
                (group_id, max(1, int(max_terms))),
            ).fetchall()
            samples = connection.execute(
                "SELECT timestamp, speaker_name, content FROM learning_samples "
                "WHERE group_id = ? ORDER BY timestamp DESC LIMIT ?",
                (group_id, max(1, int(max_samples))),
            ).fetchall()
            expressions = connection.execute(
                "SELECT situation, pattern, count, keywords_json, examples_json "
                "FROM style_expressions WHERE group_id = ? AND status = 'active' "
                "ORDER BY count DESC, updated_at DESC LIMIT 40",
                (group_id,),
            ).fetchall()
            return {
                "group_id": group_id,
                "message_count": int(profile["message_count"]),
                "style_stats": _load_json(profile["style_json"], {}),
                "expression_usage": _load_json(profile["style_json"], {}).get("expression_usage") or {},
                "existing_card": self._get_style_card_connection(connection, group_id),
                "candidate_terms": [
                    {
                        "phrase": row["phrase"],
                        "occurrence_count": int(row["occurrence_count"]),
                        "speaker_count": int(row["speaker_count"]),
                        "confidence": float(row["confidence"]),
                        "safe_to_use": bool(row["safe_to_use"]),
                        "examples": _load_json(row["examples_json"], [])[:2],
                    }
                    for row in terms
                ],
                "recent_samples": [
                    {
                        "timestamp": int(row["timestamp"]),
                        "speaker_name": row["speaker_name"],
                        "content": str(row["content"] or "")[:180],
                    }
                    for row in reversed(samples)
                ],
                "expressions": [
                    {
                        "situation": row["situation"],
                        "pattern": row["pattern"],
                        "count": int(row["count"] or 0),
                        "keywords": _load_json(row["keywords_json"], [])[:6],
                        "examples": _load_json(row["examples_json"], [])[:2],
                    }
                    for row in expressions
                ],
            }

    def save_style_card(self, group_id: str, card: dict, source_message_count: int):
        group_id = str(group_id or "unknown").strip() or "unknown"
        now = int(time.time())
        card_json = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
        with self._managed_connection() as connection:
            current = connection.execute(
                "SELECT version FROM style_cards WHERE group_id = ?", (group_id,)
            ).fetchone()
            version = int(current["version"] if current else 0) + 1
            connection.execute(
                "INSERT INTO style_cards(group_id, version, updated_at, source_message_count, card_json) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(group_id) DO UPDATE SET "
                "version=excluded.version, updated_at=excluded.updated_at, "
                "source_message_count=excluded.source_message_count, card_json=excluded.card_json",
                (group_id, version, now, int(source_message_count), card_json),
            )


    # ------------------------------------------------------------------
    # maibot-style expression library: (situation -> pattern) records.
    # The LLM decides at cycle end which expressions enter the library;
    # frequency counters and recall ranking are maintained locally.
    # ------------------------------------------------------------------

    @staticmethod
    def _find_expression_locked(connection, group_id, situation):
        """Find an existing expression that is exactly or lexically similar."""
        normalized = _normalize_match_text(situation)
        if not normalized:
            return None
        rows = connection.execute(
            "SELECT situation, pattern, count, status, keywords_json, examples_json "
            "FROM style_expressions WHERE group_id = ? ORDER BY count DESC, updated_at DESC LIMIT 200",
            (str(group_id or "unknown").strip() or "unknown",),
        ).fetchall()
        exact = None
        best = None
        best_score = 0.0
        for row in rows:
            other = _normalize_match_text(row["situation"])
            if not other:
                continue
            if other == normalized:
                exact = dict(row)
                break
            if normalized in other or other in normalized:
                score = 0.55 + 0.4 * min(len(normalized), len(other)) / max(len(normalized), len(other))
            else:
                shared = _longest_shared_span(normalized, other)
                if shared < 2:
                    continue
                score = shared / max(len(normalized), len(other))
            if score > best_score:
                best_score = score
                best = dict(row)
        return exact or (best if best_score >= 0.45 else None)

    @staticmethod
    def _delete_expression_locked(connection, group_id, situation):
        existing = ProfileStore._find_expression_locked(connection, group_id, situation)
        if existing is None:
            return False
        connection.execute(
            "DELETE FROM style_expressions WHERE group_id=? AND situation=?",
            (str(group_id or "unknown").strip() or "unknown", existing["situation"]),
        )
        return True

    @_maintenance_serialized
    def apply_expression_actions(self, group_id, actions) -> int:
        """Apply bounded LLM expression actions at cycle end; returns applied count."""
        group_id = str(group_id or "unknown").strip() or "unknown"
        now = int(time.time())
        applied = 0
        with self._managed_connection() as connection:
            for action in actions or []:
                if not isinstance(action, dict):
                    continue
                operation = str(action.get("action") or "keep").strip().lower()
                if operation == "keep":
                    continue
                situation = _clean_text(action.get("situation"), 160)
                if not situation:
                    continue
                if operation in {"delete", "remove"}:
                    if self._delete_expression_locked(connection, group_id, situation):
                        applied += 1
                    continue
                pattern = _clean_text(action.get("pattern"), 240)
                if not pattern:
                    continue
                searchable = f"{situation} {pattern}".casefold()
                if any(marker in searchable for marker in _EXPRESSION_UNSAFE_MARKERS):
                    continue
                keywords = _clean_text_list(action.get("situation_keywords") or action.get("keywords"), 8, 24)
                examples = _clean_text_list(action.get("examples"), 3, 160)
                try:
                    delta = max(0, min(200, int(action.get("occurrence_delta") or 1)))
                except (TypeError, ValueError):
                    delta = 1
                existing = self._find_expression_locked(connection, group_id, situation)
                if existing is not None:
                    merged_keywords = _clean_text_list(
                        _load_json(existing["keywords_json"], []) + keywords, 8, 24
                    )
                    merged_examples = _clean_text_list(
                        _load_json(existing["examples_json"], []) + examples, 3, 160
                    )
                    connection.execute(
                        "UPDATE style_expressions SET pattern=?, count=count+?, status='active', "
                        "keywords_json=?, examples_json=?, updated_at=?, last_seen=? "
                        "WHERE group_id=? AND situation=?",
                        (
                            pattern, delta,
                            json.dumps(merged_keywords, ensure_ascii=False, separators=(",", ":")),
                            json.dumps(merged_examples, ensure_ascii=False, separators=(",", ":")),
                            now, now, group_id, existing["situation"],
                        ),
                    )
                else:
                    connection.execute(
                        "INSERT INTO style_expressions("
                        "group_id, situation, pattern, count, status, keywords_json, examples_json, updated_at, last_seen) "
                        "VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                        (
                            group_id, situation, pattern, delta,
                            json.dumps(keywords, ensure_ascii=False, separators=(",", ":")),
                            json.dumps(examples, ensure_ascii=False, separators=(",", ":")),
                            now, now,
                        ),
                    )
                applied += 1
        return applied

    def get_expressions(self, group_id, limit=40, status="active") -> list[dict]:
        group_id = str(group_id or "unknown").strip() or "unknown"
        limit = max(1, min(int(limit or 40), 200))
        with self._managed_connection() as connection:
            if status:
                rows = connection.execute(
                    "SELECT situation, pattern, count, status, keywords_json, examples_json "
                    "FROM style_expressions WHERE group_id=? AND status=? "
                    "ORDER BY count DESC, updated_at DESC LIMIT ?",
                    (group_id, status, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT situation, pattern, count, status, keywords_json, examples_json "
                    "FROM style_expressions WHERE group_id=? "
                    "ORDER BY count DESC, updated_at DESC LIMIT ?",
                    (group_id, limit),
                ).fetchall()
        return [
            {
                "situation": row["situation"],
                "pattern": row["pattern"],
                "count": int(row["count"] or 0),
                "status": row["status"],
                "keywords": _load_json(row["keywords_json"], [])[:6],
                "examples": _load_json(row["examples_json"], [])[:2],
            }
            for row in rows
        ]

    def get_context_expressions(self, group_id, messages, max_items=6, max_chars=900) -> list[dict]:
        """Return a small expression set whose situation matches the current context.

        Lightweight local recall (??): keyword substring hits plus situation-token
        overlap with the current non-bot user messages. The full library never
        enters a prompt; the LLM still decides whether to actually use a pattern.
        """
        group_id = str(group_id or "unknown").strip() or "unknown"
        limit = max(1, min(int(max_items or 6), 12))
        budget = max(300, min(int(max_chars or 900), 2400))
        context_texts = []
        for item in messages or []:
            if not isinstance(item, dict) or item.get("is_bot") or item.get("role") == "assistant":
                continue
            text = _normalize_match_text(item.get("content"))
            if text:
                context_texts.append(text)
        if not context_texts:
            return []
        context = "".join(context_texts)
        context_tokens = _topic_tokens(" ".join(context_texts))
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT situation, pattern, count, keywords_json, examples_json "
                "FROM style_expressions WHERE group_id=? AND status='active' "
                "ORDER BY count DESC, updated_at DESC LIMIT 200",
                (group_id,),
            ).fetchall()
        ranked = []
        for row in rows:
            situation = str(row["situation"] or "")
            keywords = _load_json(row["keywords_json"], [])
            if not keywords:
                keywords = list(_topic_tokens(situation))[:6]
            hits = sum(1 for keyword in keywords if keyword and _normalize_match_text(keyword) in context)
            overlap = len(_topic_tokens(situation) & context_tokens)
            if hits == 0 and overlap == 0:
                continue
            score = hits * 3 + overlap * 2 + min(2.0, int(row["count"] or 0) / 20.0)
            ranked.append((score, row))
        ranked.sort(key=lambda entry: entry[0], reverse=True)
        result = []
        used = 0
        for _, row in ranked:
            item = {
                "situation": str(row["situation"] or "")[:140],
                "pattern": str(row["pattern"] or "")[:220],
                "count": int(row["count"] or 0),
                "examples": _load_json(row["examples_json"], [])[:2],
            }
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if used + len(encoded) + 1 > budget:
                continue
            result.append(item)
            used += len(encoded) + 1
            if len(result) >= limit:
                break
        return result


    def build_expression_pool(self, group_id, messages, pool_size=12, max_chars=2400, scan_limit=2000) -> list[dict]:
        """Build a maibot-style candidate pool: context hits plus count-weighted
        and recency-weighted samples, so low-frequency expressions also get
        exposure instead of being permanently starved by top-count ranking."""
        group_id = str(group_id or "unknown").strip() or "unknown"
        limit = max(4, min(int(pool_size or 12), 24))
        budget = max(600, min(int(max_chars or 2400), 4800))
        scan_limit = max(200, int(scan_limit or 2000))
        context_texts = []
        for item in messages or []:
            if not isinstance(item, dict) or item.get("is_bot") or item.get("role") == "assistant":
                continue
            text = _normalize_match_text(item.get("content"))
            if text:
                context_texts.append(text)
        context = "".join(context_texts)
        context_tokens = _topic_tokens(" ".join(context_texts)) if context_texts else set()
        now = int(time.time())
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT situation, pattern, count, keywords_json, examples_json, last_seen, last_active_time "
                "FROM style_expressions WHERE group_id=? AND status='active' "
                "ORDER BY count DESC, updated_at DESC LIMIT ?",
                (group_id, scan_limit),
            ).fetchall()
        hits = []
        rest = []
        for row in rows:
            situation = str(row["situation"] or "")
            keywords = _load_json(row["keywords_json"], [])
            if not keywords:
                keywords = list(_topic_tokens(situation))[:6]
            keyword_hits = sum(
                1 for keyword in keywords if keyword and _normalize_match_text(keyword) in context
            )
            overlap = len(_topic_tokens(situation) & context_tokens) if context_tokens else 0
            count = int(row["count"] or 0)
            if keyword_hits or overlap:
                hits.append((keyword_hits * 3 + overlap * 2 + min(2.0, count / 20.0), row))
            else:
                rest.append(row)
        hits.sort(key=lambda entry: entry[0], reverse=True)
        picked = []
        picked_keys = set()
        used_budget = 0

        def take(row, source, score):
            nonlocal used_budget
            situation = str(row["situation"] or "")
            if situation in picked_keys:
                return
            item = {
                "situation": situation,
                "pattern": str(row["pattern"] or "")[:220],
                "count": int(row["count"] or 0),
                "examples": _load_json(row["examples_json"], [])[:2],
                "source": source,
                "score": round(float(score or 0), 4),
            }
            encoded = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
            if used_budget + len(encoded) + 1 > budget:
                return
            picked.append(item)
            picked_keys.add(situation)
            used_budget += len(encoded) + 1

        for _, row in hits:
            take(row, "hit", 0)
            if len(picked) >= limit:
                break
        high_count = sorted(rest, key=lambda row: int(row["count"] or 0), reverse=True)
        high_budget = max(2, (limit - len(picked)) // 2 + 1)
        for row in high_count:
            if high_budget <= 0:
                break
            before = len(picked)
            take(row, "high", 0)
            if len(picked) > before:
                high_budget -= 1
            if len(picked) >= limit:
                break
        if len(picked) < limit:
            remaining = [row for row in rest if str(row["situation"] or "") not in picked_keys]
            random.shuffle(remaining)
            for row in remaining:
                weight = 1.0 + min(4.0, int(row["count"] or 0) / 20.0)
                try:
                    last = int(row["last_active_time"] or 0)
                except (TypeError, ValueError):
                    last = 0
                if not last:
                    try:
                        last = int(row["last_seen"] or 0)
                    except (TypeError, ValueError):
                        last = 0
                if last and now - last <= 30 * 86400:
                    weight *= 1.5
                take(row, "random", weight)
                if len(picked) >= limit:
                    break
        return picked

    def record_expression_selection(self, group_id, situations) -> int:
        """Mark expressions as injected into a reply prompt (recency + counter)."""
        group_id = str(group_id or "unknown").strip() or "unknown"
        situations = [str(item or "").strip() for item in (situations or []) if str(item or "").strip()]
        if not situations:
            return 0
        now = int(time.time())
        with self._managed_connection() as connection:
            updated = 0
            for situation in situations[:20]:
                cursor = connection.execute(
                    "UPDATE style_expressions SET last_active_time=? "
                    "WHERE group_id=? AND situation=? AND status='active'",
                    (now, group_id, situation),
                )
                updated += int(cursor.rowcount or 0)
            row = connection.execute(
                "SELECT style_json FROM group_profiles WHERE group_id = ?", (group_id,)
            ).fetchone()
            style = _load_json(row["style_json"], {}) if row else {}
            usage = style.get("expression_usage")
            if not isinstance(usage, dict):
                usage = {}
            for situation in situations[:20]:
                bucket = usage.setdefault(situation, {})
                bucket["injected"] = int(bucket.get("injected", 0) or 0) + 1
                bucket["last_injected_ts"] = now
            style["expression_usage"] = usage
            serialized = json.dumps(style, ensure_ascii=False, separators=(",", ":"))
            if row:
                connection.execute(
                    "UPDATE group_profiles SET updated_at=?, style_json=? WHERE group_id=?",
                    (now, serialized, group_id),
                )
            else:
                connection.execute(
                    "INSERT INTO group_profiles(group_id, updated_at, message_count, style_json, top_terms_json) "
                    "VALUES (?, ?, 0, ?, '[]')",
                    (group_id, now, serialized),
                )
        return updated

    def record_expression_usage(self, group_id, messages) -> int:
        """Detect whether the bot actually used an injected expression in its
        reply (pattern / examples substring match), mirroring slang usage."""
        group_id = str(group_id or "unknown").strip() or "unknown"
        reply_texts = []
        for item in messages or []:
            if not isinstance(item, dict):
                continue
            text = _normalize_match_text(item.get("content"))
            if text:
                reply_texts.append(text)
        if not reply_texts:
            return 0
        now = int(time.time())
        with self._managed_connection() as connection:
            rows = connection.execute(
                "SELECT situation, pattern, examples_json FROM style_expressions "
                "WHERE group_id=? AND status='active' ORDER BY count DESC LIMIT 500",
                (group_id,),
            ).fetchall()
            used = []
            for row in rows:
                situation = str(row["situation"] or "")
                keys = [_normalize_match_text(row["pattern"])] if row["pattern"] else []
                for example in _load_json(row["examples_json"], [])[:3]:
                    key = _normalize_match_text(example)
                    if key:
                        keys.append(key)
                keys = [key for key in keys if len(key) >= 2]
                if not keys:
                    continue
                if any(any(key in text for key in keys) for text in reply_texts):
                    used.append(situation)
            if not used:
                return 0
            row = connection.execute(
                "SELECT style_json FROM group_profiles WHERE group_id = ?", (group_id,)
            ).fetchone()
            style = _load_json(row["style_json"], {}) if row else {}
            usage = style.get("expression_usage")
            if not isinstance(usage, dict):
                usage = {}
            for situation in used[:50]:
                bucket = usage.setdefault(situation, {})
                bucket["used"] = int(bucket.get("used", 0) or 0) + 1
                bucket["last_used_ts"] = now
            style["expression_usage"] = usage
            serialized = json.dumps(style, ensure_ascii=False, separators=(",", ":"))
            if row:
                connection.execute(
                    "UPDATE group_profiles SET updated_at=?, style_json=? WHERE group_id=?",
                    (now, serialized, group_id),
                )
            else:
                connection.execute(
                    "INSERT INTO group_profiles(group_id, updated_at, message_count, style_json, top_terms_json) "
                    "VALUES (?, ?, 0, ?, '[]')",
                    (group_id, now, serialized),
                )
        return len(used)

    def get_expression_usage(self, group_id) -> dict:
        group_id = str(group_id or "unknown").strip() or "unknown"
        with self._managed_connection() as connection:
            row = connection.execute(
                "SELECT style_json FROM group_profiles WHERE group_id = ?", (group_id,)
            ).fetchone()
        style = _load_json(row["style_json"], {}) if row else {}
        usage = style.get("expression_usage")
        return dict(usage) if isinstance(usage, dict) else {}

    @staticmethod
    def _rotate_emotional_pool(ranked, limit):
        """maibot-inspired rotation: keep the top picks but let a mid-confidence,
        recently-active term rotate in so the emotional pool is not monopolised
        by the same highest-confidence phrases."""
        if len(ranked) <= max(limit, 2):
            return ranked
        top_count = max(1, int(limit) - 1)
        top = ranked[:top_count]
        rest = ranked[top_count:]
        now = int(time.time())
        mid = [
            entry for entry in rest
            if 0.4 <= float(entry[1]["confidence"] or 0) <= 0.8
            and (not int(entry[1]["last_seen"] or 0) or now - int(entry[1]["last_seen"] or 0) <= 14 * 86400)
        ]
        if mid:
            return top + [random.choice(mid)]
        return top + (rest[:1] if rest else [])



def _load_json(value, default):
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, type(default)) else default
    except Exception:
        return default


def _clean_text(value, limit=160):
    return str(value or "").strip()[:limit]


def _clean_text_list(value, max_items, item_limit):
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:max_items]:
        text = str(item or "").strip()[:item_limit]
        if text and text not in result:
            result.append(text)
    return result


def _clean_scene_list(value, max_items=6, fallback_uncertain=True):
    values = _clean_text_list(value, max_items, 40)
    result = []
    for value in values:
        if value not in SLANG_SCENE_LABELS and value != "uncertain":
            value = "其他"
        if value not in result:
            result.append(value)
    return result or (["uncertain"] if fallback_uncertain else [])


def _topic_tokens(text):
    tokens = set()
    for item in re.findall(r"[A-Za-z0-9_]{2,}|[\u3400-\u9fff]{2,}", str(text or "").casefold()):
        if item not in {"这个", "那个", "现在", "然后", "就是", "怎么", "什么", "可以"}:
            tokens.add(item)
            if len(item) > 3 and all("\u3400" <= char <= "\u9fff" for char in item):
                tokens.update(item[index:index + 2] for index in range(len(item) - 1))
    return tokens


def _normalize_match_text(value):
    """Normalize text for no-embedding phrase matching without tokenization."""
    text = str(value or "").casefold().strip()
    return re.sub(r"[^0-9a-z\u3400-\u9fff]+", "", text)


def _longest_shared_span(left: str, right: str) -> int:
    """Return the longest contiguous shared span for a small phrase pair."""
    if not left or not right:
        return 0
    previous = [0] * (len(right) + 1)
    best = 0
    for left_char in left:
        current = [0]
        for index, right_char in enumerate(right, 1):
            value = previous[index - 1] + 1 if left_char == right_char else 0
            current.append(value)
            best = max(best, value)
        previous = current
    return best


def _update_style(style: dict, observation: dict) -> dict:
    content = str(observation.get("content") or "").strip()
    chars = len(content)
    style = dict(style or {})
    style["total_chars"] = int(style.get("total_chars", 0)) + chars
    style["short_messages"] = int(style.get("short_messages", 0)) + int(chars <= 12)
    style["medium_messages"] = int(style.get("medium_messages", 0)) + int(13 <= chars <= 45)
    style["long_messages"] = int(style.get("long_messages", 0)) + int(chars > 45)
    punctuation = "，。！？；：、,.!?;:\n"
    has_punctuation = any(char in punctuation for char in content)
    style["punctuation_messages"] = int(style.get("punctuation_messages", 0)) + int(has_punctuation)
    style["no_punctuation_messages"] = int(style.get("no_punctuation_messages", 0)) + int(not has_punctuation)
    style["sentence_break_count"] = int(style.get("sentence_break_count", 0)) + sum(
        content.count(char) for char in punctuation
    )
    style["fragment_messages"] = int(style.get("fragment_messages", 0)) + int(
        0 < chars <= 12 and not has_punctuation
    )
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
