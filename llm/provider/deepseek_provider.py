import httpx
from openai import OpenAI

from llm.config import get_api_key, get_llm_config


class DeepSeekProvider:
    """Thin DeepSeek/OpenAI-compatible client.

    ``send`` keeps the old string-returning API used by existing plugins.
    ``send_chat`` exposes the full assistant message for tool-call rounds.
    """

    def __init__(self):
        config = get_llm_config()
        self.model = config["model"]
        self.client = None
        self._init_client()

    def _init_client(self):
        try:
            http_client = httpx.Client(timeout=httpx.Timeout(60.0))
            self.client = OpenAI(
                api_key=get_api_key(),
                base_url="https://api.deepseek.com",
                http_client=http_client,
            )
        except RuntimeError as exc:
            if "atexit" not in str(exc).lower() or "shutdown" not in str(exc).lower():
                raise
            self.client = OpenAI(
                api_key=get_api_key(),
                base_url="https://api.deepseek.com",
            )

    def send_chat(self, messages: list, tools: list | None = None):
        """Return the SDK assistant message, including any tool calls."""
        if self.client is None:
            raise RuntimeError("LLM 客户端未初始化")

        kwargs = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if tools:
            # DeepSeek documents auto as the default when tools are present;
            # setting it explicitly makes the model's autonomous choice clear.
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        try:
            response = self.client.chat.completions.create(**kwargs)
            return response.choices[0].message
        except Exception as exc:
            if "atexit" in str(exc).lower() or "shutdown" in str(exc).lower():
                self._init_client()
                response = self.client.chat.completions.create(**kwargs)
                return response.choices[0].message
            raise

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
