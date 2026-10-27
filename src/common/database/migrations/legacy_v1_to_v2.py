"""旧版 ``0.x`` 数据库升级到 v2 schema 的迁移逻辑。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, cast

import json

import msgpack
from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.common.logger import get_logger

from .exceptions import DatabaseMigrationExecutionError
from .frozen_v2_schema import create_frozen_v2_schema
from .models import DatabaseSchemaSnapshot, MigrationExecutionContext
from .schema import SQLiteSchemaInspector

logger = get_logger("database_migration")

_LEGACY_V1_BACKUP_PREFIX = "__legacy_v1_"
_LEGACY_V1_TABLE_NAMES = (
    "action_records",
    "chat_history",
    "chat_streams",
    "emoji",
    "emoji_description_cache",
    "expression",
    "group_info",
    "image_descriptions",
    "images",
    "jargon",
    "llm_usage",
    "messages",
    "online_time",
    "person_info",
    "thinking_back",
)
_EMPTY_MESSAGE_SEQUENCE_BYTES = msgpack.packb([], use_bin_type=True)


@dataclass(frozen=True)
class LegacyTableData:
    """旧版表数据快照。"""

    source_table_name: str
    columns: Set[str]
    rows: List[Dict[str, Any]]


def migrate_legacy_v1_to_v2(context: MigrationExecutionContext) -> None:
    """执行旧版 ``0.x`` 数据库到 v2 schema 的迁移。

    Args:
        context: 当前迁移步骤执行上下文。
    """
    schema_inspector = SQLiteSchemaInspector()
    snapshot = schema_inspector.inspect(context.connection)
    _rename_legacy_v1_tables(context.connection, snapshot)
    create_frozen_v2_schema(context.connection)

    table_migration_jobs: List[Tuple[str, Callable[[MigrationExecutionContext], int]]] = [
        ("llm_usage", _migrate_model_usage),
        ("images", _migrate_images),
        ("mai_messages", _migrate_messages),
        ("online_time", _migrate_online_time),
        ("person_info", _migrate_person_info),
        ("expressions", _migrate_expressions),
        ("jargons", _migrate_jargons),
    ]
    migrated_counts: Dict[str, int] = {}
    total_record_count = _estimate_total_record_count(context.connection)
    context.start_progress(
        total_tables=len(table_migration_jobs),
        total_records=total_record_count,
        description="总迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )
    for table_name, migration_handler in table_migration_jobs:
        migrated_counts[table_name] = migration_handler(context)

    summary_text = ", ".join(f"{table_name}={count}" for table_name, count in migrated_counts.items())
    logger.info(f"旧版数据库迁移完成: {summary_text}")


def _legacy_backup_table_name(table_name: str) -> str:
    """构建旧版表的备份表名。

    Args:
        table_name: 旧版原始表名。

    Returns:
        str: 带前缀的备份表名。
    """
    return f"{_LEGACY_V1_BACKUP_PREFIX}{table_name}"


def _quote_identifier(identifier: str) -> str:
    """为 SQLite 标识符添加安全引号。

    Args:
        identifier: 待引用的标识符。

    Returns:
        str: 可安全拼接到 SQL 中的标识符。
    """
    escaped_identifier = identifier.replace('"', '""')
    return f'"{escaped_identifier}"'


def _rename_legacy_v1_tables(connection: Connection, snapshot: DatabaseSchemaSnapshot) -> None:
    """将旧版表统一改名为带备份前缀的表名。

    Args:
        connection: 当前数据库连接。
        snapshot: 当前数据库结构快照。

    Raises:
        DatabaseMigrationExecutionError: 当发现同名旧表与备份表同时存在时抛出。
    """
    for table_name in _LEGACY_V1_TABLE_NAMES:
        if not snapshot.has_table(table_name):
            continue
        backup_table_name = _legacy_backup_table_name(table_name)
        if snapshot.has_table(backup_table_name):
            raise DatabaseMigrationExecutionError(
                "检测到旧版表与迁移备份表同时存在，无法安全继续迁移。"
                f" 冲突表={table_name}，备份表={backup_table_name}"
            )
        connection.execute(
            text(
                f"ALTER TABLE {_quote_identifier(table_name)} "
                f"RENAME TO {_quote_identifier(backup_table_name)}"
            )
        )


def _load_legacy_table_data(connection: Connection, original_table_name: str) -> Optional[LegacyTableData]:
    """加载单张旧版备份表的数据快照。

    Args:
        connection: 当前数据库连接。
        original_table_name: 旧版原始表名。

    Returns:
        Optional[LegacyTableData]: 若备份表存在则返回其数据快照，否则返回 ``None``。
    """
    backup_table_name = _legacy_backup_table_name(original_table_name)
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, backup_table_name):
        return None

    table_schema = schema_inspector.get_table_schema(connection, backup_table_name)
    rows = connection.execute(text(f"SELECT * FROM {_quote_identifier(backup_table_name)}")).mappings().all()
    return LegacyTableData(
        source_table_name=backup_table_name,
        columns=set(table_schema.columns),
        rows=[dict(row) for row in rows],
    )


def _normalize_optional_text(value: Any) -> Optional[str]:
    """将任意值标准化为可空字符串。

    Args:
        value: 待标准化的原始值。

    Returns:
        Optional[str]: 标准化后的文本；若值为空则返回 ``None``。
    """
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _normalize_required_text(value: Any, default: str = "") -> str:
    """将任意值标准化为非空字符串。

    Args:
        value: 待标准化的原始值。
        default: 为空时使用的默认值。

    Returns:
        str: 标准化后的字符串。
    """
    normalized_value = _normalize_optional_text(value)
    if normalized_value is None:
        return default
    return normalized_value


def _normalize_int(value: Any, default: int = 0) -> int:
    """将任意值标准化为整数。

    Args:
        value: 待标准化的原始值。
        default: 转换失败时的默认值。

    Returns:
        int: 标准化后的整数。
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_float(value: Any, default: float = 0.0) -> float:
    """将任意值标准化为浮点数。

    Args:
        value: 待标准化的原始值。
        default: 转换失败时的默认值。

    Returns:
        float: 标准化后的浮点数。
    """
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_optional_bool(value: Any) -> Optional[bool]:
    """将任意值标准化为可空布尔值。

    Args:
        value: 待标准化的原始值。

    Returns:
        Optional[bool]: 标准化后的布尔值；若无法确定则返回 ``None``。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(int(value))

    normalized_text = str(value).strip().lower()
    if normalized_text in {"", "null", "none"}:
        return None
    if normalized_text in {"1", "true", "t", "yes", "y"}:
        return True
    if normalized_text in {"0", "false", "f", "no", "n"}:
        return False
    return None


def _normalize_bool(value: Any, default: bool = False) -> bool:
    """将任意值标准化为布尔值。

    Args:
        value: 待标准化的原始值。
        default: 无法识别时的默认值。

    Returns:
        bool: 标准化后的布尔值。
    """
    parsed_value = _normalize_optional_bool(value)
    return default if parsed_value is None else parsed_value


def _coerce_datetime(value: Any, fallback_now: bool = False) -> Optional[datetime]:
    """将旧版时间字段标准化为 ``datetime``。

    Args:
        value: 待转换的原始值。
        fallback_now: 转换失败时是否回退到当前时间。

    Returns:
        Optional[datetime]: 转换后的时间对象。
    """
    if value is None or value == "":
        return datetime.now() if fallback_now else None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (OSError, OverflowError, ValueError):
            return datetime.now() if fallback_now else None

    normalized_text = str(value).strip()
    if not normalized_text:
        return datetime.now() if fallback_now else None
    try:
        return datetime.fromtimestamp(float(normalized_text))
    except (TypeError, ValueError, OSError, OverflowError):
        pass
    try:
        return datetime.fromisoformat(normalized_text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now() if fallback_now else None


def _normalize_string_list(value: Any) -> List[str]:
    """将旧版文本或 JSON 字段规范化为字符串列表。

    Args:
        value: 待标准化的原始值。

    Returns:
        List[str]: 规范化后的字符串列表。
    """
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]

    normalized_text = str(value).strip()
    if not normalized_text:
        return []
    try:
        parsed_value = json.loads(normalized_text)
    except json.JSONDecodeError:
        return [normalized_text]

    if isinstance(parsed_value, list):
        return [str(item).strip() for item in parsed_value if str(item).strip()]
    if isinstance(parsed_value, str):
        parsed_text = parsed_value.strip()
        return [parsed_text] if parsed_text else []
    if parsed_value is None:
        return []
    return [str(parsed_value).strip()]


def _normalize_json_dict_text(value: Any) -> Optional[str]:
    """将旧版附加配置标准化为 JSON 字典字符串。

    Args:
        value: 待标准化的原始值。

    Returns:
        Optional[str]: 合法的 JSON 字典字符串；若无内容则返回 ``None``。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)

    normalized_text = str(value).strip()
    if not normalized_text:
        return None
    try:
        parsed_value = json.loads(normalized_text)
    except json.JSONDecodeError:
        return json.dumps({"_legacy_additional_config_raw": normalized_text}, ensure_ascii=False)

    if isinstance(parsed_value, dict):
        return json.dumps(parsed_value, ensure_ascii=False)
    return json.dumps({"_legacy_additional_config_raw": parsed_value}, ensure_ascii=False)


