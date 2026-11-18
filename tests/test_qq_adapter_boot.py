"""core.qq_adapter_boot 的启动判定与安全旁路测试。

这些用例只验证「要不要拉起外部协议端」的判定逻辑，绝不真的启动
NapukettoQQ 进程，也不占用 13010 / 13011 端口。
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_boot_module():
    """按文件路径加载仓库根目录的 core/qq_adapter_boot。

    pytest 的 pythonpath 同时包含 ``.`` 与 ``src``，而 ``src/core`` 是常规包、
    仓库根 ``core`` 是命名空间包，``import core.xxx`` 会被解析到 ``src/core``。
    这里直接按文件加载，保证测的是真正被 main.py 使用的实现。
    """

    spec = importlib.util.spec_from_file_location(
        'qq_adapter_boot_under_test', ROOT / 'core' / 'qq_adapter_boot.py'
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qq_adapter_boot = _load_boot_module()


def _write_launch(root: Path, **overrides) -> Path:
    """在临时根目录写入 launch.json，返回该文件路径。"""

    target = root / 'depends-data' / 'qq-adapter' / 'launch.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        'enabled': True,
        'autostart': True,
        'replace_napcat': True,
        'workdir': str(root),
        'command': 'pnpm start',
        'uin': '10001',
        'http_port': 0,
        'ws_port': 0,
    }
    payload.update(overrides)
    target.write_text(json.dumps(payload), encoding='utf8')
    return target


def test_load_settings_returns_none_without_file(tmp_path: Path) -> None:
    assert qq_adapter_boot.load_settings(tmp_path) is None


def test_load_settings_returns_none_for_broken_json(tmp_path: Path) -> None:
    target = tmp_path / 'depends-data' / 'qq-adapter' / 'launch.json'
    target.parent.mkdir(parents=True)
    target.write_text('{not json', encoding='utf8')
    assert qq_adapter_boot.load_settings(tmp_path) is None


def test_is_napcat_superseded_true_when_enabled_and_replace(tmp_path: Path) -> None:
    _write_launch(tmp_path, replace_napcat=True)
    assert qq_adapter_boot.is_napcat_superseded(tmp_path) is True


def test_is_napcat_superseded_false_when_disabled(tmp_path: Path) -> None:
    _write_launch(tmp_path, enabled=False, replace_napcat=True)
    assert qq_adapter_boot.is_napcat_superseded(tmp_path) is False


def test_is_napcat_superseded_false_without_replace_flag(tmp_path: Path) -> None:
    _write_launch(tmp_path, replace_napcat=False)
    assert qq_adapter_boot.is_napcat_superseded(tmp_path) is False


def test_ensure_qq_adapter_skips_when_intercepting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """测试/拦截模式下必须直接返回，不创建子进程。"""

    _write_launch(tmp_path)
    monkeypatch.setenv('MAIBOT_INTERCEPT_SEND', '1')

    def _boom(*args, **kwargs):  # pragma: no cover - 被调用即失败
        raise AssertionError('拦截模式下不应拉起协议端进程')

    monkeypatch.setattr(qq_adapter_boot.subprocess, 'Popen', _boom)
    assert qq_adapter_boot.ensure_qq_adapter(tmp_path) is False


def test_ensure_qq_adapter_requires_autostart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_launch(tmp_path, autostart=False)
    monkeypatch.delenv('MAIBOT_INTERCEPT_SEND', raising=False)
    monkeypatch.setattr(qq_adapter_boot.os, 'name', 'nt')
    assert qq_adapter_boot.ensure_qq_adapter(tmp_path) is False


def test_ensure_qq_adapter_requires_existing_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_launch(tmp_path, workdir=str(tmp_path / 'missing-dir'))
    monkeypatch.delenv('MAIBOT_INTERCEPT_SEND', raising=False)
    monkeypatch.setattr(qq_adapter_boot.os, 'name', 'nt')
    assert qq_adapter_boot.ensure_qq_adapter(tmp_path) is False


def test_ensure_qq_adapter_detects_running_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """端口已被占用说明协议端在跑，不重复拉起。"""

    server = socket.socket()
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    port = server.getsockname()[1]
    try:
        _write_launch(tmp_path, ws_port=port)
        monkeypatch.delenv('MAIBOT_INTERCEPT_SEND', raising=False)
        monkeypatch.setattr(qq_adapter_boot.os, 'name', 'nt')

        def _boom(*args, **kwargs):  # pragma: no cover - 被调用即失败
            raise AssertionError('端口已占用时不应重复拉起协议端')

        monkeypatch.setattr(qq_adapter_boot.subprocess, 'Popen', _boom)
        assert qq_adapter_boot.ensure_qq_adapter(tmp_path) is False
    finally:
        server.close()


def test_port_in_use_false_for_closed_port() -> None:
    probe = socket.socket()
    probe.bind(('127.0.0.1', 0))
    port = probe.getsockname()[1]
    probe.close()
    assert qq_adapter_boot._port_in_use(port) is False


def test_latest_login_url_strips_trailing_punctuation(tmp_path: Path) -> None:
    """日志里的中文括号不能跟着 URL 一起被复制出去。"""

    log_dir = tmp_path / '.napuketto' / '10001' / 'logs'
    log_dir.mkdir(parents=True)
    (log_dir / 'napuketto.log').write_text(
        '{"msg":"请扫描二维码登录（保存 C:\\qr.png | URL: '
        'https://txz.qq.com/p?k=1eAVLuEfDF3KB7V3rOwnB*3jshvPkN91&f=1600001604）"}',
        encoding='utf8',
    )
    url = qq_adapter_boot._latest_login_url(tmp_path, '10001')
    assert url == 'https://txz.qq.com/p?k=1eAVLuEfDF3KB7V3rOwnB*3jshvPkN91&f=1600001604'


def test_latest_login_url_empty_without_log(tmp_path: Path) -> None:
    assert qq_adapter_boot._latest_login_url(tmp_path, '10001') == ''


def test_stale_process_is_recycled_for_a_fresh_qr(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """进程活着但没登录时必须重启，否则用户拿到的是过期二维码。"""

    _write_launch(tmp_path)
    (tmp_path / 'depends-data' / 'qq-adapter' / 'adapter.pid').write_text('424242', encoding='utf8')
    monkeypatch.delenv('MAIBOT_INTERCEPT_SEND', raising=False)
    monkeypatch.setattr(qq_adapter_boot.os, 'name', 'nt')
    monkeypatch.setattr(qq_adapter_boot, '_pid_alive', lambda pid: True)
    monkeypatch.setattr(qq_adapter_boot, '_port_in_use', lambda *args, **kwargs: False)

    killed: list[int] = []
    monkeypatch.setattr(qq_adapter_boot, '_terminate_tree', lambda pid: killed.append(pid))

    spawned: list[list[str]] = []

    class _FakeProcess:
        pid = 999001

    def _fake_popen(args, **kwargs):
        spawned.append(args)
        return _FakeProcess()

    monkeypatch.setattr(qq_adapter_boot.subprocess, 'Popen', _fake_popen)
    monkeypatch.setattr(qq_adapter_boot.threading, 'Thread', lambda *a, **k: type(
        'T', (), {'start': lambda self: None})())

    assert qq_adapter_boot.ensure_qq_adapter(tmp_path) is True
    assert killed == [424242]
    assert spawned and spawned[0][:2] == ['cmd.exe', '/c']


def test_live_process_with_open_port_is_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """已登录（端口在听）时不重启进程。"""

    _write_launch(tmp_path)
    (tmp_path / 'depends-data' / 'qq-adapter' / 'adapter.pid').write_text('424243', encoding='utf8')
    monkeypatch.delenv('MAIBOT_INTERCEPT_SEND', raising=False)
    monkeypatch.setattr(qq_adapter_boot.os, 'name', 'nt')
    monkeypatch.setattr(qq_adapter_boot, '_pid_alive', lambda pid: True)
    monkeypatch.setattr(qq_adapter_boot, '_port_in_use', lambda *args, **kwargs: True)

    killed: list[int] = []
    monkeypatch.setattr(qq_adapter_boot, '_terminate_tree', lambda pid: killed.append(pid))
    monkeypatch.setattr(qq_adapter_boot.subprocess, 'Popen', lambda *a, **k: pytest.fail('不应重启'))
    started: list[Path] = []
    monkeypatch.setattr(qq_adapter_boot, '_start_watcher', lambda root, settings: started.append(root))

    assert qq_adapter_boot.ensure_qq_adapter(tmp_path) is False
    assert killed == []
    assert started == [tmp_path], '协议端已在运行时仍要挂上登录守护，登录失效后自动重新出码'


def _fake_window_process(pid: int = 4242):
    """最小化的 Popen 替身：只提供 pid / poll。"""

    class _FakeProcess:
        def __init__(self) -> None:
            self.pid = pid

        def poll(self):
            return None

    return _FakeProcess()


def test_qr_window_is_disabled_while_intercepting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MAIBOT_INTERCEPT_SEND', '1')
    assert qq_adapter_boot._qr_window_supported() is False
    assert qq_adapter_boot._open_qr_window(tmp_path, tmp_path / 'qrcode.png', '10001') is None


def test_open_qr_window_spawns_a_new_console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('MAIBOT_INTERCEPT_SEND', raising=False)
    monkeypatch.setattr(qq_adapter_boot.os, 'name', 'nt')
    monkeypatch.setattr(qq_adapter_boot, '_QR_WINDOWS', {})
    script = tmp_path / 'scripts' / 'show_qq_qr.ps1'
    script.parent.mkdir(parents=True)
    script.write_text('# fixture', encoding='utf8')

    captured: dict = {}

    def _fake_popen(args, **kwargs):
        captured['args'] = list(args)
        captured['flags'] = kwargs.get('creationflags')
        return _fake_window_process()

    monkeypatch.setattr(qq_adapter_boot.subprocess, 'Popen', _fake_popen)
    process = qq_adapter_boot._open_qr_window(tmp_path, tmp_path / 'qrcode.png', '10001')

    assert process is not None
    assert captured['args'][:3] == ['cmd.exe', '/c', 'powershell.exe']
    assert '-File' in captured['args'] and '-ImagePath' in captured['args']
    assert '10001' in captured['args']
    assert captured['flags'] == getattr(qq_adapter_boot.subprocess, 'CREATE_NEW_CONSOLE', 0)
    assert qq_adapter_boot._QR_WINDOWS[str(tmp_path)] is process


def test_open_qr_window_skips_when_script_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('MAIBOT_INTERCEPT_SEND', raising=False)
    monkeypatch.setattr(qq_adapter_boot.os, 'name', 'nt')
    monkeypatch.setattr(qq_adapter_boot, '_QR_WINDOWS', {})
    monkeypatch.setattr(
        qq_adapter_boot.subprocess, 'Popen', lambda *a, **k: pytest.fail('不应拉起窗口')
    )
    assert qq_adapter_boot._open_qr_window(tmp_path, tmp_path / 'qrcode.png', '10001') is None


def test_close_qr_window_kills_the_tracked_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    process = _fake_window_process(5150)
    monkeypatch.setattr(qq_adapter_boot, '_QR_WINDOWS', {str(tmp_path): process})
    killed: list[int] = []
    monkeypatch.setattr(qq_adapter_boot, '_terminate_tree', lambda pid: killed.append(pid))

    qq_adapter_boot._close_qr_window(tmp_path)

    assert killed == [5150]
    assert str(tmp_path) not in qq_adapter_boot._QR_WINDOWS





@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch):
    """把 watcher 的轮询间隔压成 0，避免测试等真秒数。"""

    monkeypatch.setattr(qq_adapter_boot.time, 'sleep', lambda seconds: None)


def _prepare_qr(tmp_path: Path, uin: str = '10001') -> Path:
    qr_dir = tmp_path / '.napuketto' / uin / 'cache'
    qr_dir.mkdir(parents=True, exist_ok=True)
    qr = qr_dir / 'qrcode.png'
    qr.write_bytes(b'fixture-qr')
    return qr


def _state_file(tmp_path: Path) -> Path:
    return tmp_path / 'depends-data' / 'qq-adapter' / 'status.json'


def test_watch_login_pops_the_window_and_closes_it_after_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_sleep: None
) -> None:
    uin = '10001'
    _prepare_qr(tmp_path, uin)

    opened: list[str] = []
    closed: list[str] = []
    monkeypatch.setattr(qq_adapter_boot, '_latest_login_url', lambda workdir, uin: '')
    monkeypatch.setattr(
        qq_adapter_boot, '_open_qr_window', lambda root, path, uin: opened.append(str(path))
    )
    monkeypatch.setattr(qq_adapter_boot, '_close_qr_window', lambda root: closed.append(str(root)))

    stop = threading.Event()
    calls = {'n': 0}

    def _confirmed(*args, **kwargs) -> bool:
        calls['n'] += 1
        if calls['n'] >= 3:
            stop.set()
            return True
        return False

    monkeypatch.setattr(qq_adapter_boot, '_login_confirmed', _confirmed)

    qq_adapter_boot._watch_login(
        tmp_path, tmp_path, uin, 13011, 13010, timeout=None, show_window=True, stop_event=stop
    )

    assert opened == [str(tmp_path / 'depends-data' / 'qq-adapter' / 'qrcode.png')]
    assert closed, '登录成功后必须关闭二维码窗口'
    state = json.loads(_state_file(tmp_path).read_text(encoding='utf8'))
    assert state['state'] == 'logged_in'


def test_watch_login_keeps_the_window_closed_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_sleep: None
) -> None:
    uin = '10001'
    _prepare_qr(tmp_path, uin)

    opened: list[str] = []
    monkeypatch.setattr(qq_adapter_boot, '_latest_login_url', lambda workdir, uin: '')
    monkeypatch.setattr(
        qq_adapter_boot, '_open_qr_window', lambda root, path, uin: opened.append(str(path))
    )
    monkeypatch.setattr(qq_adapter_boot, '_close_qr_window', lambda root: None)

    stop = threading.Event()
    calls = {'n': 0}

    def _confirmed(*args, **kwargs) -> bool:
        calls['n'] += 1
        if calls['n'] >= 3:
            stop.set()
            return True
        return False

    monkeypatch.setattr(qq_adapter_boot, '_login_confirmed', _confirmed)

    qq_adapter_boot._watch_login(
        tmp_path, tmp_path, uin, 13011, 13010, timeout=None, show_window=False, stop_event=stop
    )
    assert opened == []


def test_watch_login_repops_the_window_when_the_qr_is_refreshed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_sleep: None
) -> None:
    """二维码过期换新码时：先关旧窗口，再弹新窗口。"""

    uin = '10001'
    qr = _prepare_qr(tmp_path, uin)

    opened: list[str] = []
    closed: list[str] = []
    monkeypatch.setattr(qq_adapter_boot, '_latest_login_url', lambda workdir, uin: '')
    monkeypatch.setattr(
        qq_adapter_boot, '_open_qr_window', lambda root, path, uin: opened.append(str(path))
    )
    monkeypatch.setattr(qq_adapter_boot, '_close_qr_window', lambda root: closed.append(str(root)))

    stop = threading.Event()
    calls = {'n': 0}

    def _confirmed(*args, **kwargs) -> bool:
        calls['n'] += 1
        if calls['n'] == 3:
            qr.write_bytes(b'second-qr')
            stamp = qr.stat().st_mtime + 5
            os.utime(qr, (stamp, stamp))
        if calls['n'] >= 5:
            stop.set()
        return False

    monkeypatch.setattr(qq_adapter_boot, '_login_confirmed', _confirmed)

    qq_adapter_boot._watch_login(
        tmp_path, tmp_path, uin, 13011, 13010, timeout=None, show_window=True, stop_event=stop
    )

    assert len(opened) == 2, '新二维码必须重新弹窗'
    assert closed, '换码前必须先关掉过期窗口'
    published = (tmp_path / 'depends-data' / 'qq-adapter' / 'qrcode.png').read_bytes()
    assert published == b'second-qr'


def test_watch_login_rearms_after_login_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_sleep: None
) -> None:
    """掉线后写 login_lost，并在协议端重新出码时再次弹窗，不需要重启 bot。"""

    uin = '10001'
    qr = _prepare_qr(tmp_path, uin)

    opened: list[str] = []
    states: list[str] = []
    monkeypatch.setattr(qq_adapter_boot, '_latest_login_url', lambda workdir, uin: '')
    monkeypatch.setattr(
        qq_adapter_boot, '_open_qr_window', lambda root, path, uin: opened.append(str(path))
    )
    monkeypatch.setattr(qq_adapter_boot, '_close_qr_window', lambda root: None)
    monkeypatch.setattr(
        qq_adapter_boot, '_write_state', lambda root, state: states.append(state['state'])
    )

    stop = threading.Event()
    calls = {'n': 0}

    def _confirmed(*args, **kwargs) -> bool:
        calls['n'] += 1
        if calls['n'] <= 2:
            return True
        if calls['n'] == 3:
            return False
        if calls['n'] == 4:
            qr.write_bytes(b'reissued-qr')
            stamp = qr.stat().st_mtime + 5
            os.utime(qr, (stamp, stamp))
            return False
        stop.set()
        return False

    monkeypatch.setattr(qq_adapter_boot, '_login_confirmed', _confirmed)

    qq_adapter_boot._watch_login(
        tmp_path, tmp_path, uin, 13011, 13010, timeout=None, show_window=True, stop_event=stop
    )

    assert 'logged_in' in states and 'login_lost' in states
    assert 'waiting_scan' in states, '重新出码后应再次弹出二维码'
    assert len(opened) == 2


def test_watch_login_respawns_a_dead_adapter_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_sleep: None
) -> None:
    """协议端进程退出后由守护线程自愈，不必等用户重启 bot。"""

    uin = '10001'
    _prepare_qr(tmp_path, uin)

    monkeypatch.setattr(qq_adapter_boot, '_latest_login_url', lambda workdir, uin: '')
    monkeypatch.setattr(qq_adapter_boot, '_open_qr_window', lambda root, path, uin: None)
    monkeypatch.setattr(qq_adapter_boot, '_close_qr_window', lambda root: None)
    monkeypatch.setattr(qq_adapter_boot, '_read_pid', lambda root: 4242)
    monkeypatch.setattr(qq_adapter_boot, '_pid_alive', lambda pid: False)

    respawns: list[int] = []

    def _respawn() -> int:
        respawns.append(1)
        return 1

    stop = threading.Event()
    calls = {'n': 0}

    def _confirmed(*args, **kwargs) -> bool:
        calls['n'] += 1
        if calls['n'] >= 2:
            stop.set()
        return False

    monkeypatch.setattr(qq_adapter_boot, '_login_confirmed', _confirmed)

    qq_adapter_boot._watch_login(
        tmp_path, tmp_path, uin, 13011, 13010,
        timeout=None, show_window=True, stop_event=stop, respawn=_respawn,
    )

    assert respawns == [1], '进程死亡后必须重新拉起一次'


def test_start_watcher_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """同一个 root 只允许一个守护线程，避免重复弹窗。"""

    started: list[object] = []

    class _FakeThread:
        daemon = True

        def __init__(self, *args, **kwargs) -> None:
            started.append(self)

        def start(self) -> None:
            pass

    monkeypatch.setattr(qq_adapter_boot.threading, 'Thread', _FakeThread)
    monkeypatch.setattr(qq_adapter_boot, '_WATCHERS', {})

    settings = {'uin': '10001', 'workdir': str(tmp_path), 'ws_port': 13011, 'http_port': 13010}
    qq_adapter_boot._start_watcher(tmp_path, settings)
    qq_adapter_boot._start_watcher(tmp_path, settings)

    assert len(started) == 1
    qq_adapter_boot._stop_all_watchers()



def test_publish_qr_closes_the_old_window_before_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """窗口进程锁着 PNG：必须先关窗再复制，否则 WinError 32。"""

    qr_dir = tmp_path / '.napuketto' / '10001' / 'cache'
    qr_dir.mkdir(parents=True)
    (qr_dir / 'qrcode.png').write_bytes(b'fresh')

    order: list[str] = []
    monkeypatch.setattr(qq_adapter_boot, '_close_qr_window', lambda root: order.append('close'))
    monkeypatch.setattr(qq_adapter_boot, '_open_qr_window', lambda root, path, uin: order.append('open'))
    monkeypatch.setattr(qq_adapter_boot, '_latest_login_url', lambda workdir, uin: '')
    real_copy = qq_adapter_boot.shutil.copy2

    def _copy(src, dst, *args, **kwargs):
        order.append('copy')
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(qq_adapter_boot.shutil, 'copy2', _copy)

    assert qq_adapter_boot._publish_qr(tmp_path, tmp_path, '10001', True) is True
    assert order == ['close', 'copy', 'open']


def test_publish_qr_falls_back_when_the_target_file_stays_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """句柄迟迟不释放时改用唯一文件名，保证新码一定能发布。"""

    qr_dir = tmp_path / '.napuketto' / '10001' / 'cache'
    qr_dir.mkdir(parents=True)
    (qr_dir / 'qrcode.png').write_bytes(b'fresh')

    monkeypatch.setattr(qq_adapter_boot, '_close_qr_window', lambda root: None)
    monkeypatch.setattr(qq_adapter_boot, '_open_qr_window', lambda root, path, uin: None)
    monkeypatch.setattr(qq_adapter_boot, '_latest_login_url', lambda workdir, uin: '')
    monkeypatch.setattr(qq_adapter_boot.time, 'sleep', lambda seconds: None)
    real_copy = qq_adapter_boot.shutil.copy2

    def _copy(src, dst, *args, **kwargs):
        if Path(dst).name == 'qrcode.png':
            raise OSError(32, 'The process cannot access the file')
        return real_copy(src, dst, *args, **kwargs)

    monkeypatch.setattr(qq_adapter_boot.shutil, 'copy2', _copy)

    assert qq_adapter_boot._publish_qr(tmp_path, tmp_path, '10001', True) is True
    state = json.loads(_state_file(tmp_path).read_text(encoding='utf8'))
    assert state['state'] == 'waiting_scan'
    published = Path(state['qr_path'])
    assert published.name.startswith('qrcode-')
    assert published.read_bytes() == b'fresh'



def test_login_confirmed_ignores_fast_login_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QQ 启动时的“快速登录成功”不能当成 OneBot 已就绪。"""

    uin = '10001'
    _prepare_qr(tmp_path, uin)
    log_dir = tmp_path / '.napuketto' / uin / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / 'napuketto.log').write_text(
        '{"level":30,"msg":"快速登录成功","uin":"10001"}' + chr(10), encoding='utf8'
    )
    monkeypatch.setattr(qq_adapter_boot, '_port_in_use', lambda *a, **k: False)

    assert qq_adapter_boot._login_confirmed(tmp_path, uin, 13011, 13010) is False


