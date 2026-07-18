import os
import threading
import time
from datetime import date, datetime, timedelta

import services.mc_api as mc_api
from auto import tournament_monitor
from services.tournament import db as tournament_db
from utils.points_manager import add_points
from utils.sqlite_store import (
    player_stats_get, player_stats_all, player_stats_ensure,
    player_stats_increment, player_stats_update,
)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

login_times = {}


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
    print("[MC] player monitor started")

    def loop():
        print("[MC] monitor loop started")

        last = set()
        tick = 0
        initialized = False
        empty_cooldown = False  # 空列表冷却标记：防止因拉取失败/假人移除导致的误判

        while True:
            tick += 1

            try:
                print(f"[MC] tick={tick} fetching player list...")

                cur_raw = mc_api.player_list()
                cur = set(cur_raw or [])
                now = time.time()
                now_iso = current_time_iso(now)

                if not initialized:
                    for player in cur:
                        if player not in login_times:
                            login_times[player] = now
                        player_stats_ensure(player, first_join_at=now_iso)

                    last = cur
                    initialized = True
                    print(f"[MC] initial online player baseline: {sorted(cur)}")
                    time.sleep(10)
                    continue

                # 冷却机制：上次有玩家，本次变为空 → 冷却本次结果，等待下次确认
                if last and not cur:
                    if not empty_cooldown:
                        empty_cooldown = True
                        print(f"[MC] tick={tick} 玩家列表由非空变为空，进入冷却，等待下次确认")
                        time.sleep(10)
                        continue
                    else:
                        # 连续两次为空，确认玩家真的全部离开了
                        empty_cooldown = False
                        print(f"[MC] tick={tick} 连续两次拉取为空，确认玩家已离开")
                else:
                    # 本次有玩家，清除冷却标记
                    empty_cooldown = False

                joined = cur - last
                left = last - cur
                msgs = []

                for player in joined:
                    if player not in login_times:
                        login_times[player] = now

                if joined:
                    joined_msgs = []

                    for player in joined:
                        player_stats_ensure(player, first_join_at=now_iso)

                        # 检查是否是当日首次进服
                        today_str = str(date.today())
                        pstats = player_stats_get(player)
                        last_login_date = pstats.get("last_login_date") if pstats else None

                        join_msg = player
                        if last_login_date != today_str:
                            # 是新的一天，首次进服 +5 积分
                            user_wxid = pstats.get("bind_user") if pstats else None
                            if user_wxid:
                                try:
                                    add_points(user_wxid, 5)
                                    player_stats_update(
                                        player,
                                        last_login_date=today_str,
                                        login_points_today=5,
                                        online_time_points_today=0,
                                    )
                                    join_msg += " 获得每日登录积分💎+5"
                                    print(f"[MC POINTS] {player} ({user_wxid}) 首次进服，获得 +5 积分")
                                except Exception as e:
                                    print(f"[MC POINTS ERROR] {player} 积分增加失败: {e}")
                            else:
                                player_stats_update(
                                    player,
                                    last_login_date=today_str,
                                    login_points_today=5,
                                    online_time_points_today=0,
                                )

                        joined_msgs.append(join_msg)

                    msgs.append("# 玩家进服: " + " ".join(joined_msgs))

                if left:
                    # 本批离服玩家共享一次 Storage 同步，避免重复 RCON 拉取。
                    try:
                        sync_result = tournament_monitor.sync_new_matches()
                        if not sync_result.get("ok"):
                            print(f"[MC THEROOM WARN] 对局同步失败: {sync_result.get('message')}")
                        else:
                            print(f"[MC THEROOM] {sync_result.get('message')}")
                    except Exception as e:
                        print(f"[MC THEROOM ERROR] 同步对局失败: {e}")

                    for player in left:
                        start_time = login_times.pop(player, None)

                        if start_time is None:
                            msgs.append(f"# 玩家离开: {player} (无上线记录)")
                            continue

                        duration = int(time.time() - start_time)
                        player_stats_ensure(player)
                        player_stats_increment(player, "total_time", duration)

                        print(f"[MC TIME] {player} session {duration}s")

                        # 基于已入库 Storage 对局计算今日 The Room 次数与积分差额。
                        today_str = str(date.today())
                        pstats = player_stats_get(player)
                        last_login_date = pstats.get("last_login_date") if pstats else None
                        theroom_stats = update_daily_theroom_from_matches(player)
                        ther_room_delta = theroom_stats["reward_delta"]
                        ther_room_points_awarded = 0
                        user_wxid = pstats.get("bind_user") if pstats else None
                        if ther_room_delta > 0 and user_wxid:
                            try:
                                add_points(user_wxid, ther_room_delta)
                                ther_room_points_awarded = ther_room_delta
                                print(
                                    f"[MC POINTS] {player} ({user_wxid}) The Room 今日参与奖励 "
                                    f"+{ther_room_delta}（总 {theroom_stats['total']} 局："
                                    f"LP {theroom_stats['luckypillar']} / MC {theroom_stats['miner_chaos']}）"
                                )
                            except Exception as e:
                                print(f"[MC POINTS ERROR] {player} The Room 积分增加失败: {e}")

                        # 计算在线时长奖励积分
                        online_time_points_today = pstats.get("online_time_points_today", 0) if pstats else 0
                        if not isinstance(online_time_points_today, (int, float)):
                            online_time_points_today = 0
                        leave_msg = f"# 玩家离开: {player} (本次在线 {format_time(duration)}"
                        points_earned = 0

                        if last_login_date == today_str and online_time_points_today < 10:
                            minutes_online = max(1, duration // 60)  # 向下取整，最少 1 分钟
                            points_to_add = min(minutes_online, 10 - online_time_points_today)  # 不超过上限

                            if user_wxid:
                                try:
                                    add_points(user_wxid, points_to_add)
                                    new_online_pts = online_time_points_today + points_to_add
                                    player_stats_update(player, online_time_points_today=new_online_pts)
                                    points_earned = points_to_add
                                    print(f"[MC POINTS] {player} ({user_wxid}) 在线 {minutes_online} 分钟，获得 +{points_to_add} 积分 (每日上限: {new_online_pts}/10)")
                                except Exception as e:
                                    print(f"[MC POINTS ERROR] {player} 在线时长积分增加失败: {e}")

                        if points_earned > 0:
                            leave_msg += f" 获得每日活跃奖励积分 💎+{points_earned}"

                        if ther_room_points_awarded > 0:
                            if points_earned > 0:
                                leave_msg += " "
                            leave_msg += (
                                f"\nThe Room 今日 {theroom_stats['total']} 局"
                                f"（LP {theroom_stats['luckypillar']} / MC {theroom_stats['miner_chaos']}），"
                                f"获得参与奖励 💎+{ther_room_points_awarded}"
                            )

                        leave_msg += ")"
                        msgs.append(leave_msg)

                if msgs:
                    final_msg = "\n".join(msgs)

                    try:
                        sender(final_msg)
                        print("[MC SEND] sent")
                    except Exception as e:
                        print("[MC ERROR] sender failed:", e)

                last = cur

            except Exception as e:
                print("[MC ERROR] loop exception:", e)

            time.sleep(10)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
