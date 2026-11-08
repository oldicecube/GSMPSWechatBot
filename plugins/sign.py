
# GSMPS WebUI metadata only; execution remains in this shell module.
WEBUI_CONFIG = {'version': 1, 'fields': {}}

import random

from utils.points_manager import (
    has_signed_today,
)

COMMAND = "/sign"


def handle(content, context):
    """处理签到命令"""
    from utils.points_manager import binding_required, claim_sign
    error = binding_required(context)
    if error:
        return error
    wxid = context.get("wxid") or context.get("raw", {}).get("wxid")
    
    if not wxid:
        return "无法获取用户信息"
    
    wxid = str(wxid).strip()
    
    # 检查是否已签到
    if has_signed_today(wxid):
        return "你今日已签到过了，请明天再来"
    
    # 随机获取5-10积分
    points_gained = random.randint(5, 10)
    
    success, total_points = claim_sign(wxid, points_gained)
    if not success:
        return "你今日已签到过了，请明天再来"
    
    return (
        f"✅ 签到成功！\n"
        f"获得积分: +{points_gained}\n"
        f"当前积分: {total_points:.1f}"
    )
