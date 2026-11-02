"""Manual delivery receipt and inline native-plugin settings regression; no live sends."""
import asyncio
import importlib
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
INSTANCE=tempfile.TemporaryDirectory(prefix='repost-settings-',dir=ROOT/'temp',ignore_cleanup_errors=True)
os.environ['MAIBOT_INSTANCE_DIR']=INSTANCE.name;os.environ['MAIBOT_INTERCEPT_SEND']='1'
(Path(INSTANCE.name)/'config').mkdir()
for name in ('bot_config.toml','model_config.toml'):
    shutil.copy2(ROOT/'config'/name,Path(INSTANCE.name)/'config'/name)
pkg=ModuleType('manual_repost_test');pkg.__path__=[str(ROOT/'plugins/mai-repost-shit')];sys.modules[pkg.__name__]=pkg
mod=importlib.import_module(pkg.__name__+'.plugin')
deliver=importlib.import_module(pkg.__name__+'.core.deliver')


def plugin():
    obj=mod.create_plugin()
    obj.set_plugin_config({'plugin':{'config_version':'1.3.0','enabled':True},
                          'target':{'groups':['g@chatroom']},'source':{'keywords':['retained-legacy-keyword']}})
    obj._set_context(NS(logger=logging.getLogger('manual-test'),send=NS(text=AsyncMock())))
    return obj


class Settings(unittest.TestCase):
    def test_native_schema_hides_old_controls_without_deleting_values(self):
        obj=plugin();schema=obj.get_webui_config_schema(plugin_id='github.erhan111119.mai-repost-shit')
        self.assertEqual(set(schema['sections']),{'story'})
        self.assertEqual(set(schema['sections']['story']['fields']),
            {'enabled','use_account','expected_uid','seed_aid','parse_linked_enabled'})
        self.assertEqual(schema['embedded_webui_page'],'story')
        self.assertEqual(obj.config.source.keywords,['retained-legacy-keyword'])
        self.assertTrue(obj.config.plugin.enabled)
        self.assertEqual(obj.config.target.groups,['g@chatroom'])

    def test_settings_page_metadata_keeps_gateway_and_documents_registered(self):
        from src.plugin_runtime.webui_schema import load_webui_extension
        from src.webui.routers.plugin.webui_extensions import _serialize_extensions
        ext=load_webui_extension(str(ROOT/'plugins/mai-repost-shit'))
        pid='github.erhan111119.mai-repost-shit'
        supervisor=NS(_registered_plugins={pid:NS(config_schema=plugin().get_webui_config_schema())})
        payload=_serialize_extensions({pid:(supervisor,ext)})['extensions'][0]
        self.assertEqual(payload['config_page_id'],'story')
        self.assertEqual(payload['pages'][0]['id'],'story')
        self.assertIn('auth',payload['pages'][0]['actions'])
        self.assertNotIn('document',payload['pages'][0])
        supervisor._registered_plugins[pid].config_schema={'embedded_webui_page':'missing'}
        self.assertIsNone(_serialize_extensions({pid:(supervisor,ext)})['extensions'][0]['config_page_id'])


