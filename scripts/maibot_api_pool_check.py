"""Synthetic API health check and optional availability-first pool ordering."""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import time
import tomlkit
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def family(api):
    model=str(api.get('model','')).lower()
    return 0 if 'gemini' in model else 1 if 'deepseek' in model else 2


def capability_rank(api):
    """Operator's general capability preference, independent of network latency.

    Endpoint availability is checked separately. This is an explicit deployment
    policy, not a claim to be a universal intelligence benchmark.
    """
    model=str(api.get('model', '')).lower()
    order=('nemotron-3.5-super-vl', 'dots-3-note', 'qwen3.5-397b', 'qwen3.8-27b',
           'mistral-small-3.2', 'nemotron-3-nano-omni')
    return next((i for i, fragment in enumerate(order) if fragment in model), len(order))


def probe(api):
    row=dict(name=api['name'],model=api['model'],enabled=api.get('enabled',True))
    key=str(api.get('api_key') or '')
    mode=api.get('auth_mode') or 'bearer'
    if mode!='none' and not key:
        return dict(row,state='missing_credentials',passed=False)
    headers={'User-Agent':'WechatBot-Synthetic-Health/1.0', **(api.get('default_headers') or {})}
    if mode=='bearer': headers['Authorization']='Bearer '+key
    elif mode!='none': return dict(row,state='unsupported_auth_probe',passed=False)
    started=time.monotonic()
    try:
        response=requests.post(api['base_url'].rstrip('/')+'/chat/completions',headers=headers,
            json=dict(model=api['model'],messages=[dict(role='user',content='Synthetic health check. Reply exactly OK.')],max_tokens=256,
                      **(api.get('thinking_params') or {})),timeout=(8,90))
        row.update(status=response.status_code,seconds=round(time.monotonic()-started,2))
        data=response.json()
        message=((data.get('choices') or [{}])[0].get('message') or {})
        if response.status_code==200 and str(message.get('content') or '').strip():
            # Do not enable/reorder a route as healthy on text alone.
            interval=float(api.get('request_min_interval') or 0)
            time.sleep(max(0,interval-(time.monotonic()-started)))
            from scripts.maibot_free_api_probe import run
            contract=run(api['model'],base_url=api['base_url'],api_key=key if mode!='none' else '',
                extra_params=api.get('thinking_params') or {},max_tokens=2048,min_interval=interval,tool_choice='auto')
            row.update(contract=contract,passed=contract['passed'],state='available' if contract['passed'] else 'contract_failed')
        else:
            error=str(data.get('error') or data.get('message') or data.get('detail') or '')[:400]
            row.update(state='upstream_unavailable',passed=False,error=error.replace(key,'[redacted]') if key else error)
    except Exception as exc:
        row.update(state='network_error',passed=False,error=type(exc).__name__,seconds=round(time.monotonic()-started,2))
    return row


def apply_results(cfg,rows):
    by_name={r['name']:r for r in rows}
    apis=cfg['llm']['apis']
    for api in apis:
        result=by_name[api['name']]
        api['last_health']=dict(checked_at=datetime.now().isoformat(),state=result['state'],passed=result['passed'])
    # Availability is authoritative; family preference applies within each tier.
    def rank(api):
        row=by_name[api['name']]
        tier=0 if row['passed'] else 2 if row['state']=='missing_credentials' else 1
        return tier,family(api),int(api.get('priority',100))
    apis.sort(key=rank)
    for index,api in enumerate(apis):
        tier=rank(api)[0]
        api['priority']=(10 if tier==0 else 500 if tier==1 else 800)+index
    return cfg


def native_entries(document):
    """Read endpoints from the authoritative pool; never recreate llm.apis."""
    providers = {p['name']: p for p in document['api_providers']}
    entries = []
    for model in document['models']:
        if model.get('model_type', 'llm') != 'llm':
            continue
        provider = providers[model['api_provider']]
        entries.append(dict(name=model['name'], model=model['model_identifier'],
            enabled=provider.get('enabled', True), base_url=provider['base_url'],
            api_key=provider.get('api_key', ''), auth_mode=provider.get('auth_type', 'bearer'),
            default_headers=provider.get('default_headers') or {},
            thinking_params=model.get('extra_params') or {},
            request_min_interval=provider.get('request_min_interval', 0)))
    return entries


