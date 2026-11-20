"""Start and stop the NapCat QQ adapter together with the bot lifecycle."""
import json
import os
from pathlib import Path
import subprocess

OWNER_RELATIVE = Path('depends-data/napcat/config/owner-pid.txt')


def _no_window() -> int:
    return int(getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def write_owner_pid(root):
    """Record which bot process currently owns the QQ adapter."""

    path = Path(root) / OWNER_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()) + '\n', encoding='utf8')
    return path


def ensure_napcat(root):
    root=Path(root)
    path=root/'depends-data/napcat/config/maibot-launch.json'
    if os.name!='nt' or os.environ.get('MAIBOT_INTERCEPT_SEND')=='1' or not path.is_file():return False
    settings=json.loads(path.read_text(encoding='utf8'))
    if settings.get('autostart') is not True:return False
    try:
        write_owner_pid(root)
    except OSError:
        pass
    script=root/'scripts/start_napcat.ps1'
    with (path.parent/'bootstrap.log').open('ab') as output:
        subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
                          '-IfConfigured','-Background','-ParentPid',str(os.getpid())],cwd=str(root),
                         stdin=subprocess.DEVNULL,stdout=output,stderr=subprocess.STDOUT,
                         creationflags=_no_window())
    return True


def stop_napcat(root):
    """Stop every QQ adapter process owned by this bot.

    The login guard is terminated first: otherwise it treats the vanished client
    as a disconnect and starts it again before this call returns.
    """

    if os.name != 'nt':
        return False
    root = Path(root)
    napcat_root = root / 'depends-data/napcat'
    qq_path = napcat_root / 'qq-client/Files/QQ.exe'
    scripts = root / 'scripts'
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        "$root='" + str(scripts) + "';"
        "$napcat='" + str(napcat_root) + "';"
        "$qq='" + str(qq_path) + "';"
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -and ("
        "$_.CommandLine.Contains($root + '\\napcat_login_watch.py') -or "
        "$_.CommandLine.Contains($root + '\\napcat_lan_webui.py')) } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force };"
        "Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
        "Where-Object { $_.CommandLine -and ("
        "$_.CommandLine.Contains($root + '\\show_qq_qr.ps1') -or "
        "$_.CommandLine.Contains($root + '\\start_napcat.ps1') -or "
        "$_.CommandLine.Contains($root + '\\napcat_parent_watch.ps1')) } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force };"
        "Get-CimInstance Win32_Process -Filter \"Name='NapCatWinBootMain.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($napcat) } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force };"
        "Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -notmatch '--type=' -and "
        "$_.CommandLine.Contains($qq) } | "
        "ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null }"
    )
    try:
        subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                        '-Command', script], cwd=str(root), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25,
                       creationflags=_no_window())
    except Exception:
        return False
    try:
        (napcat_root / 'config' / 'owner-pid.txt').unlink(missing_ok=True)
        (napcat_root / 'config' / 'login-watch.lock').unlink(missing_ok=True)
    except OSError:
        pass
    return True