def test_login_confirmed_accepts_qr_success_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """扫码成功的 QR 状态仍然可以作为端口之外的兜底判据。"""

    uin = '10001'
    _prepare_qr(tmp_path, uin)
    log_dir = tmp_path / '.napuketto' / uin / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / 'napuketto.log'
    log.write_text('{"level":30,"state":"success","msg":"QR 登录状态"}' + chr(10), encoding='utf8')
    stamp = log.stat().st_mtime + 5
    os.utime(log, (stamp, stamp))
    monkeypatch.setattr(qq_adapter_boot, '_port_in_use', lambda *a, **k: False)

    assert qq_adapter_boot._login_confirmed(tmp_path, uin, 13011, 13010) is True



def test_read_pid_tolerates_a_utf8_bom(tmp_path: Path) -> None:
    """Out-File -Encoding utf8 会写 BOM，不能因此把存活进程当成已死。"""

    state = tmp_path / 'depends-data' / 'qq-adapter'
    state.mkdir(parents=True)
    (state / 'adapter.pid').write_bytes('\ufeff424242'.encode('utf-8'))

    assert qq_adapter_boot._read_pid(tmp_path) == 424242
def test_session_start_hint_prefers_runtime_json_started_at(tmp_path: Path) -> None:
    import datetime as _dt

    base = tmp_path / '.napuketto' / '10001'
    base.mkdir(parents=True)
    (base / 'runtime.json').write_text(
        json.dumps({'uin': '10001', 'startedAt': '2026-11-18T17:43:42.911Z'}), encoding='utf8'
    )
    (base / 'instance.lock').write_text('{}', encoding='utf8')

    hint = qq_adapter_boot._session_start_hint(tmp_path, '10001')

    expected = _dt.datetime.fromisoformat('2026-11-18T17:43:42.911+00:00').timestamp()
    assert hint is not None and abs(hint - expected) < 1.0


