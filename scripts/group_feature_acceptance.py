"""Isolated per-adapter group feature API, persistence and execution gates.

No production edits, model calls, real transport messages or process restart.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
INSTANCE = tempfile.TemporaryDirectory(prefix='group-features-',dir=ROOT/'temp')
BASE = Path(INSTANCE.name)
(BASE/'config').mkdir()
for name in ('bot_config.toml','model_config.toml'):
    shutil.copy2(ROOT/'config'/name,BASE/'config'/name)
os.environ['MAIBOT_INSTANCE_DIR'] = str(BASE)

from src.platform_io.adapter_policy import AdapterIdentity, AdapterPolicyManager
from src.platform_io import chat_features as features
from src.platform_io.drivers.base import PlatformIODriver
from src.platform_io.manager import PlatformIOManager
from src.platform_io.types import DriverDescriptor, DriverKind, RouteBinding
from src.plugin_runtime.host.message_utils import PluginMessageUtils as U
from src.webui.routers.chat import routes


class Driver(PlatformIODriver):
    async def send_message(self, *args, **kwargs):
        raise AssertionError('No physical send is permitted')


class GroupFeatures(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.path=BASE/(self.id().rsplit('.',1)[-1]+'.toml')
        self.policy=AdapterPolicyManager(self.path)
        self.broker=PlatformIOManager()
        self.identities={}
        for platform in ('qq','wechat'):
            driver=Driver(DriverDescriptor(driver_id='fixture-'+platform,kind=DriverKind.LOCAL,
                platform=platform,account_id='bot-'+platform,scope='test',plugin_id='adapter.'+platform,
                metadata={'plugin_type':'adapter'}))
            await self.broker.add_driver(driver)
            self.broker.bind_send_route(RouteBinding(driver.descriptor.route_key,driver.driver_id,DriverKind.LOCAL))
            self.identities[platform]=routes._get_driver_adapter_identity(driver)
        self.stack=ExitStack()
        self.addCleanup(self.stack.close)
        for module,name,value in ((features,'get_adapter_policy_manager',self.policy),
                (routes,'get_adapter_policy_manager',self.policy),(routes,'get_platform_io_manager',self.broker)):
            self.stack.enter_context(patch.object(module,name,return_value=value))
        self.stack.enter_context(patch('src.platform_io.manager.get_platform_io_manager',return_value=self.broker))
        self.stack.enter_context(patch('src.platform_io.manager.get_adapter_policy_manager',return_value=self.policy))

    def document(self, flags=None):
        group=dict(default_action='block',allow_ids=['100'],deny_ids=[])
        if flags is not None:group['features']={'100':flags}
        return dict(group=group,private=dict(default_action='inherit',allow_ids=[],deny_ids=[]))

    def save(self, flags, platform='qq'):
        self.policy.set_adapter_policy(self.identities[platform],self.document(flags))

    async def message(self, platform='qq'):
        from src.chat.message_receive.chat_manager import chat_manager
        msg=U._build_session_message_from_dict(dict(platform=platform,account_id='bot-'+platform,scope='test',
            message_id='fixture-'+self.id().rsplit('.',1)[-1],message_info=dict(user_info=dict(user_id='sender',user_nickname='sender'),
            group_info=dict(group_id='100',group_name='same-name')),raw_message=[dict(type='text',data='@bot /ping')],is_at=True))
        session=await chat_manager.get_or_create_session(platform,'sender','100',account_id='bot-'+platform,scope='test')
        msg.session_id=session.session_id
        return msg

    async def test_persistence_and_independent_switches(self):
        self.save(dict(llm_enabled=False,plugins_enabled=True))
        reread=AdapterPolicyManager(self.path)
        self.assertEqual(reread.get_group_features(self.identities['qq'],'100'),dict(llm_enabled=False,plugins_enabled=True))
        self.assertTrue(reread.evaluate(self.identities['qq'],chat_type='group',target_id='100').allowed)

    async def test_same_name_and_id_on_other_platform_unchanged(self):
        self.save(dict(llm_enabled=False,plugins_enabled=False))
        wx=await self.message('wechat');qq=await self.message()
        self.assertIsNone(features.message_override(wx,'llm_enabled'))
        self.assertFalse(features.message_override(qq,'llm_enabled'))

    async def test_other_account_and_scope_not_affected(self):
        self.save(dict(llm_enabled=False))
        own=self.identities['qq']
        for field,value in [('account_id','another'),('scope','another')]:
            from dataclasses import replace
            other=replace(own,**{field:value})
            self.assertEqual(self.policy.get_group_features(other,'100'),{})

    async def test_wildcard_fallback_and_specific_precedence(self):
        doc=self.document();doc['group']['features']={'*':dict(llm_enabled=False,plugins_enabled=True),'100':dict(llm_enabled=True)}
        self.policy.set_adapter_policy(self.identities['qq'],doc)
        self.assertEqual(self.policy.get_group_features(self.identities['qq'],'100'),dict(llm_enabled=True,plugins_enabled=True))
        self.assertFalse(self.policy.get_group_features(self.identities['qq'],'999')['llm_enabled'])

    async def test_disabled_admission_cannot_be_overridden_by_features(self):
        doc=self.document(dict(llm_enabled=True));doc['group']['deny_ids']=['200']
        self.policy.set_adapter_policy(self.identities['qq'],doc)
        self.assertFalse(self.policy.evaluate(self.identities['qq'],chat_type='group',target_id='200').allowed)

    async def test_old_client_update_preserves_switches(self):
        self.save(dict(llm_enabled=False))
        self.policy.set_adapter_policy(self.identities['qq'],self.document())
        self.assertFalse(self.policy.get_group_features(self.identities['qq'],'100')['llm_enabled'])

    async def test_validation_and_empty_reset(self):
        for value in ({'100':{'unknown':True}}, {'100':{'llm_enabled':'no'}}, []):
            doc=self.document();doc['group']['features']=value
            with self.assertRaises(ValueError):self.policy.set_adapter_policy(self.identities['qq'],doc)
        self.save(dict(llm_enabled=False));doc=self.document();doc['group']['features']={}
        self.policy.set_adapter_policy(self.identities['qq'],doc)
        self.assertEqual(self.policy.get_group_features(self.identities['qq'],'100'),{})

    async def test_active_policy_group_roundtrip_copy_and_switch(self):
        off=self.document(dict(llm_enabled=False,plugins_enabled=True));on=self.document(dict(llm_enabled=True,plugins_enabled=False))
        groups=[dict(id='off',name='Off',**off),dict(id='on',name='On',**on)]
        self.policy.set_adapter_policy(self.identities['qq'],off,policy_groups=groups,active_group='off')
        self.assertFalse(self.policy.get_group_features(self.identities['qq'],'100')['llm_enabled'])
        self.policy.set_adapter_policy(self.identities['qq'],on,policy_groups=groups,active_group='on')
        loaded=self.policy.get_adapter_policy(self.identities['qq'])
        self.assertTrue(loaded['group']['features']['100']['llm_enabled'])
        self.assertFalse(loaded['policy_groups'][0]['group']['features']['100']['llm_enabled'])

    async def test_chat_admission_edits_preserve_group_feature_snapshot(self):
        off=self.document(dict(llm_enabled=False))
        self.policy.set_adapter_policy(self.identities['qq'],off,policy_groups=[dict(id='off',name='Off',**off)],active_group='off')
        self.policy.set_chat_override(self.identities['qq'],chat_type='group',target_id='200',action='allow')
        loaded=self.policy.get_adapter_policy(self.identities['qq'])
        self.assertFalse(loaded['policy_groups'][0]['group']['features']['100']['llm_enabled'])

    async def test_file_replacement_invalidates_runtime_cache(self):
        msg=await self.message();self.save(dict(llm_enabled=False))
        self.assertFalse(features.message_override(msg,'llm_enabled'))
        self.save(dict(llm_enabled=True))
        self.assertTrue(features.message_override(msg,'llm_enabled'))

    async def test_parallel_adapter_updates_do_not_lose_each_other(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda p:self.save(dict(llm_enabled=False),p),('qq','wechat')))
        for identity in self.identities.values():self.assertFalse(self.policy.get_group_features(identity,'100')['llm_enabled'])

    async def test_api_put_get_unseen_whitelisted_group(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app=FastAPI();app.include_router(routes.router);app.dependency_overrides[routes.require_auth]=lambda:True
        with TestClient(app) as client:
            r=client.put('/api/chat/adapters/plugins/adapter.qq/policy',json=self.document(dict(llm_enabled=False,plugins_enabled=True)))
            self.assertEqual(r.status_code,200,r.text)
            r=client.get('/api/chat/adapters/plugins/adapter.qq/policy')
            self.assertTrue(r.json()['group_features_supported'])
            self.assertFalse(r.json()['policy']['group']['features']['100']['llm_enabled'])

    async def test_shell_features_override_legacy_and_control_both_plugin_kinds(self):
        from core.dispatcher import Dispatcher
        d=Dispatcher();d.config={'group_features':{'default':dict(llm_enabled=False,plugins_enabled=False,auto_plugins_enabled=False)}}
        self.save(dict(llm_enabled=True,plugins_enabled=False),'wechat')
        context=dict(platform='wechat',account_id='bot-wechat',scope='test',group_id='100',sessionId='100')
        self.assertTrue(d._context_feature_enabled(context,'llm_enabled'))
        self.assertFalse(d._context_feature_enabled(context,'plugins_enabled'))
        self.assertFalse(d._context_feature_enabled(context,'auto_plugins_enabled'))

    async def test_native_plugin_component_disabled_but_gateway_remains(self):
        from src.plugin_runtime.host.component_registry import ComponentRegistry
        msg=await self.message();self.save(dict(plugins_enabled=False,llm_enabled=True))
        registry=ComponentRegistry()
        registry.register_plugin_components('fixture',[dict(name='tool',component_type='TOOL',metadata={}),
            dict(name='gateway',component_type='MESSAGE_GATEWAY',metadata={'route_type':'duplex','platform':'qq'})])
        self.assertEqual(registry.get_tools(session_id=msg.session_id),[])
        self.assertEqual(len(registry.get_message_gateways(session_id=msg.session_id)),1)

    async def test_hooks_skip_disabled_group_without_invoking_plugin(self):
        from src.plugin_runtime.host.hook_dispatcher import HookDispatcher
        msg=await self.message();self.save(dict(plugins_enabled=False))
        dispatcher=HookDispatcher()
        dispatcher._resolve_supervisors=Mock(side_effect=AssertionError('Hook must not run'))
        result=await dispatcher.invoke_hook('chat.receive.before_process',message=U._session_message_to_dict(msg))
        self.assertFalse(result.aborted)

    async def test_native_llm_off_keeps_observation_and_skips_even_at(self):
        from src.chat.message_receive.bot import chat_bot
        msg=await self.message();self.save(dict(llm_enabled=False,plugins_enabled=False))
        receiver=AsyncMock()
        with patch.object(chat_bot,'heartflow_message_receiver',receiver),patch.object(chat_bot,'_process_adapter_lifecycle_command',AsyncMock(return_value=False)),patch.object(chat_bot,'_process_clear_context_command',AsyncMock(return_value=False)):
            result=await chat_bot.receive_message(msg,raise_errors=True)
        self.assertEqual(result,'llm_disabled');receiver.process_message.assert_not_called()

    async def test_outbound_blocks_llm_but_keeps_explicit_plugin_reply(self):
        from src.common.message_provenance import plugin_scope
        from src.platform_io.types import DeliveryReceipt,DeliveryStatus
        msg=await self.message();self.save(dict(llm_enabled=False,plugins_enabled=True))
        driver=self.broker.driver_registry.get('fixture-qq')
        send=AsyncMock(return_value=DeliveryReceipt(internal_message_id=msg.message_id,route_key=driver.descriptor.route_key,status=DeliveryStatus.SENT,driver_id=driver.driver_id,driver_kind=DriverKind.LOCAL))
        with patch.object(driver,'send_message',send):
            batch=await self.broker.send_message(msg,driver.descriptor.route_key)
            self.assertFalse(batch.has_success);send.assert_not_called()
            with plugin_scope('fixture.plugin'):
                batch=await self.broker.send_message(msg,driver.descriptor.route_key)
            self.assertTrue(batch.has_success);self.assertEqual(send.call_count,1)


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GroupFeatures))
    from src.common.database.database import engine
    engine.dispose()
    report=dict(passed=result.wasSuccessful(),tests=result.testsRun,failures=[(str(t),e) for t,e in result.failures+result.errors],
                physical_messages=False,model_calls=False,production_edits=False,restarted=False)
    (ROOT/'docs/maibot-wechat-acceptance/group-features-20261031.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    INSTANCE.cleanup()
    raise SystemExit(not result.wasSuccessful())
