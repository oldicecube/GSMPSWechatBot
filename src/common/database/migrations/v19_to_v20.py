"""v19 到 v20 schema 迁移：重建行为学习为独立场景簇结构。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .v18_to_v19 import (
    _create_behavior_action_nodes_table,
    _create_behavior_action_outcome_edges_table,
    _create_behavior_experience_scene_links_table,
    _create_behavior_outcome_nodes_table,
    _create_behavior_scene_action_edges_table,
    _create_behavior_scene_edges_table,
    _create_behavior_scene_nodes_table,
)

logger = get_logger("database_migration")


def migrate_v19_to_v20(context: MigrationExecutionContext) -> None:
    """删除测试期旧行为数据，并创建独立场景簇版本的行为经验路径结构。"""

    context.start_progress(
        total_tables=10,
        total_records=0,
        description="v19 -> v20 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _drop_behavior_graph_tables(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_graph_tables")

    _create_behavior_scene_clusters_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_scene_clusters")

    _create_behavior_scene_nodes_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_scene_nodes")

    _create_behavior_scene_edges_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_scene_edges")

    _create_behavior_action_nodes_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_action_nodes")

    _create_behavior_outcome_nodes_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_outcome_nodes")

    _create_behavior_experience_paths_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_experience_paths")

    _create_behavior_experience_scene_links_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_experience_scene_links")

    _create_behavior_scene_action_edges_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_scene_action_edges")

    _create_behavior_action_outcome_edges_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_action_outcome_edges")

    logger.info("v19 -> v20 数据库迁移完成：行为表现已改为独立场景簇概率分布结构")


def _drop_behavior_graph_tables(connection: Connection) -> None:
    table_names = (
        "behavior_scene_action_edges",
        "behavior_action_outcome_edges",
        "behavior_scene_edges",
        "behavior_experience_scene_links",
        "behavior_experience_paths",
        "behavior_scene_clusters",
        "behavior_scene_nodes",
        "behavior_action_nodes",
        "behavior_outcome_nodes",
    )
    for table_name in table_names:
        connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}"')


def _create_behavior_scene_clusters_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS behavior_scene_clusters (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            name TEXT NOT NULL,
            normalized_tags TEXT NOT NULL,
            tag_distribution TEXT NOT NULL DEFAULT '[]',
            source_count INTEGER NOT NULL DEFAULT 0,
            score FLOAT NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_behavior_scene_cluster_scope_tags
                UNIQUE (session_id, normalized_tags)
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_clusters_session_id "
        "ON behavior_scene_clusters (session_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_clusters_updated_at "
        "ON behavior_scene_clusters (update_time)"
    )


def _create_behavior_experience_paths_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS behavior_experience_paths (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            scene_cluster_id INTEGER NOT NULL,
            action_node_id INTEGER NOT NULL,
            outcome_node_id INTEGER NOT NULL,
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
            CONSTRAINT uq_behavior_experience_path_scope_cluster_action_outcome_actor
                UNIQUE (session_id, scene_cluster_id, action_node_id, outcome_node_id, actor_type, learning_type)
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_session_enabled "
        "ON behavior_experience_paths (session_id, enabled)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_cluster "
        "ON behavior_experience_paths (scene_cluster_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_learning_type "
        "ON behavior_experience_paths (learning_type)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_actor_type "
        "ON behavior_experience_paths (actor_type)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_action "
        "ON behavior_experience_paths (action_node_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_outcome "
        "ON behavior_experience_paths (outcome_node_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_session_id "
        "ON behavior_experience_paths (session_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_updated_at "
        "ON behavior_experience_paths (update_time)"
    )
