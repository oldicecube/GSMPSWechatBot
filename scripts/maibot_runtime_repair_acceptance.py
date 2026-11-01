"""Offline regression for live-log repairs; isolated DB, no group/API requests."""
from __future__ import annotations

import asyncio
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
sys.path.insert(0, str(ROOT))
INSTANCE = Path(tempfile.mkdtemp(prefix='runtime-repair-', dir=ROOT/'temp'))
os.environ['MAIBOT_INSTANCE_DIR'] = str(INSTANCE)
(INSTANCE/'config').mkdir()
for name in ['bot_config.toml', 'model_config.toml']:
    shutil.copy2(ROOT/'config'/name, INSTANCE/'config'/name)

from src.llm_models import quota_cooldown as quota
from src.llm_models.exceptions import ModelCooldownError, RespNotOkException


class RepairAcceptance(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        quota._BLOCKED.clear()

    def test_expression_repair_preserves_content_and_native_enum(self):
        from sqlalchemy import create_engine
        from sqlmodel import Session, select
        from src.common.database.database_model import Expression, ModifiedBy
        from src.common.database.expression_compat import repair_legacy_expression_sources
        engine = create_engine('sqlite:///:memory:')
        Expression.__table__.create(engine)
        with Session(engine) as session:
            session.add_all([Expression(situation='hello', style='unchanged', content_list='[]',
                            count=7, checked=True, modified_by=x) for x in ['legacy', 'AI', 'USER', None]])
            session.commit()
        with engine.begin() as connection:
            self.assertEqual(repair_legacy_expression_sources(connection), 1)
            self.assertEqual(repair_legacy_expression_sources(connection), 0)
        with Session(engine) as session:
            rows = session.exec(select(Expression).order_by(Expression.id)).all()
            self.assertEqual([r.modified_by for r in rows], [None, ModifiedBy.AI, ModifiedBy.USER, None])
            self.assertTrue(all(r.style=='unchanged' and r.checked and r.count==7 for r in rows))
        engine.dispose()

    def test_quota_recovery_and_credential_isolation(self):
        provider = NS(name='p', base_url='https://fixture.invalid', api_key='old')
        model = NS(name='m', model_identifier='model', api_provider='p')
        cfg = NS(models=[model], api_providers=[provider], model_task_config=NS(planner=NS(model_list=['m'])))
        quota.mark_exhausted(provider, model)
        self.assertGreater(quota.task_retry_after(cfg,'planner'), 590)
        provider.api_key = 'new'
        self.assertEqual(quota.task_retry_after(cfg,'planner'), 0)
        provider.api_key = 'old'
        with patch.object(quota.time, 'monotonic', return_value=10**15):
            self.assertEqual(quota.task_retry_after(cfg,'planner'), 0)

    def test_new_opencode_quota_message_detected(self):
        self.assertTrue(quota.explicit_quota_exhaustion(RespNotOkException(429,'Free usage limit reached.')))
        self.assertFalse(quota.explicit_quota_exhaustion(RespNotOkException(429,'Please slow down')))

    def test_transient_rate_limit_honors_retry_after(self):
        provider = NS(base_url='https://fixture.invalid',api_key='')
        model = NS(model_identifier='model')
        cause = Exception('temporary rate limit')
        cause.response = NS(headers={'retry-after':'90'})
        error = RespNotOkException(429,'Please slow down')
        error.__cause__ = cause
        quota.mark_rate_limited(provider,model,error)
        self.assertGreater(quota.remaining_seconds(provider,model),89)
        self.assertLessEqual(quota.remaining_seconds(provider,model),90)

    async def test_reasoning_waits_and_recovers_without_task_restart(self):
        from src.maisaka import reasoning_engine as module
        runtime = NS(_running=True, log_prefix='[fixture]', _update_stage_status=Mock())
        engine = module.MaisakaReasoningEngine(runtime)
        calls = 0
        async def cycle():
            nonlocal calls
            calls += 1
            if calls==1:
                raise ModelCooldownError(30,'planner')
            runtime._running = False
        engine._run_loop = cycle
        with patch.object(module,'task_retry_after',side_effect=[30,0]), patch.object(module.asyncio,'sleep',new_callable=AsyncMock) as sleep:
            await engine.run_loop()
            sleep.assert_awaited_once_with(15.0)
        self.assertEqual(calls,2)
        runtime._update_stage_status.assert_called_once()

    async def test_reasoning_cooldown_is_cancellable(self):
        from src.maisaka import reasoning_engine as module
        runtime = NS(_running=True, log_prefix='[fixture]', _update_stage_status=Mock())
        engine = module.MaisakaReasoningEngine(runtime)
        engine._run_loop = AsyncMock(side_effect=ModelCooldownError(600,'planner'))
        entered = asyncio.Event()
        async def wait(_):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(module,'task_retry_after',return_value=600), patch.object(module.asyncio,'sleep',side_effect=wait):
            task = asyncio.create_task(engine.run_loop())
            await entered.wait()
            self.assertFalse(task.done())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        engine._run_loop.assert_awaited_once()

    async def test_shared_pacing_and_cancelled_waiter_cleanup(self):
        from src.llm_models import request_pacing as pacing
        provider = NS(base_url='https://fixture.invalid', api_key='', request_min_interval=.08)
        model = NS(model_identifier='fixture')
        pacing._GATES.clear()
        await pacing.wait_for_request_slot(provider,model)
        cancelled = asyncio.create_task(pacing.wait_for_request_slot(provider,model))
        await asyncio.sleep(.01)
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        start = asyncio.get_running_loop().time()
        await pacing.wait_for_request_slot(provider,model,'maisaka.planner')
        self.assertGreaterEqual(asyncio.get_running_loop().time()-start,.04)
        self.assertTrue(all(not gate.waiters for gate in pacing._GATES.values()))

    async def test_provider_image_cap_in_actual_request_projection(self):
        import base64
        import io
        from PIL import Image
        from src.config.config import config_manager
        from src.llm_models.utils_model import LLMOrchestrator, RequestType
        from src.llm_models.payload_content.context_item import ContextItemBuilder, ContextImagePart, ContextTextPart
        config = config_manager.get_model_config()
        model = config.models[0].model_copy(deep=True)
        provider = next(p for p in config.api_providers if p.name == model.api_provider).model_copy(deep=True)
        stream = io.BytesIO()
        Image.new('RGB',(32,32),'red').save(stream,format='PNG')
        image = base64.b64encode(stream.getvalue()).decode()
        builder = ContextItemBuilder().add_text_content('Keep message identity and text')
        for _ in range(6):
            builder.add_image_content('png', image)
        original = [builder.build()]
        orchestrator = LLMOrchestrator(task_name='planner', request_type='maisaka.acceptance')
        transport = AsyncMock(return_value=NS(usage=None))
        with patch.object(orchestrator,'_select_model',new=AsyncMock(return_value=(model,provider,Mock()))), \
             patch.object(orchestrator,'_attempt_request_on_model_with_timeout',new=transport), \
             patch.object(orchestrator,'_adjust_model_usage'):
            for limit, expected in [(5,5),(0,6)]:
                provider.max_images_per_request = limit
                await orchestrator._execute_request(request_type=RequestType.RESPONSE,
                    context_factory=lambda client: original, model_name=model.name)
                sent = transport.call_args.args[2].context_items
                self.assertEqual(sum(isinstance(p,ContextImagePart) for m in sent for p in m.parts),expected)
                self.assertEqual(sent[0].meta.item_id, original[0].meta.item_id)
                self.assertTrue(any(isinstance(p,ContextTextPart) and p.text=='Keep message identity and text' for p in sent[0].parts))
        self.assertEqual(sum(isinstance(p,ContextImagePart) for p in original[0].parts),6)

    def test_fx_backoff_retry_after_and_reset(self):
        import plugins.fx as fx
        import requests
        fx._FEED_BACKOFF.clear()
        response = requests.Response()
        response.status_code = 429
        response.headers['Retry-After'] = '120'
        failure = requests.HTTPError('rate limited', response=response)
        clock = NS(monotonic=Mock(return_value=1000.0))
        good = Mock()
        good.json.return_value = {'price':123}
        with patch.object(fx,'time',clock), patch.object(fx.requests,'get',side_effect=[failure,good]) as get:
            self.assertIsNone(fx._fetch_quote_payload('fixture'))
            self.assertIsNone(fx._fetch_quote_payload('fixture'))
            self.assertEqual(get.call_count,1)
            clock.monotonic.return_value = 1121.0
            self.assertEqual(fx._fetch_quote_payload('fixture'),{'price':123})
            self.assertNotIn('fixture',fx._FEED_BACKOFF)


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RepairAcceptance))
    report = dict(tests=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                  passed=result.wasSuccessful(), isolated_instance=str(INSTANCE), physical_messages=False)
    (ROOT/'docs/maibot-wechat-acceptance/runtime-repair-20261029.json').write_text(json.dumps(report,indent=2),encoding='utf8')
    raise SystemExit(0 if result.wasSuccessful() else 1)
