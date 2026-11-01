
# GSMPS WebUI metadata only; execution remains in this shell module.
WEBUI_CONFIG = {'version': 1,
 'fields': {'poll_interval_seconds': {'type': 'int',
                                      'default': 10,
                                      'min': 5,
                                      'max': 600,
                                      'label': '在线玩家轮询间隔（秒）'}}}

import os
import threading
import time
from datetime import date, datetime, timedelta

import services.mc_api as mc_api
from auto import tournament_monitor
from services.tournament import db as tournament_db
from utils.points_manager import add_points
from utils.sqlite_store import player_stats_get, player_stats_ensure, player_stats_update
from utils import player_rewards

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

login_times = {}
EVENT_HOST = 'event.sie.gdutmc.com'

def format_time(seconds):
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def current_time_iso(timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(timestamp))


def init(config):
    """初始化玩家监控与基于 Storage 的 The Room 对局同步。"""
    tournament_monitor.init(config or {})
    print("[MC] player monitor initialized with The Room Storage sync")


def update_daily_theroom_from_matches(player_name):
    """依据已入库的当日有效对局更新玩家统计并返回奖励差额。

    单局只会在 matches 表中出现一次；按 UUID 查询使改名不影响统计。
    daily_theroom_points 记录当天已发放的参与奖励，最高 10 分，避免同一
    玩家多次离服时重复发奖。
    """
    player_stats_ensure(player_name)
    stats = player_stats_get(player_name) or {}
    player = tournament_db.get_player_by_name(player_name)
    if not player:
        print(
            f"[MC THEROOM WARN] 未找到 {player_name} 的 UUID 映射，"
            "请确认 usercache 已更新后执行 /theroom --syncnames"
        )
        return {"total": 0, "luckypillar": 0, "miner_chaos": 0, "reward_delta": 0}

    day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    counts = tournament_db.get_daily_match_counts_by_uuid(
        player["uuid"],
        day_start.isoformat(timespec="seconds"),
        day_end.isoformat(timespec="seconds"),
    )
    lucky_count = counts.get("Lucky Pillar", 0)
    miner_count = counts.get("Miner Chaos", 0)
    total_count = sum(counts.values())

    today_str = str(date.today())
    already_awarded = stats.get("daily_theroom_points", 0)
    if stats.get("theroom_stats_date") != today_str:
        # 新的一天必须从 0 开始；旧版仅有 daily_theroom_points 的记录也不会跨日继承。
        already_awarded = 0
    try:
        already_awarded = max(0, int(already_awarded))
    except (TypeError, ValueError):
        already_awarded = 0

    target_award = min(10, total_count)
    reward_delta = max(0, target_award - already_awarded)
    player_stats_update(
        player_name,
        theroom_stats_date=today_str,
        theroom_match_count_today=total_count,
        theroom_luckypillar_today=lucky_count,
        theroom_miner_chaos_today=miner_count,
        daily_theroom_points=max(already_awarded, target_award),
    )
    return {
        "total": total_count,
        "luckypillar": lucky_count,
        "miner_chaos": miner_count,
        "reward_delta": reward_delta,
    }


def start(sender):
    """Run each endpoint independently; event scope never changes mc_api defaults."""
    for server_id, host in (('main', None), ('event', EVENT_HOST)):
        monitor = ServerMonitor(server_id, host)
        threading.Thread(target=monitor.run, args=(sender,), daemon=True,
                         name=f'player-monitor-{server_id}').start()


