"""基于当前群成员身份转换文本 @，并合并相邻重复提及。"""

from typing import Any, Dict, List, Mapping, Set

import re


def build_member_aliases(members: List[Mapping[str, Any]]) -> Dict[str, str]:
    """仅收录能唯一定位成员的 QQ 号、群名片和昵称，重名不猜测。"""
    candidates: Dict[str, Set[str]] = {}
    for member in members:
        user_id = str(member.get("user_id") or "").strip()
        if not user_id.isascii() or not user_id.isdecimal() or int(user_id) <= 0:
            continue
        for alias in (user_id, member.get("card"), member.get("nickname")):
            if isinstance(alias, str) and alias.strip():
                candidates.setdefault(alias.strip(), set()).add(user_id)
    return {name: next(iter(ids)) for name, ids in candidates.items() if len(ids) == 1}


def convert_text_mentions(
    segments: List[Dict[str, Any]], aliases: Dict[str, str]
) -> List[Dict[str, Any]]:
    """转换完整名称，保留未知文本；不匹配邮箱、路径中的 @ 或名称前缀。"""
    if not aliases:
        return segments
    pattern = re.compile(
        r"@(" + "|".join(re.escape(name) for name in sorted(aliases, key=len, reverse=True)) + r")(?!\w)"
    )
    result: List[Dict[str, Any]] = []
    for segment in segments:
        if segment.get("type") != "text":
            result.append(segment)
            continue
        value = segment["data"]["text"]
        cursor = 0
        for match in pattern.finditer(value):
            # 连写的已识别 @ 可以相邻；其他单词、邮箱、URL 和路径内的 @ 不转换。
            if match.start() > 0 and match.start() != cursor:
                previous = value[match.start() - 1]
                if previous.isalnum() or previous in "_@./\\":
                    continue
            if match.start() > cursor:
                result.append({"type": "text", "data": {"text": value[cursor:match.start()]}})
            result.append({"type": "at", "data": {"qq": aliases[match.group(1)]}})
            cursor = match.end()
        if cursor < len(value) or cursor == 0:
            result.append({"type": "text", "data": {"text": value[cursor:]}})
    return result


def merge_adjacent_mentions(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """相同目标的相邻 @ 合并；仅空格或制表符不打断相邻关系，正文和换行会打断。"""
    result: List[Dict[str, Any]] = []
    last_target = ""
    for segment in segments:
        segment_type = segment.get("type")
        if segment_type == "at":
            target = str(segment["data"]["qq"])
            if target and target == last_target:
                while result and result[-1].get("type") == "text":
                    result.pop()
                continue
            last_target = target
        elif segment_type != "text" or segment["data"]["text"].strip(" \t"):
            last_target = ""
        result.append(segment)
    return result
