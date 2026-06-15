import atexit
import httpx
from openai import OpenAI

from llm.config import get_api_key, get_llm_config


class DeepSeekProvider:
    def __init__(self):
        config = get_llm_config()
        self.model = config["model"]
        self.client = None
        self._init_client()

    def _init_client(self):
        """初始化 OpenAI 客户端，处理 atexit 相关错误"""
        try:
            # 创建自定义的 httpx 客户端配置，避免 atexit 注册问题
            http_client = httpx.Client(
                timeout=httpx.Timeout(60.0)
            )
            
            self.client = OpenAI(
                api_key=get_api_key(),
                base_url="https://api.deepseek.com",
                http_client=http_client
            )
        except RuntimeError as e:
            # 捕获 "can't register atexit after shutdown" 错误
            if "atexit" in str(e).lower() and "shutdown" in str(e).lower():
                # 降级处理：使用默认配置重试
                try:
                    # 禁用自动atexit处理
                    import sys
                    if not sys.is_finalizing():
                        self.client = OpenAI(
                            api_key=get_api_key(),
                            base_url="https://api.deepseek.com"
                        )
                    else:
                        raise RuntimeError("程序正在关闭，无法初始化 OpenAI 客户端")
                except Exception as retry_err:
                    raise RuntimeError(f"LLM 客户端初始化失败: {retry_err}")
            else:
                raise

    def send(self, messages: list) -> str:
        """发送消息到 DeepSeek API"""
        if self.client is None:
            raise RuntimeError("LLM 客户端未初始化")
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"}
            )
            return response.choices[0].message.content
        except Exception as e:
            # 如果发生连接错误，尝试重新初始化客户端
            if "atexit" in str(e).lower() or "shutdown" in str(e).lower():
                self._init_client()
                if self.client is None:
                    raise RuntimeError(f"LLM 客户端恢复失败: {e}")
                # 重试一次
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    response_format={"type": "json_object"}
                )
                return response.choices[0].message.content
            raise

    def __del__(self):
        """清理资源"""
        try:
            if self.client is not None and hasattr(self.client, '_client'):
                self.client._client.close()
        except Exception:
            pass
