"""v11 schema 升级到 v12：移除黑话推理过程缓存字段。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")

_REMOVED_JARGON_COLUMNS = ("inference_with_context", "inference_with_content_only")
_JARGON_COLUMNS_TO_KEEP = (
    "id",
    "content",
    "raw_content",
    "meaning",
    "session_id_dict",
    "count",
    "is_jargon",
    "is_complete",
    "is_global",
    "last_inference_count",
    "created_timestamp",
    "updated_timestamp",
)
_JARGON_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_jargons_content ON jargons (content)",
    "CREATE INDEX IF NOT EXISTS ix_jargons_created_timestamp ON jargons (created_timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_jargons_updated_timestamp ON jargons (updated_timestamp)",
)


def migrate_v11_to_v12(context: MigrationExecutionContext) -> None:
    """移除 ``jargons`` 中不再持久化的推理结果字段。"""

    context.start_progress(
        total_tables=1,
        total_records=0,
        description="v11 -> v12 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _drop_jargon_inference_columns(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="jargons")

    logger.info("v11 -> v12 数据库迁移完成：jargons 已移除推理过程缓存字段")


def _drop_jargon_inference_columns(connection: Connection) -> None:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "jargons"):
        return

    table_schema = schema_inspector.get_table_schema(connection, "jargons")
    if not any(table_schema.has_column(column_name) for column_name in _REMOVED_JARGON_COLUMNS):
        return

    keep_columns_sql = ", ".join(_JARGON_COLUMNS_TO_KEEP)
    connection.exec_driver_sql("DROP TABLE IF EXISTS jargons_v12")
    connection.exec_driver_sql(
        """
        CREATE TABLE jargons_v12 (
            id INTEGER NOT NULL,
            content VARCHAR(255) NOT NULL,
            raw_content TEXT,
            meaning TEXT NOT NULL,
            session_id_dict TEXT NOT NULL,
            count INTEGER NOT NULL,
            is_jargon BOOLEAN,
            is_complete BOOLEAN NOT NULL,
            is_global BOOLEAN NOT NULL,
            last_inference_count INTEGER NOT NULL,
            created_timestamp DATETIME,
            updated_timestamp DATETIME,
            PRIMARY KEY (id)
        )
        """
    )
    connection.exec_driver_sql(
        f"""
        INSERT INTO jargons_v12 ({keep_columns_sql})
        SELECT {keep_columns_sql}
        FROM jargons
        """
    )
    connection.exec_driver_sql("DROP TABLE jargons")
    connection.exec_driver_sql("ALTER TABLE jargons_v12 RENAME TO jargons")

    for statement in _JARGON_INDEXES:
        connection.exec_driver_sql(statement)
