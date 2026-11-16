"""
/anima 命令插件
用于调用 Anima 图像生成服务
"""

# GSMPS WebUI metadata only; execution remains in this shell module.
WEBUI_CONFIG = {'version': 1, 'fields': {
    'comfy_url': {'type': 'str', 'default': 'http://127.0.0.1:8188', 'max_length': 500,
                  'label': 'ComfyUI 服务地址', 'config_path': 'comfy.url', 'reload': 'hot',
                  'description': '填写实际绘图电脑的 HTTP 地址；localhost 指 bot 本机。下次请求生效。'},
    'generation_timeout': {'type': 'int', 'default': 180, 'min': 10, 'max': 600,
                           'label': '生成等待上限（秒）', 'config_path': 'anima.generation_timeout', 'reload': 'hot'},
}}


import os
import sys
from core.sender import send
from utils.points_manager import get_points, try_spend_points

COMMAND = "/anima"
POINTS_COST = 8  # 成功送图或 LLM 明确拒绝收费，其他异常不收费

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# 全局服务实例
_anima_service = None
_config = {}


def init(config):
    """初始化插件"""
    global _anima_service, _config
    
    _config = config or {}
    
    try:
        from anima.anima_service import AnimaService
        _anima_service = AnimaService(_config)
        print("[PLUGIN] /anima 初始化成功")
    except Exception as e:
        print(f"[PLUGIN] /anima 初始化失败: {e}")


def handle(content, context):
    """
    处理 /anima 命令
    
    用法: /anima <提示词>
    示例: /anima 一个穿着蓝色连衣裙的女孩在樱花树下
    """
    global _anima_service
    
    if not _anima_service:
        return "❌ Anima 服务未初始化"
    
    # 提取提示词
    user_prompt = content.strip()
    if not user_prompt:
        return "❌ 请提供提示词\n用法: /anima <提示词>"
    
    # 获取用户信息
    from utils.points_manager import binding_required
    from utils.account_identity import canonical_user
    error = binding_required(context)
    if error:
        return error
    user_wxid = canonical_user(context.get("wxid", "unknown"))
    nickname = context.get("user", "未知用户")
    group_name = context.get("group", "")
    
    # 检查积分是否充足
    current_points = get_points(user_wxid)
    if current_points < POINTS_COST:
        return f"❌ 积分不足！生成一张图像需要消耗 {POINTS_COST} 积分，你当前有 {current_points:.1f} 积分。"
    
    # 检查是否可以开始生成
    can_generate, msg = _anima_service.can_start_generate(user_wxid)
    if not can_generate:
        return msg
    
    # 标记开始生成
    if not _anima_service.mark_generating(user_wxid):
        return "❌ 已有生成任务正在进行，请稍候"
    
    image_path = None
    delivered = False
    try:
        from core.shell_plugin_config import settings
        current = settings('gsmps.command.anima')
        _anima_service.configure_connection(current['comfy_url'], current['generation_timeout'])
        ready, reason = _anima_service.check_connection()
        if not ready:
            return f'❌ {reason}\n本次未扣积分。'
        # 通知用户开始处理
        send(group_name, f"🎨 {nickname} 发起图像生成，处理中...")
        
        # 执行生成流程
        result = _anima_service.generate(user_prompt)
        
        # 构建响应消息
        if result.get("success"):
            image_path = result.get("image_path")
            elapsed = result.get("elapsed_seconds", 0)
            
            outcome = send(group_name, file_path=image_path, mode="wechat_file")
            delivered = isinstance(outcome, tuple) and outcome[0] is True
            if not delivered:
                return '⚠️ 图片已生成，但未确认发送成功。本次未扣积分，请检查适配器；不会自动重发。'
            # Atomically settle once, after physical delivery; failures never
            # fall into an exception branch that performs a second debit.
            charged, remaining_points = try_spend_points(user_wxid, POINTS_COST)
            if not charged:
                return '✅ 图片已发送；生成期间余额发生变化，本次未扣积分。'
            
            return f"✅ 生成完成，用时 {elapsed} 秒\n💎 消耗积分: -{POINTS_COST} (剩余: {remaining_points:.1f})"
        else:
            error = result.get("error", "未知错误")
            
            # 检查是否被拒绝
            if result.get("rejected") is True:
                charged, remaining_points = try_spend_points(user_wxid, POINTS_COST)
                if charged:
                    return f"⛔ 生成请求被拒绝：{error}\n💎 消耗积分: -{POINTS_COST} (剩余: {remaining_points:.1f})"
                return f"⛔ 生成请求被拒绝：{error}\n余额发生变化，本次未扣积分。"
            error_msg = f"❌ 生成失败：{error}"
            
            error_msg += '\n本次未扣积分。'
            
            return error_msg
    
    except Exception as e:
        print(f'[ANIMA] request failed: {type(e).__name__}: {e}', flush=True)
        return ('⚠️ 图片已发送，但积分结算异常，请联系管理员核对，未重复扣费。' if delivered
                else '❌ 绘图请求未完成，本次未扣积分，请稍后重试。')
    
    finally:
        if image_path:
            try:
                _anima_service.delete_image(image_path)
            except Exception as exc:
                print(f'[ANIMA] cache cleanup failed: {type(exc).__name__}', flush=True)
        # 标记生成完成
        _anima_service.mark_done()
