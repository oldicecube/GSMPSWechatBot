"""
/jane（别名 /火钱）命令插件
校园幽默段子生成器；部署专用风格提示词仅保存在本地。
"""

from llm.config import get_llm_config
from llm.core.response_parser import (
    BALANCE_ERROR_MESSAGE,
    is_insufficient_balance_error,
    load_json_lenient,
)

COMMAND = "/火钱"
ALIASES = ["/jane"]

# 场景选项（不带参数时展示）
SCENE_OPTIONS = """\
━━━━━━━━━━━━━━━━━━

现在请你从以下场景中选一个：
A. 新生报到 B. 选课 C. 社团招新 D. 军训 E. 期末考试 F. 买电脑 G. 找实习 H. 宿舍空调"""

# Default public prompt uses a fictional persona.
SYSTEM_PROMPT = """你是一个虚构的校园生活幽默段子生成器，不模仿或声称代表真实人物。
根据用户场景生成简短、夸张但无害的校园吐槽，避免将虚构内容当作真实事实。
可以使用1至3个 Emoji 增加语气。只返回 JSON：{"messages":["句子1","句子2"]}。
messages 最多3条简短字符串，不输出解释、推理或 Markdown。"""


def _system_prompt():
    """Optional local style material is deliberately excluded from public Git."""
    from pathlib import Path
    local = Path(__file__).with_name("jane_prompt.txt")
    if local.is_file():
        return local.read_text(encoding="utf-8")
    return SYSTEM_PROMPT



def _build_user_prompt(user_input: str, with_scene_options: bool = False) -> str:
    """构建用户消息（含场景或用户指定内容）。"""
    if with_scene_options:
        return f"请按上面的规则生成一条段子。{SCENE_OPTIONS}"
    return f"请按上面的规则，围绕以下场景生成一条段子：{user_input}"


def _parse_llm_response(text: str) -> list[str]:
    """Parse only the public JSON payload; never echo model scratch text."""
    invalid_message = "\u274c \u6bb5\u5b50\u751f\u6210\u5668\u6682\u65f6\u65e0\u6cd5\u751f\u6210\u6bb5\u5b50\uff0c\u5df2\u5ffd\u7565\u65e0\u6548\u6a21\u578b\u8f93\u51fa"
    try:
        data = load_json_lenient(text, preferred_keys=("messages",))
    except Exception:
        return [invalid_message]

    if not isinstance(data, dict):
        return [invalid_message]

    messages = data.get("messages")
    if not isinstance(messages, list):
        return [invalid_message]

    result = [item.strip() for item in messages if isinstance(item, str) and item.strip()]
    return result or ["\u274c \u6bb5\u5b50\u751f\u6210\u5668\u8fd9\u6b21\u4e0d\u60f3\u8bf4\u8bdd"]


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
        return "❌ LLM 功能未启用，无法使用 校园段子生成器"

    try:
        from llm.core.maibot_service import MaibotLLMService
        provider = MaibotLLMService()
    except Exception as e:
        return f"❌ MaiBot LLM 客户端初始化失败：{e}"

    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": _build_user_prompt(user_input, with_scene_options)},
    ]

    try:
        response_text = str(provider._provider_call(messages, task_name="utils").content or "")
    except Exception as e:
        if is_insufficient_balance_error(e):
            return BALANCE_ERROR_MESSAGE
        return f"❌ LLM 请求失败：{e}"

    parsed = _parse_llm_response(response_text)

    # 一次生成应作为一条完整微信消息发送，避免把一个段子拆成多条刷屏。
    target = context.get("group") or context.get("user")
    return {
        "target": target,
        "messages": parsed,
        "mode": "wechat_text",
    }
