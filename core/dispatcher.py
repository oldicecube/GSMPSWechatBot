import os
import importlib
import threading
import time
import sys

from core.auto_registry import register_raw_message_target
from core.sender import send
from llm.core import LLMService

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

PLUGIN_DIR = "plugins"
AUTO_DIR = "auto"


class Dispatcher:

    def __init__(self):
        self.plugins = {}   # command -> module
        self.modules = {}   # all modules
        self.mtimes = {}

        self.inited = False
        self.reload_lock = threading.Lock()

        self.debug = True
        self.config = {}
        self.llm_service = None
        self.llm_intercept_auto_plugins = set()
        self.auto_modules = {}

    # =========================================================
    # init plugins
    # =========================================================
    def init_plugins(self, config):
        if self.inited:
            return

        print("[PLUGIN] 初始化配置...")
        self.config = config or {}
        target_groups = self._normalize_target_groups(config.get("target_group"))
        self._init_llm()

        for module in self.modules.values():
            if hasattr(module, "init"):
                try:
                    module.init(config)
                except Exception as e:
                    print("[PLUGIN INIT ERROR]", module.__name__, e)

            if hasattr(module, "start"):
                try:
                    module.start(
                        lambda content, target_groups=target_groups: self._broadcast_text(
                            target_groups,
                            content
                        )
                    )
                except Exception as e:
                    print("[PLUGIN START ERROR]", module.__name__, e)

        self.inited = True

    # =========================================================
    # load plugins
    # =========================================================
    def load_plugins(self):
        self._load_command_plugins()
        self._load_auto_plugins()

        print("\n[PLUGIN REGISTER TABLE]")
        for k in self.plugins:
            print("  -", k)

    # =========================================================
    # command plugins
    # =========================================================
    def _load_command_plugins(self):
        if not os.path.exists(PLUGIN_DIR):
            return

        for fname in os.listdir(PLUGIN_DIR):
            if fname.endswith(".py"):
                self._load_plugin(fname)

    def _load_plugin(self, fname):
        name = fname[:-3]
        path = f"{PLUGIN_DIR}.{name}"

        try:
            module = importlib.import_module(path)
            self.modules[name] = module

            if not hasattr(module, "handle"):
                return

            command = getattr(module, "COMMAND", None) or f"/{name}"
            self.plugins[command] = module

            self.mtimes[name] = os.path.getmtime(
                os.path.join(PLUGIN_DIR, fname)
            )

            print(f"[PLUGIN REGISTER] {name} -> {command}")

        except Exception as e:
            print(f"[PLUGIN ERROR] {fname}: {e}")

    # =========================================================
    # auto plugins（已改为 handle_auto 模式）
    # =========================================================
    def _load_auto_plugins(self):
        if not os.path.exists(AUTO_DIR):
            return

        for fname in os.listdir(AUTO_DIR):
            if fname.endswith(".py"):
                self._load_auto(fname)

    def _load_auto(self, fname):
        name = fname[:-3]
        path = f"{AUTO_DIR}.{name}"

        try:
            module = importlib.import_module(path)
            self.modules[name] = module
            self.auto_modules[name] = module

            if getattr(module, "MATCH_RAW_MESSAGE", False):
                register_raw_message_target(name)

            print(f"[AUTO REGISTER] {name}")

        except Exception as e:
            print(f"[AUTO LOAD ERROR] {fname}: {e}")

    # =========================================================
    # 🔥 核心 dispatch（融合 auto + command）
    # =========================================================
    def dispatch(self, command, args, context):

        if self.debug:
            print("\n[DISPATCH]")
            print("content =", context.get("content"))

        # =====================================================
        # 1. AUTO 阶段（统一 context 直通）
        # =====================================================
        auto_results = []
        auto_target = context.get("auto_target")

        for name, module in self.modules.items():
            if auto_target:
                if isinstance(auto_target, (list, tuple, set)):
                    if name not in auto_target:
                        continue
                elif name != auto_target:
                    continue
            if hasattr(module, "handle_auto"):
                try:
                    res = module.handle_auto(context)
                    if res is not None:
                        auto_results.append(res)
                except Exception as e:
                    print("[AUTO ERROR]", name, e)

        if self.debug:
            print("[AUTO RESULT]", auto_results)

        # =====================================================
        # 2. 无 command → 直接走 auto
        # =====================================================
        if not command:
            if auto_results:
                return auto_results[-1]

            if self._can_forward_to_llm(context):
                return self._dispatch_llm(context)

            return None

        # =====================================================
        # 3. command debug
        # =====================================================
        if self.debug:
            print("\n[COMMAND]")
            print(command, args)

        plugin = self.plugins.get(command)

        # =====================================================
        # 4. 未找到 command → fallback auto
        # =====================================================
        if not plugin:
            return auto_results[-1] if auto_results else f"未知命令: {command}"

        # =====================================================
        # 5. 执行 command plugin
        # =====================================================
        try:
            result = plugin.handle(args, context)

            if result is None:
                return auto_results[-1] if auto_results else None

            return result

        except Exception as e:
            print("[PLUGIN ERROR]", command, e)
            return "插件执行异常"

    def _init_llm(self):
        try:
            llm_config = (self.config or {}).get("llm", {}) or {}
            intercept_list = llm_config.get("intercept_auto_plugins", []) or []
            if isinstance(intercept_list, str):
                intercept_list = [intercept_list]

            self.llm_intercept_auto_plugins = {
                str(item).strip()
                for item in intercept_list
                if str(item).strip()
            }

            if llm_config.get("enabled"):
                self.llm_service = LLMService()
            else:
                self.llm_service = None
        except Exception as e:
            print("[LLM INIT ERROR]", e)
            self.llm_service = None
            self.llm_intercept_auto_plugins = set()

    def _can_forward_to_llm(self, context):
        if not self.llm_service:
            return False

        if not context.get("prefix_used"):
            return False

        content = str(context.get("content") or "").strip()
        if not content:
            return False

        if content.startswith("/"):
            return False

        configured = self.llm_intercept_auto_plugins
        if not configured:
            return True

        for name in configured:
            module = self.auto_modules.get(name)
            if not module:
                print(f"[LLM BLOCK] missing auto intercept module: {name}")
                return False

            if not getattr(module, "INTERCEPT_LLM", False):
                print(f"[LLM BLOCK] auto plugin not declared for llm intercept: {name}")
                return False

            allow_fn = getattr(module, "allow_llm", None)
            if not callable(allow_fn):
                print(f"[LLM BLOCK] auto plugin missing allow_llm(): {name}")
                return False

            try:
                allow = bool(allow_fn(context))
            except Exception as e:
                print(f"[LLM BLOCK] allow_llm error: {name} {e}")
                return False

            if not allow:
                print(f"[LLM BLOCK] auto plugin denied llm forward: {name}")
                return False

        return True

    def _dispatch_llm(self, context):
        try:
            result = self.llm_service.handle_message(
                group_id=context.get("group") or context.get("sessionId") or "unknown",
                nickname=context.get("user", "未知用户"),
                content=context.get("content", "")
            )
        except Exception as e:
            print("[LLM ERROR]", e)
            return None

        if not isinstance(result, dict):
            return None

        return {
            "target": context.get("group") or context.get("user"),
            "messages": result.get("messages") or [],
            "animation": result.get("animation"),
            "mode": "wechat_text",
            "delay_seconds": context.get("planned_send_delay_seconds")
        }

    # =========================================================
    # debug
    # =========================================================
    def debug_state(self):
        print("\n===== DISPATCHER STATE =====")
        print("plugins:", list(self.plugins.keys()))
        print("modules:", list(self.modules.keys()))
        print("===========================\n")

    def _normalize_target_groups(self, target_group):
        if isinstance(target_group, str):
            group = target_group.strip()
            return [group] if group else []

        if isinstance(target_group, (list, tuple, set)):
            return [
                str(group).strip()
                for group in target_group
                if str(group).strip()
            ]

        return []

    def _broadcast_text(self, target_groups, content):
        result = False

        for target in target_groups:
            ok, _ = send(
                target=target,
                content=content,
                mode="wechat_text"
            )
            result = result or ok

        return result
