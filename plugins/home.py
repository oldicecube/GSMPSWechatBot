import os

COMMAND = "/home"

BASE_DIR = os.path.dirname(__file__)
HOME_FILE = os.path.join(BASE_DIR, "home.txt")


def handle(content, context):
    try:
        with open(HOME_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()

    except FileNotFoundError:
        # fallback 内容（避免文件缺失导致None）
        return (
            "🏠 服务器主站：\nyour-website.com\n"
            "📚 帮助文档站：\ndocs.your-website.com\n"
            "🧭 新手指南：\ndocs.your-website.com/guide\n"
            "📺 Bilibili：\nYourChannel"
        )

    except Exception as e:
        return f"读取home失败: {str(e)}"