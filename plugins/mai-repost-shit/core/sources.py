"""Optional native source adapters for the unified repost planner."""
from __future__ import annotations

import random
from typing import Any

SOURCE_TOOLS = {
    'bilibili': ('repost_shi',),
    'qq_forward': ('cateye_forward_messages', 'forward_messages', 'pack_chat_messages', 'share_message_pack'),
    'tieba': ('tieba_share', 'tieba_repost', 'tieba_post_share', 'repost_tieba'),
}


def source_order(status, channels, platform, preferred='') -> list[str]:
    """Equal first-choice probability for Bilibili/Tieba; keep one draw across RPC."""
    enabled = set(channels)
    primary = [source for source in ('bilibili', 'tieba')
               if source in enabled and status.get(source, {}).get('available')]
    if preferred in primary:
        primary.remove(preferred)
        primary.insert(0, preferred)
    else:
        random.shuffle(primary)
    if platform == 'qq' and 'qq_forward' in enabled and status.get('qq_forward', {}).get('available'):
        primary.append('qq_forward')
    return primary


async def discover(ctx) -> dict[str, dict[str, Any]]:
    try:
        # Only Host owns the registered tools. Importing its singleton inside
        # Runner produces an empty, independent registry forever.
        payload = await ctx.call_capability('tool.get_definitions')
        tools = payload.get('tools', []) if isinstance(payload, dict) else payload
        registered = {str(item.get('name')) for item in (tools or []) if isinstance(item, dict)}
        result = {source: {'available': bool((tools := [name for name in names
                         if name in registered])), 'tools': tools}
                for source, names in SOURCE_TOOLS.items()}
        # Bilibili is this plugin's in-process pipeline and need not be a
        # separately registered native Tool component.
        result['bilibili'] = {'available': True, 'tools': ['repost_shi']}
        return result
    except Exception as exc:
        result = {source: {'available': False, 'tools': [], 'error': type(exc).__name__}
                  for source in SOURCE_TOOLS}
        result['bilibili'] = {'available': True, 'tools': ['repost_shi']}
        return result


async def call_api(ctx, name, **arguments):
    """Allow actual fetch/render/media operations to finish across Runner RPC."""
    value = await ctx.call_capability('api.call', timeout_ms=360000, api_name=name, args=arguments)
    while isinstance(value, dict) and isinstance(value.get('result'), dict):
        value = value['result']
    if isinstance(value, dict) and value.get('success') is False:
        error = str(value.get('error') or '').lower()
        if any(marker in error for marker in ('timeout', 'timed out', '超时')):
            value = {**value, 'unknown': True, 'sent': False}
    return value if isinstance(value, dict) else {'success': False, 'error': 'invalid API receipt'}


def planner_instruction(status: dict[str, dict[str, Any]]) -> str:
    available = [source for source, info in status.items() if info.get('available')]
    names = '、'.join(available) or '当前仅有 Bilibili'
    return (f'可用搬运来源：{names}。所有来源共用同一个 Planner、去重、群活跃度判断和分享限额；'
            '来源工具未加载时不要猜工具名、不要模拟成功。QQ 聊天记录优先原样合并转发，贴吧优先长图，'
            'B站优先视频；B站和贴吧在均可用时按1:1等权随机选择首选来源，确认未发送后才尝试另一来源，QQ收藏包仍作后备；仅向当前目标平台投递；媒体发送失败时记录失败，'
            '不退化成编造正文。')
