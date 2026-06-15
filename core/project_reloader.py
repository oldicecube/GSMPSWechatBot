import os
import sys
import threading
import time


BASE_DIR = os.path.dirname(os.path.dirname(__file__))
_RESTARTING = False
_RESTART_LOCK = threading.Lock()


def _scan_py_mtimes():
    mtimes = {}

    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]

        for name in files:
            if not name.endswith(".py"):
                continue

            path = os.path.join(root, name)

            try:
                mtimes[path] = os.path.getmtime(path)
            except OSError:
                continue

    return mtimes


def _restart_process(changed_path):
    global _RESTARTING

    with _RESTART_LOCK:
        if _RESTARTING:
            return
        _RESTARTING = True

    print(f"[HOT-RELOAD] 检测到变更，退出码 100 → launcher 接管重启: {changed_path}")
    sys.stdout.flush()
    sys.stderr.flush()

    # 硬退出，不执行 atexit/finally。launcher.py 检测到 100 后立即重启 main.py
    os._exit(100)


def _watch_loop(interval):
    known = _scan_py_mtimes()
    print("[HOT-RELOAD] 项目级 .py 监控已启动")

    while True:
        time.sleep(interval)

        current = _scan_py_mtimes()

        for path, mtime in current.items():
            if known.get(path) != mtime:
                _restart_process(path)
                return

        for path in list(known.keys()):
            if path not in current:
                _restart_process(path)
                return

        known = current


def start(interval=1):
    threading.Thread(target=_watch_loop, args=(interval,), daemon=True).start()
