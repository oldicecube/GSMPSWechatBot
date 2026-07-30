"""/emoji 命令插件：根据表情序列生成群聊风格的小故事。"""

from llm.config import get_llm_config

import json
import re
import unicodedata

COMMAND = "/emoji"

SYSTEM_PROMPT = """\
你是一个擅长根据表情创作故事的AI编剧。

你的任务：
用户会输入一串由表情名称和emoji组成的内容，你需要根据这些表情的情绪、含义和排列顺序，创作一个完整的小故事。

====================
输入规则：
====================

1. 用户输入只会由以下两种形式组成：
   - 任意非空的[表情名称]
   - emoji表情
    - 标点符号（可作为表情之间的分隔或故事节奏的一部分）
    - 空格、换行和其他空白字符

[表情名称]不需要存在于任何预设表情表中，只要是成对方括号包住的非空名称即可。你必须原样保留所有输入标记。

2. 转发前会自动忽略普通文字、数字、英文字母或其他无法识别的字符。标点符号和空白字符属于合法输入。

3. 不允许根据上下文猜测用户意图，只根据当前输入创作故事。

====================
故事生成规则：
====================

1. 故事必须以“真拿你没办法，坐好喽，那是……”作为开头，并且必须紧接着自然补充故事类型、发生地点或具体场景，不能停在“那是……”处。
    例如：
    “真拿你没办法，坐好喽，那是一个发生在深夜群聊里的离奇故事。”

2. 根据输入表情的顺序设计故事发展。每个表情至少对应一个剧情事件。

3. 表情不能直接出现在故事剧情中。表情只作为剧情节点标注，并且标注必须放在对应剧情段落最后。

4. 按输入顺序依次出现标记，不增加输入中不存在的表情，不删除任何输入表情，不在开头或结尾单独罗列表情。

5. 故事正文不能出现“这个表情代表什么”之类的解释，也不要解释表情含义。

====================
风格要求：
====================

故事风格：
- 像微信群友讲故事
- 节奏快
- 有反转
- 有幽默感
- 可以夸张，但逻辑必须连贯
- 避免儿童故事风和过度文学化
- 像真实群聊里的“整活故事”

====================
输出格式：
====================

只返回一个合法 JSON 对象，不要使用 Markdown 代码块：
{"messages":["完整故事"]}

故事放在 messages 数组的一个字符串中。不要在故事字符串中使用换行符。开头必须是完整自然的句子，例如“真拿你没办法，坐好喽，那是一个发生在深夜群聊里的离奇故事。”"""

_EMOJI_SINGLETONS = {
    "©",
    "®",
    "‼",
    "⁉",
    "™",
    "ℹ",
    "↔",
    "↕",
    "↖",
    "↗",
    "↘",
    "↙",
    "⏏",
    "⌚",
    "⌛",
    "〰",
    "〽",
    "㊗",
    "㊙",
}


def _is_emoji_base(char: str) -> bool:
    codepoint = ord(char)
    return (
        char in _EMOJI_SINGLETONS
        or 0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
    )


def _consume_emoji(text: str, start: int) -> int:
    """返回从 start 开始的一个 emoji 序列长度，无法识别时返回 0。"""
    if start >= len(text):
        return 0

    char = text[start]
    codepoint = ord(char)

    # 数字、#、* 加 VS16 和 keycap 组合。
    if char in "0123456789#*":
        if (
            start + 2 < len(text)
            and text[start + 1] == "\ufe0f"
            and text[start + 2] == "\u20e3"
        ):
            return 3
        return 0

    # 国旗由两个区域指示符组成。
    if 0x1F1E6 <= codepoint <= 0x1F1FF:
        if start + 1 < len(text):
            next_codepoint = ord(text[start + 1])
            if 0x1F1E6 <= next_codepoint <= 0x1F1FF:
                return 2
        return 0

    if not _is_emoji_base(char):
        return 0

    end = start + 1
    while end < len(text) and text[end] in ("\ufe0e", "\ufe0f"):
        end += 1
    if end < len(text) and 0x1F3FB <= ord(text[end]) <= 0x1F3FF:
        end += 1
    if end < len(text) and text[end] == "\u20e3":
        end += 1

    # 支持家庭、职业等由 ZWJ 连接的 emoji 序列。
    while end + 1 < len(text) and text[end] == "\u200d":
        next_length = _consume_emoji(text, end + 1)
        if not next_length:
            break
        end += 1 + next_length

    return end - start


def _filter_valid_input(text: str) -> tuple[str, bool]:
    """过滤非法字符，返回可转发内容及是否包含表情 token。"""
    position = 0
    filtered = []
    has_emoji_token = False
    while position < len(text):
        if text[position] == "[":
            match = re.match(r"\[[^\[\]\r\n]+\]", text[position:])
            if not match:
                position += 1
                continue
            token = match.group(0)
            filtered.append(token)
            position += len(token)
            has_emoji_token = True
            continue

        if text[position] == "]":
            position += 1
            continue

        if text[position].isspace():
            filtered.append(text[position])
            position += 1
            continue

        if unicodedata.category(text[position]).startswith("P"):
            filtered.append(text[position])
            position += 1
            continue

        emoji_length = _consume_emoji(text, position)
        if emoji_length:
            filtered.append(text[position:position + emoji_length])
            position += emoji_length
            has_emoji_token = True
            continue

        position += 1

    return "".join(filtered).strip(), has_emoji_token


def _parse_response(text: str) -> str:
    try:
        match = re.search(r"\{[\s\S]*\}", text or "")
        data = json.loads(match.group(0) if match else text)
    except (TypeError, json.JSONDecodeError):
        return "❌ 故事生成失败：LLM 返回内容不是合法 JSON"

    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        return "❌ 故事生成失败：JSON 缺少 messages 数组"

    story = next((str(item).strip() for item in messages if str(item).strip()), "")
    return story or "❌ 故事生成失败：返回内容为空"


def handle(content, context):
    user_input = str(content or "").strip()
    if not user_input:
        return "用法：/emoji <表情序列>"

    user_input, has_emoji_token = _filter_valid_input(user_input)
    if not has_emoji_token or not user_input:
        return "用法：/emoji <表情序列>"

    try:
        llm_config = get_llm_config()
    except Exception as error:
        return f"❌ 读取 LLM 配置失败：{error}"

    if not llm_config.get("enabled"):
        return "❌ LLM 功能未启用，无法生成故事"

    user_prompt = f"以下是用户输入的内容：\n{user_input}"
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    try:
        from llm.provider import DeepSeekProvider

        response_text = DeepSeekProvider().send(messages)
    except Exception as error:
        return f"❌ LLM 请求失败：{error}"

    story = _parse_response(response_text)
    return {
        "target": context.get("group") or context.get("user"),
        "messages": [story],
        "mode": "wechat_text",
    }