def rank_native_tasks(document, rows):
    """Reorder existing task members only; do not enable or add models."""
    import copy
    result = copy.deepcopy(document)
    by_name = {r['name']: r for r in rows}
    for task in result['model_task_config'].values():
        names = task.get('model_list', [])
        # Preserve unrelated embedding/audio/search tasks exactly.
        if names and all(name in by_name for name in names):
            def rank(name):
                row = by_name[name]
                return not row['passed'], family(row), capability_rank(row), names.index(name)
            task['model_list'] = sorted(names, key=rank)
    return result


def check_native_pool(apply=False):
    path = ROOT / 'config/model_config.toml'
    snapshot = path.read_bytes()
    document = tomlkit.loads(snapshot.decode('utf8')).unwrap()
    def enabled_probe(api):
        if not api['enabled']:
            return dict(name=api['name'], model=api['model'], enabled=False, state='disabled', passed=False)
        return probe(api)
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(enabled_probe, native_entries(document)))
    report = dict(at=datetime.now().isoformat(), synthetic_only=True, physical_messages=False,
                  source='maibot', apis=rows, restarted=False)
    if apply:
        ranked = rank_native_tasks(document, rows)
        if path.read_bytes() != snapshot:
            raise RuntimeError('Native pool changed while probing; left untouched')
        backup = ROOT / 'data/backups' / ('native-api-pool-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
        backup.mkdir(parents=True)
        shutil.copy2(path, backup / path.name)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(tomlkit.dumps(ranked), encoding='utf8')
        os.replace(temporary, path)
        report.update(backup=str(backup), tasks=ranked['model_task_config'])
    dest = ROOT / 'docs/maibot-wechat-acceptance' / ('api-pool-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.json')
    dest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(dict(report=str(dest), source='maibot', applied=apply,
        states=[{k:r.get(k) for k in ['model','state','passed','status']} for r in rows])))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    path=ROOT/'config.json'
    cfg=json.loads(path.read_text(encoding='utf8'))
    if cfg.get('llm', {}).get('model_config_source') == 'maibot':
        return check_native_pool(args.apply)
    rows=list(ThreadPoolExecutor(max_workers=4).map(probe,cfg['llm']['apis']))
    report=dict(at=datetime.now().isoformat(),synthetic_only=True,physical_messages=False,apis=rows,restarted=False)
    if args.apply:
        # Preserve concurrent operator edits; merge health/order into latest config.
        latest=json.loads(path.read_text(encoding='utf8'))
        if {a['name'] for a in latest['llm']['apis']}!={r['name'] for r in rows}:
            raise RuntimeError('Pool changed while probing; inspect report before applying')
        for api in latest['llm']['apis']:
            old=next(a for a in cfg['llm']['apis'] if a['name']==api['name'])
            if any(api.get(k)!=old.get(k) for k in ['base_url','model','api_key','auth_mode']):
                raise RuntimeError('Endpoint changed while probing; config left untouched')
        backup=ROOT/'temp'/('api-pool-backup-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
        for name in ['config.json','config/model_config.toml','config/bot_config.toml']:
            target=backup/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(ROOT/name,target)
        latest=apply_results(latest,rows)
        path.write_text(json.dumps(latest,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
        from llm.core.maibot_native_config import sync_native_config
        report.update(backup=str(backup),native=sync_native_config(),order=[dict(name=a['name'],priority=a['priority'],enabled=a.get('enabled',True)) for a in latest['llm']['apis']])
    dest=ROOT/'docs/maibot-wechat-acceptance'/('api-pool-'+datetime.now().strftime('%Y%m%d-%H%M%S')+'.json')
    dest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(dict(report=str(dest),states=[{k:r.get(k) for k in ['model','state','passed','status']} for r in rows],applied=args.apply)))


if __name__=='__main__': main()
