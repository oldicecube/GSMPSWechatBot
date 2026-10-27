"""v14 schema 升级到 v15：记录 LLM prompt cache token 统计。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")

_LLM_USAGE_CACHE_COLUMNS = (
    ("prompt_cache_hit_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ("prompt_cache_miss_tokens", "INTEGER NOT NULL DEFAULT 0"),
)


def migrate_v14_to_v15(context: MigrationExecutionContext) -> None:
    """为 ``llm_usage`` 增加 prompt cache 命中与未命中 token 字段。"""

    context.start_progress(
        total_tables=1,
        total_records=0,
        description="v14 -> v15 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _add_llm_usage_cache_columns(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="llm_usage")

    logger.info("v14 -> v15 数据库迁移完成：llm_usage 已添加 prompt cache token 统计字段")


def _add_llm_usage_cache_columns(connection: Connection) -> None:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "llm_usage"):
        return

    table_schema = schema_inspector.get_table_schema(connection, "llm_usage")
    for column_name, column_sql in _LLM_USAGE_CACHE_COLUMNS:
        if table_schema.has_column(column_name):
            continue
        connection.exec_driver_sql(f"ALTER TABLE llm_usage ADD COLUMN {column_name} {column_sql}")
