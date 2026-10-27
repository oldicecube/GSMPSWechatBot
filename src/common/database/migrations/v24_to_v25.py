"""v24 到 v25 schema 迁移：场景簇只保留 domain 分布。"""

from __future__ import annotations

from typing import Any

from sqlalchemy.engine import Connection

import json

from src.common.logger import get_logger

from .models import MigrationExecutionContext

logger = get_logger("database_migration")


def migrate_v24_to_v25(context: MigrationExecutionContext) -> None:
    """将 behavior_scene_clusters.tag_distribution 规整为 domain-only。"""

    context.start_progress(
        total_tables=1,
        total_records=0,
        description="v24 -> v25 迁移进度",
        table_unit_name="表",
        record_unit_name="记录",
    )
    migrated_count = _normalize_scene_cluster_distributions(context.connection)
    context.advance_progress(records=migrated_count, completed_tables=1, item_name="behavior_scene_clusters")
    logger.info(f"v24 -> v25 数据库迁移完成：已规整 {migrated_count} 个 domain-only 行为场景簇")


def _normalize_scene_cluster_distributions(connection: Connection) -> int:
    if not _has_table(connection, "behavior_scene_clusters"):
        return 0
    rows = connection.exec_driver_sql(
        "SELECT id, tag_distribution FROM behavior_scene_clusters ORDER BY id"
    ).fetchall()
    migrated_count = 0
    for cluster_id, raw_distribution in rows:
        domain_distribution = _domain_only_distribution(raw_distribution)
        connection.exec_driver_sql(
            "UPDATE behavior_scene_clusters SET tag_distribution = ? WHERE id = ?",
            (json.dumps(domain_distribution, ensure_ascii=False, sort_keys=True), int(cluster_id)),
        )
        migrated_count += 1
    return migrated_count


def _domain_only_distribution(raw_distribution: Any) -> list[dict[str, float | str]]:
    if not isinstance(raw_distribution, str) or not raw_distribution.strip():
        return []
    try:
        parsed_distribution = json.loads(raw_distribution)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed_distribution, list):
        return []

    probabilities: dict[str, float] = {}
    for item in parsed_distribution:
        if not isinstance(item, dict):
            continue
        tag = str(item.get("tag") or "").strip()
        if ":" not in tag:
            continue
        tag_kind, cluster_key = tag.split(":", 1)
        if tag_kind.strip().lower() != "domain" or not cluster_key.strip():
            continue
        try:
            probability = float(item.get("probability") or 0.0)
        except (TypeError, ValueError):
            continue
        if probability <= 0:
            continue
        normalized_tag = f"domain:{cluster_key.strip()}"
        probabilities[normalized_tag] = probabilities.get(normalized_tag, 0.0) + probability

    total_probability = sum(probabilities.values())
    if total_probability <= 0:
        return []
    return [
        {
            "tag": tag,
            "probability": round(probability / total_probability, 6),
        }
        for tag, probability in sorted(probabilities.items())
    ]


def _has_table(connection: Connection, table_name: str) -> bool:
    exists = connection.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return exists is not None
