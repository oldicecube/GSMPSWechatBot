"""
wechat_sender 启动器
在后台线程启动 HTTP 服务器（localhost:9999），接收 wxSend 请求并发送微信消息。
"""
import threading
import sys
import os

# 确保路径正确
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_server_started = False
_server_thread = None


def start_server(host: str = "127.0.0.1", port: int = 9999):
    """在后台线程启动微信消息发送 HTTP 服务器"""
    global _server_started, _server_thread

    if _server_started:
        return True

    try:
        from core.wechat_sender.wechat_hook_server import run_server

        _server_thread = threading.Thread(
            target=run_server,
            args=(host, port),
            daemon=True,
            name="wechat-sender-server",
        )
        _server_thread.start()
        _server_started = True
        print(f"[sender] 微信发送服务已启动: http://{host}:{port}", flush=True)
        return True
    except Exception as e:
        print(f"[sender] 微信发送服务启动失败: {e}", flush=True)
        return False


def stop_server():
    """停止微信发送服务"""
    global _server_started
    _server_started = False
