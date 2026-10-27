"""v4 schema 升级到 v5 的迁移逻辑。"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")


def migrate_v4_to_v5(context: MigrationExecutionContext) -> None:
    """执行 v4 到 v5 的数据归一化迁移。"""

    connection = context.connection
    affected_rows = _count_group_sessions_with_user_id(connection)
    context.start_progress(
        total_tables=1,
        total_records=affected_rows,
        description="v4 -> v5 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    normalized_rows = _clear_group_session_user_ids(connection)
    context.advance_progress(
        records=normalized_rows,
        completed_tables=1,
        item_name="chat_sessions",
    )

    logger.info(f"v4 -> v5 数据库迁移完成: 已清空群聊 chat_sessions.user_id {normalized_rows} 条")


def _count_group_sessions_with_user_id(connection: Connection) -> int:
    """统计需要清空 ``user_id`` 的群聊会话数量。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "chat_sessions"):
        return 0
    row = connection.execute(
        text(
            """
            SELECT COUNT(*)
            FROM chat_sessions
            WHERE group_id IS NOT NULL
              AND TRIM(group_id) <> ''
              AND user_id IS NOT NULL
              AND TRIM(user_id) <> ''
            """
        )
    ).first()
    return int(row[0]) if row else 0


def _clear_group_session_user_ids(connection: Connection) -> int:
    """清空群聊会话中没有归属语义的 ``user_id``。"""

    affected_rows = _count_group_sessions_with_user_id(connection)
    if affected_rows <= 0:
        return 0

    connection.execute(
        text(
            """
            UPDATE chat_sessions
            SET user_id = NULL
            WHERE group_id IS NOT NULL
              AND TRIM(group_id) <> ''
              AND user_id IS NOT NULL
              AND TRIM(user_id) <> ''
            """
        )
    )
    return affected_rows
