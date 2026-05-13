import requests
import json
import time
import re


class WeFlowClient:
    def __init__(self, token, queue, dedup):
        self.url = f"http://127.0.0.1:5031/api/v1/push/messages?access_token={token}"
        self.queue = queue
        self.dedup = dedup

    # =========================
    # 🧠 提取发送者 wxid
    # 来源：
    # 1. content 第一行 wxid_xxx:
    # 2. messageKey 倒数第二段
    # =========================
    def get_wxid(self, msg: dict):
        content = msg.get("content", "")

        # ① content 第一行提取
        if content:
            first_line = content.split("\n")[0].strip()
            m = re.match(r"^(wxid_[^:]+):?$", first_line)
            if m:
                return m.group(1)

        # ② messageKey 提取
        mk = msg.get("messageKey", "")
        if mk:
            parts = mk.split(":")
            if len(parts) >= 2:
                maybe = parts[-2]
                if maybe.startswith("wxid_"):
                    return maybe

        return None

    # =========================
    # 🧠 提取纯文本内容（去掉首行 wxid）
    # =========================
    def get_real_content(self, msg: dict):
        content = msg.get("content", "")

        lines = content.split("\n")

        if len(lines) >= 2 and lines[0].startswith("wxid_"):
            return "\n".join(lines[1:]).strip()

        return content

    # =========================
    # 🧠 判断是否群消息
    # =========================
    def is_group(self, msg: dict):
        return msg.get("sessionType") == "group"

    # =========================
    # 🧠 判断是否私聊
    # =========================
    def is_private(self, msg: dict):
        return msg.get("sessionType") == "private"

    # =========================
    # 🧠 消息协议层（关键）
    # =========================
    def _normalize_msg(self, msg: dict) -> dict:
        return {
            # ===== 标准字段（插件只用这些）=====
            "user": msg.get("sourceName") or msg.get("sender") or "未知用户",
            "content": msg.get("content", ""),
            "group": msg.get("groupName"),
            "sessionId": msg.get("sessionId"),
            "type": msg.get("sessionType"),

            # ===== 新增实用字段 =====
            "wxid": self.get_wxid(msg),                    # 发送者微信ID
            "text": self.get_real_content(msg),           # 清洗后的正文内容
            "is_group": self.is_group(msg),               # 是否群聊
            "is_private": self.is_private(msg),           # 是否私聊

            # ===== 原始JSON字段直通 =====
            "event": msg.get("event"),
            "sourceName": msg.get("sourceName"),
            "groupName": msg.get("groupName"),
            "sessionType": msg.get("sessionType"),

            # ===== 扩展字段 =====
            "messageKey": msg.get("messageKey"),
            "avatarUrl": msg.get("avatarUrl"),

            # ===== debug =====
            "_ts": time.time(),
            "_raw": msg
        }

    def start(self):
        print("[SSE] 客户端启动")

        while True:
            try:
                print(f"[SSE] 连接: {self.url}")

                with requests.get(
                    self.url,
                    stream=True,
                    timeout=60,
                    headers={
                        "Accept": "text/event-stream",
                        "Cache-Control": "no-cache"
                    }
                ) as r:

                    if r.status_code != 200:
                        print("[SSE] 非200，5秒重连")
                        time.sleep(5)
                        continue

                    for line in r.iter_lines():
                        if not line:
                            continue

                        decoded = line.decode(errors="ignore").strip()

                        if not decoded.startswith("data:"):
                            continue

                        try:
                            msg = json.loads(decoded[5:].strip())
                        except:
                            continue

                        if not msg.get("messageKey"):
                            continue

                        key = msg["messageKey"]
                        if self.dedup.exists(key):
                            continue
                        self.dedup.add(key)

                        # =========================
                        # 🧠 统一结构
                        # =========================
                        msg = self._normalize_msg(msg)

                        print(
                            f"[QUEUE] 入队 | "
                            f"group={msg['group']} "
                            f"user={msg['user']} "
                            f"content={msg['content']}"
                        )

                        self.queue.put(msg)

            except Exception as e:
                print("[SSE ERROR]", e)
                time.sleep(5)