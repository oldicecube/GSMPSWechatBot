"""v7 schema 升级到 v8 的迁移逻辑。"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")


def migrate_v7_to_v8(context: MigrationExecutionContext) -> None:
    """执行 v7 到 v8 的表达方式审核状态迁移。"""

    connection = context.connection
    affected_rows = _count_ai_checked_expressions(connection)
    context.start_progress(
        total_tables=1,
        total_records=affected_rows,
        description="v7 -> v8 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    migrated_rows = _clear_ai_checked_expressions(connection)
    context.advance_progress(records=migrated_rows, completed_tables=1, item_name="expressions")

    logger.info(f"v7 -> v8 数据库迁移完成: 已将 AI 标记的已审核表达方式改回待人工审核 {migrated_rows} 条")


def _count_ai_checked_expressions(connection: Connection) -> int:
    """统计需要改回待人工审核的 AI checked 表达方式数量。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "expressions"):
        return 0
    table_schema = schema_inspector.get_table_schema(connection, "expressions")
    if not table_schema.has_column("checked") or not table_schema.has_column("modified_by"):
        return 0

    row = connection.execute(
        text(
            """
            SELECT COUNT(*)
            FROM expressions
            WHERE COALESCE(checked, 0) = 1
              AND UPPER(TRIM(REPLACE(CAST(modified_by AS TEXT), '"', ''))) = 'AI'
            """
        )
    ).first()
    return int(row[0]) if row else 0


def _clear_ai_checked_expressions(connection: Connection) -> int:
    """将 AI checked 的历史表达方式改回未人工审核状态。"""

    affected_rows = _count_ai_checked_expressions(connection)
    if affected_rows <= 0:
        return 0

    connection.execute(
        text(
            """
            UPDATE expressions
            SET checked = 0
            WHERE COALESCE(checked, 0) = 1
              AND UPPER(TRIM(REPLACE(CAST(modified_by AS TEXT), '"', ''))) = 'AI'
            """
        )
    )
    return affected_rows
