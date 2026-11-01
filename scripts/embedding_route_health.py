"""Fresh free-route probes using synthetic text; no chat contents or credentials in reports.

This does not change the active vector space or enable unverified providers.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
import tomllib

import requests

ROOT = Path(__file__).resolve().parents[1]
TEXTS = ['Minecraft服务器今天正常运行，没有故障。', '游戏服务器一切正常，可以进去玩。', '我想吃一碗牛肉面，再加一个鸡蛋。']


def cosine(a, b):
    if len(a) != len(b):
        return 0.0
    return sum(x*y for x, y in zip(a, b)) / math.sqrt(sum(x*x for x in a)*sum(y*y for y in b))


def probe(label, base, model, *, dimensions=None):
    payload = dict(model=model, input=TEXTS)
    if dimensions is not None:
        payload['dimensions'] = dimensions
    start = time.monotonic()
    result = dict(label=label, endpoint=base, model=model, available=False)
    vectors = []
    try:
        response = requests.post(base.rstrip('/')+'/embeddings', json=payload, timeout=(8, 35), allow_redirects=False)
        result['http_status'] = response.status_code
        if response.status_code == 200:
            rows = sorted(response.json().get('data', []), key=lambda item: item.get('index', 0))
            vectors = [r['embedding'] for r in rows]
            valid = len(vectors) == len(TEXTS) and all(v and all(isinstance(x, (float, int)) and math.isfinite(x) for x in v)
                and sum(x*x for x in v) > 1e-6 for v in vectors)
            result['dimensions'] = [len(v) for v in vectors]
            if valid:
                related, unrelated = cosine(vectors[0], vectors[1]), cosine(vectors[0], vectors[2])
                result.update(related=related, unrelated=unrelated, semantic_ok=related > unrelated + .1)
            result['available'] = valid and result.get('semantic_ok', False) and (dimensions is None or all(len(v)==dimensions for v in vectors))
        else:
            # Remote bodies may echo authentication information. Record status only.
            result['reason'] = ('credentials_required' if response.status_code in (401, 403)
                else 'rate_limited' if response.status_code == 429 else 'http_failure')
    except (requests.RequestException, ValueError, TypeError, KeyError, ZeroDivisionError) as exc:
        result['reason'] = type(exc).__name__
    result['seconds'] = round(time.monotonic()-start, 3)
    return result, vectors


def main():
    doc = tomllib.loads((ROOT/'config/model_config.toml').read_text(encoding='utf8'))
    providers = {p['name']: p for p in doc['api_providers']}
    primary = providers['FreeEmbeddingKilo']
    ovh = providers['FreeEmbeddingOVH']
    model = next(m for m in doc['models'] if m['name'] == doc['model_task_config']['embedding']['model_list'][0])
    args = [('kilo_primary', primary['base_url'], model['model_identifier'], 1024),
            ('kilo_gateway', primary['embedding_fallback_urls'][0], model['model_identifier'], 1024),
            ('ovh_bge', ovh['base_url'], 'bge-m3', None),
            ('openrouter_anonymous_candidate', 'https://openrouter.ai/api/v1', model['model_identifier'], 1024)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(probe, label, base, identifier, dimensions=dim) for label, base, identifier, dim in args]
        rows = [future.result() for future in futures]
    by_label = {r['label']: (r, v) for r, v in rows}
    old, new = by_label['kilo_primary'], by_label['kilo_gateway']
    identical = bool(old[0]['available'] and new[0]['available'] and len(old[1]) == len(new[1])
                     and all(cosine(a, b) > .99999 for a, b in zip(old[1], new[1])))
    report = dict(checked_at=datetime.now(timezone.utc).isoformat(), synthetic_only=True,
        routes=[r for r, _ in rows], same_model_routes_verified=identical,
        current_text_pool=doc['model_task_config']['embedding']['model_list'],
        independent_backup_registered=any(m['name']=='free_bge_m3_ovh_1024' for m in doc['models']),
        cross_model_fallback_enabled=False,
        passed=identical and by_label['ovh_bge'][0]['available'])
    path = ROOT/'temp/pending-task-recheck-20261030/embedding-route-health.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
