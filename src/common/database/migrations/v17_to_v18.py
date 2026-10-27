"""v17 schema 升级到 v18：新增高频词/词组词库表。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")


def migrate_v17_to_v18(context: MigrationExecutionContext) -> None:
    """新增 ``high_frequency_terms`` 表，用于记录当前高频词/词组词库。"""

    context.start_progress(
        total_tables=1,
        total_records=0,
        description="v17 -> v18 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _create_high_frequency_terms_table(context.connection)
    context.advance_progress(records=0, completed_tables=1, item_name="high_frequency_terms")

    logger.info("v17 -> v18 数据库迁移完成：high_frequency_terms 表已就绪")


def _create_high_frequency_terms_table(connection: Connection) -> None:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "high_frequency_terms"):
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS high_frequency_terms (
                id INTEGER NOT NULL,
                term TEXT NOT NULL,
                normalized_term TEXT NOT NULL,
                term_type VARCHAR(20) NOT NULL DEFAULT 'word',
                rank INTEGER NOT NULL DEFAULT 0,
                occurrence_count INTEGER NOT NULL DEFAULT 0,
                message_count INTEGER NOT NULL DEFAULT 0,
                frequency FLOAT NOT NULL DEFAULT 0,
                message_frequency FLOAT NOT NULL DEFAULT 0,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                PRIMARY KEY (id),
                CONSTRAINT uq_high_frequency_terms_normalized_term UNIQUE (normalized_term)
            )
            """
        )

    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS ix_high_frequency_terms_rank
        ON high_frequency_terms (rank)
        """
    )
    connection.exec_driver_sql(
        """
        CREATE INDEX IF NOT EXISTS ix_high_frequency_terms_updated_at
        ON high_frequency_terms (updated_at)
        """
    )
