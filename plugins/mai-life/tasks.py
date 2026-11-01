"""当前聊天流单次任务的确定性操作，不调用模型重写任务列表。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import math

from .store import LifeStore, ShareItem, new_task_id, set_task_state, task_due_at, task_signature, task_state
from .times import parse_date, parse_task_datetime

_STATE_LABELS = {
    "pending": "待处理", "submitting": "正在提交规划器", "submitted": "已提交规划器（不等于已发言）",
    "skipped": "已跳过", "cancelled": "已取消", "consumed": "已消费（结果未知）",
}
_SOURCE_LABELS = {"generated": "自动分享", "planner": "规划器安排/接管", "legacy": "旧任务（来源未知）"}


def task_record(owner_date: str, item: ShareItem, timezone: str) -> dict[str, Any]:
    """工具返回的身份、逻辑时刻和状态，不暴露其他流的任务。"""

    try:
        due = task_due_at(item, owner_date).isoformat(timespec="seconds")
    except ValueError:
        due = item.due_at
    state = task_state(item)
    return {
        "task_id": item.id,
        "owner_date": owner_date,
        "run_at": due,
        "timezone": timezone,
        "title": item.title,
        "intent": item.intent,
        "source": item.source,
        "state": state,
        "state_reason": item.state_reason,
        "request_id": item.request_id,
    }


def format_task_records(records: list[dict[str, Any]]) -> str:
    if not records:
        return "（无）"
    return "\n".join(
        f"- {item['run_at']} {item['title']} "
        f"[{_SOURCE_LABELS[item['source']]} / {_STATE_LABELS[item['state']]}] "
        f"task_id={item['task_id']} owner_date={item['owner_date']}"
        + (f" | 意图：{item['intent']}" if item["intent"] else "")
        + (f" | 原因：{item['state_reason']}" if item["state_reason"] else "")
        for item in records
    )


def _string(value: Any, name: str, *, required: bool = False, limit: int = 4000) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{name} 必须是字符串")
    text = value.strip()
    if required and not text:
        raise ValueError(f"{name} 不能为空")
    if len(text) > limit:
        raise ValueError(f"{name} 过长（最多 {limit} 字符）")
    return text


def resolve_task_time(run_at: str, delay_minutes: Any, now: datetime) -> datetime | None:
    """时间二选一，绝对时间不推断明天，相对时间保持首次幂等结果。"""

    if run_at and delay_minutes is not None:
        raise ValueError("run_at 与 delay_minutes 只能提供一个")
    if run_at:
        return parse_task_datetime(run_at)
    if delay_minutes is None:
        return None
    if isinstance(delay_minutes, bool) or not isinstance(delay_minutes, (int, float)):
        raise ValueError("delay_minutes 必须是大于 0 的数字")
    try:
        delay = float(delay_minutes)
        if not math.isfinite(delay) or delay <= 0:
            raise ValueError("delay_minutes 必须是有限且大于 0 的数字")
        return (now + timedelta(minutes=delay)).replace(microsecond=0)
    except (OverflowError, TypeError) as exc:
        raise ValueError("delay_minutes 超出可表示的日期范围") from exc


class TaskManager:
    """保留 shares 文档和原始归属日期，修改不搬迁文件。"""

    def __init__(self, store: LifeStore) -> None:
        self.store = store

    def list(self, stream_id: str, timezone: str, date: str = "") -> list[dict[str, Any]]:
        if not stream_id:
            raise ValueError("无法获取当前聊天流，不能查询其他流任务")
        if date:
            parse_date(date)
        return [
            task_record(owner_date, item, timezone)
            for owner_date, item in self.store.list_tasks(
                stream_id=stream_id, pending_only=not date, due_date=date,
            )
        ]

    def manage(
        self, *, action: str, stream_id: str, now: datetime, timezone: str,
        date: str = "", task_id: str = "", title: str = "", intent: str = "",
        run_at: str = "", delay_minutes: Any = None, request_id: str = "",
        request: str = "", wake_planner: bool = True,
    ) -> dict[str, Any]:
        if not stream_id:
            raise ValueError("无法获取 Host 提供的当前聊天流，不能创建或修改任务")
        if action not in {"list", "add", "update", "delete"}:
            raise ValueError("target=share 只支持 list/add/update/delete，暂不支持周期或 replace_day")
        date = _string(date, "date")
        if date:
            parse_date(date)
        task_id = _string(task_id, "task_id", limit=200)
        title = _string(title, "title", limit=200)
        intent = _string(intent, "intent")
        request_id = _string(request_id, "request_id", limit=128)
        run_at = _string(run_at, "run_at")
        if _string(request, "request"):
            raise ValueError("定时任务不再用 request 自由文本改写列表。请使用 task_id/title/intent/run_at 或 delay_minutes")
        due = resolve_task_time(run_at, delay_minutes, now)
        if action == "list":
            if task_id or title or intent or due is not None or request_id:
                raise ValueError("list 只接受可选 date；默认列出当前流跨日期待处理任务")
            records = self.list(stream_id, timezone, date)
            return {"success": True, "operation": "list", "tasks": records, "content": format_task_records(records)}
        if action == "add":
            if task_id or date:
                raise ValueError("add 不接受 task_id/date；到期日期写在 run_at，原始归属日期由插件确定")
            if not title or not intent:
                raise ValueError("新增任务必须提供 title 和 intent（到点后规划器要做什么）")
            if due is None:
                raise ValueError("新增任务必须提供 run_at 或 delay_minutes，且只支持单次任务")
            item = ShareItem(
                id=new_task_id(), time=due.strftime("%H:%M"), title=title, intent=intent,
                stream_id=stream_id, source="planner", due_at=due.isoformat(timespec="seconds"),
                request_id=request_id,
            )
            owner, saved, duplicate = self.store.add_task(
                now.strftime("%Y-%m-%d"), item, now, request_spec={
                    "intent": intent, "run_at": due.isoformat(timespec="seconds") if run_at else "",
                    "delay_minutes": float(delay_minutes) if delay_minutes is not None else None,
                },
            )
            return self._result("existing" if duplicate else "created", owner, saved, timezone, wake_planner)
        if not task_id:
            raise ValueError("修改/取消任务必须提供 task_id；先 list 或 query 获取任务身份")
        if request_id:
            raise ValueError("request_id 只用于 add 的重试幂等，不用于重新激活已有任务")
        if action == "delete" and (title or intent or due is not None):
            raise ValueError("delete 仅接受 task_id 和可选原始归属 date")
        if action == "update" and not (title or intent or due is not None):
            raise ValueError("update 至少提供 title、intent 或新时间之一")
        if due is not None and due <= now:
            raise ValueError("新时间必须在未来；不会自动挪到明天")

        def change(item: ShareItem, owner_date: str) -> None:
            if task_state(item) != "pending":
                raise ValueError("任务已提交、正在提交或已结束，不能修改/取消或重新激活；需要时另建任务")
            if item.source == "generated" and not item.generation_key:
                item.generation_key = task_signature(item, owner_date)
            if action == "delete":
                set_task_state(item, "cancelled", "planner_cancelled", now)
                return
            if due is None and task_due_at(item, owner_date) <= now:
                raise ValueError("任务已到期，请重新指定未来时间，或直接取消")
            if title:
                item.title = title
            if intent:
                item.intent = intent
            if due is not None:
                item.due_at = due.isoformat(timespec="seconds")
                item.time = due.strftime("%H:%M")
            elif not item.due_at:
                item.due_at = task_due_at(item, owner_date).isoformat(timespec="seconds")
            if not item.intent:
                raise ValueError("接管旧分享时请同时提供 intent，明确到点后规划器应做什么")
            item.source = "planner"
            item.state_reason = ""

        owner, saved = self.store.edit_task(stream_id, task_id, date, change)
        return self._result("cancelled" if action == "delete" else "updated", owner, saved, timezone, wake_planner)

    @staticmethod
    def _result(operation: str, owner: str, item: ShareItem, timezone: str, wake_planner: bool) -> dict[str, Any]:
        record = task_record(owner, item, timezone)
        content = f"任务操作：{operation}\n{format_task_records([record])}"
        result: dict[str, Any] = {"success": True, "operation": operation, "task": record, "content": content}
        if not wake_planner and task_state(item) == "pending":
            warning = "share.wake_planner=false：任务已保存，但不会自动唤醒规划器"
            result["warning"] = warning
            result["content"] += f"\n{warning}"
        if task_state(item) == "pending":
            result["content"] += "\n到期按巡检间隔与同流冷却提交规划器，不保证秒级准点或消息已发出。"
        return result
