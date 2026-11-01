"""Login recovery protocol/state tests. No client launch, browser popup or login."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.napcat_login_watch import LoginClient, watch


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='napcat-login-',dir=ROOT/'temp');self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);cfg=self.root/'depends-data/napcat/config';cfg.mkdir(parents=True)
        (cfg/'webui.json').write_text(json.dumps({'port':6099,'token':'fixture-token'}),encoding='utf8')
        self.client=LoginClient(self.root)
        self.clock=0
    def tick(self,interval):self.clock+=interval
    def run_watch(self,states):
        self.client.status=Mock(side_effect=states)
        self.client.remember=Mock(return_value=True)
        self.opener=Mock(return_value=True)
        self.client.call=Mock(return_value={})
        return watch(self.client,seconds=22,interval=2,now=lambda:self.clock,sleep=self.tick,opener=self.opener)
    def test_online_never_opens_qr(self):
        result=self.run_watch([{'isLogin':True}]);self.assertTrue(result['online']);self.opener.assert_not_called()
    def test_failed_login_refreshes_and_opens_once_then_recovers(self):
        result=self.run_watch([{'loginError':'expired'},{'isLogin':False},{'isLogin':True}])
        self.assertTrue(result['online']);self.assertEqual(self.opener.call_count,1)
        self.client.call.assert_called_once_with('QQLogin/RefreshQRcode')
        self.client.remember.assert_called_once()
    def test_ready_qr_needs_no_refresh(self):
        result=self.run_watch([{'qrcodeurl':'fixture','loginError':'quick login failed'},{'isLogin':True}])
        self.assertTrue(result['qr_opened']);self.client.call.assert_not_called()
    def test_slow_quick_login_gets_grace_period(self):
        result=self.run_watch([{'isLogin':False}]*9+[{'isLogin':True}])
        self.assertTrue(result['qr_opened']);self.assertEqual(self.opener.call_count,1)
    def test_scan_accepted_does_not_refresh_during_initialization(self):
        result=self.run_watch([{'loginError':'old','loginPhase':'initializing','qrLoginAccepted':True}]*4+[{'isLogin':True}])
        self.assertFalse(result['qr_opened']);self.client.call.assert_not_called()
    def test_qr_wait_timeout_does_not_spawn_many_tabs(self):
        result=self.run_watch([{'loginError':'failed','qrcodeurl':'fixture'}]*20)
        self.assertFalse(result['online']);self.assertEqual(self.opener.call_count,1)
    def test_actual_authentication_protocol_and_no_secret_logging(self):
        requests=[]
        def open_request(request,timeout):
            requests.append(request)
            if request.full_url.endswith('/auth/login'):data={'Credential':'fixture-credential'}
            else:data={'isLogin':False,'loginPhase':'waiting_qrcode','qrcodeurl':'fixture'}
            return io.BytesIO(json.dumps({'code':0,'data':data}).encode())
        with patch('urllib.request.build_opener',return_value=NS(open=open_request)):
            state=self.client.status()
        self.assertFalse(state['isLogin'])
        self.assertEqual(json.loads(requests[0].data)['hash'],hashlib.sha256(b'fixture-token.napcat').hexdigest())
        self.assertEqual(requests[1].get_header('Authorization'),'Bearer fixture-credential')
        self.assertNotIn('fixture-token',requests[0].full_url)
    def test_successful_login_saves_actual_account_and_native_quick_login(self):
        self.client.call=Mock(side_effect=[{'uin':'123456'},None])
        self.assertTrue(self.client.remember())
        saved=json.loads((self.root/'depends-data/napcat/config/maibot-launch.json').read_text())
        self.assertEqual(saved,{'account':'123456','autostart':True})
        self.client.call.assert_called_with('QQLogin/SetQuickLoginQQ',{'uin':'123456'})
    def test_missing_account_is_never_guessed(self):
        self.client.call=Mock(return_value={'nick':'some nickname'})
        self.assertFalse(self.client.remember())
    def test_nonzero_webui_response_fails_closed(self):
        with patch('urllib.request.build_opener',return_value=NS(open=lambda *a,**k:io.BytesIO(b'{"code":-1}'))):
            with self.assertRaises(RuntimeError):self.client.status()
    def test_power_shell_launcher_has_no_live_side_effect_in_plan(self):
        import subprocess
        value=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass','-File',str(ROOT/'scripts/start_napcat.ps1'),
                              '-PlanOnly','-Account','123456','-Background'],capture_output=True,text=True,check=True)
        plan=json.loads(value.stdout)
        self.assertEqual(plan['arguments'],['-q','123456']);self.assertTrue(plan['background'])
    def test_main_boot_hook_never_starts_during_acceptance(self):
        from core.napcat_boot import ensure_napcat
        with patch.dict('os.environ',{'MAIBOT_INTERCEPT_SEND':'1'}),patch('subprocess.Popen') as spawn:
            self.assertFalse(ensure_napcat(self.root));spawn.assert_not_called()
    def test_enabled_boot_starts_hidden_recovery_without_restarting_bot(self):
        import subprocess
        from core.napcat_boot import ensure_napcat
        saved=self.root/'depends-data/napcat/config/maibot-launch.json'
        saved.write_text('{"autostart":true,"account":"123456"}',encoding='utf8')
        with patch.dict('os.environ',{'MAIBOT_INTERCEPT_SEND':'0'}),patch('subprocess.Popen') as spawn:
            self.assertTrue(ensure_napcat(self.root))
        args,kwargs=spawn.call_args
        self.assertIn(str(self.root/'scripts/start_napcat.ps1'),args[0])
        self.assertEqual(args[0][-2:],['-IfConfigured','-Background'])
        self.assertEqual(kwargs['creationflags'],subprocess.CREATE_NO_WINDOW)
        self.assertEqual(kwargs['cwd'],str(self.root))
    def test_disabled_boot_leaves_separately_managed_client_alone(self):
        from core.napcat_boot import ensure_napcat
        saved=self.root/'depends-data/napcat/config/maibot-launch.json'
        saved.write_text('{"autostart":false}',encoding='utf8')
        with patch.dict('os.environ',{'MAIBOT_INTERCEPT_SEND':'0'}),patch('subprocess.Popen') as spawn:
            self.assertFalse(ensure_napcat(self.root));spawn.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
