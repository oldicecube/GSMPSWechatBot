"""v28 schema 升级到 v29：按聊天流重建高频词表。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")


def migrate_v28_to_v29(context: MigrationExecutionContext) -> None:
    """将 ``high_frequency_terms`` 重建为按 ``chat_id`` 分类的新结构。"""

    existing_records = _count_existing_high_frequency_terms(context.connection)
    context.start_progress(
        total_tables=1,
        total_records=existing_records,
        description="v28 -> v29 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _replace_high_frequency_terms_with_empty_v29_table(context.connection)
    context.advance_progress(records=existing_records, completed_tables=1, item_name="high_frequency_terms")

    if existing_records > 0:
        logger.info(
            "v28 -> v29 已重建 high_frequency_terms："
            f"旧全局词频 {existing_records} 条无法可靠拆分到 chat_id，已丢弃并等待后续重新统计"
        )
    logger.info("v28 -> v29 数据库迁移完成：high_frequency_terms 已按 chat_id 分类")


def _count_existing_high_frequency_terms(connection: Connection) -> int:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "high_frequency_terms"):
        return 0
    row = connection.exec_driver_sql("SELECT COUNT(*) FROM high_frequency_terms").fetchone()
    return int(row[0] or 0) if row is not None else 0


def _replace_high_frequency_terms_with_empty_v29_table(connection: Connection) -> None:
    connection.exec_driver_sql("DROP TABLE IF EXISTS high_frequency_terms")
    _create_empty_high_frequency_terms_table(connection)
    _create_high_frequency_terms_indexes(connection)


def _create_empty_high_frequency_terms_table(connection: Connection) -> None:
    connection.exec_driver_sql(
        """
        CREATE TABLE high_frequency_terms (
            id INTEGER NOT NULL,
            chat_id VARCHAR(255) NOT NULL,
            term TEXT NOT NULL,
            rank INTEGER NOT NULL DEFAULT 0,
            occurrence_count INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            frequency FLOAT NOT NULL DEFAULT 0,
            message_frequency FLOAT NOT NULL DEFAULT 0,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (id),
            CONSTRAINT uq_high_frequency_terms_chat_term UNIQUE (chat_id, term)
        )
        """
    )


def _create_high_frequency_terms_indexes(connection: Connection) -> None:
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_high_frequency_terms_chat_id ON high_frequency_terms (chat_id)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_high_frequency_terms_chat_rank ON high_frequency_terms (chat_id, rank)"
    )
    connection.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS ix_high_frequency_terms_updated_at ON high_frequency_terms (updated_at)"
    )
