"""v22 到 v23 schema 迁移：移除行为场景簇冗余身份字段。"""

from __future__ import annotations

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext

logger = get_logger("database_migration")


def migrate_v22_to_v23(context: MigrationExecutionContext) -> None:
    """重建 behavior_scene_clusters，移除 name 与 normalized_tags。"""

    context.start_progress(
        total_tables=1,
        total_records=0,
        description="v22 -> v23 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )
    migrated_count = _rebuild_behavior_scene_clusters_table(context.connection)
    context.advance_progress(records=migrated_count, completed_tables=1, item_name="behavior_scene_clusters")
    logger.info(f"v22 -> v23 数据库迁移完成：behavior_scene_clusters 已移除冗余字段，共保留 {migrated_count} 条记录")


def _rebuild_behavior_scene_clusters_table(connection: Connection) -> int:
    if not _has_table(connection, "behavior_scene_clusters"):
        _create_behavior_scene_clusters_table(connection)
        return 0
    if not _has_column(connection, "behavior_scene_clusters", "name") and not _has_column(
        connection,
        "behavior_scene_clusters",
        "normalized_tags",
    ):
        _create_behavior_scene_cluster_indexes(connection)
        count_row = connection.exec_driver_sql("SELECT COUNT(*) FROM behavior_scene_clusters").fetchone()
        return int(count_row[0] or 0) if count_row is not None else 0

    connection.exec_driver_sql("DROP TABLE IF EXISTS behavior_scene_clusters_v23")
    _create_behavior_scene_clusters_table(connection, table_name="behavior_scene_clusters_v23")
    connection.exec_driver_sql(
        """
        INSERT INTO behavior_scene_clusters_v23 (
            id,
            session_id,
            tag_distribution,
            source_count,
            score,
            update_time
        )
        SELECT
            id,
            session_id,
            COALESCE(tag_distribution, '[]'),
            COALESCE(source_count, 0),
            COALESCE(score, 0),
            update_time
        FROM behavior_scene_clusters
        """
    )
    count_row = connection.exec_driver_sql("SELECT COUNT(*) FROM behavior_scene_clusters_v23").fetchone()
    migrated_count = int(count_row[0] or 0) if count_row is not None else 0
    connection.exec_driver_sql("DROP TABLE behavior_scene_clusters")
    connection.exec_driver_sql("ALTER TABLE behavior_scene_clusters_v23 RENAME TO behavior_scene_clusters")
    _create_behavior_scene_cluster_indexes(connection)
    return migrated_count


def _create_behavior_scene_clusters_table(
    connection: Connection,
    *,
    table_name: str = "behavior_scene_clusters",
) -> None:
    escaped_table_name = table_name.replace('"', '""')
    connection.exec_driver_sql(
        f"""
        CREATE TABLE IF NOT EXISTS "{escaped_table_name}" (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            tag_distribution TEXT NOT NULL DEFAULT '[]',
            source_count INTEGER NOT NULL DEFAULT 0,
            score FLOAT NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id)
        )
        """
    )


def _create_behavior_scene_cluster_indexes(connection: Connection) -> None:
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_clusters_session_id "
        "ON behavior_scene_clusters (session_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_clusters_update_time "
        "ON behavior_scene_clusters (update_time)"
    )


def _has_table(connection: Connection, table_name: str) -> bool:
    exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return exists is not None


def _has_column(connection: Connection, table_name: str, column_name: str) -> bool:
    rows = connection.exec_driver_sql(f"PRAGMA table_info({table_name})").fetchall()
    return any(row[1] == column_name for row in rows)
