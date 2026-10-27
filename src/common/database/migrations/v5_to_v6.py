"""v5 schema 升级到 v6 的迁移逻辑。"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")

_CHAT_SESSION_ROUTE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS ix_chat_sessions_account_id ON chat_sessions (account_id)",
    "CREATE INDEX IF NOT EXISTS ix_chat_sessions_scope ON chat_sessions (scope)",
)


def migrate_v5_to_v6(context: MigrationExecutionContext) -> None:
    """执行 v5 到 v6 的 schema 迁移。"""

    connection = context.connection
    context.start_progress(
        total_tables=1,
        total_records=0,
        description="v5 -> v6 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    _add_chat_session_route_columns(connection)
    context.advance_progress(records=0, completed_tables=1, item_name="chat_sessions")

    logger.info("v5 -> v6 数据库迁移完成: chat_sessions 已添加 account_id/scope 路由字段")


def _add_chat_session_route_columns(connection: Connection) -> None:
    """为 ``chat_sessions`` 增加可解释的路由归属字段，历史数据默认留空。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "chat_sessions"):
        return

    table_schema = schema_inspector.get_table_schema(connection, "chat_sessions")
    if not table_schema.has_column("account_id"):
        connection.execute(text("ALTER TABLE chat_sessions ADD COLUMN account_id VARCHAR(255)"))
    if not table_schema.has_column("scope"):
        connection.execute(text("ALTER TABLE chat_sessions ADD COLUMN scope VARCHAR(255)"))

    for statement in _CHAT_SESSION_ROUTE_INDEXES:
        connection.exec_driver_sql(statement)
