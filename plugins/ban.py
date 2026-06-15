import os
import threading

from utils.sqlite_store import load_document, save_document

COMMAND = "/ban"
BLACKLIST_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "blacklist.json")
ADMIN_WXIDS = []

lock = threading.Lock()


def init(config=None):
    global ADMIN_WXIDS
    os.makedirs(os.path.dirname(BLACKLIST_PATH), exist_ok=True)

    if not os.path.exists(BLACKLIST_PATH):
        with open(BLACKLIST_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f, ensure_ascii=False, indent=2)

    if config:
        llm_config = config.get("llm") or {}
        ADMIN_WXIDS = llm_config.get("admin_wxids") or []


def handle(content, context):
    wxid = str(context.get("wxid") or context.get("raw", {}).get("wxid") or "").strip()
    
    # 检查是否为管理员
    if wxid not in ADMIN_WXIDS:
        return None

    if not content:
        return "用法: /ban <wxid>"

    target_wxid = str(content).strip()

    # 基础验证：wxid 应该以 wxid_ 开头
    if not target_wxid.startswith("wxid_"):
        return f"无效的 wxid 格式: {target_wxid}"

    return ban_user(target_wxid)


def ban_user(target_wxid):
    """将用户加入黑名单"""
    with lock:
        data = load_blacklist()

        if target_wxid in data:
            return f"用户 {target_wxid} 已在黑名单中"

        data[target_wxid] = True
        save_blacklist(data)

    return f"已将 {target_wxid} 加入黑名单"


def load_blacklist():
    """读取黑名单"""
    return load_document(BLACKLIST_PATH, default={}) or {}


def save_blacklist(data):
    """保存黑名单"""
    save_document(BLACKLIST_PATH, data)