class ServerMonitor:
    def __init__(self, server_id, host=None):
        if server_id not in player_rewards.POLICIES:
            raise ValueError('Unknown monitor server')
        self.server_id = server_id
        self.host = host
        self.last = set()
        self.initialized = False
        self.empty_cooldown = False
        self.uncertain = False
        self.starts = login_times if server_id == 'main' else {}
        self._names = {}

    def poll(self, sender, *, now=None, interval=10):
        snapshot = mc_api.player_snapshot(host=self.host)
        if snapshot is None:
            self.uncertain = True
            print(f'[MC {self.server_id}] player snapshot unavailable/incomplete; no offline or reward inference')
            return
        self.accept(snapshot, sender, now=time.time() if now is None else now, interval=interval)

    def accept(self, snapshot, sender, *, now, interval=10):
        """Successful complete snapshot only; failed reads never become [] leaves."""
        current = {str(p).strip().casefold(): str(p).strip() for p in snapshot if str(p).strip()}
        cur = set(current)
        if self.last and not cur and not self.empty_cooldown:
            self.empty_cooldown = True
            return
        self.empty_cooldown = False
        baseline = not self.initialized or self.uncertain
        if self.server_id == 'main':
            for key, player in current.items():
                current[key] = player_rewards.ensure_primary_player(player, current_time_iso(now))
        rewards = player_rewards.observe(self.server_id, current.values(), now, baseline=baseline,
                                         max_gap_seconds=max(60, int(interval) * 3))
        for name, awarded in rewards.items():
            if awarded['login_points'] or awarded['active_points']:
                print(f"[MC POINTS {self.server_id}] {name} login=+{awarded['login_points']} active=+{awarded['active_points']}")
        joined, left = cur - self.last, self.last - cur
        for key in joined:
            self.starts[key] = now
        messages = []
        prefix = '# ' if self.server_id == 'main' else f'# 活动服 {EVENT_HOST} '
        if self.initialized and joined:
            players = []
            for key in sorted(joined):
                name = current[key]
                amount = rewards.get(name, {}).get('login_points', 0)
                players.append(name + (f' 获得每日登录积分💎+{amount}' if amount else ''))
            messages.append(prefix + '玩家进服: ' + ' '.join(players))
        if self.initialized and left:
            # Storage/RCON and match rewards belong exclusively to the original server.
            if self.server_id == 'main':
                try:
                    result = tournament_monitor.sync_new_matches()
                    print(f'[MC THEROOM] {result.get("message")}')
                except Exception as exc:
                    print(f'[MC THEROOM ERROR] {exc}')
            for key in sorted(left):
                name = self._names.get(key, key)
                started = self.starts.pop(key, None)
                duration = max(0, int(now - started)) if started is not None else 0
                message = prefix + f'玩家离开: {name} (本次在线 {format_time(duration)})'
                row = player_stats_get(name) or {}
                if row.get('bind_user'):
                    message += f" 今日活跃累计 💎{row.get('online_time_points_today', 0)}（两服共享）"
                if self.server_id == 'main':
                    try:
                        stats = update_daily_theroom_from_matches(name)
                        row = player_stats_get(name) or {}
                        if stats['reward_delta'] > 0 and row.get('bind_user'):
                            add_points(row['bind_user'], stats['reward_delta'])
                            message += f"\nThe Room 今日 {stats['total']} 局，获得参与奖励 💎+{stats['reward_delta']}"
                    except Exception as exc:
                        print(f'[MC THEROOM ERROR] {name}: {exc}')
                messages.append(message)
        self.last, self._names = cur, current
        self.initialized = True
        self.uncertain = False
        if messages:
            try:
                sender('\n'.join(messages))
            except Exception as exc:
                print(f'[MC {self.server_id}] sender failed: {exc}')

    def run(self, sender):
        from core.maibot_shell_registry import allowed
        from core.shell_plugin_config import settings
        print(f'[MC] monitor started: {self.server_id} {self.host or "configured primary"}')
        while True:
            if not allowed('gsmps.automation.player_monitor'):
                self.uncertain = True
                time.sleep(5)
                continue
            interval = 10
            try:
                interval = settings('gsmps.automation.player_monitor')['poll_interval_seconds']
                self.poll(sender, interval=interval)
            except Exception as exc:
                self.uncertain = True
                print(f'[MC {self.server_id}] monitor error: {exc}')
            time.sleep(interval)
