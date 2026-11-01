"""运行状态持久化：已检查/已搬记录 + 当天随机发送计划。

只存一份小 JSON 在插件 data 目录，不依赖宿主数据库模型。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# 被否掉的候选只记 14 天：搜索会把同一批视频反复捞出来，记太久会让可检查候选枯竭
# （已搬过的另用 _sent 永久记住，不会重复搬）
_CHECKED_KEEP_DAYS = 14
_SENT_KEEP = 500
_MANUAL_KEEP_DAYS = 7


class StateStore:
    """插件运行状态。"""

    def __init__(self, path: Path, logger: Any) -> None:
        self._path = path
        self._logger = logger
        self._checked: Dict[str, float] = {}
        self._sent: List[Dict[str, Any]] = []
        self._plan_date: str = ""
        self._plan_times: List[float] = []
        self._plan_done: List[float] = []
        # 手动（自然语言）触发记录：聊天流 -> 触发时间戳列表
        self._manual: Dict[str, List[float]] = {}
        self._planner: Dict[str, Dict[str, float]] = {}
        # 「群/人 -> 当前活动聊天流」：从入站消息学到的，发送时优先用它。
        # 必须持久化：宿主 chat.get_stream_by_group_id 是 next(...) 取第一个匹配，
        # 同一群存在多条历史会话时会返回过期的那个（实测路由落到 legacy 旧链、发送必失败）。
        self._live: Dict[str, str] = {}
        self._dirty = False

    # ===== 读写 =====

    def load(self) -> None:
        """同步读取（只在 on_load 里调用一次）。"""
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._logger.warning(f"状态文件读取失败，按空状态继续: {exc!r}")
            return
        self._checked = {
            str(k): float(v) for k, v in (raw.get("checked") or {}).items() if isinstance(v, (int, float))
        }
        self._sent = [item for item in (raw.get("sent") or []) if isinstance(item, dict)]
        plan = raw.get("plan") or {}
        self._plan_date = str(plan.get("date") or "")
        self._plan_times = [float(x) for x in (plan.get("times") or [])]
        self._plan_done = [float(x) for x in (plan.get("done") or [])]
        self._manual = {
            str(key): [float(x) for x in value]
            for key, value in (raw.get("manual") or {}).items()
            if isinstance(value, list)
        }
        self._live = {
            str(key): str(value) for key, value in (raw.get("live_sessions") or {}).items() if value
        }
        self._planner = {
            str(key): {str(k): float(v) for k, v in value.items() if isinstance(v, (int, float))}
            for key, value in (raw.get("planner") or {}).items() if isinstance(value, dict)
        }

    async def save(self) -> None:
        """异步落盘，避免阻塞事件循环。"""
        if not self._dirty:
            return
        payload = {
            "checked": self._checked,
            "sent": self._sent[-_SENT_KEEP:],
            "plan": {"date": self._plan_date, "times": self._plan_times, "done": self._plan_done},
            "manual": self._manual,
            "live_sessions": self._live,
            "planner": self._planner,
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        path = self._path
        try:
            await asyncio.to_thread(path.write_text, text, encoding="utf-8")
            self._dirty = False
        except OSError as exc:
            self._logger.warning(f"状态文件写入失败: {exc!r}")

    # ===== 去重 =====

    def planner_state(self, stream_id: str) -> Dict[str, float]:
        return dict(self._planner.get(stream_id) or {})

    def record_planner(self, stream_id: str, now: float, newest_share: float) -> None:
        self._planner[stream_id] = {"last_intent": now, "newest_share": newest_share}
        self._dirty = True

    def is_checked(self, bvid: str) -> bool:
        return bvid in self._checked

    def mark_checked(self, bvid: str) -> None:
        self._checked[bvid] = time.time()
        self._prune_checked()
        self._dirty = True

    def _prune_checked(self) -> None:
        deadline = time.time() - _CHECKED_KEEP_DAYS * 86400
        stale = [bvid for bvid, ts in self._checked.items() if ts < deadline]
        for bvid in stale:
            self._checked.pop(bvid, None)

    def is_sent(self, bvid: str) -> bool:
        return any(record.get("bvid") == bvid for record in self._sent)

    def record_sent(self, record: Dict[str, Any]) -> None:
        self._sent.append(record)
        self._dirty = True

    def recent_sent(self, limit: int = 10) -> List[Dict[str, Any]]:
        return self._sent[-limit:]

    # ===== 当日计划 =====

    def _roll_plan(self, now: datetime, times_per_day: int, hour_start: int, hour_end: int) -> None:
        """为今天生成 times_per_day 个随机发送时刻。"""
        today = now.strftime("%Y-%m-%d")
        self._plan_date = today
        self._plan_done = []
        count = max(1, times_per_day)
        if not 0 <= hour_start < 24 or not 0 <= hour_end <= 24:
            raise ValueError('active hours must use start 0..23 and end 0..24')
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = midnight + timedelta(hours=hour_start)
        end = midnight + timedelta(hours=hour_end)
        if end <= start:
            end = start + timedelta(hours=12)
        span = (end - start).total_seconds()
        # 均匀分段后在各段内随机，避免三次发送挤在同一小时
        step = span / count
        moments = [start.timestamp() + step * i + random.uniform(step * 0.1, step * 0.9) for i in range(count)]
        self._plan_times = sorted(moments)
        self._dirty = True

    def ensure_plan(self, now: datetime, times_per_day: int, hour_start: int, hour_end: int) -> None:
        if self._plan_date != now.strftime("%Y-%m-%d"):
            self._roll_plan(now, times_per_day, hour_start, hour_end)

    def next_due(self, now: datetime) -> Optional[float]:
        """返回下一个还没发过的计划时刻（已过期但未发送的也返回，交给调用方判断补发）。"""
        for moment in self._plan_times:
            if moment not in self._plan_done:
                return moment
        return None

    def mark_plan_done(self, moment: float) -> None:
        if moment not in self._plan_done:
            self._plan_done.append(moment)
        self._dirty = True

    def plan_summary(self, now: datetime) -> Dict[str, Any]:
        remaining = [m for m in self._plan_times if m not in self._plan_done]
        return {
            "date": self._plan_date,
            "total": len(self._plan_times),
            "done": len(self._plan_done),
            "times": [datetime.fromtimestamp(m).strftime("%H:%M") for m in self._plan_times],
            "remaining": [datetime.fromtimestamp(m).strftime("%H:%M") for m in remaining],
        }

    # ===== 活动聊天流 =====

    def remember_live_session(self, key: str, session_id: str) -> None:
        """记下某个群/某个人当前实际在用的聊天流（来自入站消息）。"""
        if not key or not session_id:
            return
        if self._live.get(key) == session_id:
            return
        self._live[key] = session_id
        self._dirty = True

    def live_session(self, key: str) -> str:
        return self._live.get(key, "")

    # ===== 手动（自然语言）触发 =====

    def _prune_manual(self, now_ts: float) -> None:
        deadline = now_ts - _MANUAL_KEEP_DAYS * 86400
        for key in list(self._manual):
            kept = [ts for ts in self._manual[key] if ts >= deadline]
            if kept:
                self._manual[key] = kept
            else:
                self._manual.pop(key, None)

    def manual_block_reason(self, key: str, min_gap_seconds: int) -> str:
        """返回不允许手动触发的原因；允许则返回空串。

        只挡「刚搬完又来一条」这种贴脸请求，不设冷却时长、也不设每日次数上限。
        min_gap_seconds <= 0 表示完全不挡。
        """
        if min_gap_seconds <= 0:
            return ""
        now_ts = time.time()
        self._prune_manual(now_ts)
        history = self._manual.get(key) or []
        if history:
            elapsed = now_ts - max(history)
            if elapsed < min_gap_seconds:
                return f"刚搬过（{int(elapsed)} 秒前），间隔太短，过 {int(min_gap_seconds - elapsed)} 秒再来"
        return ""

    def record_manual(self, key: str) -> None:
        self._manual.setdefault(key, []).append(time.time())
        self._prune_manual(time.time())
        self._dirty = True
