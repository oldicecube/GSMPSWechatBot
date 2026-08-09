import httpx
from openai import OpenAI

from llm.config import get_api_key, get_llm_config
from llm.prompt_capture import capture_prompt


class DeepSeekProvider:
    """Thin DeepSeek/OpenAI-compatible client.

    ``send`` keeps the old string-returning API used by existing plugins.
    ``send_chat`` exposes the full assistant message for tool-call rounds.
    """

    def __init__(self):
        config = get_llm_config()
        self.model = config["model"]
        self.client = None
        self.last_usage = {}
        self._init_client()

    def _init_client(self):
        try:
            http_client = httpx.Client(timeout=httpx.Timeout(60.0))
            self.client = OpenAI(
                api_key=get_api_key(),
                base_url="https://api.deepseek.com",
                http_client=http_client,
                max_retries=0,
            )
        except RuntimeError as exc:
            if "atexit" not in str(exc).lower() or "shutdown" not in str(exc).lower():
                raise
            self.client = OpenAI(
                api_key=get_api_key(),
                base_url="https://api.deepseek.com",
                max_retries=0,
            )

    def send_chat(self, messages: list, tools: list | None = None):
        """Return the SDK assistant message, including any tool calls."""
        if self.client is None:
            raise RuntimeError("LLM 客户端未初始化")

        kwargs = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            # DeepSeek documents auto as the default when tools are present;
            # setting it explicitly makes the model's autonomous choice clear.
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        else:
            # The final post-tool request uses JSON output. Tool-call rounds
            # use the provider's native assistant/tool message protocol.
            kwargs["response_format"] = {"type": "json_object"}

        try:
            capture_prompt(kwargs["messages"])
            response = self.client.chat.completions.create(**kwargs)
            self._log_usage(response)
            return response.choices[0].message
        except httpx.TimeoutException as exc:
            print(
                f"[LLM TIMEOUT] error={type(exc).__name__} "
                f"messages={len(messages)} chars={self._message_chars(messages)} "
                f"estimated_tokens={self._estimate_tokens(messages)}",
                flush=True,
            )
            raise
        except Exception as exc:
            if "atexit" in str(exc).lower() or "shutdown" in str(exc).lower():
                self._init_client()
                response = self.client.chat.completions.create(**kwargs)
                self._log_usage(response)
                return response.choices[0].message
            error_text = str(exc).lower()
            if any(marker in error_text for marker in (
                "context length", "maximum context", "too many tokens", "request too large",
                "payload too large", "413", "prompt is too long",
            )):
                print(
                    f"[LLM CONTEXT LIMIT] error={type(exc).__name__} "
                    f"messages={len(messages)} chars={self._message_chars(messages)} "
                    f"estimated_tokens={self._estimate_tokens(messages)}",
                    flush=True,
                )
            raise

    @staticmethod
    def _usage_value(usage, name):
        if isinstance(usage, dict):
            return usage.get(name)
        return getattr(usage, name, None)

    @staticmethod
    def _message_chars(messages):
        return sum(len(str(item.get("content") or "")) for item in messages if isinstance(item, dict))

    @classmethod
    def _estimate_tokens(cls, messages):
        text = "".join(str(item.get("content") or "") for item in messages if isinstance(item, dict))
        cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
        non_cjk = len(text) - cjk
        return cjk + max(0, (non_cjk + 3) // 4)

    def _log_usage(self, response):
        usage = getattr(response, "usage", None)
        if usage is None:
            print("[LLM TOKENS] usage unavailable", flush=True)
            return
        fields = {
            "prompt": self._usage_value(usage, "prompt_tokens"),
            "completion": self._usage_value(usage, "completion_tokens"),
            "total": self._usage_value(usage, "total_tokens"),
            "cache_hit": self._usage_value(usage, "prompt_cache_hit_tokens"),
            "cache_miss": self._usage_value(usage, "prompt_cache_miss_tokens"),
        }
        self.last_usage = {key: value for key, value in fields.items() if value is not None}
        print(
            "[LLM TOKENS] " + " ".join(
                f"{key}={value}" for key, value in fields.items() if value is not None
            ),
            flush=True,
        )

    def send(self, messages: list) -> str:
        """Backward-compatible string API for existing non-tool plugins."""
        message = self.send_chat(messages)
        return str(getattr(message, "content", None) or "")

    def __del__(self):
        try:
            if self.client is not None and hasattr(self.client, "_client"):
                self.client._client.close()
        except Exception:
            pass
