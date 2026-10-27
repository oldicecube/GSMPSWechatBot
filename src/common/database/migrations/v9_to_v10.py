"""v9 schema 升级到 v10：为聊天流持久化展示身份字段。"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")

_CHAT_SESSION_IDENTITY_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_chat_sessions_user_nickname ON chat_sessions (user_nickname)",
    "CREATE INDEX IF NOT EXISTS ix_chat_sessions_group_name ON chat_sessions (group_name)",
)


def migrate_v9_to_v10(context: MigrationExecutionContext) -> None:
    """为 ``chat_sessions`` 增加群名与私聊用户展示名字段。"""

    connection = context.connection
    context.start_progress(
        total_tables=1,
        total_records=0,
        description="v9 -> v10 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _add_chat_session_identity_columns(connection)
    context.advance_progress(records=0, completed_tables=1, item_name="chat_sessions")

    logger.info("v9 -> v10 数据库迁移完成: chat_sessions 已添加展示身份字段")


def _add_chat_session_identity_columns(connection: Connection) -> None:
    """历史聊天流默认留空，后续由真实入站消息逐步补齐。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "chat_sessions"):
        return

    table_schema = schema_inspector.get_table_schema(connection, "chat_sessions")
    if not table_schema.has_column("user_nickname"):
        connection.execute(text("ALTER TABLE chat_sessions ADD COLUMN user_nickname VARCHAR(255)"))
    if not table_schema.has_column("user_cardname"):
        connection.execute(text("ALTER TABLE chat_sessions ADD COLUMN user_cardname VARCHAR(255)"))
    if not table_schema.has_column("group_name"):
        connection.execute(text("ALTER TABLE chat_sessions ADD COLUMN group_name VARCHAR(255)"))

    for statement in _CHAT_SESSION_IDENTITY_INDEXES:
        connection.exec_driver_sql(statement)
