"""
/anima 命令插件
用于调用 Anima 图像生成服务
"""

# GSMPS WebUI metadata only; execution remains in this shell module.
WEBUI_CONFIG = {'version': 1, 'fields': {
    'output_dir': {'type': 'str', 'default': r'H:\Anima\ComfyUI\output', 'config_path': 'comfy.output_dir', 'label': 'ComfyUI远端映射输出目录'},
    'comfy_url': {'type': 'str', 'default': 'http://127.0.0.1:8188', 'max_length': 500,
                  'label': 'ComfyUI 服务地址', 'config_path': 'comfy.url', 'reload': 'hot',
                  'description': '填写实际绘图电脑的 HTTP 地址；localhost 指 bot 本机。下次请求生效。'},
        'reference_wait_seconds': {'type': 'int', 'default': 120, 'min': 30, 'max': 300, 'label': '参考图等待秒数'},
    'model': {'type': 'str', 'default': 'anima-base-v1.0.safetensors', 'label': 'Base模型文件名'},
    'clip': {'type': 'str', 'default': 'qwen_3_06b_base.safetensors', 'label': '文本编码器文件名'},
    'vae': {'type': 'str', 'default': 'qwen_image_vae.safetensors', 'label': 'VAE文件名'},
    'upscale_model': {'type': 'str', 'default': 'RealESRGAN_x4plus_anime_6B.pth', 'label': '超分模型文件名'},
'generation_timeout': {'type': 'int', 'default': 180, 'min': 10, 'max': 600,
                           'label': '生成等待上限（秒）', 'config_path': 'anima.generation_timeout', 'reload': 'hot'},
'gi2_api_base': {'type': 'str', 'default': 'https://api.uni-token.com/v1', 'max_length': 300,
                    'label': 'GPT Image 网关地址', 'config_path': 'anima.gi2_api_base', 'reload': 'hot',
                    'description': 'OpenAI 兼容网关，形如 https://host/v1；/anima gi2 子命令使用。'},
'gi2_api_key': {'type': 'str', 'default': '', 'max_length': 300, 'secret': True,
                   'label': 'GPT Image API Key', 'config_path': 'anima.gi2_api_key', 'reload': 'hot',
                   'description': '敏感信息，仅保存在本机 config.json，不随仓库提交。'},
'gi2_model': {'type': 'str', 'default': 'gpt-image-2.6', 'max_length': 100,
                 'label': 'GPT Image 模型', 'config_path': 'anima.gi2_model', 'reload': 'hot'},
'gi2_size': {'type': 'str', 'default': '1024x1024', 'max_length': 20,
                'label': 'GPT Image 尺寸', 'config_path': 'anima.gi2_size', 'reload': 'hot'},
'gi2_timeout': {'type': 'int', 'default': 300, 'min': 30, 'max': 900,
                   'label': 'GPT Image 超时（秒）', 'config_path': 'anima.gi2_timeout', 'reload': 'hot'},
}}


import os
import sys
from core.sender import send
from anima.commands import GPT_IMAGE_MODES, HELP, IMAGE_MODES, Request, parse_request
from anima.gpt_image import GPTImageClient, GPTImageError
from utils.points_manager import get_points, try_spend_points

COMMAND = "/anima"
POINTS_COST = 8  # 本地 ComfyUI 工作流：成功送图或 LLM 明确拒绝收费，其他异常不收费
POINTS_COST_GI2 = 40  # gi2 走远端 GPT Image 付费网关，单独计价


def _points_cost(spec):
    """按模式返回本次应扣积分：gi2 系走远端付费网关，单价更高。"""
    return POINTS_COST_GI2 if (spec is not None and spec.mode in GPT_IMAGE_MODES) else POINTS_COST

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


