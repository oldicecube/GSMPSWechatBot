# -*- coding: utf-8 -*-
"""Daily content lottery v2.

Public gameplay is intentionally simple: choose up to five of six daily content
cards, stake 10..100 points per card, and two cards are drawn at 22:00.
Whether a content actually happens has no relationship with winning odds; the
winning cards are selected by a cryptographically strong random source.

The LLM integration is optional and may generate only playful labels. It never
chooses winners, odds, payout budgets, or eligibility.
"""
from __future__ import annotations

import json
import math
import re
import secrets
import statistics
import threading
import time
from collections import defaultdict
from datetime import date, datetime, timedelta

from core.sender import send
from utils.points_manager import add_points, get_points, try_spend_points
from utils.sqlite_store import (
    lottery_add_bet,
    lottery_create_draw,
    lottery_create_options,
    lottery_daily_rank,
    lottery_daily_total,
    lottery_get_draw,
    lottery_get_fund,
    lottery_get_options,
    lottery_get_player_streak,
    lottery_history,
    lottery_pending_bets,
    lottery_settle_bet,
    lottery_update_draw_totals,
    lottery_update_fund,
    lottery_update_option,
    lottery_update_player_streak,
    lottery_user_bets,
)

COMMAND = "/lottery"
ALIASES = ["/彩票"]

_config = {}
_llm_service = None
_lock = threading.RLock()
_draw_thread = None
_draw_stop = threading.Event()
_option_generation_days = set()

DEFAULTS = {
    "enabled": True,
    "min_stake": 10,
    "max_stake_per_option": 100,
    "max_options_per_user_per_day": 5,
    "daily_total_limit": 300,
    "option_count": 6,
    "winner_count": 2,
    "base_payout_budget": 0.70,
    "participation_rebate_rate": 0.10,
    "loss_protection_rate": 0.20,
    "loss_protection_max": 50,
    "min_odds": 1.50,
    "max_odds": 3.00,
    "initial_pool": 500.0,
    "draw_hour": 22,
    "draw_minute": 0,
    "bet_close_minutes": 5,
    "rank_size": 10,
    "announce_groups": [],
    "llm_content_enabled": False,
}

DEFAULT_LABELS = [
    "服务器今晚稳定运行",
    "今晚有人发新图",
    "群里出现整活消息",
    "Minecraft 话题继续",
    "今晚有人约联机",
    "Bot 被群友吐槽",
]
DEFAULT_BASE_ODDS = [2.10, 2.35, 1.85, 2.00, 2.25, 2.55]
_BET_TOKEN_RE = re.compile(r"^(\d{1,2})[=:](\d+(?:\.\d+)?)$")


def set_llm_service(service):
    """Optional dependency injection; LLM may generate labels only."""
    global _llm_service
    _llm_service = service


def init(config):
    global _config, _draw_thread
    _config = config if isinstance(config, dict) else {}
    with _lock:
        if _draw_thread is None or not _draw_thread.is_alive():
            _draw_stop.clear()
            _draw_thread = threading.Thread(target=_draw_loop, daemon=True, name="daily-lottery-draw")
            _draw_thread.start()


def _settings():
    merged = dict(DEFAULTS)
    raw = ((_config or {}).get("lottery") or {}) if isinstance(_config, dict) else {}
    if isinstance(raw, dict):
        merged.update(raw)
    from core.shell_plugin_config import settings, COMMON_FIELDS
    merged.update({k:v for k,v in settings('gsmps.command.lottery').items() if k not in COMMON_FIELDS})
    return merged


def _today():
    return date.today().isoformat()


def _now():
    return datetime.now()


def _draw_time(day):
    cfg = _settings()
    try:
        hour = max(0, min(int(cfg.get("draw_hour", 22)), 23))
        minute = max(0, min(int(cfg.get("draw_minute", 0)), 59))
    except (TypeError, ValueError):
        hour, minute = 22, 0
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)


