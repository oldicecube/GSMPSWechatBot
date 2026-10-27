from typing import TYPE_CHECKING, List

from src.common.utils.math_utils import translate_timestamp_to_human_readable, TimestampMode

if TYPE_CHECKING:
    from src.common.data_models.tool_record_data_model import MaiToolRecord


class ActionUtils:
    @staticmethod
    def build_readable_action_records(action_records: List["MaiToolRecord"], timestamp_mode: str | TimestampMode):
        """
        将动作列表转换为可读的文本格式。

        格式: `在`time`，你使用了`tool_name`

        Args:
            action_records: 动作记录字典列表。
            timestamp_mode: 时间戳模式。

        Returns:
            格式化的动作字符串。
        """
        if not action_records:
            return ""

        output_lines = []
        for record in action_records:
            timestamp_str = translate_timestamp_to_human_readable(record.timestamp.timestamp(), mode=timestamp_mode)
            line = f"在{timestamp_str}，你使用了{record.tool_name}"
            output_lines.append(line)
        return "\n".join(output_lines)
