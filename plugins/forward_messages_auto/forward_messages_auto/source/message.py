"""source 入站合并转发消息的结构与来源解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..core.messages import ForwardMessageParser


@dataclass(frozen=True, slots=True)
class SourceForwardObservation:
    """一条可触发 source Planner 的入站合并转发消息。"""

    message_id: str
    stream_id: str
    group_id: str
    platform: str


class SourceForwardMessageParser:
    """从入站 Hook 载荷识别外部用户发送的 QQ 合并转发。"""

    @staticmethod
    def parse(message: Any) -> SourceForwardObservation | None:
        """校验入站消息环境、来源身份和合并转发结构。

        解析器只接受 QQ 群聊中的真实合并转发段。若适配器在
        ``additional_config`` 提供 ``self_id`` 或平台账号标记，发送者与
        机器人账号相同时直接忽略，避免插件发送回声再次触发 source Planner。

        Args:
            message: ``chat.receive.after_process`` Hook 提供的序列化
                ``SessionMessage``；非字典或字段不完整时视为不可处理。

        Returns:
            校验成功时返回 source 消息标识；私聊、非 QQ、非合并转发、
            字段不完整或机器人自身消息返回 ``None``。
        """

        if not isinstance(message, dict):
            return None
        platform = str(message.get("platform") or "").strip().lower()
        message_id = str(message.get("message_id") or "").strip()
        stream_id = str(message.get("session_id") or "").strip()
        message_info = message.get("message_info")
        if platform != "qq" or not message_id or not stream_id or not isinstance(message_info, dict):
            return None
        group_info = message_info.get("group_info")
        if not isinstance(group_info, dict):
            return None
        group_id = str(group_info.get("group_id") or "").strip()
        if not group_id or SourceForwardMessageParser._is_self_message(message_info):
            return None
        if ForwardMessageParser.extract(message) is None:
            return None
        return SourceForwardObservation(
            message_id=message_id,
            stream_id=stream_id,
            group_id=group_id,
            platform=platform,
        )

    @staticmethod
    def _is_self_message(message_info: dict[str, Any]) -> bool:
        """根据适配器路由标记判断发送者是否为机器人自身。

        Args:
            message_info: Hook 消息中的 ``message_info`` 字典，包含发送者和
                ``additional_config`` 路由元数据。

        Returns:
            发送者 ID 与 ``self_id``、``platform_io_account_id`` 或
            ``bot_account`` 任一非空账号标记一致时返回 ``True``。
        """

        user_info = message_info.get("user_info")
        additional_config = message_info.get("additional_config")
        if not isinstance(user_info, dict) or not isinstance(additional_config, dict):
            return False
        sender_id = str(user_info.get("user_id") or "").strip()
        if not sender_id:
            return False
        bot_ids = {
            str(additional_config.get(key) or "").strip()
            for key in ("self_id", "platform_io_account_id", "bot_account")
        }
        bot_ids.discard("")
        return sender_id in bot_ids
