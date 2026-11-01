"""规划器 messages 注入。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from .prompts import DEFAULT_INJECT_TEMPLATE, choose_template, render_template
from .store import Activity, LifeDocument
from .times import activity_interval, weekday_cn


def format_activity_line(activity: Activity, *, cross_day: bool = False) -> str:
    """把一条活动格式化成注入文本。"""

    suffix = "(次日)" if cross_day else ""
    line = f"- {activity.start}-{activity.end}{suffix} {activity.title}"
    if activity.notes:
        line += f" | {activity.notes}"
    return line


@dataclass
class _DatedActivity:
    """只读日程视图的一项，不回写或搬移源活动。"""

    activity: Activity
    source_date: str
    interval: tuple[datetime, datetime]
    conflicts: list[str] = field(default_factory=list)


def _parse_activities(activities: list[Activity], day: datetime) -> list[_DatedActivity]:
    views = []
    for activity in activities:
        try:
            interval = activity_interval(activity.start, activity.end, day)
        except ValueError:
            continue
        views.append(_DatedActivity(activity, day.strftime("%Y-%m-%d"), interval))
    return views


def _activity_score(interval: tuple[datetime, datetime], now: datetime) -> tuple[int, float]:
    start_dt, end_dt = interval
    if start_dt <= now < end_dt:
        return 0, 0.0
    distance = (start_dt - now).total_seconds() if now < start_dt else (now - end_dt).total_seconds()
    return 1, distance


def _select_recent_views(
    views: list[_DatedActivity],
    now: datetime,
    *,
    count: int,
    window_minutes: int,
) -> list[_DatedActivity]:
    if count <= 0 or not views:
        return []
    scored = sorted(views, key=lambda view: _activity_score(view.interval, now))
    window = timedelta(minutes=max(0, int(window_minutes)))
    in_window = [
        view for view in scored if view.interval[0] - window <= now <= view.interval[1] + window
    ]
    return (in_window or scored)[:count]


def select_recent_activities(
    activities: list[Activity],
    now: datetime,
    *,
    count: int,
    window_minutes: int,
    schedule_date: datetime | None = None,
) -> list[tuple[Activity, tuple[datetime, datetime]]]:
    """选取最近活动，优先 [start, end) 内进行中的活动。窗口外取最近。"""

    day = schedule_date or now.replace(hour=0, minute=0, second=0, microsecond=0)
    selected = _select_recent_views(
        _parse_activities(activities, day), now, count=count, window_minutes=window_minutes
    )
    return [(view.activity, view.interval) for view in selected]


def _document_views(
    document: LifeDocument | None,
    expected_day: datetime,
    source_label: str,
    warnings: list[str],
) -> list[_DatedActivity]:
    if document is None:
        return []
    try:
        day = datetime.strptime(document.date, "%Y-%m-%d").replace(tzinfo=expected_day.tzinfo)
    except (TypeError, ValueError):
        warnings.append(f"已跳过{source_label}文档：源日期无效，未推断活动时间。")
        return []
    if day.date() != expected_day.date():
        warnings.append(
            f"已跳过{source_label}文档：源日期 {day:%Y-%m-%d} 与请求日期 "
            f"{expected_day:%Y-%m-%d} 不一致。"
        )
        return []
    return _parse_activities(document.activities, day)


def _build_current_schedule_view(
    document: LifeDocument | None,
    yesterday: LifeDocument | None,
    now: datetime,
) -> tuple[list[_DatedActivity], list[str]]:
    """今日与昨日独立落日期；昨日仅保留确实跨入今日且仍进行的活动。"""

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    warnings: list[str] = []
    yesterday_views = _document_views(yesterday, today - timedelta(days=1), "昨日", warnings)
    carry = [
        view
        for view in yesterday_views
        if view.interval[0].date() < today.date() <= view.interval[1].date()
        and view.interval[0] <= now < view.interval[1]
    ]
    views = carry + _document_views(document, today, "今日", warnings)
    # 先在完整视图上标记重叠，避免 count=1 时隐去另一来源而掩盖冲突。
    for index, left in enumerate(views):
        for right in views[index + 1 :]:
            if max(left.interval[0], right.interval[0]) < min(left.interval[1], right.interval[1]):
                left.conflicts.append(f"{right.source_date} 日程「{right.activity.title}」")
                right.conflicts.append(f"{left.source_date} 日程「{left.activity.title}」")
    return views, warnings


def _relative_day_label(day: datetime, now: datetime) -> str:
    offset = (day.date() - now.date()).days
    return {-1: "昨日", 0: "今日", 1: "明日"}.get(offset, day.strftime("%Y-%m-%d"))


def _format_dated_activity(view: _DatedActivity, now: datetime) -> str:
    start_dt, end_dt = view.interval
    if start_dt <= now < end_dt:
        status = "进行中"
    elif now < start_dt:
        status = "待开始"
    else:
        status = "已结束"
    line = format_activity_line(view.activity, cross_day=end_dt.date() > start_dt.date())
    line += f" | 起止：{start_dt:%Y-%m-%d %H:%M} → {end_dt:%Y-%m-%d %H:%M}"
    line += f"（{_relative_day_label(start_dt, now)}开始、{_relative_day_label(end_dt, now)}结束）"
    line += f" | 状态：{status} | 来源：{view.source_date} 日程"
    if view.conflicts:
        line += f" | 来源冲突（时段重叠）：与 {'、'.join(view.conflicts)} 重叠，未合并或修改源日程"
    return line


def _format_schedule_view(views: list[_DatedActivity], warnings: list[str], now: datetime) -> str:
    lines = [_format_dated_activity(view, now) for view in views]
    lines.extend(f"提示：{warning}" for warning in warnings)
    return "\n".join(lines)


def build_recent_schedule_text(
    document: LifeDocument | None,
    now: datetime,
    *,
    count: int,
    window_minutes: int,
    yesterday: LifeDocument | None = None,
) -> str:
    """生成最近日程正文，进行中的昨日延续优先。没有活动时返回空字符串。"""

    if count <= 0:
        return ""
    views, warnings = _build_current_schedule_view(document, yesterday, now)
    today_date = now.strftime("%Y-%m-%d")
    carry = [view for view in views if view.source_date != today_date]
    today_views = [view for view in views if view.source_date == today_date]
    # 延续活动先占数量，今日邻近活动仍独立沿用窗口及窗口外最近兜底。
    selected = carry[:count] + _select_recent_views(
        today_views, now, count=count - len(carry), window_minutes=window_minutes
    )
    if not selected:
        return ""
    return _format_schedule_view(selected, warnings, now)


def build_today_schedule_text(
    document: LifeDocument | None,
    yesterday: LifeDocument | None,
    now: datetime,
) -> str:
    """展示完整今日日程和昨日仍进行的延续，不受注入数量与窗口限制。"""

    views, warnings = _build_current_schedule_view(document, yesterday, now)
    views.sort(key=lambda view: view.interval[0])
    if not views:
        return "\n".join(["暂无今日日程。", *(f"提示：{warning}" for warning in warnings)])
    return _format_schedule_view(views, warnings, now)


def build_inject_text(
    document: LifeDocument | None,
    now: datetime,
    *,
    count: int,
    window_minutes: int,
    template: str,
    extra_values: dict[str, object] | None = None,
    yesterday: LifeDocument | None = None,
) -> str:
    """渲染注入规划器的完整文本。"""

    recent = build_recent_schedule_text(
        document,
        now,
        count=count,
        window_minutes=window_minutes,
        yesterday=yesterday,
    )
    if not recent:
        return ""
    values: dict[str, object] = {
        "date": now.strftime("%Y-%m-%d"),
        "weekday": weekday_cn(now),
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "recent_schedule": recent,
    }
    if extra_values:
        values.update(extra_values)
    return render_template(choose_template(template, DEFAULT_INJECT_TEMPLATE), values).strip()


def inject_into_messages(messages: list[Any], text: str) -> list[Any]:
    """在最后一条 system 消息后插入注入文本。没有 system 则插到最前。"""

    content = str(text or "").strip()
    if not content:
        return list(messages)
    inserted = {"role": "system", "content": content}
    result = list(messages)
    last_system = -1
    for index, item in enumerate(result):
        if isinstance(item, dict) and str(item.get("role") or "") == "system":
            last_system = index
    if last_system >= 0:
        result.insert(last_system + 1, inserted)
    else:
        result.insert(0, inserted)
    return result


def build_system_item(text: str, *, now: datetime | None = None) -> dict[str, Any]:
    """构造 MaiBot 1.2+ 规划器 Hook 使用的 SystemMessageItem。"""

    stamp = now or datetime.now()
    return {
        "item_type": "SystemMessageItem",
        "meta": {
            "item_id": uuid4().hex,
            "logical_turn_id": None,
            "timestamp": stamp.isoformat(),
        },
        "parts": [{"type": "text", "text": str(text)}],
    }


def inject_into_items(items: list[Any], text: str, *, now: datetime | None = None) -> list[Any]:
    """在最后一条 SystemMessageItem 后插入日程 Item。没有则插到最前。"""

    content = str(text or "").strip()
    if not content:
        return list(items)
    inserted = build_system_item(content, now=now)
    result = list(items)
    last_system = -1
    for index, item in enumerate(result):
        if isinstance(item, dict) and str(item.get("item_type") or "") == "SystemMessageItem":
            last_system = index
    if last_system >= 0:
        result.insert(last_system + 1, inserted)
    else:
        result.insert(0, inserted)
    return result
