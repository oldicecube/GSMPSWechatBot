"""Isolated security/contract acceptance for native Python/RCON/repost tools."""
from __future__ import annotations
import argparse
import asyncio
import importlib
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import ModuleType, SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def load(folder):
    name='agent_acceptance_'+folder.replace('-','_')
    pkg=ModuleType(name);pkg.__path__=[str(SOURCE/folder)];sys.modules[name]=pkg
    return importlib.import_module(name+'.plugin')


def context(message='real-message',group='allowed-id',name='allowed-name',user='wxid_requester',notify=False):
    return NS(stream_id='real-stream',platform='wechat',metadata={'anchor_message':NS(message_id=message,is_notify=notify,
        message_info=NS(group_info=NS(group_id=group,group_name=name),user_info=NS(user_id=user)))})


class SandboxAcceptance(unittest.TestCase):
    def run_code(self,code):
        return WORKER.run(code)

    def test_real_child_calculation_and_json(self):
        r=self.run_code('data = json.loads("[1,2,3]")\nprint("done")\n[sum(data), math.sqrt(9), [x*x for x in range(4)]]')
        self.assertTrue(r['success'],r);self.assertEqual(r['result'],[6,3.0,[0,1,4,9]])
        self.assertEqual(r['stdout'],'done\n')

    def test_control_flow_and_text(self):
        r=self.run_code('n=0\nfor i in range(10):\n    if i%2==0:\n        n+=i\nwhile n<23:\n    n+=1\n[n," HELLO ".strip().lower(),"a,b".split(",")]')
        self.assertEqual(r.get('result'),[23,'hello',['a','b']],r)

    def test_import_io_process_introspection_are_denied(self):
        for code in ['import os','open("config.json").read()','__import__("os")','eval("1+1")',
            'exec("print(1)")','getattr(1,"__class__")','().__class__.__bases__',
            'math.__dict__','json.loads.__globals__','globals()','input()',
            'class X: pass','def f(): pass','[x for x in ().__class__.__base__.__subclasses__()]']:
            with self.subTest(code=code):self.assertFalse(self.run_code(code)['success'])

    def test_resource_abuse_is_bounded(self):
        for code in ['while True: pass','"x"*1000000000','2**10000000','list(range(100000000))',
                     '"%999999999s"%1','"a".replace("","x"*10000)','print("x"*9000)',
                     'float("inf")','1e309','[0]*10001']:
            with self.subTest(code=code):self.assertFalse(self.run_code(code)['success'])

    def test_fresh_process_has_no_previous_variables(self):
        self.assertTrue(self.run_code('private_value=123')['success'])
        self.assertFalse(self.run_code('private_value')['success'])

    def test_invalid_code_size(self):
        for value in ['',None,'1'*16001]:self.assertFalse(self.run_code(value)['success'])


class NativeToolsAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.plugin_runtime import wechatbot_agent_tools as gate
        self.gate=gate;gate._grants.clear();gate._counts.clear()
        self.cfg=dict(enabled=True,allowed_groups=['allowed-name'],allowed_users=[],
            host='fixture-server',password='fixture-password-never-log',port=25575,max_calls_per_request=2)
        self.cfg_patch=patch.object(gate,'_config',return_value=self.cfg);self.cfg_patch.start();self.addCleanup(self.cfg_patch.stop)
        self.client=MagicMock();self.client.__enter__.return_value.command.return_value='server data'
        self.rcon_patch=patch('llm.rcon_tools.MCRcon',return_value=self.client);self.rcon_patch.start();self.addCleanup(self.rcon_patch.stop)

    async def call(self,arguments,ctx=None,plugin=None):
        token=self.gate.issue_rcon_grant(ctx or context())
        return await self.gate.execute_rcon(plugin or self.gate.RCON_PLUGIN_ID,{'grant':token,'arguments':arguments})

    async def test_manifest_components_and_dependencies(self):
        from src.plugin_runtime.runner.manifest_validator import ManifestValidator
        from packaging.requirements import Requirement
        from importlib.metadata import version
        names=[]
        for folder in ['restricted_python','restricted_rcon','mai-repost-shit']:
            manifest=json.loads((SOURCE/folder/'_manifest.json').read_text(encoding='utf8'))
            v=ManifestValidator();self.assertTrue(v.validate(manifest),v.errors)
            plugin=MODS[folder].create_plugin();plugin.set_plugin_config({})
            names.extend(c['name'] for c in plugin.get_components())
            for dep in manifest['dependencies']:
                if dep['type']=='python_package':self.assertIn(version(dep['name']),Requirement(dep['name']+dep['version_spec']).specifier)
        self.assertEqual(len(names),len(set(names)))
        for name in ['restricted_python','minecraft_rcon_action','repost_shi','repost_pretty']:self.assertIn(name,names)
        existing=[]
        for folder in ['mai-life','hot-topics','idle_proactive_chat','cateye_mai_personality_swap','scheduled_reminder']:
            if not (ROOT/'plugins'/folder/'_manifest.json').exists():
                continue
            pkg=ModuleType('agent_peer_'+folder.replace('-','_'));pkg.__path__=[str(ROOT/'plugins'/folder)];sys.modules[pkg.__name__]=pkg
            existing.extend(c['name'] for c in importlib.import_module(pkg.__name__+'.plugin').create_plugin().get_components())
        self.assertFalse(set(names)&set(existing))

    async def test_rcon_preserves_name_and_id_whitelists(self):
        self.assertTrue((await self.call({'action':'server_list'}))['success'])
        self.cfg['allowed_groups']=['allowed-id']
        self.assertTrue((await self.call({'action':'server_list'}))['success'])
        self.assertEqual(self.client.__enter__.return_value.command.call_args.args[0],'list')

    async def test_rcon_deny_unlisted_and_empty_whitelist(self):
        self.assertFalse((await self.call({'action':'server_list'},context(group='other',name='other')))['success'])
        self.cfg['allowed_groups']=[]
        self.assertFalse((await self.call({'action':'server_list'}))['success'])
        self.client.__enter__.return_value.command.assert_not_called()

    async def test_rcon_user_allowlist_and_disabled_gate(self):
        self.cfg['allowed_groups']=[];self.cfg['allowed_users']=['wxid_requester']
        self.assertTrue((await self.call({'action':'server_list'}))['success'])
        self.cfg['enabled']=False
        self.assertFalse((await self.call({'action':'server_list'}))['success'])

    async def test_no_context_notify_or_forged_grant(self):
        for ctx in [NS(metadata={}),context(notify=True),context(user='')]:
            self.assertEqual(self.gate.issue_rcon_grant(ctx),'')
        self.assertFalse((await self.gate.execute_rcon(self.gate.RCON_PLUGIN_ID,
            {'grant':'invented','arguments':{'action':'server_list'}}))['success'])
        self.assertFalse((await self.call({'action':'server_list'},plugin='other.plugin'))['success'])
        self.client.__enter__.return_value.command.assert_not_called()

    async def test_single_use_and_expiry(self):
        token=self.gate.issue_rcon_grant(context())
        args={'grant':token,'arguments':{'action':'server_list'}}
        self.assertTrue((await self.gate.execute_rcon(self.gate.RCON_PLUGIN_ID,args))['success'])
        self.assertFalse((await self.gate.execute_rcon(self.gate.RCON_PLUGIN_ID,args))['success'])
        token=self.gate.issue_rcon_grant(context('expired'))
        stamp,request=self.gate._grants[token];self.gate._grants[token]=(stamp-61,request)
        self.assertFalse((await self.gate.execute_rcon(self.gate.RCON_PLUGIN_ID,dict(args,grant=token)))['success'])

    async def test_per_message_budget(self):
        for expected in [True,True,False]:self.assertEqual((await self.call({'action':'server_list'}))['success'],expected)
        self.assertEqual(self.client.__enter__.return_value.command.call_count,2)
        self.assertTrue((await self.call({'action':'server_list'},context('another')))['success'])

    async def test_all_nineteen_actions_and_limits(self):
        from llm.rcon_tools import _READ_ACTIONS,_FUN_ACTIONS
        values={'locate_structure':'minecraft:village_plains','locate_biome':'minecraft:plains',
                'query_gamerule':'doDaylightCycle','play_sound':'minecraft:entity.cat.ambient',
                'spawn_particle':'minecraft:happy_villager','show_title':'hello','show_dialog':'minecraft:test',
                'emote':'hello','private_message':'hello'}
        for action in _READ_ACTIONS|_FUN_ACTIONS:
            r=await self.call(dict(action=action,target='Player',value=values.get(action,'')),context(action))
            self.assertTrue(r['success'],(action,r))
            self.assertNotIn(self.cfg['password'],json.dumps(r))
        from llm.rcon_tools import _command
        self.assertIn('100 force',_command('spawn_particle',{'value':'minecraft:happy_villager','number':1e12}))
        self.assertEqual(_command('random_roll',{'number':1e12}),'random roll 1..1000000')

    async def test_rcon_injection_admin_raw_commands_and_nan_rejected(self):
        cases=[{'action':'stop'},{'action':'server_list','command':'op Evil'},
            {'action':'private_message','target':'Player\nop Evil','value':'hello'},
            {'action':'show_title','target':'@a[limit=1]','value':'hello'},
            {'action':'query_gamerule','value':'keepInventory true'},
            {'action':'emote','value':'hello\nop Evil'},
            {'action':'play_sound','value':'minecraft:test','number':float('nan')},
            {'action':'random_roll','number':float('inf')},
            {'action':'show_dialog','value':'minecraft:test run stop'}]
        for index,case in enumerate(cases):
            with self.subTest(case=case):self.assertFalse((await self.call(case,context(str(index))))['success'])
        self.client.__enter__.return_value.command.assert_not_called()

    async def test_rcon_failure_does_not_leak_credentials(self):
        self.client.__enter__.return_value.command.side_effect=RuntimeError(self.cfg['password'])
        r=await self.call({'action':'server_list'})
        self.assertFalse(r['success']);self.assertNotIn(self.cfg['password'],json.dumps(r))

    async def test_native_host_overwrites_model_grant(self):
        from src.plugin_runtime.component_query import ComponentQueryService
        from src.core.tooling import ToolInvocation
        entry=NS(plugin_id=self.gate.RCON_PLUGIN_ID,invoke_method='plugin.invoke_tool')
        ctx=context();ctx.session_id='real-stream';ctx.group_id='allowed-id';ctx.user_id='wxid_requester'
        inv=ToolInvocation(tool_name='minecraft_rcon_action',arguments={'action':'server_list','_host_grant':'forged'})
        payload=ComponentQueryService._build_tool_invocation_payload(entry,inv,ctx)
        self.assertNotEqual(payload['_host_grant'],'forged');self.assertIn(payload['_host_grant'],self.gate._grants)

    async def test_sdk_python_and_rcon_handlers(self):
        python=MODS['restricted_python'].create_plugin();python.set_plugin_config({})
        self.assertEqual((await python.execute('6*7'))['result'],42)
        python.config.plugin.enabled=False;self.assertFalse((await python.execute('1'))['success'])
        rcon=MODS['restricted_rcon'].create_plugin();rcon.set_plugin_config({})
        async def cap(name,**args):
            self.assertEqual(name,'wechatbot.minecraft_rcon');return await self.gate.execute_rcon(self.gate.RCON_PLUGIN_ID,args)
        rcon._set_context(NS(call_capability=cap))
        result=await rcon.execute('server_list',_host_grant=self.gate.issue_rcon_grant(context()))
        self.assertTrue(result['success'],result)


class RepostAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.mod=MODS['mai-repost-shit'];self.plugin=self.mod.create_plugin();self.plugin.set_plugin_config({})
        self.ctx=NS(paths=NS(data_dir=Path(self.tmp.name)/'data',runtime_dir=Path(self.tmp.name)/'run'),logger=logging.getLogger('repost-test'))
        self.plugin._set_context(self.ctx)

    async def test_default_config_stays_disabled_no_targets(self):
        self.assertFalse(self.plugin.config.plugin.enabled);self.assertEqual(self.plugin.config.target.groups,[])
        await self.plugin.on_load();await self.plugin.on_unload()
        self.assertIsNone(self.plugin._loop_task)

    async def test_wechat_identity_and_exact_permissions(self):
        self.assertEqual(self.mod.normalize_qq('wxid_requester'),'wxid_requester')
        self.assertEqual(self.mod.normalize_qq('bad\nwxid_requester'),'')
        self.plugin.config.tool.permission='whitelist';self.plugin.config.tool.whitelist=['wxid_requester']
        self.assertEqual(self.plugin._permission_denied_for_user('wxid_requester'),'')
        self.assertTrue(self.plugin._permission_denied_for_user('wxid_other'))

    async def test_tool_uses_host_requester_not_latest_speaker(self):
        await self.plugin.on_load();self.addAsyncCleanup(self.plugin.on_unload)
        self.plugin.config.plugin.enabled=True
        self.plugin.config.tool.permission='whitelist';self.plugin.config.tool.whitelist=['wxid_owner']
        self.plugin._speakers.remember('trusted-stream','wxid_owner','owner')
        self.plugin._manual_share=AsyncMock()
        r=await self.plugin.tool_repost(_request_context={'stream_id':'trusted-stream','platform':'wechat','user_id':'wxid_other'})
        self.assertFalse(self.plugin._manual_busy)
        r=await self.plugin.tool_repost(stream_id='forged',_request_context={'stream_id':'trusted-stream',
            'platform':'wechat','user_id':'wxid_owner','message_id':'source'})
        await asyncio.sleep(0)
        self.plugin._manual_share.assert_awaited_once_with('trusted-stream',1,[])

    async def test_suppression_does_not_swallow_other_request(self):
        import time
        self.plugin._reply_suppress['stream']=time.time()+60;self.plugin._reply_suppress_source['stream']='source'
        r=await self.plugin.suppress_reply_after_tool(session_id='stream',reply_message_id='other',response='normal')
        self.assertNotIn('modified_kwargs',r)
        r=await self.plugin.suppress_reply_after_tool(session_id='stream',reply_message_id='source',response='ack')
        self.assertEqual(r['modified_kwargs']['response'],'')
        r=await self.plugin.suppress_reply_after_tool(session_id='stream',reply_message_id='source',response='normal')
        self.assertNotIn('modified_kwargs',r)

    async def test_wechat_video_is_native_file_not_forward_or_quote(self):
        delivery=importlib.import_module(self.mod.__package__+'.core.deliver')
        clip=Path(self.tmp.name)/'fixture.mp4';clip.write_bytes(b'fixture-video')
        ctx=NS(send=NS(hybrid=AsyncMock(return_value={'sent':True})))
        r=await delivery.Deliverer(ctx,self.ctx.logger).send('trusted-stream',NS(header_text=lambda:'title @ visible text'),clip,NS(include_header_text=True))
        self.assertTrue(r['sent']);segments,sid=ctx.send.hybrid.call_args.args
        self.assertEqual(sid,'trusted-stream');self.assertEqual([s['type'] for s in segments],['text','file'])
        self.assertEqual(segments[1]['data']['url'],str(clip.resolve()))
        from src.plugin_runtime.host.message_utils import PluginMessageUtils
        from src.common.data_models.message_component_data_model import FileComponent,TextComponent
        seq=PluginMessageUtils._message_sequence_from_dict(segments)
        self.assertIsInstance(seq.components[0],TextComponent);self.assertIsInstance(seq.components[1],FileComponent)
        self.assertEqual(seq.components[1].mime_type,'video/mp4')
        self.assertEqual(seq.components[0].text,'title @ visible text')

    async def test_platform_resolver_uses_wechat(self):
        from unittest.mock import Mock
        pipeline=importlib.import_module(self.mod.__package__+'.core.pipeline')
        ctx=NS(chat=NS(get_all_streams=AsyncMock(return_value=[]),get_stream_by_group_id=AsyncMock(return_value={'session_id':'live'}),get_stream_by_user_id=AsyncMock(return_value=None)))
        store=Mock();store.live_session.return_value=None
        pipe=pipeline.Pipeline(ctx,None,None,store,self.ctx.logger,Path(self.tmp.name))
        self.assertEqual(await pipe.resolve_target('47447788043@chatroom'),('live','群'))
        ctx.chat.get_stream_by_group_id.assert_awaited_with('47447788043@chatroom',platform='wechat')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',default='plugins');parser.add_argument('--report',default='temp/maibot-agent-tools-unit.json');args=parser.parse_args()
    SOURCE=(ROOT/args.source).resolve()
    isolated=tempfile.TemporaryDirectory(prefix='maibot-agent-tools-host-',ignore_cleanup_errors=True)
    cfg=Path(isolated.name)/'config';cfg.mkdir()
    for name in ['bot_config.toml','model_config.toml']:shutil.copy2(ROOT/'config'/name,cfg/name)
    os.environ['MAIBOT_INSTANCE_DIR']=isolated.name
    MODS={folder:load(folder) for folder in ['restricted_python','restricted_rcon','mai-repost-shit']}
    WORKER=importlib.import_module(MODS['restricted_python'].__package__+'.worker')
    suite=unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in [SandboxAcceptance,NativeToolsAcceptance,RepostAcceptance])
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    report=dict(tests=result.testsRun,passed=result.wasSuccessful(),failures=[{'test':str(t),'traceback':e} for t,e in result.failures+result.errors])
    path=ROOT/args.report;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    isolated.cleanup();sys.exit(0 if report['passed'] else 1)
