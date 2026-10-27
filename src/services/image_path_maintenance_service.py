"""图片存储路径一次性规整任务。"""

from __future__ import annotations

from dataclasses import dataclass
from json import dumps, loads
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    ProgressColumn,
    Task,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text
from sqlalchemy import text

import asyncio

from src.common.database.database import get_db_session
from src.common.logger import get_logger
from src.common.utils.image_path import PROJECT_ROOT, serialize_stored_image_path

logger = get_logger("image_path_maintenance_service")

_TASK_NAME = "image_storage_path_normalization_v1"
_PHASE_NORMALIZE = "normalize"
_PHASE_DONE = "done"
_STATUS_PENDING = "pending"
_STATUS_RUNNING = "running"
_STATUS_DONE = "done"
_BATCH_SIZE = 100
_PROGRESS_LOG_INTERVAL = 1000


class ImagePathMaintenanceSpeedColumn(ProgressColumn):
    """渲染图片路径规整速度。"""

    def render(self, task: Task) -> Text:
        if task.speed is None or task.speed <= 0:
            return Text("-- 条/s")
        return Text(f"{task.speed:.2f} 条/s")


@dataclass(frozen=True)
class ImagePathMaintenanceState:
    """图片路径规整任务状态。"""

    phase: str
    status: str
    cursor_id: int = 0
    scanned_records: int = 0
    converted_records: int = 0
    discarded_records: int = 0


@dataclass(frozen=True)
class ImagePathMaintenanceBatchResult:
    """单批图片路径规整结果。"""

    completed: bool
    scanned_records: int = 0
    converted_records: int = 0
    discarded_records: int = 0
    skipped_records: int = 0
    last_processed_id: int = 0
    total_scanned_records: int = 0
    total_converted_records: int = 0
    total_discarded_records: int = 0


def _load_stats(raw_stats: Any) -> dict[str, int]:
    if not isinstance(raw_stats, str) or not raw_stats.strip():
        return {
            "scanned_records": 0,
            "converted_records": 0,
            "discarded_records": 0,
        }
    try:
        stats = loads(raw_stats)
    except Exception:
        return {
            "scanned_records": 0,
            "converted_records": 0,
            "discarded_records": 0,
        }
    if not isinstance(stats, dict):
        return {
            "scanned_records": 0,
            "converted_records": 0,
            "discarded_records": 0,
        }
    return {
        "scanned_records": int(stats.get("scanned_records") or 0),
        "converted_records": int(stats.get("converted_records") or 0),
        "discarded_records": int(stats.get("discarded_records") or 0),
    }


