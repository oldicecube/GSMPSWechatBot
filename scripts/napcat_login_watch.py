"""Local NapCat login recovery. Never changes chat policies or sends messages."""
import argparse
import hashlib
import json
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


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    client=LoginClient()
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
