"""Synthetic free API contract tests. Never sends chat history or changes config."""
import base64
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://ai-gateway.kilo.ai/api/v1'
MODELS = ['nvidia/nemotron-3.5-super-vl:free', 'dots-studio/dots-3-note-preview:free',
          'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free']


def parse(value):
    value = str(value).strip()
    if value.startswith('```'):
        value = value.split('\n', 1)[1].rsplit('```', 1)[0]
    return json.loads(value)


def run(model, base_url=BASE, api_key='', extra_params=None, max_tokens=1024, min_interval=0.0, tool_choice='forced'):
    checks = []
    last_start = None
    def post(kind, **payload):
        nonlocal last_start
        if last_start is not None:
            time.sleep(max(0.0, float(min_interval) - (time.monotonic()-last_start)))
        start = time.monotonic()
        last_start = start
        try:
            headers = {'User-Agent': 'WechatBot-API-Health/1.0'}
            if api_key:
                headers['Authorization'] = 'Bearer ' + api_key
            extras = {'reasoning': {'enabled': False}} if extra_params is None else extra_params
            response = requests.post(base_url.rstrip('/') + '/chat/completions', headers=headers,
                json=dict(model=model, max_tokens=max_tokens, **extras, **payload), timeout=(8, 90))
            data = response.json()
            message = ((data.get('choices') or [{}])[0].get('message') or {})
            item = dict(kind=kind, status=response.status_code, seconds=round(time.monotonic()-start, 2),
                response_model=data.get('model'), usage=data.get('usage'), success=False)
            if response.status_code != 200 or data.get('error'):
                error = str(data.get('error') or data.get('detail') or data.get('message') or '')[:500]
                item['error'] = error.replace(api_key, '[redacted]') if api_key else error
            checks.append(item)
            return message, item
        except Exception as exc:
            item = dict(kind=kind, success=False, error=type(exc).__name__, seconds=round(time.monotonic()-start, 2))
            checks.append(item)
            return {}, item
    message, item = post('planner_json', messages=[dict(role='system', content='Synthetic API contract test. Return valid JSON only.'),
        dict(role='user', content='Return {"action":"rest","reason":"health check"}.')])
    try:
        item['success'] = item.get('status') == 200 and parse(message.get('content', ''))['action'] == 'rest'
    except Exception:
        pass
    messages = [dict(role='user', content='Synthetic tool test. Call health_probe with value ok. Do not answer directly.')]
    message, item = post('tool_call', messages=messages, tools=[dict(type='function', function=dict(name='health_probe',
        description='Return synthetic health status', parameters=dict(type='object', properties=dict(value=dict(type='string', enum=['ok'])), required=['value'], additionalProperties=False)))],
        tool_choice='auto' if tool_choice == 'auto' else dict(type='function', function=dict(name='health_probe')))
    calls = message.get('tool_calls') or []
    try:
        item['success'] = (item.get('status') == 200 and len(calls) == 1 and calls[0]['function']['name'] == 'health_probe'
            and parse(calls[0]['function']['arguments'])['value'] == 'ok')
    except Exception:
        pass
    if item['success']:
        assistant = {k: v for k, v in message.items() if k in {'role', 'content', 'tool_calls', 'reasoning_content'}}
        assistant['role'] = 'assistant'
        message, item = post('tool_result_roundtrip', messages=[*messages, assistant,
            dict(role='tool', tool_call_id=calls[0]['id'], content='{"status":"ok","value":49}'),
            dict(role='user', content='Return only JSON {"value":49} from the tool result.')])
        try:
            item['success'] = item.get('status') == 200 and parse(message.get('content', ''))['value'] == 49
        except Exception:
            pass
    data = io.BytesIO(); Image.new('RGB', (48, 48), (255, 0, 0)).save(data, format='PNG')
    uri = 'data:image/png;base64,' + base64.b64encode(data.getvalue()).decode()
    message, item = post('vision', messages=[dict(role='user', content=[dict(type='text', text='Synthetic vision test. What is the dominant color in this image? Answer just one English color word.'),
        dict(type='image_url', image_url=dict(url=uri))])])
    text = str(message.get('content', '')).strip().lower()
    item['output_excerpt'] = text[:100]
    item['success'] = item.get('status') == 200 and ('red' in text or '\u7ea2' in text)
    return dict(model=model, base_url=base_url, checks=checks, passed=len(checks) == 4 and all(c['success'] for c in checks))


if __name__ == '__main__':
    rows = list(ThreadPoolExecutor(max_workers=3).map(run, MODELS))
    result = dict(date=datetime.now().isoformat(), synthetic_only=True, physical_messages=False, providers=rows)
    path = ROOT / 'docs/maibot-wechat-acceptance/free-api-contract-20261029.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(rows, ensure_ascii=True))
    sys.exit(0 if any(r['passed'] for r in rows) else 1)
