"""每日生成与到点唤醒调度。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any
import asyncio
import logging

from .config_model import MaiLifeConfig
from .store import LifeDocument, LifeStore, ShareItem, task_due_at, task_state
from .times import (
    is_valid_hhmm,
    next_daily_datetime,
    parse_task_datetime,
    time_to_minutes,
    wall_clock_now,
)

# 到期后仍可补触发的内部宽限。巡检有间隔，且重启后不应把过期很久的提醒一次性发出。
DEFAULT_MISS_TOLERANCE_MINUTES = 30

logger = logging.getLogger("mailife.mai-life")

GenerateFn = Callable[[bool], Awaitable[LifeDocument | None]]
FireShareFn = Callable[[ShareItem, LifeDocument, datetime], Awaitable[None]]
LoadTodayFn = Callable[[datetime], LifeDocument | None]


def share_due_status(
    item: ShareItem,
    now: datetime,
    *,
    miss_tolerance_minutes: int,
    owner_date: str = "",
) -> str:
    """兼容 fired 结果；提交中和终态不再触发，旧时间锚定归属日。"""

    state = task_state(item)
    if state == "submitting":
        return "submitting"
    if state != "pending":
        return "fired"
    try:
        due_at = task_due_at(item, owner_date or now.strftime("%Y-%m-%d"))
    except ValueError:
        return "missed"
    if now < due_at:
        return "pending"
    late = now - due_at
    if late <= timedelta(minutes=max(0, int(miss_tolerance_minutes))):
        return "due"
    return "missed"


class LifeScheduler:
    """后台循环：每日生成 + 分享巡检。"""

    def __init__(
        self,
        *,
        get_config: Callable[[], MaiLifeConfig],
        generate_today: GenerateFn,
        load_today: LoadTodayFn,
        fire_share: FireShareFn,
        mark_share_fired: Callable[[str, str, datetime], Any],
        store: LifeStore | None = None,
    ) -> None:
        self._get_config = get_config
        self._generate_today = generate_today
        self._load_today = load_today
        self._fire_share = fire_share
        self._mark_share_fired = mark_share_fired
        self._store = store
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._last_wake_at: dict[str, datetime] = {}
        if store is not None:
            for _, item in store.list_tasks():
                if (task_state(item) != "submitted" and item.state_reason != "submission_result_unknown") or not item.stream_id or not item.fired_at:
                    continue
                try:
                    stamp = parse_task_datetime(item.fired_at)
                except ValueError:
                    continue
                previous = self._last_wake_at.get(item.stream_id)
                if previous is None or stamp > previous:
                    self._last_wake_at[item.stream_id] = stamp

    async def start(self, *, generate_if_missing: bool = False) -> None:
        """启动循环。重复 start 会先停止旧循环。"""

        await self.stop()
        self._stop = asyncio.Event()
        config = self._get_config()
        self._tasks = [
            asyncio.create_task(self._generation_loop(), name="mai-life-generate"),
            asyncio.create_task(self._patrol_loop(), name="mai-life-patrol"),
        ]
        if generate_if_missing and config.plugin.enabled:
            self._tasks.append(asyncio.create_task(self._backfill_missing(), name="mai-life-backfill"))

    async def stop(self) -> None:
        """停止循环并等待退出。"""

        self._stop.set()
        tasks = list(self._tasks)
        self._tasks = []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _now(self, config: MaiLifeConfig) -> datetime:
        return wall_clock_now(config.generation.timezone)

    def _needs_backfill(self, config: MaiLifeConfig, document: LifeDocument | None) -> bool:
        if document is None:
            return True
        if config.schedule.enabled and not document.activities:
            return True
        if config.share.enabled:
            profiles = [profile.stream.strip() for profile in config.share.stream_profiles if profile.stream.strip()]
            known = {item.stream_id for item in document.shares if item.source in {"generated", "legacy"}}
            known.update(document.generated_streams)
            if not known:
                return True
            if any(stream != "all" and stream not in known for stream in profiles):
                # profile 可能是外部别名，由生成器解析；最多按原启动重试次数补齐。
                return True
        return False

    async def _backfill_missing(self) -> None:
        """启动后补生成：日程文件已有但分享任务因当时没有聊天流而空着时再试。"""

        delays = (0, 15, 45)
        for delay in delays:
            if delay:
                await self._wait(delay)
            if self._stop.is_set():
                return
            config = self._get_config()
            if not config.plugin.enabled:
                return
            try:
                now = self._now(config)
            except ValueError:
                logger.exception("时区无效，跳过启动补生成")
                return
            document = self._load_today(now)
            if not self._needs_backfill(config, document):
                return
            try:
                await self._generate_today(False)
            except Exception:
                logger.exception("启动时生成今日生活记录失败")
            document = self._load_today(now)
            if not self._needs_backfill(config, document):
                return

    async def _generation_loop(self) -> None:
        while not self._stop.is_set():
            config = self._get_config()
            if not config.plugin.enabled:
                await self._wait(30)
                continue
            if not is_valid_hhmm(config.generation.time):
                logger.error("generation.time 不是合法 HH:MM: %s", config.generation.time)
                await self._wait(60)
                continue
            try:
                now = self._now(config)
            except ValueError:
                logger.exception("时区无效，暂停每日生成")
                await self._wait(60)
                continue
            next_run = next_daily_datetime(now, config.generation.time)
            wait_seconds = max(1.0, (next_run - now).total_seconds())
            logger.info("下次生活记录生成时间: %s", next_run.strftime("%Y-%m-%d %H:%M"))
            if await self._wait(wait_seconds):
                break
            if self._stop.is_set():
                break
            try:
                await self._generate_today(False)
            except Exception:
                logger.exception("定时生成生活记录失败")

    async def _patrol_loop(self) -> None:
        while not self._stop.is_set():
            config = self._get_config()
            interval = max(5, int(config.share.patrol_interval_seconds))
            try:
                await self._patrol_tick(config)
            except Exception:
                logger.exception("分享巡检异常")
            if await self._wait(interval):
                break

    async def _patrol_tick(self, config: MaiLifeConfig) -> None:
        if not config.plugin.enabled or not config.share.enabled or not config.share.wake_planner:
            return
        try:
            now = self._now(config)
        except ValueError:
            logger.exception("时区无效，跳过本次巡检")
            return
        if self._store is not None:
            candidates = self._store.list_tasks(pending_only=True)
        else:
            document = self._load_today(now)
            candidates = [(document.date, item) for item in document.shares] if document else []
        for owner_date, item in candidates:
            # 前一个 RPC 可能等待较久，宽限与冷却必须使用本项的当前时刻。
            now = self._now(config)
            status = share_due_status(
                item, now, owner_date=owner_date,
                miss_tolerance_minutes=DEFAULT_MISS_TOLERANCE_MINUTES,
            )
            if status == "missed":
                if self._store is not None:
                    self._store.mark_task_state(owner_date, item.stream_id, item.id, "skipped", "missed_tolerance", now, expected=item)
                else:
                    self._mark_share_fired(owner_date, item.id, now)
                logger.info("任务已过宽限，记录跳过: task=%s owner_date=%s", item.id, owner_date)
                continue
            if status != "due":
                continue
            document = self._store.load(owner_date) if self._store is not None else self._load_today(now)
            if document is None:
                continue
            try:
                await self._fire_share(item, document, now)
            except Exception:
                logger.exception("提交规划器失败: task=%s", item.id)

    def can_wake_stream(self, stream_id: str, now: datetime, cooldown_seconds: int) -> bool:
        """同一聊天流是否已过冷却。"""

        last = self._last_wake_at.get(stream_id)
        if last is None:
            return True
        return now - last >= timedelta(seconds=max(0, int(cooldown_seconds)))

    def remember_wake(self, stream_id: str, now: datetime) -> None:
        """记录某聊天流刚刚被唤醒。"""

        self._last_wake_at[stream_id] = now

    async def _wait(self, seconds: float) -> bool:
        """等待 seconds。若被 stop 打断返回 True。"""

        try:
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.1, float(seconds)))
            return True
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            return True


def sort_shares(shares: list[ShareItem]) -> list[ShareItem]:
    """按时间排序分享任务。"""

    def key(item: ShareItem) -> int:
        try:
            return time_to_minutes(item.time)
        except ValueError:
            return 0

    return sorted(shares, key=key)
