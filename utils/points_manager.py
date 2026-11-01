from datetime import datetime, date


def binding_required(context):
    from utils.account_identity import actor, bound_user
    return None if bound_user(actor(context)) else '积分系统仅对已绑定游戏账号的玩家开放，请先 /bind <游戏玩家名>'


def claim_sign(wxid, amount):
    """One daily reward across linked platforms, including concurrent requests."""
    from contextlib import closing
    from utils.account_identity import bound_user
    from utils import sqlite_store as store
    key = bound_user(wxid)
    if not key:
        return False, 0.0
    now = datetime.now().isoformat()
    with store._LOCK, closing(store._connect()) as conn, conn:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT points,last_sign_at FROM user_points WHERE wxid=?', (key,)).fetchone()
        if row and row[1] and str(row[1])[:10] == now[:10]:
            return False, float(row[0])
        conn.execute('INSERT INTO user_points(wxid,points,created_at,updated_at,last_sign_at) VALUES(?,?,?,?,?) '
                     'ON CONFLICT(wxid) DO UPDATE SET points=points+excluded.points,updated_at=excluded.updated_at,last_sign_at=excluded.last_sign_at',
                     (key, float(amount), now, now, now))
        return True, float(conn.execute('SELECT points FROM user_points WHERE wxid=?', (key,)).fetchone()[0])

from utils.sqlite_store import (
    user_points_get, user_points_add, user_points_set,
    user_points_set_sign, user_points_get_sign_date, user_points_try_spend,
    user_points_all_wxids,
)


def get_points(wxid):
    """获取用户积分"""
    wxid = str(wxid).strip()
    data = user_points_get(wxid)
    if not data:
        return 0.0
    return float(data.get("points", 0))


def add_points(wxid, amount):
    """增加用户积分"""
    wxid = str(wxid).strip()
    return user_points_add(wxid, amount)


def set_points(wxid, amount):
    """设置用户积分"""
    wxid = str(wxid).strip()
    return user_points_set(wxid, amount)


def get_all_wxids():
    """获取所有用户 wxid"""
    return user_points_all_wxids()


def get_last_sign_at(wxid):
    """获取上次签到时间"""
    return user_points_get_sign_date(wxid)


def set_last_sign_at(wxid):
    """设置签到时间为今天"""
    user_points_set_sign(wxid)


def has_signed_today(wxid):
    """检查是否已签到"""
    last_sign_date = get_last_sign_at(wxid)
    if last_sign_date is None:
        return False
    return last_sign_date == date.today()


def try_spend_points(wxid, amount):
    """Atomically spend points; returns (success, remaining_points)."""
    return user_points_try_spend(wxid, amount)
