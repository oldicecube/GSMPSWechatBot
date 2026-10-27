"""v16 schema 升级到 v17：新增行为表现模式表。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")


def migrate_v16_to_v17(context: MigrationExecutionContext) -> None:
    """新增 ``behavior_patterns`` 表，用于存储可复用的起因-行为-结果模式。"""

    context.start_progress(
        total_tables=1,
        total_records=0,
        description="v16 -> v17 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _create_behavior_patterns_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="behavior_patterns")

    logger.info("v16 -> v17 数据库迁移完成：behavior_patterns 表已就绪")


def _create_behavior_patterns_table(connection: Connection) -> None:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "behavior_patterns"):
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS behavior_patterns (
                id INTEGER NOT NULL,
                trigger TEXT NOT NULL,
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                evidence_list TEXT NOT NULL DEFAULT '[]',
                feedback_list TEXT NOT NULL DEFAULT '[]',
                count INTEGER NOT NULL DEFAULT 0,
                activation_count INTEGER NOT NULL DEFAULT 0,
                success_count INTEGER NOT NULL DEFAULT 0,
                failure_count INTEGER NOT NULL DEFAULT 0,
                score FLOAT NOT NULL DEFAULT 0,
                enabled BOOLEAN NOT NULL DEFAULT 1,
                last_active_time DATETIME,
                last_feedback_time DATETIME,
                create_time DATETIME,
                update_time DATETIME,
                session_id VARCHAR(255),
                PRIMARY KEY (id)
            )
            """
        )

    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_patterns_session_id ON behavior_patterns (session_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_patterns_last_active_time ON behavior_patterns (last_active_time)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_behavior_patterns_update_time ON behavior_patterns (update_time)"
    )
