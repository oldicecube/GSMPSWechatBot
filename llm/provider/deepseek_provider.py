# -*- coding: utf-8 -*-
"""多协议 LLM API 池 Provider。

DeepSeekProvider 是历史类名，实际从 ``llm.apis`` 读取按 ``priority``
排序的 API 池，并支持 ``openai``（Chat Completions）、``responses``
（OpenAI Responses）和 ``anthropic``（Anthropic Messages）。

每笔请求从最高优先级且未停用的 API 开始；失败时在同一笔请求内依次
回退。单路在当前 Bot 响应期累计 3 次失败后停用，到下一不响应期边界
清零并恢复。Responses 的 system 提示词放在 ``instructions``；工具定义
会从 Chat Completions 的嵌套 ``function`` 结构转换为 Responses 所需的
顶层 ``name`` / ``description`` / ``parameters`` 结构。缓存是否可用及
自动缓存能力由所配置的服务端决定，Anthropic 的 ``auto_cached`` 仅应在
服务商明确支持时启用。
"""

import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime, timedelta

import httpx
from openai import OpenAI

from llm.prompt_capture import capture_prompt

# 部分网关会按浏览器 User-Agent 放行。统一带上浏览器 UA，避免被
# 误判为非浏览器客户端。
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


# =========================================================
# 解释器退出保护
# =========================================================
# 热重载/优雅退出时解释器进入 finalization，httpx/httpcore 在新建同步连接时
# 会调用 atexit.register，此时会抛 "can't register atexit after shutdown"。
# 做法：在模块导入时（解释器尚健康）注册回调置位标志；一旦置位，
# 任何后台线程都不得再新建 OpenAI/httpx 客户端或发起新的 LLM 请求，
# 统一抛 ProviderShuttingDown 快速失败（上层会静默丢弃，不发错误消息）。
#
# 自愈机制：置位后每次检查都会做一次“存活探测”（能否在当前进程新建 httpx
# 客户端）。若进程仍健康（例如 httpx 竞态误报/曾短暂进入退出流程又恢复），
# 自动清除标志并继续服务，避免一次误置位让 bot 永久沉默、所有消息静默丢弃。
# 真正的解释器退出时探测必然失败，仍会快速失败并静默丢弃。
_INTERP_SHUTTING_DOWN = threading.Event()
_SHUTDOWN_LOCK = threading.RLock()
_SHUTDOWN_AT = None       # 置位时刻（time.monotonic），诊断用
_SHUTDOWN_SOURCE = None   # 置位来源：atexit / threading_atexit / httpx_race
_LAST_SHUTDOWN_DIAGNOSTIC_AT = 0.0
_SHUTDOWN_DIAGNOSTIC_INTERVAL_SECONDS = 10.0


class ProviderShuttingDown(RuntimeError):
    """解释器退出中，禁止再发起新的 LLM 请求。"""


def _mark_interp_shutdown(source="atexit"):
    """置位退出标志，并记录来源与置位时刻（幂等，仅首次生效）。"""
    global _SHUTDOWN_AT, _SHUTDOWN_SOURCE
    with _SHUTDOWN_LOCK:
        _INTERP_SHUTTING_DOWN.set()
        if _SHUTDOWN_AT is None:
            _SHUTDOWN_AT = time.monotonic()
            _SHUTDOWN_SOURCE = source
            print(f"[LLM] 解释器退出保护置位 source={source}", flush=True)
            _shutdown_diagnostic(f"guard_marked:{source}")


# 用普通 atexit 注册置位回调：本模块 import 较晚，atexit 按 LIFO 执行，
# 因此 finalization 一开始（其它 atexit handler 运行前）就会置位标志，
# 尽早拦截后台线程新建 OpenAI/httpx 客户端；再额外用 threading._register_atexit
# 注册一次作为兜底（在线程 join 前再次置位，幂等无副作用）。
import atexit

try:
    atexit.register(lambda: _mark_interp_shutdown("atexit"))
except Exception:
    pass

_atexit_registerer = getattr(threading, "_register_atexit", None)
if _atexit_registerer is not None:
    try:
        _atexit_registerer(lambda: _mark_interp_shutdown("threading_atexit"))
    except Exception:
        pass


def _threading_shutdown_started():
    """Return whether threading shutdown has started."""
    try:
        return bool(getattr(threading, "_SHUTTING_DOWN", False))
    except Exception:
        return True


def _shutdown_diagnostic(reason, exc=None):
    """Best-effort diagnostic wrapper: diagnostics must never affect shutdown."""
    try:
        _shutdown_diagnostic_impl(reason, exc)
    except Exception:
        pass