def _normalize_group_cardname_json(value: Any) -> Optional[str]:
    """将旧版群昵称字段转换为当前使用的 JSON 结构。

    Args:
        value: 旧版 ``group_nick_name`` 字段值。

    Returns:
        Optional[str]: 新版 ``group_cardname`` JSON 字符串。
    """
    if value is None:
        return None

    normalized_text = str(value).strip()
    if not normalized_text:
        return None
    try:
        parsed_value = json.loads(normalized_text)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed_value, list):
        return None

    normalized_items: List[Dict[str, str]] = []
    for item in parsed_value:
        if not isinstance(item, Mapping):
            continue
        group_id = _normalize_required_text(item.get("group_id"))
        group_cardname = _normalize_required_text(item.get("group_cardname") or item.get("group_nick_name"))
        if not group_id or not group_cardname:
            continue
        normalized_items.append(
            {
                "group_id": group_id,
                "group_cardname": group_cardname,
            }
        )
    if not normalized_items:
        return None
    return json.dumps(normalized_items, ensure_ascii=False)


def _normalize_modified_by(value: Any) -> Optional[str]:
    """将旧版审核来源字段标准化为当前枚举名称。

    Args:
        value: 待标准化的原始值。

    Returns:
        Optional[str]: 若能识别则返回 ``AI`` / ``USER``，否则返回 ``None``。
    """
    normalized_text = _normalize_required_text(value).lower()
    if normalized_text in {"", "null", "none"}:
        return None
    if normalized_text in {"ai"}:
        return "AI"
    if normalized_text in {"user"}:
        return "USER"
    return None


