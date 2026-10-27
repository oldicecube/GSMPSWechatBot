"""v20 到 v21 schema 迁移：为行为路径增加主体与学习类型。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext

logger = get_logger("database_migration")


def migrate_v20_to_v21(context: MigrationExecutionContext) -> None:
    """重建行为路径表，让同一场景下的他人观察与自身反馈路径可并存。"""

    context.start_progress(
        total_tables=1,
        total_records=0,
        description="v20 -> v21 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _rebuild_behavior_experience_paths_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_experience_paths")

    logger.info("v20 -> v21 数据库迁移完成：行为经验路径已区分 actor_type 与 learning_type")


def _rebuild_behavior_experience_paths_table(connection: Connection) -> None:
    connection.exec_driver_sql("DROP TABLE IF EXISTS behavior_experience_paths_v21")
    connection.exec_driver_sql(
        """
        CREATE TABLE behavior_experience_paths_v21 (
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
                UNIQUE (
                    session_id,
                    scene_cluster_id,
                    action_node_id,
                    outcome_node_id,
                    actor_type,
                    learning_type
                )
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT OR IGNORE INTO behavior_experience_paths_v21 (
            id,
            session_id,
            scene_cluster_id,
            action_node_id,
            outcome_node_id,
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
            id,
            session_id,
            scene_cluster_id,
            action_node_id,
            outcome_node_id,
            'other_user',
            'observed_behavior',
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
        FROM behavior_experience_paths
        """
    )
    connection.exec_driver_sql("DROP TABLE behavior_experience_paths")
    connection.exec_driver_sql("ALTER TABLE behavior_experience_paths_v21 RENAME TO behavior_experience_paths")
    _create_behavior_experience_path_indexes(connection)


def _create_behavior_experience_path_indexes(connection: Connection) -> None:
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
