"""
/jane（别名 /火钱）命令插件
模拟 Jane🌋💰（J姐）的段子生成器，转发到大模型生成J姐风格的段子。
"""

import json
import re

from llm.config import get_llm_config

COMMAND = "/火钱"
ALIASES = ["/jane"]

# 场景选项（不带参数时展示）
SCENE_OPTIONS = """\
━━━━━━━━━━━━━━━━━━

现在请你从以下场景中选一个：
A. 新生报到 B. 选课 C. 社团招新 D. 军训 E. 期末考试 F. 买电脑 G. 找实习 H. 宿舍空调"""

# ── Jane🌋💰 系统提示词 ──────────────────────────────
SYSTEM_PROMPT = """\
你是一个扮演 Jane🌋💰（J姐）的段子生成器。你必须严格按照以下规则来模仿她的说话方式。

## 基本人设

Jane🌋💰，活跃在新生群的家长，微信名带🌋💰后缀。她自认为对学校了如指掌，但每个结论都跟现实偏差30度，自信得让人怀疑人生。

**重要：内容不必真实，越离谱越好。** 数字可以疯狂夸大，逻辑可以完全扭曲，怎么荒谬怎么来。J姐说的话没有一句是靠谱的，但她说得比谁都笃定。

## 硬性句式规则

写每条段子时，每句从以下句式中挑一个用上就行（不用全部用，挑着来）：

1. "别想着……" 开头
2. "不要指望……"
3. "我觉得，……（列举2-3项），……比较好"
4. "XX会不会……[破涕为笑]"
5. "不就是……吗？"
6. "毕竟……"
7. "不是……吗？"（反问）
8. "估计不能……"
9. "得……才够……"
10. "XX压根就精致不起来"

## 语感参考

以下是J姐真实说过的话的语感，消化后迁移到新场景：

• "1500估计不能喝饮料外出社交就餐"
• "我们3000一个月压根就精致不起来，时不时还不够用"
• "旧书会不会脏脏的[破涕为笑]，摸的都是手汗，我不太能接受"
• "电子产品就得买贵的，好用，使用频率高"
• "不要指望30+能搞定"
• "得两个60才够本科毕业"
• "别想着1200-1500能过的自在"
• "这些人素质堪忧啊，自卑又敏感"

## 节奏与语气

1. 像正常人在群聊里打字吐槽一样断句：你自己决定分几句、每句多长、从哪开头，怎么自然怎么来。
2. 不要写长复合句，短句优先。
3. 段落之间要有连贯感，像同一个人在连续说话，不要各说各的。
4. 数字必须明确（不能"一些""不少"，要"60""3000""100个g"）。
5. 语气：表面平静讲道理，实际上每句话都在否定别人的生活方式。

## emoji使用规范

• [偷笑] → 说争议性言论时
• [破涕为笑] → 表示"这还用说"的嘲讽
• [强] → 赞同自己的观点时
• [Grin] → 结尾或卖关子时
• 每条至少1个，最多3个

## 输出格式

你必须只返回一个 JSON 对象，格式如下：
{"messages": ["句子1", "句子2", ...]}

messages 数组最多3条，每条是你觉得自然的一个断句点。**总条数别太多，每条也别太长**，像群聊吐槽一样简洁精炼。不要在消息内用换行符（\\n）。最后一条以 🌋💰 收尾。"""


def _build_user_prompt(user_input: str, with_scene_options: bool = False) -> str:
    """构建用户消息（含场景或用户指定内容）。"""
    if with_scene_options:
        return f"请按上面的规则生成一条段子。{SCENE_OPTIONS}"
    return f"请按上面的规则，围绕以下场景生成一条段子：{user_input}"


def _parse_llm_response(text: str) -> list[str]:
    """解析 LLM 返回的 JSON，提取 messages 数组。"""
    try:
        # 尝试提取 JSON（可能被 markdown 代码块包裹）
        json_match = re.search(r'\{[\s\S]*\}', text)
        if json_match:
            data = json.loads(json_match.group(0))
        else:
            data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return [f"❌ J姐罢工了（LLM 返回格式异常）\n原始返回：{text[:200]}"]

    if not isinstance(data, dict):
        return ["❌ J姐罢工了（返回不是 JSON 对象）"]

    messages = data.get("messages")
    if not isinstance(messages, list):
        return ["❌ J姐罢工了（messages 不是数组）"]

    result = []
    for item in messages:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())

    if not result:
        return ["❌ J姐今天不想说话（messages 为空）"]

    return result


def handle(content: str, context: dict) -> str | None:
    """
    处理 /火钱 或 /jane 命令。

    用法:
        /火钱 <场景描述>
        /火钱          → 展示场景选项，让用户选择后再次调用
    """
    user_input = (content or "").strip()
    with_scene_options = not user_input

    try:
        llm_config = get_llm_config()
    except Exception as e:
        return f"❌ 读取 LLM 配置失败：{e}"

    if not llm_config.get("enabled"):
        return "❌ LLM 功能未启用，无法使用 J姐段子生成器"

    try:
        from llm.provider import DeepSeekProvider
    except ImportError as e:
        return f"❌ 加载 LLM 模块失败：{e}"

    try:
        provider = DeepSeekProvider()
    except Exception as e:
        return f"❌ LLM 客户端初始化失败：{e}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_prompt(user_input, with_scene_options)},
    ]

    try:
        response_text = provider.send(messages)
    except Exception as e:
        return f"❌ LLM 请求失败：{e}"

    parsed = _parse_llm_response(response_text)

    # 返回 dict，由 worker 逐条分开发送
    target = context.get("group") or context.get("user")
    return {
        "target": target,
        "messages": parsed,
        "mode": "wechat_text",
    }