def execute_request(content, context, *, spec=None, reference=None, mask_png=None):
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
    
    # 检查积分是否充足（gi2 走远端付费网关，单价不同）
    cost = _points_cost(spec)
    current_points = get_points(user_wxid)
    if current_points < cost:
        return f"❌ 积分不足！生成一张图像需要消耗 {cost} 积分，你当前有 {current_points:.1f} 积分。"
    
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
        configure_service(current)
        # gi2 走远端 GPT Image 网关，完全不碰 ComfyUI；其余模式仍走本地工作流。
        remote = spec is not None and spec.mode in GPT_IMAGE_MODES
        client = build_gpt_image_client(current) if remote else None
        ready, reason = client.check() if remote else _anima_service.check_connection()
        if not ready:
            return f'❌ {reason}\n本次未扣积分。'
        # 通知用户开始处理
        send(group_name, f"🎨 {nickname} 发起图像生成，处理中...")
        
        if remote:
            reference_png = reference.get('png') if isinstance(reference, dict) else None
            if spec.mode == 'gi2_img2img' and not reference_png:
                return '❌ 图生图需要参考图片。本次未扣积分。'
            try:
                image_path = client.generate(user_prompt, reference_png=reference_png)
            except GPTImageError as exc:
                return f'❌ 生成失败：{exc}\n本次未扣积分。'
            outcome = send(group_name, file_path=image_path, mode='wechat_file')
            delivered = isinstance(outcome, tuple) and outcome[0] is True
            if not delivered:
                return '⚠️ 图片已生成，但未确认发送成功。本次未扣积分，请检查适配器；不会自动重发。'
            charged, remaining_points = try_spend_points(user_wxid, cost)
            if not charged:
                return '✅ 图片已发送；生成期间余额发生变化，本次未扣积分。'
            return f'✅ 生成完成（GPT Image {client.model}）\n💎 消耗积分: -{cost} (剩余: {remaining_points:.1f})'

        # 执行生成流程
        result = _anima_service.generate(user_prompt, spec=spec, reference=reference, mask_png=mask_png)
        
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
            charged, remaining_points = try_spend_points(user_wxid, cost)
            if not charged:
                return '✅ 图片已发送；生成期间余额发生变化，本次未扣积分。'
            
            return f"✅ 生成完成，用时 {elapsed} 秒\n💎 消耗积分: -{cost} (剩余: {remaining_points:.1f})"
        else:
            error = result.get("error", "未知错误")
            
            # 检查是否被拒绝
            if result.get("rejected") is True:
                charged, remaining_points = try_spend_points(user_wxid, cost)
                if charged:
                    return f"⛔ 生成请求被拒绝：{error}\n💎 消耗积分: -{cost} (剩余: {remaining_points:.1f})"
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


def configure_service(current):
    _anima_service.configure_connection(current['comfy_url'], current['generation_timeout'])
    _anima_service.COMFY_OUTPUT_DIR = current.get('output_dir') or r'H:\Anima\ComfyUI\output'
    for key in ('model', 'clip', 'vae', 'upscale_model'):
        if current.get(key):
            _anima_service.anima_config[key] = current[key]


def build_gpt_image_client(current):
    """Build the remote GPT Image client from validated WebUI/config fields."""
    return GPTImageClient(
        current.get('gi2_api_base'),
        current.get('gi2_api_key'),
        model=current.get('gi2_model'),
        size=current.get('gi2_size'),
        timeout=current.get('gi2_timeout'),
        output_dir=_anima_service.output_cache,
    )


def handle(content, context):
    """Default text generation; image modes reserve the same per-user follow-up as picture."""
    if not _anima_service:
        return "❌ Anima 服务未初始化"
    from core.followup import register, clear
    from core.shell_plugin_config import settings
    from utils.points_manager import binding_required
    from utils.account_identity import canonical_user
    try:
        if str(content).strip() == '取消':
            clear(context.get('sessionId'), wxid=context.get('wxid'))
            return '已取消等待图片。'
        spec = parse_request(content)
        if spec is None:
            return HELP
        error = binding_required(context)
        if error:
            return error
        cost = _points_cost(spec)
        if get_points(canonical_user(context.get('wxid', 'unknown'))) < cost:
            return f'❌ 积分不足，需 {cost} 积分。'
        if spec.mode not in IMAGE_MODES:
            return execute_request(spec.prompt, context, spec=spec)
        if not context.get('sessionId') or not context.get('wxid'):
            return '❌ 无法识别会话或用户，未进入等待图片状态。'
        current = settings('gsmps.command.anima')
        configure_service(current)
        if spec.mode in GPT_IMAGE_MODES:
            ready, reason = build_gpt_image_client(current).check()
        else:
            ready, reason = _anima_service.check_connection()
        if not ready:
            return '❌ ' + reason + '\n本次未扣积分。'
        if spec.mode not in GPT_IMAGE_MODES:
            _anima_service.preflight(spec)
        ttl = int(current.get('reference_wait_seconds', 120))
        register(context['sessionId'], 'anima_media', ttl=ttl,
                 payload={'request': spec.payload(), 'stage': 'image'}, wxid=context['wxid'])
        extra = ''
        return f'请在 {ttl} 秒内紧接着发送参考图片或动画表情。动画仅取第一帧，输出静态图。{extra}\n等待阶段不扣积分；/anima 取消 可取消。'
    except (ValueError, TypeError) as exc:
        return '❌ ' + str(exc)
    except Exception as exc:
        print(f'[ANIMA] request setup failed: {type(exc).__name__}: {exc}')
        return '❌ 无法准备当前模式，请检查图像服务连接与节点配置。本次未扣积分。'
