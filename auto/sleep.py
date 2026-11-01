# -*- coding: utf-8 -*-
"""Raw-message sleep tracker for /sleep.

The command starts at most one record per user in each 18:00-06:00 sleep
period. Any subsequent user message within four hours invalidates it and
charges a 10-point phone-use penalty; the first message after four hours
completes it and awards points. Sleep longer than 12 hours is settled as a
5-hour sleep.
"""
from __future__ import annotations

# GSMPS WebUI metadata only; execution remains in this shell module.
WEBUI_CONFIG = {'version': 1, 'fields': {}}


from datetime import datetime, timedelta
from pathlib import Path

from utils.sqlite_store import sleep_settle_once, sleep_get_active, sleep_recent, sleep_start_once

MATCH_RAW_MESSAGE = True
INTERCEPT_LLM = True
FALLBACK_ONLY = False
AUTO_COMMAND_CONSUMES = True
AUTO_COMMANDS = {"/sleep"}

FOUR_HOURS = 4 * 3600
MAX_SLEEP_SECONDS = 12 * 3600
OVERSLEEP_SETTLE_SECONDS = 5 * 3600
WITHIN_FOUR_HOURS_PENALTY = 10
SLEEP_WINDOW_START_HOUR = 18
SLEEP_WINDOW_END_HOUR = 6


def init(config):
    print("[AUTO PLUGIN] sleep tracker ready", flush=True)


def _wxid(context):
    from utils.account_identity import actor
    return actor(context or {})


