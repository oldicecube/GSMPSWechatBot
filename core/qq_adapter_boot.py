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
        return int((_state_dir(root) / 'adapter.pid').read_text(encoding='utf8').strip())
    except (OSError, ValueError):
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


def _login_confirmed(workdir: Path, uin: str, ws_port: Optional[int]) -> bool:
    """OneBot WS listening (or an explicit success log) means the scan finished."""

    if _port_in_use(ws_port):
        return True
    text = _tail(workdir / '.napuketto' / uin / 'logs' / 'napuketto.log').replace(' ', '')
    return '"state":"success"' in text or '登录成功' in text


def _watch_login(root: Path, workdir: Path, uin: str, ws_port: Optional[int], timeout: float) -> None:
    """Publish the QR code and login URL while the session is unauthenticated."""

    qr_source = workdir / '.napuketto' / uin / 'cache' / 'qrcode.png'
    qr_target = _state_dir(root) / 'qrcode.png'
    deadline = time.time() + timeout
    last_stamp: Optional[float] = None
    while time.time() < deadline:
        if _login_confirmed(workdir, uin, ws_port):
            _print(f'协议端已登录，OneBot WS 端口 {ws_port} 就绪')
            _write_state(root, dict(state='logged_in', uin=uin, ws_port=ws_port))
            return
        try:
            stamp: Optional[float] = qr_source.stat().st_mtime
        except OSError:
            stamp = None
        if stamp is not None and stamp != last_stamp:
            last_stamp = stamp
            try:
                shutil.copy2(qr_source, qr_target)
            except OSError as exc:
                _print(f'复制登录二维码失败: {exc}')
            else:
                url = _latest_login_url(workdir, uin)
                _print(f'需要扫码登录 QQ {uin}：二维码 {qr_target}')
                if url:
                    _print(f'登录链接（手机 QQ 打开）: {url}')
                _write_state(root, dict(state='waiting_scan', uin=uin, qr_path=str(qr_target), qr_url=url))
        time.sleep(DEFAULT_WATCH_INTERVAL_SEC)
    _print(f'等待扫码超时（{int(timeout)}s）：协议端仍在后台等待，可重启协议端重新出码')


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
    timeout = float(settings.get('qr_watch_seconds') or DEFAULT_QR_TIMEOUT_SEC)

    if _port_in_use(ws_port) or _port_in_use(http_port):
        _print(f'协议端已在运行（WS {ws_port} / HTTP {http_port} 已监听），跳过启动')
        _write_state(root, dict(state='running', uin=uin, ws_port=ws_port))
        return False
    pid = _read_pid(root)
    if _pid_alive(pid):
        # 进程活着但端口没起来 = 上次扫码没完成（二维码已过期）。此时直接复用
        # 旧进程会让用户拿到一张永远扫不进去的旧二维码，因此重启一次换新码。
        _print(f'协议端进程存活但未登录（pid={pid}，WS {ws_port} 未监听），重启以刷新二维码')
        _terminate_tree(pid)
        _clear_pid(root)

    state = _state_dir(root)
    log_path = state / 'adapter.log'
    command = str(settings.get('command') or 'pnpm start')
    env = dict(os.environ)
    for key, value in dict(settings.get('env') or {}).items():
        if value:
            env[str(key)] = str(value)

    _print(f'启动协议端: {command} (cwd={workdir})')
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
    _write_pid(root, process.pid)
    _write_state(root, dict(state='starting', uin=uin, ws_port=ws_port, pid=process.pid, log=str(log_path)))
    if uin:
        threading.Thread(target=_watch_login, args=(root, workdir, uin, ws_port, timeout), daemon=True).start()
    return True