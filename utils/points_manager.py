from datetime import datetime, date

from utils.sqlite_store import (
    user_points_get, user_points_add, user_points_set,
    user_points_set_sign, user_points_get_sign_date,
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
