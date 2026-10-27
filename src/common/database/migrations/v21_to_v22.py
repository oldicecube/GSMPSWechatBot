"""v21 到 v22 schema 迁移：重建行为场景索引并清理 legacy v1 遗留表。"""

from __future__ import annotations

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext

logger = get_logger("database_migration")


LEGACY_V1_BACKUP_TABLE_PREFIX = "__legacy_v1_"
LEGACY_V1_BACKUP_TABLES = (
    "__legacy_v1_action_records",
    "__legacy_v1_chat_history",
    "__legacy_v1_chat_streams",
    "__legacy_v1_emoji",
    "__legacy_v1_emoji_description_cache",
    "__legacy_v1_expression",
    "__legacy_v1_group_info",
    "__legacy_v1_image_descriptions",
    "__legacy_v1_images",
    "__legacy_v1_jargon",
    "__legacy_v1_llm_usage",
    "__legacy_v1_messages",
    "__legacy_v1_online_time",
    "__legacy_v1_person_info",
    "__legacy_v1_thinking_back",
)
LEGACY_V1_STALE_TABLES = (
    "chat_history",
    "thinking_back",
)
LEGACY_V1_CLEANUP_TABLES = LEGACY_V1_BACKUP_TABLES + LEGACY_V1_STALE_TABLES

_BEHAVIOR_GRAPH_TABLES = (
    "behavior_action_outcome_edges",
    "behavior_scene_action_edges",
    "behavior_experience_scene_links",
    "behavior_scene_edges",
    "behavior_scene_node_tags",
    "behavior_experience_paths",
    "behavior_action_nodes",
    "behavior_outcome_nodes",
    "behavior_scene_nodes",
    "behavior_scene_clusters",
    "behavior_scene_tag_clusters",
)


def migrate_v21_to_v22(context: MigrationExecutionContext) -> None:
    """合并本地行为图结构迁移，并清理 legacy v1 遗留表。"""

    existing_cleanup_tables = _existing_tables(context.connection, LEGACY_V1_CLEANUP_TABLES)
    context.start_progress(
        total_tables=len(_BEHAVIOR_GRAPH_TABLES) + 3 + max(len(existing_cleanup_tables), 1),
        total_records=0,
        description="v21 -> v22 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _ensure_behavior_scene_nodes_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_scene_nodes")
    _ensure_behavior_scene_tag_clusters_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_scene_tag_clusters")
    _create_behavior_scene_node_tags_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_scene_node_tags")

    removed_behavior_records = 0
    for table_name in _BEHAVIOR_GRAPH_TABLES:
        removed_count = _clear_table(context.connection, table_name)
        removed_behavior_records += removed_count
        context.advance_progress(records=removed_count, completed_tables=1, item_name=table_name)

    for table_name in existing_cleanup_tables:
        _drop_table(context.connection, table_name)
        context.advance_progress(records=0, completed_tables=1, item_name=table_name)

    if not existing_cleanup_tables:
        context.advance_progress(records=0, completed_tables=1, item_name="legacy_v1_cleanup")

    logger.info(
        "v21 -> v22 数据库迁移完成：已合并行为场景索引重建、旧行为学习数据清理和 legacy v1 遗留表清理，"
        f"共清空 {removed_behavior_records} 条旧行为场景学习数据，"
        f"删除 legacy 表 {len(existing_cleanup_tables)} 个"
    )


def has_legacy_v1_cleanup_tables(connection: Connection) -> bool:
    """检查数据库是否仍包含 legacy v1 清理目标。"""

    return bool(_existing_tables(connection, LEGACY_V1_CLEANUP_TABLES))


def _ensure_behavior_scene_nodes_table(connection: Connection) -> None:
    if not _has_table(connection, "behavior_scene_nodes"):
        return
    if not _has_column(connection, "behavior_scene_nodes", "normalized_name"):
        _create_behavior_scene_node_indexes(connection)
        return

    connection.exec_driver_sql("DROP TABLE IF EXISTS behavior_scene_nodes_v22")
    connection.exec_driver_sql(
        """
        CREATE TABLE behavior_scene_nodes_v22 (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            node_kind VARCHAR(40) NOT NULL DEFAULT 'scene',
            name TEXT NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 0,
            score FLOAT NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id)
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO behavior_scene_nodes_v22 (
            id,
            session_id,
            node_kind,
            name,
            source_count,
            score,
            update_time
        )
        SELECT
            id,
            session_id,
            node_kind,
            CASE
                WHEN normalized_name IS NOT NULL AND TRIM(normalized_name) != '' THEN normalized_name
                ELSE LOWER(TRIM(name))
            END,
            source_count,
            score,
            update_time
        FROM behavior_scene_nodes
        """
    )
    connection.exec_driver_sql("DROP TABLE behavior_scene_nodes")
    connection.exec_driver_sql("ALTER TABLE behavior_scene_nodes_v22 RENAME TO behavior_scene_nodes")
    _create_behavior_scene_node_indexes(connection)