def _build_session_id_dict(value: Any, fallback_count: int) -> str:
    """将旧版 ``chat_id`` 字段转换为新版 ``session_id_dict``。

    Args:
        value: 旧版 ``chat_id`` 字段值。
        fallback_count: 默认引用次数。

    Returns:
        str: 新版 ``session_id_dict`` JSON 字符串。
    """
    if value is None:
        return json.dumps({}, ensure_ascii=False)

    normalized_text = str(value).strip()
    if not normalized_text:
        return json.dumps({}, ensure_ascii=False)
    try:
        parsed_value = json.loads(normalized_text)
    except json.JSONDecodeError:
        return json.dumps({normalized_text: max(fallback_count, 1)}, ensure_ascii=False)

    if isinstance(parsed_value, str):
        parsed_text = parsed_value.strip()
        if not parsed_text:
            return json.dumps({}, ensure_ascii=False)
        return json.dumps({parsed_text: max(fallback_count, 1)}, ensure_ascii=False)
    if not isinstance(parsed_value, list):
        return json.dumps({}, ensure_ascii=False)

    session_counts: Dict[str, int] = {}
    for item in parsed_value:
        if not isinstance(item, list) or not item:
            continue
        session_id = _normalize_required_text(item[0])
        if not session_id:
            continue
        session_count = fallback_count
        if len(item) > 1:
            session_count = _normalize_int(item[1], default=fallback_count)
        session_counts[session_id] = max(session_count, 1)
    return json.dumps(session_counts, ensure_ascii=False)


def _build_legacy_message_additional_config(row: Mapping[str, Any]) -> Optional[str]:
    """构建新版消息表使用的附加配置 JSON。

    Args:
        row: 旧版消息表行数据。

    Returns:
        Optional[str]: 新版消息表 ``additional_config`` 字段内容。
    """
    additional_config_text = _normalize_json_dict_text(row.get("additional_config"))
    if additional_config_text:
        merged_config = json.loads(additional_config_text)
    else:
        merged_config = {}

    legacy_fields = {
        "intercept_message_level": row.get("intercept_message_level"),
        "priority_info": row.get("priority_info"),
        "priority_mode": row.get("priority_mode"),
        "selected_expressions": row.get("selected_expressions"),
    }
    for field_name, field_value in legacy_fields.items():
        if field_value is None:
            continue
        merged_config[field_name] = field_value

    if not merged_config:
        return None
    return json.dumps(merged_config, ensure_ascii=False)


def _build_message_raw_content(processed_plain_text: Optional[str], display_message: Optional[str]) -> bytes:
    """为旧版消息构造一个可被当前代码读取的占位 ``raw_content``。

    Args:
        processed_plain_text: 旧版消息的处理后文本。
        display_message: 旧版消息的展示文本。

    Returns:
        bytes: 可被当前消息模型安全反序列化的 msgpack 字节串。
    """
    message_text = _normalize_optional_text(display_message) or _normalize_optional_text(processed_plain_text)
    if not message_text:
        return cast(bytes, _EMPTY_MESSAGE_SEQUENCE_BYTES)
    serialized_payload = [{"type": "text", "data": message_text}]
    return cast(bytes, msgpack.packb(serialized_payload, use_bin_type=True))


