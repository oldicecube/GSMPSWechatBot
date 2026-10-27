from __future__ import annotations

from src.common.logger import get_logger

from .models import MigrationExecutionContext

logger = get_logger("database_migration")


_REMOVED_SCENE_NODE_TABLES = (
    "behavior_experience_scene_links",
    "behavior_scene_node_tags",
    "behavior_scene_edges",
    "behavior_scene_nodes",
)


def migrate_v25_to_v26(context: MigrationExecutionContext) -> None:
    """移除行为学习中不再持久化的 scene node 图层。"""

    context.start_progress(
        total_tables=len(_REMOVED_SCENE_NODE_TABLES),
        total_records=0,
        description="v25 -> v26 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )
    for table_name in _REMOVED_SCENE_NODE_TABLES:
        context.connection.exec_driver_sql(f"DROP TABLE IF EXISTS {table_name}")
        context.advance_progress(records=0, completed_tables=1, item_name=table_name)

    logger.info("v25 -> v26 数据库迁移完成：已移除行为 scene node 图层相关表")
