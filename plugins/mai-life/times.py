"""时间工具。"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import re


_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATETIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?")


def parse_date(value: str) -> datetime:
    """严格解析 YYYY-MM-DD，不接受路径片段或缩写日期。"""

    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError("日期必须是 YYYY-MM-DD")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期不存在，请使用合法的 YYYY-MM-DD") from exc


def parse_task_datetime(value: str) -> datetime:
    """解析配置时区下的完整墙钟时刻，不隐式换日或猜测时区。"""

    if not isinstance(value, str) or not _DATETIME_RE.fullmatch(value):
        raise ValueError("run_at 必须是 YYYY-MM-DD HH:MM[:SS]，使用插件配置时区")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("run_at 不是合法的日期时间") from exc


def parse_hhmm(value: str) -> time:
    """解析 HH:MM。非法值抛出 ValueError。"""

    text = str(value or "").strip()
    parsed = datetime.strptime(text, "%H:%M")
    return parsed.time()


def is_valid_hhmm(value: str) -> bool:
    """判断是否为合法 HH:MM。"""

    try:
        parse_hhmm(value)
        return True
    except ValueError:
        return False


def time_to_minutes(value: str) -> int:
    """把 HH:MM 转成从 0 点起的分钟数。"""

    parsed = parse_hhmm(value)
    return parsed.hour * 60 + parsed.minute


def is_cross_day(start: str, end: str) -> bool:
    """结束时间不晚于开始时间时视为跨天。"""

    return time_to_minutes(end) <= time_to_minutes(start)


def wall_clock_now(timezone_name: str, now: datetime | None = None) -> datetime:
    """返回配置时区下的墙上时钟。返回值为 naive datetime。"""

    if now is not None:
        if now.tzinfo is not None:
            zone = _resolve_zone(timezone_name)
            if zone is None:
                return now.replace(tzinfo=None)
            return now.astimezone(zone).replace(tzinfo=None)
        return now

    zone = _resolve_zone(timezone_name)
    if zone is None:
        return datetime.now()
    return datetime.now(zone).replace(tzinfo=None)


def _resolve_zone(timezone_name: str) -> ZoneInfo | None:
    name = str(timezone_name or "").strip()
    if not name or name.lower() in {"local", "system"}:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"未知时区: {name}") from exc


def combine_on_date(day: datetime, hhmm: str) -> datetime:
    """把 HH:MM 落到指定日期。"""

    parsed = parse_hhmm(hhmm)
    return day.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)


def activity_interval(start: str, end: str, day: datetime) -> tuple[datetime, datetime]:
    """计算活动起止时间。跨天则结束时间加一天。"""

    start_dt = combine_on_date(day, start)
    end_dt = combine_on_date(day, end)
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)
    return start_dt, end_dt


def is_in_time_window(start: str, end: str, current: str) -> bool:
    """判断 current(HH:MM) 是否落在 [start, end) 窗口。支持跨天。"""

    start_min = time_to_minutes(start)
    end_min = time_to_minutes(end)
    current_min = time_to_minutes(current)
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= current_min < end_min
    return current_min >= start_min or current_min < end_min


def next_daily_datetime(now: datetime, hhmm: str) -> datetime:
    """下一次每日时刻。若今天该时刻未到则返回今天，否则返回明天。"""

    target = combine_on_date(now, hhmm)
    if now < target:
        return target
    return target + timedelta(days=1)


def weekday_cn(day: datetime) -> str:
    """返回中文星期。"""

    return "一二三四五六日"[day.weekday()]
