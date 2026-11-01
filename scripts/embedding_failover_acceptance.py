"""Deterministic transport failover checks; no live API or chat data."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.llm_models.model_client.embedding_failover import EmbeddingFailoverTransport


async def main():
    checks = []
    def check(name, passed):
        checks.append(dict(name=name, passed=bool(passed)))
        assert passed, name
    calls = []
    mode = 'network'
    def handler(request):
        calls.append(request)
        if request.url.host == 'primary.invalid':
            if mode == 'network':
                raise httpx.ConnectError('fixture connection reset', request=request)
            return httpx.Response(int(mode), json={'error': 'fixture'}, request=request)
        return httpx.Response(200, json={'data': [{'embedding': [0.1, 0.2]}]}, request=request)
    transport = EmbeddingFailoverTransport('https://primary.invalid/v1', ['https://backup.invalid/api/v1'], transport=httpx.MockTransport(handler))
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.post('https://primary.invalid/v1/embeddings', json={'model': 'same', 'input': 'synthetic'})
        check('network fallback succeeds', response.status_code == 200 and len(calls) == 2)
        check('fallback body preserved', calls[0].content == calls[1].content)
        check('fallback path correct', calls[1].url.path == '/api/v1/embeddings')
        check('fallback host header correct', calls[1].headers['host'] == 'backup.invalid')
        calls.clear()
        await client.post('https://primary.invalid/v1/embeddings', json={'model': 'same', 'input': 'next'})
        check('broken route cools down', len(calls) == 1 and calls[0].url.host == 'backup.invalid')
        calls.clear()
        mode = '400'
        response = await client.post('https://primary.invalid/v1/chat/completions', json={})
        check('chat requests never rerouted', response.status_code == 400 and len(calls) == 1)
    for mode in ('400', '401', '403', '429', '503'):
        calls.clear()
        transport = EmbeddingFailoverTransport('https://primary.invalid/v1', ['https://backup.invalid/api/v1'], transport=httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=transport) as client:
            response = await client.post('https://primary.invalid/v1/embeddings', json={})
            check(mode + ' respects status', response.status_code == (200 if mode == '503' else int(mode)))
            check(mode + ' dispatch count', len(calls) == (2 if mode == '503' else 1))
    raw = json.loads((ROOT / 'temp/embedding-free-routes-live-20261030.json').read_text(encoding='utf8'))
    old = next(r for r in raw if r['label'] == 'kilo_legacy')
    new = next(r for r in raw if r['label'] == 'kilo_gateway')
    check('live text routes identical', old['vectors'] == new['vectors'] and old['semantic_ok'] and new['semantic_ok'])
    ovh = next(r for r in raw if r['label'] == 'ovh_bge')
    check('live independent OVH semantics', ovh['status'] == 200 and ovh['semantic_ok'])
    check('different model never declared equivalent', ovh['vectors'] != old['vectors'])
    images = json.loads((ROOT / 'temp/embedding-image-routes-live-20261030.json').read_text(encoding='utf8'))
    check('live image routes identical', images['passed'] and images['identical'])
    # A request already in the pacing queue must not send another HTTP request
    # after a concurrent task puts the same provider/model into cooldown.
    from src.llm_models import utils_model
    from src.llm_models.quota_cooldown import mark_rate_limited
    provider = SimpleNamespace(name='queued-fixture', base_url='https://queued.invalid', api_key='fixture', max_retry=1)
    model = SimpleNamespace(name='queued-fixture', model_identifier='queued-fixture')
    request = SimpleNamespace(model_info=model, trace_context=None)
    client = SimpleNamespace(get_response=AsyncMock(), get_embedding=AsyncMock(), get_audio_transcriptions=AsyncMock())
    async def wait_and_receive_429(*args):
        mark_rate_limited(provider, model, utils_model.RespNotOkException(429, 'fixture throttled'))
    with patch.object(utils_model, 'wait_for_request_slot', wait_and_receive_429):
        try:
            await utils_model.LLMOrchestrator._attempt_request_on_model(
                SimpleNamespace(request_type='embedding.acceptance'), provider, client, request)
        except utils_model.ModelAttemptFailed:
            check('queued cooldown blocks dispatch', not any(fn.called for fn in (client.get_response, client.get_embedding, client.get_audio_transcriptions)))
        else:
            check('queued cooldown blocks dispatch', False)
    output = ROOT / 'temp/embedding-failover-acceptance.json'
    output.write_text(json.dumps(dict(passed=len(checks), checks=checks), indent=2), encoding='utf8')
    print(json.dumps(dict(passed=len(checks), report=str(output))))


if __name__ == '__main__':
    asyncio.run(main())
