"""v8 schema 升级到 v9：新增统计汇总表与统计索引。"""

from sqlalchemy import text

from src.common.logger import get_logger

from .models import MigrationExecutionContext

logger = get_logger("database_migration")


def migrate_v8_to_v9(context: MigrationExecutionContext) -> None:
    """创建统计汇总表，并在数据库侧回填历史统计数据。"""

    connection = context.connection
    context.start_progress(
        total_tables=4,
        total_records=0,
        description="v8 -> v9 统计汇总迁移",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _create_tables(connection)
    context.advance_progress(completed_tables=1, item_name="statistics_tables")

    _create_statistics_indexes(connection)
    context.advance_progress(completed_tables=1, item_name="statistics_indexes")

    _backfill_statistics(connection)
    context.advance_progress(completed_tables=1, item_name="statistics_backfill")

    _initialize_cursors(connection)
    context.advance_progress(completed_tables=1, item_name="statistics_cursors")

    logger.info("v8 -> v9 数据库迁移完成：已创建并回填统计汇总表")


def _create_tables(connection) -> None:
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS statistics_aggregation_cursors (
                source_name VARCHAR(100) PRIMARY KEY,
                last_processed_id INTEGER NOT NULL DEFAULT 0,
                updated_at DATETIME NOT NULL
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS statistics_message_hourly (
                id INTEGER PRIMARY KEY,
                bucket_time DATETIME NOT NULL,
                chat_id VARCHAR(255) NOT NULL,
                chat_name VARCHAR(255) NOT NULL,
                chat_type VARCHAR(20) NOT NULL,
                message_count INTEGER NOT NULL DEFAULT 0,
                latest_timestamp DATETIME NOT NULL,
                CONSTRAINT uq_statistics_message_hourly_bucket_chat UNIQUE (bucket_time, chat_id)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS statistics_tool_hourly (
                id INTEGER PRIMARY KEY,
                bucket_time DATETIME NOT NULL,
                tool_name VARCHAR(255) NOT NULL,
                call_count INTEGER NOT NULL DEFAULT 0,
                CONSTRAINT uq_statistics_tool_hourly_bucket_tool UNIQUE (bucket_time, tool_name)
            )
            """
        )
    )
    connection.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS statistics_model_hourly (
                id INTEGER PRIMARY KEY,
                bucket_time DATETIME NOT NULL,
                request_type VARCHAR(100) NOT NULL,
                module_name VARCHAR(100) NOT NULL,
                provider_name VARCHAR(255) NOT NULL,
                model_name VARCHAR(255) NOT NULL,
                request_count INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cost FLOAT NOT NULL DEFAULT 0,
                time_cost_sum FLOAT NOT NULL DEFAULT 0,
                time_cost_sq_sum FLOAT NOT NULL DEFAULT 0,
                CONSTRAINT uq_statistics_model_hourly_bucket_request_model_provider
                    UNIQUE (bucket_time, request_type, model_name, provider_name)
            )
            """
        )
    )


def _create_statistics_indexes(connection) -> None:
    index_sql = [
        ("mai_messages", "CREATE INDEX IF NOT EXISTS ix_mai_messages_timestamp ON mai_messages(timestamp)"),
        (
            "mai_messages",
            "CREATE INDEX IF NOT EXISTS ix_mai_messages_timestamp_group_id ON mai_messages(timestamp, group_id)",
        ),
        (
            "mai_messages",
            "CREATE INDEX IF NOT EXISTS ix_mai_messages_timestamp_user_id ON mai_messages(timestamp, user_id)",
        ),
        (
            "tool_records",
            "CREATE INDEX IF NOT EXISTS ix_tool_records_timestamp_tool_name ON tool_records(timestamp, tool_name)",
        ),
        (
            "llm_usage",
            "CREATE INDEX IF NOT EXISTS ix_llm_usage_timestamp_request_type ON llm_usage(timestamp, request_type)",
        ),
        ("llm_usage", "CREATE INDEX IF NOT EXISTS ix_llm_usage_timestamp_model_name ON llm_usage(timestamp, model_name)"),
        (
            "llm_usage",
            "CREATE INDEX IF NOT EXISTS ix_llm_usage_timestamp_model_assign_name ON llm_usage(timestamp, model_assign_name)",
        ),
        (
            "statistics_message_hourly",
            "CREATE INDEX IF NOT EXISTS ix_statistics_message_hourly_bucket_time ON statistics_message_hourly(bucket_time)",
        ),
        (
            "statistics_tool_hourly",
            "CREATE INDEX IF NOT EXISTS ix_statistics_tool_hourly_bucket_time ON statistics_tool_hourly(bucket_time)",
        ),
        (
            "statistics_model_hourly",
            "CREATE INDEX IF NOT EXISTS ix_statistics_model_hourly_bucket_time ON statistics_model_hourly(bucket_time)",
        ),
    ]
    for table_name, sql in index_sql:
        if _table_exists(connection, table_name):
            connection.execute(text(sql))


def _backfill_statistics(connection) -> None:
    connection.execute(text("DELETE FROM statistics_message_hourly"))
    connection.execute(text("DELETE FROM statistics_tool_hourly"))
    connection.execute(text("DELETE FROM statistics_model_hourly"))
    if _table_exists(connection, "mai_messages"):
        _backfill_messages(connection)
    if _table_exists(connection, "tool_records"):
        _backfill_tools(connection)
    if _table_exists(connection, "llm_usage"):
        _backfill_model_usage(connection)


def _backfill_messages(connection) -> None:
    connection.execute(
        text(
            """
            INSERT INTO statistics_message_hourly (
                bucket_time, chat_id, chat_name, chat_type, message_count, latest_timestamp
            )
            WITH message_source AS (
                SELECT
                    id,
                    datetime(strftime('%Y-%m-%d %H:00:00', timestamp)) AS bucket_time,
                    CASE
                        WHEN group_id IS NOT NULL AND group_id != '' THEN 'g' || group_id
                        ELSE 'u' || user_id
                    END AS chat_id,
                    COALESCE(
                        CASE WHEN group_id IS NOT NULL AND group_id != '' THEN group_name ELSE user_nickname END,
                        CASE
                            WHEN group_id IS NOT NULL AND group_id != '' THEN '群' || group_id
                            ELSE '用户' || user_id
                        END
                    ) AS chat_name,
                    CASE WHEN group_id IS NOT NULL AND group_id != '' THEN 'group' ELSE 'private' END AS chat_type,
                    timestamp
                FROM mai_messages
                WHERE timestamp IS NOT NULL
                  AND (
                      (group_id IS NOT NULL AND group_id != '')
                      OR (user_id IS NOT NULL AND user_id != '')
                  )
            ),
            message_aggregate AS (
                SELECT
                    bucket_time,
                    chat_id,
                    COUNT(*) AS message_count,
                    MAX(timestamp) AS latest_timestamp
                FROM message_source
                GROUP BY bucket_time, chat_id
            ),
            latest_message AS (
                SELECT
                    bucket_time,
                    chat_id,
                    chat_name,
                    chat_type,
                    ROW_NUMBER() OVER (
                        PARTITION BY bucket_time, chat_id
                        ORDER BY timestamp DESC, id DESC
                    ) AS row_num
                FROM message_source
            )
            SELECT
                message_aggregate.bucket_time,
                message_aggregate.chat_id,
                latest_message.chat_name,
                latest_message.chat_type,
                message_aggregate.message_count,
                message_aggregate.latest_timestamp
            FROM message_aggregate
            JOIN latest_message
              ON latest_message.bucket_time = message_aggregate.bucket_time
             AND latest_message.chat_id = message_aggregate.chat_id
             AND latest_message.row_num = 1
            """
        )
    )


def _backfill_tools(connection) -> None:
    connection.execute(
        text(
            """
            INSERT INTO statistics_tool_hourly (bucket_time, tool_name, call_count)
            SELECT
                datetime(strftime('%Y-%m-%d %H:00:00', timestamp)) AS bucket_time,
                COALESCE(tool_name, 'unknown') AS tool_name,
                COUNT(*) AS call_count
            FROM tool_records
            WHERE timestamp IS NOT NULL
            GROUP BY bucket_time, COALESCE(tool_name, 'unknown')
            """
        )
    )


def _backfill_model_usage(connection) -> None:
    connection.execute(
        text(
            """
            INSERT INTO statistics_model_hourly (
                bucket_time, request_type, module_name, provider_name, model_name,
                request_count, prompt_tokens, completion_tokens, total_tokens, cost,
                time_cost_sum, time_cost_sq_sum
            )
            SELECT
                datetime(strftime('%Y-%m-%d %H:00:00', timestamp)) AS bucket_time,
                COALESCE(request_type, 'unknown') AS request_type,
                CASE
                    WHEN instr(COALESCE(request_type, 'unknown'), '.') > 0
                    THEN substr(COALESCE(request_type, 'unknown'), 1, instr(COALESCE(request_type, 'unknown'), '.') - 1)
                    ELSE COALESCE(request_type, 'unknown')
                END AS module_name,
                COALESCE(model_api_provider_name, 'unknown') AS provider_name,
                COALESCE(model_assign_name, model_name, 'unknown') AS model_name,
                COUNT(*) AS request_count,
                COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) AS completion_tokens,
                COALESCE(SUM(total_tokens), 0) AS total_tokens,
                COALESCE(SUM(cost), 0) AS cost,
                COALESCE(SUM(time_cost), 0) AS time_cost_sum,
                COALESCE(SUM(time_cost * time_cost), 0) AS time_cost_sq_sum
            FROM llm_usage
            WHERE timestamp IS NOT NULL
            GROUP BY
                datetime(strftime('%Y-%m-%d %H:00:00', timestamp)),
                COALESCE(request_type, 'unknown'),
                COALESCE(model_api_provider_name, 'unknown'),
                COALESCE(model_assign_name, model_name, 'unknown')
            """
        )
    )


def _initialize_cursors(connection) -> None:
    for source_name, table_name in [
        ("messages", "mai_messages"),
        ("tool_records", "tool_records"),
        ("model_usage", "llm_usage"),
    ]:
        if _table_exists(connection, table_name):
            row = connection.execute(text(f"SELECT COALESCE(MAX(id), 0) FROM {table_name}")).first()
            max_id = int(row[0]) if row else 0
        else:
            max_id = 0
        connection.execute(
            text(
                """
                INSERT INTO statistics_aggregation_cursors (source_name, last_processed_id, updated_at)
                VALUES (:source_name, :last_processed_id, CURRENT_TIMESTAMP)
                ON CONFLICT(source_name) DO UPDATE SET
                    last_processed_id = excluded.last_processed_id,
                    updated_at = excluded.updated_at
                """
            ),
            {"source_name": source_name, "last_processed_id": max_id},
        )


def _table_exists(connection, table_name: str) -> bool:
    row = connection.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:table_name"),
        {"table_name": table_name},
    ).first()
    return row is not None
