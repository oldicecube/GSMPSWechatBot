"""v13 schema 升级到 v14：补充遥测统计所需字段。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")


def migrate_v13_to_v14(context: MigrationExecutionContext) -> None:
    """为消息与模型调用记录增加遥测聚合所需的轻量字段。"""

    context.start_progress(
        total_tables=2,
        total_records=0,
        description="v13 -> v14 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _add_message_reply_frequency_column(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="mai_messages")

    _add_llm_usage_task_name_column(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="llm_usage")

    logger.info("v13 -> v14 数据库迁移完成：已增加消息回复频率与 LLM 任务名称字段")


def _add_message_reply_frequency_column(connection: Connection) -> None:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "mai_messages"):
        return

    table_schema = schema_inspector.get_table_schema(connection, "mai_messages")
    if table_schema.has_column("reply_frequency"):
        return

    connection.exec_driver_sql("ALTER TABLE mai_messages ADD COLUMN reply_frequency FLOAT")


def _add_llm_usage_task_name_column(connection: Connection) -> None:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "llm_usage"):
        return

    table_schema = schema_inspector.get_table_schema(connection, "llm_usage")
    if table_schema.has_column("task_name"):
        return

    connection.exec_driver_sql("ALTER TABLE llm_usage ADD COLUMN task_name VARCHAR(100)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_llm_usage_task_name ON llm_usage (task_name)")
