"""Small SQLite-backed semantic and episodic memory store.

This module is intentionally independent from the JSON short-term history.
Message ingestion is local and deterministic; the optional curator only runs
on a bounded batch of high-signal candidates.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_DB_PATH = os.path.join(BASE_DIR, "data", "memory.sqlite3")

_FACT_PATTERNS = (
    ("name", re.compile(r"^\s*我(?:叫|是)(?P<value>[^，,。.!！?？]{1,40})")),
    ("likes", re.compile(r"^\s*我(?:喜欢|喜歡)(?P<value>[^，,。.!！?？]{1,80})")),
    ("dislikes", re.compile(r"^\s*我不(?:喜欢|喜歡)(?P<value>[^，,。.!！?？]{1,80})")),
    ("role", re.compile(r"^\s*我(?:负责|負責|是群里的|是群裡的)(?P<value>[^，,。.!！?？]{1,80})")),
)
_SENSITIVE_MARKERS = (
    "密码", "密碼", "token", "secret", "密钥", "密鑰", "身份证", "身分證",
    "银行卡", "銀行卡", "电话", "電話", "邮箱", "郵箱", "住址",
)
_HIGH_SIGNAL_MARKERS = (
    "记住", "記住", "以后", "以後", "我们叫", "我們叫", "意思是",
    "约定", "約定", "负责", "負責", "喜欢", "喜歡", "不喜欢", "不喜歡",
)


def _safe(value, limit=500):
    return str(value or "").strip()[:limit]


def _now():
    return int(time.time())


def _tokens(text):
    text = _safe(text, 1200).casefold()
    result = set()
    for part in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9_]{2,}", text):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            result.update(part[index:index + 2] for index in range(len(part) - 1))
        else:
            result.add(part)
    return {item for item in result if len(item) >= 2}


def _estimate_tokens(value) -> int:
    """轻量 token 估算（CJK 每字 1 token，其他每 4 字符 1 token），与 prompt_builder/MemoryManager 一致。"""
    text = str(value or "")
    cjk = sum("㐀" <= char <= "鿿" for char in text)
    return cjk + max(0, (len(text) - cjk + 3) // 4)


def _truncate_to_token_budget(text, budget):
    """保留不超过预算的最长前缀（单条超长时的安全兆底）。"""
    text = str(text or "")
    if not text or _estimate_tokens(text) <= budget:
        return text
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if _estimate_tokens(text[:mid]) <= budget:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip()


class LongTermMemory:
    """SQLite store for person facts, group knowledge, and episodes."""

    VALID_KINDS = {"person_fact", "group_knowledge", "episode"}

    def __init__(self, config=None):
        config = config if isinstance(config, dict) else {}
        settings = config.get("memory") if isinstance(config.get("memory"), dict) else {}
        path = str(settings.get("db_path") or DEFAULT_DB_PATH).strip()
        if not os.path.isabs(path):
            path = os.path.join(BASE_DIR, path)
        self.path = os.path.abspath(path)
        self.enabled = bool(settings.get("enabled", True))
        self.max_candidates = max(100, self._int(settings.get("candidate_batch_size"), 30))
        self.max_context_chars = max(400, self._int(settings.get("context_max_chars"), 1400))
        self.fact_limit = max(1, self._int(settings.get("person_fact_limit"), 8))
        self.knowledge_limit = max(1, self._int(settings.get("group_knowledge_limit"), 10))
        self.short_memory_max_tokens = max(50, self._int(settings.get("short_memory_max_tokens"), 1000))
        self.bot_wxids = {
            _safe(item, 100)
            for item in (config.get("bot_wxids") or [])
            if _safe(item, 100)
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @staticmethod
    def _int(value, default):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    @contextmanager
    def _connection(self):
        with self._lock:
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
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    subject_id TEXT NOT NULL DEFAULT '',
                    group_id TEXT NOT NULL DEFAULT '',
                    fact_key TEXT NOT NULL DEFAULT '',
                    content TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0.5,
                    evidence_count INTEGER NOT NULL DEFAULT 1,
                    first_seen INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL,
                    expires_at INTEGER,
                    source_message_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    UNIQUE(kind, scope, subject_id, group_id, fact_key)
                );
                CREATE TABLE IF NOT EXISTS memory_terms (
                    memory_id INTEGER NOT NULL,
                    term TEXT NOT NULL,
                    PRIMARY KEY(memory_id, term),
                    FOREIGN KEY(memory_id) REFERENCES memories(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS memory_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL DEFAULT '',
                    subject_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_message_id TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    processed_at INTEGER,
                    fingerprint TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS group_memory_state (
                    group_id TEXT PRIMARY KEY,
                    short_memory TEXT NOT NULL DEFAULT '',
                    medium_memory TEXT NOT NULL DEFAULT '',
                    long_memory TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_memory_scope
                    ON memories(group_id, subject_id, status, confidence DESC, last_seen DESC);
                CREATE INDEX IF NOT EXISTS idx_memory_terms_term
                    ON memory_terms(term, memory_id);
                CREATE INDEX IF NOT EXISTS idx_candidates_pending
                    ON memory_candidates(processed_at, created_at);
                """
            )

    def record_message(self, context):
        """Extract high-signal candidates without making an LLM request."""
        if not self.enabled or not isinstance(context, dict):
            return 0
        content = _safe(context.get("content"), 1000)
        if not content or content.startswith("/") or context.get("prefix_used"):
            return 0
        if any(marker in content.casefold() for marker in _SENSITIVE_MARKERS):
            return 0
        wxid = _safe(context.get("wxid"), 120)
        if context.get("is_bot") or (wxid and wxid in self.bot_wxids):
            return 0
        group_id = _safe(context.get("group") or context.get("sessionId"), 200)
        message_id = _safe(
            context.get("messageKey") or context.get("message_id")
            or context.get("rawid") or context.get("serverId"),
            200,
        )
        candidates = []
        for predicate, pattern in _FACT_PATTERNS:
            match = pattern.search(content)
            if match:
                value = _safe(match.group("value"), 160)
                if value:
                    candidates.append(("person_fact", f"{predicate}={value}"))
                break

        if not candidates and any(marker in content for marker in _HIGH_SIGNAL_MARKERS):
            candidates.append(("group_knowledge", content))
        elif len(content) >= 45 and not content.startswith(("http://", "https://")):
            candidates.append(("episode", content))

        if not candidates:
            return 0

        inserted = 0
        now = _now()
        with self._connection() as connection:
            for kind, candidate in candidates[:3]:
                fingerprint = hashlib.sha256(
                    f"{group_id}|{wxid}|{kind}|{candidate}".encode("utf-8")
                ).hexdigest()
                row = connection.execute(
                    "INSERT OR IGNORE INTO memory_candidates "
                    "(group_id, subject_id, kind, content, source_message_id, created_at, fingerprint) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (group_id, wxid, kind, candidate, message_id, now, fingerprint),
                )
                inserted += max(0, row.rowcount)

                if kind == "person_fact" and wxid:
                    predicate, _, value = candidate.partition("=")
                    self._upsert_memory_locked(
                        connection,
                        kind=kind,
                        scope="person",
                        subject_id=wxid,
                        group_id=group_id,
                        fact_key=predicate,
                        content=f"{predicate}: {value}",
                        confidence=0.62,
                        source_message_id=message_id,
                        now=now,
                    )
        return inserted

    def _upsert_memory_locked(
        self, connection, *, kind, scope, subject_id, group_id, fact_key,
        content, confidence=0.5, source_message_id="", expires_at=None, now=None,
    ):
        kind = _safe(kind, 40)
        if kind not in self.VALID_KINDS:
            return None
        now = now or _now()
        content = _safe(content, 500)
        if not content:
            return None
        values = (
            kind, _safe(scope, 20), _safe(subject_id, 160), _safe(group_id, 200),
            _safe(fact_key, 120), content, max(0.0, min(float(confidence), 1.0)),
            now, now, expires_at, _safe(source_message_id, 200),
        )
        connection.execute(
            "INSERT INTO memories "
            "(kind, scope, subject_id, group_id, fact_key, content, confidence, "
            "evidence_count, first_seen, last_seen, expires_at, source_message_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?) "
            "ON CONFLICT(kind, scope, subject_id, group_id, fact_key) DO UPDATE SET "
            "content=excluded.content, confidence=MAX(memories.confidence, excluded.confidence), "
            "evidence_count=memories.evidence_count + 1, last_seen=excluded.last_seen, "
            "expires_at=excluded.expires_at, source_message_id=excluded.source_message_id, "
            "status='active'",
            values,
        )
        row = connection.execute(
            "SELECT id FROM memories WHERE kind=? AND scope=? AND subject_id=? AND group_id=? AND fact_key=?",
            values[:5],
        ).fetchone()
        if not row:
            return None
        memory_id = int(row[0])
        connection.execute("DELETE FROM memory_terms WHERE memory_id=?", (memory_id,))
        connection.executemany(
            "INSERT OR IGNORE INTO memory_terms(memory_id, term) VALUES (?, ?)",
            [(memory_id, term) for term in _tokens(content)],
        )
        return memory_id

    def add_curated_memory(self, item):
        if not self.enabled or not isinstance(item, dict):
            return False
        kind = _safe(item.get("kind"), 40)
        scope = _safe(item.get("scope") or ("person" if kind == "person_fact" else "group"), 20)
        content = _safe(item.get("content"), 500)
        if kind not in self.VALID_KINDS or not content:
            return False
        if any(marker in content.casefold() for marker in _SENSITIVE_MARKERS):
            return False
        if scope == "person" and not _safe(item.get("subject_id"), 160):
            return False
        try:
            confidence = float(item.get("confidence", 0.55))
        except (TypeError, ValueError):
            confidence = 0.55
        expires_at = None
        if item.get("expires_days") not in (None, ""):
            try:
                expires_at = _now() + max(1, int(item.get("expires_days"))) * 86400
            except (TypeError, ValueError):
                expires_at = None
        with self._connection() as connection:
            self._upsert_memory_locked(
                connection,
                kind=kind,
                scope=scope,
                subject_id=_safe(item.get("subject_id"), 160),
                group_id=_safe(item.get("group_id"), 200),
                fact_key=_safe(item.get("fact_key") or content[:80], 120),
                content=content,
                confidence=confidence,
                source_message_id=_safe(item.get("source_message_id"), 200),
                expires_at=expires_at,
            )
        return True

    def get_context(self, group_id, subject_id="", query="", max_chars=None):
        if not self.enabled:
            return ""
        group_id = _safe(group_id, 200)
        subject_id = _safe(subject_id, 160)
        configured_max = self.max_context_chars if max_chars is None else int(max_chars or 0)
        terms = _tokens(query)
        now = _now()
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM memories WHERE status='active' AND "
                "(group_id=? OR group_id='' OR subject_id=?) AND "
                "(expires_at IS NULL OR expires_at>?) "
                "ORDER BY confidence DESC, last_seen DESC LIMIT 120",
                (group_id, subject_id, now),
            ).fetchall()
            memory_ids = [int(row["id"]) for row in rows]
            terms_by_memory = {memory_id: set() for memory_id in memory_ids}
            if memory_ids:
                placeholders = ",".join("?" for _ in memory_ids)
                term_rows = connection.execute(
                    f"SELECT memory_id, term FROM memory_terms WHERE memory_id IN ({placeholders})",
                    memory_ids,
                ).fetchall()
                for term_row in term_rows:
                    terms_by_memory[int(term_row["memory_id"])].add(term_row["term"])
            scored = []
            for row in rows:
                content = str(row["content"] or "")
                row_terms = terms_by_memory.get(int(row["id"]), set())
                overlap = len(terms & row_terms)
                age_days = max(0, (now - int(row["last_seen"] or now)) / 86400)
                score = overlap * 3 + float(row["confidence"]) * 2 + min(1.0, int(row["evidence_count"]) / 5)
                score += max(0.0, 0.5 - age_days / 365)
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        lines = []
        used = 0
        counts = {"person_fact": 0, "group_knowledge": 0, "episode": 0}
        for _, row in scored:
            kind = row["kind"]
            if kind == "person_fact" and counts[kind] >= self.fact_limit:
                continue
            if kind == "group_knowledge" and counts[kind] >= self.knowledge_limit:
                continue
            line = f"[{kind}] {row['content']}"
            if configured_max > 0 and used + len(line) + 1 > configured_max:
                continue
            lines.append(line)
            used += len(line) + 1
            counts[kind] += 1
            if configured_max > 0 and used >= configured_max:
                break
        return "\n".join(lines)

    def get_group_memory_state(self, group_id) -> dict:
        group_id = _safe(group_id, 200)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT short_memory, medium_memory, long_memory, updated_at "
                "FROM group_memory_state WHERE group_id=?",
                (group_id,),
            ).fetchone()
        if not row:
            return {"short_memory": "", "medium_memory": "", "long_memory": "", "updated_at": 0}
        return dict(row)

    def save_group_memory_state(self, group_id, state: dict) -> bool:
        if not self.enabled or not isinstance(state, dict):
            return False
        group_id = _safe(group_id, 200)
        values = (
            str(state.get("short_memory") or ""),
            str(state.get("medium_memory") or ""),
            str(state.get("long_memory") or ""),
            _now(),
            group_id,
        )
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO group_memory_state(group_id, short_memory, medium_memory, long_memory, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(group_id) DO UPDATE SET "
                "short_memory=excluded.short_memory, medium_memory=excluded.medium_memory, "
                "long_memory=excluded.long_memory, updated_at=excluded.updated_at",
                (group_id, values[0], values[1], values[2], values[3]),
            )
        return True

    def get_person_profile_context(self, group_id, subject_id="", query="") -> str:
        group_id = _safe(group_id, 200)
        subject_id = _safe(subject_id, 160)
        terms = _tokens(query)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, fact_key, content, confidence, evidence_count, last_seen "
                "FROM memories WHERE kind='person_fact' AND scope='person' AND group_id=? "
                "AND (?='' OR subject_id=?) AND status='active' "
                "ORDER BY last_seen DESC, confidence DESC",
                (group_id, subject_id, subject_id),
            ).fetchall()
        ranked = []
        for row in rows:
            content = str(row["content"] or "")
            score = len(terms & _tokens(content))
            ranked.append((score, row))
        ranked.sort(key=lambda item: (item[0], item[1]["last_seen"], item[1]["confidence"]), reverse=True)
        return "\n".join(
            f"[person_profile id={row['id']} key={row['fact_key']}] {row['content']}"
            for _, row in ranked
        )

    def get_memory_records(self, group_id, subject_id="") -> list[dict]:
        group_id = _safe(group_id, 200)
        subject_id = _safe(subject_id, 160)
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, kind, scope, subject_id, group_id, fact_key, content, confidence, "
                "evidence_count, last_seen FROM memories WHERE group_id=? "
                "AND (?='' OR subject_id=?) AND status='active' ORDER BY last_seen DESC",
                (group_id, subject_id, subject_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def _enforce_short_memory_budget(self, state, counters=None):
        """短期记忆硬上限：超限时从开头丢弃最旧条目
        （行首为最早记忆，新内容追加在末尾），单条超长时截断；
        保证写库后的短期记忆不超过上限。"""
        counters = counters if counters is not None else {}
        text = str(state.get("short_memory") or "").strip()
        if not text:
            return
        budget = self.short_memory_max_tokens
        if _estimate_tokens(text) <= budget:
            return
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        kept = []
        used = 0
        for line in reversed(lines):  # 自最新往最早收集，优先保留最新的短期记忆
            cost = _estimate_tokens(line)
            if not kept and cost > budget:
                kept.append(_truncate_to_token_budget(line, budget))
                used = _estimate_tokens(kept[0])
                break
            if kept and used + cost > budget:
                break
            kept.append(line)
            used += cost
        kept.reverse()
        state["short_memory"] = "\n".join(kept)
        counters["truncated"] = int(counters.get("truncated") or 0) + 1

    def enforce_short_memory_budget(self, group_id) -> int:
        """独立触发的短期记忆上限整理并落库（返回被截断的条数）。"""
        if not self.enabled:
            return 0
        group_id = _safe(group_id, 200)
        state = self.get_group_memory_state(group_id)
        counters = {}
        self._enforce_short_memory_budget(state, counters)
        if counters.get("truncated"):
            self.save_group_memory_state(group_id, state)
        return int(counters.get("truncated") or 0)

    def apply_memory_actions(self, group_id, actions) -> dict:
        """Apply validated LLM actions atomically; the LLM chooses the changes."""
        if not self.enabled:
            return {"updated": 0, "deleted": 0, "added": 0}
        group_id = _safe(group_id, 200)
        state = self.get_group_memory_state(group_id)
        counters = {"updated": 0, "deleted": 0, "added": 0}
        with self._connection() as connection:
            for action in actions or []:
                if not isinstance(action, dict):
                    continue
                memory_type = str(action.get("memory_type") or action.get("kind") or "").strip().lower()
                operation = str(action.get("action") or "update").strip().lower()
                content = str(action.get("content") or "")
                if memory_type in {"short", "medium", "long"}:
                    if operation in {"delete", "clear", "remove"}:
                        state[f"{memory_type}_memory"] = ""
                        counters["deleted"] += 1
                    elif memory_type == "short" and operation in {"append", "add", "replace", "update", "set"}:
                        key = "short_memory"
                        state[key] = (str(state.get(key) or "") + ("\n" if state.get(key) else "") + content).strip()
                        counters["added"] += 1
                    elif operation in {"append", "add"}:
                        key = f"{memory_type}_memory"
                        state[key] = (str(state.get(key) or "") + ("\n" if state.get(key) else "") + content).strip()
                        counters["added"] += 1
                    elif operation in {"replace", "update", "set"}:
                        state[f"{memory_type}_memory"] = content
                        counters["updated"] += 1
                    continue

                if memory_type not in {"person", "person_profile", "person_fact"}:
                    continue
                subject_id = _safe(action.get("subject_id"), 160)
                fact_key = _safe(action.get("fact_key") or content[:80], 120)
                if not subject_id or not fact_key:
                    continue
                if operation in {"delete", "clear", "remove"}:
                    memory_id = action.get("memory_id")
                    if str(memory_id).isdigit():
                        connection.execute(
                            "DELETE FROM memories WHERE id=? AND group_id=? AND kind='person_fact'",
                            (int(memory_id), group_id),
                        )
                    else:
                        connection.execute(
                            "DELETE FROM memories WHERE group_id=? AND kind='person_fact' "
                            "AND subject_id=? AND fact_key=?",
                            (group_id, subject_id, fact_key),
                        )
                    counters["deleted"] += 1
                    continue
                if not content:
                    continue
                self._upsert_memory_locked(
                    connection,
                    kind="person_fact",
                    scope="person",
                    subject_id=subject_id,
                    group_id=group_id,
                    fact_key=fact_key,
                    content=content,
                    confidence=float(action.get("confidence", 0.6) or 0.6),
                    source_message_id=_safe(action.get("source_message_id"), 200),
                )
                counters["updated" if operation in {"update", "replace", "set"} else "added"] += 1

            self._enforce_short_memory_budget(state, counters)
            connection.execute(
                "INSERT INTO group_memory_state(group_id, short_memory, medium_memory, long_memory, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(group_id) DO UPDATE SET "
                "short_memory=excluded.short_memory, medium_memory=excluded.medium_memory, "
                "long_memory=excluded.long_memory, updated_at=excluded.updated_at",
                (group_id, state.get("short_memory", ""), state.get("medium_memory", ""),
                 state.get("long_memory", ""), _now()),
            )
        return counters

    def pending_candidates(self, group_id=None, limit=None):
        limit = max(1, min(int(limit or self.max_candidates), 500))
        group_id = _safe(group_id, 200)
        with self._connection() as connection:
            if group_id:
                rows = connection.execute(
                    "SELECT * FROM memory_candidates WHERE processed_at IS NULL AND group_id=? "
                    "ORDER BY created_at ASC LIMIT ?", (group_id, limit)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM memory_candidates WHERE processed_at IS NULL "
                    "ORDER BY created_at ASC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    def mark_candidates_processed(self, candidate_ids):
        ids = [int(item) for item in (candidate_ids or []) if str(item).isdigit()]
        if not ids:
            return
        now = _now()
        with self._connection() as connection:
            connection.executemany(
                "UPDATE memory_candidates SET processed_at=? WHERE id=?",
                [(now, item) for item in ids],
            )
