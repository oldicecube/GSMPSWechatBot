"""v32 schema 升级到 v33：修复黑话记录中的空时间字段。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")


def migrate_v32_to_v33(context: MigrationExecutionContext) -> None:
    """修复 ``jargons`` 中无法被 DateTime 解析的空字符串时间。"""

    bad_timestamp_rows = _count_jargon_empty_timestamp_rows(context.connection)
    context.start_progress(
        total_tables=1,
        total_records=bad_timestamp_rows,
        description="v32 -> v33 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    updated_rows = cleanup_jargon_empty_timestamps(context.connection)
    context.advance_progress(records=updated_rows, completed_tables=1, item_name="jargons")

    logger.info(f"v32 -> v33 数据库迁移完成：修复黑话空时间字段 {updated_rows} 条")


def cleanup_jargon_empty_timestamps(connection: Connection) -> int:
    """将黑话记录中的空时间字段修正为 SQLite 可解析的时间文本。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "jargons"):
        return 0

    table_schema = schema_inspector.get_table_schema(connection, "jargons")
    if not table_schema.has_column("created_timestamp") or not table_schema.has_column("updated_timestamp"):
        return 0

    cursor = connection.exec_driver_sql(
        """
        UPDATE jargons
        SET
            created_timestamp = CASE
                WHEN created_timestamp IS NULL OR TRIM(CAST(created_timestamp AS TEXT)) = ''
                    THEN COALESCE(NULLIF(TRIM(CAST(updated_timestamp AS TEXT)), ''), CURRENT_TIMESTAMP)
                ELSE created_timestamp
            END,
            updated_timestamp = CASE
                WHEN updated_timestamp IS NULL OR TRIM(CAST(updated_timestamp AS TEXT)) = ''
                    THEN COALESCE(NULLIF(TRIM(CAST(created_timestamp AS TEXT)), ''), CURRENT_TIMESTAMP)
                ELSE updated_timestamp
            END
        WHERE created_timestamp IS NULL
           OR updated_timestamp IS NULL
           OR TRIM(CAST(created_timestamp AS TEXT)) = ''
           OR TRIM(CAST(updated_timestamp AS TEXT)) = ''
        """
    )
    return int(cursor.rowcount or 0)


def _count_jargon_empty_timestamp_rows(connection: Connection) -> int:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "jargons"):
        return 0

    table_schema = schema_inspector.get_table_schema(connection, "jargons")
    if not table_schema.has_column("created_timestamp") or not table_schema.has_column("updated_timestamp"):
        return 0

    row = connection.exec_driver_sql(
        """
        SELECT COUNT(*)
        FROM jargons
        WHERE created_timestamp IS NULL
           OR updated_timestamp IS NULL
           OR TRIM(CAST(created_timestamp AS TEXT)) = ''
           OR TRIM(CAST(updated_timestamp AS TEXT)) = ''
        """
    ).fetchone()
    return int(row[0] or 0) if row is not None else 0
