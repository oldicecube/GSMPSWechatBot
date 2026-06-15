import os
import threading
import time
import re
from datetime import date

from mcrcon import MCRcon

import services.mc_api as mc_api
from utils.points_manager import add_points
from utils.sqlite_store import (
    player_stats_get, player_stats_all, player_stats_ensure,
    player_stats_increment, player_stats_update,
)

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

login_times = {}
_rcon_config = None  # 全局RCON配置


def format_time(seconds):
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def current_time_iso(timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(timestamp))


def add_scoreboard_counts_to_stats(player, scoreboard_data):
    """将计分板数据原子累加到 player_stats 表"""
    for key, value in scoreboard_data.items():
        if isinstance(value, (int, float)) and int(value) > 0:
            player_stats_increment(player, key, int(value))


def update_daily_theroom_points(player, scoreboard_data):
    """
    计算 The Room 每日积分变化，返回 delta。
    读取当前值，计算新值，写回。
    """
    today_str = str(date.today())
    stats = player_stats_get(player)
    if not stats:
        return 0

    last_login = stats.get("last_login_date")
    current = stats.get("daily_theroom_points", 0)
    if not isinstance(current, (int, float)):
        current = 0

    if last_login != today_str:
        current = 0

    increment = 0
    for key in ["luckypillar_times", "battlepaint_times", "collapse_times"]:
        value = scoreboard_data.get(key, 0)
        if isinstance(value, (int, float)):
            increment += int(value)

    new_total = min(10, current + increment)
    delta = new_total - int(current)

    if delta != 0 or last_login != today_str:
        player_stats_update(player, daily_theroom_points=new_total)

    return delta


def init(config):
    """初始化player_monitor，获取rcon配置"""
    global _rcon_config
    _rcon_config = config.get("rcon", {})
    print("[MC] player monitor initialized with rcon config")


def execute_rcon_command(command):
    """执行RCON命令并返回结果"""
    if not _rcon_config:
        print("[MC RCON ERROR] RCON config not initialized")
        return None
    
    host = _rcon_config.get("host")
    port = _rcon_config.get("port")
    password = _rcon_config.get("password")
    
    if not all([host, port, password]):
        print("[MC RCON ERROR] Invalid rcon config")
        return None
    
    try:
        with MCRcon(host, password, port) as mcr:
            result = mcr.command(command)
            return result
    except Exception as e:
        print(f"[MC RCON ERROR] Failed to execute command: {e}")
        return None


def parse_scoreboard_response(response):
    """
    解析计分板返回值
    标准格式：player_name has 1 [scoreboard_name]
    返回分数，如果解析失败返回None
    """
    if not response:
        return None
    
    # 匹配 "player_name has N [scoreboard_name]" 的格式
    match = re.search(r'has\s+(\d+)\s+\[', response)
    if match:
        return int(match.group(1))
    
    return None


def get_player_scoreboard_data(player_name):
    """
    获取玩家的三个计分板数据
    返回字典：{"luckypillar_times": score, "battlepaint_times": score, "collapse_times": score}
    """
    scoreboards = ["luckypillar_times", "battlepaint_times", "collapse_times"]
    data = {}
    
    for scoreboard in scoreboards:
        command = f"scoreboard players get {player_name} {scoreboard}"
        response = execute_rcon_command(command)
        
        if response:
            print(f"[MC RCON] {command} -> {response}")
            score = parse_scoreboard_response(response)
            data[scoreboard] = score if score is not None else 0
        else:
            data[scoreboard] = 0
    
    return data


def reset_player_scoreboard(player_name):
    """重置玩家的三个计分板"""
    scoreboards = ["luckypillar_times", "battlepaint_times", "collapse_times"]
    
    for scoreboard in scoreboards:
        command = f"scoreboard players reset {player_name} {scoreboard}"
        response = execute_rcon_command(command)
        if response:
            print(f"[MC RCON] {command} -> {response}")


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
                    for player in left:
                        start_time = login_times.pop(player, None)

                        if start_time is None:
                            msgs.append(f"# 玩家离开: {player} (无上线记录)")
                            continue

                        duration = int(time.time() - start_time)
                        player_stats_ensure(player)
                        player_stats_increment(player, "total_time", duration)

                        print(f"[MC TIME] {player} session {duration}s")

                        # 查询RCON计分板数据并累加到player_stats
                        scoreboard_data = get_player_scoreboard_data(player)
                        add_scoreboard_counts_to_stats(player, scoreboard_data)
                        reset_player_scoreboard(player)

                        # 计算The Room每日积分变化
                        today_str = str(date.today())
                        pstats = player_stats_get(player)
                        last_login_date = pstats.get("last_login_date") if pstats else None
                        ther_room_delta = update_daily_theroom_points(player, scoreboard_data)
                        ther_room_points_awarded = 0
                        user_wxid = pstats.get("bind_user") if pstats else None
                        if ther_room_delta > 0 and user_wxid:
                            try:
                                add_points(user_wxid, ther_room_delta)
                                ther_room_points_awarded = ther_room_delta
                                print(f"[MC POINTS] {player} ({user_wxid}) The Room 今日参与次数奖励 +{ther_room_delta}")
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
                            leave_msg += f"\n获得 The Room 参与次数奖励积分 💎+{ther_room_points_awarded}"

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
