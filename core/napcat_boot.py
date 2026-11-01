"""Start the separately managed QQ client on a user-initiated bot startup."""
import json
import os
from pathlib import Path
import subprocess


def ensure_napcat(root):
    root=Path(root)
    path=root/'depends-data/napcat/config/maibot-launch.json'
    if os.name!='nt' or os.environ.get('MAIBOT_INTERCEPT_SEND')=='1' or not path.is_file():return False
    settings=json.loads(path.read_text(encoding='utf8'))
    if settings.get('autostart') is not True:return False
    script=root/'scripts/start_napcat.ps1'
    with (path.parent/'bootstrap.log').open('ab') as output:
        subprocess.Popen(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(script),
                          '-IfConfigured','-Background'],cwd=str(root),stdin=subprocess.DEVNULL,
                         stdout=output,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    return True
