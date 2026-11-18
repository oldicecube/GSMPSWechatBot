"""core.qq_adapter_boot 的启动判定与安全旁路测试。

这些用例只验证「要不要拉起外部协议端」的判定逻辑，绝不真的启动
NapukettoQQ 进程，也不占用 13010 / 13011 端口。
"""

from __future__ import annotations

import importlib.util
import json
import socket
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

    assert qq_adapter_boot.ensure_qq_adapter(tmp_path) is False
    assert killed == []
