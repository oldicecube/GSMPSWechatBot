"""Isolated ComfyUI settings, connectivity and success-only billing checks."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock,patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
INSTANCE=tempfile.TemporaryDirectory(prefix='anima-billing-',ignore_cleanup_errors=True)
root=Path(INSTANCE.name);(root/'config').mkdir();(root/'plugins').mkdir()
for name in ('bot_config.toml','model_config.toml'):shutil.copy2(ROOT/'config'/name,root/'config'/name)
shutil.copy2(ROOT/'plugins/anima.py',root/'plugins/anima.py')
(root/'config.json').write_text(json.dumps({'comfy':{'url':'http://drawing.invalid:8188'},
                                         'anima':{'generation_timeout':180}}),encoding='utf8')
os.environ['MAIBOT_INSTANCE_DIR']=str(root);os.environ['MAIBOT_INTERCEPT_SEND']='1'
from plugins import anima
from anima.anima_service import AnimaService


class Billing(unittest.TestCase):
    def setUp(self):
        self.service=Mock()
        self.service.can_start_generate.return_value=(True,'')
        self.service.mark_generating.return_value=True
        self.service.check_connection.return_value=(True,'')
        self.service.generate.return_value={'success':True,'image_path':'fixture.png','elapsed_seconds':12}
        self.stack=[]
        for patcher in (
            patch.object(anima,'_anima_service',self.service),
            patch('utils.points_manager.binding_required',return_value=None),
            patch('utils.account_identity.canonical_user',side_effect=lambda uid:uid),
            patch.object(anima,'get_points',return_value=100),
        ):
            patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(anima,'try_spend_points',return_value=(True,92));self.spend=patcher.start();self.addCleanup(patcher.stop)
        patcher=patch.object(anima,'send',return_value=(True,''));self.send=patcher.start();self.addCleanup(patcher.stop)
        self.context={'platform':'qq','wxid':'qq:123456','group':'qq:bot:scope:group:987654','user':'fixture'}

    def test_connection_refusal_is_free_and_does_not_call_llm(self):
        self.service.check_connection.return_value=(False,'无法连接 ComfyUI')
        result=anima.handle('a flower garden',self.context)
        self.assertIn('未扣积分',result);self.spend.assert_not_called()
        self.service.generate.assert_not_called();self.send.assert_not_called()
        self.service.configure_connection.assert_called_once_with('http://drawing.invalid:8188',180)
        self.service.mark_done.assert_called_once()

    def test_generation_failure_timeout_and_clarification_are_free(self):
        for result in ({'success':False,'error':'timeout'},
                       {'success':False,'needs_clarification':True,'error':'which character?'}):
            self.service.generate.return_value=result
            text=anima.handle('a flower garden',self.context)
            self.assertNotIn('消耗积分',text);self.spend.assert_not_called()

    def test_only_explicit_llm_rejection_costs_once_without_sending_image(self):
        self.service.generate.return_value={'success':False,'rejected':True,'error':'explicit refusal'}
        text=anima.handle('refused request',self.context)
        self.assertIn('消耗积分: -8',text)
        self.spend.assert_called_once_with('qq:123456',8)
        self.assertEqual(self.send.call_count,1)

    def test_rejection_like_error_or_non_boolean_flag_is_not_a_charge(self):
        self.service.generate.return_value={'success':False,'rejected':'true','error':'gateway rejected the connection'}
        text=anima.handle('request',self.context)
        self.assertIn('未扣积分',text);self.spend.assert_not_called()

    def test_delivery_false_and_exception_are_free(self):
        for outcome in ((False,'unknown delivery'),TimeoutError('receipt unavailable')):
            self.send.side_effect=[(True,''),outcome]
            text=anima.handle('a flower garden',self.context)
            self.assertIn('未扣积分',text);self.spend.assert_not_called()
        self.assertEqual(self.service.delete_image.call_count,2)

    def test_success_is_debited_once_after_delivery(self):
        events=[]
        self.send.side_effect=lambda *args,**kwargs:(events.append('image' if 'file_path' in kwargs else 'start') or (True,''))
        self.spend.side_effect=lambda *args:(events.append('debit') or (True,92))
        text=anima.handle('a flower garden',self.context)
        self.assertIn('消耗积分: -8',text)
        self.assertEqual(events,['start','image','debit'])
        self.spend.assert_called_once_with('qq:123456',8)
        self.service.delete_image.assert_called_once_with('fixture.png')

    def test_debit_exception_never_retries_debit_or_claims_unsent(self):
        self.spend.side_effect=RuntimeError('database unavailable')
        result=anima.handle('a flower garden',self.context)
        self.assertIn('图片已发送',result);self.assertIn('结算异常',result)
        self.spend.assert_called_once()

    def test_generation_exception_and_insufficient_balance_are_free(self):
        self.service.generate.side_effect=RuntimeError('connection refused')
        self.assertIn('未扣积分',anima.handle('flowers',self.context));self.spend.assert_not_called()
        with patch.object(anima,'get_points',return_value=0):
            self.assertIn('积分不足',anima.handle('flowers',self.context))
        self.spend.assert_not_called()


class Connection(unittest.TestCase):
    def test_url_validation_timeout_and_server_identity(self):
        obj=AnimaService.__new__(AnimaService)
        obj.configure_connection('http://drawing.invalid:8188/',180)
        self.assertEqual(obj.COMFY_URL,'http://drawing.invalid:8188')
        self.assertEqual(obj.COMFY_WAIT_TIMEOUT,180)
        for value in ('file:///x','http://user:password@host','http://host?token=secret','no-host'):
            with self.assertRaises(ValueError):obj.configure_connection(value)
        response=Mock();response.json.return_value={'devices':[{'type':'cuda'}]}
        with patch('anima.anima_service.requests.get',return_value=response) as get:
            self.assertTrue(obj.check_connection()[0])
            get.assert_called_once_with('http://drawing.invalid:8188/system_stats',timeout=5)
            response.json.return_value={}
            self.assertFalse(obj.check_connection()[0])

if __name__=='__main__':unittest.main(verbosity=2)
