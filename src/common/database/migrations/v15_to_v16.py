"""v15 schema 升级到 v16：记录 LLM 请求是否启用 prompt cache 计费。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")


def migrate_v15_to_v16(context: MigrationExecutionContext) -> None:
    """为 ``llm_usage`` 增加当次请求是否启用 prompt cache 计费的字段。"""

    context.start_progress(
        total_tables=1,
        total_records=0,
        description="v15 -> v16 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _add_llm_usage_cache_enabled_column(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="llm_usage")

    logger.info("v15 -> v16 数据库迁移完成：llm_usage 已添加 prompt_cache_enabled 字段")


def _add_llm_usage_cache_enabled_column(connection: Connection) -> None:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "llm_usage"):
        return

    table_schema = schema_inspector.get_table_schema(connection, "llm_usage")
    if table_schema.has_column("prompt_cache_enabled"):
        return

    connection.exec_driver_sql("ALTER TABLE llm_usage ADD COLUMN prompt_cache_enabled BOOLEAN NOT NULL DEFAULT 0")
