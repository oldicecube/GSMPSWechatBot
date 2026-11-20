from pathlib import Path
import importlib.util
import os

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('napcat_boot_lifecycle', ROOT / 'core/napcat_boot.py')
napcat_boot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(napcat_boot)


def test_ensure_napcat_records_owner_and_passes_parent_pid(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / 'depends-data/napcat/config'
    cfg.mkdir(parents=True)
    (cfg / 'maibot-launch.json').write_text('{"autostart": true, "account": "12345"}', encoding='utf8')
    (tmp_path / 'scripts').mkdir()
    calls = []
    monkeypatch.setattr(napcat_boot.os, 'name', 'nt')
    monkeypatch.delenv('MAIBOT_INTERCEPT_SEND', raising=False)
    monkeypatch.setattr(napcat_boot.subprocess, 'Popen', lambda *args, **kwargs: calls.append((args, kwargs)))
    assert napcat_boot.ensure_napcat(tmp_path) is True
    assert (cfg / 'owner-pid.txt').read_text(encoding='utf8').strip() == str(os.getpid())
    command = calls[0][0][0]
    assert command[command.index('-ParentPid') + 1] == str(os.getpid())


def test_stop_napcat_kills_owned_processes_and_cleans_state(tmp_path: Path, monkeypatch) -> None:
    cfg = tmp_path / 'depends-data/napcat/config'
    cfg.mkdir(parents=True)
    (cfg / 'owner-pid.txt').write_text('4242', encoding='utf8')
    (cfg / 'login-watch.lock').write_text('', encoding='utf8')
    recorded = []
    monkeypatch.setattr(napcat_boot.os, 'name', 'nt')
    monkeypatch.setattr(napcat_boot.subprocess, 'run', lambda *args, **kwargs: recorded.append((args, kwargs)))
    assert napcat_boot.stop_napcat(tmp_path) is True
    command = ' '.join(recorded[0][0][0])
    assert 'napcat_login_watch.py' in command
    assert 'NapCatWinBootMain.exe' in command
    assert 'QQ.exe' in command
    assert not (cfg / 'owner-pid.txt').exists()
    assert not (cfg / 'login-watch.lock').exists()


def test_stop_napcat_is_noop_outside_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(napcat_boot.os, 'name', 'posix')
    assert napcat_boot.stop_napcat(tmp_path) is False
