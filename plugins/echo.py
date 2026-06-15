import json
import os
import random
import threading
from datetime import datetime
from difflib import SequenceMatcher

from utils.points_manager import get_points, add_points

COMMAND = "/echo"
BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "echo.json")

lock = threading.Lock()
request_timestamps = {}
REQUEST_INTERVAL_SECONDS = 10
MAX_ECHO_LENGTH = 60
ADMIN_WXIDS = []
POINTS_COST = 3


def init(config=None):
    global ADMIN_WXIDS
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)

    if config:
        llm_config = config.get("llm") or {}
        ADMIN_WXIDS = llm_config.get("admin_wxids") or []


def handle(content, context):
    if content is None:
        return None

    content = str(content).strip()
    user = context.get("user", "未知用户")
    group = context.get("group", "unknown")
    wxid = str(context.get("wxid") or context.get("raw", {}).get("wxid") or "").strip()

    # 检查是否为删除命令
    if content == "/echo --delete":
        is_admin = wxid in ADMIN_WXIDS
        if is_admin:
            return delete_last_echo()
        else:
            # 普通用户输入删除命令，不做任何处理
            return None

    rate_limit_result = check_rate_limit(wxid)
    if rate_limit_result is False:
        return None
    if isinstance(rate_limit_result, str):
        return rate_limit_result

    if content:
        return save_echo(content, user, group, wxid=wxid)

    return get_random_echo()


def normalize_echo_text(text):
    if text is None:
        return ""

    text = str(text).replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    return "\n".join(lines).strip()


def check_rate_limit(wxid):
    if not wxid:
        return None

    now = datetime.now().timestamp()

    with lock:
        state = request_timestamps.get(wxid) or {}
        last_request_at = state.get("last_request_at")
        notice_sent = bool(state.get("notice_sent"))

        if last_request_at is not None and now - last_request_at < REQUEST_INTERVAL_SECONDS:
            if notice_sent:
                return False

            request_timestamps[wxid] = {
                "last_request_at": last_request_at,
                "notice_sent": True
            }
            return f"请求过于频繁，请在 {REQUEST_INTERVAL_SECONDS} 秒后再试"

        request_timestamps[wxid] = {
            "last_request_at": now,
            "notice_sent": False
        }

    return None


def calculate_similarity(text1, text2):
    """计算两个字符串的相似度，返回0-1之间的值"""
    matcher = SequenceMatcher(None, text1, text2)
    return matcher.ratio()


def has_long_common_substring(text1, text2, min_length=20):
    """检查两个字符串去掉空格和回车后是否有连续min_length个字符相同"""
    cleaned1 = text1.replace(" ", "").replace("\n", "")
    cleaned2 = text2.replace(" ", "").replace("\n", "")
    
    if len(cleaned1) < min_length or len(cleaned2) < min_length:
        return False
    
    # 检查cleaned2中是否包含cleaned1的min_length长子串
    for i in range(len(cleaned1) - min_length + 1):
        substring = cleaned1[i:i + min_length]
        if substring in cleaned2:
            return True
    
    return False


def save_echo(text, user, group, wxid=None):
    normalized_text = normalize_echo_text(text)

    if not normalized_text:
        return "不能加入空白内容"

    if len(normalized_text) > MAX_ECHO_LENGTH:
        return f"echo 信息不能超过 {MAX_ECHO_LENGTH} 个字符"

    # 检查积分是否足够
    if wxid:
        current_points = get_points(wxid)
        if current_points < POINTS_COST:
            return f"❌ 积分不足！新增echo内容需要消耗 {POINTS_COST} 积分，你当前有 {current_points:.1f} 积分。"

    with lock:
        data = load_data()
        text_length = len(normalized_text)

        for item in data:
            if not isinstance(item, dict):
                continue

            item_text = normalize_echo_text(item.get("content"))
            
            # 对于小于27个字符的消息：检查相似度是否超过75%
            if text_length < 27:
                similarity = calculate_similarity(normalized_text, item_text)
                if similarity > 0.75:
                    return "此内容与已存在的消息相似度过高，不允许存入"
            
            # 对于大于等于27个字符的消息：检查去掉空格和回车后是否有连续20个字符相同
            if text_length >= 27:
                if has_long_common_substring(normalized_text, item_text, min_length=20):
                    return "此内容与已存在的消息有过长的重复部分，不允许存入"

        # 检查通过，消耗积分
        if wxid:
            try:
                add_points(wxid, -POINTS_COST)
            except Exception as e:
                print(f"[ECHO ERROR] 积分消耗失败: {e}")
                return "❌ 积分消耗失败，请重试"
        
        data.append({
            "content": normalized_text,
            "user": user,
            "wxid": wxid,
            "group": group,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

        save_data(data)

    response = f"✅ 已存入回声树洞"
    if wxid:
        remaining_points = get_points(wxid)
        response += f"\n💎 消耗积分: -{POINTS_COST} (剩余: {remaining_points:.1f})"
    
    return response


def get_random_echo():
    with lock:
        data = load_data()

    if not data:
        return "树洞里什么都没有"

    item = random.choice(data)
    return f"{item['content']}\n- {item['user']}"


def delete_last_echo():
    """删除最后一条存入的echo消息（仅限管理员调用）"""
    with lock:
        data = load_data()

        if not data:
            return "树洞里没有消息可删除"

        deleted_item = data.pop()
        save_data(data)

    return f"已删除最后一条消息: {deleted_item['content']}"


def load_data():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_data(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
