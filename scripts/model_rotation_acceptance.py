"""Rate-limit rotation through the actual selector; no external model requests."""
import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
INSTANCE=tempfile.TemporaryDirectory(prefix='rotation-',dir=ROOT/'temp',ignore_cleanup_errors=True)
os.environ['MAIBOT_INSTANCE_DIR']=INSTANCE.name
(Path(INSTANCE.name)/'config').mkdir()
for name in ('bot_config.toml','model_config.toml'):
    shutil.copy2(ROOT/'config'/name,Path(INSTANCE.name)/'config'/name)
from src.llm_models import request_pacing as pace, quota_cooldown as quota
from src.llm_models import utils_model as mod
from src.llm_models.exceptions import ModelCooldownError, RespNotOkException


class Rotation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pace._GATES.clear();quota._BLOCKED.clear()
        self.models={name:NS(name=name,model_identifier=name,api_provider=name) for name in ('gemini','deepseek','fallback')}
        self.providers={name:NS(name=name,base_url='https://'+name+'.invalid/v1',api_key='fixture',
            request_min_interval=32,max_retry=1,retry_interval=0,enabled=True) for name in self.models}
        self.obj=object.__new__(mod.LLMOrchestrator)
        self.obj.task_name='utils';self.obj.request_type='maisaka.planner'
        self.obj.model_for_task=NS(selection_strategy='sequential',model_list=list(self.models),hard_timeout=1)
        self.obj.model_usage={name:(0,0,0) for name in self.models}
        self.obj._explicit_model_usage={};self.obj._refresh_task_config=Mock();self.obj._adjust_model_usage=Mock()
        self.obj._record_success_generation_attempt=Mock()
        self.obj._resolve_effective_session_id=lambda sid:sid
        self.client=NS(get_audio_transcriptions=AsyncMock(return_value=NS(usage=None)))
        patches=[patch.object(mod.TempMethodsLLMUtils,'get_model_info_by_name',side_effect=self.models.__getitem__),
                 patch.object(mod.TempMethodsLLMUtils,'get_provider_by_name',side_effect=self.providers.__getitem__),
                 patch.object(mod.client_registry,'get_client_class_instance_async',AsyncMock(return_value=self.client)),
                 patch.object(mod,'mark_request_succeeded',Mock())]
        for p in patches:p.start();self.addCleanup(p.stop)
        @asynccontextmanager
        async def usage(*args):yield
        p=patch.object(mod,'track_model_task_usage',usage);p.start();self.addCleanup(p.stop)
        self.obj._build_client_request=lambda **kw:NS(model_info=kw['model_info'],trace_context=kw['trace_context'])

    async def test_local_limit_rotates_and_returns_to_quality_priority(self):
        self.assertTrue(pace.try_request_slot(self.providers['gemini']))
        info,_,_=await self.obj._select_model();self.assertEqual(info.name,'deepseek')
        pace._GATES[pace._key(self.providers['gemini'])].next_at=time.monotonic()-1
        info,_,_=await self.obj._select_model();self.assertEqual(info.name,'gemini')

    async def test_generic_429_cools_whole_endpoint_not_just_model_name(self):
        provider=self.providers['gemini']
        quota.mark_rate_limited(provider,self.models['gemini'],RespNotOkException(429,'rate limited'))
        sibling=NS(model_identifier='same-endpoint-other-model')
        self.assertTrue(quota.blocked_reason(provider,sibling))
        self.assertFalse(quota.blocked_reason(self.providers['deepseek'],self.models['deepseek']))
        info,_,_=await self.obj._select_model();self.assertEqual(info.name,'deepseek')
        for key in list(quota._BLOCKED):quota._BLOCKED[key]=(time.monotonic()-1,'expired')
        info,_,_=await self.obj._select_model();self.assertEqual(info.name,'gemini')

    async def test_endpoint_slot_is_shared_and_atomic(self):
        provider=self.providers['gemini']
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as pool:
            results=list(pool.map(lambda n:pace.try_request_slot(provider,NS(model_identifier=str(n))),range(16)))
        self.assertEqual(sum(results),1)

    async def test_all_busy_returns_earliest_cooldown_without_penalty_or_request(self):
        for provider in self.providers.values():pace.try_request_slot(provider)
        with self.assertRaises(ModelCooldownError):
            await self.obj._execute_request(mod.RequestType.RESPONSE)
        self.client.get_audio_transcriptions.assert_not_awaited()
        self.assertFalse(any(call.kwargs.get('penalty_delta') for call in self.obj._adjust_model_usage.call_args_list))

    async def test_dispatch_race_rotates_without_waiting_or_sending_busy_endpoint(self):
        original=self.obj._select_model
        raced=False
        async def select(**kwargs):
            nonlocal raced
            result=await original(**kwargs)
            if not raced:
                raced=True;pace.try_request_slot(result[1],result[0])
            return result
        self.obj._select_model=select
        result=await self.obj._execute_request(mod.RequestType.RESPONSE)
        self.assertEqual(result.model_info.name,'deepseek')
        self.assertEqual(self.client.get_audio_transcriptions.await_count,1)
        self.assertFalse(any(call.kwargs.get('penalty_delta') for call in self.obj._adjust_model_usage.call_args_list))


if __name__=='__main__':unittest.main(verbosity=2)
