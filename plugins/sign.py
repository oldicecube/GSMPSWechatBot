import random
from datetime import date

from utils.points_manager import (
    has_signed_today,
    set_last_sign_at,
    add_points,
    get_points
)

COMMAND = "/sign"


def handle(content, context):
    """处理签到命令"""
    wxid = context.get("wxid") or context.get("raw", {}).get("wxid")
    
    if not wxid:
        return "无法获取用户信息"
    
    wxid = str(wxid).strip()
    
    # 检查是否已签到
    if has_signed_today(wxid):
        return "你今日已签到过了，请明天再来"
    
    # 随机获取5-10积分
    points_gained = random.randint(5, 10)
    
    # 记录签到时间
    set_last_sign_at(wxid)
    
    # 增加积分
    total_points = add_points(wxid, points_gained)
    
    return (
        f"✅ 签到成功！\n"
        f"获得积分: +{points_gained}\n"
        f"当前积分: {total_points:.1f}\n"
        f"-\n"
        f"💡 小贴士：你也可以通过 /player --fortune 抽取今日运势获得5-10积分哦"
    )
