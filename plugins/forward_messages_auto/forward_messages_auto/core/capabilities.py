"""MaiBot capability 返回值兼容解析。"""

from __future__ import annotations

from typing import Any


class CapabilityResult:
    """统一解释 MaiBot capability 的返回值。"""

    @staticmethod
    def succeeded(result: Any) -> bool:
        """判断 capability 调用结果是否表示成功。

        Args:
            result: MaiBot capability 返回值。兼容直接布尔值和包含
                ``success`` 或 ``sent`` 字段的字典。

        Returns:
            布尔值原样返回；字典的 ``sent`` 或 ``success`` 为真时返回
            ``True``；其他类型或缺失成功标记时返回 ``False``。
        """

        if isinstance(result, bool):
            return result
        if not isinstance(result, dict):
            return False
        if "sent" in result:
            return bool(result.get("sent"))
        return bool(result.get("success", False))

    @staticmethod
    def message_id(result: Any) -> str | None:
        """从详细发送结果中提取平台最终目标消息 ID。

        Args:
            result: ``send.forward`` capability 返回值。

        Returns:
            详细结果包含非空 ``message_id`` 时返回清洗后的字符串；旧 Host
            布尔结果、失败结果或缺少 ID 时返回 ``None``。
        """

        if not isinstance(result, dict) or not bool(result.get("sent")):
            return None
        message_id = str(result.get("message_id") or "").strip()
        return message_id or None

    @staticmethod
    def error(result: Any) -> str:
        """从 capability 失败结果中提取可读原因。

        Args:
            result: MaiBot capability 返回值。

        Returns:
            字典中优先使用 ``error``，其次使用 ``message``，均不存在时返回
            “未知错误”；非字典返回“返回格式错误”。
        """

        if isinstance(result, dict):
            return str(result.get("error") or result.get("message") or "未知错误")
        return "返回格式错误"
