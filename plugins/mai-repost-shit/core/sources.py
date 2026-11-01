"""Optional native source adapters for the unified repost planner."""
from __future__ import annotations

from typing import Any

SOURCE_TOOLS = {
    'bilibili': ('repost_shi',),
    'qq_forward': ('cateye_forward_messages', 'forward_messages', 'pack_chat_messages', 'share_message_pack'),
    'tieba': ('tieba_share', 'tieba_repost', 'tieba_post_share', 'repost_tieba'),
}


def discover() -> dict[str, dict[str, Any]]:
    try:
        from src.plugin_runtime.component_query import component_query_service
        result = {source: {'available': bool((tools := [name for name in names
                         if component_query_service.get_tool_info(name) is not None])), 'tools': tools}
                for source, names in SOURCE_TOOLS.items()}
        # Bilibili is this plugin's in-process pipeline and need not be a
        # separately registered native Tool component.
        result['bilibili'] = {'available': True, 'tools': ['repost_shi']}
        return result
    except Exception as exc:
        return {source: {'available': False, 'tools': [], 'error': type(exc).__name__}
                for source in SOURCE_TOOLS}


def planner_instruction(status: dict[str, dict[str, Any]]) -> str:
    available = [source for source, info in status.items() if info.get('available')]
    names = '、'.join(available) or '当前仅有 Bilibili'
    return (f'可用搬运来源：{names}。所有来源共用同一个 Planner、去重、群活跃度判断和分享限额；'
            '来源工具未加载时不要猜工具名、不要模拟成功。QQ 聊天记录优先原样合并转发，贴吧优先长图，'
            'B站优先视频；本轮会在可用来源间轮换，并把同一决定投递到当前目标平台；媒体发送失败时记录失败，'
            '不退化成编造正文。')