def _group(context):
    raw = context.get("raw") if isinstance(context, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    return str((context or {}).get('sessionId') or (context or {}).get('group_id') or (context or {}).get("group") or raw.get("group") or "").strip()


def _nickname(context):
    raw = context.get("raw") if isinstance(context, dict) else {}
    raw = raw if isinstance(raw, dict) else {}
    return str((context or {}).get("user") or raw.get("user") or "未知用户").strip()


def _text(context):
    context = context if isinstance(context, dict) else {}
    command = str(context.get("command") or "").strip()
    args = str(context.get("args") or "").strip()
    if command.casefold() == "/sleep":
        return f"/sleep {args}".strip()
    content = str(context.get("content") or "").strip()
    return content


def _sleep_args(text):
    parts = str(text or "").strip().split(None, 1)
    if not parts or parts[0].casefold() != "/sleep":
        return None
    return parts[1].strip().casefold() if len(parts) > 1 else ""


def _allowed_start(now=None):
    now = now or datetime.now()
    # Sleep check-in is available from 18:00 through 05:59:59.
    return now.hour >= 18 or now.hour < 6


def _sleep_period_start(now=None):
    """Return the start of the current sleepable period.

    A period runs from 18:00 to 06:00 the following day. The same period
    therefore has one stable key across midnight, which prevents a user from
    starting a second sleep record at 01:00 after already checking in at 22:00.
    """
    now = now or datetime.now()
    start = now.replace(hour=SLEEP_WINDOW_START_HOUR, minute=0, second=0, microsecond=0)
    if now.hour < SLEEP_WINDOW_END_HOUR:
        start -= timedelta(days=1)
    return start


def _has_sleep_in_current_period(wxid, now=None):
    now = now or datetime.now()
    period_start = _sleep_period_start(now)
    period_end = period_start + timedelta(hours=12)
    for row in sleep_recent(wxid, limit=50):
        try:
            started = datetime.fromisoformat(str(row.get("start_at") or ""))
        except (TypeError, ValueError):
            continue
        if period_start <= started < period_end:
            return True
    return False


def _duration_text(seconds):
    total_minutes = max(0, int(round(float(seconds or 0) / 60)))
    hours, minutes = divmod(total_minutes, 60)
    if hours and minutes:
        return f"{hours}小时{minutes}分钟"
    if hours:
        return f"{hours}小时"
    return f"{max(1, minutes)}分钟"


def _bedtime_label(start):
    hour = start.hour + start.minute / 60.0
    if hour >= 18 or hour < 0:
        return "非常早睡"
    if hour < 1:
        return "正常时间入睡"
    if hour < 2:
        return "比较晚入睡"
    if hour < 3:
        return "晚睡入场"
    if hour < 4:
        return "非常晚入睡"
    return "熬穿夜色后入睡"


def _duration_label(hours):
    if hours >= 10:
        return "超长睡眠"
    if hours >= 8:
        return "满足睡眠"
    if hours >= 6:
        return "勉强睡眠"
    if hours >= 4:
        return "不足睡眠"
    return "严重不足睡眠"


def _score(start, duration_seconds):
    hours = max(0.0, float(duration_seconds or 0) / 3600.0)
    # Duration: 70 points maximum.  8-10h is ideal; oversleeping is useful but
    # receives a small penalty so the score does not reward sleeping forever.
    if 8 <= hours <= 10:
        duration_score = 70.0
    elif 10 < hours <= 12:
        duration_score = 64.0
    elif hours > 12:
        duration_score = 56.0
    elif 6 <= hours < 8:
        duration_score = 58.0 + (hours - 6) * 6.0
    elif 4 <= hours < 6:
        duration_score = 34.0 + (hours - 4) * 12.0
    else:
        duration_score = max(8.0, hours * 8.0)

    hour = start.hour + start.minute / 60.0
    if hour >= 18:
        bedtime_score = 30.0
    elif hour < 1:
        bedtime_score = 26.0
    elif hour < 2:
        bedtime_score = 20.0
    elif hour < 3:
        bedtime_score = 14.0
    elif hour < 4:
        bedtime_score = 8.0
    else:
        bedtime_score = 2.0
    return round(min(100.0, max(0.0, duration_score + bedtime_score)), 1)


def _wake_message(start, duration_seconds, score, points, oversleep_settled=False):
    hours = float(duration_seconds or 0) / 3600.0
    suffix = "\uff08\u8d85\u8fc712\u5c0f\u65f6\uff0c\u63095\u5c0f\u65f6\u7761\u7720\u7ed3\u7b97\uff09" if oversleep_settled else ""
    return (
        f"\u672c\u6b21\u7761\u7720 {_duration_text(duration_seconds)}\uff0c"
        f"\u5c5e\u4e8e{_duration_label(hours)}\uff0c{_bedtime_label(start)}\u3002"
        f"\u7761\u7720\u8bc4\u5206\uff1a{score:.1f}/100\uff0c\u83b7\u5f97 {points:.1f} \u79ef\u5206\u3002{suffix}"
    )


def _record_text(wxid):
    rows = sleep_recent(wxid, limit=10)
    if not rows:
        return "还没有睡眠记录，今晚早点睡喵～"
    lines = ["最近 10 条睡眠记录："]
    for row in rows:
        start = str(row.get("start_at") or "").replace("T", " ")[:16]
        duration = _duration_text(row.get("duration_seconds") or 0)
        status = row.get("status")
        if status == "completed":
            lines.append(f"{start} · {duration} · +{float(row.get('points_awarded') or 0):.1f} 积分")
        elif status == "invalid":
            lines.append(f"{start} · 记录无效 · 手机没忍住")
        else:
            lines.append(f"{start} · 正在计时中")
    return "\n".join(lines)


def _invalidate(active, now):
    start = datetime.fromisoformat(str(active["start_at"]))
    seconds = max(0.0, (now - start).total_seconds())
    if not sleep_settle_once(active['id'], 'invalid', now.isoformat(), seconds,
            invalid_reason='message_within_four_hours', points_delta=-WITHIN_FOUR_HOURS_PENALTY):
        return None
    return f"\u4f60\u5728\u7761\u7720\u5f00\u59cb\u540e4\u5c0f\u65f6\u5185\u53d1\u4e86\u6d88\u606f\uff0c\u672c\u6b21\u7761\u7720\u65e0\u6548\uff0c\u5e76\u6263\u9664 {WITHIN_FOUR_HOURS_PENALTY} \u79ef\u5206\u3002"

def _complete(active, now):
    start = datetime.fromisoformat(str(active["start_at"]))
    elapsed = max(0.0, (now - start).total_seconds())
    oversleep_settled = elapsed > MAX_SLEEP_SECONDS
    seconds = OVERSLEEP_SETTLE_SECONDS if oversleep_settled else max(FOUR_HOURS, elapsed)
    score = _score(start, seconds)
    points = round(score / 10.0, 1)
    if not sleep_settle_once(active['id'], 'completed', now.isoformat(), seconds,
            score=score, points_awarded=points, points_delta=points):
        return None
    return _wake_message(start, seconds, score, points, oversleep_settled=oversleep_settled)

def handle_auto(context):
    context = context if isinstance(context, dict) else {}
    raw = context.get('raw') or {}
    if (context.get("is_bot") or context.get('is_notify') or raw.get('is_bot') or raw.get('is_notify')
            or context.get('source_kind') == 'system_event' or raw.get('source_kind') == 'system_event'):
        return None
    wxid = _wxid(context)
    if not wxid:
        return None
    text = _text(context)
    args = _sleep_args(text)
    from utils.account_identity import bound_user
    if not bound_user(wxid):
        return '请先 /bind <游戏玩家名> 绑定游戏账号后使用睡眠积分功能' if args is not None else None
    active = sleep_get_active(wxid)
    now = datetime.now()

    # Any message from a sleeping user is the wake/invalidating event.
    if active:
        elapsed = (now - datetime.fromisoformat(str(active["start_at"]))).total_seconds()
        if elapsed < FOUR_HOURS:
            return _invalidate(active, now)
        return _complete(active, now)

    if args is None:
        return None
    if args in {"record", "records", "记录"}:
        return _record_text(wxid)
    if args:
        return "用法：/sleep 开始睡眠打卡；/sleep record 查看最近记录。"
    if not _allowed_start(now):
        return "现在还是白天时段，晚上 18:00 之后再来睡眠打卡喵～"

    if _has_sleep_in_current_period(wxid, now):
        return "\u672c\u4e2a\u7761\u7720\u65f6\u6bb5\u5df2\u7ecf\u7761\u8fc7\u4e00\u6b21\u4e86\uff0c\u4e0d\u80fd\u91cd\u590d\u6253\u5361\u3002"

    period = _sleep_period_start(now)
    if not sleep_start_once(wxid, _nickname(context), _group(context), now.isoformat(),
                            period.isoformat(), (period+timedelta(hours=12)).isoformat()):
        return '本个睡眠时段已经睡过一次了，不能重复打卡。'
    return "睡眠已打卡！晚安喵～手机放远一点，四小时后再见。"
