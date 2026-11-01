"""Synchronous shell callers use the same native task pool as WebUI and plugins."""
from contextlib import nullcontext
from types import SimpleNamespace
import json
import threading


async def generate_native(messages, *, task_name='utils', tools=None, timeout=None):
    from src.services.llm_service import LLMServiceClient, _build_prompt_context_factory
    from src.common.data_models.llm_service_data_models import LLMGenerationOptions
    client = LLMServiceClient(task_name, request_type='gsmps.shell.' + task_name)
    result = await client.generate_response_with_context(_build_prompt_context_factory(messages),
        LLMGenerationOptions(tool_options=tools or None))
    calls = [SimpleNamespace(id=c.call_id, type='function', function=SimpleNamespace(
        name=c.func_name, arguments=json.dumps(c.args, ensure_ascii=False))) for c in (result.tool_calls or [])]
    return SimpleNamespace(content=result.response or '', tool_calls=calls or None,
                           reasoning_content=result.reasoning or '', native_result=result)


def shell_generate(messages, *, task_name='utils', tools=None, timeout=None):
    from .maibot_runtime import native_runtime
    native_runtime._ensure_thread()
    if threading.current_thread() is native_runtime._thread:
        raise RuntimeError('Use generate_native() on the native asyncio loop')
    return native_runtime.submit(generate_native(messages, task_name=task_name, tools=tools),
                                 timeout=timeout or 120)


class NativePoolProvider:
    """Compatibility API, without constructing a second provider/endpoint pool."""
    def __init__(self, **kwargs):
        self.last_usage = {}

    def send_chat(self, messages, tools=None, prompt_cache_key=None, thinking=None, timeout=None):
        result = shell_generate(messages, tools=tools, timeout=timeout)
        self.last_usage = {name: getattr(result.native_result, name, 0) for name in
                           ('prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens', 'prompt_cache_miss_tokens')}
        return result

    def send(self, messages, prompt_cache_key=None, thinking=None, timeout=None):
        return self.send_chat(messages, timeout=timeout).content

    def current_protocol(self):
        return 'maibot'

    def current_cache_enabled(self):
        return False

    def current_cache_scope(self):
        return 'none'

    def prompt_capture_kind(self, kind):
        return nullcontext()

    def close(self):
        # The native runtime owns its clients and lifecycle.
        pass
