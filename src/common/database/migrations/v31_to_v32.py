"""v31 schema 升级到 v32：清理表达方式 prompt 示例污染。"""

from collections import defaultdict
from typing import Any, Sequence

import json

from sqlalchemy.engine import Connection

from src.common.logger import get_logger
from src.learners.expression_style_utils import (
    is_prompt_example_expression_style,
    normalize_expression_style_for_learning,
)

from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")


def migrate_v31_to_v32(context: MigrationExecutionContext) -> None:
    """清理表达方式中由学习 prompt 示例带出的“使用”前缀和示例内容。"""

    context.start_progress(
        total_tables=1,
        total_records=_count_expression_rows(context.connection),
        description="v31 -> v32 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )
    cleanup_stats = cleanup_expression_style_pollution(context.connection)
    context.advance_progress(
        records=cleanup_stats["updated_rows"] + cleanup_stats["deleted_rows"],
        completed_tables=1,
        item_name="expressions",
    )
    logger.info(
        "v31 -> v32 数据库迁移完成：表达方式更新=%s, 删除=%s, 去前缀=%s, 示例泄漏=%s, 合并重复组=%s",
        cleanup_stats["updated_rows"],
        cleanup_stats["deleted_rows"],
        cleanup_stats["prefix_rows"],
        cleanup_stats["example_rows"],
        cleanup_stats["merged_groups"],
    )


def cleanup_expression_style_pollution(connection: Connection) -> dict[str, int]:
    """执行表达方式污染数据清理，并返回统计信息。"""

    cleanup_plan = _build_expression_style_cleanup_plan(connection)
    for row_id, values in cleanup_plan["update_rows"].items():
        connection.exec_driver_sql(
            """
            UPDATE expressions
            SET situation = ?,
                style = ?,
                content_list = ?,
                count = ?,
                checked = ?,
                modified_by = ?,
                create_time = ?,
                last_active_time = ?
            WHERE id = ?
            """,
            (
                values["situation"],
                values["style"],
                values["content_list"],
                values["count"],
                values["checked"],
                values["modified_by"],
                values["create_time"],
                values["last_active_time"],
                row_id,
            ),
        )

    deleted_rows = _delete_expression_rows(connection, sorted(cleanup_plan["delete_ids"]))
    return {
        "updated_rows": len(cleanup_plan["update_rows"]),
        "deleted_rows": deleted_rows,
        "prefix_rows": cleanup_plan["prefix_rows"],
        "example_rows": cleanup_plan["example_rows"],
        "merged_groups": cleanup_plan["merged_groups"],
    }


def _count_expression_rows(connection: Connection) -> int:
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "expressions"):
        raise RuntimeError("v31 -> v32 迁移需要 expressions 表")
    row = connection.exec_driver_sql("SELECT COUNT(*) FROM expressions").fetchone()
    return int(row[0] or 0) if row is not None else 0


