"""v33 schema 升级到 v34：重建黑话证据消息结构。"""

from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")


def migrate_v33_to_v34(context: MigrationExecutionContext) -> None:
    """为 ``jargons`` 增加 ``evidence_messages`` 列，并移除旧 ``raw_content`` 列。"""

    context.start_progress(
        total_tables=1,
        total_records=2,
        description="v33 -> v34 迁移进度",
        table_unit_name="表",
        record_unit_name="列",
    )

    changed_columns = rebuild_jargons_for_evidence_messages(context.connection)
    context.advance_progress(records=changed_columns, completed_tables=1, item_name="jargons")

    logger.info("v33 -> v34 数据库迁移完成：黑话证据消息引用列已就绪，旧 raw_content 列已移除")


_JARGON_V34_COLUMNS = (
    "id",
    "content",
    "evidence_messages",
    "meaning",
    "session_id_dict",
    "count",
    "is_jargon",
    "is_complete",
    "is_global",
    "last_inference_count",
    "created_timestamp",
    "updated_timestamp",
    "created_by",
)

_JARGON_V34_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_jargons_content ON jargons (content)",
    "CREATE INDEX IF NOT EXISTS ix_jargons_created_timestamp ON jargons (created_timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_jargons_updated_timestamp ON jargons (updated_timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_jargons_status_count_id ON jargons (is_jargon, count DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_jargons_global_count_id ON jargons (is_global, count DESC, id DESC)",
    "CREATE INDEX IF NOT EXISTS ix_jargons_complete_count_id ON jargons (is_complete, count DESC, id DESC)",
)


def rebuild_jargons_for_evidence_messages(connection: Connection) -> int:
    """重建黑话表：补齐证据消息引用列，并移除不再使用的 raw_content。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "jargons"):
        return 0

    table_schema = schema_inspector.get_table_schema(connection, "jargons")
    has_evidence_messages = table_schema.has_column("evidence_messages")
    has_raw_content = table_schema.has_column("raw_content")
    if has_evidence_messages and not has_raw_content:
        return 0

    connection.exec_driver_sql("DROP TABLE IF EXISTS jargons_v34")
    connection.exec_driver_sql(
        """
        CREATE TABLE jargons_v34 (
            id INTEGER NOT NULL,
            content VARCHAR(255) NOT NULL,
            evidence_messages TEXT,
            meaning TEXT NOT NULL,
            session_id_dict TEXT NOT NULL,
            count INTEGER NOT NULL,
            is_jargon BOOLEAN,
            is_complete BOOLEAN NOT NULL,
            is_global BOOLEAN NOT NULL,
            last_inference_count INTEGER NOT NULL,
            created_timestamp DATETIME,
            updated_timestamp DATETIME,
            created_by VARCHAR(6) NOT NULL,
            PRIMARY KEY (id)
        )
        """
    )

    select_columns = []
    for column_name in _JARGON_V34_COLUMNS:
        if column_name == "created_by":
            select_columns.append("COALESCE(created_by, 'AI')" if table_schema.has_column(column_name) else "'AI'")
        elif column_name == "evidence_messages":
            select_columns.append("evidence_messages" if table_schema.has_column(column_name) else "NULL")
        else:
            select_columns.append(column_name)
    insert_columns_sql = ", ".join(_JARGON_V34_COLUMNS)
    select_columns_sql = ", ".join(select_columns)
    connection.exec_driver_sql(
        f"""
        INSERT INTO jargons_v34 ({insert_columns_sql})
        SELECT {select_columns_sql}
        FROM jargons
        """
    )
    connection.exec_driver_sql("DROP TABLE jargons")
    connection.exec_driver_sql("ALTER TABLE jargons_v34 RENAME TO jargons")

    for statement in _JARGON_V34_INDEXES:
        connection.exec_driver_sql(statement)

    changed_columns = 0
    if not has_evidence_messages:
        changed_columns += 1
    if has_raw_content:
        changed_columns += 1
    return changed_columns
