"""v12 schema 升级到 v13：为黑话记录增加创建来源字段。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")


def migrate_v12_to_v13(context: MigrationExecutionContext) -> None:
    """为 ``jargons`` 增加 ``created_by`` 字段，历史数据默认视为 AI 学习来源。"""

    context.start_progress(
        total_tables=1,
        total_records=0,
        description="v12 -> v13 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _add_jargon_created_by_column(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="jargons")

    logger.info("v12 -> v13 数据库迁移完成：jargons 已添加 created_by 字段")


def _add_jargon_created_by_column(connection: Connection) -> None:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "jargons"):
        return

    table_schema = schema_inspector.get_table_schema(connection, "jargons")
    if table_schema.has_column("created_by"):
        return

    connection.exec_driver_sql(
        "ALTER TABLE jargons ADD COLUMN created_by VARCHAR(6) NOT NULL DEFAULT 'AI'"
    )
    connection.exec_driver_sql("UPDATE jargons SET created_by = 'AI' WHERE created_by IS NULL OR created_by = ''")
