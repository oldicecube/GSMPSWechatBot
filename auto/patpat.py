
# GSMPS WebUI metadata only; execution remains in this shell module.
WEBUI_CONFIG = {'version': 1,
 'fields': {'reply_text': {'type': 'str',
                           'default': '拍你吗',
                           'max_length': 200,
                           'label': '被拍一拍后的回复'},
            'q_punishment_enabled': {'type': 'bool', 'default': True, 'label': 'QQ 拍一拍递增禁言'},
            'q_punishment_probability_at_third': {'type': 'float', 'default': 0.6, 'min': 0.0, 'max': 1.0,
                                                   'label': '第三次触发概率'},
            'q_punishment_guarantee_count': {'type': 'int', 'default': 10, 'min': 3, 'max': 100,
                                              'label': '强制禁言累计次数'},
            'q_punishment_max_seconds': {'type': 'int', 'default': 3600, 'min': 60, 'max': 3600,
                                          'label': '最长禁言时长（秒）'}}}

import re
import asyncio
import random
import threading

from utils.wechat_identity import canonical_wechat_user_id
from utils.wechat_pat import pat_event

CONFIG = None
MATCH_RAW_MESSAGE = True
INTERCEPT_LLM = True
AUTO_PRIORITY = 10
_STATE_LOCK = threading.RLock()
_STATE_PATH = 'data/patpat_state.json'


def init(config):
    global CONFIG
    CONFIG = config
    print("[AUTO PLUGIN] patpat 初始化完成")


def _settings(context):
    from core.shell_plugin_config import settings
    defaults = dict(reply_text='拍你吗', q_punishment_enabled=True,
                    q_punishment_probability_at_third=0.6,
                    q_punishment_guarantee_count=10, q_punishment_max_seconds=3600)
    try:
        loaded = settings('gsmps.automation.patpat')
        if isinstance(loaded, dict):
            for key, value in loaded.items():
                if key in defaults and isinstance(value, type(defaults[key])):
                    defaults[key] = value
    except Exception:
        pass
    return defaults


def _qq_key(context):
    return (str(context.get('group_id') or context.get('sessionId') or context.get('group') or '').strip(),
            str(context.get('user_id') or context.get('wxid') or '').strip())


def _load_counts():
    from utils.sqlite_store import load_document
    value = load_document(_STATE_PATH, default={})
    return value if isinstance(value, dict) else {}


def _count_pat(context):
    key = _qq_key(context)
    if not all(key):
        return 0
    with _STATE_LOCK:
        state = _load_counts()
        name = '|'.join(key)
        count = int(state.get(name, 0) or 0) + 1
        state[name] = count
        from utils.sqlite_store import save_document
        save_document(_STATE_PATH, state)
        return count


def _reset_pat(context):
    key = _qq_key(context)
    if not all(key):
        return
    with _STATE_LOCK:
        state = _load_counts()
        state.pop('|'.join(key), None)
        from utils.sqlite_store import save_document
        save_document(_STATE_PATH, state)


def _mute_plan(count, settings):
    """递增惩罚曲线：前两次免疫，第十次（默认）必定 30–60 分钟。"""
    guarantee = max(3, int(settings.get('q_punishment_guarantee_count', 10)))
    max_seconds = min(3600, max(60, int(settings.get('q_punishment_max_seconds', 3600))))
    if count <= 2:
        return False, 0
    if count >= guarantee:
        return True, random.randint(max(1800, min(max_seconds, 1800)), max_seconds)
    if count == 3:
        return random.random() < float(settings.get('q_punishment_probability_at_third', 0.25)), 60
    progress = (count - 3) / max(1, guarantee - 3)
    probability = min(0.98, max(0.75, float(settings.get('q_punishment_probability_at_third', 0.6))) + progress * 0.20)
    if random.random() >= probability:
        return False, 0
    lower = max(60, int(60 + progress * (max_seconds - 60) * 0.35))
    upper = max(lower, int(60 + progress * (max_seconds - 60) * 0.85))
    # Bias the draw toward the shorter part of the current range; only the
    # guarantee count enters the 30–60 minute bracket.
    draw = random.random() ** 2
    return True, int(lower + (upper - lower) * draw)


