"""表达方式表兼容修复：旧修改者和 Unix 时间戳。"""

NORMALIZE_EXPRESSION_SOURCES_SQL = """
UPDATE expressions SET modified_by = NULL
WHERE LOWER(TRIM(modified_by)) IN ('legacy', 'learned', '')
"""


def repair_legacy_expression_sources(connection) -> int:
    """仅修复已知旧值，不伪造 AI/USER 归属，也不改变表达内容、审核状态。"""
    result = connection.exec_driver_sql(NORMALIZE_EXPRESSION_SOURCES_SQL)
    return max(0, result.rowcount)


def repair_legacy_expression_timestamps(connection) -> int:
    """Convert legacy integer Unix timestamps before SQLModel loads DateTime.

    Older GSMPS imports stored both expression timestamps as integer seconds;
    SQLAlchemy's DateTime processor rejects those values before WebUI can
    serialize a row, so this repair must happen at database initialization.
    """
    rows = connection.exec_driver_sql(
        "SELECT id, last_active_time, create_time FROM expressions "
        "WHERE typeof(last_active_time) IN ('integer','real') "
        "OR typeof(create_time) IN ('integer','real')"
    ).fetchall()
    from datetime import datetime
    changed = 0
    for row_id, last_active, created in rows:
        if isinstance(last_active, (int, float)):
            last_active = datetime.fromtimestamp(float(last_active)).isoformat(timespec='microseconds')
        if isinstance(created, (int, float)):
            created = datetime.fromtimestamp(float(created)).isoformat(timespec='microseconds')
        connection.exec_driver_sql(
            "UPDATE expressions SET last_active_time=?, create_time=? WHERE id=?",
            (last_active, created, row_id),
        )
        changed += 1
    return changed
