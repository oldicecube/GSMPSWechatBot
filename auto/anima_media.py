"""Consume only Anima's per-session/per-user next image, using picture's media loader."""
from core.followup import peek, consume
from core.weflow_media import load_visual_bytes
from anima.commands import Request
from anima.media import prepare_reference

INTERCEPT_LLM = True
WEBUI_CONFIG = {'version': 1, 'fields': {}}


def init(config):
    pass


def handle_auto(context):
    session, owner = context.get('sessionId'), context.get('wxid')
    state = peek(session, wxid=owner)
    if not state or state.get('target') != 'anima_media':
        return None
    state = consume(session, wxid=owner)
    if not state or state.get('target') != 'anima_media':
        return None
    payload = state.get('payload') or {}
    raw = context.get('raw') or {}
    content = context.get('content', '')
    is_image = (context.get('is_picture') or context.get('is_emoji') or raw.get('is_picture')
                or raw.get('is_emoji') or content in {'[图片]', '[表情包]', '[动画表情]', '[表情]', '[图片文件]'})
    if not is_image:
        # Same WeFlow wire-type fallback used by auto.picture for type-47 stickers.
        from llm.core.wechat_translation import wire_fields
        fields = wire_fields(context)
        try:
            is_image = (int(fields.get('localType') or fields.get('local_type') or 0) & 0xffffffff) in {3,47}
        except (ValueError, TypeError):
            is_image = False
    if not is_image:
        return '已取消Anima等待图片：下一条消息不是图片或动画表情。本次未扣积分。'
    try:
        spec = Request.from_payload(payload['request'])
        binary = load_visual_bytes(context)
        if payload.get('stage') != 'image':
            return '原等待流程已停用，请重新发送支持的Anima命令。本次未扣积分。'
        reference = prepare_reference(binary)
        mask = None
        from plugins.anima import execute_request
        result = execute_request(spec.prompt, context, spec=spec, reference=reference, mask_png=mask)
        if reference.get('animated'):
            result += '\n动画表情已取第一帧，输出为静态图。'
        return result
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return '❌ 无法处理本次参考图：' + str(exc) + '\n本次未扣积分，请重新发送命令。'
    except Exception as exc:
        print(f'[ANIMA] reference handling failed: {type(exc).__name__}: {exc}')
        return '❌ 参考图处理失败，本次未扣积分，请重新发送命令。'