def _shutdown_diagnostic_impl(reason, exc=None):
    """Emit rate-limited local diagnostics; never send this to WeChat."""
    global _LAST_SHUTDOWN_DIAGNOSTIC_AT
    now = time.monotonic()
    with _SHUTDOWN_LOCK:
        if now - _LAST_SHUTDOWN_DIAGNOSTIC_AT < _SHUTDOWN_DIAGNOSTIC_INTERVAL_SECONDS:
            return
        _LAST_SHUTDOWN_DIAGNOSTIC_AT = now
    try:
        thread_info = [{"name": t.name, "daemon": t.daemon, "alive": t.is_alive()} for t in threading.enumerate()]
    except Exception as thread_exc:
        thread_info = [{"error": f"{type(thread_exc).__name__}: {thread_exc}"}]
    details = {
        "reason": reason,
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
        "main_thread_alive": threading.main_thread().is_alive(),
        "sys_finalizing": _runtime_is_finalizing(),
        "threading_shutting_down": _threading_shutdown_started(),
        "guard_set": _INTERP_SHUTTING_DOWN.is_set(),
        "guard_source": _SHUTDOWN_SOURCE,
        "threads": thread_info,
    }
    if exc is not None:
        details["error"] = f"{type(exc).__name__}: {exc}"
        details["traceback"] = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=8)).strip()
    print("[LLM SHUTDOWN DIAGNOSTIC] " + json.dumps(details, ensure_ascii=False, default=str), flush=True)

