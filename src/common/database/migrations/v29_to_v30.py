"""v29 schema 升级到 v30：调整 LLM 使用记录归属字段。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .exceptions import DatabaseMigrationExecutionError
from .models import MigrationExecutionContext, TableSchema
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")

_LLM_USAGE_TABLE = "llm_usage"
_LLM_USAGE_V29_BACKUP_TABLE = "llm_usage_v29_backup"


def migrate_v29_to_v30(context: MigrationExecutionContext) -> None:
    """重建 ``llm_usage``，移除 endpoint/user_type 并新增 session_id。"""

    _recover_interrupted_llm_usage_rebuild(context.connection)
    existing_records = _count_existing_llm_usage(context.connection)
    context.start_progress(
        total_tables=1,
        total_records=existing_records,
        description="v29 -> v30 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _replace_llm_usage_with_v30_table(context.connection)
    context.advance_progress(records=existing_records, completed_tables=1, item_name="llm_usage")
    logger.info("v29 -> v30 数据库迁移完成：llm_usage 已改为记录 session_id 并移除 endpoint/user_type")


def _count_existing_llm_usage(connection: Connection) -> int:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, _LLM_USAGE_TABLE):
        return 0
    row = connection.exec_driver_sql(f"SELECT COUNT(*) FROM {_quote_identifier(_LLM_USAGE_TABLE)}").fetchone()
    return int(row[0] or 0) if row is not None else 0


def _replace_llm_usage_with_v30_table(connection: Connection) -> None:
    schema_inspector = SQLiteSchemaInspector()
    _recover_interrupted_llm_usage_rebuild(connection, schema_inspector=schema_inspector)
    if not schema_inspector.table_exists(connection, _LLM_USAGE_TABLE):
        _create_llm_usage_v30_table(connection, _LLM_USAGE_TABLE)
        _create_llm_usage_v30_indexes(connection)
        return

    table_schema = schema_inspector.get_table_schema(connection, _LLM_USAGE_TABLE)
    if _is_llm_usage_v30_schema(table_schema):
        _create_llm_usage_v30_indexes(connection)
        return

    connection.exec_driver_sql(
        f"ALTER TABLE {_quote_identifier(_LLM_USAGE_TABLE)} RENAME TO {_quote_identifier(_LLM_USAGE_V29_BACKUP_TABLE)}"
    )
    _create_llm_usage_v30_table(connection, _LLM_USAGE_TABLE)
    connection.exec_driver_sql(
        f"""
        INSERT INTO llm_usage (
            id,
            model_name,
            model_assign_name,
            model_api_provider_name,
            session_id,
            task_name,
            request_type,
            time_cost,
            timestamp,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            prompt_cache_enabled,
            prompt_cache_hit_tokens,
            prompt_cache_miss_tokens,
            cost
        )
        SELECT
            id,
            model_name,
            model_assign_name,
            model_api_provider_name,
            '',
            task_name,
            request_type,
            COALESCE(time_cost, 0),
            timestamp,
            COALESCE(prompt_tokens, 0),
            COALESCE(completion_tokens, 0),
            COALESCE(total_tokens, 0),
            COALESCE(prompt_cache_enabled, 0),
            COALESCE(prompt_cache_hit_tokens, 0),
            COALESCE(prompt_cache_miss_tokens, 0),
            COALESCE(cost, 0)
        FROM {_quote_identifier(_LLM_USAGE_V29_BACKUP_TABLE)}
        """
    )
    connection.exec_driver_sql(f"DROP TABLE {_quote_identifier(_LLM_USAGE_V29_BACKUP_TABLE)}")
    _create_llm_usage_v30_indexes(connection)


def _recover_interrupted_llm_usage_rebuild(
    connection: Connection,
    schema_inspector: SQLiteSchemaInspector | None = None,
) -> None:
    """恢复上次中断后留下的 ``llm_usage`` 重建现场。"""

    schema_inspector = schema_inspector or SQLiteSchemaInspector()
    has_current_table = schema_inspector.table_exists(connection, _LLM_USAGE_TABLE)
    has_backup_table = schema_inspector.table_exists(connection, _LLM_USAGE_V29_BACKUP_TABLE)
    if not has_backup_table:
        return

    if not has_current_table:
        logger.warning(
            f"检测到 {_LLM_USAGE_TABLE} 不存在但 {_LLM_USAGE_V29_BACKUP_TABLE} 存在，"
            "将恢复备份表后重新执行 v29 -> v30 迁移。"
        )
        connection.exec_driver_sql(
            f"ALTER TABLE {_quote_identifier(_LLM_USAGE_V29_BACKUP_TABLE)} RENAME TO "
            f"{_quote_identifier(_LLM_USAGE_TABLE)}"
        )
        return

    table_schema = schema_inspector.get_table_schema(connection, _LLM_USAGE_TABLE)
    if _is_llm_usage_v29_schema(table_schema):
        logger.warning(
            f"检测到残留备份表 {_LLM_USAGE_V29_BACKUP_TABLE}，当前 {_LLM_USAGE_TABLE} 仍是 v29 结构，"
            "将删除残留备份表后重新执行迁移。"
        )
        connection.exec_driver_sql(f"DROP TABLE {_quote_identifier(_LLM_USAGE_V29_BACKUP_TABLE)}")
        return

    if _is_llm_usage_v30_schema(table_schema):
        logger.warning(
            f"检测到 {_LLM_USAGE_TABLE} 与 {_LLM_USAGE_V29_BACKUP_TABLE} 同时存在，"
            f"且当前 {_LLM_USAGE_TABLE} 已是 v30 结构，将以备份表作为源数据重新执行迁移。"
        )
        connection.exec_driver_sql(f"DROP TABLE {_quote_identifier(_LLM_USAGE_TABLE)}")
        connection.exec_driver_sql(
            f"ALTER TABLE {_quote_identifier(_LLM_USAGE_V29_BACKUP_TABLE)} RENAME TO "
            f"{_quote_identifier(_LLM_USAGE_TABLE)}"
        )
        return

    raise DatabaseMigrationExecutionError(
        f"检测到 {_LLM_USAGE_TABLE} 与 {_LLM_USAGE_V29_BACKUP_TABLE} 同时存在，"
        f"但当前 {_LLM_USAGE_TABLE} 既不是 v29 结构也不是 v30 结构，无法自动判断数据来源。"
    )


def _is_llm_usage_v29_schema(table_schema: TableSchema) -> bool:
    return table_schema.has_column("endpoint") or table_schema.has_column("user_type")


def _is_llm_usage_v30_schema(table_schema: TableSchema) -> bool:
    return (
        table_schema.has_column("session_id")
        and not table_schema.has_column("endpoint")
        and not table_schema.has_column("user_type")
    )


def _create_llm_usage_v30_table(connection: Connection, table_name: str) -> None:
    connection.exec_driver_sql(
        f"""
        CREATE TABLE {_quote_identifier(table_name)} (
            id INTEGER NOT NULL,
            model_name VARCHAR(255) NOT NULL,
            model_assign_name VARCHAR(255),
            model_api_provider_name VARCHAR(255) NOT NULL,
            session_id VARCHAR(255) NOT NULL DEFAULT '',
            task_name VARCHAR(100),
            request_type VARCHAR(50) NOT NULL,
            time_cost FLOAT NOT NULL DEFAULT 0,
            timestamp DATETIME,
            prompt_tokens INTEGER NOT NULL,
            completion_tokens INTEGER NOT NULL,
            total_tokens INTEGER NOT NULL,
            prompt_cache_enabled BOOLEAN NOT NULL DEFAULT 0,
            prompt_cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
            prompt_cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
            cost FLOAT NOT NULL,
            PRIMARY KEY (id)
        )
        """
    )


def _quote_identifier(identifier: str) -> str:
    escaped_identifier = identifier.replace('"', '""')
    return f'"{escaped_identifier}"'


def _create_llm_usage_v30_indexes(connection: Connection) -> None:
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_llm_usage_model_name ON llm_usage (model_name)")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_llm_usage_model_assign_name ON llm_usage (model_assign_name)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_llm_usage_model_api_provider_name ON llm_usage (model_api_provider_name)"
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_llm_usage_session_id ON llm_usage (session_id)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_llm_usage_task_name ON llm_usage (task_name)")
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_llm_usage_timestamp ON llm_usage (timestamp)")
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_llm_usage_timestamp_request_type ON llm_usage(timestamp, request_type)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_llm_usage_timestamp_model_name ON llm_usage(timestamp, model_name)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_llm_usage_timestamp_model_assign_name "
        "ON llm_usage(timestamp, model_assign_name)"
    )
