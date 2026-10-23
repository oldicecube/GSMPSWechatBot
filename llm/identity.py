"""Speaker identity is separate from bot-ness, mentions and quoted speakers."""

import re
def truth(value):
    return value.strip().lower() in {"1", "true", "yes", "on"} if isinstance(value, str) else bool(value)

def bot_identity(config):
    config = config if isinstance(config, dict) else {}
    identity = config.get("identity") if isinstance(config.get("identity"), dict) else {}
    def values(value):
        return [value] if isinstance(value, str) else (value or [])
    wxids = {str(value).strip() for value in values(config.get("bot_wxids")) if str(value).strip()}
    # WeFlow sends clean account IDs while xwechat directory configuration can
    # contain a four-character profile suffix (e.g. wxid_account_ab12).
    # Derive aliases only from configured self IDs, never from arbitrary users.
    for own in list(wxids):
        match = re.fullmatch(r"(wxid_[^_]+)_[A-Za-z0-9]{4}", own)
        if match:
            wxids.add(match.group(1))
    prefixes = [str(value).strip() for value in values(config.get("prefixes")) if str(value).strip()]
    aliases = {str(config.get("assistant_nickname") or "LLM").strip(), str(identity.get("name") or "").strip()}
    aliases.update(prefixes)
    aliases.update(value.lstrip("@＠").strip() for value in prefixes)
    aliases.discard("")
    return wxids, aliases

def annotate_records(records, config):
    wxids, aliases = bot_identity(config)
    learning = config.get("learning") if isinstance(config.get("learning"), dict) else {}
    configured_other_bots = config.get("other_bot_names") or learning.get("bot_names") or []
    if isinstance(configured_other_bots, str):
        configured_other_bots = [configured_other_bots]
    other_bot_names = {
        str(value).strip().lstrip("@＠").casefold()
        for value in configured_other_bots
        if str(value).strip()
    }
    output = []
    for raw in records or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        wxid = str(item.get("wxid") or item.get("sender_wxid") or item.get("subject_id") or "").strip()
        nickname = str(item.get("nickname") or item.get("sender_nickname") or item.get("user") or "").strip()
        role = str(item.get("role") or "").strip().lower()
        known_other = bool(wxid and wxids and wxid not in wxids)
        self_authored = wxid in wxids or (
            not known_other and (truth(item.get("is_bot_self")) or role == "assistant" or (not wxid and nickname in aliases))
        )
        # An unrelated robot is not this assistant. A nickname collision cannot
        # override a verified different wxid. Quoting/mentioning self isn't authorship.
        item["is_bot_self"] = self_authored
        item["is_bot"] = self_authored or truth(item.get("is_bot"))
        item["is_other_bot"] = bool(
            not self_authored
            and (
                truth(item.get("is_other_bot"))
                or nickname.lstrip("@＠").casefold() in other_bot_names
            )
        )
        item["role"] = "assistant" if self_authored else ("user" if role == "assistant" else role or "user")
        item["bot_identity"] = "self" if self_authored else "other"
        if wxid:
            item.setdefault("wxid", wxid); item.setdefault("sender_wxid", wxid)
        if nickname:
            item.setdefault("nickname", nickname); item.setdefault("sender_nickname", nickname)
        if item.get("message_id") in (None, "") and item.get("messageKey") not in (None, ""):
            item["message_id"] = item["messageKey"]
        quote_wxid = str(item.get("quoted_sender_wxid") or "").strip()
        quote_name = str(item.get("quoted_sender") or "").strip()
        has_quote = bool(quote_wxid or quote_name or item.get("quoted_content") or item.get("quoted_message_id"))
        if has_quote:
            quote_other = bool(quote_wxid and wxids and quote_wxid not in wxids)
            quote_self = quote_wxid in wxids or (not quote_other and (
                truth(item.get("quoted_is_bot")) or item.get("quoted_role") == "assistant" or (not quote_wxid and quote_name in aliases)
            ))
            item["quoted_is_bot"] = quote_self
            item["quoted_role"] = "assistant" if quote_self else "user"
            if quote_self and not truth(item.get("directed_to_other")):
                item["reply_to_bot"] = True
        # Native pat messages expose their actor/recipient independently of
        # the transport sender. Only parse the complete native quoted form;
        # ordinary discussion containing these words does not become an action.
        pat = re.match(r'^[“"]([^”"]+)[”"]\s*拍了拍\s*[“"]([^”"]+)[”"]', str(item.get("content") or "").strip())
        if pat:
            actor, recipient = pat.groups()
            item["action_type"] = "patpat"
            item["action_actor"] = actor
            item["action_recipient"] = recipient
            item["action_recipient_is_self"] = recipient in aliases or recipient in wxids
            if item["action_recipient_is_self"]:
                item["directed_to_bot"] = True
        output.append(item)
    return output