def _close_time(day):
    try:
        minutes = max(0, min(int(_settings().get("bet_close_minutes", 5)), 120))
    except (TypeError, ValueError):
        minutes = 5
    return _draw_time(day) - timedelta(minutes=minutes)


def _betting_open(day=None):
    return _now() < _close_time(day or date.today())


def _option_count():
    try:
        return max(3, min(int(_settings().get("option_count", 6)), 12))
    except (TypeError, ValueError):
        return 6


def _winner_count():
    return max(1, min(int(_settings().get("winner_count", 2) or 2), _option_count() - 1))


def _option_id(value):
    text = str(value or "").strip()
    if not re.fullmatch(r"\d{1,2}", text):
        return None
    number = int(text)
    return f"{number:02d}" if 1 <= number <= _option_count() else None


def _float_setting(name, default, low=0.0, high=999999.0):
    try:
        return max(low, min(float(_settings().get(name, default)), high))
    except (TypeError, ValueError):
        return default


def _llm_labels(day_text):
    """Generate labels only when explicitly enabled; fallback is deterministic."""
    if not _llm_service or not bool(_settings().get("llm_content_enabled", False)):
        return []
    if day_text in _option_generation_days:
        return []
    _option_generation_days.add(day_text)
    try:
        response = _llm_service._provider_call([
            {"role": "system", "content": "只输出 JSON 数组，不要解释。生成 6 个适合群聊积分小游戏的中性趣味内容标题，每条不超过 18 个字。不要暗示内容发生会影响中奖概率，不要生成真实承诺。"},
            {"role": "user", "content": f"日期：{day_text}。输出格式：[\"内容1\",\"内容2\",\"内容3\",\"内容4\",\"内容5\",\"内容6\"]"},
        ], thinking={})
        raw = str(getattr(response, "content", response) or "")
        data = json.loads(raw[raw.find("["): raw.rfind("]") + 1])
        labels = [str(item).strip()[:18] for item in data if str(item).strip()]
        if len(labels) >= _option_count():
            return labels[:_option_count()]
    except Exception as exc:
        print(f"[LOTTERY] optional LLM label generation skipped: {type(exc).__name__}: {exc}", flush=True)
    return []


def _daily_options(day=None):
    day_text = (day or date.today()).isoformat() if isinstance(day or date.today(), date) else str(day)
    existing = lottery_get_options(day_text)
    if len(existing) >= _option_count():
        return existing
    cfg_labels = _settings().get("options")
    labels = []
    base_odds = []
    if isinstance(cfg_labels, list):
        for item in cfg_labels[:_option_count()]:
            if isinstance(item, dict):
                labels.append(str(item.get("label") or "").strip()[:18])
                base_odds.append(_float_value(item.get("base_odds"), 2.0))
    llm_labels = _llm_labels(day_text)
    if llm_labels:
        labels = llm_labels
    labels += [x for x in DEFAULT_LABELS if x not in labels]
    labels = (labels + [f"今日趣味内容 {i:02d}" for i in range(1, _option_count() + 1)])[:_option_count()]
    odds_cfg = _settings().get("base_odds")
    if isinstance(odds_cfg, list):
        base_odds = [_float_value(x, 2.0) for x in odds_cfg[:_option_count()]]
    base_odds = (base_odds + DEFAULT_BASE_ODDS + [2.1] * _option_count())[:_option_count()]
    options = [
        {"option_id": f"{i:02d}", "label": labels[i - 1], "base_odds": max(1.5, min(2.7, base_odds[i - 1]))}
        for i in range(1, _option_count() + 1)
    ]
    return lottery_create_options(day_text, options)


