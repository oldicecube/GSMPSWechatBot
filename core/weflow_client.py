import requests
import json
import time
import re


class WeFlowClient:
    def __init__(self, token, queue, dedup):
        self.api_base = "http://127.0.0.1:5031"
        self.api_token = str(token or "").strip()
        self.url = f"{self.api_base}/api/v1/push/messages?access_token={self.api_token}"
        self.queue = queue
        self.dedup = dedup

    def get_original_message(self, session_id, local_id=None, server_id=None,
                             timeout=8):
        """Fetch one raw WeFlow message for LLM context hydration."""
        session_id = str(session_id or "").strip()
        if not session_id:
            return {"ok": False, "error": "missing session_id"}

        params = {"talker": session_id}
        if local_id is not None:
            try:
                parsed_local_id = int(local_id)
            except (TypeError, ValueError):
                parsed_local_id = 0
            if parsed_local_id > 0:
                params["localId"] = parsed_local_id
        if not params.get("localId") and str(server_id or "").strip():
            params["serverId"] = str(server_id).strip()
        if len(params) == 1:
            return {"ok": False, "error": "missing local_id or server_id"}

        try:
            response = requests.get(
                f"{self.api_base}/api/v1/messages/original",
                params=params,
                headers={"Authorization": f"Bearer {self.api_token}"},
                timeout=timeout,
            )
            if response.status_code == 404:
                return {"ok": False, "error": "original message not found"}
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                return {"ok": False, "error": "invalid WeFlow response"}
            return {"ok": True, "message": data.get("message"), "lookup": data.get("lookup")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}

    # =========================
    # 🧠 提取发送者 wxid
    # 来源：
    # 1. content 第一行 wxid_xxx:
    # 2. messageKey 倒数第二段
    # =========================
    def get_wxid(self, msg: dict):
        # SSE 推送直接包含 senderWxid
        sender_wxid = msg.get("senderWxid", "")
        if sender_wxid and sender_wxid.strip():
            return sender_wxid.strip()

        # 兜底：从 content 第一行提取
        content = msg.get("content", "")
        if content:
            first_line = content.split("\n")[0].strip()
            m = re.match(r"^(wxid_[^:]+):?$", first_line)
            if m:
                return m.group(1)

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
            "is_at": bool(msg.get("is_at") or msg.get("isAt") or msg.get("atBot")),
            "is_mentioned": bool(
                msg.get("is_mentioned")
                or msg.get("isMentioned")
                or msg.get("isMention")
            ),

            # ===== 原始JSON字段直通 =====
            "event": msg.get("event"),
            "sourceName": msg.get("sourceName"),
            "groupName": msg.get("groupName"),
            "sessionType": msg.get("sessionType"),

            # ===== 扩展字段 =====
            "messageKey": msg.get("messageKey"),
            "localId": msg.get("localId") or msg.get("local_id"),
            "serverId": msg.get("serverId") or msg.get("server_id"),
            "svrid": msg.get("svrid") or msg.get("rawid") or msg.get("serverId"),
            "rawid": msg.get("rawid"),
            "timestamp": msg.get("timestamp"),
            "localType": msg.get("localType") or msg.get("local_type"),
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

                        # 使用 rawid 去重（SSE 推送事件不含 messageKey）
                        rawid = msg.get("rawid") or msg.get("messageKey")
                        if not rawid:
                            continue

                        if self.dedup.exists(rawid):
                            continue
                        self.dedup.add(rawid)

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
