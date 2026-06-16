import json
import os
import sys
import signal
import threading

from core.project_reloader import start as start_project_reloader
from core.sender import configure as configure_sender
from core.weflow_media import configure as configure_weflow_media
from core.windows_awake import start as start_windows_awake
from core.queue import task_queue
from core.weflow_client import WeFlowClient
from core.router import Router
from core.dispatcher import Dispatcher
from core.worker import Worker
from core.weflow_headless import WeFlowCore
from core.wechat_sender import start_server as start_sender_server
from utils.dedup import Dedup
from utils.sqlite_store import migrate_legacy_storage


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)


# 全局引用，供信号处理器使用
_weflow: WeFlowCore | None = None


def graceful_shutdown(signum, frame):
    """Ctrl+C → 先停 weflow-core → 再退出"""
    print("\n⚠️  收到终止信号，正在优雅退出...", flush=True)
    if _weflow:
        _weflow.stop()
    sys.exit(0)


signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


def main():
    global _weflow

    config = json.load(open("config.json", encoding="utf-8"))
    migrate_legacy_storage()
    start_windows_awake()
    configure_sender(config)
    configure_weflow_media(config)
    start_project_reloader()

    # ═══════════════════════════════════════
    # � 启动微信发送服务（localhost:9999）
    # ═══════════════════════════════════════
    start_sender_server()

    # ═══════════════════════════════════════
    # �🚀 启动 weflow-core（Node.js 无头服务）
    # ═══════════════════════════════════════
    print("[bot] 正在启动 weflow-core...", flush=True)
    _weflow = WeFlowCore(BASE_DIR)
    if not _weflow.start():
        print("[bot] ❌ weflow-core 启动失败，退出", flush=True)
        sys.exit(1)

    dedup = Dedup()

    router = Router(
        prefix=config.get("prefix", "@服务器状态@我"),
        target_group=config.get("target_group", []),
        time_slots=config.get("time_slots", []),
        rate_limit_cfg=config.get("rate_limit", {}),
        prefix_mode=config.get("prefix_mode", "only")
    )

    dispatcher = Dispatcher()
    dispatcher.load_plugins()
    dispatcher.init_plugins(config)   # ⭐如果你需要 config

    worker = Worker(task_queue, router, dispatcher, config=config)
    worker.start(config["worker_num"])

    client = WeFlowClient(
        token=config["token"],
        queue=task_queue,
        dedup=dedup
    )

    threading.Thread(target=client.start, daemon=False).start()


if __name__ == "__main__":
    main()
