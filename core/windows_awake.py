import atexit
import ctypes
import platform
import threading
import time

_RUNNING = False
_LOCK = threading.Lock()
_THREAD = None

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001
_ES_DISPLAY_REQUIRED = 0x00000002


def start():
    global _RUNNING, _THREAD

    if platform.system() != "Windows":
        return

    with _LOCK:
        if _RUNNING:
            return

        _RUNNING = True
        _apply_keep_awake()
        _THREAD = threading.Thread(target=_heartbeat, daemon=True)
        _THREAD.start()
        atexit.register(stop)
        print("[SYSTEM] Windows keep-awake enabled")


def stop():
    global _RUNNING

    if platform.system() != "Windows":
        return

    with _LOCK:
        if not _RUNNING:
            return
        _RUNNING = False

    ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    print("[SYSTEM] Windows keep-awake disabled")


def _heartbeat():
    while True:
        with _LOCK:
            if not _RUNNING:
                return

        _apply_keep_awake()
        time.sleep(30)


def _apply_keep_awake():
    ctypes.windll.kernel32.SetThreadExecutionState(
        _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED | _ES_DISPLAY_REQUIRED
    )
