"""v23 到 v24 schema 迁移：收敛行为动作/结果实体并移除冗余图边。"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from typing import Any

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext

logger = get_logger("database_migration")


def migrate_v23_to_v24(context: MigrationExecutionContext) -> None:
    """将 action/outcome 从图节点降级为文本实体，并重建行为路径表。"""

    context.start_progress(
        total_tables=5,
        total_records=0,
        description="v23 -> v24 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    connection = context.connection
    action_count = _rebuild_behavior_actions_table(connection)
    context.advance_progress(records=action_count, completed_tables=1, item_name="behavior_actions")

    outcome_count = _rebuild_behavior_outcomes_table(connection)
    context.advance_progress(records=outcome_count, completed_tables=1, item_name="behavior_outcomes")

    path_count = _rebuild_behavior_experience_paths_table(connection)
    context.advance_progress(records=path_count, completed_tables=1, item_name="behavior_experience_paths")

    _drop_table_if_exists(connection, "behavior_scene_action_edges")
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_scene_action_edges")

    _drop_table_if_exists(connection, "behavior_action_outcome_edges")
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_action_outcome_edges")

    logger.info(
        "v23 -> v24 数据库迁移完成："
        f"actions={action_count} outcomes={outcome_count} paths={path_count}，已移除冗余 action/outcome 图边"
    )


def _rebuild_behavior_actions_table(connection: Connection) -> int:
    _drop_table_if_exists(connection, "behavior_actions_v24")
    _create_behavior_actions_table(connection, table_name="behavior_actions_v24")
    _drop_table_if_exists(connection, "behavior_action_id_map")
    connection.exec_driver_sql(
        """
        CREATE TEMP TABLE behavior_action_id_map (
            old_id INTEGER NOT NULL PRIMARY KEY,
            new_id INTEGER NOT NULL
        )
        """
    )

    if not _has_table(connection, "behavior_action_nodes"):
        _create_behavior_actions_indexes(connection, table_name="behavior_actions_v24")
        connection.exec_driver_sql("ALTER TABLE behavior_actions_v24 RENAME TO behavior_actions")
        _create_behavior_actions_indexes(connection)
        return 0

    rows = connection.exec_driver_sql(
        """
        SELECT id, session_id, action, COALESCE(source_count, 0), update_time
        FROM behavior_action_nodes
        ORDER BY id
        """
    ).fetchall()
    for old_id, session_id, action, source_count, update_time in rows:
        action_text = _normalize_text(action, max_length=240)
        if not action_text:
            continue
        action_hash = _hash_text(action_text)
        existing_row = connection.exec_driver_sql(
            """
            SELECT id, source_count
            FROM behavior_actions_v24
            WHERE session_id IS ? AND action_hash = ?
            """,
            (session_id, action_hash),
        ).fetchone()
        if existing_row is None:
            new_id = int(old_id)
            connection.exec_driver_sql(
                """
                INSERT INTO behavior_actions_v24 (
                    id,
                    session_id,
                    action,
                    action_hash,
                    source_count,
                    create_time,
                    update_time
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    session_id,
                    action_text,
                    action_hash,
                    int(source_count or 0),
                    update_time or _now_text(),
                    update_time or _now_text(),
                ),
            )
        else:
            new_id = int(existing_row[0])
            merged_source_count = int(existing_row[1] or 0) + int(source_count or 0)
            connection.exec_driver_sql(
                """
                UPDATE behavior_actions_v24
                SET source_count = ?, update_time = ?
                WHERE id = ?
                """,
                (merged_source_count, update_time or _now_text(), new_id),
            )
        connection.exec_driver_sql(
            "INSERT INTO behavior_action_id_map (old_id, new_id) VALUES (?, ?)",
            (int(old_id), new_id),
        )

    count = _count_table(connection, "behavior_actions_v24")
    _drop_table_if_exists(connection, "behavior_actions")
    connection.exec_driver_sql("ALTER TABLE behavior_actions_v24 RENAME TO behavior_actions")
    _create_behavior_actions_indexes(connection)
    _drop_table_if_exists(connection, "behavior_action_nodes")
    return count