def _build_expression_style_cleanup_plan(connection: Connection) -> dict[str, Any]:
    rows = connection.exec_driver_sql(
        """
        SELECT id, session_id, situation, style, content_list, count, checked,
               modified_by, create_time, last_active_time
        FROM expressions
        ORDER BY id
        """
    ).mappings().all()

    delete_ids: set[int] = set()
    normalized_rows: list[dict[str, Any]] = []
    prefix_rows = 0
    example_rows = 0

    for row in rows:
        row_id = int(row["id"])
        situation = str(row["situation"] or "").strip()
        style = str(row["style"] or "").strip()
        normalized_style = normalize_expression_style_for_learning(style)

        if style != normalized_style:
            prefix_rows += 1
        if is_prompt_example_expression_style(style):
            delete_ids.add(row_id)
            example_rows += 1
            continue
        if not situation or not normalized_style:
            delete_ids.add(row_id)
            continue

        normalized_rows.append(
            {
                "id": row_id,
                "session_id": row["session_id"],
                "situation": situation,
                "style": style,
                "normalized_style": normalized_style,
                "content_list": str(row["content_list"] or "[]"),
                "count": int(row["count"] or 0),
                "checked": int(bool(row["checked"])),
                "modified_by": row["modified_by"],
                "create_time": row["create_time"],
                "last_active_time": row["last_active_time"],
            }
        )

    rows_by_identity: dict[tuple[str | None, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in normalized_rows:
        rows_by_identity[(row["session_id"], row["situation"], row["normalized_style"])].append(row)

    update_rows: dict[int, dict[str, Any]] = {}
    merged_groups = 0
    for group_rows in rows_by_identity.values():
        sorted_group = sorted(group_rows, key=lambda item: (item["create_time"] or "", item["id"]))
        keep_row = sorted_group[0]
        duplicate_rows = sorted_group[1:]
        if duplicate_rows:
            merged_groups += 1
            delete_ids.update(int(row["id"]) for row in duplicate_rows)

        merged_content = _merge_expression_content(sorted_group)
        serialized_content = json.dumps(merged_content, ensure_ascii=False)
        if (
            keep_row["style"] == keep_row["normalized_style"]
            and not duplicate_rows
            and keep_row["content_list"] == serialized_content
        ):
            continue

        update_rows[int(keep_row["id"])] = {
            "situation": keep_row["situation"],
            "style": keep_row["normalized_style"],
            "content_list": serialized_content,
            "count": sum(int(row["count"]) for row in sorted_group),
            "checked": int(any(row["checked"] for row in sorted_group)),
            "modified_by": _choose_modified_by(sorted_group),
            "create_time": _choose_min_time(sorted_group, "create_time"),
            "last_active_time": _choose_max_time(sorted_group, "last_active_time"),
        }

    for row_id in delete_ids:
        update_rows.pop(row_id, None)

    return {
        "delete_ids": delete_ids,
        "update_rows": update_rows,
        "prefix_rows": prefix_rows,
        "example_rows": example_rows,
        "merged_groups": merged_groups,
    }


def _merge_expression_content(rows: Sequence[dict[str, Any]]) -> list[str]:
    merged_content: list[str] = []
    for row in rows:
        for content in _load_content_list(row["content_list"], int(row["id"])):
            if content not in merged_content:
                merged_content.append(content)
    situation = str(rows[0]["situation"]).strip()
    if situation and situation not in merged_content:
        merged_content.append(situation)
    return merged_content


def _load_content_list(raw_content_list: str, row_id: int) -> list[str]:
    parsed = json.loads(raw_content_list)
    if not isinstance(parsed, list):
        raise ValueError(f"表达方式 #{row_id} 的 content_list 不是 JSON 数组")

    contents: list[str] = []
    for item in parsed:
        content = str(item).strip()
        if content and content not in contents:
            contents.append(content)
    return contents


def _choose_modified_by(rows: Sequence[dict[str, Any]]) -> str | None:
    modified_by_values = {row["modified_by"] for row in rows if row["modified_by"]}
    if "USER" in modified_by_values:
        return "USER"
    if "AI" in modified_by_values:
        return "AI"
    return None


def _choose_min_time(rows: Sequence[dict[str, Any]], field_name: str) -> str | None:
    values = [row[field_name] for row in rows if row[field_name]]
    return min(values) if values else None


def _choose_max_time(rows: Sequence[dict[str, Any]], field_name: str) -> str | None:
    values = [row[field_name] for row in rows if row[field_name]]
    return max(values) if values else None


def _delete_expression_rows(connection: Connection, row_ids: Sequence[int]) -> int:
    if not row_ids:
        return 0

    deleted_count = 0
    for batch in _iter_batches(list(row_ids)):
        placeholders = ",".join("?" for _ in batch)
        cursor = connection.exec_driver_sql(
            f"DELETE FROM expressions WHERE id IN ({placeholders})",
            tuple(batch),
        )
        deleted_count += int(cursor.rowcount or 0)
    return deleted_count


def _iter_batches(items: list[int], batch_size: int = 500) -> Sequence[list[int]]:
    return [items[index : index + batch_size] for index in range(0, len(items), batch_size)]
