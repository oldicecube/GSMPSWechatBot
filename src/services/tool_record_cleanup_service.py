"""工具记录维护收尾任务。"""

from __future__ import annotations

from dataclasses import dataclass
from json import dumps, loads
from sqlite3 import OperationalError, connect
from time import perf_counter
from typing import Any

from sqlalchemy import text

from src.common.database.database import ROOT_PATH, get_db_session
from src.common.logger import get_logger

logger = get_logger("tool_record_cleanup_service")

_TASK_NAME = "tool_record_prompt_payload_cleanup_v1"
_PHASE_AWAITING_VACUUM = "awaiting_vacuum"
_PHASE_DONE = "done"
_STATUS_RUNNING = "running"
_STATUS_DONE = "done"
_STATUS_FAILED = "failed"
_VACUUM_BUSY_TIMEOUT_MS = 30000


@dataclass(frozen=True)
class MaintenanceTaskState:
    """一次性维护任务状态。"""

    phase: str
    status: str
    cursor_id: int = 0
    scanned_records: int = 0
    updated_records: int = 0


def _load_stats(raw_stats: Any) -> dict[str, int]:
    if not isinstance(raw_stats, str) or not raw_stats.strip():
        return {"scanned_records": 0, "updated_records": 0}
    try:
        stats = loads(raw_stats)
    except Exception:
        return {"scanned_records": 0, "updated_records": 0}
    if not isinstance(stats, dict):
        return {"scanned_records": 0, "updated_records": 0}
    return {
        "scanned_records": int(stats.get("scanned_records") or 0),
        "updated_records": int(stats.get("updated_records") or 0),
    }


def _dump_stats(scanned_records: int, updated_records: int) -> str:
    return dumps(
        {
            "scanned_records": scanned_records,
            "updated_records": updated_records,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _get_state(session: Any) -> MaintenanceTaskState:
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
        return MaintenanceTaskState(phase=_PHASE_DONE, status=_STATUS_DONE)

    stats = _load_stats(row[3])
    return MaintenanceTaskState(
        phase=str(row[0] or _PHASE_DONE),
        status=str(row[1] or _STATUS_DONE),
        cursor_id=int(row[2] or 0),
        scanned_records=stats["scanned_records"],
        updated_records=stats["updated_records"],
    )


def _save_state(
    session: Any,
    *,
    phase: str,
    status: str,
    cursor_id: int,
    scanned_records: int,
    updated_records: int,
    completed: bool = False,
    last_error: str = "",
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
                :last_error,
                CASE WHEN :completed = 1 THEN CURRENT_TIMESTAMP ELSE NULL END,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT(task_name) DO UPDATE SET
                phase = excluded.phase,
                status = excluded.status,
                cursor_id = excluded.cursor_id,
                stats_json = excluded.stats_json,
                last_error = excluded.last_error,
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
            "stats_json": _dump_stats(scanned_records, updated_records),
            "last_error": last_error[:1000] if last_error else None,
            "completed": 1 if completed else 0,
        },
    )


def run_startup_tool_record_vacuum_if_needed() -> bool:
    """启动早期执行待完成的 VACUUM，完成后主程序才能继续启动。"""

    with get_db_session(auto_commit=False) as session:
        state = _get_state(session)
        if state.phase != _PHASE_AWAITING_VACUUM:
            session.commit()
            return state.phase == _PHASE_DONE
        _save_state(
            session,
            phase=_PHASE_AWAITING_VACUUM,
            status=_STATUS_RUNNING,
            cursor_id=state.cursor_id,
            scanned_records=state.scanned_records,
            updated_records=state.updated_records,
        )
        session.commit()

    db_path = ROOT_PATH / "data" / "MaiBot.db"
    logger.info(
        "工具记录维护 VACUUM 开始："
        f"数据库={db_path}，清理候选 {state.scanned_records} 条，更新 {state.updated_records} 条"
    )
    vacuum_start_time = perf_counter()
    try:
        connection = connect(str(db_path), timeout=_VACUUM_BUSY_TIMEOUT_MS / 1000)
        try:
            connection.execute(f"PRAGMA busy_timeout={_VACUUM_BUSY_TIMEOUT_MS}")
            connection.execute("VACUUM")
            connection.commit()
            logger.info(f"工具记录维护 VACUUM 主体完成，耗时={int((perf_counter() - vacuum_start_time) * 1000)}ms")
            checkpoint_result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint_result is not None and int(checkpoint_result[0] or 0) != 0:
                raise OperationalError(f"VACUUM 后 WAL checkpoint 未完成: {checkpoint_result}")
            logger.info(f"工具记录维护 WAL checkpoint 完成：{checkpoint_result}")
        finally:
            connection.close()
    except Exception as exc:
        message = f"工具记录维护 VACUUM 失败，请关闭占用数据库的程序后重启: {exc}"
        with get_db_session(auto_commit=False) as session:
            latest_state = _get_state(session)
            _save_state(
                session,
                phase=_PHASE_AWAITING_VACUUM,
                status=_STATUS_FAILED,
                cursor_id=latest_state.cursor_id,
                scanned_records=latest_state.scanned_records,
                updated_records=latest_state.updated_records,
                last_error=message,
            )
            session.commit()
        raise RuntimeError(message) from exc

    with get_db_session(auto_commit=False) as session:
        latest_state = _get_state(session)
        _save_state(
            session,
            phase=_PHASE_DONE,
            status=_STATUS_DONE,
            cursor_id=latest_state.cursor_id,
            scanned_records=latest_state.scanned_records,
            updated_records=latest_state.updated_records,
            completed=True,
        )
        session.commit()
    logger.info(
        f"工具记录维护 VACUUM 结束，总耗时={int((perf_counter() - vacuum_start_time) * 1000)}ms，继续启动主程序"
    )
    return True