def _deduce_image_type_name(value: Any) -> str:
    """将旧版图片类型转换为当前枚举名称。

    Args:
        value: 旧版图片类型字段值。

    Returns:
        str: 当前 ``ImageType`` 枚举在数据库中的文本值。
    """
    normalized_text = _normalize_required_text(value, default="image").lower()
    if normalized_text == "emoji":
        return "EMOJI"
    return "IMAGE"


def _count_legacy_table_rows(connection: Connection, original_table_name: str) -> int:
    """统计单张旧版备份表中的记录总数。

    Args:
        connection: 当前数据库连接。
        original_table_name: 旧版原始表名。

    Returns:
        int: 备份表中的记录数；若表不存在则返回 ``0``。
    """
    backup_table_name = _legacy_backup_table_name(original_table_name)
    schema_inspector = SQLiteSchemaInspector()
    if not schema_inspector.table_exists(connection, backup_table_name):
        return 0
    row = connection.execute(
        text(f"SELECT COUNT(*) FROM {_quote_identifier(backup_table_name)}")
    ).first()
    if row is None:
        return 0
    return _normalize_int(row[0], default=0)


def _estimate_total_record_count(connection: Connection) -> int:
    """估算旧版迁移步骤需要处理的总记录数。

    Args:
        connection: 当前数据库连接。

    Returns:
        int: 本次迁移预计处理的总记录数。
    """
    return (
        _count_legacy_table_rows(connection, "llm_usage")
        + _count_legacy_table_rows(connection, "emoji")
        + _count_legacy_table_rows(connection, "images")
        + _count_legacy_table_rows(connection, "messages")
        + _count_legacy_table_rows(connection, "online_time")
        + _count_legacy_table_rows(connection, "person_info")
    )


def _complete_table_progress(context: MigrationExecutionContext, table_name: str) -> None:
    """标记单张表的迁移已经完成。

    Args:
        context: 当前迁移步骤执行上下文。
        table_name: 已完成迁移的表名。
    """
    context.advance_progress(completed_tables=1, item_name=table_name)


def _migrate_model_usage(context: MigrationExecutionContext) -> int:
    """迁移旧版 ``llm_usage`` 到新版 ``llm_usage``。

    Args:
        context: 当前迁移步骤执行上下文。

    Returns:
        int: 迁移成功的记录数。
    """
    connection = context.connection
    legacy_table = _load_legacy_table_data(connection, "llm_usage")
    if legacy_table is None:
        _complete_table_progress(context, "llm_usage")
        return 0

    migrated_count = 0
    insert_sql = text(
        """
        INSERT OR IGNORE INTO llm_usage (
            id,
            model_name,
            model_assign_name,
            model_api_provider_name,
            endpoint,
            user_type,
            request_type,
            time_cost,
            timestamp,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            cost
        ) VALUES (
            :id,
            :model_name,
            :model_assign_name,
            :model_api_provider_name,
            :endpoint,
            :user_type,
            :request_type,
            :time_cost,
            :timestamp,
            :prompt_tokens,
            :completion_tokens,
            :total_tokens,
            :cost
        )
        """
    )
    for row in legacy_table.rows:
        connection.execute(
            insert_sql,
            {
                "id": row.get("id"),
                "model_name": _normalize_required_text(row.get("model_name"), default="unknown"),
                "model_assign_name": _normalize_optional_text(row.get("model_assign_name")),
                "model_api_provider_name": _normalize_required_text(row.get("model_api_provider"), default="unknown"),
                "endpoint": _normalize_optional_text(row.get("endpoint")),
                "user_type": "SYSTEM",
                "request_type": _normalize_required_text(row.get("request_type"), default="unknown"),
                "time_cost": _normalize_float(row.get("time_cost"), default=0.0),
                "timestamp": _coerce_datetime(row.get("timestamp"), fallback_now=True),
                "prompt_tokens": _normalize_int(row.get("prompt_tokens"), default=0),
                "completion_tokens": _normalize_int(row.get("completion_tokens"), default=0),
                "total_tokens": _normalize_int(row.get("total_tokens"), default=0),
                "cost": _normalize_float(row.get("cost"), default=0.0),
            },
        )
        migrated_count += 1
        context.advance_progress(records=1)
    _complete_table_progress(context, "llm_usage")
    return migrated_count


