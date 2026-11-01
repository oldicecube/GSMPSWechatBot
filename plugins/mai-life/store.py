"""日程与统一定时任务持久化。"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, TypeVar
from uuid import uuid4
import json
import logging
import os
import tempfile

from .times import combine_on_date, is_valid_hhmm, parse_date, parse_task_datetime

logger = logging.getLogger("mailife.mai-life")

TASK_STATES = {"pending", "submitting", "submitted", "skipped", "cancelled", "consumed"}
TASK_SOURCES = {"generated", "planner", "legacy"}
_TERMINAL_STATES = {"submitted", "skipped", "cancelled", "consumed"}
_T = TypeVar("_T")


def _text(value: Any) -> str:
    return str(value or "").strip()


@dataclass
class Activity:
    """一条日程活动。"""

    id: str
    start: str
    end: str
    title: str
    notes: str = ""

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class ShareItem:
    """自动分享或规划器安排的单次意图；fired 仅作旧格式兼容。"""

    id: str
    time: str
    title: str
    hint: str = ""
    intent: str = ""
    stream_id: str = ""
    fired: bool = False
    fired_at: str = ""
    source: str = "legacy"
    due_at: str = ""
    state: str = "pending"
    state_reason: str = ""
    request_id: str = ""
    request_bindings: dict[str, dict[str, Any]] = field(default_factory=dict)
    generation_key: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def task_state(item: ShareItem) -> str:
    """旧 fired=true 只能解释为已消费，不能推断消息已经发出。"""

    if item.fired and item.state in {"pending", "submitting"}:
        return "consumed"
    return item.state


def task_due_at(item: ShareItem, owner_date: str) -> datetime:
    """新任务用完整时刻；旧任务的 HH:MM 必须落在源文档日期。"""

    if item.due_at:
        return parse_task_datetime(item.due_at)
    return combine_on_date(parse_date(owner_date), item.time)


def task_signature(item: ShareItem, owner_date: str) -> str:
    """自动分享的原始签名用于保护改期/接管，不以易变 ID 判断同一意图。"""

    return json.dumps([task_due_at(item, owner_date).isoformat(timespec="seconds"), item.intent or item.title], ensure_ascii=False)


def set_task_state(item: ShareItem, state: str, reason: str, now: datetime) -> None:
    if state not in TASK_STATES:
        raise ValueError("未知任务状态")
    item.state = state
    item.state_reason = reason
    item.fired = state in _TERMINAL_STATES
    item.fired_at = now.isoformat(timespec="seconds") if item.fired else ""


@dataclass
class LifeDocument:
    """某一天的生活记录；任务改期后仍保留在最初归属文档。"""

    date: str
    generated_at: str
    activities: list[Activity] = field(default_factory=list)
    shares: list[ShareItem] = field(default_factory=list)
    generated_streams: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in self.shares:
            grouped.setdefault(item.stream_id, []).append(item.to_dict())
        return {
            "date": self.date,
            "generated_at": self.generated_at,
            "activities": [item.to_dict() for item in self.activities],
            "shares": [item.to_dict() for item in self.shares],
            "shares_by_stream": grouped,
            "generated_streams": list(self.generated_streams),
        }


def shares_for_stream(document: LifeDocument, stream_id: str) -> list[ShareItem]:
    """取出某个聊天流的统一任务。"""

    stream_id = _text(stream_id)
    return [item for item in document.shares if item.stream_id == stream_id]


def replace_stream_shares(document: LifeDocument, stream_id: str, shares: list[ShareItem]) -> None:
    """兼容旧调用：替换一个流的任务。新生成合并不使用此全量接口。"""

    stream_id = _text(stream_id)
    kept = [item for item in document.shares if item.stream_id != stream_id]
    for item in shares:
        item.stream_id = stream_id
    document.shares = kept + shares


def stream_generation_done(document: LifeDocument, stream_id: str) -> bool:
    """planner 任务不代表自动分享已生成；旧任务存在时保守保护。"""

    return stream_id in document.generated_streams or any(
        item.source in {"generated", "legacy"}
        for item in shares_for_stream(document, stream_id)
    )


def make_item_id(prefix: str, time_str: str, index: int) -> str:
    """兼容旧活动及未提供 ID 的旧 JSON。"""

    compact = _text(time_str).replace(":", "")
    return f"{prefix}_{compact}_{index:02d}"


def new_task_id() -> str:
    return f"task_{uuid4().hex}"


def activity_from_dict(raw: dict[str, Any], index: int) -> Activity | None:
    """从字典构造活动。缺字段则返回 None。"""

    start = _text(raw.get("start"))
    end = _text(raw.get("end"))
    title = _text(raw.get("title"))
    if not title or not is_valid_hhmm(start) or not is_valid_hhmm(end):
        return None
    return Activity(
        id=_text(raw.get("id")) or make_item_id("act", start, index),
        start=start, end=end, title=title, notes=_text(raw.get("notes")),
    )


def share_from_dict(raw: dict[str, Any], index: int) -> ShareItem | None:
    """读取旧分享和新任务；非法新时刻不能退回 HH:MM 意外触发。"""

    time_str = _text(raw.get("time"))
    title = _text(raw.get("title"))
    if not title or not is_valid_hhmm(time_str):
        return None
    source = _text(raw.get("source")) or "legacy"
    if source not in TASK_SOURCES:
        source = "legacy"
    fired = bool(raw.get("fired"))
    state = _text(raw.get("state")) or ("consumed" if fired else "pending")
    if state not in TASK_STATES:
        state = "consumed"
    reason = _text(raw.get("state_reason"))
    if fired and state in {"pending", "submitting"}:
        state = "consumed"
    if state == "consumed" and not reason:
        reason = "legacy_result_unknown"
    due_at = _text(raw.get("due_at"))
    try:
        if due_at:
            parse_task_datetime(due_at)
        elif source != "legacy":
            raise ValueError("新任务缺少完整到期时间")
    except ValueError:
        state, reason = "skipped", "invalid_due_at"
    return ShareItem(
        id=_text(raw.get("id")) or make_item_id("share", time_str, index),
        time=time_str, title=title, hint=_text(raw.get("hint")),
        intent=_text(raw.get("intent")), stream_id=_text(raw.get("stream_id")),
        fired=state in _TERMINAL_STATES, fired_at=_text(raw.get("fired_at")),
        source=source, due_at=due_at, state=state, state_reason=reason,
        request_id=_text(raw.get("request_id")),
        request_bindings={
            _text(key): dict(value) for key, value in raw.get("request_bindings", {}).items()
            if _text(key) and isinstance(value, dict)
        } if isinstance(raw.get("request_bindings"), dict) else {},
        generation_key=_text(raw.get("generation_key")),
    )


def document_from_dict(raw: dict[str, Any]) -> LifeDocument | None:
    """从字典构造生活记录。"""

    date = _text(raw.get("date"))
    try:
        parse_date(date)
    except ValueError:
        return None
    raw_activities = raw.get("activities", [])
    if not isinstance(raw_activities, list):
        return None
    activities: list[Activity] = []
    for index, item in enumerate(raw_activities):
        if isinstance(item, dict):
            parsed = activity_from_dict(item, index)
            if parsed is not None:
                activities.append(parsed)
    shares: list[ShareItem] = []
    raw_by_stream = raw.get("shares_by_stream")
    if isinstance(raw_by_stream, dict):
        groups = [(stream_id, items) for stream_id, items in raw_by_stream.items()]
    else:
        raw_shares = raw.get("shares", [])
        if not isinstance(raw_shares, list):
            return None
        groups = [(None, raw_shares)]
    for stream_id, items in groups:
        if not isinstance(items, list):
            continue
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            parsed = share_from_dict(item if stream_id is None else {**item, "stream_id": stream_id}, index)
            if parsed is not None:
                shares.append(parsed)
    raw_streams = raw.get("generated_streams")
    generated_streams = list(dict.fromkeys(_text(item) for item in raw_streams if _text(item))) if isinstance(raw_streams, list) else []
    return LifeDocument(
        date=date, generated_at=_text(raw.get("generated_at")),
        activities=activities, shares=shares, generated_streams=generated_streams,
    )


class LifeStore:
    """按日 JSON 是唯一事实源；内存任务定位可从文件重建。"""

    def __init__(self, data_dir: Path, *, now: Callable[[], datetime] | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._now = now or datetime.now
        self._lock = RLock()
        self._tasks_by_date: dict[str, list[ShareItem]] = {}
        self.rebuild_index()

    def path_for(self, date: str) -> Path:
        parse_date(date)
        return self.data_dir / f"life-{date}.json"

    def rebuild_index(self) -> None:
        """启动时读取合法日文件，损坏文件仅报告，绝不当空文档覆盖。"""

        with self._lock:
            self._tasks_by_date.clear()
            for path in sorted(self.data_dir.glob("life-*.json")):
                date = path.name[5:-5]
                try:
                    document = self.load(date)
                except (OSError, ValueError):
                    logger.warning("日文件无效，未加入任务索引: %s", path.name)
                    continue
                if document is None:
                    continue
                # RPC 中断后无法知道 Host 是否接收，保留未知终态而非盲目重发。
                interrupted = [item for item in document.shares if task_state(item) == "submitting"]
                for item in interrupted:
                    set_task_state(item, "consumed", "submission_result_unknown", self._now())
                if interrupted:
                    self.save(document)
                else:
                    self._index_document(document)

    def _index_document(self, document: LifeDocument) -> None:
        self._tasks_by_date[document.date] = deepcopy(document.shares)

    def load(self, date: str) -> LifeDocument | None:
        with self._lock:
            path = self.path_for(date)
            if not path.is_file():
                return None
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(f"日文件损坏: {path.name}，请先检查备份") from exc
            document = document_from_dict(raw) if isinstance(raw, dict) else None
            if document is None or document.date != date:
                raise ValueError(f"日文件日期或格式无效: {path.name}")
            return document

    def save(self, document: LifeDocument) -> None:
        """同目录临时文件 + 原子替换，避免巡检读到截断的 JSON。"""

        with self._lock:
            path = self.path_for(document.date)
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.data_dir)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(document.to_dict(), handle, ensure_ascii=False, indent=2)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
                self._index_document(document)
            finally:
                temporary.unlink(missing_ok=True)

    def mutate(
        self, date: str, change: Callable[[LifeDocument], _T], *, generated_at: str = ""
    ) -> tuple[LifeDocument, _T]:
        """短同步读改写，调用者不得在 change 内进行模型/RPC/await。"""

        with self._lock:
            document = self.load(date) or LifeDocument(date=date, generated_at=generated_at)
            result = change(document)
            self.save(document)
            return document, result

    def exists(self, date: str) -> bool:
        return self.path_for(date).is_file()

    def list_tasks(
        self, *, stream_id: str | None = None, pending_only: bool = False, due_date: str = ""
    ) -> list[tuple[str, ShareItem]]:
        """查询跨日期任务，不在每次巡检扫描全部历史文件。"""

        if due_date:
            parse_date(due_date)
        with self._lock:
            result: list[tuple[str, ShareItem]] = []
            for date, items in self._tasks_by_date.items():
                if not self.path_for(date).is_file():
                    continue
                for item in items:
                    if stream_id is not None and item.stream_id != stream_id:
                        continue
                    if pending_only and task_state(item) != "pending":
                        continue
                    if due_date:
                        try:
                            if task_due_at(item, date).strftime("%Y-%m-%d") != due_date:
                                continue
                        except ValueError:
                            continue
                    result.append((date, deepcopy(item)))
            return sorted(result, key=lambda pair: (pair[1].due_at or f"{pair[0]}T{pair[1].time}", pair[1].id))

    def find_task(self, stream_id: str, task_id: str, owner_date: str = "") -> tuple[str, ShareItem]:
        """旧 ID 可跨日期重复；歧义必须由原始归属日期消除。"""

        if owner_date:
            parse_date(owner_date)
        matches = [
            (date, item) for date, item in self.list_tasks(stream_id=stream_id)
            if item.id == task_id and (not owner_date or date == owner_date)
        ]
        if not matches:
            raise ValueError("当前聊天流找不到该任务，请先查询获取 task_id 和 owner_date")
        if len(matches) != 1:
            raise ValueError("该旧 task_id 不唯一，请同时提供查询返回的 owner_date")
        return matches[0]

    def add_task(
        self, owner_date: str, item: ShareItem, now: datetime, *, request_spec: dict[str, Any],
    ) -> tuple[str, ShareItem, bool]:
        """幂等查找和新增在同一短临界区，初始请求签名不随任务改期而改变。"""

        with self._lock:
            existing = self.list_tasks(stream_id=item.stream_id)
            if item.request_id:
                matches = [
                    (date, old) for date, old in existing
                    if item.request_id == old.request_id or item.request_id in old.request_bindings
                ]
                if matches:
                    if len(matches) != 1:
                        raise ValueError("request_id 已有歧义，请先查询任务")
                    date, old = matches[0]
                    bound = old.request_bindings.get(item.request_id)
                    if bound is None:
                        # 兼容早期有 request_id 但无签名的记录，先固定当前已知值。
                        same = old.intent == item.intent and (
                            not request_spec.get("run_at")
                            or task_due_at(old, date).isoformat(timespec="seconds") == request_spec["run_at"]
                        )
                    else:
                        same = bound == request_spec
                    if not same:
                        raise ValueError("该 request_id 已用于其他内容或时刻，请更换幂等键")
                    if bound is None:
                        def bind(document: LifeDocument) -> None:
                            for task in document.shares:
                                if task.id == old.id and task.stream_id == old.stream_id:
                                    task.request_bindings[item.request_id] = dict(request_spec)
                        self.mutate(date, bind)
                        old.request_bindings[item.request_id] = dict(request_spec)
                    return date, old, True
            if task_due_at(item, owner_date) <= now:
                raise ValueError("任务时间必须在未来；不会自动挪到明天")
            for date, old in existing:
                if task_state(old) in {"pending", "submitting", "submitted"} and old.intent == item.intent:
                    try:
                        same_time = task_due_at(old, date) == task_due_at(item, owner_date)
                    except ValueError:
                        same_time = False
                    if same_time:
                        def adopt_and_bind(document: LifeDocument) -> ShareItem:
                            matches = [task for task in document.shares if task.id == old.id and task.stream_id == old.stream_id]
                            if len(matches) != 1:
                                raise ValueError("旧任务 ID 不唯一，请先检查旧数据")
                            task = matches[0]
                            if task.source != "planner" and task_state(task) == "pending":
                                if task.source == "generated" and not task.generation_key:
                                    task.generation_key = task_signature(task, date)
                                task.source, task.due_at = "planner", item.due_at
                            if item.request_id:
                                if not task.request_id:
                                    task.request_id = item.request_id
                                task.request_bindings[item.request_id] = dict(request_spec)
                            return deepcopy(task)
                        _, saved = self.mutate(date, adopt_and_bind)
                        return date, saved, True
            if item.request_id:
                item.request_bindings[item.request_id] = dict(request_spec)

            def append(document: LifeDocument) -> None:
                document.shares.append(item)

            self.mutate(owner_date, append, generated_at=now.isoformat(timespec="seconds"))
            return owner_date, deepcopy(item), False

    def edit_task(
        self, stream_id: str, task_id: str, owner_date: str,
        change: Callable[[ShareItem, str], None],
    ) -> tuple[str, ShareItem]:
        """定位并修改当前流唯一任务，所有其他任务和最新终态保持不变。"""

        with self._lock:
            date, _ = self.find_task(stream_id, task_id, owner_date)

            def apply(document: LifeDocument) -> ShareItem:
                matches = [item for item in document.shares if item.stream_id == stream_id and item.id == task_id]
                if len(matches) != 1:
                    raise ValueError("任务定位不唯一，请检查旧数据")
                change(matches[0], date)
                return deepcopy(matches[0])

            _, result = self.mutate(date, apply)
            return date, result

    def claim_task(self, date: str, expected: ShareItem) -> ShareItem | None:
        """RPC 前按完整旧值认领，避免发现任务后 await 期间的改期/取消被覆盖。"""

        with self._lock:
            document = self.load(date)
            if document is None:
                return None
            matches = [item for item in document.shares if item.id == expected.id and item.stream_id == expected.stream_id]
            if len(matches) != 1:
                return None
            item = matches[0]
            if task_state(item) != "pending" or item.to_dict() != expected.to_dict():
                return None
            item.state, item.state_reason = "submitting", ""
            self.save(document)
            return deepcopy(item)

    def mark_task_state(
        self, date: str, stream_id: str, task_id: str, state: str, reason: str,
        now: datetime, *, expected_state: str = "pending", expected: ShareItem | None = None,
    ) -> ShareItem | None:
        with self._lock:
            document = self.load(date)
            if document is None:
                return None
            matches = [item for item in document.shares if item.id == task_id and item.stream_id == stream_id]
            if len(matches) != 1 or task_state(matches[0]) != expected_state:
                return None
            if expected is not None and matches[0].to_dict() != expected.to_dict():
                return None
            target = matches[0]
            set_task_state(target, state, reason, now)
            self.save(document)
            return deepcopy(target)

    def mark_share_fired(
        self, date: str, share_id: str, fired_at: datetime, *, stream_id: str | None = None
    ) -> ShareItem | None:
        """兼容旧调用；ID 歧义时拒绝更新，不再误标另一个流。"""

        matches = [(day, item) for day, item in self.list_tasks(stream_id=stream_id) if day == date and item.id == share_id]
        if len(matches) != 1:
            return None
        item = matches[0][1]
        return self.mark_task_state(date, item.stream_id, share_id, "consumed", "legacy_result_unknown", fired_at)
