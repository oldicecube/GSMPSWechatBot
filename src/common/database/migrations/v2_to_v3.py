"""v2 schema 升级到 v3 的迁移逻辑。"""

from typing import Any, Dict, List

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .exceptions import DatabaseMigrationExecutionError
from .models import MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")

_V2_IMAGES_BACKUP_TABLE = "__v2_images_backup"
_V3_IMAGES_CREATE_SQL = """
CREATE TABLE images (
    id INTEGER NOT NULL,
    image_hash VARCHAR(255) NOT NULL,
    description VARCHAR NOT NULL,
    full_path VARCHAR(1024) NOT NULL,
    image_type VARCHAR(5),
    query_count INTEGER NOT NULL,
    is_registered BOOLEAN NOT NULL,
    is_banned BOOLEAN NOT NULL,
    no_file_flag BOOLEAN NOT NULL,
    record_time DATETIME,
    register_time DATETIME,
    last_used_time DATETIME,
    vlm_processed BOOLEAN NOT NULL,
    PRIMARY KEY (id)
)
"""
_V3_IMAGES_INDEX_STATEMENTS = (
    "CREATE INDEX ix_images_image_hash ON images (image_hash)",
    "CREATE INDEX ix_images_record_time ON images (record_time)",
)


def migrate_v2_to_v3(context: MigrationExecutionContext) -> None:
    """执行 v2 到 v3 的 schema 迁移。

    Args:
        context: 当前迁移步骤执行上下文。
    """

    connection = context.connection
    total_records = (
        _count_table_rows(connection, "action_records")
        + _count_table_rows(connection, "thinking_questions")
        + _count_table_rows(connection, "images")
    )
    context.start_progress(
        total_tables=3,
        total_records=total_records,
        description="v2 -> v3 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )

    migrated_tool_records = _migrate_action_records_to_tool_records(connection)
    action_record_count = _count_table_rows(connection, "action_records")
    _drop_table_if_exists(connection, "action_records")
    context.advance_progress(
        records=action_record_count,
        completed_tables=1,
        item_name="action_records",
    )

    thinking_question_count = _count_table_rows(connection, "thinking_questions")
    _drop_table_if_exists(connection, "thinking_questions")
    context.advance_progress(
        records=thinking_question_count,
        completed_tables=1,
        item_name="thinking_questions",
    )

    migrated_image_rows = _migrate_images_table_to_v3(connection)
    context.advance_progress(
        records=migrated_image_rows,
        completed_tables=1,
        item_name="images",
    )

    logger.info(
        "v2 -> v3 数据库迁移完成: "
        f"tool_records补迁移={migrated_tool_records}，"
        f"images重建={migrated_image_rows}"
    )


def _count_table_rows(connection: Connection, table_name: str) -> int:
    """统计表记录数，不存在时返回 0。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, table_name):
        return 0
    row = connection.execute(text(f'SELECT COUNT(*) FROM "{table_name}"')).first()
    return int(row[0]) if row else 0


def _drop_table_if_exists(connection: Connection, table_name: str) -> None:
    """删除指定表，不存在时静默跳过。"""

    connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}"')


def _migrate_action_records_to_tool_records(connection: Connection) -> int:
    """把 v2 中残留的 ``action_records`` 数据转存到 ``tool_records``。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "action_records"):
        return 0

    inserted_count = _count_table_rows(connection, "action_records")
    connection.execute(
        text(
            """
            INSERT INTO tool_records (
                tool_id,
                timestamp,
                session_id,
                tool_name,
                tool_reasoning,
                tool_data,
                tool_builtin_prompt,
                tool_display_prompt
            )
            SELECT
                action_id,
                timestamp,
                session_id,
                action_name,
                action_reasoning,
                action_data,
                action_builtin_prompt,
                action_display_prompt
            FROM action_records
            WHERE NOT EXISTS (
                SELECT 1
                FROM tool_records
                WHERE tool_records.tool_id = action_records.action_id
            )
            """
        )
    )
    return inserted_count