def _mute_qq(context, duration):
    """在自动化线程中调用原生 QQ mute Tool；不构造第二个 QQ 连接。"""
    loop = context.get('_native_loop')
    if loop is None or not loop.is_running():
        return {'success': False, 'error': 'QQ native event loop unavailable'}
    from src.plugin_runtime.component_query import component_query_service
    executor = component_query_service.get_tool_executor('mute')
    if executor is None:
        return {'success': False, 'error': 'QQ mute tool unavailable'}
    raw = context.get('raw') if isinstance(context.get('raw'), dict) else {}
    is_notice = bool(context.get('is_notify') or raw.get('is_notify'))
    args = dict(msg_id='' if is_notice else str(context.get('serverId') or context.get('messageKey') or raw.get('serverId') or ''),
                duration=int(duration), group_id=str(context.get('group_id') or ''),
                stream_id=str(context.get('native_session_id') or context.get('sessionId') or ''),
                platform='qq', user_id=str(context.get('user_id') or ''))
    if not args['group_id'] or not args['user_id']:
        return {'success': False, 'error': 'QQ poke lacks actor/group identity'}
    future = asyncio.run_coroutine_threadsafe(executor(args), loop)
    try:
        result = future.result(timeout=30)
        return result if isinstance(result, dict) else {'success': bool(result), 'content': str(result)}
    except Exception as exc:
        future.cancel()
        return {'success': False, 'error': type(exc).__name__}


# =========================================================
# 🧠 统一 content 提取（适配 dispatcher context）
# =========================================================
def _get_content(context):

    if not isinstance(context, dict):
        return ""

    # 直接标准字段（新 dispatcher 保证有）
    content = context.get("content")

    if content:
        return str(content).strip()

    # fallback：兼容旧 raw
    raw = context.get("raw")
    if isinstance(raw, dict):
        return str(
            raw.get("content")
            or raw.get("text")
            or ""
        ).strip()

    return ""


def _match_prefix(text, pfx):
    pfx = str(pfx or '').strip().lstrip('@')
    if not pfx:
        return False
    # A closing quote permits an adjacent custom suffix. An unquoted name must
    # end at a boundary, so a longer member name cannot match a bot-name prefix.
    _, action, tail = str(text or '').partition('拍了拍')
    if not action:
        return False
    target = re.escape(pfx)
    quoted = r'^\s*["“‘]@?' + target + r'["”’]'
    plain = r'^\s*@?' + target + r'(?:\s|$|[、。，！？!?])'
    return bool(re.search(quoted, tail, re.I) or re.search(plain, tail, re.I))


def _is_bot_pat(context):
    if not isinstance(context, dict):
        return False
    if context.get('platform') == 'qq':
        event = context.get('qq_notice') or (context.get('raw') or {}).get('qq_notice') or {}
        own = str(context.get('account_id') or '')
        return bool(context.get('is_notify') and own and event.get('notice_type') == 'notify'
                    and event.get('sub_type') == 'poke' and str(event.get('self_id') or '') == own
                    and str(event.get('target_id') or '') == own and str(event.get('user_id') or '') != own)
    event = pat_event(context)
    if event:
        own = canonical_wechat_user_id(((CONFIG or {}).get('weflow') or {}).get('myWxid', ''))
        if event['actor'] and own and canonical_wechat_user_id(event['actor']) == own:
            return False
        if event['target']:
            return bool(own and event['target'] == own)
        content = event['title']
    else:
        content = _get_content(context)
    prefixes = (CONFIG or {}).get('prefix', [])
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    return any(_match_prefix(content, prefix) for prefix in prefixes)


# =========================================================
# 🔥 dispatcher 统一入口（必须叫 handle_auto）
# =========================================================
def handle_auto(context):
    if not isinstance(context, dict):
        return None

    user = context.get("user", "未知用户")
    group = context.get("group")

    if _is_bot_pat(context) and (group or user):
        # Dispatcher 会继续遍历其他 auto 插件；留下明确的消费标记，
        # 让低优先级 repeat_echo 不会把 patpat 的结果覆盖成“拍你吗”的复读。
        context['_patpat_matched'] = True
        settings = _settings(context)
        if context.get('platform') == 'qq' and settings.get('q_punishment_enabled', True):
            count = _count_pat(context)
            should_mute, duration = _mute_plan(count, settings)
            if should_mute:
                _mute_qq(context, duration)
                # A triggered punishment consumes this poke regardless of
                # whether QQ accepted the mute request. Never expose the
                # duration/failure and never reply with the normal poke text.
                _reset_pat(context)
                return {'messages': [], 'mode': 'wechat_text', 'suppress_reply': True}
        return settings['reply_text']

    return None


def allow_llm(context):
    return not _is_bot_pat(context)
