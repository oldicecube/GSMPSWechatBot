"""
Launcher: 保障永远只有一个 main.py 进程在线。
- 追踪子进程 PID，确保旧进程彻底退出后才启动新进程
- main.py 退出码 100 (=热重载) -> 立即重启
- main.py 其他退出码       -> 透传给 start.bat 处理
"""
import subprocess
import sys
import os
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(BASE_DIR, "main.py")


def _ensure_gone(pid, timeout=3):
    """等待 PID 彻底退出，超时则强杀进程树。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _pid_alive(pid):
            return
        time.sleep(0.1)
    # 超时：强杀残留进程树
    print(f"[LAUNCHER] PID={pid} 超时未退出，强制终止")
    if sys.platform == "win32":
        os.system(f"taskkill /F /T /PID {pid} 2>nul")
    else:
        os.kill(pid, 9)


def _pid_alive(pid):
    """检测进程是否存活（跨平台）。"""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


while True:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    proc = subprocess.Popen([sys.executable, MAIN], creationflags=flags)
    child_pid = proc.pid
    print(f"[LAUNCHER] main.py 已启动 PID={child_pid}")

    code = proc.wait()

    # 确保子进程及其子进程树已彻底退出
    _ensure_gone(child_pid)

    if code == 100:
        print(f"[LAUNCHER] PID={child_pid} 请求热重载，立即重启...")
        continue

    print(f"[LAUNCHER] PID={child_pid} 退出码={code}，停止守护")
    sys.exit(code)
