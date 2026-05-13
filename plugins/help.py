import os

COMMAND = "/help"

BASE_DIR = os.path.dirname(__file__)
HELP_FILE = os.path.join(BASE_DIR, "help.txt")


def handle(content, context):
    try:
        with open(HELP_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return "帮助文件不存在"
    except Exception as e:
        return f"读取帮助失败: {str(e)}"