def test_session_start_hint_falls_back_to_instance_lock(tmp_path: Path) -> None:
    base = tmp_path / '.napuketto' / '10001'
    base.mkdir(parents=True)
    lock = base / 'instance.lock'
    lock.write_text('{}', encoding='utf8')

    assert qq_adapter_boot._session_start_hint(tmp_path, '10001') == lock.stat().st_mtime


def test_session_start_hint_is_none_without_session_files(tmp_path: Path) -> None:
    assert qq_adapter_boot._session_start_hint(tmp_path, '10001') is None


def test_watch_login_skips_a_qr_left_over_from_an_earlier_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fast_sleep: None
) -> None:
    """A QR written by a dead session must never be popped as if it were valid."""

    import datetime as _dt
    import time as _time

    uin = '10001'
    qr = _prepare_qr(tmp_path, uin)
    stale = _time.time() - 3600
    os.utime(qr, (stale, stale))

    base = tmp_path / '.napuketto' / uin
    (base / 'runtime.json').write_text(
        json.dumps({'uin': uin, 'startedAt': _dt.datetime.now(_dt.timezone.utc).isoformat().replace('+00:00', 'Z')}),
        encoding='utf8',
    )

    opened: list[str] = []
    monkeypatch.setattr(qq_adapter_boot, '_latest_login_url', lambda workdir, uin: '')
    monkeypatch.setattr(
        qq_adapter_boot, '_open_qr_window', lambda root, path, uin: opened.append(str(path))
    )
    monkeypatch.setattr(qq_adapter_boot, '_close_qr_window', lambda root: None)

    stop = threading.Event()
    calls = {'n': 0}

    def _confirmed(*args, **kwargs) -> bool:
        calls['n'] += 1
        if calls['n'] >= 3:
            stop.set()
        return False

    monkeypatch.setattr(qq_adapter_boot, '_login_confirmed', _confirmed)

    qq_adapter_boot._watch_login(
        tmp_path, tmp_path, uin, 13011, 13010, timeout=None, show_window=True, stop_event=stop
    )

    assert opened == [], 'stale QR from an earlier session must not be shown'
    assert not _state_file(tmp_path).exists(), 'skipped QR must not report waiting_scan'



def test_login_confirmed_rejects_log_success_without_qr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """QR 图片缺失时不能凭日志里的 success 认定"已登录"。

    2026-11-19 实测：协议端进程被杀、端口已断，只因二维码文件被清理过，
    _login_confirmed 返回 True，watcher 永远不再拉起进程，QQ 侧彻底失聪。
    """

    uin = '10001'
    log_dir = tmp_path / '.napuketto' / uin / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / 'napuketto.log'
    log.write_text('{"level":30,"state":"success","msg":"QR 登录状态"}' + chr(10), encoding='utf8')
    monkeypatch.setattr(qq_adapter_boot, '_port_in_use', lambda *a, **k: False)

    assert qq_adapter_boot._login_confirmed(tmp_path, uin, 13011, 13010) is False
