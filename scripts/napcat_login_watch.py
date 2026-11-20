"""Local NapCat login recovery. Never changes chat policies or sends messages."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[1]


class LoginClient:
    def __init__(self, root=ROOT):
        self.root = root
        self.config = json.loads((root/'depends-data/napcat/config/webui.json').read_text(encoding='utf8'))
        self.base = 'http://127.0.0.1:'+str(int(self.config['port']))
        self.credential = ''

    def call(self, path, payload=None):
        headers={'Content-Type':'application/json'}
        if self.credential:headers['Authorization']='Bearer '+self.credential
        request=urllib.request.Request(self.base+'/api/'+path,
            data=json.dumps(payload or {}).encode(),headers=headers,method='POST')
        # Credentials stay on loopback; never follow a redirect to another host.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args,**kwargs):return None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect)
        with opener.open(request,timeout=5) as response:
            result=json.loads(response.read(262144))
        if result.get('code') != 0:raise RuntimeError('NapCat request rejected: '+path)
        return result.get('data')

    def status(self):
        if not self.credential:
            result=self.call('auth/login',{'hash':hashlib.sha256((self.config['token']+'.napcat').encode()).hexdigest()})
            self.credential=(result or {}).get('Credential','')
            if not self.credential:raise RuntimeError('Interactive WebUI authentication required')
        return self.call('QQLogin/CheckLoginStatus') or {}

    def remember(self):
        info=self.call('QQLogin/GetQQLoginInfo') or {}
        account=str(info.get('uin') or info.get('user_id') or '')
        if not account.isascii() or not account.isdecimal() or int(account)<=0:return False
        path=self.root/'depends-data/napcat/config/maibot-launch.json'
        config=json.loads(path.read_text(encoding='utf8')) if path.exists() else {}
        config.update(account=account,autostart=True)
        temporary=path.with_suffix('.tmp');temporary.write_text(json.dumps(config)+'\n',encoding='utf8');temporary.replace(path)
        self.call('QQLogin/SetQuickLoginQQ',{'uin':account})
        return True

    def show_qr(self, state, opener=webbrowser.open):
        # Do not interrupt an accepted QR scan or the native initialization.
        if state.get('isLogin') or state.get('qrLoginAccepted') or state.get('loginPhase') in {'initializing','qrcode_scanned','reconnecting'}:
            return False
        if not state.get('qrcodeurl'):
            self.call('QQLogin/RefreshQRcode')
        return bool(opener(self.base+'/webui?token='+urllib.parse.quote(self.config['token'],safe='')))


def watch(client, *, seconds=600, interval=2, now=time.monotonic, sleep=time.sleep, opener=webbrowser.open):
    deadline=now()+seconds;started=now();shown=False;last_error=''
    while now()<deadline:
        try:
            state=client.status()
            if state.get('isLogin'):
                client.remember()
                return {'online':True,'qr_opened':shown}
            if not shown and (state.get('loginError') or now()-started>=15):
                shown=client.show_qr(state,opener=opener)
        except Exception as exc:
            last_error=type(exc).__name__  # Never print token-bearing URLs or credentials.
            client.credential=''
        sleep(interval)
    return {'online':False,'qr_opened':shown,'error':last_error or 'LoginPending'}



QR_IMAGE = ROOT / 'depends-data' / 'napcat' / 'cache' / 'qrcode.png'
QR_WINDOW_SCRIPT = ROOT / 'scripts' / 'show_qq_qr.ps1'


def _close_window(proc) -> None:
    """关掉上一个二维码窗口（尽力而为）。"""

    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def _open_window(proc, image, uin, popen):
    """新开一个二维码窗口，返回新的进程句柄。"""

    _close_window(proc)
    if not image.is_file():
        return None
    args = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            str(QR_WINDOW_SCRIPT), '-ImagePath', str(image), '-Scale', '4']
    if uin:
        args += ['-Uin', str(uin)]
    try:
        return popen(args, cwd=str(ROOT))
    except Exception:
        return None


def _append_event(event, *, root=ROOT):
    """记录登录状态变化，便于定位“掉线”原因；绝不写入凭据。"""

    path = root / 'depends-data/napcat/config/login-events.jsonl'
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf8') as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + '\n')
    except OSError:
        pass


CONSOLE_LOG_RELATIVE = Path('depends-data/napcat/config/napcat-console.log')


def _console_tail(root=ROOT, limit=262144):
    path = root / CONSOLE_LOG_RELATIVE
    try:
        with path.open('rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - limit))
            return handle.read().decode('utf8', 'replace')
    except OSError:
        return ''


def _drop_reason(tail=''):
    for line in reversed([item.strip() for item in tail.splitlines() if item.strip()][-600:]):
        lowered = line.lower()
        if ('登录态已失效' in line or '被踢' in line or '另一设备' in line
                or 'kickedoffline' in lowered or 'kicked' in lowered):
            return line[-240:]
    return ''


def _latest_login_url(root=ROOT):
    """从 NapCat 控制台日志里取最新二维码链接，避免显示过期二维码。"""

    marker = 'https://txz.qq.com/p?k='
    allowed = set('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:/?=&%-._~*+!$,;@#[]')
    latest = ''
    for line in _console_tail(root).splitlines():
        index = line.find(marker)
        if index < 0:
            continue
        candidate = line[index:]
        end = len(candidate)
        for position, char in enumerate(candidate):
            if char not in allowed:
                end = position
                break
        latest = candidate[:end].strip() or latest
    return latest


def _render_login_qr(url, target):
    """把最新登录链接重新编码成二维码图片，返回图片路径。"""

    import qrcode
    if target.is_file() and target.stat().st_size > 0:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    code = qrcode.QRCode(border=2, box_size=10)
    code.add_data(url)
    code.make(fit=True)
    code.make_image(fill_color='black', back_color='white').save(target)
    return target


def _webui_alive(host='127.0.0.1', port=6099, timeout=1.0):
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _relaunch_napcat(*, root=ROOT, popen=None):
    """后台重启 NapCat/QQ 客户端，并把控制台日志保留下来供下次掉线定位。"""

    import subprocess
    popen = popen or subprocess.Popen
    log = root / CONSOLE_LOG_RELATIVE
    log.parent.mkdir(parents=True, exist_ok=True)
    inner = ('powershell.exe -NoProfile -ExecutionPolicy Bypass -File "'
             + str(root / 'scripts/start_napcat.ps1') + '" -NoWatch'
             + ' >> "' + str(log) + '" 2>&1')
    return popen('cmd.exe /c ' + inner, cwd=str(root), stdin=subprocess.DEVNULL,
                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def _terminate_qq_client(root=ROOT):
    """结束本仓库内由 NapCat 注入的 QQ 主进程，用于重新生成登录二维码。"""

    import subprocess
    qq_path = str(root / 'depends-data/napcat/qq-client/Files/QQ.exe')
    napcat_root = str(root / 'depends-data/napcat')
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='QQ.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine -notmatch '--type=' -and "
        "$_.CommandLine.Contains('" + qq_path + "') } | "
        "ForEach-Object { taskkill /PID $_.ProcessId /T /F | Out-Null }; "
        "Get-CimInstance Win32_Process -Filter \"Name='NapCatWinBootMain.exe'\" | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.Contains('" + napcat_root + "') } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    try:
        subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                        '-Command', script], cwd=str(root), stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=20, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except Exception:
        pass


def _restart_client_for_qr(root, previous_url='', *, popen=None, wait=40.0,
                           now=time.monotonic, sleep=time.sleep):
    """重启 QQ 客户端以强制生成新的登录二维码，返回新的登录链接。"""

    for _attempt in range(2):
        _terminate_qq_client(root)
        sleep(6)
        try:
            _relaunch_napcat(root=root, popen=popen)
        except Exception:
            pass
        ready_deadline = now() + 25
        while now() < ready_deadline:
            sleep(1)
            if _webui_alive():
                break
        else:
            continue
        deadline = now() + wait
        while now() < deadline:
            sleep(1)
            candidate = _latest_login_url(root)
            if candidate and candidate != previous_url:
                return candidate
        candidate = _latest_login_url(root)
        if candidate and candidate != previous_url:
            return candidate
        return ''
    return ''


def _refresh_login_qr(client, root, previous_url='', *, wait=4.0, interval=0.5,
                      now=time.monotonic, sleep=time.sleep):
    """重新打开二维码窗口前，主动让 NapCat 生成一张新的登录码。"""

    try:
        client.call('QQLogin/RefreshQRcode')
    except Exception:
        return ''
    deadline = now() + wait
    while now() < deadline:
        sleep(interval)
        candidate = _latest_login_url(root)
        if candidate and candidate != previous_url:
            return candidate
    return ''


def qr_window_loop(client, *, seconds=0, interval=3, now=time.monotonic,
                   sleep=time.sleep, popen=None, image=None, root=ROOT, port=6099):
    """常驻守护：登录后继续监控；掉线时重弹二维码，客户端退出时自动拉起。"""

    import subprocess
    popen = popen or subprocess.Popen
    image = image or root / 'depends-data/napcat/cache/qrcode.png'
    status_path = root / 'depends-data/napcat/config/login-watch-status.json'
    deadline = None if not seconds else now() + seconds
    proc = None
    last_stamp = None
    shown = 0
    opened_once = False
    logged_in = None
    offline_since = None
    last_relaunch = None
    last_error = ''
    debug = bool(os.environ.get('NAPCAT_GUARD_DEBUG'))
    while deadline is None or now() < deadline:
        if debug:
            print('guard loop start', time.strftime('%H:%M:%S'), flush=True)
        online = False
        state = {}
        try:
            state = client.status()
            online = bool(state.get('isLogin'))
        except Exception as exc:
            last_error = type(exc).__name__
            client.credential = ''
        if online != logged_in:
            stamp = time.strftime('%Y-%m-%dT%H:%M:%S%z')
            if online:
                _close_window(proc)
                proc = None
                client.remember()
                offline_since = None
                _append_event({'at': stamp, 'online': True}, root=root)
            else:
                offline_since = now()
                _append_event({'at': stamp, 'online': False,
                               'phase': state.get('loginPhase') if state else 'webui_unavailable',
                               'error': state.get('loginError') if state else last_error,
                               'reason': _drop_reason(_console_tail(root))}, root=root)
            logged_in = online
        if not online:
            if offline_since is None:
                offline_since = now()
            if (not _webui_alive(port=port) and now() - offline_since >= 20
                    and (last_relaunch is None or now() - last_relaunch >= 300)):
                try:
                    _relaunch_napcat(root=root, popen=popen)
                    last_relaunch = now()
                    last_stamp = None
                    _append_event({'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                   'event': 'relaunch'}, root=root)
                except Exception as exc:
                    last_error = type(exc).__name__
            login_url = _latest_login_url(root)
            window_alive = proc is not None and getattr(proc, 'poll', lambda: 1)() is None
            if not window_alive:
                if opened_once:
                    refreshed = _restart_client_for_qr(root, login_url, popen=popen,
                                                       now=now, sleep=sleep)
                else:
                    refreshed = _refresh_login_qr(client, root, login_url, now=now, sleep=sleep)
                if refreshed:
                    login_url = refreshed
            stamp = None
            display_image = image
            if login_url:
                digest = hashlib.sha1(login_url.encode('utf8')).hexdigest()
                try:
                    display_image = _render_login_qr(
                        login_url, root / ('depends-data/napcat/cache/qrcode-guard-'
                                           + digest[:12] + '.png'))
                    stamp = digest
                except Exception:
                    stamp = None
            if stamp is None:
                try:
                    stamp = hashlib.sha1(image.read_bytes()).hexdigest()
                except OSError:
                    stamp = None
            if debug:
                print('qr state', (stamp or '')[:8], (last_stamp or '')[:8], window_alive,
                      hashlib.sha1(login_url.encode('utf8')).hexdigest()[:8],
                      login_url.split('k=')[-1][:10], flush=True)
            if stamp is not None and (stamp != last_stamp or not window_alive):
                uin = ''
                try:
                    uin = str((client.call('QQLogin/GetQQLoginInfo') or {}).get('uin') or '')
                except Exception:
                    uin = ''
                proc = _open_window(proc if window_alive else None, display_image, uin, popen)
                last_stamp = stamp
                shown += 1
                if proc is not None:
                    opened_once = True
                try:
                    cache_dir = root / 'depends-data/napcat/cache'
                    stale = sorted(cache_dir.glob('qrcode-guard-*.png'),
                                   key=lambda item: item.stat().st_mtime, reverse=True)[6:]
                    for old_image in stale:
                        old_image.unlink()
                except OSError:
                    pass
        try:
            status_path.write_text(json.dumps({'online': online, 'qr_windows': shown,
                                               'updated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                               'error': last_error}, ensure_ascii=False) + '\n',
                                   encoding='utf8')
        except OSError:
            pass
        if debug:
            print('guard loop end', online, shown, last_error, flush=True)
        sleep(interval)
    _close_window(proc)
    return {'online': bool(logged_in), 'qr_windows': shown, 'error': last_error}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch',action='store_true')
    parser.add_argument('--qr-window',action='store_true',dest='qr_window',
                        help='常驻守护：掉线自动弹二维码、客户端退出自动拉起')
    parser.add_argument('--guard',action='store_true',dest='guard',
                        help='同 --qr-window（供启动脚本使用）')
    args=parser.parse_args()
    client=LoginClient()
    if args.qr_window or args.guard:
        lock_path=ROOT/'depends-data/napcat/config/login-watch.lock'
        with lock_path.open('a+b') as lock:
            import msvcrt
            try:
                lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:return 0
            result=qr_window_loop(client)
            (lock_path.parent/'login-watch-status.json').write_text(json.dumps(result)+'\n',encoding='utf8')
        return 0
    if not args.watch:
        state=client.status()
        print(json.dumps({'online':bool(state.get('isLogin')),'phase':state.get('loginPhase'),
                          'qr_available':bool(state.get('qrcodeurl')),'has_login_error':bool(state.get('loginError'))}))
        return 0
    lock_path=ROOT/'depends-data/napcat/config/login-watch.lock'
    with lock_path.open('a+b') as lock:
        import msvcrt
        try:
            lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:return 0
        result=watch(client)
        (lock_path.parent/'login-watch-status.json').write_text(json.dumps(result)+'\n',encoding='utf8')
    return 0


if __name__=='__main__':raise SystemExit(main())
