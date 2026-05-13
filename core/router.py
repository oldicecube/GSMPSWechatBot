import re
import json
import os
import time
import threading
from datetime import datetime
from core.auto_registry import get_raw_message_targets
from core.followup import peek as peek_followup
from core.followup import consume as consume_followup


class Router:
    def __init__(self, prefix, target_group, time_slots=None, rate_limit_cfg=None, prefix_mode="only"):
        # 处理prefix，支持单个字符串或数组
        if isinstance(prefix, str):
            self.prefixes = [prefix] if prefix.strip() else []
        elif isinstance(prefix, (list, tuple)):
            self.prefixes = [str(p).strip() for p in prefix if str(p).strip()]
        else:
            self.prefixes = []

        self.prefix_mode = self._normalize_prefix_mode(prefix_mode)
        
        self.target_groups = self._normalize_target_groups(target_group)
        self.blacklist_file = "data/blacklist.json"
        
        # 时间段和速率限制
        self.time_slots = time_slots or []
        self.rate_limit_cfg = rate_limit_cfg or {}
        
        # wxid速率限制计数器：{wxid: [timestamp, ...]}
        self.rate_limit_tracking = {}
        # 全局速率限制计数器：[timestamp, ...]
        self.global_rate_limit_tracking = []
        self.rate_limit_lock = threading.Lock()

    # =========================================================
    # 🚦 解析入口
    # =========================================================
    def parse(self, msg):

        # =========================
        # ⓪ 黑名单检测（优先级最高）
        # 若 wxid 在 blacklist.json 中，直接拦截
        # =========================
        wxid = msg.get("wxid")

        if self._is_blacklisted(wxid):
            return None

        # =========================
        # ① 时间段检测
        # =========================
        if not self._is_in_time_slot():
            return None

        # =========================
        # ② 群过滤
        # =========================
        if not self._is_target_group(msg.get("group")):
            return None

        raw_content = msg.get("content", "")
        content = self._clean(raw_content)

        user = msg.get("user", "未知用户")
        group = msg.get("group")
        session_id = msg.get("sessionId")

        # 对于已进入 follow-up 等待态的会话，放行下一条跟随消息，
        # 但只交给登记的目标插件处理，不恢复普通无前缀消息。
        # 特殊处理：如果是命令消息，先释放follow-up，再让后续逻辑处理命令
        followup = peek_followup(session_id, wxid=wxid)
        if followup:
            # 检查是否为命令消息（cleaned content 以/开头）
            is_cmd = content.strip().startswith("/")
            
            if is_cmd:
                # 命令消息：先释放followup，然后继续处理这条命令
                consume_followup(session_id, wxid=wxid)
                print(f"[ROUTER] 检测到待处理命令，先释放pending的followup")
                # 不返回，继续往下处理命令
            else:
                # 非命令消息：正常进入followup处理
                return {
                    "command": None,
                    "args": None,
                    "user": user,
                    "group": group,
                    "sessionId": session_id,
                    "content": content,
                    "type": "auto",
                    "raw": msg,
                    "auto_target": followup.get("target"),
                    "followup_payload": followup.get("payload") or {},
                    "wxid": wxid,
                    "prefix_used": False
                }

        raw_targets = get_raw_message_targets()
        has_prefix = self._has_prefix(content)
        after_prefix, used_prefix = self._resolve_routing_content(content)

        if raw_targets and self.prefix_mode == "only" and not has_prefix:
            # =========================
            # ③ 速率限制检测
            # =========================
            if not self._check_rate_limit(wxid):
                return None
                
            return {
                "command": None,
                "args": None,
                "user": user,
                "group": group,
                "sessionId": session_id,
                "content": content,
                "type": "auto",
                "raw": msg,
                "auto_target": raw_targets,
                "wxid": wxid,
                "prefix_used": False
            }

        if after_prefix is None:
            return None

        # =========================
        # ③ 速率限制检测
        # =========================
        if not self._check_rate_limit(wxid):
            return None

        # =========================
        # 🚨 AUTO 事件
        # =========================
        auto_event = {
            "command": None,
            "args": None,
            "user": user,
            "group": group,
            "sessionId": session_id,
            "content": after_prefix,
            "type": "auto",
            "raw": msg,
            "wxid": wxid,
            "prefix_used": used_prefix
        }

        # =========================
        # ② 只有 prefix 或 prefix + 空格
        # → 执行 /default_status
        # =========================
        if used_prefix and not after_prefix:
            return {
                "command": "/default_status",
                "args": "",
                "user": user,
                "group": group,
                "sessionId": session_id,
                "content": "",
                "type": "command",
                "raw": msg,
                "wxid": wxid,
                "prefix_used": used_prefix
            }

        # =========================
        # ④ 命令匹配
        # =========================
        match = re.match(r"(/[a-zA-Z0-9_]+)(?:\s+([\s\S]*))?$", after_prefix)

        if not match:
            return auto_event

        # =========================
        # ⑤ 命令解析
        # =========================
        command = match.group(1)
        after = (match.group(2) or "").strip()

        return {
            "command": command,
            "args": after,
            "user": user,
            "group": group,
            "sessionId": session_id,
            "content": after,
            "type": "command",
            "raw": msg,
            "wxid": wxid,
            "prefix_used": used_prefix
        }

    def inspect_message(self, msg):
        raw_content = msg.get("content", "")
        content = self._clean(raw_content)

        return {
            "group": msg.get("group"),
            "user": msg.get("user", "未知用户"),
            "wxid": msg.get("wxid"),
            "content": content,
            "is_target_group": self._is_target_group(msg.get("group")),
            "has_prefix": self._has_prefix(content),
            "is_command_like": content.strip().startswith("/")
        }

    # =========================================================
    # 🚫 黑名单检测
    # data/blacklist.json 示例：
    # {
    #   "wxid_xxx": true,
    #   "wxid_yyy": true
    # }
    # =========================================================
    def _is_blacklisted(self, wxid):

        if not wxid:
            return False

        if not os.path.exists(self.blacklist_file):
            return False

        try:
            with open(self.blacklist_file, "r", encoding="utf-8") as f:
                data = json.load(f)

            return wxid in data

        except:
            return False

    # =========================================================
    # 🧼 文本清洗
    # =========================================================
    def _clean(self, content: str):

        if not isinstance(content, str):
            return ""

        # 去微信前缀
        content = re.sub(r"wxid_.*?:", "", content)

        # 空白字符统一
        content = content.replace("\u2005", " ")
        content = content.replace("\u200b", " ")
        content = content.replace("　", " ")

        return content.strip()

    def _normalize_target_groups(self, target_group):
        if isinstance(target_group, str):
            group = target_group.strip()
            return {group} if group else set()

        if isinstance(target_group, (list, tuple, set)):
            return {
                str(group).strip()
                for group in target_group
                if str(group).strip()
            }

        return set()

    def _normalize_prefix_mode(self, prefix_mode):
        mode = str(prefix_mode or "only").strip().lower()

        alias_map = {
            "only": "only",
            "仅": "only",
            "on": "only",
            "strict": "only",
            "off": "off",
            "关闭": "off",
            "disable": "off",
            "disabled": "off",
            "none": "off",
            "mixed": "mixed",
            "混合": "mixed",
            "both": "mixed"
        }

        normalized = alias_map.get(mode, "only")
        print(f"[ROUTER] prefix_mode = {normalized}")
        return normalized

    def _is_target_group(self, group):
        if not self.target_groups:
            return False

        if not isinstance(group, str):
            return False

        return group.strip() in self.target_groups

    # =========================================================
    # ⏰ 时间段检测
    # =========================================================
    def _is_in_time_slot(self):
        """检查当前时间是否在配置的时间段内"""
        if not self.time_slots:
            return True  # 如果没有配置时间段，则始终允许

        now = datetime.now().time()
        now_minutes = now.hour * 60 + now.minute

        for slot in self.time_slots:
            start_str = slot.get("start", "00:00")
            end_str = slot.get("end", "23:59")

            try:
                start_time = datetime.strptime(start_str, "%H:%M").time()
                end_time = datetime.strptime(end_str, "%H:%M").time()

                start_minutes = start_time.hour * 60 + start_time.minute
                end_minutes = end_time.hour * 60 + end_time.minute

                # 跨天处理
                if start_minutes <= end_minutes:
                    if start_minutes <= now_minutes <= end_minutes:
                        return True
                else:
                    # 跨午夜
                    if now_minutes >= start_minutes or now_minutes <= end_minutes:
                        return True
            except Exception as e:
                print(f"[ROUTER] 时间段解析错误: {e}")
                continue

        return False

    # =========================================================
    # 💨 速率限制检测
    # =========================================================
    def _check_rate_limit(self, wxid):
        """检查该wxid是否超过每分钟消息限制，同时检查全局限制"""
        if not wxid:
            return True

        per_user_limit = self.rate_limit_cfg.get("messages_per_minute")
        global_limit = self.rate_limit_cfg.get("global_messages_per_minute")

        with self.rate_limit_lock:
            current_time = time.time()
            one_minute_ago = current_time - 60

            # =========== 检查全局速率限制 ===========
            if global_limit and global_limit > 0:
                # 清理一分钟外的记录
                self.global_rate_limit_tracking = [
                    ts for ts in self.global_rate_limit_tracking
                    if ts > one_minute_ago
                ]

                # 检查是否超过全局限制
                if len(self.global_rate_limit_tracking) >= global_limit:
                    print(f"[ROUTER] 全局消息超过速率限制 ({global_limit}/min)")
                    return False

            # =========== 检查个人速率限制 ===========
            if not per_user_limit or per_user_limit <= 0:
                # 如果没有个人限制但有全局限制，记录并通过
                if global_limit and global_limit > 0:
                    self.global_rate_limit_tracking.append(current_time)
                return True

            # 初始化或清理旧记录
            if wxid not in self.rate_limit_tracking:
                self.rate_limit_tracking[wxid] = []

            # 清理一分钟外的记录
            self.rate_limit_tracking[wxid] = [
                ts for ts in self.rate_limit_tracking[wxid]
                if ts > one_minute_ago
            ]

            # 检查是否超过个人限制
            if len(self.rate_limit_tracking[wxid]) >= per_user_limit:
                print(f"[ROUTER] {wxid} 超过速率限制 ({per_user_limit}/min)")
                return False

            # 同时记录个人消息和全局消息
            self.rate_limit_tracking[wxid].append(current_time)
            if global_limit and global_limit > 0:
                self.global_rate_limit_tracking.append(current_time)
            return True

    # =========================================================
    # 🎯 检查是否有前缀
    # =========================================================
    def _has_prefix(self, content):
        """检查内容是否以任何配置的前缀开头"""
        for prefix in self.prefixes:
            if content == prefix or content.startswith(f"{prefix} "):
                return True
        return False

    def _resolve_routing_content(self, content):
        """根据 prefix_mode 决定是否需要命中前缀，以及最终参与路由的内容"""
        after_prefix = self._extract_after_prefix(content)

        if self.prefix_mode == "only":
            if after_prefix is None:
                return None, False
            return after_prefix, True

        if after_prefix is not None:
            return after_prefix, True

        return content, False

    # =========================================================
    # 📝 提取前缀后的内容
    # =========================================================
    def _extract_after_prefix(self, content):
        """提取前缀后的内容，如果没有前缀则返回None"""
        for prefix in self.prefixes:
            if content == prefix:
                return ""
            elif content.startswith(f"{prefix} "):
                return content[len(prefix) + 1:].strip()
        return None