class ManualReceipt(unittest.IsolatedAsyncioTestCase):
    async def test_manual_background_success_never_sends_a_failure(self):
        obj=plugin();obj._run_and_report=AsyncMock(return_value={'sent':True})
        await obj._manual_share('sid',1,[])
        obj.ctx.send.text.assert_not_awaited()

    async def test_unknown_receipt_neither_broadcasts_failure_nor_retries(self):
        obj=plugin();obj._run_and_report=AsyncMock(return_value={'sent':False,'unknown':True,'reason':'投递失败'})
        await obj._manual_share('sid',3,[])
        obj._run_and_report.assert_awaited_once();obj.ctx.send.text.assert_not_awaited()
        for command in (obj.cmd_share_now,obj.cmd_share_here,obj.cmd_pretty_here):
            handled,text,intercept=await command(stream_id='sid')
            self.assertTrue(handled);self.assertEqual(intercept,1);self.assertIn('未确认',text)
            self.assertNotIn('没搬成',text)

    async def test_definite_failure_reports_once(self):
        obj=plugin();obj._run_and_report=AsyncMock(return_value={'sent':False,'reason':'no candidate'})
        await obj._manual_share('sid',3,[])
        obj._run_and_report.assert_awaited_once();obj.ctx.send.text.assert_awaited_once()
        self.assertIn('no candidate',obj.ctx.send.text.call_args.args[0])

    async def test_real_sdk_extends_media_rpc_timeout_on_both_platforms(self):
        from maibot_sdk.context import PluginContext
        calls=[]
        async def rpc(method,pid,payload,timeout_ms=None):
            name=payload['capability'];args=payload['args'];calls.append((name,args,timeout_ms))
            if name=='chat.get_all_streams':
                return {'success':True,'streams':[
                    {'session_id':'wx','platform':'wechat','account_id':'wxid_self','group_id':'g@chatroom'},
                    {'session_id':'qq','platform':'qq','account_id':'123','scope':'napcat','group_id':'456'}]}
            if name=='config.get':return {'success':True,'value':'bot'}
            # Scaled timing: a delivery slower than the default 30 s RPC gets
            # its real receipt rather than generating a fixed failure message.
            self.assertEqual(timeout_ms,180000)
            self.assertNotIn('timeout_ms',args)
            async def slow():
                await asyncio.sleep(.06)
                return {'success':True,'sent':True,'message_id':'actual-receipt'}
            return await asyncio.wait_for(slow(),timeout=(timeout_ms or 30000)/1000000)
        ctx=PluginContext('github.erhan111119.mai-repost-shit',rpc_call=rpc)
        sender=deliver.Deliverer(ctx,logging.getLogger('manual-test'))
        path=Path(INSTANCE.name)/'synthetic.mp4';path.write_bytes(b'not-actually-sent')
        for sid in ('wx','qq'):
            report=await sender.send(sid,NS(header_text=lambda:'test'),path,NS(include_header_text=True))
            self.assertTrue(report['sent']);self.assertNotIn('unknown',report)
        self.assertEqual({name for name,_,_ in calls if name.startswith('send.')},{'send.hybrid','send.forward'})
        components=plugin().get_components()
        manual=next(item for item in components if item['name']=='repost.manual_one')
        self.assertEqual(manual['metadata']['timeout_ms'],600000)

    async def test_platform_unknown_receipts_and_rpc_exceptions_are_preserved(self):
        candidate=NS(header_text=lambda:'test')
        path=Path(INSTANCE.name)/'receipt.mp4';path.write_bytes(b'not-sent')
        for platform in ('wechat','qq'):
            for result in ({'sent':False,'unknown':True}, {'sent':False,'queued':True},
                           {'sent':False,'error':'[E_TIMEOUT] receipt expired'}, TimeoutError()):
                send=AsyncMock(side_effect=result) if isinstance(result,Exception) else AsyncMock(return_value=result)
                ctx=NS(send=NS(hybrid=send,forward=send),config=NS(get=AsyncMock(return_value='bot')))
                sender=deliver.Deliverer(ctx,logging.getLogger('manual-test'))
                with patch('utils.plugin_stream.stream_for',AsyncMock(return_value={'platform':platform,'account_id':'123'})):
                    report=await sender.send('sid',candidate,path,NS(include_header_text=True))
                self.assertFalse(report['sent']);self.assertTrue(report['unknown'])
                send.assert_awaited_once()


class ShellReceipt(unittest.TestCase):
    def test_shell_command_does_not_turn_pending_or_unknown_receipts_into_failure(self):
        from plugins import shit
        from llm.core.maibot_runtime import native_runtime
        for result in ({'sent':True},{'sent':False,'unknown':True},{'queued':True},
                       {'success':False,'error':'[E_TIMEOUT] invoke API timeout'}):
            def submit(coro,timeout):
                coro.close();self.assertEqual(timeout,660);return result
            with patch.object(native_runtime,'submit',side_effect=submit),patch('core.sender.send') as send, \
                 patch.object(shit,'_slots',NS(release=Mock())):
                shit._work('bv',{'group':'fixture','platform':'wechat'})
                send.assert_not_called()

    def test_shell_submit_timeout_is_unconfirmed_not_failed(self):
        from plugins import shit
        from llm.core.maibot_runtime import native_runtime
        def submit(coro,timeout):
            coro.close();raise TimeoutError()
        with patch.object(native_runtime,'submit',side_effect=submit),patch('core.sender.send') as send, \
             patch.object(shit,'_slots',NS(release=Mock())):
            shit._work('bv',{'group':'fixture','platform':'wechat'})
            send.assert_not_called()


if __name__=='__main__':unittest.main(verbosity=2)