def _runtime_is_finalizing():
    """?? CPython ???????? finalization?"""
    checker = getattr(sys, "is_finalizing", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:
        # finalization ????????????????????????????
        return True


def _is_shutdown_registration_error(exc):
    """????????? httpx/OpenAI ?????????"""
    text = str(exc or "").casefold()
    return "atexit" in text and "shutdown" in text


def _probe_interpreter_alive():
    """???????????????? finalization ????????"""
    if _runtime_is_finalizing():
        return False
    try:
        client = httpx.Client(timeout=httpx.Timeout(2.0))
        client.close()
        return not _runtime_is_finalizing()
    except Exception:
        return False


def _shutting_down():
    """Return whether new LLM requests must be suppressed during shutdown."""
    # In Python 3.14 this is the exact state behind
    # "can't register atexit after shutdown". A successful temporary httpx
    # construction does not make a following SDK request safe once it is true.
    if _runtime_is_finalizing() or _threading_shutdown_started():
        _shutdown_diagnostic("runtime_shutdown_state")
        return True
    if not _INTERP_SHUTTING_DOWN.is_set():
        return False
    if _probe_interpreter_alive():
        global _SHUTDOWN_AT, _SHUTDOWN_SOURCE
        with _SHUTDOWN_LOCK:
            if _runtime_is_finalizing() or _threading_shutdown_started():
                _shutdown_diagnostic("shutdown_started_during_probe")
                return True
            _INTERP_SHUTTING_DOWN.clear()
            _SHUTDOWN_AT = None
            _SHUTDOWN_SOURCE = None
        print("[LLM] cleared stale shutdown guard; runtime is healthy", flush=True)
        return False
    _shutdown_diagnostic("guarded_shutdown_probe_failed")
    return True


def _parse_clock(value, default):
    """解析 'HH:MM' 为当天分钟数；非法返回 default。"""
    try:
        parts = str(value).strip().split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
        return hour * 60 + minute
    except (TypeError, ValueError, IndexError):
        return default


def compute_daily_reset_minute(time_slots):
    """计算每日重置时刻（0..1439 分钟）。

    重置时刻 = 每日最早的不响应时间段起点，即响应窗口结束时间（按当天折算）。
    未配置 time_slots（或全部非法）时返回 0，表示每日 0 点。
    """
    if not time_slots:
        return 0
    ends = []
    for slot in time_slots:
        if not isinstance(slot, dict):
            continue
        start = _parse_clock(slot.get("start"), None)
        end = _parse_clock(slot.get("end"), None)
        if start is None or end is None:
            continue
        if end <= start:
            end += 1440  # 跨午夜窗口（如 08:00 -> 02:00）
        ends.append(end % 1440)
    if not ends:
        return 0
    return min(ends)


def _cache_control_dict(ttl=""):
    """构建 Anthropic cache_control；ttl='1h' 时显式延长缓存生存期。"""
    cache_control = {"type": "ephemeral"}
    if str(ttl or "").strip().lower() in ("1h", "3600"):
        cache_control["ttl"] = "1h"
    return cache_control


def _content_to_text(content):
    """把 OpenAI 消息 content（str / 文本块列表 / tool_result）转成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                btype = block.get("type")
                if btype == "text":
                    parts.append(str(block.get("text") or ""))
                elif btype == "tool_result":
                    parts.append(_content_to_text(block.get("content")))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    return str(content)


def _to_anthropic_tools(tools):
    """OpenAI function tools -> Anthropic tools 数组。"""
    result = []
    for tool in tools or []:
        if isinstance(tool, dict):
            fn = tool.get("function") or {}
            name = str(fn.get("name") or "")
            description = str(fn.get("description") or "")
            parameters = fn.get("parameters") or {"type": "object", "properties": {}}
        else:
            fn = getattr(tool, "function", None)
            name = str(getattr(fn, "name", "") or "")
            description = str(getattr(fn, "description", "") or "")
            parameters = getattr(fn, "parameters", None) or {"type": "object", "properties": {}}
        if not name:
            continue
        result.append({
            "name": name,
            "description": description,
            "input_schema": parameters,
        })
    return result


def _to_responses_tools(tools):
    """Convert Chat Completions function tools to Responses function tools.

    Chat Completions nests the definition under `function`; Responses uses
    the same outer `type` but puts `name`, `description` and
    `parameters` at the top level. Passing the nested shape through causes
    gateways to report `tools[0].name` as missing.
    """
    result = []
    for tool in tools or []:
        if isinstance(tool, dict):
            fn = tool.get("function") or tool
            tool_type = str(tool.get("type") or "function")
            name = str(fn.get("name") or "")
            description = str(fn.get("description") or "")
            parameters = fn.get("parameters") or {"type": "object", "properties": {}}
            strict = fn.get("strict", tool.get("strict"))
        else:
            fn = getattr(tool, "function", None) or tool
            tool_type = str(getattr(tool, "type", None) or "function")
            name = str(getattr(fn, "name", "") or "")
            description = str(getattr(fn, "description", "") or "")
            parameters = getattr(fn, "parameters", None) or {"type": "object", "properties": {}}
            strict = getattr(fn, "strict", None)
        if not name:
            continue
        converted = {
            "type": tool_type,
            "name": name,
            "description": description,
            "parameters": parameters,
        }
        if strict is not None:
            converted["strict"] = bool(strict)
        result.append(converted)
    return result


def _merge_consecutive(messages):
    """合并连续同角色消息（Anthropic 不允许同角色连续）。"""
    merged = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            prev = merged[-1]["content"]
            cur = msg["content"]
            if isinstance(prev, str) and isinstance(cur, list):
                base = [{"type": "text", "text": prev}] if prev else []
                merged[-1]["content"] = base + cur
            elif isinstance(prev, list) and isinstance(cur, str):
                if cur:
                    prev.append({"type": "text", "text": cur})
            elif isinstance(prev, str) and isinstance(cur, str):
                merged[-1]["content"] = (prev + "\n" + cur) if (prev and cur) else (prev or cur)
            elif isinstance(prev, list) and isinstance(cur, list):
                prev.extend(cur)
        else:
            merged.append(dict(msg))
    return merged


def _to_anthropic_messages(messages, cache_enabled=False, cache_ttl="", cache_mode="auto"):
    """OpenAI 消息 -> (system_text, Anthropic messages)。

    cache_enabled=True 且 cache_mode='manual' 时按 cache_breakpoint 位置
    给聊天上下文注入 cache_control，让服务端缓存该前缀；
    cache_mode='auto' 时不注入消息内断点，改由请求层 auto_cached=true
    让网关自动计算最长稳定前缀，cache_ttl 只作为 TTL 计时声明。
    cache_ttl='1h' 表示 1 小时 TTL，云端缓存会自动续期，最长支持 5 小时。
    """
    system_parts = []
    out = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role == "system":
            text = _content_to_text(content)
            if text:
                system_parts.append(text)
            continue
        if role == "user":
            text = _content_to_text(content)
            if (
                cache_enabled
                and cache_mode == "manual"
                and msg.get("cache_breakpoint")
                and text
            ):
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "text",
                        "text": text,
                        "cache_control": _cache_control_dict(cache_ttl),
                    }],
                })
            else:
                out.append({"role": "user", "content": text})
        elif role == "assistant":
            text = _content_to_text(content)
            blocks = []
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    tc_id = str(tc.get("id") or "")
                    fn = tc.get("function") or {}
                    fn_name = str(fn.get("name") or "")
                    args = fn.get("arguments") or "{}"
                else:
                    tc_id = str(getattr(tc, "id", "") or "")
                    fn = getattr(tc, "function", None)
                    fn_name = str(getattr(fn, "name", "") or "")
                    args = getattr(fn, "arguments", "{}") or "{}"
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False)
                try:
                    input_obj = json.loads(args) if str(args).strip() else {}
                except (TypeError, ValueError):
                    input_obj = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc_id or f"toolu_{len(blocks)}",
                    "name": fn_name,
                    "input": input_obj,
                })
            out.append({"role": "assistant", "content": blocks if blocks else text})
        elif role == "tool":
            tool_use_id = str(msg.get("tool_call_id") or "")
            tool_text = _content_to_text(msg.get("content")) or ""
            if not out or out[-1]["role"] != "user":
                out.append({"role": "user", "content": ""})
            if isinstance(out[-1]["content"], str):
                base = [{"type": "text", "text": out[-1]["content"]}] if out[-1]["content"] else []
                out[-1]["content"] = base
            out[-1]["content"].append({
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": tool_text,
            })
    system_text = "\n".join(part for part in system_parts if part).strip()
    return system_text, _merge_consecutive(out)


def _responses_instructions(messages):
    """提取 system 消息为 Responses 顶层 instructions 文本（无则返回空串）。

    OpenAI Responses 规范中 system 不放入 input items，而应作为顶层
    ``instructions`` 参数传递，避免兼容网关忽略自定义 system 或改写它。
    """
    parts = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "system":
            text = _content_to_text(msg.get("content"))
            if text:
                parts.append(text)
    return "\n\n".join(part for part in parts if part).strip()


def _to_responses_input(messages, include_cache_breakpoints=False):
    """OpenAI chat messages -> OpenAI Responses input items.

    GPT-5.6 retains implicit/automatic caching by default. ``cache_breakpoint``
    merely adds an explicit checkpoint to an immutable history record; it does
    not switch the request into an explicit-only cache mode.
    """
    items = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "system":
            continue
        if role == "user":
            content = _content_to_text(msg.get("content"))
            if include_cache_breakpoints and msg.get("cache_breakpoint"):
                items.append({
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": content,
                        "prompt_cache_breakpoint": {"mode": "explicit"},
                    }],
                })
            else:
                items.append({"role": "user", "content": content})
        elif role == "assistant":
            content = _content_to_text(msg.get("content"))
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                items.append({"role": "assistant", "content": content or None})
                for call in tool_calls:
                    if isinstance(call, dict):
                        call_id = str(call.get("id") or "")
                        function = call.get("function") or {}
                        name = str(function.get("name") or "")
                        arguments = function.get("arguments") or "{}"
                    else:
                        call_id = str(getattr(call, "id", "") or "")
                        function = getattr(call, "function", None)
                        name = str(getattr(function, "name", "") or "")
                        arguments = getattr(function, "arguments", "{}") or "{}"
                    if not isinstance(arguments, str):
                        arguments = json.dumps(arguments, ensure_ascii=False)
                    items.append({
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments,
                    })
            else:
                items.append({"role": "assistant", "content": content})
        elif role == "tool":
            items.append({
                "type": "function_call_output",
                "call_id": str(msg.get("tool_call_id") or ""),
                "output": _content_to_text(msg.get("content")) or "",
            })
    return items


def _responses_breakpoint_count(items) -> int:
    """Count explicit markers without logging any prompt content."""
    return sum(
        1
        for item in items or []
        if isinstance(item, dict) and isinstance(item.get("content"), list)
        for block in item["content"]
        if isinstance(block, dict) and block.get("prompt_cache_breakpoint")
    )


def _responses_breakpoint_unsupported(exc) -> bool:
    error_text = str(exc or "").lower()
    return "prompt_cache_breakpoint" in error_text and any(
        token in error_text for token in ("unknown", "unsupported", "unrecognized", "invalid", "extra fields")
    )


class _SimpleFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _SimpleToolCall:
    def __init__(self, id, name, arguments):
        self.id = id
        self.function = _SimpleFunction(name, arguments)


class _SimpleMessage:
    """非 pydantic 的消息对象，兼容 llm_service 的 _assistant_message_dict。"""

    def __init__(self, content, tool_calls=None, usage=None):
        self.content = content
        self.tool_calls = tool_calls or []
        self.usage = usage or {}


# =========================================================
# 单端点客户端
# =========================================================

class OpenAICompatibleClient:
    """单个 OpenAI Chat Completions 兼容端点。"""

    def __init__(self, entry):
        self.entry = entry
        self.name = entry["name"]
        self.model = entry["model"]
        self.timeout = entry["timeout_seconds"]
        self.last_usage = {}
        # Automatic Responses caching is widely supported; the GPT-specific
        # content-block breakpoint extension is not.  Keep it opt-in per API.
        self._explicit_cache_breakpoints_enabled = bool(
            self.entry.get("responses_explicit_cache_breakpoint", False)
        )
        self._init_client()
        # 缓存范围：full（完整前缀缓存）/ system（仅系统提示词）/ none（不可缓存）。
        scope = str(self.entry.get("cache_scope") or "").strip().lower()
        if scope not in {"full", "system", "none"}:
            scope = 'full'
        self.cache_scope = scope

    def _init_client(self):
        if _shutting_down():
            raise ProviderShuttingDown("LLM provider is shutting down")
        api_key = self.entry["api_key"]
        base_url = self.entry["base_url"]
        try:
            http_client = httpx.Client(timeout=httpx.Timeout(self.timeout))
            self._client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                http_client=http_client,
                max_retries=0,
                default_headers={"User-Agent": BROWSER_UA},
            )
        except RuntimeError as exc:
            if not _is_shutdown_registration_error(exc):
                raise
            _mark_interp_shutdown("httpx_client_init")
            _shutdown_diagnostic("httpx_client_init_error", exc)
            raise ProviderShuttingDown("LLM provider is shutting down") from exc

    def chat(self, messages, tools=None, prompt_cache_key=None):
        """调用该端点，返回 assistant 消息（含工具调用）。"""
        if _shutting_down():
            raise ProviderShuttingDown("LLM provider is shutting down")
        kwargs = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        else:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            capture_prompt(kwargs["messages"])
            response = self._client.chat.completions.create(**kwargs)
            self._log_usage(response)
            return response.choices[0].message
        except httpx.TimeoutException as exc:
            print(
                f"[LLM TIMEOUT] api={self.name} error={type(exc).__name__} "
                f"messages={len(messages)} chars={self._message_chars(messages)} "
                f"estimated_tokens={self._estimate_tokens(messages)}",
                flush=True,
            )
            raise
        except Exception as exc:
            if _is_shutdown_registration_error(exc):
                _mark_interp_shutdown("openai_chat_request")
                _shutdown_diagnostic("openai_chat_request_error", exc)
                raise ProviderShuttingDown("LLM provider is shutting down") from exc
            error_text = str(exc).lower()
            if any(marker in error_text for marker in (
                "context length", "maximum context", "too many tokens", "request too large",
                "payload too large", "413", "prompt is too long",
            )):
                print(
                    f"[LLM CONTEXT LIMIT] api={self.name} error={type(exc).__name__} "
                    f"messages={len(messages)} chars={self._message_chars(messages)} "
                    f"estimated_tokens={self._estimate_tokens(messages)}",
                    flush=True,
                )
            raise

    def _log_usage(self, response):
        usage = getattr(response, "usage", None)
        if usage is None:
            print(f"[LLM TOKENS] api={self.name} usage unavailable", flush=True)
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
            f"[LLM TOKENS] api={self.name} " + " ".join(
                f"{key}={value}" for key, value in fields.items() if value is not None
            ),
            flush=True,
        )
        # 前缀缓存命中率遥测（DeepSeek/OpenAI 兼容端点的 prompt_cache_hit/miss 字段）。
        hit = self._usage_value(usage, "prompt_cache_hit_tokens")
        miss = self._usage_value(usage, "prompt_cache_miss_tokens")
        if hit is not None and miss is not None and (hit + miss) > 0:
            rate = hit / float(hit + miss) * 100
            print(
                f"[LLM CACHE] api={self.name} hit_rate={rate:.1f}% hit={hit} miss={miss}",
                flush=True,
            )

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

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            if getattr(self, "_client", None) is not None and hasattr(self._client, "_client"):
                self._client._client.close()
        except Exception:
            pass


class ResponsesClient:
    """单个 OpenAI Responses 协议端点（base_url/responses）。"""

    def __init__(self, entry):
        self.entry = entry
        self.name = entry["name"]
        self.model = entry["model"]
        self.timeout = entry["timeout_seconds"]
        self.max_output_tokens = int(entry.get("max_tokens") or 0)
        self.last_usage = {}
        self._init_client()
        # 缓存范围：full（完整前缀缓存）/ system（仅系统提示词）/ none（不可缓存）。
        scope = str(self.entry.get("cache_scope") or "").strip().lower()
        if scope not in {"full", "system", "none"}:
            scope = 'full'
        self.cache_scope = scope

    def _init_client(self):
        if _shutting_down():
            raise ProviderShuttingDown("LLM provider is shutting down")
        api_key = self.entry["api_key"]
        base_url = self.entry["base_url"]
        try:
            http_client = httpx.Client(timeout=httpx.Timeout(self.timeout))
            self._client = OpenAI(
                api_key=api_key,
                base_url=base_url,
                http_client=http_client,
                max_retries=0,
                default_headers={"User-Agent": BROWSER_UA},
            )
        except RuntimeError as exc:
            if not _is_shutdown_registration_error(exc):
                raise
            _mark_interp_shutdown("httpx_client_init")
            raise ProviderShuttingDown("LLM provider is shutting down") from exc

    def chat(self, messages, tools=None, prompt_cache_key=None):
        """Call this endpoint with implicit cache plus one history checkpoint."""
        if _shutting_down():
            raise ProviderShuttingDown("LLM provider is shutting down")
        responses_input = _to_responses_input(
            messages,
            include_cache_breakpoints=self._explicit_cache_breakpoints_enabled,
        )
        kwargs = {
            "model": self.model,
            "input": responses_input,
        }
        instructions = _responses_instructions(messages)
        if instructions:
            kwargs["instructions"] = instructions
        if tools:
            kwargs["tools"] = _to_responses_tools(tools)
            kwargs["tool_choice"] = "auto"
        else:
            kwargs["text"] = {"format": {"type": "json_object"}}
        if self.max_output_tokens > 0:
            kwargs["max_output_tokens"] = self.max_output_tokens

        # Do not set prompt_cache_options.mode=explicit: GPT-5.6's default
        # implicit/automatic cache remains enabled. The key routes a group/flow
        # to a stable cache shard and never contains the raw group identifier.
        cache_enabled = bool(self.entry.get("cache", True)) and self.cache_scope != "none"
        cache_key = str(prompt_cache_key or self.entry.get("prompt_cache_key") or "").strip()
        if cache_enabled and cache_key:
            kwargs["prompt_cache_key"] = cache_key
        breakpoint_count = _responses_breakpoint_count(responses_input)
        if cache_enabled:
            print(
                f"[LLM CACHE REQUEST] api={self.name} mode=implicit "
                f"key={'yes' if cache_key else 'no'} explicit_breakpoints={breakpoint_count}",
                flush=True,
            )

        retried_format = False
        try:
            capture_prompt(messages)
            response = self._client.responses.create(**kwargs)
            self._log_usage(response)
            return _responses_message_to_openai(response)
        except httpx.TimeoutException as exc:
            print(
                f"[LLM TIMEOUT] api={self.name} error={type(exc).__name__} "
                f"messages={len(messages)} chars={OpenAICompatibleClient._message_chars(messages)}",
                flush=True,
            )
            raise
        except Exception as exc:
            if _is_shutdown_registration_error(exc):
                _mark_interp_shutdown("responses_request")
                _shutdown_diagnostic("responses_request_error", exc)
                raise ProviderShuttingDown("LLM provider is shutting down") from exc
            # Keep the request usable with gateways that implement automatic
            # caching but have not yet added the GPT-5.6 content-block marker.
            if _responses_breakpoint_unsupported(exc) and breakpoint_count:
                # Prevent every future call in this process from first paying
                # for a known-invalid request.
                self._explicit_cache_breakpoints_enabled = False
                print(
                    f"[LLM CACHE FALLBACK] api={self.name} gateway rejected explicit breakpoint; "
                    "disabling it for this process and retrying automatic caching only",
                    flush=True,
                )
                kwargs["input"] = _to_responses_input(messages, include_cache_breakpoints=False)
                response = self._client.responses.create(**kwargs)
                self._log_usage(response)
                return _responses_message_to_openai(response)
            if (
                not tools
                and not retried_format
                and any(marker in str(exc).lower() for marker in (
                    "502", "503", "504", "upstream", "internal server",
                    "bad gateway", "server error", "must contain the word 'json'",
                ))
            ):
                retried_format = True
                print(
                    f"[LLM FORMAT RETRY] api={self.name} "
                    "Gateway rejected text.format=json_object; retrying without text.format",
                    flush=True,
                )
                kwargs.pop("text", None)
                response = self._client.responses.create(**kwargs)
                self._log_usage(response)
                return _responses_message_to_openai(response)
            error_text = str(exc).lower()
            if any(marker in error_text for marker in (
                "context length", "maximum context", "too many tokens", "request too large",
                "payload too large", "413", "prompt is too long",
            )):
                print(
                    f"[LLM CONTEXT LIMIT] api={self.name} error={type(exc).__name__} "
                    f"messages={len(messages)} chars={OpenAICompatibleClient._message_chars(messages)} "
                    f"estimated_tokens={OpenAICompatibleClient._estimate_tokens(messages)}",
                    flush=True,
                )
            raise

    def _log_usage(self, response):
        usage = getattr(response, "usage", None)
        if usage is None:
            print(f"[LLM TOKENS] api={self.name} usage unavailable", flush=True)
            return
        def _val(name):
            if isinstance(usage, dict):
                return usage.get(name)
            return getattr(usage, name, None)
        def _detail(details, name):
            if isinstance(details, dict):
                return details.get(name)
            return getattr(details, name, None) if details is not None else None
        details = _val("input_tokens_details")
        cache_read = _detail(details, "cached_tokens")
        cache_write = _detail(details, "cache_write_tokens")
        fields = {
            "prompt": _val("input_tokens"),
            "completion": _val("output_tokens"),
            "total": _val("total_tokens"),
        }
        self.last_usage = {k: v for k, v in fields.items() if v is not None}
        if cache_read is not None:
            self.last_usage["cache_read"] = cache_read
        if cache_write is not None:
            self.last_usage["cache_creation"] = cache_write
        print(
            f"[LLM TOKENS] api={self.name} " + " ".join(
                f"{k}={v}" for k, v in fields.items() if v is not None
            )
            + (f" cache_read={cache_read}" if cache_read is not None else "")
            + (f" cache_creation={cache_write}" if cache_write is not None else ""),
            flush=True,
        )
        input_tokens = fields.get("prompt")
        if cache_read is not None and input_tokens not in (None, 0):
            try:
                hit_rate = max(0.0, min(1.0, float(cache_read) / float(input_tokens)))
                print(
                    f"[LLM CACHE] api={self.name} hit_rate={hit_rate:.1%} "
                    f"hit={cache_read} input={input_tokens}",
                    flush=True,
                )
            except (TypeError, ValueError, ZeroDivisionError):
                pass

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            if getattr(self, "_client", None) is not None and hasattr(self._client, "_client"):
                self._client._client.close()
        except Exception:
            pass


def _responses_message_to_openai(response):
    """OpenAI Responses 响应 -> 兼容 OpenAI assistant 消息对象。"""
    text = str(getattr(response, "output_text", None) or "")
    tool_calls = []
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) == "function_call":
            tool_calls.append(_SimpleToolCall(
                id=str(getattr(item, "call_id", None) or getattr(item, "id", "") or ""),
                name=str(getattr(item, "name", "") or ""),
                arguments=getattr(item, "arguments", "{}") or "{}",
            ))
    return _SimpleMessage(content=text, tool_calls=tool_calls, usage=getattr(response, "usage", None))


class AnthropicClient:
    """单个 Anthropic Messages 协议端点（POST {base_url}/v1/messages）。"""

    def __init__(self, entry):
        self.entry = entry
        self.name = entry["name"]
        self.model = entry["model"]
        self.timeout = entry["timeout_seconds"]
        self.max_tokens = int(entry.get("max_tokens") or 4096)
        # Anthropic 缓存开关与 TTL 归一化（cache / cache_ttl）
        self.cache_enabled = bool(entry.get("cache", True))
        self.cache_ttl = str(entry.get("cache_ttl") or "").strip().lower()
        self.last_usage = {}
        # cache_scope：full=完整前缀可缓存；system=仅 system 块可缓存；none=关闭缓存
        scope = str(self.entry.get("cache_scope") or "").strip().lower()
        if scope not in {"full", "system", "none"}:
            scope = 'system'
        self.cache_scope = scope
        # cache_mode：auto=网关自动缓存（仅声明 TTL）；manual=手动断点；off=关闭
        cache_mode = str(self.entry.get("cache_mode") or "").strip().lower()
        if cache_mode not in {"auto", "manual", "off"}:
            cache_mode = "auto"
        if scope == "none":
            cache_mode = "off"
        self.cache_mode = cache_mode
        self._closed = False
        if _shutting_down():
            raise ProviderShuttingDown("LLM provider is shutting down")
        try:
            self._http = httpx.Client(timeout=httpx.Timeout(self.timeout))
        except RuntimeError as exc:
            if _is_shutdown_registration_error(exc):
                _mark_interp_shutdown("anthropic_httpx_client_init")
                _shutdown_diagnostic("anthropic_httpx_client_init_error", exc)
                raise ProviderShuttingDown("LLM provider is shutting down") from exc
            raise

    def chat(self, messages, tools=None, prompt_cache_key=None):
        if _shutting_down():
            raise ProviderShuttingDown("LLM provider is shutting down")
        # 供 /prompt 等使用的最后一次完整提示词快照（在协议转换前记录 OpenAI 格式）。
        capture_prompt(messages)
        system, anth_messages = _to_anthropic_messages(
            messages,
            cache_enabled=self.cache_enabled,
            cache_ttl=self.cache_ttl,
            cache_mode=self.cache_mode,
        )
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": anth_messages,
        }
        if system:
            if self.cache_enabled and self.cache_mode != "off":
                # 仅作为 TTL 计时载体：system 块仍带 cache_control+ttl 声明；
                # auto 模式不再注入消息内断点，由网关 auto_cached 自动计算最长前缀；
                body["system"] = [{
                    "type": "text",
                    "text": system,
                    "cache_control": _cache_control_dict(self.cache_ttl),
                }]
            else:
                body["system"] = system
        if self.cache_enabled and self.cache_mode == "auto":
            # auto 模式：请求顶层声明 auto_cached=true，网关自动计算最长稳定前缀，
            # 服务端按 cache_read / cache_creation 计量，无需消息内断点。
            body["auto_cached"] = True
        if tools:
            body["tools"] = _to_anthropic_tools(tools)

        headers = {
            "x-api-key": self.entry["api_key"],
            "Authorization": "Bearer " + self.entry["api_key"],
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
            "User-Agent": BROWSER_UA,
        }
        try:
            resp = self._http.post(
                self.entry["base_url"].rstrip("/") + "/v1/messages",
                headers=headers,
                json=body,
            )
        except httpx.TimeoutException as exc:
            print(
                f"[LLM TIMEOUT] api={self.name} error={type(exc).__name__} "
                f"messages={len(messages)} chars={OpenAICompatibleClient._message_chars(messages)}",
                flush=True,
            )
            raise
        except Exception as exc:
            if _is_shutdown_registration_error(exc):
                _mark_interp_shutdown("anthropic_request")
                _shutdown_diagnostic("anthropic_request_error", exc)
                raise ProviderShuttingDown("LLM provider is shutting down") from exc
            raise
        if resp.status_code >= 400:
            raise RuntimeError(
                f"{self.name} HTTP {resp.status_code}: {resp.text[:400]}"
            )
        data = resp.json()
        self._log_usage(data)
        return _anthropic_message_to_openai(data)

    def _log_usage(self, data):
        usage = data.get("usage") or {}
        try:
            prompt = int(usage.get("input_tokens") or 0)
            completion = int(usage.get("output_tokens") or 0)
        except (TypeError, ValueError):
            prompt = completion = 0
        try:
            cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
            cache_read = int(usage.get("cache_read_input_tokens") or 0)
        except (TypeError, ValueError):
            cache_creation = cache_read = 0
        self.last_usage = {
            "prompt": prompt,
            "completion": completion,
            "total": prompt + completion,
            "cache_creation": cache_creation,
            "cache_read": cache_read,
        }
        print(
            f"[LLM TOKENS] api={self.name} prompt={prompt} completion={completion} total={prompt + completion} "
            f"cache_creation={cache_creation} cache_read={cache_read}",
            flush=True,
        )

    def close(self):
        if getattr(self, "_closed", False):
            return
        self._closed = True
        try:
            self._http.close()
        except Exception:
            pass


def _anthropic_message_to_openai(data):
    """Anthropic 响应 -> 兼容 OpenAI assistant 消息对象。"""
    text_parts = []
    tool_calls = []
    for block in data.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text") or ""))
        elif btype == "tool_use":
            tool_calls.append(_SimpleToolCall(
                id=str(block.get("id") or ""),
                name=str(block.get("name") or ""),
                arguments=block.get("input") or {},
            ))
    usage = data.get("usage") or {}
    try:
        prompt = int(usage.get("input_tokens") or 0)
        completion = int(usage.get("output_tokens") or 0)
        usage_map = {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }
    except (TypeError, ValueError):
        usage_map = {}
    return _SimpleMessage(
        content="\n".join(part for part in text_parts if part),
        tool_calls=tool_calls,
        usage=usage_map,
    )


# =========================================================
# 多 API 池
# =========================================================

class PoolExhaustedError(RuntimeError):
    """所有 API 均调用失败。"""

    def __init__(self, errors):
        self.errors = list(errors)
        combined = "; ".join(self.errors) if self.errors else "all LLM APIs failed"
        if any("402" in err for err in self.errors):
            combined = "402 " + combined
        super().__init__(combined)


class DeepSeekProvider:
    """Priority-ordered multi-API pool with per-request fallback.

    Each request starts from the highest-priority endpoint that is not disabled.
    A failed endpoint is retried immediately against the next available endpoint in
    the same request.  Failure counts are cumulative for the current bot response
    period: after three failures, that endpoint is skipped until the next reset.
    """

    def __init__(self):
        if _shutting_down():
            raise ProviderShuttingDown("LLM provider is shutting down")
        from llm.config import get_llm_config

        config = get_llm_config()
        self.config = config
        entries = config.get("apis") or []
        if not entries:
            raise ValueError("Missing llm api entries (llm.apis)")
        # Explicit priority: smaller values are tried first; ties retain config order.
        ordered = sorted(
            enumerate(entries),
            key=lambda item: (int(item[1].get("priority") or 0), item[0]),
        )
        self._endpoints = [self._build_endpoint(entry) for _, entry in ordered]
        self._lock = threading.RLock()
        self._reset_key = None
        self._daily_reset_minute = compute_daily_reset_minute(config.get("time_slots"))
        # Cumulative failures during the current response period, per endpoint.
        self._failure_counts = [0 for _ in self._endpoints]
        self._disabled_until_reset = [False for _ in self._endpoints]
        self._failure_disable_threshold = 3
        self.last_usage = {}
        self._apply_daily_reset_if_needed()
        print(
            f"[LLM POOL] Loaded {len(self._endpoints)} API(s): "
            + ", ".join(
                f"{i + 1}.{ep.name}({ep.model}, priority={ep.entry.get('priority')})"
                for i, ep in enumerate(self._endpoints)
            )
            + f"; reset at {self._daily_reset_minute // 60:02d}:{self._daily_reset_minute % 60:02d}",
            flush=True,
        )

    @staticmethod
    def _build_endpoint(entry):
        protocol = str(entry.get("protocol") or "openai").strip().lower()
        if protocol == "anthropic":
            return AnthropicClient(entry)
        if protocol == "responses":
            return ResponsesClient(entry)
        return OpenAICompatibleClient(entry)

    def _first_available_index(self):
        """Return the highest-priority endpoint that is not disabled."""
        with self._lock:
            for index, disabled in enumerate(self._disabled_until_reset):
                if not disabled:
                    return index
        return None

    @property
    def current_api_name(self):
        self._apply_daily_reset_if_needed()
        index = self._first_available_index()
        return self._endpoints[index].name if index is not None else ""

    def current_cache_scope(self):
        """Cache scope of the first endpoint eligible for the next request."""
        self._apply_daily_reset_if_needed()
        index = self._first_available_index()
        if index is None:
            return "none"
        return getattr(self._endpoints[index], "cache_scope", "none")

    def _today_reset_key(self):
        now = datetime.now()
        reset_hour, reset_minute = divmod(self._daily_reset_minute, 60)
        reset_today = now.replace(hour=reset_hour, minute=reset_minute, second=0, microsecond=0)
        if now >= reset_today:
            return now.date()
        return (now - timedelta(days=1)).date()

    def _apply_daily_reset_if_needed(self):
        reset_key = self._today_reset_key()
        with self._lock:
            if reset_key != self._reset_key:
                self._reset_key = reset_key
                self._failure_counts = [0 for _ in self._endpoints]
                self._disabled_until_reset = [False for _ in self._endpoints]
                if self._endpoints:
                    print(
                        f"[LLM POOL] Reset failure counters and re-enabled API pool "
                        f"from {self._endpoints[0].name}",
                        flush=True,
                    )

    def _available_indices_in_priority_order(self):
        """Snapshot all enabled endpoints in priority order for one request."""
        with self._lock:
            return [
                index
                for index, disabled in enumerate(self._disabled_until_reset)
                if not disabled
            ]

    def _record_failure(self, index):
        """Record one cumulative failure and disable after the configured limit."""
        with self._lock:
            self._failure_counts[index] += 1
            failures = self._failure_counts[index]
            disabled = failures >= self._failure_disable_threshold
            if disabled:
                self._disabled_until_reset[index] = True
        if disabled:
            print(
                f"[LLM POOL] API {self._endpoints[index].name} reached {failures} "
                f"failures in this response period; disabled until the next reset",
                flush=True,
            )
        return failures, disabled

    def send_chat(self, messages, tools=None, prompt_cache_key=None):
        """Try each enabled API in priority order within the same LLM request."""
        if _shutting_down():
            raise ProviderShuttingDown("LLM provider is shutting down")
        self._apply_daily_reset_if_needed()

        indices = self._available_indices_in_priority_order()
        if not indices:
            raise PoolExhaustedError([
                "All LLM APIs are disabled after five cumulative failures in this response period"
            ])

        errors = []
        for attempt, index in enumerate(indices, start=1):
            endpoint = self._endpoints[index]
            try:
                message = endpoint.chat(messages, tools=tools, prompt_cache_key=prompt_cache_key)
                self.last_usage = dict(endpoint.last_usage or {})
                return message
            except Exception as exc:
                if _shutting_down():
                    raise ProviderShuttingDown("LLM provider is shutting down") from exc
                failures, disabled = self._record_failure(index)
                errors.append(f"{endpoint.name}: {type(exc).__name__}: {exc}")
                if attempt < len(indices):
                    status = "disabled" if disabled else f"failure {failures}/{self._failure_disable_threshold}"
                    print(
                        f"[LLM POOL] API {endpoint.name} failed ({status}); "
                        f"trying {self._endpoints[indices[attempt]].name} in the same request",
                        flush=True,
                    )

        raise PoolExhaustedError(errors)

    def send(self, messages: list) -> str:
        """向后兼容的字符串 API。"""
        message = self.send_chat(messages)
        return str(getattr(message, "content", None) or "")

    def __del__(self):
        for endpoint in getattr(self, "_endpoints", []) or []:
            try:
                endpoint.close()
            except Exception:
                pass