def _migrate_images_table_to_v3(connection: Connection) -> int:
    """重建 ``images`` 表并移除 ``emotion`` 列。"""

    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, "images"):
        return 0
    if not schema_inspector.get_table_schema(connection, "images").has_column("emotion"):
        return _count_table_rows(connection, "images")
    if schema_inspector.table_exists(connection, _V2_IMAGES_BACKUP_TABLE):
        raise DatabaseMigrationExecutionError(
            f"检测到残留备份表 {_V2_IMAGES_BACKUP_TABLE}，无法安全执行 v2 -> v3 images 迁移。"
        )

    connection.exec_driver_sql(f'ALTER TABLE "images" RENAME TO "{_V2_IMAGES_BACKUP_TABLE}"')
    connection.exec_driver_sql(_V3_IMAGES_CREATE_SQL)

    legacy_rows = connection.execute(
        text(f'SELECT * FROM "{_V2_IMAGES_BACKUP_TABLE}" ORDER BY id')
    ).mappings().all()
    insert_sql = text(
        """
        INSERT INTO images (
            id,
            image_hash,
            description,
            full_path,
            image_type,
            query_count,
            is_registered,
            is_banned,
            no_file_flag,
            record_time,
            register_time,
            last_used_time,
            vlm_processed
        ) VALUES (
            :id,
            :image_hash,
            :description,
            :full_path,
            :image_type,
            :query_count,
            :is_registered,
            :is_banned,
            :no_file_flag,
            :record_time,
            :register_time,
            :last_used_time,
            :vlm_processed
        )
        """
    )

    for row in legacy_rows:
        payload: Dict[str, Any] = {
            "id": row.get("id"),
            "image_hash": str(row.get("image_hash") or "").strip(),
            "description": _migrate_v3_emoji_description(row),
            "full_path": str(row.get("full_path") or "").strip(),
            "image_type": row.get("image_type"),
            "query_count": int(row.get("query_count") or 0),
            "is_registered": bool(row.get("is_registered")),
            "is_banned": bool(row.get("is_banned")),
            "no_file_flag": bool(row.get("no_file_flag")),
            "record_time": row.get("record_time"),
            "register_time": row.get("register_time"),
            "last_used_time": row.get("last_used_time"),
            "vlm_processed": bool(row.get("vlm_processed")),
        }
        connection.execute(insert_sql, payload)

    connection.exec_driver_sql(f'DROP TABLE "{_V2_IMAGES_BACKUP_TABLE}"')
    for statement in _V3_IMAGES_INDEX_STATEMENTS:
        connection.exec_driver_sql(statement)
    return len(legacy_rows)


def _migrate_v3_emoji_description(row: Dict[str, Any]) -> str:
    """为 v3 统一 emoji 描述字段语义。

    v3 中 `description` 对 emoji 统一承担“标签列表”的职责，因此迁移时：
        1. 若旧 `emotion` 非空，优先将其规范化后写入 `description`；
        2. 否则保留并规范化当前 `description`；
        3. 非 emoji 图片保持原描述不变。
    """

    image_type = str(row.get("image_type") or "").strip().upper()
    current_description = str(row.get("description") or "").strip()
    current_emotion = str(row.get("emotion") or "").strip()
    if image_type != "EMOJI":
        return current_description

    normalized_tags = _normalize_emoji_tag_text(current_emotion or current_description)
    if normalized_tags:
        return ",".join(normalized_tags)
    return current_description


def _normalize_emoji_tag_text(raw_value: Any) -> List[str]:
    """将 emoji 标签文本转换为去重后的标签列表。"""

    normalized_text = str(raw_value or "").strip()
    if not normalized_text:
        return []

    separators = [",", "，", "、", ";", "；", "\n", "\r", "\t"]
    for separator in separators[1:]:
        normalized_text = normalized_text.replace(separator, separators[0])

    deduped_tags: List[str] = []
    seen_tags: set[str] = set()
    for part in normalized_text.split(separators[0]):
        normalized_part = part.strip()
        lowered_part = normalized_part.lower()
        if not normalized_part or lowered_part in seen_tags:
            continue
        seen_tags.add(lowered_part)
        deduped_tags.append(normalized_part)
    return deduped_tags
