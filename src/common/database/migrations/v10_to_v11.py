"""v10 schema 升级到 v11：为黑话记录补充时间字段。"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")

_JARGON_TIMESTAMP_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_jargons_created_timestamp ON jargons (created_timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_jargons_updated_timestamp ON jargons (updated_timestamp)",
)


def migrate_v10_to_v11(context: MigrationExecutionContext) -> None:
    """为 ``jargons`` 增加创建时间和更新时间字段。"""

    connection = context.connection
    context.start_progress(
        total_tables=1,
        total_records=0,
        description="v10 -> v11 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _add_jargon_timestamp_columns(connection)
    context.advance_progress(records=0, completed_tables=1, item_name="jargons")

    logger.info("v10 -> v11 数据库迁移完成：jargons 已添加时间字段")


def _add_jargon_timestamp_columns(connection: Connection) -> None:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "jargons"):
        return

    table_schema = schema_inspector.get_table_schema(connection, "jargons")
    if not table_schema.has_column("created_timestamp"):
        connection.execute(text("ALTER TABLE jargons ADD COLUMN created_timestamp DATETIME"))
    if not table_schema.has_column("updated_timestamp"):
        connection.execute(text("ALTER TABLE jargons ADD COLUMN updated_timestamp DATETIME"))

    connection.execute(
        text(
            """
            UPDATE jargons
            SET
                created_timestamp = COALESCE(created_timestamp, CURRENT_TIMESTAMP),
                updated_timestamp = COALESCE(updated_timestamp, created_timestamp, CURRENT_TIMESTAMP)
            """
        )
    )

    for statement in _JARGON_TIMESTAMP_INDEXES:
        connection.exec_driver_sql(statement)