def _dump_stats(scanned_records: int, converted_records: int, discarded_records: int) -> str:
    return dumps(
        {
            "scanned_records": scanned_records,
            "converted_records": converted_records,
            "discarded_records": discarded_records,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _get_state(session: Any) -> ImagePathMaintenanceState:
    row = session.exec(
        text(
            """
            SELECT phase, status, cursor_id, stats_json
            FROM one_time_maintenance_tasks
            WHERE task_name = :task_name
            """
        ),
        params={"task_name": _TASK_NAME},
    ).first()
    if row is None:
        return ImagePathMaintenanceState(phase=_PHASE_NORMALIZE, status=_STATUS_PENDING)

    stats = _load_stats(row[3])
    return ImagePathMaintenanceState(
        phase=str(row[0] or _PHASE_NORMALIZE),
        status=str(row[1] or _STATUS_PENDING),
        cursor_id=int(row[2] or 0),
        scanned_records=stats["scanned_records"],
        converted_records=stats["converted_records"],
        discarded_records=stats["discarded_records"],
    )


def _save_state(
    session: Any,
    *,
    phase: str,
    status: str,
    cursor_id: int,
    scanned_records: int,
    converted_records: int,
    discarded_records: int,
    completed: bool = False,
) -> None:
    session.exec(
        text(
            """
            INSERT INTO one_time_maintenance_tasks (
                task_name, phase, status, cursor_id, stats_json,
                last_error, completed_at, updated_at
            )
            VALUES (
                :task_name, :phase, :status, :cursor_id, :stats_json,
                NULL,
                CASE WHEN :completed = 1 THEN CURRENT_TIMESTAMP ELSE NULL END,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT(task_name) DO UPDATE SET
                phase = excluded.phase,
                status = excluded.status,
                cursor_id = excluded.cursor_id,
                stats_json = excluded.stats_json,
                last_error = NULL,
                completed_at = CASE
                    WHEN :completed = 1 THEN CURRENT_TIMESTAMP
                    ELSE one_time_maintenance_tasks.completed_at
                END,
                updated_at = excluded.updated_at
            """
        ),
        params={
            "task_name": _TASK_NAME,
            "phase": phase,
            "status": status,
            "cursor_id": cursor_id,
            "stats_json": _dump_stats(scanned_records, converted_records, discarded_records),
            "completed": 1 if completed else 0,
        },
    )


def _images_table_exists(session: Any) -> bool:
    row = session.exec(
        text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'images'")
    ).first()
    return row is not None


def _fetch_image_path_rows(session: Any, cursor_id: int) -> list[tuple[int, str]]:
    rows = session.exec(
        text(
            """
            SELECT id, full_path
            FROM images
            WHERE id > :cursor_id
              AND full_path IS NOT NULL
            ORDER BY id ASC
            LIMIT :batch_size
            """
        ),
        params={"cursor_id": cursor_id, "batch_size": _BATCH_SIZE},
    ).all()
    return [(int(row[0]), str(row[1] or "")) for row in rows]


def _count_remaining_image_path_rows(session: Any, cursor_id: int) -> int:
    row = session.exec(
        text(
            """
            SELECT COUNT(*)
            FROM images
            WHERE id > :cursor_id
              AND full_path IS NOT NULL
            """
        ),
        params={"cursor_id": cursor_id},
    ).first()
    return int(row[0] or 0) if row is not None else 0


def _normalize_stored_image_path(raw_path: str) -> str | None:
    path = Path(raw_path)
    resolved_path = path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    try:
        resolved_path.relative_to(PROJECT_ROOT)
    except ValueError:
        return None
    return serialize_stored_image_path(resolved_path)


def _mark_done(session: Any, state: ImagePathMaintenanceState) -> ImagePathMaintenanceState:
    next_state = ImagePathMaintenanceState(
        phase=_PHASE_DONE,
        status=_STATUS_DONE,
        cursor_id=state.cursor_id,
        scanned_records=state.scanned_records,
        converted_records=state.converted_records,
        discarded_records=state.discarded_records,
    )
    _save_state(
        session,
        phase=next_state.phase,
        status=next_state.status,
        cursor_id=next_state.cursor_id,
        scanned_records=next_state.scanned_records,
        converted_records=next_state.converted_records,
        discarded_records=next_state.discarded_records,
        completed=True,
    )
    return next_state


def run_image_path_maintenance_batch() -> ImagePathMaintenanceBatchResult:
    """执行一批图片路径规整。"""

    with get_db_session(auto_commit=False) as session:
        state = _get_state(session)
        if state.phase != _PHASE_NORMALIZE:
            session.commit()
            return ImagePathMaintenanceBatchResult(
                completed=True,
                last_processed_id=state.cursor_id,
                total_scanned_records=state.scanned_records,
                total_converted_records=state.converted_records,
                total_discarded_records=state.discarded_records,
            )

        if not _images_table_exists(session):
            next_state = _mark_done(session, state)
            session.commit()
            return ImagePathMaintenanceBatchResult(
                completed=True,
                last_processed_id=next_state.cursor_id,
                total_scanned_records=next_state.scanned_records,
                total_converted_records=next_state.converted_records,
                total_discarded_records=next_state.discarded_records,
            )

        rows = _fetch_image_path_rows(session, state.cursor_id)
        if not rows:
            next_state = _mark_done(session, state)
            session.commit()
            return ImagePathMaintenanceBatchResult(
                completed=True,
                last_processed_id=next_state.cursor_id,
                total_scanned_records=next_state.scanned_records,
                total_converted_records=next_state.converted_records,
                total_discarded_records=next_state.discarded_records,
            )

        converted_records = 0
        discarded_records = 0
        skipped_records = 0
        batch_last_id = state.cursor_id
        for record_id, raw_path in rows:
            batch_last_id = record_id
            normalized_raw_path = raw_path.strip()
            if not normalized_raw_path:
                skipped_records += 1
                continue

            normalized_path = _normalize_stored_image_path(normalized_raw_path)
            if normalized_path is None:
                session.exec(text("DELETE FROM images WHERE id = :record_id"), params={"record_id": record_id})
                discarded_records += 1
                continue

            if normalized_path == raw_path:
                skipped_records += 1
                continue

            session.exec(
                text("UPDATE images SET full_path = :full_path WHERE id = :record_id"),
                params={"full_path": normalized_path, "record_id": record_id},
            )
            converted_records += 1

        scanned_records = len(rows)
        total_scanned = state.scanned_records + scanned_records
        total_converted = state.converted_records + converted_records
        total_discarded = state.discarded_records + discarded_records
        _save_state(
            session,
            phase=_PHASE_NORMALIZE,
            status=_STATUS_RUNNING,
            cursor_id=batch_last_id,
            scanned_records=total_scanned,
            converted_records=total_converted,
            discarded_records=total_discarded,
        )
        session.commit()
        return ImagePathMaintenanceBatchResult(
            completed=False,
            scanned_records=scanned_records,
            converted_records=converted_records,
            discarded_records=discarded_records,
            skipped_records=skipped_records,
            last_processed_id=batch_last_id,
            total_scanned_records=total_scanned,
            total_converted_records=total_converted,
            total_discarded_records=total_discarded,
        )


def get_image_path_maintenance_state() -> ImagePathMaintenanceState:
    """读取图片路径规整任务状态。"""

    with get_db_session(auto_commit=False) as session:
        state = _get_state(session)
        session.commit()
        return state


def count_remaining_image_path_maintenance_records() -> int:
    """统计当前图片路径规整任务剩余待检查记录数。"""

    with get_db_session(auto_commit=False) as session:
        state = _get_state(session)
        if state.phase != _PHASE_NORMALIZE or not _images_table_exists(session):
            session.commit()
            return 0
        remaining_records = _count_remaining_image_path_rows(session, state.cursor_id)
        session.commit()
        return remaining_records


def should_schedule_image_path_maintenance_background() -> bool:
    """判断是否需要调度图片路径规整后台任务。"""

    with get_db_session(auto_commit=False) as session:
        state = _get_state(session)
        session.commit()
        return state.phase == _PHASE_NORMALIZE


async def run_image_path_maintenance_background() -> None:
    """后台执行可中断的图片路径规整。"""

    initial_state = await asyncio.to_thread(get_image_path_maintenance_state)
    remaining_records = await asyncio.to_thread(count_remaining_image_path_maintenance_records)
    logger.info(
        "图片路径规整一次性维护开始："
        f"从 images.id > {initial_state.cursor_id} 继续，"
        f"已检查 {initial_state.scanned_records} 条，转换 {initial_state.converted_records} 条，"
        f"丢弃 {initial_state.discarded_records} 条，剩余约 {remaining_records} 条，批量大小 {_BATCH_SIZE}"
    )
    console = Console()
    progress_disabled = not console.is_terminal
    with Progress(
        "{task.description}",
        BarColumn(),
        MofNCompleteColumn(),
        ImagePathMaintenanceSpeedColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
        disable=progress_disabled,
        expand=True,
    ) as progress:
        task_id = progress.add_task("图片路径规整进度", total=max(remaining_records, 1))
        while True:
            try:
                result = await asyncio.to_thread(run_image_path_maintenance_batch)
            except asyncio.CancelledError:
                logger.info("图片路径规整一次性维护已中断，下次启动后继续")
                raise
            except Exception as exc:
                logger.warning(f"图片路径规整批次失败，将在下次启动后继续: {exc}")
                return

            if result.completed:
                if remaining_records == 0:
                    progress.update(task_id, completed=1)
                total_skipped = (
                    result.total_scanned_records
                    - result.total_converted_records
                    - result.total_discarded_records
                )
                logger.info(
                    "图片路径规整一次性维护完成："
                    f"累计检查 {result.total_scanned_records} 条，"
                    f"转换 {result.total_converted_records} 条，丢弃 {result.total_discarded_records} 条，"
                    f"跳过 {total_skipped} 条，最后处理 id={result.last_processed_id}"
                )
                return

            progress_task = progress.tasks[task_id]
            progressed_records = int(progress_task.completed) + result.scanned_records
            if progress_task.total is not None and progressed_records > progress_task.total:
                progress.update(task_id, total=progressed_records)
            progress.update(task_id, advance=result.scanned_records)
            if result.total_scanned_records % _PROGRESS_LOG_INTERVAL < result.scanned_records:
                total_skipped = (
                    result.total_scanned_records
                    - result.total_converted_records
                    - result.total_discarded_records
                )
                logger.info(
                    "图片路径规整进行中："
                    f"累计检查 {result.total_scanned_records} 条，"
                    f"转换 {result.total_converted_records} 条，丢弃 {result.total_discarded_records} 条，"
                    f"跳过 {total_skipped} 条；本批检查 {result.scanned_records} 条，"
                    f"转换 {result.converted_records} 条，丢弃 {result.discarded_records} 条，"
                    f"跳过 {result.skipped_records} 条，进度 id={result.last_processed_id}"
                )
