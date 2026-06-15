from llm.config import get_llm_config
from llm.core.response_parser import (
    FALLBACK_RESPONSE,
    build_error_response,
    parse_llm_response,
)
from llm.memory import MemoryManager
from llm.prompt import build_system_prompt, build_user_prompt
from llm.provider import DeepSeekProvider
from llm.security import build_emoji_index, get_emoji_list


class LLMService:
    def __init__(self):
        try:
            self.config = get_llm_config()
        except Exception:
            self.config = {"enabled": False}

        try:
            self.memory_manager = MemoryManager()
        except Exception:
            self.memory_manager = None

        self.provider = None
        self.assistant_nickname = self.config.get("assistant_nickname", "LLM")
        self.emoji_list = []

        try:
            build_emoji_index(self.config.get("emoji_dir"))
            self.emoji_list = get_emoji_list()
        except Exception:
            self.emoji_list = []

    def handle_message(self, group_id, nickname, content, wxid=""):
        result_to_return = dict(FALLBACK_RESPONSE)

        try:
            if not self.config.get("enabled"):
                return build_error_response("LLM转发失败：LLM 功能未启用")

            if self.memory_manager is None:
                return build_error_response("LLM转发失败：记忆模块不可用")

            # 初始化或重新初始化 provider（如果失败过）
            if self.provider is None:
                try:
                    self.provider = DeepSeekProvider()
                except Exception as init_err:
                    return build_error_response(f"LLM转发失败：LLM 客户端初始化失败 - {init_err}")

            self.memory_manager.add_llm_message(group_id, nickname, content)

            chat_history = self.memory_manager.get_llm_history(group_id)
            group_messages = self.memory_manager.get_group_messages(group_id)
            emoji_list = list(self.emoji_list)

            system_prompt = build_system_prompt()
            user_prompt = build_user_prompt({
                "chat_history": chat_history,
                "group_messages": group_messages,
                "emoji_list": emoji_list,
                "identity": self.config.get("identity") or {},
                "llm_config": self.config or {},
                "sender_wxid": wxid
            })

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]

            response_text = self.provider.send(messages)
            parsed = parse_llm_response(response_text, emoji_list)
            result_to_return = parsed
        except Exception as e:
            result_to_return = build_error_response(f"LLM转发失败：{e}")

        self._store_assistant_messages(group_id, result_to_return)
        return result_to_return

    def _store_assistant_messages(self, group_id, result):
        if self.memory_manager is None:
            return

        try:
            messages = result.get("messages") or []
            for item in messages:
                content = str(item or "").strip()
                if not content:
                    continue

                self.memory_manager.add_llm_message(
                    group_id,
                    self.assistant_nickname,
                    content
                )
        except Exception:
            return