def _rebuild_behavior_outcomes_table(connection: Connection) -> int:
    _drop_table_if_exists(connection, "behavior_outcomes_v24")
    _create_behavior_outcomes_table(connection, table_name="behavior_outcomes_v24")
    _drop_table_if_exists(connection, "behavior_outcome_id_map")
    connection.exec_driver_sql(
        """
        CREATE TEMP TABLE behavior_outcome_id_map (
            old_id INTEGER NOT NULL PRIMARY KEY,
            new_id INTEGER NOT NULL
        )
        """
    )

    if not _has_table(connection, "behavior_outcome_nodes"):
        _create_behavior_outcomes_indexes(connection, table_name="behavior_outcomes_v24")
        connection.exec_driver_sql("ALTER TABLE behavior_outcomes_v24 RENAME TO behavior_outcomes")
        _create_behavior_outcomes_indexes(connection)
        return 0

    rows = connection.exec_driver_sql(
        """
        SELECT id, session_id, outcome, COALESCE(source_count, 0), update_time
        FROM behavior_outcome_nodes
        ORDER BY id
        """
    ).fetchall()
    for old_id, session_id, outcome, source_count, update_time in rows:
        outcome_text = _normalize_text(outcome, max_length=220)
        if not outcome_text:
            continue
        outcome_hash = _hash_text(outcome_text)
        existing_row = connection.exec_driver_sql(
            """
            SELECT id, source_count
            FROM behavior_outcomes_v24
            WHERE session_id IS ? AND outcome_hash = ?
            """,
            (session_id, outcome_hash),
        ).fetchone()
        if existing_row is None:
            new_id = int(old_id)
            connection.exec_driver_sql(
                """
                INSERT INTO behavior_outcomes_v24 (
                    id,
                    session_id,
                    outcome,
                    outcome_hash,
                    source_count,
                    create_time,
                    update_time
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    session_id,
                    outcome_text,
                    outcome_hash,
                    int(source_count or 0),
                    update_time or _now_text(),
                    update_time or _now_text(),
                ),
            )
        else:
            new_id = int(existing_row[0])
            merged_source_count = int(existing_row[1] or 0) + int(source_count or 0)
            connection.exec_driver_sql(
                """
                UPDATE behavior_outcomes_v24
                SET source_count = ?, update_time = ?
                WHERE id = ?
                """,
                (merged_source_count, update_time or _now_text(), new_id),
            )
        connection.exec_driver_sql(
            "INSERT INTO behavior_outcome_id_map (old_id, new_id) VALUES (?, ?)",
            (int(old_id), new_id),
        )

    count = _count_table(connection, "behavior_outcomes_v24")
    _drop_table_if_exists(connection, "behavior_outcomes")
    connection.exec_driver_sql("ALTER TABLE behavior_outcomes_v24 RENAME TO behavior_outcomes")
    _create_behavior_outcomes_indexes(connection)
    _drop_table_if_exists(connection, "behavior_outcome_nodes")
    return count


def _rebuild_behavior_experience_paths_table(connection: Connection) -> int:
    if not _has_table(connection, "behavior_experience_paths"):
        _create_behavior_experience_paths_table(connection)
        return 0
    if _has_column(connection, "behavior_experience_paths", "action_id") and _has_column(
        connection,
        "behavior_experience_paths",
        "outcome_id",
    ):
        _create_behavior_experience_paths_indexes(connection)
        return _count_table(connection, "behavior_experience_paths")

    _drop_table_if_exists(connection, "behavior_experience_paths_v24")
    _create_behavior_experience_paths_table(connection, table_name="behavior_experience_paths_v24")
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO behavior_experience_paths_v24 (
            id,
            session_id,
            scene_cluster_id,
            action_id,
            outcome_id,
            actor_type,
            learning_type,
            evidence_list,
            feedback_list,
            count,
            activation_count,
            success_count,
            failure_count,
            score,
            enabled,
            last_active_time,
            last_feedback_time,
            create_time,
            update_time
        )
        SELECT
            path.id,
            path.session_id,
            path.scene_cluster_id,
            action_map.new_id,
            outcome_map.new_id,
            COALESCE(path.actor_type, 'other_user'),
            COALESCE(path.learning_type, 'observed_behavior'),
            COALESCE(path.evidence_list, '[]'),
            COALESCE(path.feedback_list, '[]'),
            COALESCE(path.count, 0),
            COALESCE(path.activation_count, 0),
            COALESCE(path.success_count, 0),
            COALESCE(path.failure_count, 0),
            COALESCE(path.score, 0),
            COALESCE(path.enabled, 1),
            path.last_active_time,
            path.last_feedback_time,
            path.create_time,
            path.update_time
        FROM behavior_experience_paths AS path
        INNER JOIN behavior_action_id_map AS action_map
            ON action_map.old_id = path.action_node_id
        INNER JOIN behavior_outcome_id_map AS outcome_map
            ON outcome_map.old_id = path.outcome_node_id
        """
    )
    count = _count_table(connection, "behavior_experience_paths_v24")
    connection.exec_driver_sql("DROP TABLE behavior_experience_paths")
    connection.exec_driver_sql("ALTER TABLE behavior_experience_paths_v24 RENAME TO behavior_experience_paths")
    _create_behavior_experience_paths_indexes(connection)
    return count


