import os

COMMAND = "/seed"

BASE_DIR = os.path.dirname(__file__)
HOME_FILE = os.path.join(BASE_DIR, "home.txt")


def handle(content, context):
    try:
        with open(HOME_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()

    except FileNotFoundError:
        # fallback 内容（避免文件缺失导致None）
        return (
            "🗺️ 服务器种子：\nyour-seed-value-here\n"
        )

    except Exception as e:
        return f"读取seed失败: {str(e)}"