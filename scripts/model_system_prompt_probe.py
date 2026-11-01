"""Probe every configured LLM model with a tiny system-message request.

Disabled providers are reported as skipped; no credentials or response text is
printed. This script never touches the bot process or sends a chat message.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
from pathlib import Path
import tomllib
import httpx
import sys

ROOT = Path(__file__).resolve().parents[1]


def _headers(provider):
    api_key = str(provider.get('api_key') or '').strip()
    auth_type = str(provider.get('auth_type') or 'none').lower()
    if not api_key or api_key in {'placeholder', 'public', 'not-required'}:
        return {}
    if auth_type == 'bearer':
        return {'Authorization': 'Bearer ' + api_key}
    return {'Authorization': 'Bearer ' + api_key}


async def probe(model, provider):
    base = str(provider.get('base_url') or '').rstrip('/')
    if not base:
        return {'model': model['name'], 'provider': provider.get('name'), 'status': 'invalid_base_url'}
    payload = {
        'model': model['model_identifier'],
        'messages': [
            {'role': 'system', 'content': 'You are a connectivity probe. Reply with exactly SYSTEM_OK.'},
            {'role': 'user', 'content': 'Return SYSTEM_OK.'},
        ],
        'max_tokens': 16,
        'temperature': 0,
    }
    try:
        async with httpx.AsyncClient(timeout=float(provider.get('timeout', 20) or 20), trust_env=True) as client:
            response = await client.post(base + '/chat/completions', headers=_headers(provider), json=payload)
        if response.status_code >= 400:
            return {'model': model['name'], 'provider': provider.get('name'), 'status': 'http_error', 'code': response.status_code,
                    'detail': response.text[:160]}
        data = response.json()
        content = str(((data.get('choices') or [{}])[0].get('message') or {}).get('content') or '')
        return {'model': model['name'], 'provider': provider.get('name'),
                'status': 'pass' if content.strip() else 'empty_response', 'content_chars': len(content)}
    except Exception as exc:
        return {'model': model['name'], 'provider': provider.get('name'), 'status': type(exc).__name__}


async def main():
    doc = tomllib.loads((ROOT / 'config/model_config.toml').read_text(encoding='utf8'))
    providers = {str(p.get('name')): p for p in doc.get('api_providers', [])}
    models = [m for m in doc.get('models', []) if m.get('model_type') == 'llm']
    results = []
    for model in models:
        provider = providers.get(str(model.get('api_provider')))
        if provider is None:
            results.append({'model': model.get('name'), 'provider': model.get('api_provider'), 'status': 'missing_provider'})
            continue
        if provider.get('enabled', True) is False and '--include-disabled' not in sys.argv:
            results.append({'model': model.get('name'), 'provider': provider.get('name'), 'status': 'skipped_disabled_provider'})
            continue
        if provider.get('enabled', True) is False and not str(provider.get('api_key') or '').strip():
            results.append({'model': model.get('name'), 'provider': provider.get('name'), 'status': 'disabled_no_credentials'})
            continue
        results.append(await probe(model, provider))
    report = {'at': datetime.now().isoformat(), 'system_prompt': 'sent_as_messages_role_system', 'results': results}
    path = ROOT / 'docs/maibot-wechat-acceptance/model-system-prompt-probe-20261031.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return all(r['status'] in {'pass', 'skipped_disabled_provider'} for r in results)


if __name__ == '__main__':
    raise SystemExit(0 if asyncio.run(main()) else 1)
