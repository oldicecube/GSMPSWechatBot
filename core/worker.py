import threading
import sys
import os
import time
import queue as queue_lib

from core.sender import preview_delay_seconds, send
from llm.memory import LongTermMemory, MemoryManager
from llm.learning import StyleLearner
from llm.security import get_emoji_path

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)


class Worker:
    def __init__(self, queue, router, dispatcher, config=None):
        self.queue = queue
        self.router = router
        self.dispatcher = dispatcher
        self.config = config or {}
        self._task_local = threading.local()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._threads = []
        self.memory_manager = self._build_memory_manager()
        self.long_term_memory = self._build_long_term_memory()
        try:
            worker_llm_config = (self.config or {}).get("llm") if isinstance(self.config, dict) else {}
            worker_llm_config = dict(worker_llm_config or {})
            learning_config = dict(worker_llm_config.get("learning") or {})
            weflow_config = (self.config or {}).get("weflow") or {}
            learning_config["bot_wxids"] = list(learning_config.get("bot_wxids") or [])
            learning_config["prefixes"] = list(self.config.get("prefix") or []) if isinstance(self.config.get("prefix"), list) else [self.config.get("prefix")]
            if weflow_config.get("myWxid"):
                learning_config["bot_wxids"].append(weflow_config.get("myWxid"))
            worker_llm_config["learning"] = learning_config
            self.style_learner = StyleLearner(worker_llm_config)
        except Exception as exc:
            print(f"[WORKER LEARNING INIT ERROR] {exc}")
            self.style_learner = None

    # =========================================================
    # 🚀 主线程
    # =========================================================
    def run(self):
        """Keep the consumer alive when one malformed message/plugin dies."""
        while not self._stop_event.is_set():
            try:
                self._run_loop()
                return
            except Exception as exc:
                if getattr(self._task_local, "active", False):
                    try:
                        self.queue.task_done()
                    except Exception:
                        pass
                    self._task_local.active = False
                print(f"[WORKER FATAL] consumer restarted: {type(exc).__name__}: {exc}", flush=True)

    def _run_loop(self):
        print("[WORKER] 线程启动")

        while not self._stop_event.is_set():
            if self._pause_event.is_set():
                time.sleep(0.2)
                continue
            try:
                msg = self.queue.get(timeout=1)
            except queue_lib.Empty:
                continue

            self._task_local.active = True

            print("[WORKER] 收到任务")
            self._listen_group_message(msg)

            parsed = self.router.parse(msg)

            if not parsed:
                self._finish_task()
                print("[WORKER] 未匹配命令")
                continue

            parsed_list = parsed if isinstance(parsed, list) else [parsed]

            for item in parsed_list:

                command = item.get("command")
                args = item.get("args")

                print(f"[WORKER] 命中命令: {command} {args}")

                # =========================
                # 📦 context
                # =========================
                raw_flags = msg.get("_raw") if isinstance(msg.get("_raw"), dict) else {}
                try:
                    routing = self.router.inspect_message(msg)
                except Exception:
                    routing = {}
                sender_wxid = item.get("wxid") or msg.get("wxid")
                is_bot_echo = bool(
                    msg.get("is_bot")
                    or raw_flags.get("is_bot")
                    or (sender_wxid and self._is_bot_wxid(sender_wxid))
                )
                try:
                    normalized_local_type = int(msg.get("localType") or msg.get("local_type") or 0)
                except (TypeError, ValueError):
                    normalized_local_type = 0
                # Source precedence is deliberate: system events and Bot echoes
                # must never be reclassified as user input by an upstream
                # normalizer that supplied a generic ``user_incoming`` value.
                if normalized_local_type in {10000, 10002}:
                    inbound_source_kind = "system_event"
                elif is_bot_echo:
                    inbound_source_kind = "bot_echo_external"
                else:
                    inbound_source_kind = msg.get("source_kind") or "user_incoming"
                inbound_source_origin = (
                    "weflow_system_event" if normalized_local_type in {10000, 10002}
                    else ("wechat_echo" if is_bot_echo else (msg.get("source_origin") or "wechat_inbound"))
                )
                inbound_learnable = (
                    False if normalized_local_type in {10000, 10002} or is_bot_echo
                    else bool(msg.get("learnable", True))
                )
                inbound_prompt_visible = (
                    False if normalized_local_type in {10000, 10002} or is_bot_echo
                    else bool(msg.get("prompt_visible", True))
                )
                context = {
                    "user": item.get("user", "未知用户"),
                    "group": msg.get("group"),
                    "sessionId": msg.get("sessionId"),
                    "messageKey": msg.get("messageKey"),
                    "localId": msg.get("localId"),
                    "serverId": msg.get("serverId") or msg.get("svrid") or msg.get("rawid"),
                    "svrid": msg.get("svrid"),
                    "rawid": msg.get("rawid"),
                    "wxid": sender_wxid,
                    "is_bot": is_bot_echo,
                    "type": msg.get("type"),
                    "is_group": msg.get("is_group", msg.get("type") == "group"),
                    "is_private": msg.get("is_private", msg.get("type") == "private"),
                    "is_at": bool(
                        msg.get("is_at")
                        or msg.get("isAt")
                        or raw_flags.get("is_at")
                        or raw_flags.get("isAt")
                    ),
                    "is_mentioned": bool(
                        msg.get("is_mentioned")
                        or msg.get("isMentioned")
                        or raw_flags.get("is_mentioned")
                        or raw_flags.get("isMentioned")
                    ),
                    "is_picture": msg.get("is_picture", False),
                    "is_emoji": msg.get("is_emoji", False),
                    "is_voice": msg.get("is_voice", False),
                    "source_kind": inbound_source_kind,
                    "source_origin": inbound_source_origin,
                    "learnable": inbound_learnable,
                    "prompt_visible": inbound_prompt_visible,
                    "quoted_message": routing.get("quoted_message"),
                    "quoted_message_id": routing.get("quoted_message_id"),
                    "quoted_sender": routing.get("quoted_sender"),
                    "quoted_sender_wxid": routing.get("quoted_sender_wxid"),
                    "quoted_content": routing.get("quoted_content"),
                    "quoted_message_type": routing.get("quoted_message_type"),
                    "quoted_timestamp": routing.get("quoted_timestamp"),
                    "quoted_available": routing.get("quoted_available"),
                    "quoted_role": routing.get("quoted_role"),
                    "quoted_is_bot": routing.get("quoted_is_bot"),
                    "command": command,
                    "args": args,
                    "content": item.get("content"),
                    "raw": msg,
                    "auto_target": item.get("auto_target"),
                    "followup_payload": item.get("followup_payload") or {},
                    "prefix_used": bool(item.get("prefix_used")),
                    # Preserve the Router's mixed-mode admission decision.
                    # Without this field the Worker rebuilt the context and
                    # silently disabled every ordinary-message proactive turn.
                    "proactive_candidate": bool(item.get("proactive_candidate")),
                    "planned_send_delay_seconds": preview_delay_seconds("wechat_text"),
                    # Internal direction hints only; outbound router/send payloads
                    # remain unchanged. Unknown direction is deliberately left
                    # for the prompt/LLM to judge from the full transcript.
                    "mentions": routing.get("mentions") or [],
                    "reply_target": routing.get("reply_target") or "",
                    "directed_to_bot": bool(routing.get("directed_to_bot")),
                    "directed_to_other": bool(routing.get("directed_to_other")),
                    "address_confidence": routing.get("address_confidence", 0.0),
                    "reply_to_bot": bool(routing.get("reply_to_bot")),
                    "reply_to_other": bool(routing.get("reply_to_other")),
                }

                # =========================
                # ⚙️ 执行 dispatcher
                # =========================
                result = self.dispatcher.dispatch(command, args, context)

                print("[WORKER DEBUG] raw result =", result)

                if not result:
                    self._finish_task()
                    print("[WORKER] 空返回，跳过")
                    continue

                # =========================================================
                # 🧠 1. 统一 dict 协议（推荐）
                # =========================================================
                if isinstance(result, dict):
                    target = result.get("target") or context.get("group")
                    mode = result.get("mode", "wechat_text")
                    delay_seconds = result.get("delay_seconds")

                    if isinstance(result.get("messages"), list):
                        self._send_structured_result(target, result)
                        self._finish_task()
                        continue

                    content = result.get("content")

                    if content is None or content == "":
                        self._finish_task()
                        print("[WORKER] content 为空，丢弃")
                        continue

                    print(f"[WORKER] 发送结果: target={target}, content={content}")

                    send(
                        target=target,
                        content=content,
                        mode=mode,
                        delay_seconds=delay_seconds,
                        **({"preserve_text": True} if result.get("preserve_text") is True else {})
                    )
                    self._finish_task()
                    continue

                # =========================================================
                # 🧠 2. 兼容 string 返回（旧插件）
                # =========================================================
                if isinstance(result, str):

                    print(f"[WORKER] string result: {result}")

                    send(
                        target=context.get("group") or context.get("user"),
                        content=result,
                        mode="wechat_text",
                        delay_seconds=context.get("planned_send_delay_seconds")
                    )
                    self._finish_task()
                    continue

                # =========================================================
                # 🧠 3. 其他类型直接丢弃
                # =========================================================
                print("[WORKER] unknown result type:", type(result))
                self._finish_task()

    def _finish_task(self):
        if not getattr(self._task_local, "active", False):
            return
        try:
            self.queue.task_done()
        finally:
            self._task_local.active = False

    def _build_memory_manager(self):
        try:
            if not isinstance(self.config.get("llm"), dict):
                return None
            return MemoryManager()
        except Exception:
            return None

    def _build_long_term_memory(self):
        try:
            llm_config = self.config.get("llm")
            if not isinstance(llm_config, dict):
                return None
            memory_config = dict(llm_config.get("memory") or {})
            bot_wxids = list(llm_config.get("bot_wxids") or [])
            my_wxid = (self.config.get("weflow") or {}).get("myWxid")
            if my_wxid:
                bot_wxids.append(my_wxid)
            return LongTermMemory({"memory": memory_config, "bot_wxids": bot_wxids})
        except Exception as exc:
            print(f"[WORKER MEMORY INIT ERROR] {exc}")
            return None

    def _is_bot_wxid(self, wxid):
        from llm.identity import bot_identity
        own = (self.config.get("weflow") or {}).get("myWxid")
        configured = ((self.config or {}).get("llm") or {}).get("bot_wxids") or []
        if isinstance(configured, str): configured = [configured]
        wxids, _ = bot_identity({"bot_wxids": list(configured) + ([own] if own else [])})
        return bool(wxid and str(wxid).strip() in wxids)

    def _listen_group_message(self, msg):
        if self.memory_manager is None and self.long_term_memory is None:
            return

        try:
            info = self.router.inspect_message(msg)
        except Exception:
            return

        sender_wxid = info.get("wxid") or msg.get("wxid")
        is_bot_echo = bool(info.get("is_bot") or self._is_bot_wxid(sender_wxid))
        if is_bot_echo:
            info["source_kind"] = "bot_echo_external"
            info["source_origin"] = "wechat_echo"
            info["learnable"] = False
            # Echoes remain available for diagnostics but never become prompt evidence.
            info["prompt_visible"] = False

        if not info.get("is_target_group"):
            return

        # Media messages are still part of the conversation context even when
        # strict prefix mode would not dispatch them.  In particular, an image
        # must retain its real WeFlow local_id/server_id so a later direct
        # question can invoke the image lookup tool.
        is_picture = bool(
            info.get("is_picture")
            or str(info.get("media_type") or info.get("mediaType") or "").lower() == "image"
            or info.get("local_type") == 3
            or info.get("localType") == 3
            or str(info.get("content") or "").strip().lower() in {"[\u56fe\u7247]", "[image]"}
        )
        if getattr(self.router, "prefix_mode", "strict") == "strict" and not info.get("has_prefix") and not is_picture:
            return

        content = str(info.get("content") or "").strip()
        if not content and is_picture:
            content = "[\u56fe\u7247]"
        if not content:
            return

        # Ordinary placeholders (voice/emoji/etc.) remain excluded from
        # learning.  Images are the exception: they are stored as context,
        # but never treated as text-learning evidence below.
        if content.startswith("[") and content.endswith("]") and not is_picture:
            return

        group_id = info.get("group")
        nickname = info.get("user", "未知用户")
        if not group_id:
            return

        if self.memory_manager:
            self.memory_manager.add_group_message(
                group_id=group_id,
                nickname=nickname,
                content=content,
                message_id=info.get("message_id"),
                local_id=info.get("local_id"),
                server_id=info.get("server_id"),
                session_id=info.get("sessionId") or msg.get("sessionId"),
                prefix_used=bool(info.get("has_prefix")),
                is_at_bot=bool(info.get("is_at") or info.get("is_at_bot")),
                is_mentioned=bool(info.get("is_mentioned")),
                message_type=info.get("message_type") or msg.get("type"),
                wxid=info.get("wxid") or msg.get("wxid"),
                subject_id=info.get("wxid") or msg.get("wxid"),
                directed_to_bot=bool(info.get("directed_to_bot")),
                directed_to_other=bool(info.get("directed_to_other")),
                reply_target=info.get("reply_target") or "",
                address_confidence=info.get("address_confidence", 0.0),
                reply_to_bot=bool(info.get("reply_to_bot")),
                reply_to_other=bool(info.get("reply_to_other")),
                mentions=info.get("mentions") or [],
                quoted_message=info.get("quoted_message"),
                quoted_message_id=info.get("quoted_message_id"),
                quoted_sender=info.get("quoted_sender"),
                quoted_sender_wxid=info.get("quoted_sender_wxid"),
                quoted_content=info.get("quoted_content"),
                quoted_message_type=info.get("quoted_message_type"),
                quoted_timestamp=info.get("quoted_timestamp"),
                quoted_available=info.get("quoted_available"),
                quoted_role=info.get("quoted_role"),
                quoted_is_bot=info.get("quoted_is_bot"),
            )
        if self.long_term_memory and not is_picture:
            sender_wxid = info.get("wxid") or msg.get("wxid") or ""
            self.long_term_memory.record_message({
                "group": group_id,
                "sessionId": info.get("sessionId") or msg.get("sessionId"),
                "wxid": sender_wxid,
                "user": nickname,
                "content": content,
                "messageKey": info.get("message_id"),
                "serverId": info.get("server_id"),
                "prefix_used": bool(info.get("has_prefix")),
                "is_bot": bool(info.get("is_bot") or self._is_bot_wxid(sender_wxid)),
                "source_kind": info.get("source_kind") or "user_incoming",
                "source_origin": info.get("source_origin") or "wechat_inbound",
                "learnable": info.get("learnable", True),
                "prompt_visible": info.get("prompt_visible", True),
            })
        if self.style_learner and not is_picture:
            self.style_learner.record_message({
                "group": group_id,
                "sessionId": info.get("sessionId") or msg.get("sessionId"),
                "user": nickname,
                "wxid": info.get("wxid") or msg.get("wxid"),
                "content": content,
                "messageKey": info.get("message_id"),
                "localId": info.get("local_id"),
                "serverId": info.get("server_id"),
                "replyToMessageId": info.get("quoted_message_id") or msg.get("replyToMessageId"),
                "quoted_message": info.get("quoted_message"),
                "quoted_message_id": info.get("quoted_message_id"),
                "quoted_sender": info.get("quoted_sender"),
                "quoted_sender_wxid": info.get("quoted_sender_wxid"),
                "quoted_content": info.get("quoted_content"),
                "quoted_message_type": info.get("quoted_message_type"),
                "quoted_timestamp": info.get("quoted_timestamp"),
                "quoted_available": info.get("quoted_available"),
                "quoted_role": info.get("quoted_role"),
                "quoted_is_bot": info.get("quoted_is_bot"),
                "source_kind": info.get("source_kind") or "user_incoming",
                "source_origin": info.get("source_origin") or "wechat_inbound",
                "learnable": info.get("learnable", True),
                "prompt_visible": info.get("prompt_visible", True),
                "is_picture": msg.get("is_picture", False),
                "is_emoji": msg.get("is_emoji", False),
                "is_voice": msg.get("is_voice", False),
                "raw": msg,
                "is_bot": bool(
                    (info.get("wxid") or msg.get("wxid"))
                    and self._is_bot_wxid(info.get("wxid") or msg.get("wxid"))
                ),
            })

    @staticmethod
    def _send_succeeded(outcome):
        if isinstance(outcome, tuple):
            return bool(outcome and outcome[0]), (outcome[1] if len(outcome) > 1 else "")
        return bool(outcome), ""

    def _send_structured_result(self, target, result):
        messages = result.get("messages") or []
        animation = result.get("animation")
        delay_seconds = result.get("delay_seconds")
        first_send = True
        sent_messages = []
        animation_sent = False

        try:
            for item in messages:
                content = str(item or "").strip()
                if not content:
                    continue

                ok, reason = self._send_succeeded(send(
                    target=target,
                    content=content,
                    mode="wechat_text",
                    delay_seconds=delay_seconds if first_send else 0
                ))
                if ok:
                    sent_messages.append(content)
                    first_send = False
                else:
                    print(f"[WORKER SEND FAILED] target={target} reason={reason}", flush=True)

            if animation:
                file_path = get_emoji_path(animation)
                if not file_path:
                    print(f"[WORKER] animation 未找到: {animation}", flush=True)
                else:
                    ok, reason = self._send_succeeded(send(
                        target=target,
                        file_path=file_path,
                        mode="wechat_file",
                        delay_seconds=delay_seconds if first_send else 0
                    ))
                    if ok:
                        animation_sent = True
                    else:
                        print(f"[WORKER SEND FAILED] target={target} reason={reason}", flush=True)

        finally:
            confirm = getattr(self.dispatcher, "confirm_llm_delivery", None)
            if callable(confirm):
                try:
                    confirm(result, sent_messages, animation_sent=animation_sent)
                except Exception as exc:
                    print(f"[WORKER DELIVERY CONFIRM ERROR] {exc}", flush=True)
        return bool(sent_messages or animation_sent)

        # =========================================================
        # 🚀 启动线程池
        # =========================================================
    def start(self, n=4):
        print(f"[WORKER] 启动 {n} 个线程")

        for i in range(max(1, int(n or 1))):
            t = threading.Thread(
                target=self.run,
                daemon=True,
                name=f"wechat-worker-{i}",
            )
            t.start()
            self._threads.append(t)
            print(f"[WORKER] 线程 {i} 已启动")

    def pause(self):
        """Pause message consumption without terminating worker threads."""
        self._pause_event.set()

    def resume(self):
        """Resume message consumption after a WebUI pause."""
        self._pause_event.clear()

    def is_paused(self):
        return self._pause_event.is_set()

    def shutdown(self):
        self._stop_event.set()
        for thread in self._threads:
            if thread.is_alive():
                thread.join(timeout=2.0)
