"""v18 到 v19 schema 迁移：一次性落地最终行为经验路径图谱。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext

logger = get_logger("database_migration")


def migrate_v18_to_v19(context: MigrationExecutionContext) -> None:
    """移除旧行为表现表并创建最终节点化行为经验路径结构。

    本迁移只保证 schema 能升级到最终结构，不迁移旧 ``behavior_patterns`` 中的学习数据。
    """

    context.start_progress(
        total_tables=10,
        total_records=0,
        description="v18 -> v19 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    context.connection.exec_driver_sql('DROP TABLE IF EXISTS "command_records"')
    context.advance_progress(records=0, completed_tables=1, item_name="command_records")

    _drop_legacy_behavior_tables(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_patterns")

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

    logger.info("v18 -> v19 数据库迁移完成：行为表现已改为行为经验路径图谱")


def _drop_legacy_behavior_tables(connection: Connection) -> None:
    """删除旧行为表现表和未发布中间图谱表，确保最终结构干净。"""

    table_names = (
        "behavior_pattern_scene_links",
        "behavior_scene_action_edges",
        "behavior_action_outcome_edges",
        "behavior_scene_edges",
        "behavior_scene_nodes",
        "behavior_action_nodes",
        "behavior_outcome_nodes",
        "behavior_experience_scene_links",
        "behavior_experience_paths",
        "behavior_patterns",
    )
    for table_name in table_names:
        connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}"')


def _create_behavior_scene_nodes_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS behavior_scene_nodes (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            node_kind VARCHAR(40) NOT NULL DEFAULT 'scene',
            name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 0,
            score FLOAT NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_behavior_scene_node_scope_kind_name
                UNIQUE (session_id, node_kind, normalized_name)
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_nodes_session_kind "
        "ON behavior_scene_nodes (session_id, node_kind)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_nodes_updated_at ON behavior_scene_nodes (update_time)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_nodes_session_id ON behavior_scene_nodes (session_id)"
    )


def _create_behavior_scene_edges_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS behavior_scene_edges (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            source_scene_id INTEGER NOT NULL,
            target_scene_id INTEGER NOT NULL,
            edge_type VARCHAR(40) NOT NULL DEFAULT 'co_occurs',
            weight FLOAT NOT NULL DEFAULT 1,
            count INTEGER NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_behavior_scene_edge_scope_source_target_type
                UNIQUE (session_id, source_scene_id, target_scene_id, edge_type)
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_edges_session_type "
        "ON behavior_scene_edges (session_id, edge_type)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_edges_source ON behavior_scene_edges (source_scene_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_edges_target ON behavior_scene_edges (target_scene_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_edges_session_id ON behavior_scene_edges (session_id)"
    )


def _create_behavior_action_nodes_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS behavior_action_nodes (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            action TEXT NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 0,
            score FLOAT NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_behavior_action_node_scope_action
                UNIQUE (session_id, action)
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_action_nodes_session_id ON behavior_action_nodes (session_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_action_nodes_updated_at ON behavior_action_nodes (update_time)"
    )


def _create_behavior_outcome_nodes_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS behavior_outcome_nodes (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            outcome TEXT NOT NULL,
            source_count INTEGER NOT NULL DEFAULT 0,
            score FLOAT NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_behavior_outcome_node_scope_outcome
                UNIQUE (session_id, outcome)
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_outcome_nodes_session_id ON behavior_outcome_nodes (session_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_outcome_nodes_updated_at ON behavior_outcome_nodes (update_time)"
    )


def _create_behavior_experience_paths_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS behavior_experience_paths (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            start_scene_node_id INTEGER NOT NULL,
            action_node_id INTEGER NOT NULL,
            outcome_node_id INTEGER NOT NULL,
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
            CONSTRAINT uq_behavior_experience_path_scope_scene_action_outcome
                UNIQUE (session_id, start_scene_node_id, action_node_id, outcome_node_id)
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_session_enabled "
        "ON behavior_experience_paths (session_id, enabled)"
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
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_start_scene_node_id "
        "ON behavior_experience_paths (start_scene_node_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_paths_updated_at "
        "ON behavior_experience_paths (update_time)"
    )


def _create_behavior_experience_scene_links_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS behavior_experience_scene_links (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            behavior_experience_path_id INTEGER NOT NULL,
            scene_node_id INTEGER NOT NULL,
            link_role VARCHAR(40) NOT NULL DEFAULT 'start',
            weight FLOAT NOT NULL DEFAULT 1,
            count INTEGER NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_behavior_experience_scene_link_path_node_role
                UNIQUE (behavior_experience_path_id, scene_node_id, link_role)
        )
        """
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_scene_links_session_role "
        "ON behavior_experience_scene_links (session_id, link_role)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_scene_links_node "
        "ON behavior_experience_scene_links (scene_node_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_scene_links_path "
        "ON behavior_experience_scene_links (behavior_experience_path_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_experience_scene_links_session_id "
        "ON behavior_experience_scene_links (session_id)"
    )


def _create_behavior_scene_action_edges_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS behavior_scene_action_edges (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            scene_node_id INTEGER NOT NULL,
            action_node_id INTEGER NOT NULL,
            behavior_experience_path_id INTEGER NOT NULL,
            weight FLOAT NOT NULL DEFAULT 1,
            count INTEGER NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_behavior_scene_action_edge_scope_scene_action_path
                UNIQUE (session_id, scene_node_id, action_node_id, behavior_experience_path_id)
        )
        """
    )
    _create_behavior_scene_action_edges_indexes(connection)


def _create_behavior_scene_action_edges_indexes(connection: Connection) -> None:
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_action_edges_scene "
        "ON behavior_scene_action_edges (scene_node_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_action_edges_action "
        "ON behavior_scene_action_edges (action_node_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_action_edges_path "
        "ON behavior_scene_action_edges (behavior_experience_path_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_scene_action_edges_session_id "
        "ON behavior_scene_action_edges (session_id)"
    )


def _create_behavior_action_outcome_edges_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS behavior_action_outcome_edges (
            id INTEGER NOT NULL,
            session_id VARCHAR(255),
            action_node_id INTEGER NOT NULL,
            outcome_node_id INTEGER NOT NULL,
            behavior_experience_path_id INTEGER NOT NULL,
            weight FLOAT NOT NULL DEFAULT 1,
            count INTEGER NOT NULL DEFAULT 0,
            update_time DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_behavior_action_outcome_edge_scope_action_outcome_path
                UNIQUE (session_id, action_node_id, outcome_node_id, behavior_experience_path_id)
        )
        """
    )
    _create_behavior_action_outcome_edges_indexes(connection)


def _create_behavior_action_outcome_edges_indexes(connection: Connection) -> None:
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_action_outcome_edges_action "
        "ON behavior_action_outcome_edges (action_node_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_action_outcome_edges_outcome "
        "ON behavior_action_outcome_edges (outcome_node_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_action_outcome_edges_path "
        "ON behavior_action_outcome_edges (behavior_experience_path_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_action_outcome_edges_session_id "
        "ON behavior_action_outcome_edges (session_id)"
    )
