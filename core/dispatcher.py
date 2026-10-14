import os
import importlib
import threading
import queue
import copy
from contextlib import nullcontext
import sys

from core.auto_registry import register_raw_message_target
from core.sender import preview_delay_seconds, register_send_listener, send
from utils.runtime_trace import trace_runtime
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
        self.llm_prefix_bypass_wxids = set()
        self.auto_modules = {}
        self.enabled_modules = {}
        self.prefix_mode = "strict"
        # One bounded gate protects the provider and the synchronous web-tool
        # loop from being multiplied by worker/proactive threads.
        self._llm_slots = threading.BoundedSemaphore(1)
        self._llm_direct_slots = threading.BoundedSemaphore(1)
        self._llm_direct_wait_seconds = 120.0
        self._direct_stop = threading.Event()
        self._direct_queue_lock = threading.Lock()
        self._direct_queue = None
        self._direct_thread = None
        self._proactive_delivery_lock = threading.Lock()
        self._proactive_deliveries = {}
        self._pending_proactive_dispatches = {}

    # =========================================================
    # per-group feature switches
    # =========================================================
    def _group_feature_settings(self, group_id):
        """Return per-group switches while keeping router payloads unchanged."""
        group_key = str(group_id or "").strip()
        all_settings = self.config.get("group_features") or {}
        if not isinstance(all_settings, dict):
            all_settings = {}
        raw = all_settings.get(group_key)
        if not isinstance(raw, dict):
            raw = all_settings.get("default") if isinstance(all_settings.get("default"), dict) else {}

        def read(name, alias):
            return bool(raw.get(name, raw.get(alias, True)))

        return {
            "plugins_enabled": read("plugins_enabled", "plugins"),
            "auto_plugins_enabled": read("auto_plugins_enabled", "auto_plugins"),
            "llm_enabled": read("llm_enabled", "llm"),
        }

    def _group_feature_enabled(self, group_id, feature):
        return bool(self._group_feature_settings(group_id).get(feature, True))

    def _group_plugin_allowlist(self, group_id):
        """Return a normalized group-local module allowlist, or None for all."""
        group_key = str(group_id or "").strip()
        settings = self.config.get("group_features") or {}
        raw = settings.get(group_key) if isinstance(settings, dict) else None
        if not isinstance(raw, dict) and isinstance(settings, dict):
            raw = settings.get("default")
        if not isinstance(raw, dict):
            return None
        allowlist = raw.get("plugin_allowlist")
        if not isinstance(allowlist, (list, tuple, set)):
            return None
        return {
            str(item or "").strip().lstrip("/").casefold()
            for item in allowlist
            if str(item or "").strip()
        }

    def _module_enabled_for_group(self, module_name, group_id):
        """Combine the WebUI global switch with an optional group allowlist."""
        if not self.enabled_modules.get(module_name, True):
            return False
        allowlist = self._group_plugin_allowlist(group_id)
        return allowlist is None or str(module_name or "").casefold() in allowlist

    def _context_feature_enabled(self, context, feature):
        return self._group_feature_enabled((context or {}).get("group"), feature)

    # =========================================================
    # init plugins
    # =========================================================
    def init_plugins(self, config):
        if self.inited:
            return

        print("[PLUGIN] 初始化配置...")
        self.config = config or {}
        # Plugins are initialized globally once; optional group allowlists are
        # enforced only while dispatching or broadcasting to each destination.
        configured_prefix_mode = str(
            self.config.get("prefix_mode", "strict") or "strict"
        ).strip().lower()
        self.prefix_mode = "strict" if configured_prefix_mode == "only" else configured_prefix_mode
        if self.prefix_mode not in {"strict", "mixed"}:
            self.prefix_mode = "strict"
        target_groups = self._normalize_target_groups(config.get("target_group"))
        self._init_llm()

        # Optional dependency injection for command plugins that need to use
        # the already-initialized LLM service. This does not alter router payloads.
        for module_name, module in self.modules.items():
            setter = getattr(module, "set_llm_service", None)
            if callable(setter):
                try:
                    setter(self.llm_service)
                except Exception as e:
                    print("[PLUGIN LLM INJECT ERROR]", module.__name__, e)

        for module_name, module in self.modules.items():
            set_batch_callback = getattr(module, "set_batch_callback", None)
            if callable(set_batch_callback):
                try:
                    set_batch_callback(self._dispatch_background_batch)
                except Exception as e:
                    print("[AUTO CALLBACK ERROR]", module.__name__, e)

        for module_name, module in self.modules.items():
            if hasattr(module, "init"):
                try:
                    module.init(config)
                except Exception as e:
                    print("[PLUGIN INIT ERROR]", module.__name__, e)

            if hasattr(module, "start"):
                try:
                    feature = (
                        "auto_plugins_enabled"
                        if module in self.auto_modules.values()
                        else "plugins_enabled"
                    )
                    module.start(
                        lambda content, target_groups=target_groups, feature=feature, module_name=module_name: self._broadcast_text(
                            target_groups,
                            content,
                            feature=feature,
                            module_name=module_name,
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
            self.enabled_modules.setdefault(name, True)

            if not hasattr(module, "handle"):
                return

            command = getattr(module, "COMMAND", None) or f"/{name}"
            self.plugins[command] = module

            # 注册别名
            aliases = getattr(module, "ALIASES", None)
            if isinstance(aliases, (list, tuple, set)):
                for alias in aliases:
                    alias_str = str(alias).strip()
                    if alias_str:
                        self.plugins[alias_str] = module

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
            self.enabled_modules.setdefault(name, True)

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
        auto_command_handled = False
        auto_command_consumed = False
        auto_plugins_enabled = self._context_feature_enabled(context, "auto_plugins_enabled")
        plugins_enabled = self._context_feature_enabled(context, "plugins_enabled")
        llm_enabled = self._context_feature_enabled(context, "llm_enabled")

        for name, module in self.modules.items():
            if not auto_plugins_enabled:
                break
            if getattr(module, "FALLBACK_ONLY", False):
                continue
            if not self._module_enabled_for_group(name, context.get("group")):
                continue
            if auto_target:
                if isinstance(auto_target, (list, tuple, set)):
                    if name not in auto_target:
                        continue
                elif name != auto_target:
                    continue
            auto_commands = getattr(module, "AUTO_COMMANDS", ()) or ()
            if command and command in {str(item).strip() for item in auto_commands}:
                auto_command_handled = True
                auto_command_consumed = bool(
                    auto_command_consumed or getattr(module, "AUTO_COMMAND_CONSUMES", False)
                )
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
                auto_result = auto_results[-1]
                if isinstance(auto_result, dict) and auto_result.get("forward_batch_to_llm"):
                    return self._dispatch_llm(
                        context,
                        batch_messages=auto_result.get("batch_messages") or [],
                        force_reply=bool(auto_result.get("force_reply")),
                        trigger_source=auto_result.get("trigger_source"),
                        cycle_id=auto_result.get("cycle_id"),
                        conversation_priority=auto_result.get("conversation_priority"),
                    )
                if isinstance(auto_result, dict) and auto_result.get("forward_to_llm"):
                    return self._dispatch_llm(context)
                return auto_result

            proactive_candidate = bool(context.get("proactive_candidate"))
            if (
                not proactive_candidate
                and self.prefix_mode == "mixed"
                and not context.get("prefix_used")
                and str(context.get("content") or "").strip()
                and not str(context.get("content") or "").lstrip().startswith("/")
            ):
                # Compatibility fallback for callers that build a context
                # directly instead of passing Router.parse()'s full result.
                proactive_candidate = True
                context["proactive_candidate"] = True

            if proactive_candidate and self.llm_service and llm_enabled:
                print(
                    f"[LLM PROACTIVE ROUTE] group={context.get('group') or context.get('sessionId') or ''} "
                    f"content_chars={len(str(context.get('content') or '').strip())}",
                    flush=True,
                )
                proactive_result = self.llm_service.handle_proactive_message(context)
                if isinstance(proactive_result, dict) and proactive_result.get("density_admitted"):
                    context["_density_admitted"] = True
                if isinstance(proactive_result, dict) and proactive_result.get("forward_batch_to_llm"):
                    return self._dispatch_llm(
                        context,
                        batch_messages=proactive_result.get("batch_messages") or [],
                        force_reply=bool(proactive_result.get("force_reply")),
                        trigger_source=proactive_result.get("trigger_source"),
                        attention_check=bool(proactive_result.get("attention_check")),
                        cycle_id=proactive_result.get("cycle_id"),
                        conversation_priority=proactive_result.get("conversation_priority"),
                    )

                # When non-prefix autonomous mode is enabled, the proactive
                # manager owns the message even while it is idle/attending or
                # waiting for the ten-second flush. Do not let prefix-bypass
                # rules turn the same message into an immediate one-message
                # LLM request and defeat batch context.
                if not context.get("prefix_used"):
                    return self._dispatch_fallback_auto(context)

            if llm_enabled and self._can_forward_to_llm(context):
                return self._dispatch_llm(context)

            return self._dispatch_fallback_auto(context) if auto_plugins_enabled else None

        # An opted-in auto command owns the message. This prevents a command
        # plugin with the same alias from enqueueing the same job a second time.
        if auto_command_consumed and auto_results:
            return auto_results[-1]

        # =====================================================
        # 3. command debug
        # =====================================================
        if self.debug:
            print("\n[COMMAND]")
            print(command, args)

        plugin = self.plugins.get(command)
        plugin_name = getattr(plugin, "__name__", "").rsplit(".", 1)[-1] if plugin else ""
        if plugin and not self.enabled_modules.get(plugin_name, True):
            print(f"[PLUGIN BLOCK] module={plugin_name} disabled by WebUI")
            plugin = None
        elif plugin and not self._module_enabled_for_group(plugin_name, context.get("group")):
            print(f"[PLUGIN BLOCK] group={context.get('group')} module={plugin_name} disabled by group allowlist")
            plugin = None
        if plugin and not plugins_enabled:
            print(f"[PLUGIN BLOCK] group={context.get('group')} command={command} disabled by group_features")
            plugin = None

        # =====================================================
        # 4. 未找到 command → fallback auto
        # =====================================================
        if not plugin:
            # Auto command-like plugins may consume a command to create a
            # follow-up state. Do not turn that interception into an unknown
            # command reply.
            if auto_command_handled:
                return auto_results[-1] if auto_results else None
            if not plugins_enabled:
                return None
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

            prefix_bypass_wxids = llm_config.get("prefix_bypass_wxids", []) or []
            if isinstance(prefix_bypass_wxids, str):
                prefix_bypass_wxids = [prefix_bypass_wxids]

            self.llm_intercept_auto_plugins = {
                str(item).strip()
                for item in intercept_list
                if str(item).strip()
            }
            self.llm_prefix_bypass_wxids = {
                str(item).strip()
                for item in prefix_bypass_wxids
                if str(item).strip()
            }

            if llm_config.get("enabled"):
                self.llm_service = LLMService()
                max_concurrent = max(
                    1,
                    min(int(self.llm_service.config.get("max_concurrent_requests", 1) or 1), 4),
                )
                self._llm_slots = threading.BoundedSemaphore(max_concurrent)
                direct_concurrent = max(
                    1,
                    min(int(self.llm_service.config.get("direct_max_concurrent_requests", 1) or 1), 2),
                )
                self._llm_direct_slots = threading.BoundedSemaphore(direct_concurrent)
                try:
                    self._llm_direct_wait_seconds = max(
                        0.0,
                        min(float(self.llm_service.config.get("direct_request_wait_seconds", 120) or 120), 300.0),
                    )
                except (TypeError, ValueError):
                    self._llm_direct_wait_seconds = 120.0
                self.llm_service.set_proactive_callback(self._dispatch_background_batch)
                self.llm_service.set_scheduled_event_callback(self._dispatch_scheduled_event)
                self.llm_service.proactive_reply.set_cycle_end_callback(self._curate_cycle)
                register_send_listener(
                    "llm-proactive-web-message-counter",
                    self._on_successful_bot_send,
                )
            else:
                self.llm_service = None
        except Exception as e:
            print("[LLM INIT ERROR]", e)
            self.llm_service = None
            self.llm_intercept_auto_plugins = set()
            self.llm_prefix_bypass_wxids = set()

    def _dispatch_scheduled_event(self, event):
        """Run a due reminder as an LLM turn without blocking message workers."""
        event = event if isinstance(event, dict) else {}
        group_id = str(event.get("group_id") or "").strip()
        if not self.llm_service or not group_id:
            return False, "LLM service or reminder group is unavailable"
        if not self._group_feature_enabled(group_id, "llm_enabled"):
            print(f"[LLM SCHEDULED EVENT] discarded while LLM disabled group={group_id} id={event.get('id')}", flush=True)
            return True
        if not self._llm_slots.acquire(timeout=120.0):
            return False, "LLM provider remained busy"
        try:
            result = self.llm_service.handle_scheduled_event(event)
            if not isinstance(result, dict) or not result.get("_llm_ok", True):
                return False, "LLM failed to generate a reminder"
            messages = [str(item or "").strip() for item in (result.get("messages") or []) if str(item or "").strip()]
            target = group_id
            sent_messages = []
            first_send = True
            delay = preview_delay_seconds("wechat_text")
            for content in messages:
                outcome = send(
                    target=target,
                    content=content,
                    mode="wechat_text",
                    delay_seconds=delay if first_send else 0,
                )
                ok = bool(outcome[0]) if isinstance(outcome, tuple) else bool(outcome)
                if ok:
                    sent_messages.append(content)
                    first_send = False
                else:
                    reason = outcome[1] if isinstance(outcome, tuple) and len(outcome) > 1 else "send failed"
                    print(f"[LLM SCHEDULED SEND FAILED] group={group_id} event={event.get('id')} reason={reason}", flush=True)
            animation_sent = False
            animation = result.get("animation")
            if animation:
                try:
                    from llm.security import get_emoji_path
                    file_path = get_emoji_path(animation)
                    if file_path:
                        outcome = send(
                            target=target,
                            file_path=file_path,
                            mode="wechat_file",
                            delay_seconds=delay if first_send else 0,
                        )
                        animation_sent = bool(outcome[0]) if isinstance(outcome, tuple) else bool(outcome)
                except Exception as exc:
                    print(f"[LLM SCHEDULED ANIMATION ERROR] {exc}", flush=True)
            if not sent_messages and not animation_sent:
                return False, "LLM returned no delivered reminder text"
            try:
                self.llm_service.commit_assistant_delivery(
                    group_id,
                    result,
                    sent_messages=sent_messages,
                    animation_sent=animation_sent,
                )
            except Exception as exc:
                print(f"[LLM SCHEDULED COMMIT ERROR] {exc}", flush=True)
            print(
                f"[LLM SCHEDULED EVENT] delivered group={group_id} event={event.get('id')} "
                f"messages={len(sent_messages)}",
                flush=True,
            )
            return True
        except Exception as exc:
            print(f"[LLM SCHEDULED EVENT ERROR] group={group_id} event={event.get('id')} {exc}", flush=True)
            return False, f"{type(exc).__name__}: {exc}"
        finally:
            self._llm_slots.release()
            self._drain_deferred_proactive()

    def _on_successful_bot_send(self, target=None, mode=None, content=None, file_path=None):
        """Count each successfully sent Bot text for proactive-web timing."""
        if mode != "wechat_text" or not self.llm_service:
            return
        group_id = str(target or "").strip()
        if not group_id:
            return
        self.llm_service.proactive_reply.record_sent_bot_message(group_id, 1)

    def _reserve_proactive_delivery(self, group_id):
        from uuid import uuid4
        key = str(group_id or "unknown")
        with self._proactive_delivery_lock:
            if key in self._proactive_deliveries:
                return None
            token = uuid4().hex
            self._proactive_deliveries[key] = {"token": token, "lane": None}
            return token

    def _release_proactive_delivery(self, group_id, token, drain=True):
        key = str(group_id or "unknown")
        with self._proactive_delivery_lock:
            lease = self._proactive_deliveries.get(key)
            if not lease or lease["token"] != token:
                return False
            self._proactive_deliveries.pop(key, None)
        if lease.get("lane") is not None:
            lease["lane"].release()
        trace_runtime("llm_proactive_delivery_finished", group=key)
        if drain:
            self._drain_deferred_proactive()
        return True

    def _defer_proactive_dispatch(self, context, options):
        if self._direct_stop.is_set():
            return
        key = str(context.get("group") or context.get("sessionId") or "unknown")
        # Coalesce only requests waiting for a delivered history. Keep actual
        # message IDs and the newest request metadata; do not generate a second
        # reply against stale history and do not discard the final queued turn.
        with self._proactive_delivery_lock:
            previous = self._pending_proactive_dispatches.get(key)
            merged = list((previous or {}).get("options", {}).get("batch_messages") or [])
            seen = {str(item.get("message_id") or item.get("messageKey") or "") for item in merged if isinstance(item, dict)}
            for item in options.get("batch_messages") or []:
                identity = str(item.get("message_id") or item.get("messageKey") or "") if isinstance(item, dict) else ""
                if identity and identity in seen:
                    continue
                merged.append(copy.deepcopy(item))
                if identity: seen.add(identity)
            queued_options = dict(options)
            queued_options["batch_messages"] = merged[-256:]
            self._pending_proactive_dispatches[key] = {"context": copy.deepcopy(context), "options": queued_options}
        trace_runtime("llm_proactive_deferred", group=key, messages=len(merged[-256:]))

    def _drain_deferred_proactive(self):
        if self._direct_stop.is_set():
            return
        # A background helper may own provider capacity without a delivery
        # lease. Do not start a busy-spin chain of deferred worker threads.
        if not self._llm_slots.acquire(blocking=False):
            return
        self._llm_slots.release()
        with self._proactive_delivery_lock:
            # Background turn delivery is the boundary across all groups, not
            # merely an available HTTP slot. This also avoids busy-spin retries.
            if self._proactive_deliveries or not self._pending_proactive_dispatches:
                return
            key = next(iter(self._pending_proactive_dispatches))
            pending = self._pending_proactive_dispatches.pop(key)
        def run():
            try:
                context = pending["context"]
                context["planned_send_delay_seconds"] = preview_delay_seconds("wechat_text")
                result = self._dispatch_llm(context, **pending["options"])
                self._deliver_background_result(context, result)
            except Exception as exc:
                print(f"[LLM DEFERRED ERROR] group={key} {exc}", flush=True)
        threading.Thread(target=run, daemon=True, name="llm-after-delivery").start()

    def confirm_llm_delivery(self, result, sent_messages, animation_sent=False):
        # The lease ends after send outcomes have been committed to history, never
        # when generation merely returns. A delayed/failed/partial send also ends it.
        try:
            return self._confirm_llm_delivery(result, sent_messages, animation_sent)
        finally:
            if isinstance(result, dict) and result.get("_proactive_delivery_token"):
                self._release_proactive_delivery(result.get("_proactive_delivery_group"), result["_proactive_delivery_token"])

    def _confirm_llm_delivery(self, result, sent_messages, animation_sent=False):
        """Apply reply state only after the concrete sender reports delivery."""
        if not isinstance(result, dict) or not self.llm_service:
            return False
        if not result.get("_llm_delivery"):
            return False
        if result.get("_delivery_confirmed"):
            return bool(result.get("_delivery_sent_any"))

        context = result.get("_delivery_context")
        context = context if isinstance(context, dict) else {}
        group_id = str(result.get("_delivery_group_id") or context.get("group") or "")
        source_result = result.get("_delivery_result")
        source_result = source_result if isinstance(source_result, dict) else result
        sent_messages = [
            str(item or "").strip()
            for item in (sent_messages or [])
            if str(item or "").strip()
        ]
        sent_any = bool(sent_messages or animation_sent)

        try:
            committed = self.llm_service.commit_assistant_delivery(
                group_id,
                source_result,
                sent_messages=sent_messages,
                animation_sent=bool(animation_sent),
            )
        except Exception as exc:
            print(f"[LLM DELIVERY COMMIT ERROR] {exc}", flush=True)
            committed = dict(source_result)
            committed["messages"] = sent_messages
            committed["animation"] = source_result.get("animation") if animation_sent else None

        if not sent_any and result.get("_delivery_reply_reserved"):
            try:
                self.llm_service.proactive_reply.cancel_llm_reply_reservation(group_id)
            except Exception:
                pass

        try:
            if result.get("_delivery_is_batch"):
                self.llm_service.on_proactive_result(
                    context,
                    committed,
                    attention_check=bool(result.get("_delivery_attention_check")),
                )
            else:
                self.llm_service.proactive_reply.record_conversation_reply(context, committed)
        except Exception as exc:
            print(f"[LLM DELIVERY STATE ERROR] {exc}", flush=True)
        result["_delivery_confirmed"] = True
        result["_delivery_sent_any"] = sent_any
        return sent_any

    def _curate_cycle(self, group_id, context, messages):
        if not self.llm_service or not self._group_feature_enabled(group_id, "llm_enabled"):
            return True
        # Cycle curation also performs synchronous LLM/tool work. Keep it in
        # the same provider budget as foreground and proactive batches.
        if not self._llm_slots.acquire(blocking=False):
            print(f"[LLM BUSY] skip cycle curation group={group_id}", flush=True)
            return False
        try:
            return self.llm_service.curate_cycle(
                group_id,
                context=context,
                cycle_messages=messages,
            )
        finally:
            self._llm_slots.release()
            self._drain_deferred_proactive()

    def _can_forward_to_llm(self, context):
        if not self.llm_service:
            return False

        wxid = str(context.get("wxid") or "").strip()
        bypass_prefix_required = wxid in self.llm_prefix_bypass_wxids

        explicitly_addressed = bool(context.get("directed_to_bot") or context.get("reply_to_bot"))
        if not context.get("prefix_used") and not bypass_prefix_required and not explicitly_addressed:
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

    def _enqueue_direct_request(self, context, options):
        """Bounded FIFO: release message workers immediately, never drop on a short slot wait."""
        with self._direct_queue_lock:
            if self._direct_stop.is_set():
                return {"messages": ["LLM \u6b63\u5728\u5173\u95ed\uff0c\u8bf7\u91cd\u542f\u540e\u91cd\u8bd5\u3002"], "mode": "wechat_text"}
            if self._direct_queue is None:
                capacity = ((self.config or {}).get("llm") or {}).get("direct_queue_size", 64)
                try:
                    capacity = max(1, min(int(capacity or 64), 256))
                except (TypeError, ValueError):
                    capacity = 64
                self._direct_queue = queue.Queue(maxsize=capacity)
            try:
                self._direct_queue.put_nowait((copy.deepcopy(context), dict(options)))
            except queue.Full:
                print(f"[LLM DIRECT QUEUE FULL] group={context.get('group')} capacity={self._direct_queue.maxsize}", flush=True)
                return {
                    "target": context.get("group") or context.get("user"),
                    "messages": ["LLM \u8bf7\u6c42\u961f\u5217\u5df2\u6ee1\uff0c\u672c\u6b21\u8bf7\u6c42\u672a\u53d7\u7406\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002"],
                    "mode": "wechat_text",
                }
            if self._direct_thread is None or not self._direct_thread.is_alive():
                self._direct_thread = threading.Thread(
                    target=self._direct_request_worker, name="llm-direct-requests", daemon=True
                )
                self._direct_thread.start()
        trace_runtime("llm_direct_queued", group=context.get("group"),
                      wxid=context.get("wxid"), message_key=context.get("messageKey"),
                      pending=self._direct_queue.qsize())
        print(f"[LLM DIRECT QUEUED] group={context.get('group')} key={context.get('messageKey')} pending={self._direct_queue.qsize()}", flush=True)
        return None

    def _direct_request_worker(self):
        while not self._direct_stop.is_set():
            try:
                context, options = self._direct_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                result = self._dispatch_llm(context, _from_direct_queue=True, **options)
                if isinstance(result, dict):
                    self._deliver_background_result(context, result)
            except Exception as exc:
                print(f"[LLM DIRECT ERROR] group={context.get('group')} key={context.get('messageKey')} {type(exc).__name__}: {exc}", flush=True)
                if not self._direct_stop.is_set():
                    self._deliver_background_result(context, {
                        "messages": ["LLM \u8bf7\u6c42\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u91cd\u8bd5\u3002"], "mode": "wechat_text"
                    })
            finally:
                self._direct_queue.task_done()

    def _dispatch_llm(
        self,
        context,
        batch_messages=None,
        force_reply=False,
        trigger_source=None,
        attention_check=False,
        cycle_id=None,
        conversation_priority=None,
        _from_direct_queue=False,
    ):
        if not self._context_feature_enabled(context, "llm_enabled"):
            return None
        # Attention and ordinary autonomous pulls are cheap to skip when a
        # request is already using the provider. Direct prefix requests enter
        # a bounded FIFO instead of competing on a fail-fast timeout.
        priority = str(conversation_priority or context.get("_conversation_priority") or context.get("conversation_priority") or "MAY_REPLY").upper()
        explicit_address = bool(
            context.get("prefix_used")
            or context.get("directed_to_bot")
            or context.get("reply_to_bot")
            or context.get("is_at_bot")
        )
        direct = (
            batch_messages is None
            or bool(force_reply)
            or explicit_address
            or priority == "MUST_REPLY"
        )
        if explicit_address and not force_reply:
            force_reply = True
            priority = "MUST_REPLY"
        if batch_messages is None and not _from_direct_queue:
            return self._enqueue_direct_request(context, {
                "batch_messages": batch_messages,
                "force_reply": force_reply,
                "trigger_source": trigger_source,
                "attention_check": attention_check,
                "cycle_id": cycle_id,
                "conversation_priority": priority,
            })
        group_id = context.get("group") or context.get("sessionId") or "unknown"
        trace_runtime(
            "llm_release",
            group=group_id,
            wxid=context.get("wxid"), message_key=context.get("messageKey"),
            mode="batch" if batch_messages is not None else "direct",
            direct=bool(direct),
            force_reply=bool(force_reply),
            prefix_used=bool(context.get("prefix_used")),
            priority=priority,
            trigger=trigger_source or "",
            attention=bool(attention_check),
        )

        # Frequency penalties are an optional release-time backstop, not a
        # post-generation content policy. A group profile overrides only its
        # declared timing fields and inherits the global auto_reply defaults.
        auto_reply = ((self.config or {}).get("llm") or {}).get("auto_reply") or {}
        if self.llm_service is not None:
            auto_reply = dict(
                (self.llm_service._effective_group_config(group_id) or {}).get("auto_reply")
                or auto_reply
            )
        frequency_gate_enabled = auto_reply.get("reply_frequency_gate_enabled", False)
        if isinstance(frequency_gate_enabled, str):
            frequency_gate_enabled = frequency_gate_enabled.strip().casefold() in {"1", "true", "yes", "on"}
        delivery_token = None
        if batch_messages is not None:
            delivery_token = self._reserve_proactive_delivery(group_id)
            if not delivery_token:
                self._defer_proactive_dispatch(context, {
                    "batch_messages": batch_messages, "force_reply": force_reply,
                    "trigger_source": trigger_source, "attention_check": attention_check,
                    "cycle_id": cycle_id, "conversation_priority": priority,
                })
                trace_runtime("llm_proactive_delivery_busy", group=group_id, trigger=trigger_source or "")
                print(f"[LLM DELIVERY BUSY] waiting for previous proactive send group={group_id}", flush=True)
                return None
        reply_gate = None
        reply_reserved = False
        if batch_messages is not None and not direct and bool(frequency_gate_enabled) and self.llm_service:
            reply_gate = self.llm_service.proactive_reply
            try:
                reply_reserved = reply_gate.try_reserve_llm_reply(
                    group_id,
                    force_reply=force_reply,
                    attention_check=attention_check,
                    priority=priority,
                    context=context,
                    batch_messages=batch_messages,
                )
            except Exception:
                if delivery_token:
                    self._release_proactive_delivery(group_id, delivery_token)
                raise
            if not reply_reserved:
                if delivery_token:
                    self._release_proactive_delivery(group_id, delivery_token)
                return None

        wait_seconds = self._llm_direct_wait_seconds if direct else 0.0
        lane = self._llm_direct_slots if direct else self._llm_slots
        if _from_direct_queue:
            # A queued request stays queued until capacity is available; 10s/120s
            # waiting is not a reason to silently discard a user's request.
            acquired = False
            while not self._direct_stop.is_set():
                if lane.acquire(timeout=0.5):
                    acquired = True
                    break
        else:
            acquired = lane.acquire(timeout=wait_seconds)
        if not acquired:
            if delivery_token:
                self._defer_proactive_dispatch(context, {
                    "batch_messages": batch_messages, "force_reply": force_reply,
                    "trigger_source": trigger_source, "attention_check": attention_check,
                    "cycle_id": cycle_id, "conversation_priority": priority,
                })
                self._release_proactive_delivery(group_id, delivery_token, drain=False)
            if reply_reserved and reply_gate:
                reply_gate.cancel_llm_reply_reservation(
                    context.get("group") or context.get("sessionId") or "unknown"
                )
            trace_runtime(
                "llm_busy_wait_exhausted",
                group=group_id,
                mode="batch" if batch_messages is not None else "direct",
                direct=bool(direct),
                wait_seconds=wait_seconds,
                trigger=trigger_source or "",
            )
            print(
                f"[LLM BUSY] skip batch={batch_messages is not None} "
                f"attention={attention_check} trigger={trigger_source}",
                flush=True,
            )
            return None
        delivery_pending = False
        try:
            scope = getattr(self.llm_service, "request_lane", None)
            with scope("direct" if direct else "background") if callable(scope) else nullcontext():
                result = self._dispatch_llm_unlimited(
                    context,
                    batch_messages=batch_messages,
                    force_reply=force_reply,
                    trigger_source=trigger_source,
                    attention_check=attention_check,
                    cycle_id=cycle_id,
                    conversation_priority=priority,
                )
            if isinstance(result, dict):
                result["_delivery_reply_reserved"] = bool(reply_reserved)
            trace_runtime(
                "llm_result",
                group=group_id,
                mode="batch" if batch_messages is not None else "direct",
                direct=bool(direct),
                wxid=context.get("wxid"), message_key=context.get("messageKey"),
                has_messages=bool(isinstance(result, dict) and result.get("messages")),
                should_reply=bool(isinstance(result, dict) and result.get("should_reply")),
            )
            if reply_reserved and reply_gate:
                has_reply = bool(
                    isinstance(result, dict)
                    and ((result.get("messages") or []) or result.get("animation"))
                )
                if not has_reply:
                    reply_gate.cancel_llm_reply_reservation(
                        context.get("group") or context.get("sessionId") or "unknown"
                    )
            if delivery_token and isinstance(result, dict) and (result.get("messages") or result.get("animation")):
                result["_proactive_delivery_token"] = delivery_token
                result["_proactive_delivery_group"] = str(group_id)
                # Retain background provider capacity through the physical send.
                # Explicit direct requests keep their own lane; group lease still
                # forbids another passive reply built on pre-delivery history.
                if not direct:
                    with self._proactive_delivery_lock:
                        self._proactive_deliveries[str(group_id)]["lane"] = lane
                delivery_pending = True
            return result
        except Exception:
            if reply_reserved and reply_gate:
                reply_gate.cancel_llm_reply_reservation(
                    context.get("group") or context.get("sessionId") or "unknown"
                )
            raise
        finally:
            if not (delivery_pending and not direct):
                lane.release()
            if delivery_token and not delivery_pending:
                self._release_proactive_delivery(group_id, delivery_token)

    def _dispatch_llm_unlimited(
        self,
        context,
        batch_messages=None,
        force_reply=False,
        trigger_source=None,
        attention_check=False,
        cycle_id=None,
        conversation_priority=None,
    ):
        try:
            if batch_messages is not None:
                result = self.llm_service.handle_batch_message(
                    group_id=context.get("group") or context.get("sessionId") or "unknown",
                    messages=batch_messages,
                    force_reply=force_reply,
                    trigger_source=trigger_source,
                    attention_check=attention_check,
                    session_id=context.get("sessionId"),
                    allow_memory_update=False,
                    cycle_id=cycle_id or context.get("_cycle_id"),
                    attention_source=context.get("_attention_source") or "idle",
                    nonsense_opportunity=bool(context.get("_nonsense_opportunity")),
                    slang_emotional_opportunity=bool(context.get("_slang_emotional_opportunity")),
                    tieba_opportunity=bool(context.get("_tieba_opportunity")),
                    proactive_web_opportunity=bool(context.get("_proactive_web_opportunity")),
                    idle_content_opportunity=bool(context.get("_idle_content_opportunity")),
                    idle_content_probability=context.get("_idle_content_probability", 0.0),
                    idle_content_reason=context.get("_idle_content_reason") or "",
                    conversation_priority=conversation_priority or context.get("_conversation_priority") or "MAY_REPLY",
                    density_admitted=bool(context.get("_density_admitted")),
                )
            else:
                result = self.llm_service.handle_message(
                    group_id=context.get("group") or context.get("sessionId") or "unknown",
                    nickname=context.get("user", "未知用户"),
                    content=context.get("content", ""),
                    wxid=context.get("wxid", ""),
                    session_id=context.get("sessionId"),
                    message_context=context,
                    force_reply=bool(
                        context.get("prefix_used")
                        or context.get("directed_to_bot")
                        or context.get("reply_to_bot")
                    ),
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
            "should_reply": result.get("should_reply"),
            "reply_to": result.get("reply_to") or [],
            "mode": "wechat_text",
            "delay_seconds": context.get("planned_send_delay_seconds"),
            "_llm_delivery": True,
            "_delivery_group_id": result.get("_delivery_group_id") or context.get("group") or context.get("user"),
            "_delivery_context": context,
            "_delivery_is_batch": batch_messages is not None,
            "_delivery_attention_check": bool(attention_check),
            "_delivery_result": result,
        }

    def _dispatch_background_batch(
        self,
        context,
        batch_messages,
        force_reply=False,
        trigger_source=None,
        attention_check=False,
    ):
        """Flush a timer-owned proactive batch without waiting for a new event."""
        context = dict(context or {})
        if not self._context_feature_enabled(context, "llm_enabled"):
            return
        context["planned_send_delay_seconds"] = preview_delay_seconds("wechat_text")
        result = self._dispatch_llm(
            context,
            batch_messages=batch_messages,
            force_reply=force_reply,
            trigger_source=trigger_source,
            attention_check=attention_check,
        )
        self._deliver_background_result(context, result)

    def _deliver_background_result(self, context, result):
        if not isinstance(result, dict):
            return
        target = result.get("target") or context.get("group") or context.get("user")
        messages = result.get("messages") or []
        delay_seconds = result.get("delay_seconds")
        first_send = True
        sent_messages = []
        animation_sent = False
        try:
            for item in messages:
                content = str(item or "").strip()
                if not content:
                    continue
                outcome = send(
                    target=target,
                    content=content,
                    mode="wechat_text",
                    delay_seconds=delay_seconds if first_send else 0,
                )
                ok = bool(outcome[0]) if isinstance(outcome, tuple) else bool(outcome)
                if ok:
                    sent_messages.append(content)
                    first_send = False
                else:
                    reason = outcome[1] if isinstance(outcome, tuple) and len(outcome) > 1 else ""
                    print(f"[LLM BACKGROUND SEND FAILED] target={target} reason={reason}", flush=True)

            animation = result.get("animation")
            if animation:
                try:
                    from llm.security import get_emoji_path
                    file_path = get_emoji_path(animation)
                    if file_path:
                        outcome = send(
                            target=target,
                            file_path=file_path,
                            mode="wechat_file",
                            delay_seconds=delay_seconds if first_send else 0,
                        )
                        animation_sent = bool(outcome[0]) if isinstance(outcome, tuple) else bool(outcome)
                except Exception as e:
                    print("[AUTO ANIMATION ERROR]", e, flush=True)

        except Exception as exc:
            print(f"[LLM BACKGROUND DELIVERY ERROR] target={target} {exc}", flush=True)
        finally:
            self.confirm_llm_delivery(result, sent_messages, animation_sent=animation_sent)

    def _dispatch_fallback_auto(self, context):
        if not self._context_feature_enabled(context, "auto_plugins_enabled"):
            return None
        for name, module in self.auto_modules.items():
            if not self._module_enabled_for_group(name, context.get("group")):
                continue
            if not getattr(module, "FALLBACK_ONLY", False):
                continue

            try:
                result = module.handle_auto(context)
            except Exception as e:
                print("[AUTO FALLBACK ERROR]", name, e)
                continue

            if result is None:
                continue

            if isinstance(result, dict) and result.get("forward_to_llm"):
                return self._dispatch_llm(context)

            if isinstance(result, dict) and result.get("forward_batch_to_llm"):
                return self._dispatch_llm(
                    context,
                    batch_messages=result.get("batch_messages") or [],
                    force_reply=bool(result.get("force_reply")),
                    trigger_source=result.get("trigger_source"),
                )

            return result

        return None

    # =========================================================
    # debug
    # =========================================================
    def shutdown(self):
        """Stop LLM-owned background work before process teardown."""
        self._direct_stop.set()
        # Stop registered plugin monitors before their LLM/sender dependencies.
        for module in list(self.modules.values()):
            stop = getattr(module, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception as exc:
                    print(f"[PLUGIN SHUTDOWN ERROR] {getattr(module, '__name__', '')}: {exc}", flush=True)
        if self.llm_service is not None:
            try:
                self.llm_service.close()
            except Exception as exc:
                print(f"[LLM SHUTDOWN ERROR] {exc}", flush=True)
        self.llm_service = None
        self.inited = False

    def set_module_enabled(self, name, enabled):
        module_name = str(name or "").strip()
        if not module_name:
            raise ValueError("module name is required")
        if module_name not in self.modules:
            raise KeyError(module_name)
        self.enabled_modules[module_name] = bool(enabled)
        return {"name": module_name, "enabled": self.enabled_modules[module_name]}

    def module_enabled(self, name):
        return bool(self.enabled_modules.get(str(name or ""), True))

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

    def _broadcast_text(self, target_groups, content, feature="plugins_enabled", module_name=None):
        result = False

        for target in target_groups:
            if not self._group_feature_enabled(target, feature):
                continue
            if module_name and not self._module_enabled_for_group(module_name, target):
                continue
            ok, _ = send(
                target=target,
                content=content,
                mode="wechat_text"
            )
            result = result or ok

        return result
