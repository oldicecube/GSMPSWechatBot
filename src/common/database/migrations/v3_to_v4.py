"""v3 schema 升级到 v4 的迁移逻辑。"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .exceptions import DatabaseMigrationExecutionError
from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")

_V3_MESSAGES_BACKUP_TABLE = "__v3_mai_messages_backup"
_V4_MESSAGES_CREATE_SQL = """
CREATE TABLE mai_messages (
    id INTEGER NOT NULL,
    message_id VARCHAR(255) NOT NULL,
    timestamp DATETIME,
    platform VARCHAR(100) NOT NULL,
    user_id VARCHAR(255) NOT NULL,
    user_nickname VARCHAR(255) NOT NULL,
    user_cardname VARCHAR(255),
    group_id VARCHAR(255),
    group_name VARCHAR(255),
    is_mentioned BOOLEAN NOT NULL,
    is_at BOOLEAN NOT NULL,
    session_id VARCHAR(255) NOT NULL,
    reply_to VARCHAR(255),
    is_emoji BOOLEAN NOT NULL,
    is_picture BOOLEAN NOT NULL,
    is_command BOOLEAN NOT NULL,
    is_notify BOOLEAN NOT NULL,
    raw_content BLOB,
    processed_plain_text VARCHAR,
    additional_config VARCHAR,
    PRIMARY KEY (id)
)
"""
_V4_MESSAGES_INDEX_STATEMENTS = (
    "CREATE INDEX ix_mai_messages_group_id ON mai_messages (group_id)",
    "CREATE INDEX ix_mai_messages_message_id ON mai_messages (message_id)",
    "CREATE INDEX ix_mai_messages_platform ON mai_messages (platform)",
    "CREATE INDEX ix_mai_messages_session_id ON mai_messages (session_id)",
    "CREATE INDEX ix_mai_messages_user_id ON mai_messages (user_id)",
    "CREATE INDEX ix_mai_messages_user_nickname ON mai_messages (user_nickname)",
)


def migrate_v3_to_v4(context: MigrationExecutionContext) -> None:
    """执行 v3 到 v4 的 schema 迁移。"""

    connection = context.connection
    total_records = _count_table_rows(connection, "mai_messages")
    context.start_progress(
        total_tables=1,
        total_records=total_records,
        description="v3 -> v4 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    migrated_message_rows = _migrate_messages_table_to_v4(connection)
    context.advance_progress(
        records=migrated_message_rows,
        completed_tables=1,
        item_name="mai_messages",
    )

    logger.info(f"v3 -> v4 数据库迁移完成: mai_messages重建={migrated_message_rows}")


def _count_table_rows(connection: Connection, table_name: str) -> int:
    """统计表记录数，不存在时返回 0。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, table_name):
        return 0
    row = connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).first()
    return int(row[0]) if row else 0


def _migrate_messages_table_to_v4(connection: Connection) -> int:
    """重建 ``mai_messages`` 表并移除弃用的 ``display_message`` 列。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "mai_messages"):
        return 0
    if not schema_inspector.get_table_schema(connection, "mai_messages").has_column("display_message"):
        return _count_table_rows(connection, "mai_messages")
    if schema_inspector.table_exists(connection, _V3_MESSAGES_BACKUP_TABLE):
        raise DatabaseMigrationExecutionError(
            f"检测到残留备份表 {_V3_MESSAGES_BACKUP_TABLE}，无法安全执行 v3 -> v4 mai_messages 迁移。"
        )

    connection.exec_driver_sql(f'ALTER TABLE "mai_messages" RENAME TO "{_V3_MESSAGES_BACKUP_TABLE}"')
    connection.exec_driver_sql(_V4_MESSAGES_CREATE_SQL)

    connection.execute(
        text(
            f"""
            INSERT INTO mai_messages (
                id,
                message_id,
                timestamp,
                platform,
                user_id,
                user_nickname,
                user_cardname,
                group_id,
                group_name,
                is_mentioned,
                is_at,
                session_id,
                reply_to,
                is_emoji,
                is_picture,
                is_command,
                is_notify,
                raw_content,
                processed_plain_text,
                additional_config
            )
            SELECT
                id,
                message_id,
                timestamp,
                platform,
                user_id,
                user_nickname,
                user_cardname,
                group_id,
                group_name,
                is_mentioned,
                is_at,
                session_id,
                reply_to,
                is_emoji,
                is_picture,
                is_command,
                is_notify,
                raw_content,
                COALESCE(NULLIF(processed_plain_text, ''), display_message),
                additional_config
            FROM "{_V3_MESSAGES_BACKUP_TABLE}"
            ORDER BY id
            """
        )
    )

    migrated_rows = _count_table_rows(connection, "mai_messages")
    connection.exec_driver_sql(f'DROP TABLE "{_V3_MESSAGES_BACKUP_TABLE}"')
    for statement in _V4_MESSAGES_INDEX_STATEMENTS:
        connection.exec_driver_sql(statement)
    return migrated_rows
