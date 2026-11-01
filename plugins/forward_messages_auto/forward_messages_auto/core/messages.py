"""Host 合并转发消息解析。"""

from __future__ import annotations

from typing import Any


class ForwardMessageParser:
    """将 Host 消息转换为可发送的合并转发数据。"""

    @staticmethod
    def extract(message: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """从 Host 消息中提取首个有效的合并转发消息段。

        解析器会遍历 ``raw_message``，跳过非 ``forward`` 段、空节点和
        缺少有效消息内容的节点。媒体段中的二进制字段会原样复制，以便
        后续 ``ctx.send.forward`` 重建消息。

        Args:
            message: ``ctx.message.get_by_id`` 返回的 Host 消息字典。

        Returns:
            成功时返回二元组：第一个元素是适合写入 Maisaka 上下文的
            ``{"type": "forward", "data": ...}`` 消息段；第二个元素是
            适合传给 ``ctx.send.forward`` 的节点列表。没有有效合并转发
            段时返回 ``None``。
        """

        raw_message = message.get("raw_message")
        if not isinstance(raw_message, list):
            return None

        for segment in raw_message:
            if not ForwardMessageParser._is_forward_segment(segment):
                continue
            payload = ForwardMessageParser._normalize_nodes(segment["data"])
            if payload is not None:
                return payload
        return None

    @staticmethod
    def _is_forward_segment(segment: Any) -> bool:
        """判断原始消息段是否为包含节点的合并转发段。

        Args:
            segment: ``raw_message`` 中的任意元素。

        Returns:
            当元素为字典、类型为 ``forward`` 且 ``data`` 是非空列表时
            返回 ``True``，否则返回 ``False``。
        """

        return (
            isinstance(segment, dict)
            and str(segment.get("type") or "").strip().lower() == "forward"
            and isinstance(segment.get("data"), list)
            and bool(segment["data"])
        )

    @staticmethod
    def _normalize_nodes(
        raw_nodes: list[Any],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        """规范化合并转发节点，并构造上下文与发送两种表示。

        无效节点会被跳过。缺失昵称时使用“未知用户”，缺失消息 ID 时按
        节点下标生成稳定的占位 ID；消息内容中的未知字段和媒体二进制数据
        会通过浅拷贝保留。

        Args:
            raw_nodes: Host ``forward`` 消息段中的原始节点列表。

        Returns:
            至少存在一个有效节点时，返回上下文消息段与发送节点列表组成的
            二元组；所有节点均无效时返回 ``None``。
        """

        normalized_nodes: list[dict[str, Any]] = []
        forward_messages: list[dict[str, Any]] = []
        for index, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, dict):
                continue
            raw_content = raw_node.get("content")
            if not isinstance(raw_content, list):
                continue
            content = [dict(item) for item in raw_content if isinstance(item, dict)]
            if not content:
                continue

            node = {
                "user_id": str(raw_node.get("user_id") or ""),
                "user_nickname": str(raw_node.get("user_nickname") or "未知用户"),
                "user_cardname": str(raw_node.get("user_cardname") or ""),
                "message_id": str(raw_node.get("message_id") or f"forward_node_{index}"),
                "content": content,
            }
            normalized_nodes.append(node)
            forward_messages.append(
                {
                    "user_id": node["user_id"],
                    "nickname": node["user_nickname"],
                    "user_cardname": node["user_cardname"],
                    "message_id": node["message_id"],
                    "segments": content,
                }
            )

        if not normalized_nodes:
            return None
        return {"type": "forward", "data": normalized_nodes}, forward_messages