def _migrate_images(context: MigrationExecutionContext) -> int:
    """迁移旧版 ``emoji`` 与 ``images`` 到新版 ``images``。

    Args:
        context: 当前迁移步骤执行上下文。

    Returns:
        int: 迁移成功的记录数。
    """
    connection = context.connection
    migrated_count = 0
    existing_keys: Set[Tuple[str, str, str]] = set()
    existing_rows = connection.execute(
        text("SELECT full_path, image_hash, image_type FROM images")
    ).mappings().all()
    for row in existing_rows:
        existing_keys.add(
            (
                _normalize_required_text(row.get("full_path")),
                _normalize_required_text(row.get("image_hash")),
                _normalize_required_text(row.get("image_type")),
            )
        )
    insert_sql = text(
        """
        INSERT INTO images (
            image_hash,
            description,
            full_path,
            image_type,
            emotion,
            query_count,
            is_registered,
            is_banned,
            no_file_flag,
            record_time,
            register_time,
            last_used_time,
            vlm_processed
        ) VALUES (
            :image_hash,
            :description,
            :full_path,
            :image_type,
            :emotion,
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

    legacy_emoji_table = _load_legacy_table_data(connection, "emoji")
    if legacy_emoji_table is not None:
        for row in legacy_emoji_table.rows:
            full_path = _normalize_required_text(row.get("full_path"))
            image_hash = _normalize_required_text(row.get("emoji_hash"))
            dedupe_key = (full_path, image_hash, "EMOJI")
            if full_path and dedupe_key not in existing_keys:
                migrated_description = _normalize_required_text(row.get("description"))
                migrated_emotion = _normalize_optional_text(row.get("emotion"))
                connection.execute(
                    insert_sql,
                    {
                        "image_hash": image_hash,
                        "description": migrated_description,
                        "full_path": full_path,
                        "image_type": "EMOJI",
                        "emotion": migrated_emotion,
                        "query_count": _normalize_int(row.get("query_count"), default=0),
                        "is_registered": _normalize_bool(row.get("is_registered"), default=False),
                        "is_banned": _normalize_bool(row.get("is_banned"), default=False),
                        "no_file_flag": False,
                        "record_time": _coerce_datetime(row.get("record_time"), fallback_now=True),
                        "register_time": _coerce_datetime(row.get("register_time")),
                        "last_used_time": _coerce_datetime(row.get("last_used_time")),
                        "vlm_processed": False,
                    },
                )
                existing_keys.add(dedupe_key)
                migrated_count += 1
            context.advance_progress(records=1)

    legacy_images_table = _load_legacy_table_data(connection, "images")
    if legacy_images_table is not None:
        for row in legacy_images_table.rows:
            full_path = _normalize_required_text(row.get("path"))
            image_hash = _normalize_required_text(row.get("emoji_hash"))
            image_type = _deduce_image_type_name(row.get("type"))
            dedupe_key = (full_path, image_hash, image_type)
            if full_path and dedupe_key not in existing_keys:
                connection.execute(
                    insert_sql,
                    {
                        "image_hash": image_hash,
                        "description": _normalize_required_text(row.get("description")),
                        "full_path": full_path,
                        "image_type": image_type,
                        "emotion": None,
                        "query_count": _normalize_int(row.get("count"), default=0),
                        "is_registered": False,
                        "is_banned": False,
                        "no_file_flag": False,
                        "record_time": _coerce_datetime(row.get("timestamp"), fallback_now=True),
                        "register_time": None,
                        "last_used_time": None,
                        "vlm_processed": _normalize_bool(row.get("vlm_processed"), default=False),
                    },
                )
                existing_keys.add(dedupe_key)
                migrated_count += 1
            context.advance_progress(records=1)

    _complete_table_progress(context, "images")
    return migrated_count


def _migrate_messages(context: MigrationExecutionContext) -> int:
    """迁移旧版 ``messages`` 到新版 ``mai_messages``。

    Args:
        context: 当前迁移步骤执行上下文。

    Returns:
        int: 迁移成功的记录数。
    """
    connection = context.connection
    legacy_table = _load_legacy_table_data(connection, "messages")
    if legacy_table is None:
        _complete_table_progress(context, "mai_messages")
        return 0

    migrated_count = 0
    insert_sql = text(
        """
        INSERT OR IGNORE INTO mai_messages (
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
            display_message,
            additional_config
        ) VALUES (
            :id,
            :message_id,
            :timestamp,
            :platform,
            :user_id,
            :user_nickname,
            :user_cardname,
            :group_id,
            :group_name,
            :is_mentioned,
            :is_at,
            :session_id,
            :reply_to,
            :is_emoji,
            :is_picture,
            :is_command,
            :is_notify,
            :raw_content,
            :processed_plain_text,
            :display_message,
            :additional_config
        )
        """
    )
    for row in legacy_table.rows:
        session_id = _normalize_optional_text(row.get("chat_id")) or _normalize_optional_text(row.get("chat_info_stream_id"))
        if session_id:
            processed_plain_text = _normalize_optional_text(row.get("processed_plain_text"))
            display_message = _normalize_optional_text(row.get("display_message"))
            connection.execute(
                insert_sql,
                {
                    "id": row.get("id"),
                    "message_id": _normalize_required_text(row.get("message_id"), default=""),
                    "timestamp": _coerce_datetime(row.get("time"), fallback_now=True),
                    "platform": _normalize_required_text(
                        row.get("chat_info_platform") or row.get("user_platform"),
                        default="unknown",
                    ),
                    "user_id": _normalize_required_text(
                        row.get("user_id") or row.get("chat_info_user_id"),
                        default="",
                    ),
                    "user_nickname": _normalize_required_text(
                        row.get("user_nickname") or row.get("chat_info_user_nickname"),
                        default="",
                    ),
                    "user_cardname": _normalize_optional_text(
                        row.get("user_cardname") or row.get("chat_info_user_cardname")
                    ),
                    "group_id": _normalize_optional_text(row.get("chat_info_group_id")),
                    "group_name": _normalize_optional_text(row.get("chat_info_group_name")),
                    "is_mentioned": _normalize_bool(row.get("is_mentioned"), default=False),
                    "is_at": _normalize_bool(row.get("is_at"), default=False),
                    "session_id": session_id,
                    "reply_to": _normalize_optional_text(row.get("reply_to")),
                    "is_emoji": _normalize_bool(row.get("is_emoji"), default=False),
                    "is_picture": _normalize_bool(row.get("is_picid"), default=False),
                    "is_command": _normalize_bool(row.get("is_command"), default=False),
                    "is_notify": _normalize_bool(row.get("is_notify"), default=False),
                    "raw_content": _build_message_raw_content(processed_plain_text, display_message),
                    "processed_plain_text": processed_plain_text,
                    "display_message": display_message,
                    "additional_config": _build_legacy_message_additional_config(row),
                },
            )
            migrated_count += 1
        context.advance_progress(records=1)
    _complete_table_progress(context, "mai_messages")
    return migrated_count


def _migrate_action_records(context: MigrationExecutionContext) -> int:
    """迁移旧版 ``action_records`` 到新版 ``action_records``。

    Args:
        context: 当前迁移步骤执行上下文。

    Returns:
        int: 迁移成功的记录数。
    """
    connection = context.connection
    legacy_table = _load_legacy_table_data(connection, "action_records")
    if legacy_table is None:
        _complete_table_progress(context, "action_records")
        return 0

    migrated_count = 0
    insert_sql = text(
        """
        INSERT OR IGNORE INTO action_records (
            id,
            action_id,
            timestamp,
            session_id,
            action_name,
            action_reasoning,
            action_data,
            action_builtin_prompt,
            action_display_prompt
        ) VALUES (
            :id,
            :action_id,
            :timestamp,
            :session_id,
            :action_name,
            :action_reasoning,
            :action_data,
            :action_builtin_prompt,
            :action_display_prompt
        )
        """
    )
    for row in legacy_table.rows:
        session_id = _normalize_optional_text(row.get("chat_id")) or _normalize_optional_text(row.get("chat_info_stream_id"))
        if session_id:
            connection.execute(
                insert_sql,
                {
                    "id": row.get("id"),
                    "action_id": _normalize_required_text(row.get("action_id")),
                    "timestamp": _coerce_datetime(row.get("time"), fallback_now=True),
                    "session_id": session_id,
                    "action_name": _normalize_required_text(row.get("action_name"), default="unknown"),
                    "action_reasoning": _normalize_optional_text(row.get("action_reasoning")),
                    "action_data": _normalize_optional_text(row.get("action_data")),
                    "action_builtin_prompt": None,
                    "action_display_prompt": _normalize_optional_text(row.get("action_prompt_display")),
                },
            )
            migrated_count += 1
        context.advance_progress(records=1)
    _complete_table_progress(context, "action_records")
    return migrated_count


def _migrate_tool_records(context: MigrationExecutionContext) -> int:
    """迁移旧版 ``action_records`` 到新版 ``tool_records``。

    Args:
        context: 当前迁移步骤执行上下文。

    Returns:
        int: 迁移成功的记录数。
    """
    connection = context.connection
    legacy_table = _load_legacy_table_data(connection, "action_records")
    if legacy_table is None:
        _complete_table_progress(context, "tool_records")
        return 0

    migrated_count = 0
    insert_sql = text(
        """
        INSERT OR IGNORE INTO tool_records (
            id,
            tool_id,
            timestamp,
            session_id,
            tool_name,
            tool_reasoning,
            tool_data,
            tool_builtin_prompt,
            tool_display_prompt
        ) VALUES (
            :id,
            :tool_id,
            :timestamp,
            :session_id,
            :tool_name,
            :tool_reasoning,
            :tool_data,
            :tool_builtin_prompt,
            :tool_display_prompt
        )
        """
    )
    for row in legacy_table.rows:
        session_id = _normalize_optional_text(row.get("chat_id")) or _normalize_optional_text(row.get("chat_info_stream_id"))
        if session_id:
            connection.execute(
                insert_sql,
                {
                    "id": row.get("id"),
                    "tool_id": _normalize_required_text(row.get("action_id")),
                    "timestamp": _coerce_datetime(row.get("time"), fallback_now=True),
                    "session_id": session_id,
                    "tool_name": _normalize_required_text(row.get("action_name"), default="unknown"),
                    "tool_reasoning": _normalize_optional_text(row.get("action_reasoning")),
                    "tool_data": _normalize_optional_text(row.get("action_data")),
                    "tool_builtin_prompt": None,
                    "tool_display_prompt": _normalize_optional_text(row.get("action_prompt_display")),
                },
            )
            migrated_count += 1
        context.advance_progress(records=1)
    _complete_table_progress(context, "tool_records")
    return migrated_count


def _migrate_online_time(context: MigrationExecutionContext) -> int:
    """迁移旧版 ``online_time`` 到新版 ``online_time``。

    Args:
        context: 当前迁移步骤执行上下文。

    Returns:
        int: 迁移成功的记录数。
    """
    connection = context.connection
    legacy_table = _load_legacy_table_data(connection, "online_time")
    if legacy_table is None:
        _complete_table_progress(context, "online_time")
        return 0

    migrated_count = 0
    insert_sql = text(
        """
        INSERT OR IGNORE INTO online_time (
            id,
            timestamp,
            duration_minutes,
            start_timestamp,
            end_timestamp
        ) VALUES (
            :id,
            :timestamp,
            :duration_minutes,
            :start_timestamp,
            :end_timestamp
        )
        """
    )
    for row in legacy_table.rows:
        connection.execute(
            insert_sql,
            {
                "id": row.get("id"),
                "timestamp": _coerce_datetime(row.get("timestamp"), fallback_now=True),
                "duration_minutes": _normalize_int(row.get("duration"), default=0),
                "start_timestamp": _coerce_datetime(row.get("start_timestamp"), fallback_now=True),
                "end_timestamp": _coerce_datetime(row.get("end_timestamp"), fallback_now=True),
            },
        )
        migrated_count += 1
        context.advance_progress(records=1)
    _complete_table_progress(context, "online_time")
    return migrated_count


def _migrate_person_info(context: MigrationExecutionContext) -> int:
    """迁移旧版 ``person_info`` 到新版 ``person_info``。

    Args:
        context: 当前迁移步骤执行上下文。

    Returns:
        int: 迁移成功的记录数。
    """
    connection = context.connection
    legacy_table = _load_legacy_table_data(connection, "person_info")
    if legacy_table is None:
        _complete_table_progress(context, "person_info")
        return 0

    migrated_count = 0
    insert_sql = text(
        """
        INSERT OR IGNORE INTO person_info (
            id,
            is_known,
            person_id,
            person_name,
            name_reason,
            platform,
            user_id,
            user_nickname,
            group_cardname,
            memory_points,
            know_counts,
            first_known_time,
            last_known_time
        ) VALUES (
            :id,
            :is_known,
            :person_id,
            :person_name,
            :name_reason,
            :platform,
            :user_id,
            :user_nickname,
            :group_cardname,
            :memory_points,
            :know_counts,
            :first_known_time,
            :last_known_time
        )
        """
    )
    for row in legacy_table.rows:
        first_known_time = _coerce_datetime(row.get("know_times")) or _coerce_datetime(row.get("know_since"))
        last_known_time = _coerce_datetime(row.get("last_know")) or _coerce_datetime(row.get("know_since"))
        memory_points = _normalize_string_list(row.get("memory_points"))
        connection.execute(
            insert_sql,
            {
                "id": row.get("id"),
                "is_known": _normalize_bool(row.get("is_known"), default=False),
                "person_id": _normalize_required_text(row.get("person_id")),
                "person_name": _normalize_optional_text(row.get("person_name")),
                "name_reason": _normalize_optional_text(row.get("name_reason")),
                "platform": _normalize_required_text(row.get("platform"), default="unknown"),
                "user_id": _normalize_required_text(row.get("user_id"), default=""),
                "user_nickname": _normalize_required_text(row.get("nickname"), default=""),
                "group_cardname": _normalize_group_cardname_json(row.get("group_nick_name")),
                "memory_points": json.dumps(memory_points, ensure_ascii=False) if memory_points else None,
                "know_counts": 1 if _normalize_bool(row.get("is_known"), default=False) else 0,
                "first_known_time": first_known_time,
                "last_known_time": last_known_time,
            },
        )
        migrated_count += 1
        context.advance_progress(records=1)
    _complete_table_progress(context, "person_info")
    return migrated_count


def _migrate_expressions(context: MigrationExecutionContext) -> int:
    """跳过旧版 ``expression`` 到新版 ``expressions`` 的迁移。

    Args:
        context: 当前迁移步骤执行上下文。

    Returns:
        int: 迁移成功的记录数，旧版表达方式不迁移时固定为 0。
    """
    _complete_table_progress(context, "expressions")
    return 0


def _migrate_jargons(context: MigrationExecutionContext) -> int:
    """跳过旧版 ``jargon`` 到新版 ``jargons`` 的迁移。

    Args:
        context: 当前迁移步骤执行上下文。

    Returns:
        int: 迁移成功的记录数，旧版黑话不迁移时固定为 0。
    """
    _complete_table_progress(context, "jargons")
    return 0


def _migrate_chat_history(context: MigrationExecutionContext) -> int:
    """迁移旧版 ``chat_history`` 到新版 ``chat_history``。

    Args:
        context: 当前迁移步骤执行上下文。

    Returns:
        int: 迁移成功的记录数。
    """
    connection = context.connection
    legacy_table = _load_legacy_table_data(connection, "chat_history")
    if legacy_table is None:
        _complete_table_progress(context, "chat_history")
        return 0

    migrated_count = 0
    insert_sql = text(
        """
        INSERT OR IGNORE INTO chat_history (
            id,
            session_id,
            start_timestamp,
            end_timestamp,
            query_count,
            query_forget_count,
            original_messages,
            participants,
            theme,
            keywords,
            summary
        ) VALUES (
            :id,
            :session_id,
            :start_timestamp,
            :end_timestamp,
            :query_count,
            :query_forget_count,
            :original_messages,
            :participants,
            :theme,
            :keywords,
            :summary
        )
        """
    )
    for row in legacy_table.rows:
        session_id = _normalize_required_text(row.get("chat_id"))
        if session_id:
            connection.execute(
                insert_sql,
                {
                    "id": row.get("id"),
                    "session_id": session_id,
                    "start_timestamp": _coerce_datetime(row.get("start_time"), fallback_now=True),
                    "end_timestamp": _coerce_datetime(row.get("end_time"), fallback_now=True),
                    "query_count": _normalize_int(row.get("count"), default=0),
                    "query_forget_count": _normalize_int(row.get("forget_times"), default=0),
                    "original_messages": _normalize_required_text(row.get("original_text")),
                    "participants": _normalize_required_text(row.get("participants"), default="[]"),
                    "theme": _normalize_required_text(row.get("theme"), default=""),
                    "keywords": _normalize_required_text(row.get("keywords"), default="[]"),
                    "summary": _normalize_required_text(row.get("summary"), default=""),
                },
            )
            migrated_count += 1
        context.advance_progress(records=1)
    _complete_table_progress(context, "chat_history")
    return migrated_count


def _migrate_thinking_questions(context: MigrationExecutionContext) -> int:
    """迁移旧版 ``thinking_back`` 到新版 ``thinking_questions``。

    Args:
        context: 当前迁移步骤执行上下文。

    Returns:
        int: 迁移成功的记录数。
    """
    connection = context.connection
    legacy_table = _load_legacy_table_data(connection, "thinking_back")
    if legacy_table is None:
        _complete_table_progress(context, "thinking_questions")
        return 0

    migrated_count = 0
    insert_sql = text(
        """
        INSERT OR IGNORE INTO thinking_questions (
            id,
            question,
            context,
            found_answer,
            answer,
            thinking_steps,
            created_timestamp,
            updated_timestamp
        ) VALUES (
            :id,
            :question,
            :context,
            :found_answer,
            :answer,
            :thinking_steps,
            :created_timestamp,
            :updated_timestamp
        )
        """
    )
    for row in legacy_table.rows:
        connection.execute(
            insert_sql,
            {
                "id": row.get("id"),
                "question": _normalize_required_text(row.get("question"), default=""),
                "context": _normalize_optional_text(row.get("context")),
                "found_answer": _normalize_bool(row.get("found_answer"), default=False),
                "answer": _normalize_optional_text(row.get("answer")),
                "thinking_steps": _normalize_optional_text(row.get("thinking_steps")),
                "created_timestamp": _coerce_datetime(row.get("create_time"), fallback_now=True),
                "updated_timestamp": _coerce_datetime(row.get("update_time"), fallback_now=True),
            },
        )
        migrated_count += 1
        context.advance_progress(records=1)
    _complete_table_progress(context, "thinking_questions")
    return migrated_count