def _create_behavior_actions_table(connection: Connection, *, table_name: str = "behavior_actions") -> None:
    escaped_table_name = _escape_identifier(table_name)
    connection.exec_driver_sql(
        f"""
        CREATE TABLE IF NOT EXISTS "{escaped_table_name}" (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            action TEXT NOT NULL,
            action_hash VARCHAR(64) NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 0,
            create_time DATETIME NOT NULL,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (session_id, action_hash)
        )
        """
    )


def _create_behavior_actions_indexes(connection: Connection, *, table_name: str = "behavior_actions") -> None:
    escaped_table_name = _escape_identifier(table_name)
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_session_id" ON "{escaped_table_name}" (session_id)'
    )
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_action_hash" ON "{escaped_table_name}" (action_hash)'
    )
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_update_time" ON "{escaped_table_name}" (update_time)'
    )


def _create_behavior_outcomes_table(connection: Connection, *, table_name: str = "behavior_outcomes") -> None:
    escaped_table_name = _escape_identifier(table_name)
    connection.exec_driver_sql(
        f"""
        CREATE TABLE IF NOT EXISTS "{escaped_table_name}" (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            outcome TEXT NOT NULL,
            outcome_hash VARCHAR(64) NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 0,
            create_time DATETIME NOT NULL,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (session_id, outcome_hash)
        )
        """
    )


def _create_behavior_outcomes_indexes(connection: Connection, *, table_name: str = "behavior_outcomes") -> None:
    escaped_table_name = _escape_identifier(table_name)
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_session_id" ON "{escaped_table_name}" (session_id)'
    )
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_outcome_hash" ON "{escaped_table_name}" (outcome_hash)'
    )
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_update_time" ON "{escaped_table_name}" (update_time)'
    )


def _create_behavior_experience_paths_table(
    connection: Connection,
    *,
    table_name: str = "behavior_experience_paths",
) -> None:
    escaped_table_name = _escape_identifier(table_name)
    connection.exec_driver_sql(
        f"""
        CREATE TABLE IF NOT EXISTS "{escaped_table_name}" (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            scene_cluster_id INTEGER NOT NULL,
            action_id INTEGER NOT NULL,
            outcome_id INTEGER NOT NULL,
            actor_type VARCHAR(40) NOT NULL DEFAULT 'other_user',
            learning_type VARCHAR(40) NOT NULL DEFAULT 'observed_behavior',
            evidence_list TEXT NOT NULL DEFAULT '[]',
            feedback_list TEXT NOT NULL DEFAULT '[]',
            count INTEGER NOT NULL DEFAULT 0,
            activation_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            score FLOAT NOT NULL DEFAULT 0,
            enabled BOOLEAN NOT NULL DEFAULT 1,
            last_active_time DATETIME NOT NULL,
            last_feedback_time DATETIME,
            create_time DATETIME NOT NULL,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            UNIQUE (session_id, scene_cluster_id, action_id, outcome_id, actor_type, learning_type)
        )
        """
    )


def _create_behavior_experience_paths_indexes(
    connection: Connection,
    *,
    table_name: str = "behavior_experience_paths",
) -> None:
    escaped_table_name = _escape_identifier(table_name)
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_session_enabled" '
        f'ON "{escaped_table_name}" (session_id, enabled)'
    )
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_cluster" ON "{escaped_table_name}" (scene_cluster_id)'
    )
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_learning_type" ON "{escaped_table_name}" (learning_type)'
    )
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_actor_type" ON "{escaped_table_name}" (actor_type)'
    )
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_action" ON "{escaped_table_name}" (action_id)'
    )
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_outcome" ON "{escaped_table_name}" (outcome_id)'
    )
    connection.exec_driver_sql(
        f'CREATE INDEX IF NOT EXISTS "ix_{escaped_table_name}_update_time" ON "{escaped_table_name}" (update_time)'
    )


def _normalize_text(value: Any, *, max_length: int) -> str:
    normalized = " ".join(str(value or "").split()).strip()
    if len(normalized) <= max_length:
        return normalized
    return normalized[:max_length].rstrip()


def _hash_text(value: str) -> str:
    return sha256(" ".join(str(value or "").split()).strip().encode("utf-8")).hexdigest()


def _now_text() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _count_table(connection: Connection, table_name: str) -> int:
    row = connection.exec_driver_sql(f'SELECT COUNT(*) FROM "{_escape_identifier(table_name)}"').fetchone()
    return int(row[0] or 0) if row is not None else 0


def _has_table(connection: Connection, table_name: str) -> bool:
    exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return exists is not None


def _has_column(connection: Connection, table_name: str, column_name: str) -> bool:
    rows = connection.exec_driver_sql(f'PRAGMA table_info("{_escape_identifier(table_name)}")').fetchall()
    return any(row[1] == column_name for row in rows)


def _drop_table_if_exists(connection: Connection, table_name: str) -> None:
    connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{_escape_identifier(table_name)}"')


def _escape_identifier(identifier: str) -> str:
    return str(identifier).replace('"', '""')
