import os
import json
import random
import threading
from datetime import datetime

COMMAND = "/echo"

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data", "echo.json")

lock = threading.Lock()


# =========================
# 初始化
# =========================
def init(config=None):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)

    if not os.path.exists(DATA_FILE):
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)


# =========================
# 主入口（框架标准 handle）
# =========================
def handle(content, context):

    # content = 已去掉命令的参数（框架已处理）
    if content is None:
        return None

    content = content.strip()

    user = context.get("user", "未知用户")
    group = context.get("group", "unknown")

    # =========================
    # 📌 写入模式
    # =========================
    if content:
        return save_echo(content, user, group)

    # =========================
    # 📌 随机读取模式
    # =========================
    return get_random_echo()


# =========================
# 存入树洞
# =========================
def save_echo(text, user, group):

    with lock:
        data = load_data()

        data.append({
            "content": text,
            "user": user,
            "group": group,
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

        save_data(data)

    return "🌱 已存入回声树洞"


# =========================
# 随机读取
# =========================
def get_random_echo():

    with lock:
        data = load_data()

    if not data:
        return "📭 树洞里什么都没有"

    item = random.choice(data)

    return f"{item['content']}\n——{item['user']}"


# =========================
# JSON工具
# =========================
def load_data():
    if not os.path.exists(DATA_FILE):
        return []

    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return []


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)