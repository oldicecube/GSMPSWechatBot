import threading
import sys
import os
import queue as queue_lib

from core.sender import preview_delay_seconds, send
from llm.memory import MemoryManager
from llm.security import get_emoji_path

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)


class Worker:
    def __init__(self, queue, router, dispatcher, config=None):
        self.queue = queue
        self.router = router
        self.dispatcher = dispatcher
        self.config = config or {}
        self.memory_manager = self._build_memory_manager()

    # =========================================================
    # 🚀 主线程
    # =========================================================
    def run(self):
        print("[WORKER] 线程启动")

        while True:
            try:
                msg = self.queue.get(timeout=1)
            except queue_lib.Empty:
                continue

            print("[WORKER] 收到任务")
            self._listen_group_message(msg)

            parsed = self.router.parse(msg)

            if not parsed:
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
                context = {
                    "user": item.get("user", "未知用户"),
                    "group": msg.get("group"),
                    "sessionId": msg.get("sessionId"),
                    "wxid": item.get("wxid") or msg.get("wxid"),
                    "type": msg.get("type"),
                    "command": command,
                    "args": args,
                    "content": item.get("content"),
                    "raw": msg,
                    "auto_target": item.get("auto_target"),
                    "followup_payload": item.get("followup_payload") or {},
                    "prefix_used": bool(item.get("prefix_used")),
                    "planned_send_delay_seconds": preview_delay_seconds("wechat_text")
                }

                # =========================
                # ⚙️ 执行 dispatcher
                # =========================
                result = self.dispatcher.dispatch(command, args, context)

                print("[WORKER DEBUG] raw result =", result)

                if not result:
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
                        continue

                    content = result.get("content")

                    if content is None or content == "":
                        print("[WORKER] content 为空，丢弃")
                        continue

                    print(f"[WORKER] 发送结果: target={target}, content={content}")

                    send(
                        target=target,
                        content=content,
                        mode=mode,
                        delay_seconds=delay_seconds
                    )
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
                    continue

                # =========================================================
                # 🧠 3. 其他类型直接丢弃
                # =========================================================
                print("[WORKER] unknown result type:", type(result))

    def _build_memory_manager(self):
        try:
            if not isinstance(self.config.get("llm"), dict):
                return None
            return MemoryManager()
        except Exception:
            return None

    def _listen_group_message(self, msg):
        if self.memory_manager is None:
            return

        try:
            info = self.router.inspect_message(msg)
        except Exception:
            return

        if not info.get("is_target_group"):
            return

        content = str(info.get("content") or "").strip()
        if not content:
            return

        if info.get("has_prefix"):
            return

        if info.get("is_command_like"):
            return

        if content.startswith("[") and content.endswith("]"):
            return

        group_id = info.get("group")
        nickname = info.get("user", "未知用户")
        if not group_id:
            return

        self.memory_manager.add_group_message(
            group_id=group_id,
            nickname=nickname,
            content=content
        )

    def _send_structured_result(self, target, result):
        messages = result.get("messages") or []
        animation = result.get("animation")
        delay_seconds = result.get("delay_seconds")
        first_send = True

        for item in messages:
            content = str(item or "").strip()
            if not content:
                continue

            send(
                target=target,
                content=content,
                mode="wechat_text",
                delay_seconds=delay_seconds if first_send else 0
            )
            first_send = False

        if not animation:
            return

        file_path = get_emoji_path(animation)
        if not file_path:
            print(f"[WORKER] animation 未找到: {animation}")
            return

        send(
            target=target,
            file_path=file_path,
            mode="wechat_file",
            delay_seconds=delay_seconds if first_send else 0
        )

    # =========================================================
    # 🚀 启动线程池
    # =========================================================
    def start(self, n=4):
        print(f"[WORKER] 启动 {n} 个线程")

        for i in range(n):
            t = threading.Thread(target=self.run, daemon=True)
            t.start()
            print(f"[WORKER] 线程 {i} 已启动")
