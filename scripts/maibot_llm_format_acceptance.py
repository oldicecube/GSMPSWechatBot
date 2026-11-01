"""Exercise actual OpenAI SDK request serialization; no external traffic."""
from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
INSTANCE = Path(tempfile.mkdtemp(prefix='llm-format-', dir=ROOT/'temp'))
os.environ['MAIBOT_INSTANCE_DIR'] = str(INSTANCE)
(INSTANCE/'config').mkdir()
for name in ('bot_config.toml', 'model_config.toml'):
    shutil.copy2(ROOT/'config'/name, INSTANCE/'config'/name)


class FormatAcceptance(unittest.IsolatedAsyncioTestCase):
    def test_tool_history_and_native_instructions_preserved(self):
        from src.llm_models.openai_compat import normalize_chat_system_messages
        messages = [dict(role='system', content='native persona'), dict(role='user', content='question'),
            dict(role='system', content='source-labelled peer reference'),
            dict(role='assistant', content=None, tool_calls=[dict(id='call-1', type='function',
                 function=dict(name='probe', arguments='{}'))]),
            dict(role='tool', tool_call_id='call-1', content='result'),
            dict(role='system', content='native expression and behaviour boundaries')]
        before = copy.deepcopy(messages)
        result = normalize_chat_system_messages(messages)
        self.assertEqual(result[0]['content'], '\n\n'.join(m['content'] for m in messages if m['role']=='system'))
        self.assertEqual(result[1:], [m for m in messages if m['role']!='system'])
        self.assertEqual(messages, before)
        self.assertEqual(normalize_chat_system_messages(result), result)

    def test_absent_and_single_system(self):
        from src.llm_models.openai_compat import normalize_chat_system_messages as normalize
        self.assertEqual(normalize([]), [])
        for data in ([dict(role='user',content='x')], [dict(role='system',content='x')]):
            self.assertEqual(normalize(data), data)
        self.assertEqual(normalize([dict(role='user',content='x'),dict(role='system',content='y')])[0]['content'], 'y')

    def test_typed_system_content_preserved(self):
        from src.llm_models.openai_compat import normalize_chat_system_messages
        part = dict(type='text', text='native style')
        self.assertEqual(normalize_chat_system_messages([dict(role='system', content=[part]),
            dict(role='system',content='persona')])[0]['content'], [part,dict(type='text',text='persona')])

    async def test_actual_sdk_opt_in_serialization(self):
        import httpx
        from openai import AsyncOpenAI
        from src.config.model_configs import APIProvider, ModelInfo
        from src.llm_models.model_client.openai_client import OpenaiClient
        from src.llm_models.model_client.base_client import ResponseRequest
        from src.llm_models.payload_content.context_item import ContextItemBuilder, RoleType
        original = [ContextItemBuilder().set_role(role).add_text_content(text).build()
            for role, text in [(RoleType.System,'native persona'),(RoleType.User,'question'),
                               (RoleType.System,'native style'),(RoleType.Assistant,'history'),
                               (RoleType.User,'new question')]]
        for enabled in (False, True):
            received = []
            async def handle(request):
                body = json.loads(request.content)
                received.append(body)
                roles = [m['role'] for m in body['messages']]
                if enabled and roles != ['system','user','assistant','user']:
                    return httpx.Response(400,json={'error':{'message':'System message must be at the beginning.'}})
                self.assertNotIn('authorization',request.headers)
                return httpx.Response(200,json=dict(id='fixture',object='chat.completion',created=0,
                    model='fixture',choices=[dict(index=0,finish_reason='stop',message=dict(role='assistant',content='OK'))],
                    usage=dict(prompt_tokens=1,completion_tokens=1,total_tokens=2)))
            provider = APIProvider(name='fixture',api_key='',base_url='http://fixture.invalid/v1',
                auth_type='none',system_messages_at_start=enabled)
            client = OpenaiClient(provider)
            await client.client.close()
            client.client = AsyncOpenAI(api_key='fixture',base_url=provider.base_url,max_retries=0,
                http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
            try:
                result = await client.get_response(ResponseRequest(model_info=ModelInfo(name='fixture',
                    model_identifier='fixture',api_provider='fixture',model_type='llm',force_stream_mode=False),
                    context_items=original,max_tokens=32))
                self.assertEqual(result.content,'OK')
                wire = received[0]['messages']
                self.assertEqual([m['role'] for m in wire],
                    ['system','user','assistant','user'] if enabled else ['system','user','system','assistant','user'])
                if enabled:
                    self.assertEqual(wire[0]['content'],'native persona\n\nnative style')
            finally:
                await client.client.close()
        self.assertEqual(len(original), 5)

    def test_pool_ranking_uses_health_then_family_then_capability(self):
        from scripts.maibot_api_pool_check import rank_native_tasks
        names=['Mistral-Small-3.2','deepseek','nvidia/nemotron-3.5-super-vl','broken_gemini','gemini']
        document={'model_task_config':{'planner':{'model_list':names},'embedding':{'model_list':['embed']}}}
        rows=[dict(name=n,model=n,passed=n!='broken_gemini',seconds=40 if 'nemotron' in n else 2) for n in names]
        result=rank_native_tasks(document, rows)
        self.assertEqual(result['model_task_config']['planner']['model_list'],['gemini','deepseek','nvidia/nemotron-3.5-super-vl','Mistral-Small-3.2','broken_gemini'])
        self.assertEqual(result['model_task_config']['embedding'],document['model_task_config']['embedding'])
        self.assertEqual(document['model_task_config']['planner']['model_list'],names)

    async def test_disabled_profiles_are_skipped_and_hot_enable_keeps_order(self):
        from unittest.mock import patch, AsyncMock
        from src.config.config import config_manager
        from src.llm_models.utils_model import LLMOrchestrator
        from src.config.model_configs import APIProvider, ModelInfo, TaskConfig
        from types import SimpleNamespace as NS
        providers=[APIProvider(name='g',api_key='',base_url='https://fixture.invalid',enabled=False),
            APIProvider(name='d',api_key='',base_url='https://fixture.invalid',auth_type='none')]
        models=[ModelInfo(name=n,api_provider=p,model_type='llm',model_identifier=n) for n,p in [('gemini','g'),('deepseek','d')]]
        task=TaskConfig(model_list=['gemini','deepseek'],selection_strategy='sequential')
        cfg=NS(models=models,api_providers=providers,model_task_config=NS(planner=task))
        from src.llm_models.model_client.base_client import client_registry
        with patch.object(config_manager,'get_model_config',return_value=cfg), \
             patch.object(client_registry,'get_client_class_instance_async',new=AsyncMock()) as factory:
            engine=LLMOrchestrator('planner')
            selected=await engine._select_model()
            self.assertEqual(selected[0].name,'deepseek')
            self.assertEqual(factory.await_count,1)
            self.assertEqual(task.model_list,['gemini','deepseek'])
            providers[0].api_key='fixture';providers[0].enabled=True
            selected=await engine._select_model()
            self.assertEqual(selected[0].name,'gemini')

    async def test_native_parallel_maps_real_protocol_and_stable_session(self):
        from unittest.mock import AsyncMock
        from src.web_search.config import WebServiceConfig
        from src.web_search.adapters.parallel import ParallelMCPAdapter
        from src.core.tooling import ToolInvocation, ToolExecutionContext, ToolExecutionResult
        from types import SimpleNamespace as NS
        mcp=NS(call_tool_invocation=AsyncMock(return_value=ToolExecutionResult(tool_name='web_search',success=True,
            structured_content={'results':[{'url':'https://docs.python.org','title':'Python','excerpts':['TaskGroup'], 'publish_date':None}]})))
        adapter=ParallelMCPAdapter(WebServiceConfig(adapter='parallel_mcp',server_ref='parallel'),mcp,
            ToolInvocation(tool_name='web_search',session_id='wechat-group'),ToolExecutionContext(session_id='wechat-group'))
        result=await adapter.search('TaskGroup',3)
        self.assertEqual(result.results[0].snippet,'TaskGroup')
        first=mcp.call_tool_invocation.call_args.args[0]
        self.assertEqual(first.metadata['mcp_server_name'],'parallel')
        self.assertEqual(first.arguments['search_queries'],['TaskGroup'])
        self.assertNotIn('wechat-group',json.dumps(first.arguments))
        page=await adapter.fetch('https://docs.python.org')
        self.assertEqual(page.content_kind,'page_excerpts')
        self.assertEqual(first.arguments['session_id'],mcp.call_tool_invocation.call_args.args[0].arguments['session_id'])

    async def test_native_mcp_tool_duplicates_removed_only_for_selected_server(self):
        from unittest.mock import AsyncMock, patch
        from types import SimpleNamespace as NS
        from src.mcp_module.provider import MCPToolProvider
        from src.config.config import config_manager
        from src.core.tooling import ToolSpec
        specs=[ToolSpec(name=n,metadata={'server_name':server}) for server,n in
            [('selected','web_search'),('selected','web_fetch'),('other','web_search'),('selected','other_tool')]]
        cfg=NS(model_task_config=NS(web_search=NS(model_list=['native'])),
            web=NS(services={'native':NS(adapter='parallel_mcp',server_ref='selected')}))
        with patch.object(config_manager,'get_model_config',return_value=cfg):
            result=await MCPToolProvider(NS(list_tools=AsyncMock(return_value=specs))).list_tools()
        self.assertEqual([(s.metadata['server_name'],s.name) for s in result],[('other','web_search'),('selected','other_tool')])

    async def test_native_parallel_error_is_not_fake_search_success(self):
        from unittest.mock import AsyncMock
        from types import SimpleNamespace as NS
        from src.web_search.config import WebRuntimeConfig, WebServiceConfig
        from src.web_search.provider import WebSearchToolProvider
        from src.web_search.service import WebSearchService
        from src.core.tooling import ToolInvocation, ToolExecutionResult
        mcp=NS(call_tool_invocation=AsyncMock(return_value=ToolExecutionResult(tool_name='web_search',success=False)))
        cfg=WebRuntimeConfig(enabled=True,search='native',fetch='native',services={
            'native':WebServiceConfig(adapter='parallel_mcp',server_ref='parallel')})
        result=await WebSearchToolProvider(WebSearchService(mcp),lambda:cfg).invoke(
            ToolInvocation(tool_name='web_search',session_id='session',arguments={'query':'TaskGroup'}))
        self.assertFalse(result.success)
        self.assertTrue(result.error_message)


if __name__=='__main__':
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(FormatAcceptance))
    report=dict(passed=result.wasSuccessful(),tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        physical_messages=False,external_requests=False,instance=str(INSTANCE))
    (ROOT/'docs/maibot-wechat-acceptance/llm-format-20261031.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
