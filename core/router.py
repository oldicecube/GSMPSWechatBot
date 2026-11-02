import json
import re
import os
import time
import threading
from core.auto_registry import get_raw_message_targets
from core.followup import peek as peek_followup
from core.followup import consume as consume_followup


class Router:
    def __init__(
        self,
        prefix,
        target_group,
        time_slots=None,
        rate_limit_cfg=None,
        prefix_mode="strict",
        proactive_enabled=False,
    ):
        # 处理prefix，支持单个字符串或数组
        if isinstance(prefix, str):
            self.prefixes = [prefix] if prefix.strip() else []
        elif isinstance(prefix, (list, tuple)):
            self.prefixes = [str(p).strip() for p in prefix if str(p).strip()]
        else:
            self.prefixes = []

        self.prefix_mode = self._normalize_prefix_mode(prefix_mode)
        self.proactive_enabled = bool(proactive_enabled)
        
        self.target_groups = self._normalize_target_groups(target_group)
        self.blacklist_file = "data/blacklist.json"
        
        # 时间段和速率限制
        self.time_slots = time_slots or []
        self.rate_limit_cfg = rate_limit_cfg or {}
        
        # Per-category/user counters: {(category, wxid): [timestamp, ...]}
        self.rate_limit_tracking = {}
        # Per-category global counters: {category: [timestamp, ...]}
        self.global_rate_limit_tracking = {}
        self.rate_limit_violation_tracking = {}
        self.auto_blacklist_enabled = bool(
            self.rate_limit_cfg.get("auto_blacklist_enabled", False)
        )
        self.auto_blacklist_threshold = self._safe_int(
            self.rate_limit_cfg.get("auto_blacklist_threshold"),
            default=0
        )
        self.rate_limit_lock = threading.Lock()

    # =========================================================
    # 🚦 解析入口
    # =========================================================
    def parse(self, msg):
        # Source fields survive every parse branch, including follow-up events.
        event = self._parse(msg)
        if event:
            for name in ('platform', 'source_platform', 'account_id', 'scope', 'user_id', 'group_id', 'group_name', 'native_session_id'):
                if name in msg:
                    event[name] = msg[name]
        return event

    def _parse(self, msg):
        wxid = msg.get("wxid")
        if self._is_blacklisted(wxid):
            return None
        # Response hours no longer disable command/raw automation routing.
        # LLM and automatic reposting enforce the shared window separately.
        target = msg.get('group') or (msg.get('sessionId') if msg.get('platform') == 'qq' else '')
        if not (self._is_target_group(target) or self._is_target_group(msg.get('group_id') or msg.get('sessionId'))):
            return None

        content = self._clean(msg.get("content", ""))
        user = msg.get("user", "未知用户")
        group = msg.get("group")
        session_id = msg.get("sessionId")
        has_prefix = self._has_prefix(content)

        followup = peek_followup(session_id, wxid=wxid)
        if followup:
            if content.strip().startswith("/"):
                consume_followup(session_id, wxid=wxid)
            else:
                if not self._check_rate_limit(wxid, "messages"):
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
                    "auto_target": followup.get("target"),
                    "followup_payload": followup.get("payload") or {},
                    "wxid": wxid,
                    "prefix_used": False,
                }

        address = self._addressing_metadata(msg, content, has_prefix=has_prefix)
        directed_to_bot = bool(address.get("directed_to_bot") or address.get("reply_to_bot"))

        if self.prefix_mode == "strict" and not has_prefix:
            if not directed_to_bot:
                raw_targets = get_raw_message_targets()
                if raw_targets:
                    return {"command": None, "args": None, "user": user, "group": group,
                            "sessionId": session_id, "content": content, "type": "raw_auto",
                            "raw": msg, "wxid": wxid, "prefix_used": False,
                            "proactive_candidate": False, "auto_target": raw_targets}
                return None
            if not self._check_rate_limit(wxid, "llm"):
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
                "wxid": wxid,
                "prefix_used": True,
                "proactive_candidate": False,
            }

        raw_targets = get_raw_message_targets()
        after_prefix, used_prefix = self._resolve_routing_content(content)
        if after_prefix is None:
            # Raw auto plugins (for example /sleep) need to observe ordinary
            # messages even in strict prefix mode. They are explicitly
            # allowlisted by MATCH_RAW_MESSAGE and receive only this message;
            # normal LLM/command routing remains strict and unchanged.
            if raw_targets:
                return {
                    "command": None,
                    "args": None,
                    "user": user,
                    "group": group,
                    "sessionId": session_id,
                    "content": content,
                    "type": "raw_auto",
                    "raw": msg,
                    "wxid": wxid,
                    "prefix_used": False,
                    "proactive_candidate": False,
                    "auto_target": raw_targets,
                }
            return None

        routed = str(after_prefix or "").strip()
        if routed.startswith("/"):
            category = "plugin"
        elif used_prefix:
            category = "prefix"
        else:
            category = "llm"
        if not self._check_rate_limit(wxid, category):
            return None

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
            "prefix_used": used_prefix,
            "proactive_candidate": bool(self.proactive_enabled and not used_prefix),
        }

        if used_prefix and not routed:
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
                "prefix_used": used_prefix,
            }

        match = re.match(
            r"(/[a-zA-Z0-9_\u4e00-\u9fff\u3400-\u4dbf]+)(?:\s+([\s\S]*))?$",
            routed,
        )
        if not match:
            return auto_event

        command_args = (match.group(2) or "").strip()
        if match.group(1) == "/emoji":
            original_args = self._emoji_source(msg.get("content", ""), used_prefix)
            if original_args is not None:
                command_args = original_args

        return {
            "command": match.group(1),
            "args": command_args,
            "user": user,
            "group": group,
            "sessionId": session_id,
            "content": command_args,
            "type": "command",
            "raw": msg,
            "wxid": wxid,
            "prefix_used": used_prefix,
        }

    @staticmethod
    def _first_value(mapping, names):
        if not isinstance(mapping, dict):
            return None
        for name in names:
            value = mapping.get(name)
            if value not in (None, "", [], {}):
                return value
        return None

    @classmethod
    def _normalize_quoted_message(cls, value, fallback=None):
        """Normalize WeFlow/reply payloads without mixing quote text into content.

        Providers use several shapes for a reply target.  Keep the quoted record
        separate from the current message so routing and the LLM can distinguish
        the current message from the quoted message. ``available`` is false when
        only an id was supplied and the quoted body still needs to be resolved
        from history.
        """
        fallback = fallback if isinstance(fallback, dict) else {}
        if isinstance(value, dict):
            # Start with flat fallback fields, then let the nested/provider
            # payload override them. This preserves an id supplied beside a
            # partial quoted-message object.
            source = dict(fallback)
            nested = value.get("message") or value.get("reply") or value.get("quoted")
            if isinstance(nested, dict):
                source.update(nested)
            source.update(value)
        elif value not in (None, ""):
            source = {"content": value}
        else:
            source = {}
        if not source and fallback:
            source = dict(fallback)
        if not source:
            return None

        def first(names):
            for name in names:
                item = source.get(name)
                if item not in (None, "", [], {}):
                    return item
            return None

        def text(names):
            item = first(names)
            if isinstance(item, dict):
                item = item.get("name") or item.get("nickname") or item.get("text") or item.get("content")
            return str(item or "").strip()

        message_id = text((
            "message_id", "messageId", "messageKey", "key", "id", "rawid",
        ))
        local_id = text(("local_id", "localId"))
        server_id = text(("server_id", "serverId", "svrid", "rawid"))
        if not message_id:
            message_id = local_id or server_id
        sender = text((
            "sender", "senderName", "sender_nickname", "senderNickname",
            "nickname", "displayName", "display_name", "user", "from", "author",
        ))
        sender_wxid = text((
            "sender_wxid", "senderWxid", "senderWxId", "wxid", "wxId",
            "fromWxid", "from_wxid", "user_id", "userId",
        ))
        content = text((
            "content", "text", "body", "quoteText", "quotedContent",
            "quoted_content", "replyContent", "reply_content", "messageText",
        ))
        message_type = text(("message_type", "messageType", "type", "msgType", "msg_type"))
        timestamp = text(("timestamp", "time", "createdAt", "createTime", "created_at"))
        role = text(("role", "message_role", "messageRole"))
        is_bot_value = first(("is_bot", "isBot", "fromBot", "from_bot", "bot"))
        is_bot = (
            is_bot_value.strip().lower() in {"1", "true", "yes", "on"}
            if isinstance(is_bot_value, str)
            else bool(is_bot_value)
        )
        if role not in {"assistant", "user"}:
            role = "assistant" if is_bot else "user"
        available = bool(content)
        if not any((message_id, local_id, server_id, sender, sender_wxid, content, message_type, timestamp)):
            return None
        return {
            "message_id": message_id,
            "local_id": local_id,
            "server_id": server_id,
            "sender": sender,
            "sender_wxid": sender_wxid,
            "content": content,
            "message_type": message_type,
            "timestamp": timestamp,
            "role": role,
            "is_bot": is_bot,
            "available": available,
        }

    def _extract_quoted_message(self, msg):
        """Read an optional quoted/replied message from all known wire shapes."""
        msg = msg if isinstance(msg, dict) else {}
        raw = msg.get("_raw") if isinstance(msg.get("_raw"), dict) else {}
        merged = dict(raw)
        merged.update({key: value for key, value in msg.items() if key != "_raw"})
        quote_value = self._first_value(merged, (
            "reply_to", "replyTo", "reply_to_message", "replyToMessage",
            "quotedMessage", "quoted_message", "quote", "referencedMessage",
            "referenced_message", "replyMessage", "reply_message",
        ))
        flat = {
            "message_id": self._first_value(merged, (
                "replyToMessageId", "reply_to_message_id", "quotedMessageId",
                "quoted_message_id", "replyMessageId", "reply_message_id",
            )),
            "sender": self._first_value(merged, (
                "replyToUser", "reply_to_user", "quotedUser", "quoted_user",
                "replyTargetName", "reply_target_name", "replySender", "quotedSender",
            )),
            "sender_wxid": self._first_value(merged, (
                "replyToWxid", "reply_to_wxid", "quotedWxid", "quoted_wxid",
                "replyTargetWxid", "reply_target_wxid", "replySenderWxid", "quotedSenderWxid",
            )),
            "content": self._first_value(merged, (
                "replyContent", "reply_content", "quotedContent", "quoted_content",
                "quoteText", "quote_text", "replyText", "reply_text",
            )),
            "message_type": self._first_value(merged, (
                "replyMessageType", "reply_message_type", "quotedMessageType",
                "quoted_message_type",
            )),
            "timestamp": self._first_value(merged, (
                "replyTimestamp", "reply_timestamp", "quotedTimestamp", "quoted_timestamp",
            )),
        }
        normalized = self._normalize_quoted_message(quote_value, flat)
        if normalized is None:
            normalized = self._normalize_quoted_message(None, flat)
        return normalized

    def _addressing_metadata(self, msg, content, has_prefix=None):
        """Extract conservative message direction hints for local gates.

        These are hints, not a replacement for LLM context judgment.  In
        particular, a bare ``你`` or a bot name in quoted text is never treated
        as a direct address.  Unknown reply/mention metadata remains uncertain.
        """
        msg = msg if isinstance(msg, dict) else {}
        raw = msg.get("_raw") if isinstance(msg.get("_raw"), dict) else {}
        merged = dict(raw)
        merged.update({key: value for key, value in msg.items() if key != "_raw"})
        has_prefix = bool(self._has_prefix(content) if has_prefix is None else has_prefix)
        bot_ids = {
            str(value).strip()
            for value in (
                merged.get("bot_wxid"), merged.get("botWxid"), merged.get("myWxid"),
                merged.get("self_wxid"), merged.get("selfWxid"),
            )
            if str(value or "").strip()
        }
        # WeFlow may represent the same bot name inconsistently: a textual
        # prefix can include the leading @ while mention/reply metadata usually
        # contains only the display name. Keep both forms as bot aliases, but
        # do not infer a bot address from ordinary text.
        bot_names = set()
        for item in self.prefixes:
            alias = str(item or "").strip().casefold()
            if not alias:
                continue
            bot_names.add(alias)
            bot_names.add(alias.lstrip("@"))

        def collect_mentions(value):
            rows = value if isinstance(value, (list, tuple, set)) else [value]
            ids, names = set(), set()
            for row in rows:
                if isinstance(row, dict):
                    for key in ("wxid", "wxId", "user_id", "userId", "id"):
                        if row.get(key): ids.add(str(row[key]).strip())
                    for key in ("nickname", "name", "displayName", "display_name", "user"):
                        if row.get(key): names.add(str(row[key]).strip().casefold())
                elif row not in (None, ""):
                    text = str(row).strip()
                    (ids if text.startswith("wxid_") else names).add(text.casefold())
            return ids, names

        mention_value = self._first_value(merged, (
            "mentions", "mentionList", "mention_list", "atList", "at_list",
            "mentionedMembers", "mentioned_members", "atUsers", "at_users",
        ))
        mention_ids, mention_names = collect_mentions(mention_value)
        explicit_bot = bool(
            merged.get("atBot") or merged.get("at_bot") or merged.get("is_at_bot")
            or merged.get("isAtBot") or merged.get("botMentioned")
        )
        explicit_other = False
        if mention_ids or mention_names:
            explicit_bot = explicit_bot or bool(mention_ids & bot_ids) or bool(mention_names & bot_names)
            explicit_other = bool((mention_ids - bot_ids) or (mention_names - bot_names))

        quoted_message = self._extract_quoted_message(msg)
        reply_value = self._first_value(merged, (
            "reply_to", "replyTo", "reply_to_message", "replyToMessage",
            "quotedMessage", "quoted_message", "quote", "referencedMessage",
            "referenced_message", "replyMessage", "reply_message",
        ))
        reply_ids, reply_names = collect_mentions(reply_value)
        if quoted_message:
            if quoted_message.get("sender_wxid"):
                reply_ids.add(str(quoted_message["sender_wxid"]).strip())
            if quoted_message.get("sender"):
                reply_names.add(str(quoted_message["sender"]).strip().casefold())
        for key in ("replyToWxid", "reply_to_wxid", "quotedWxid", "quoted_wxid", "replyTargetWxid"):
            if merged.get(key): reply_ids.add(str(merged[key]).strip())
        for key in ("replyToUser", "reply_to_user", "quotedUser", "quoted_user", "replyTargetName"):
            if merged.get(key): reply_names.add(str(merged[key]).strip().casefold())
        reply_present = bool(quoted_message or reply_value or reply_ids or reply_names or merged.get("replyToMessageId") or merged.get("reply_to_message_id"))
        quoted_is_bot = bool(quoted_message and quoted_message.get("is_bot"))
        reply_to_bot = quoted_is_bot or bool(reply_ids & bot_ids) or bool(reply_names & bot_names) or bool(merged.get("replyToBot") or merged.get("reply_to_bot"))
        reply_to_other = bool((reply_ids - bot_ids) or (reply_names - bot_names))

        directed_to_bot = bool(
            has_prefix or explicit_bot or reply_to_bot
            or merged.get("is_at_bot") or merged.get("isAtBot")
        )
        directed_to_other = bool((explicit_other or reply_to_other) and not directed_to_bot)
        confidence = 1.0 if directed_to_bot else (0.95 if directed_to_other else (0.35 if reply_present else 0.0))
        return {
            "mentions": sorted(mention_ids | mention_names),
            "reply_target": str(
                merged.get("replyToMessageId") or merged.get("reply_to_message_id")
                or merged.get("messageKey") or ""
            ).strip(),
            "directed_to_bot": directed_to_bot,
            "directed_to_other": directed_to_other,
            "address_confidence": confidence,
            "reply_to_bot": reply_to_bot,
            "reply_to_other": reply_to_other,
            "quoted_message": quoted_message,
            "quoted_message_id": (quoted_message or {}).get("message_id", ""),
            "quoted_sender": (quoted_message or {}).get("sender", ""),
            "quoted_sender_wxid": (quoted_message or {}).get("sender_wxid", ""),
            "quoted_content": (quoted_message or {}).get("content", ""),
            "quoted_message_type": (quoted_message or {}).get("message_type", ""),
            "quoted_timestamp": (quoted_message or {}).get("timestamp", ""),
            "quoted_available": bool((quoted_message or {}).get("available")),
            "quoted_role": (quoted_message or {}).get("role", ""),
            "quoted_is_bot": bool((quoted_message or {}).get("is_bot")),
        }

    def inspect_message(self, msg):
        msg = msg if isinstance(msg, dict) else {}
        raw_content = msg.get("content", "")
        content = self._clean(raw_content)
        address = self._addressing_metadata(msg, content)
        local_type = msg.get("localType", msg.get("local_type", msg.get("msgType", msg.get("msg_type"))))
        try:
            local_type = int(local_type) if local_type not in (None, "") else 0
        except (TypeError, ValueError):
            local_type = 0
        media_type = str(msg.get("mediaType", msg.get("media_type", "")) or "").lower()
        is_picture = bool(
            msg.get("is_picture") or msg.get("isPicture")
            or local_type == 3
            or media_type == "image"
            or media_type.startswith("image/")
            or content.strip().lower() in {"[\u56fe\u7247]", "[image]"}
        )
        return {
            "group": msg.get("group"),
            "platform": msg.get("platform") or msg.get("source_platform") or "wechat",
            "account_id": msg.get("account_id") or "",
            "scope": msg.get("scope") or "",
            "sessionId": msg.get("sessionId"),
            "user": msg.get("user", "未知用户"),
            "wxid": msg.get("wxid"),
            "content": content,
            "message_id": msg.get("messageKey") or msg.get("rawid"),
            "local_id": msg.get("localId") or msg.get("local_id"),
            "server_id": msg.get("serverId") or msg.get("server_id") or msg.get("svrid") or msg.get("rawid"),
            "local_type": local_type,
            "media_type": media_type,
            "media_url": msg.get("mediaUrl") or msg.get("media_url"),
            "message_type": msg.get("message_type") or msg.get("messageType") or ("image" if is_picture else None),
            "is_picture": is_picture,
            "source_kind": msg.get("source_kind") or ("system_event" if local_type in {10000,10002} else "user_incoming"),
            "source_origin": msg.get("source_origin") or "wechat_inbound",
            "learnable": msg.get("learnable", local_type not in {10000,10002}),
            "prompt_visible": msg.get("prompt_visible", local_type not in {10000,10002}),
            "is_emoji": bool(msg.get("is_emoji") or msg.get("isEmoji") or local_type == 47),
            "is_voice": bool(msg.get("is_voice") or msg.get("isVoice") or local_type == 34),
            "is_target_group": (self._is_target_group(msg.get("group"))
                                or self._is_target_group(msg.get('group_id') or msg.get('sessionId'))),
            "has_prefix": self._has_prefix(content),
            "is_command_like": content.strip().startswith("/"),
            "is_at": bool(msg.get("is_at") or msg.get("isAt")),
            "is_at_bot": bool(msg.get("is_at_bot") or msg.get("isAtBot") or msg.get("atBot")),
            "is_mentioned": bool(msg.get("is_mentioned") or msg.get("isMentioned")),
            **address,
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
        try:
            with open(self.blacklist_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return wxid in data
        except Exception:
            return False

    def _add_to_blacklist(self, wxid):
        if not wxid:
            return False
        try:
            with open(self.blacklist_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

        if wxid in data:
            return True

        data[wxid] = True
        try:
            with open(self.blacklist_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            return False

        print(f"[ROUTER] auto blacklisted wxid: {wxid}")
        return True

    def _record_rate_limit_violation(self, wxid):

        if not wxid:
            return

        if not self.auto_blacklist_enabled or self.auto_blacklist_threshold <= 0:
            return

        violations = self.rate_limit_violation_tracking.get(wxid, 0) + 1
        self.rate_limit_violation_tracking[wxid] = violations

        if violations < self.auto_blacklist_threshold:
            print(
                f"[ROUTER] rate limit violation {wxid}: "
                f"{violations}/{self.auto_blacklist_threshold}"
            )
            return

        if self._add_to_blacklist(wxid):
            self.rate_limit_violation_tracking.pop(wxid, None)

    def _safe_int(self, value, default=0):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # =========================================================
    # 🧼 文本清洗
    # =========================================================
    def _emoji_source(self, raw_content, used_prefix):
        """Strip only routing syntax, not original /emoji argument characters.

        One separator after /emoji belongs to the command. Any further spaces
        and line endings belong to the source; preserve even trailing blanks.
        """
        if not isinstance(raw_content, str):
            return None
        text = re.sub(r"^wxid_[^\s:]+:\r?\n", "", raw_content, count=1).lstrip()
        if used_prefix:
            for prefix in self.prefixes:
                if text.startswith(prefix) and (len(text) == len(prefix) or text[len(prefix)].isspace()):
                    text = text[len(prefix):].lstrip()
                    break
            else:
                return None
        if not text.startswith("/emoji"):
            return None
        rest = text[len("/emoji"):]
        if not rest:
            return ""
        if not rest[0].isspace():
            return None
        return rest[2:] if rest.startswith("\r\n") else rest[1:]

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
        mode = str(prefix_mode or "strict").strip().lower()

        alias_map = {
            "strict": "strict",
            "only": "strict",
            "仅": "strict",
            "on": "strict",
            "mixed": "mixed",
            "混合": "mixed",
            "both": "mixed"
        }

        normalized = alias_map.get(mode, "strict")
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
        from utils.response_window import response_allowed
        return response_allowed(self.time_slots)

    # =========================================================
    # 💨 速率限制检测
    # =========================================================
    def _check_rate_limit(self, wxid, category="messages"):
        """Check the effective per-user and global limit for one routing class."""
        if not wxid:
            return True

        category = str(category or "messages").strip().lower()
        limits = {
            "messages": ("messages_per_minute", "global_messages_per_minute"),
            "llm": ("llm_messages_per_minute", "global_llm_messages_per_minute"),
            "prefix": ("prefix_llm_messages_per_minute", "global_prefix_llm_messages_per_minute"),
            "plugin": ("plugin_messages_per_minute", "global_plugin_messages_per_minute"),
        }
        per_key, global_key = limits.get(category, limits["messages"])
        per_user_limit = self.rate_limit_cfg.get(per_key)
        global_limit = self.rate_limit_cfg.get(global_key)
        if per_user_limit in (None, ""):
            per_user_limit = self.rate_limit_cfg.get("messages_per_minute")
        if global_limit in (None, ""):
            global_limit = self.rate_limit_cfg.get("global_messages_per_minute")
        try:
            per_user_limit = int(per_user_limit or 0)
        except (TypeError, ValueError):
            per_user_limit = 0
        try:
            global_limit = int(global_limit or 0)
        except (TypeError, ValueError):
            global_limit = 0

        with self.rate_limit_lock:
            current_time = time.time()
            one_minute_ago = current_time - 60
            user_key = (category, str(wxid))
            global_events = self.global_rate_limit_tracking.setdefault(category, [])
            global_events[:] = [ts for ts in global_events if ts > one_minute_ago]
            user_events = self.rate_limit_tracking.setdefault(user_key, [])
            user_events[:] = [ts for ts in user_events if ts > one_minute_ago]

            if global_limit > 0 and len(global_events) >= global_limit:
                print(f"[ROUTER] global {category} rate limit exceeded ({global_limit}/min)")
                return False
            if per_user_limit > 0 and len(user_events) >= per_user_limit:
                print(f"[ROUTER] {wxid} {category} rate limit exceeded ({per_user_limit}/min)")
                self._record_rate_limit_violation(wxid)
                return False

            user_events.append(current_time)
            if global_limit > 0:
                global_events.append(current_time)
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

        if self.prefix_mode == "strict":
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
