
# GSMPS WebUI metadata only; execution remains in this shell module.
WEBUI_CONFIG = {'version': 1,
 'fields': {'repeat_count': {'type': 'int',
                             'default': 2,
                             'min': 2,
                             'max': 10,
                             'label': '连续相同消息触发条数'},
            'special_reply_enabled': {'type': 'bool', 'default': True, 'label': '启用拍你吗特殊复读'}}}

import threading

INTERCEPT_LLM = False
AUTO_PRIORITY = 200

STATE_LOCK = threading.Lock()
SESSION_STATES = {}
SPECIAL_MESSAGE = "拍你吗"


def init(config):
    print("[AUTO PLUGIN] repeat_echo initialized")


def handle_auto(context):
    from core.shell_plugin_config import settings
    # patpat 已经消费了同一条拍一拍事件。即使本插件在后续遍历中仍被
    # 调用，也必须让 patpat 的结果保持最终优先级。
    if isinstance(context, dict) and context.get('_patpat_matched'):
        return None
    cfg = settings('gsmps.automation.repeat_echo')
    content = _get_content(context)
    if not content:
        return None

    if _is_ineligible(context, content):
        return None

    if cfg['special_reply_enabled'] and content == SPECIAL_MESSAGE:
        return SPECIAL_MESSAGE

    session_key = _get_session_key(context)
    if not session_key:
        return None

    with STATE_LOCK:
        state = SESSION_STATES.setdefault(
            session_key,
            {
                "last_message": None,
                "cooldown": False,
            }
        )

        last_message = state.get("last_message")
        cooldown = bool(state.get("cooldown"))

        if cooldown:
            if content != last_message:
                state["last_message"] = content
                state["cooldown"] = False
                state['repeat_count'] = 1
            return None

        if content == last_message:
            state['repeat_count'] = state.get('repeat_count', 1) + 1
            if state['repeat_count'] < cfg['repeat_count']:
                return None
            state["cooldown"] = True
            return content

        state["last_message"] = content
        state['repeat_count'] = 1
        return None


def allow_llm(context):
    return True


def _get_content(context):
    if not isinstance(context, dict):
        return ""

    return str(context.get("content") or "").strip()


def _is_ineligible(context, content):
    if context.get("prefix_used"):
        return True

    if content.startswith("/"):
        return True

    # 非纯文本消息（如 [表情]、[图片] 等），不作处理
    if content.startswith("[") and content.endswith("]"):
        return True

    return False


def _get_session_key(context):
    return (
        context.get("group")
        or context.get("sessionId")
        or context.get("user")
        or context.get("wxid")
    )
