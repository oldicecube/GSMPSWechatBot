from openai import OpenAI

from llm.config import get_api_key, get_llm_config


class DeepSeekProvider:
    def __init__(self):
        config = get_llm_config()
        self.model = config["model"]
        self.client = OpenAI(
            api_key=get_api_key(),
            base_url="https://api.deepseek.com"
        )

    def send(self, messages: list) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            response_format={"type": "json_object"}
        )
        return response.choices[0].message.content