def _create_behavior_scene_node_indexes(connection: Connection) -> None:
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_nodes_session_kind "
        "ON behavior_scene_nodes (session_id, node_kind)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_nodes_session_kind_name "
        "ON behavior_scene_nodes (session_id, node_kind, name)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_nodes_session_id "
        "ON behavior_scene_nodes (session_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_nodes_update_time "
        "ON behavior_scene_nodes (update_time)"
    )


def _ensure_behavior_scene_tag_clusters_table(connection: Connection) -> None:
    if _has_table(connection, "behavior_scene_tag_clusters") and _has_column(
        connection,
        "behavior_scene_tag_clusters",
        "tag",
    ):
        _create_behavior_scene_tag_cluster_indexes(connection)
        return
    if _has_table(connection, "behavior_scene_tag_clusters"):
        _rebuild_behavior_scene_tag_clusters_table(connection)
        return
    _create_behavior_scene_tag_clusters_table(connection)


def _create_behavior_scene_tag_clusters_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS behavior_scene_tag_clusters (
            id INTEGER NOT NULL,
            tag_kind VARCHAR(40) NOT NULL,
            tag TEXT NOT NULL,
            cluster_key TEXT NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_behavior_scene_tag_cluster_kind_tag UNIQUE (tag_kind, tag)
        )
        """
    )
    _create_behavior_scene_tag_cluster_indexes(connection)


def _rebuild_behavior_scene_tag_clusters_table(connection: Connection) -> None:
    source_tag_column = (
        "normalized_tag" if _has_column(connection, "behavior_scene_tag_clusters", "normalized_tag") else "tag"
    )
    connection.exec_driver_sql("DROP TABLE IF EXISTS behavior_scene_tag_clusters_v22")
    connection.exec_driver_sql(
        """
        CREATE TABLE behavior_scene_tag_clusters_v22 (
            id INTEGER NOT NULL,
            tag_kind VARCHAR(40) NOT NULL,
            tag TEXT NOT NULL,
            cluster_key TEXT NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_behavior_scene_tag_cluster_kind_tag UNIQUE (tag_kind, tag)
        )
        """
    )
    connection.exec_driver_sql(
        f"""
        INSERT OR IGNORE INTO behavior_scene_tag_clusters_v22 (
            id,
            tag_kind,
            tag,
            cluster_key,
            source_count,
            update_time
        )
        SELECT
            id,
            tag_kind,
            {source_tag_column},
            cluster_key,
            source_count,
            update_time
        FROM behavior_scene_tag_clusters
        WHERE {source_tag_column} IS NOT NULL AND TRIM({source_tag_column}) != ''
        """
    )
    connection.exec_driver_sql("DROP TABLE behavior_scene_tag_clusters")
    connection.exec_driver_sql("ALTER TABLE behavior_scene_tag_clusters_v22 RENAME TO behavior_scene_tag_clusters")
    _create_behavior_scene_tag_cluster_indexes(connection)


def _create_behavior_scene_tag_cluster_indexes(connection: Connection) -> None:
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_tag_clusters_kind_cluster "
        "ON behavior_scene_tag_clusters (tag_kind, cluster_key)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_tag_clusters_tag_kind "
        "ON behavior_scene_tag_clusters (tag_kind)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_tag_clusters_update_time "
        "ON behavior_scene_tag_clusters (update_time)"
    )


def _create_behavior_scene_node_tags_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS behavior_scene_node_tags (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            scene_node_id INTEGER NOT NULL,
            tag_kind VARCHAR(40) NOT NULL,
            cluster_key TEXT NOT NULL,
            weight FLOAT NOT NULL DEFAULT 1,
            count INTEGER NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_behavior_scene_node_tag_node_kind_cluster
                UNIQUE (scene_node_id, tag_kind, cluster_key)
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_node_tags_session_id "
        "ON behavior_scene_node_tags (session_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_node_tags_scene_node_id "
        "ON behavior_scene_node_tags (scene_node_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_node_tags_tag_kind "
        "ON behavior_scene_node_tags (tag_kind)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_node_tags_update_time "
        "ON behavior_scene_node_tags (update_time)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_node_tags_scope_kind_cluster "
        "ON behavior_scene_node_tags (session_id, tag_kind, cluster_key)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_node_tags_node "
        "ON behavior_scene_node_tags (scene_node_id)"
    )


def _clear_table(connection: Connection, table_name: str) -> int:
    if not _has_table(connection, table_name):
        return 0
    count_row = connection.exec_driver_sql(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    count = int(count_row[0] or 0) if count_row is not None else 0
    connection.exec_driver_sql(f"DELETE FROM {table_name}")
    return count


def _existing_tables(connection: Connection, table_names: tuple[str, ...]) -> list[str]:
    rows = connection.exec_driver_sql(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name IN ({placeholders})
        ORDER BY name
        """.format(placeholders=", ".join("?" for _ in table_names)),
        table_names,
    ).all()
    return [str(row[0]) for row in rows]


def _drop_table(connection: Connection, table_name: str) -> None:
    escaped_table_name = table_name.replace('"', '""')
    connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{escaped_table_name}"')


def _has_table(connection: Connection, table_name: str) -> bool:
    exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return exists is not None


def _has_column(connection: Connection, table_name: str, column_name: str) -> bool:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in rows)
