from enum import Enum

import hashlib
import time


class TimestampMode(Enum):
    NORMAL = "%Y-%m-%d %H:%M:%S"
    """标准格式，例如 2024-01-01 12:00:00"""
    NORMAL_NO_YMD = "%H:%M:%S"
    """仅显示时间不显示年月日，例如 12:00:00"""
    RELATIVE = "relative"
    """相对时间，例如 5分钟前、2小时前等"""


def number_to_short_id(original_id: int, salt: str, length: int = 6) -> str:
    """
    将数字编号转换为短ID（不可逆）

    :param original_id: 原始数字
    :param length: 想要生成的短ID长度 (建议 4-8)
    :return: 短ID字符串
    """
    # 1. 加盐，避免简单的哈希冲突和猜测
    data = f"{original_id}{salt}".encode("utf-8")

    # 2. 计算 SHA-256 哈希
    hash_digest = hashlib.sha256(data).digest()

    # 3. 取前几个字节转换为整数
    # 为了达到需要的长度，我们可能需要取更多的字节
    num_bytes_needed = max(4, length)  # 保证足够的熵
    hash_int = int.from_bytes(hash_digest[:num_bytes_needed], byteorder="big")

    # 4. 使用 Base62 字符集编码
    characters = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    base = len(characters)

    short_id = ""
    temp_num = hash_int

    # 生成指定长度的ID
    for _ in range(length):
        short_id = characters[temp_num % base] + short_id
        temp_num //= base

    return short_id


def translate_timestamp_to_human_readable(timestamp: float, mode: TimestampMode | str) -> str:
    """将时间戳按照指定模式转换为人类可读的格式

    Args:
        timestamp (float): 需要转换的时间戳
        mode (TimestampMode): 时间戳转换模式，支持NORMAL、NORMAL_NO_YMD和RELATIVE三种模式
    Returns:
        str: 转换后的时间字符串
    """
    if isinstance(mode, str):
        if mode.upper() in TimestampMode.__members__:
            mode = TimestampMode[mode.upper()]
        else:
            raise ValueError(f"不支持的时间戳转换模式: {mode}")
    if mode in [TimestampMode.NORMAL, TimestampMode.NORMAL_NO_YMD]:
        return time.strftime(mode.value, time.localtime(timestamp))
    elif mode == TimestampMode.RELATIVE:
        time_diff = time.time() - timestamp

        if time_diff < 20:
            return "刚刚"
        elif time_diff < 60:
            return f"{int(time_diff)}秒前"
        elif time_diff < 3600:
            return f"{int(time_diff // 60)}分钟前"
        elif time_diff < 86400:
            return f"{int(time_diff // 3600)}小时前"
        elif time_diff < 2592000:
            return f"{int(time_diff // 86400)}天前"
        else:
            return time.strftime(TimestampMode.NORMAL.value, time.localtime(timestamp))
    else:
        raise ValueError(f"不支持的时间戳转换模式: {mode}")


def calculate_typing_time(
    input_string: str,
    chinese_time: float = 0.2,
    english_time: float = 0.1,
    line_break_time: float = 0.05,
    is_emoji: bool = False,
) -> float:
    """
    计算输入字符串所需的时间，中文和英文字符有不同的输入时间
        input_string (str): 输入的字符串
        chinese_time (float): 中文字符的输入时间，默认为0.3秒
        english_time (float): 英文字符的输入时间，默认为0.15秒
        line_break_time (float): 换行符的输入时间，默认为0.1秒
        is_emoji (bool): 是否为emoji，默认为False

    特殊情况：
    - 如果只有一个中文字符，将使用3倍的中文输入时间
    - 在所有输入结束后，额外加上回车时间
    - 如果is_emoji为True，将使用固定1秒的输入时间
    """
    if is_emoji:
        return 1.0  # 固定1秒的输入时间

    # 正常计算所有字符的输入时间
    total_time = 0.0
    chinese_chars = 0
    for char in input_string:
        if "\u4e00" <= char <= "\u9fff":
            total_time += chinese_time
            chinese_chars += 1
        else:
            total_time += english_time

    if chinese_chars == 1 and len(input_string.strip()) == 1:
        # 如果只有一个中文字符，使用3倍时间
        return chinese_time * 3 + line_break_time  # 加上回车时间

    return total_time + line_break_time  # 加上回车时间