def _float_value(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _announce_groups():
    raw = _settings().get("announce_groups") or ((_config or {}).get("target_group") or [])
    if isinstance(raw, str):
        raw = [raw]
    return [str(item).strip() for item in raw if str(item).strip()]


def _announce(draw):
    if not draw:
        return
    options = {x["option_id"]: x for x in _daily_options(date.fromisoformat(draw["draw_date"]))}
    labels = [options.get(x, {}).get("label", x) for x in draw.get("winning_options") or []]
    message = (
        "🎉【今日积分内容开奖】\n"
        f"今日随机抽中的内容：{', '.join(labels) or '暂无'}\n"
        "提醒：内容是否真实发生，与中奖概率无关；本期开奖为独立随机抽取。\n"
        "可使用 /lottery mine 查看自己的结果。"
    )
    for group in _announce_groups():
        try:
            ok, error = send(target=group, content=message, mode="wechat_text")
            if not ok:
                print(f"[LOTTERY] announcement failed group={group} error={error}", flush=True)
        except Exception as exc:
            print(f"[LOTTERY] announcement error group={group}: {exc}", flush=True)


def _draw_loop():
    last_draw_date = None
    while not _draw_stop.is_set():
        from core.maibot_shell_registry import allowed
        if not allowed('gsmps.command.lottery') or not _settings().get('enabled', True):
            _draw_stop.wait(5)
            continue
        today = _now().date()
        if _now() >= _draw_time(today) and last_draw_date != today:
            try:
                with _lock:
                    draw = _settle_day(today)
                if draw:
                    _announce(draw)
                last_draw_date = today
            except Exception as exc:
                print(f"[LOTTERY] automatic draw failed date={today}: {exc}", flush=True)
        _draw_stop.wait(20)


def _settle_due_draws():
    today = date.today()
    if _now() >= _draw_time(today):
        _settle_day(today)


def _compute_odds(options, stakes, total_bet, protection_total):
    cfg = _settings()
    target = max(0.0, total_bet * _float_setting("base_payout_budget", 0.70, 0.0, 1.0) - protection_total)
    min_odds = _float_setting("min_odds", 1.5, 0.2, 3.0)
    max_odds = _float_setting("max_odds", 3.0, min_odds, 3.0)
    active = [float(v) for v in stakes.values() if float(v) > 0]
    reference = statistics.median(active) if active else 1.0
    raw = {}
    for item in options:
        option_id = item["option_id"]
        stake = float(stakes.get(option_id, 0.0))
        demand = ((reference / max(stake, 1.0)) ** 0.35) if stake else 1.15
        demand = max(0.85, min(1.20, demand))
        raw[option_id] = max(1.5, min(2.7, float(item.get("base_odds") or 2.0))) * demand
        lottery_update_option(item["draw_date"], option_id, total_stake=stake, demand_factor=demand)
    expected_factor = _winner_count() / float(len(options))
    if target <= 0 or not any(stakes.values()):
        scale = 1.0
    else:
        low, high = 0.05, 10.0
        for _ in range(50):
            mid = (low + high) / 2
            estimated = expected_factor * sum(float(stakes.get(k, 0.0)) * max(min_odds, min(max_odds, v * mid)) for k, v in raw.items())
            if estimated < target:
                low = mid
            else:
                high = mid
        scale = high
    final = {k: round(max(min_odds, min(max_odds, value * scale)), 2) for k, value in raw.items()}
    return final, target


def _refresh_live_odds(day=None):
    """Recalculate visible reference odds after every new bet.

    These are display odds only; final odds are recalculated and locked at
    settlement. Content occurrence never changes the random draw probability.
    """
    day = day or date.today()
    day_text = day.isoformat() if isinstance(day, date) else str(day)
    options = _daily_options(day if isinstance(day, date) else date.fromisoformat(day_text))
    pending = lottery_pending_bets(day_text)
    stakes = defaultdict(float)
    for bet in pending:
        if str(bet.get("number")) in {x["option_id"] for x in options}:
            stakes[str(bet.get("number"))] += float(bet.get("amount") or 0)
    total = sum(stakes.values())
    odds, _ = _compute_odds(options, stakes, total, 0.0)
    for item in options:
        lottery_update_option(day_text, item["option_id"], locked_odds=odds.get(item["option_id"], item.get("base_odds", 2.0)))
    return lottery_get_options(day_text)


def _settle_day(day):
    day_text = day.isoformat() if isinstance(day, date) else str(day)
    existing = lottery_get_draw(day_text)
    if existing:
        return existing
    pending = lottery_pending_bets(day_text)
    options = _daily_options(date.fromisoformat(day_text))
    if not pending:
        return None
    winners = secrets.SystemRandom().sample([x["option_id"] for x in options], _winner_count())
    stakes = defaultdict(float)
    by_user = defaultdict(list)
    for bet in pending:
        stakes[str(bet["number"])] += float(bet.get("amount") or 0)
        by_user[str(bet.get("wxid") or "")].append(bet)
    total_bet = sum(float(x.get("amount") or 0) for x in pending)
    protection_targets = {}
    protection_rate = _float_setting("loss_protection_rate", 0.20, 0.0, 1.0)
    protection_max = _float_setting("loss_protection_max", 50, 0.0, 1000.0)
    for wxid, bets in by_user.items():
        old = lottery_get_player_streak(wxid)
        hit = any(str(b["number"]) in winners for b in bets)
        if not hit and int(old.get("loss_streak", 0)) >= 3:
            protection_targets[wxid] = min(sum(float(b["amount"]) for b in bets) * protection_rate, protection_max)
    protection_total = sum(protection_targets.values())
    odds, payout_budget = _compute_odds(options, stakes, total_bet, protection_total)
    rebate_rate = _float_setting("participation_rebate_rate", 0.10, 0.0, 0.5)
    desired_win = {b["id"]: (float(b["amount"]) * odds.get(str(b["number"]), 0.0) if str(b["number"]) in winners else 0.0) for b in pending}
    desired_fixed = {b["id"]: float(b["amount"]) * rebate_rate for b in pending}
    for wxid, bonus in protection_targets.items():
        total_user = sum(float(b["amount"]) for b in by_user[wxid]) or 1.0
        for b in by_user[wxid]:
            desired_fixed[b["id"]] += bonus * float(b["amount"]) / total_user
    available = lottery_get_fund()["balance"] + total_bet * (1.0 - _float_setting("house_edge", 0.20, 0.0, 1.0))
    fixed_total = sum(desired_fixed.values())
    desired_win_total = sum(desired_win.values())
    win_scale = min(1.0, max(0.0, (available - fixed_total) / desired_win_total)) if desired_win_total else 0.0
    actual_total = 0.0
    for bet in pending:
        amount = float(bet["amount"])
        win = desired_win[bet["id"]] * win_scale
        fixed = desired_fixed[bet["id"]]
        actual = win + fixed
        status = "won" if str(bet["number"]) in winners else ("protected" if fixed > amount * rebate_rate else "rebate")
        lottery_settle_bet(
            bet["id"], status, actual, locked_odds=odds.get(str(bet["number"]), 0.0),
            participation_rebate=amount * rebate_rate,
            protection_rebate=max(0.0, fixed - amount * rebate_rate),
        )
        if actual > 0:
            add_points(bet.get("wxid"), actual)
        actual_total += actual
    for wxid, bets in by_user.items():
        old = lottery_get_player_streak(wxid)
        hit = any(str(b["number"]) in winners for b in bets)
        previous = int(old.get("loss_streak", 0))
        lottery_update_player_streak(
            wxid,
            loss_streak=0 if hit or wxid in protection_targets else previous + 1,
            last_bet_date=day_text,
            last_win_date=day_text if hit else old.get("last_win_date"),
            protection_available=False,
            protection_used_date=day_text if wxid in protection_targets else old.get("protection_used_date"),
        )
    house_edge = _float_setting("house_edge", 0.20, 0.0, 1.0)
    house_fee = total_bet * house_edge
    lottery_create_draw(
        day_text, ",".join(winners), total_bet, actual_total,
        winning_options=winners, option_count=len(options), payout_budget=payout_budget,
        participation_budget=total_bet * rebate_rate, protection_budget=protection_total,
    )
    for item in options:
        lottery_update_option(day_text, item["option_id"], locked_odds=odds.get(item["option_id"], 0.0), is_winner=item["option_id"] in winners)
    fund = lottery_get_fund()
    lottery_update_fund(
        balance=max(0.0, available - actual_total),
        seed_debt=max(0.0, float(fund.get("seed_debt") or 0) - house_fee),
        total_house_fee=float(fund.get("total_house_fee") or 0) + house_fee,
        month_bet=float(fund.get("month_bet") or 0) + total_bet,
        month_prize=float(fund.get("month_prize") or 0) + actual_total,
        month_house_fee=float(fund.get("month_house_fee") or 0) + house_fee,
        month_participation=float(fund.get("month_participation") or 0) + total_bet * rebate_rate,
        month_protection=float(fund.get("month_protection") or 0) + protection_total,
    )
    return lottery_get_draw(day_text)


def _parse_bets(content):
    text = str(content or "").strip()
    parts = text.split()
    if parts and parts[0].lower() in {"bet", "下注"}:
        parts = parts[1:]
    result = []
    if any("=" in item or ":" in item for item in parts):
        for item in parts:
            match = _BET_TOKEN_RE.fullmatch(item)
            if not match:
                return None
            result.append((_option_id(match.group(1)), float(match.group(2))))
    else:
        if len(parts) % 2 != 0:
            return None
        for i in range(0, len(parts), 2):
            result.append((_option_id(parts[i]), _float_value(parts[i + 1], -1)))
    if not result or any(option is None for option, _ in result):
        return None
    merged = defaultdict(float)
    for option, amount in result:
        merged[option] += amount
    return list(merged.items())


def _place_bet(content, context):
    wxid = str(context.get("wxid") or context.get("raw", {}).get("wxid") or "").strip()
    nickname = str(context.get("user") or context.get("nickname") or "").strip()
    if not wxid:
        return "暂时无法识别你的用户信息。"
    day = date.today()
    if not _betting_open(day):
        return "今天已经封盘，等 22:00 开奖后再来参加下一轮吧。"
    parsed = _parse_bets(content)
    if not parsed:
        return "格式：/lottery bet 01 20 03 10；每项 10-100 积分。"
    cfg = _settings()
    minimum = _float_setting("min_stake", 10, 1, 1000)
    maximum = _float_setting("max_stake_per_option", 100, minimum, 10000)
    total_limit = _float_setting("daily_total_limit", 300, maximum, 100000)
    if len(parsed) > int(cfg.get("max_options_per_user_per_day", 5)):
        return f"今天最多选择 {int(cfg.get('max_options_per_user_per_day', 5))} 个不同内容。"
    options = {x["option_id"]: x for x in _daily_options(day)}
    current = lottery_user_bets(_today(), wxid)
    current_by_option = defaultdict(float)
    for row in current:
        current_by_option[str(row.get("number"))] += float(row.get("amount") or 0)
    existing_ids = set(current_by_option)
    if len(existing_ids | {x[0] for x in parsed}) > int(cfg.get("max_options_per_user_per_day", 5)):
        return f"今天最多选择 {int(cfg.get('max_options_per_user_per_day', 5))} 个不同内容。"
    total_new = sum(amount for _, amount in parsed)
    total_old = sum(current_by_option.values())
    if total_old + total_new > total_limit:
        return f"今日总投入最多 {total_limit:.0f} 积分。"
    for option, amount in parsed:
        if amount < minimum or amount > maximum or abs(amount % 10) > 1e-9:
            return f"每个内容必须按 10 积分递增，单项范围 {minimum:.0f}-{maximum:.0f}。"
        if current_by_option[option] + amount > maximum:
            return f"内容 {option} 的累计投注不能超过 {maximum:.0f} 积分。"
    if get_points(wxid) < total_new:
        return f"积分不足，需要 {total_new:.0f}，当前只有 {get_points(wxid):.0f}。"
    ok, _ = try_spend_points(wxid, total_new)
    if not ok:
        return "积分扣除失败，请稍后重试。"
    try:
        for option, amount in parsed:
            meta = options[option]
            lottery_add_bet(day.isoformat(), wxid, nickname, option, amount,
                            option_label=meta["label"], base_odds=meta["base_odds"])
        _refresh_live_odds(day)
    except Exception:
        add_points(wxid, total_new)
        raise
    summary = "、".join(f"{option} {amount:.0f}" for option, amount in parsed)
    return f"已加入今日内容：{summary} 积分。封盘前可继续选择其他内容；内容是否发生与中奖概率无关，开奖完全随机。"


def _help_text():
    return (
        "🎟️【每日积分内容彩票】\n"
        "/lottery：查看今天的 6 个内容和当前赔率\n"
        "/lottery bet 01 20：给内容 01 投 20 积分\n"
        "/lottery bet 01=20 03=10：一次选择多个内容\n"
        "/lottery mine：查看今天和最近的参与记录\n"
        "/lottery history：查看历史结果\n"
        "/lottery rank：查看今日参与榜\n\n"
        "每个内容 10-100 积分，每天最多选择 5 个内容，总投入最多 300 积分。\n"
        "每天 22:00 随机抽取 2 个内容；内容是否真实发生，与中奖概率无关。"
    )


def _today_text():
    options = _refresh_live_odds(date.today())
    lines = ["🎟️【今日内容卡】", "内容是否发生与中奖概率无关，本轮开奖完全随机。"]
    for item in options:
        odds = item.get("locked_odds") or item.get("base_odds") or 2.0
        lines.append(f"{item['option_id']} · {item['label']} · 当前参考赔率 {odds:.2f}x")
    lines.append("使用 /lottery bet 01 20 参与；赔率以封盘时为准。")
    return "\n".join(lines)


def _history_text(wxid):
    rows = lottery_history(wxid, limit=10)
    if not rows:
        return "暂无参与记录。"
    return "我的最近记录\n" + "\n".join(
        f"{row['draw_date']} 内容{row['number']} 投{row['amount']:.0f} 结果：{row['status']} +{row['prize']:.1f}"
        for row in rows
    )


def _rank_text():
    rows = lottery_daily_rank(_today(), limit=int(_settings().get("rank_size", 10)))
    if not rows:
        return "今天还没有参与记录。"
    return "今日参与榜\n" + "\n".join(f"{i}. {r.get('nickname') or r.get('wxid')} · {r['amount']:.0f} 积分" for i, r in enumerate(rows, 1))


def _pool_text():
    gross, count = lottery_daily_total(_today())
    return f"今天已有 {count} 条参与记录，共投入 {gross:.0f} 积分。具体结果请以 22:00 开奖为准。"


def _is_admin(context):
    wxid = str(context.get("wxid") or "").strip()
    admins = ((_config or {}).get("llm") or {}).get("admin_wxids") or []
    return wxid in {str(item).strip() for item in admins}


def handle(content, context):
    if not _settings().get('enabled', True):
        return '彩票功能已停用。'
    context = context if isinstance(context, dict) else {}
    from utils.points_manager import binding_required
    error = binding_required(context)
    if error:
        return error
    content = str(content or "").strip()
    with _lock:
        _settle_due_draws()
        first = content.split()[0].lower() if content else ""
        wxid = str(context.get("wxid") or context.get("raw", {}).get("wxid") or "").strip()
        if first in {"help", "帮助"}:
            return _help_text()
        if first in {"mine", "我的", "history", "历史"}:
            return _history_text(wxid) if wxid else "暂时无法识别你的用户信息。"
        if first in {"rank", "排行"}:
            return _rank_text()
        if first in {"pool", "奖池", "stats", "统计"}:
            return _pool_text()
        if first in {"draw", "开奖"}:
            if not _is_admin(context):
                return "只有管理员可以手动开奖。"
            draw = _settle_day(date.today())
            return "今天还没有参与记录。" if not draw else f"手动开奖完成：{', '.join(draw.get('winning_options') or [])}"
        if not content or first in {"today", "今天", "内容"}:
            return _today_text()
        return _place_bet(content, context)

WEBUI_CONFIG = {'version': 1, 'fields': {
    'enabled': {'type': 'bool', 'default': True, 'label': '允许彩票业务', 'config_path': 'lottery.enabled', 'reload': 'hot'},
    'min_stake': {'type': 'int', 'default': 10, 'min': 1, 'max': 100000, 'label': '最小投注积分', 'config_path': 'lottery.min_stake', 'reload': 'hot'},
    'max_stake_per_option': {'type': 'int', 'default': 100, 'min': 1, 'max': 100000, 'label': '单项投注上限', 'config_path': 'lottery.max_stake_per_option', 'reload': 'hot'},
    'max_options_per_user_per_day': {'type': 'int', 'default': 5, 'min': 1, 'max': 6, 'label': '每人每日选项上限', 'config_path': 'lottery.max_options_per_user_per_day', 'reload': 'hot'},
    'daily_total_limit': {'type': 'int', 'default': 300, 'min': 1, 'max': 100000, 'label': '每人每日积分上限', 'config_path': 'lottery.daily_total_limit', 'reload': 'hot'},
    'draw_hour': {'type': 'int', 'default': 22, 'min': 0, 'max': 23, 'label': '开奖小时', 'config_path': 'lottery.draw_hour', 'reload': 'hot'},
    'draw_minute': {'type': 'int', 'default': 0, 'min': 0, 'max': 59, 'label': '开奖分钟', 'config_path': 'lottery.draw_minute', 'reload': 'hot'},
    'bet_close_minutes': {'type': 'int', 'default': 5, 'min': 0, 'max': 120, 'label': '提前封盘（分钟）', 'config_path': 'lottery.bet_close_minutes', 'reload': 'hot'},
    'rank_size': {'type': 'int', 'default': 10, 'min': 1, 'max': 50, 'label': '排行榜人数', 'config_path': 'lottery.rank_size', 'reload': 'hot'},
    'option_count': {'type': 'int', 'default': 6, 'min': 3, 'max': 12, 'label': '每日选项数量', 'config_path': 'lottery.option_count', 'reload': 'hot'},
    'winner_count': {'type': 'int', 'default': 2, 'min': 1, 'max': 11, 'label': '每日中奖选项数量', 'config_path': 'lottery.winner_count', 'reload': 'hot'},
    'options': {'type': 'json', 'default': [], 'label': '选项文案与基础赔率', 'config_path': 'lottery.options', 'reload': 'hot'},
    'base_odds': {'type': 'json', 'default': [], 'label': '基础赔率列表（数字数组）', 'config_path': 'lottery.base_odds', 'reload': 'hot'},
    'announce_groups': {'type': 'list', 'item_type': 'str', 'default': [], 'label': '开奖通知群', 'config_path': 'lottery.announce_groups', 'reload': 'hot'},
    'llm_content_enabled': {'type': 'bool', 'default': False, 'label': '使用模型生成选项文案', 'config_path': 'lottery.llm_content_enabled', 'reload': 'hot'},
}}

WEBUI_CONFIG_SCHEMA = {
    "title": "彩票",
    "fields": [
        {"key": "enabled", "type": "boolean", "label": "启用"},
        {"key": "min_stake", "type": "number", "label": "最小投注"},
        {"key": "max_stake_per_option", "type": "number", "label": "单项投注上限"},
        {"key": "max_options_per_user_per_day", "type": "number", "label": "每日选项上限"},
        {"key": "daily_total_limit", "type": "number", "label": "每日总额上限"},
        {"key": "draw_hour", "type": "number", "label": "开奖小时"},
        {"key": "draw_minute", "type": "number", "label": "开奖分钟"},
        {"key": "announce_groups", "type": "list", "label": "公告群组"},
        {"key": "option_count", "type": "number", "label": "每日选项数量"},
        {"key": "winner_count", "type": "number", "label": "中奖选项数量"},
        {"key": "options", "type": "json", "label": "选项文案与基础赔率"},
        {"key": "base_odds", "type": "json", "label": "基础赔率列表"},
    ],
}
