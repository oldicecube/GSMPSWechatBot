import re
from utils.points_manager import get_points, add_points, set_points, get_all_wxids

COMMAND = "/points"

# 全局config存储
_config = {}


def init(config):
    """初始化插件"""
    global _config
    _config = config or {}


def _is_admin(wxid):
    """检查用户是否是管理员"""
    admin_wxids = _config.get("llm", {}).get("admin_wxids", []) or []
    return wxid in admin_wxids


def handle(content, context):
    """处理 /points 命令"""
    wxid = context.get("wxid") or context.get("raw", {}).get("wxid")
    
    if not wxid:
        return "无法获取用户信息"
    
    wxid = str(wxid).strip()
    
    # 转换content为字符串
    if isinstance(content, list):
        content = content[0] if content else ""
    
    content_str = str(content).strip() if content else ""
    
    if not content_str:
        return "请使用：\n/points <wxid> - 查询积分\n/points +<数值> <wxid> - 增加积分\n/points -<数值> <wxid> - 减少积分\n/points --modify <数值> <wxid> - 直接修改积分\n/points --help - 查看积分获得途径"
    
    # ===================================================
    # 情况0: /points --help - 显示积分获得途径
    # ===================================================
    if content_str == "--help":
        return """💎 积分获得途径：

   每日签到 (+5-10积分)
   使用命令: /sign
   每日仅可签到一次

   今日运势 (+5-10积分)
   使用命令: /player --fortune
   每日仅可抽取一次

   每日首次进服 (+5积分)
   在服务器首次进入时自动获得
   每日仅一次

   在线奖励 (+1积分/分钟，每日上限10积分)
   在服务器在线时自动累积
   每日最多通过此方式获得10积分

   The Room小游戏活跃奖励
   每日参与任意小游戏每次获得1积分
   每日最多通过此方式获得10积分

   The Room Championship Events
   不定期开展的小游戏竞赛，通过累计积分赛制发放积分
   具体请关注特别活动公告

💰 积分消耗：
- /anima 请求生成图像 (-3积分)
- /echo 新增echo内容 (-1积分)

❓ 查询积分: /points <wxid>"""
    
    # 分割参数
    parts = content_str.split()
    
    # ===================================================
    # 情况2: /points --modify <数值> <wxid> - 直接修改积分（仅管理员）
    # ===================================================
    if len(parts) >= 3 and parts[0] == "--modify":
        if not _is_admin(wxid):
            return  # 无任何返回
        
        try:
            amount_str = parts[1]
            target_wxid = parts[2]
            
            amount = float(amount_str)
            target_wxid = str(target_wxid).strip()
            
            new_points = set_points(target_wxid, amount)
            return f"✅ 已将用户 {target_wxid} 的积分修改为 {amount}\n当前积分: {new_points:.1f}"
        except (ValueError, IndexError):
            return "错误：参数格式不正确。应使用：/points --modify <数值> <wxid>"
    
        # ===================================================
    # 情况3: /points +<数值> <wxid> 或 /points -<数值> <wxid> - 增加/减少积分（仅管理员）
    #        /points +<数值> 或 /points -<数值> - 为所有人调整积分（仅管理员）
    # ===================================================
    first_arg = parts[0]
    match = re.match(r'^([+\-])(.+)$', first_arg)

    if match:
        if not _is_admin(wxid):
            return  # 无任何返回

        try:
            operation = match.group(1)  # + 或 -
            amount_str = match.group(2)
            amount = float(amount_str)

            # 如果没有指定 wxid，则为所有人调整积分
            if len(parts) == 1:
                all_wxids = get_all_wxids()
                if not all_wxids:
                    return "暂无任何用户数据"

                action_word = "增加了" if operation == "+" else "减少了"
                for uid in all_wxids:
                    add_points(uid, amount if operation == "+" else -amount)
                return f"✅ 已为所有 {len(all_wxids)} 个用户{action_word} {amount} 积分"

            target_wxid = parts[1]
            target_wxid = str(target_wxid).strip()

            # + 表示增加，- 表示减少
            if operation == "+":
                new_points = add_points(target_wxid, amount)
                return f"✅ 为用户 {target_wxid} 增加了 {amount} 积分\n当前积分: {new_points:.1f}"
            else:  # -
                new_points = add_points(target_wxid, -amount)
                return f"✅ 为用户 {target_wxid} 减少了 {amount} 积分\n当前积分: {new_points:.1f}"
        except ValueError:
            return "错误：参数格式不正确。应使用：/points +<数值> <wxid> 或 /points -<数值> <wxid>"
    
    return "无法识别命令。请使用：\n/points <wxid> - 查询积分\n/points +<数值> <wxid> - 增加积分\n/points -<数值> <wxid> - 减少积分\n/points --modify <数值> <wxid> - 直接修改积分"
