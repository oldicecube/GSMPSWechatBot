"""Start the external QQ protocol backend (NapukettoQQ) alongside the bot.

历史背景：NapCat / SnowLuma 停更后，QQ 侧改由独立的 NapukettoQQ 进程提供
OneBot 11。它和 NapCat 一样不随 bot 生命周期结束，但需要在 bot 启动时自动拉起：

1. 进程：``pnpm start``（PowerShell 执行策略禁 ``pnpm.ps1``，必须走 ``cmd``）。
2. 登录：首次/掉线时需要扫码。NapukettoQQ 会把二维码写到
   ``<workdir>/.napuketto/<uin>/cache/qrcode.png``，这里复制到
   ``depends-data/qq-adapter/qrcode.png`` 并把登录链接打进 bot 控制台，
   省得用户去翻实验室目录。
3. 就绪：OneBot WS 端口开始监听即视为已登录，可被适配器插件连上。

配置见 ``depends-data/qq-adapter/launch.json``（本地文件，不入库）。
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_RELATIVE = Path('depends-data/qq-adapter/launch.json')
STATE_RELATIVE = Path('depends-data/qq-adapter')
DEFAULT_QR_TIMEOUT_SEC = 900
DEFAULT_WATCH_INTERVAL_SEC = 3.0
_LOG_PREFIX = '[QQ ADAPTER]'
QR_WINDOW_SCRIPT = Path('scripts/show_qq_qr.ps1')
# root -> 正在显示的二维码窗口进程（cmd -> powershell -> WinForms）
_QR_WINDOW_LOCK = threading.Lock()
_QR_WINDOWS: Dict[str, subprocess.Popen] = {}
# root -> stop event of the long-running login watcher (one per root)
_WATCHER_LOCK = threading.Lock()
_WATCHERS: Dict[str, threading.Event] = {}


def _print(message: str) -> None:
    """Print a startup line that also lands in the bot console log."""

    print(f'{_LOG_PREFIX} {message}', flush=True)


def _config_path(root: Path) -> Path:
    return Path(root) / CONFIG_RELATIVE


def load_settings(root: Path) -> Optional[Dict[str, Any]]:
    """Read the launcher config; missing or invalid file means "stay disabled"."""

    path = _config_path(root)
    if not path.is_file():
        return None
    try:
        settings = json.loads(path.read_text(encoding='utf8'))
    except (OSError, ValueError) as exc:
        _print(f'启动配置无效 {path}: {exc}')
        return None
    if not isinstance(settings, dict):
        return None
    return settings


def is_napcat_superseded(root: Path) -> bool:
    """Whether the new protocol backend replaces NapCat on this host."""

    settings = load_settings(root)
    if not settings or settings.get('enabled') is not True:
        return False
    return settings.get('replace_napcat') is True


def _state_dir(root: Path) -> Path:
    path = Path(root) / STATE_RELATIVE
    path.mkdir(parents=True, exist_ok=True)
    return path


def _port_in_use(port: Optional[int], host: str = '127.0.0.1', timeout: float = 0.4) -> bool:
    """Return whether something already listens on the given local port."""

    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _pid_alive(pid: Optional[int]) -> bool:
    """Cross-platform liveness probe for the previously spawned launcher pid."""

    if not pid:
        return False
    if os.name == 'nt':
        try:
            result = subprocess.run(
                ['tasklist', '/FI', f'PID eq {int(pid)}', '/NH'],
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(int(pid)) in (result.stdout or '')
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def _write_state(root: Path, state: Dict[str, Any]) -> None:
    """Persist the latest launcher state for the WebUI / manual inspection."""

    payload = dict(state)
    payload['updated_at'] = datetime.now().isoformat(timespec='seconds')
    try:
        (_state_dir(root) / 'status.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf8'
        )
    except OSError:
        pass


def _read_pid(root: Path) -> Optional[int]:
    """Read the pid recorded by a previous startup."""

    try:
        text = (_state_dir(root) / 'adapter.pid').read_text(encoding='utf8')
    except OSError:
        return None
    # 手工/脚本写入的 pid 文件可能带 UTF-8 BOM（Out-File -Encoding utf8 就会），
    # 直接 int() 会失败，进而误判“进程已死”而重复拉起。
    text = text.lstrip('\ufeff').strip()
    try:
        return int(text)
    except ValueError:
        return None


def _clear_pid(root: Path) -> None:
    """Remove the recorded pid file after the process was recycled."""

    try:
        (_state_dir(root) / 'adapter.pid').unlink()
    except OSError:
        pass


def _terminate_tree(pid: int) -> None:
    """Kill the adapter process tree (pnpm -> node -> QQ core) on Windows."""

    try:
        subprocess.run(
            ['taskkill', '/PID', str(int(pid)), '/T', '/F'],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _print(f'结束旧协议端进程失败 pid={pid}: {exc}')


def _write_pid(root: Path, pid: int) -> None:
    """Record the launcher pid so restarts do not spawn duplicates."""

    try:
        (_state_dir(root) / 'adapter.pid').write_text(str(pid), encoding='utf8')
    except OSError:
        pass


def _tail(path: Path) -> str:
    """Read the tail of a log file without loading the whole file."""

    try:
        with path.open('rb') as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 64 * 1024))
            return handle.read().decode('utf8', 'replace')
    except OSError:
        return ''


def _latest_login_url(workdir: Path, uin: str) -> str:
    """Pull the newest ``txz.qq.com`` login URL out of the adapter log."""

    log_path = workdir / '.napuketto' / uin / 'logs' / 'napuketto.log'
    marker = 'https://txz.qq.com/p?k='
    candidates = [line for line in _tail(log_path).splitlines() if marker in line]
    if not candidates:
        return ''
    tail_text = candidates[-1][candidates[-1].find(marker):]
    # 日志里 URL 后面常跟着中文括号或说明文字（如 "…&f=1600001604）"），
    # 按 URL 合法字符集截断，避免把标点一起复制进控制台/状态文件。
    allowed = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:/?=&%-._~*+!$,;@#[]')
    end = len(tail_text)
    for index, char in enumerate(tail_text):
        if char not in allowed:
            end = index
            break
    return tail_text[:end].strip()


def _login_confirmed(workdir: Path, uin: str, ws_port: Optional[int], http_port: Optional[int] = None) -> bool:
    """Treat the OneBot ports as the source of truth for "logged in".

    The log fallback only counts while it is newer than the QR image, otherwise a
    stale ``"state":"success"`` line would keep us "logged in" after a logout.
    """

    if _port_in_use(ws_port) or _port_in_use(http_port):
        return True
    log_path = workdir / '.napuketto' / uin / 'logs' / 'napuketto.log'
    text = _tail(log_path).replace(' ', '')
    # 只认扫码登录的 QR 状态成功。QQ 每次启动都会打“快速登录成功”，
    # 用它当判据会把崩溃重启循环误判成已登录（2026-11-18 实测踩到）。
    if '"state":"success"' not in text:
        return False
    try:
        qr_mtime = (workdir / '.napuketto' / uin / 'cache' / 'qrcode.png').stat().st_mtime
        log_mtime = log_path.stat().st_mtime
    except OSError:
        return True
    return log_mtime >= qr_mtime


def _copy_qr(source: Path, target: Path, attempts: int = 6, delay: float = 0.4) -> Optional[Path]:
    """Copy a fresh QR, tolerating the old window still holding the file open.

    Windows 上二维码窗口用 PowerShell 载入 PNG 后会一直持有文件句柄，直接 copy 会得到
    ``WinError 32``。这里先重试（关窗后句柄释放有延迟），仍然失败就退化成唯一文件名，
    保证新码一定发布得出去。
    """

    for attempt in range(attempts):
        try:
            shutil.copy2(source, target)
            return target
        except OSError:
            if attempt == attempts - 1:
                break
            time.sleep(delay)
    fallback = target.with_name(f'qrcode-{int(time.time())}{target.suffix}')
    try:
        shutil.copy2(source, fallback)
    except OSError as exc:
        _print(f'复制登录二维码失败: {exc}')
        return None
    _print(f'旧二维码文件仍被占用，改用新文件名: {fallback.name}')
    return fallback


def _publish_qr(root: Path, workdir: Path, uin: str, show_window: bool) -> bool:
    """Copy the newest QR into the state dir, print its URL and pop the window."""

    qr_source = workdir / '.napuketto' / uin / 'cache' / 'qrcode.png'
    qr_target = _state_dir(root) / 'qrcode.png'
    if show_window:
        # 必须先关旧窗口：窗口进程会锁住正在显示的 PNG，不先释放就 copy 不动。
        _close_qr_window(root)
    published = _copy_qr(qr_source, qr_target)
    if published is None:
        return False
    url = _latest_login_url(workdir, uin)
    _print(f'需要扫码登录 QQ {uin}：二维码 {published}')
    if url:
        _print(f'登录链接（手机 QQ 打开）: {url}')
    if show_window:
        _open_qr_window(root, published, uin)
    _write_state(root, dict(state='waiting_scan', uin=uin, qr_path=str(published), qr_url=url))
    return True


def _spawn_adapter(root: Path, settings: Dict[str, Any]) -> Optional[int]:
    """Spawn the external protocol backend and return its pid."""

    workdir = Path(str(settings.get('workdir') or '')).expanduser()
    if not workdir.is_dir():
        _print(f'协议端目录不存在: {workdir}')
        return None
    state = _state_dir(root)
    log_path = state / 'adapter.log'
    command = str(settings.get('command') or 'pnpm start')
    env = dict(os.environ)
    for key, value in dict(settings.get('env') or {}).items():
        if value:
            env[str(key)] = str(value)

    _print(f'启动协议端: {command} (cwd={workdir})')
    try:
        with log_path.open('ab') as output:
            process = subprocess.Popen(
                ['cmd.exe', '/c', command],
                cwd=str(workdir),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
    except (OSError, subprocess.SubprocessError) as exc:
        _print(f'启动协议端失败: {exc}')
        return None
    _write_pid(root, process.pid)
    _write_state(
        root,
        dict(
            state='starting',
            uin=str(settings.get('uin') or '').strip(),
            ws_port=settings.get('ws_port'),
            pid=process.pid,
            log=str(log_path),
        ),
    )
    return process.pid


def _qr_window_supported() -> bool:
    """Whether a popup window can be shown in this environment."""

    return os.name == 'nt' and os.environ.get('MAIBOT_INTERCEPT_SEND') != '1'


def _open_qr_window(root: Path, qr_path: Path, uin: str) -> Optional[subprocess.Popen]:
    """Pop the login QR code into its own cmd window (Windows only)."""

    if not _qr_window_supported():
        return None
    script = Path(root) / QR_WINDOW_SCRIPT
    if not script.is_file():
        _print(f'二维码窗口脚本缺失，跳过弹窗: {script}')
        return None
    try:
        process = subprocess.Popen(
            [
                'cmd.exe',
                '/c',
                'powershell.exe',
                '-NoProfile',
                '-STA',
                '-ExecutionPolicy',
                'Bypass',
                '-File',
                str(script),
                '-ImagePath',
                str(qr_path),
                '-Uin',
                str(uin or ''),
            ],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NEW_CONSOLE', 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _print(f'弹出二维码窗口失败: {exc}')
        return None
    with _QR_WINDOW_LOCK:
        _QR_WINDOWS[str(root)] = process
    _print(f'已弹出二维码窗口（pid={process.pid}），登录成功后自动关闭')
    return process


def _close_qr_window(root: Path) -> None:
    """Close the QR window opened for this root, if it is still alive."""

    with _QR_WINDOW_LOCK:
        process = _QR_WINDOWS.pop(str(root), None)
    if process is None or process.poll() is not None:
        return
    _terminate_tree(process.pid)
    _print('二维码窗口已关闭')


def _close_all_qr_windows() -> None:
    """Close every QR window (called on interpreter exit)."""

    with _QR_WINDOW_LOCK:
        processes = list(_QR_WINDOWS.items())
        _QR_WINDOWS.clear()
    for _root, process in processes:
        if process.poll() is None:
            try:
                subprocess.run(
                    ['taskkill', '/PID', str(int(process.pid)), '/T', '/F'],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
            except (OSError, subprocess.SubprocessError):
                pass


atexit.register(_close_all_qr_windows)


def _watch_login(
    root: Path,
    workdir: Path,
    uin: str,
    ws_port: Optional[int],
    http_port: Optional[int] = None,
    timeout: Optional[float] = None,
    show_window: bool = True,
    stop_event: Optional[threading.Event] = None,
    respawn: Optional[Any] = None,
) -> None:
    """Keep the QR code and its window in sync for the whole bot lifetime.

    - a new/refreshed QR file closes the old window and pops a fresh one
    - a confirmed login closes the window
    - logging back out (ports down / new QR) re-arms the flow, so an expired or
      revoked session always gets a fresh QR window without restarting the bot
    """

    qr_source = workdir / '.napuketto' / uin / 'cache' / 'qrcode.png'
    last_stamp: Optional[float] = None
    logged_in = False
    warned = False
    warn_at = time.time() + timeout if timeout else None
    last_respawn = 0.0

    while stop_event is None or not stop_event.is_set():
        if _login_confirmed(workdir, uin, ws_port, http_port):
            if not logged_in:
                logged_in = True
                warned = False
                _close_qr_window(root)
                _print(f'协议端已登录，OneBot WS 端口 {ws_port} 就绪')
                _write_state(root, dict(state='logged_in', uin=uin, ws_port=ws_port))
            time.sleep(max(DEFAULT_WATCH_INTERVAL_SEC, 5.0))
            continue

        if logged_in:
            # 已登录又掉回未登录：被踢下线 / 会话失效，重新走扫码流程
            logged_in = False
            last_stamp = None
            warned = False
            warn_at = time.time() + timeout if timeout else None
            _print('登录状态失效（端口已断开），等待协议端重新出码并弹出新二维码窗口')
            _write_state(root, dict(state='login_lost', uin=uin, ws_port=ws_port))

        if respawn is not None and time.time() - last_respawn > 60:
            last_respawn = time.time()
            pid = _read_pid(root)
            if not _pid_alive(pid):
                _print(f'协议端进程已退出（pid={pid}），重新拉起以刷新二维码')
                _close_qr_window(root)
                last_stamp = None
                if respawn() is None:
                    _print('重新拉起协议端失败，60 秒后重试')

        try:
            stamp: Optional[float] = qr_source.stat().st_mtime
        except OSError:
            stamp = None
        if stamp is not None and stamp != last_stamp:
            last_stamp = stamp
            _publish_qr(root, workdir, uin, show_window)
        if warn_at is not None and not warned and time.time() > warn_at:
            warned = True
            _print(f'等待扫码已超过 {int(timeout)}s，仍会持续监听：二维码过期会自动换新码并弹新窗口')
        time.sleep(DEFAULT_WATCH_INTERVAL_SEC)

    _close_qr_window(root)


def _start_watcher(root: Path, settings: Dict[str, Any]) -> None:
    """Start (once per root) the lifetime login watcher."""

    key = str(root)
    with _WATCHER_LOCK:
        existing = _WATCHERS.get(key)
        if existing is not None and not existing.is_set():
            return
        stop_event = threading.Event()
        _WATCHERS[key] = stop_event

    uin = str(settings.get('uin') or '').strip()
    if not uin:
        return
    workdir = Path(str(settings.get('workdir') or '')).expanduser()
    timeout_value = settings.get('qr_watch_seconds')
    thread = threading.Thread(
        target=_watch_login,
        args=(root, workdir, uin, settings.get('ws_port'), settings.get('http_port')),
        kwargs=dict(
            timeout=float(timeout_value) if timeout_value else None,
            show_window=settings.get('show_qr_window', True) is not False,
            stop_event=stop_event,
            respawn=lambda: _spawn_adapter(root, settings),
        ),
        daemon=True,
    )
    thread.start()


def _stop_all_watchers() -> None:
    """Signal every login watcher to exit (used on interpreter shutdown/tests)."""

    with _WATCHER_LOCK:
        events = list(_WATCHERS.values())
        _WATCHERS.clear()
    for event in events:
        event.set()


def ensure_qq_adapter(root: Path) -> bool:
    """Start the QQ protocol backend when configured; return whether we spawned it."""

    if os.name != 'nt' or os.environ.get('MAIBOT_INTERCEPT_SEND') == '1':
        # 与 core/napcat_boot 一致：测试/非 Windows 环境只读配置，绝不真的拉起外部进程。
        return False
    settings = load_settings(root)
    if not settings:
        return False
    if settings.get('enabled') is not True or settings.get('autostart') is not True:
        return False
    workdir = Path(str(settings.get('workdir') or '')).expanduser()
    if not workdir.is_dir():
        _print(f'协议端目录不存在: {workdir}')
        return False
    uin = str(settings.get('uin') or '').strip()
    ws_port = settings.get('ws_port')
    http_port = settings.get('http_port')

    if _port_in_use(ws_port) or _port_in_use(http_port):
        _print(f'协议端已在运行（WS {ws_port} / HTTP {http_port} 已监听），跳过启动')
        _write_state(root, dict(state='running', uin=uin, ws_port=ws_port))
        _start_watcher(root, settings)
        return False

    pid = _read_pid(root)
    spawned = False
    if _pid_alive(pid):
        # 进程活着但端口没起来 = 上次扫码没完成（二维码已过期）。此时直接复用
        # 旧进程会让用户拿到一张永远扫不进去的旧二维码，因此重启一次换新码。
        _print(f'协议端进程存活但未登录（pid={pid}，WS {ws_port} 未监听），重启以刷新二维码')
        _terminate_tree(pid)
        _clear_pid(root)

    spawned = _spawn_adapter(root, settings) is not None
    _start_watcher(root, settings)
    return spawned
