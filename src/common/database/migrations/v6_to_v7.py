"""v6 schema 升级到 v7 的迁移逻辑。"""

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .exceptions import DatabaseMigrationExecutionError
from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")

_V6_EXPRESSIONS_BACKUP_TABLE = "__v6_expressions_backup"
_V7_EXPRESSIONS_CREATE_SQL = """
CREATE TABLE expressions (
    id INTEGER NOT NULL,
    situation VARCHAR(255) NOT NULL,
    style VARCHAR(255) NOT NULL,
    content_list VARCHAR NOT NULL,
    count INTEGER NOT NULL,
    last_active_time DATETIME,
    create_time DATETIME,
    session_id VARCHAR(255),
    checked BOOLEAN NOT NULL,
    modified_by VARCHAR(4),
    PRIMARY KEY (id)
)
"""
_V7_EXPRESSIONS_INDEX_STATEMENTS = (
    "CREATE INDEX IF NOT EXISTS ix_expressions_last_active_time ON expressions (last_active_time)",
    "CREATE INDEX IF NOT EXISTS ix_expressions_situation ON expressions (situation)",
    "CREATE INDEX IF NOT EXISTS ix_expressions_style ON expressions (style)",
)


def migrate_v6_to_v7(context: MigrationExecutionContext) -> None:
    """执行 v6 到 v7 的表达方式审核状态迁移。"""

    connection = context.connection
    total_records = _count_table_rows(connection, "expressions")
    context.start_progress(
        total_tables=1,
        total_records=total_records,
        description="v6 -> v7 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    deleted_rows, migrated_rows = _migrate_expressions_table_to_v7(connection)
    context.advance_progress(
        records=migrated_rows,
        completed_tables=1,
        item_name="expressions",
    )

    logger.info(
        f"v6 -> v7 数据库迁移完成: expressions 重建={migrated_rows}，"
        f"已删除 checked=1 且 rejected=1 的表达方式 {deleted_rows} 条"
    )


def _count_table_rows(connection: Connection, table_name: str) -> int:
    """统计表记录数，不存在时返回 0。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, table_name):
        return 0
    row = connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).first()
    return int(row[0]) if row else 0


def _count_checked_rejected_expressions(connection: Connection) -> int:
    """统计人工拒绝遗留记录数量。"""

    row = connection.execute(
        text(
            """
            SELECT COUNT(*)
            FROM expressions
            WHERE COALESCE(checked, 0) = 1
              AND COALESCE(rejected, 0) = 1
            """
        )
    ).first()
    return int(row[0]) if row else 0


def _delete_checked_rejected_expressions(connection: Connection) -> int:
    """删除 checked=1 且 rejected=1 的历史表达方式。"""

    deleted_rows = _count_checked_rejected_expressions(connection)
    if deleted_rows <= 0:
        return 0

    connection.execute(
        text(
            """
            DELETE FROM expressions
            WHERE COALESCE(checked, 0) = 1
              AND COALESCE(rejected, 0) = 1
            """
        )
    )
    logger.info(f"已删除 checked=1 且 rejected=1 的表达方式 {deleted_rows} 条")
    return deleted_rows


def _migrate_expressions_table_to_v7(connection: Connection) -> tuple[int, int]:
    """重建 ``expressions`` 表，删除遗留 ``rejected`` 列。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "expressions"):
        return 0, 0
    if not schema_inspector.get_table_schema(connection, "expressions").has_column("rejected"):
        migrated_rows = _count_table_rows(connection, "expressions")
        logger.info("expressions 表已无 rejected 列，跳过重建")
        return 0, migrated_rows
    if schema_inspector.table_exists(connection, _V6_EXPRESSIONS_BACKUP_TABLE):
        raise DatabaseMigrationExecutionError(
            f"检测到残留备份表 {_V6_EXPRESSIONS_BACKUP_TABLE}，无法安全执行 v6 -> v7 expressions 迁移。"
        )

    deleted_rows = _delete_checked_rejected_expressions(connection)

    connection.exec_driver_sql(f'ALTER TABLE "expressions" RENAME TO "{_V6_EXPRESSIONS_BACKUP_TABLE}"')
    connection.exec_driver_sql(_V7_EXPRESSIONS_CREATE_SQL)
    connection.execute(
        text(
            f"""
            INSERT INTO expressions (
                id,
                situation,
                style,
                content_list,
                count,
                last_active_time,
                create_time,
                session_id,
                checked,
                modified_by
            )
            SELECT
                id,
                situation,
                style,
                content_list,
                count,
                last_active_time,
                create_time,
                session_id,
                checked,
                modified_by
            FROM "{_V6_EXPRESSIONS_BACKUP_TABLE}"
            ORDER BY id
            """
        )
    )

    migrated_rows = _count_table_rows(connection, "expressions")
    connection.exec_driver_sql(f'DROP TABLE "{_V6_EXPRESSIONS_BACKUP_TABLE}"')
    for statement in _V7_EXPRESSIONS_INDEX_STATEMENTS:
        connection.exec_driver_sql(statement)
    return deleted_rows, migrated_rows
