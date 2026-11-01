"""Configure verified Gemini/DeepSeek free-account routes ahead of fallbacks.

Default: stage disabled profiles. With --provider, require a local credential and
pass JSON, tool call/result, and vision contracts before enabling any route.
No process restarts or group messages. Google keys must belong to a free-tier
project; do not provide a paid project's key for free-only deployment.
"""
from __future__ import annotations
import argparse
import copy
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROFILES = [
    dict(name='WechatLLM-Free-Google-Gemini-3.8-Flash', channel='google',
         model='gemini-3.8-flash', base_url='https://generativelanguage.googleapis.com/v1beta/openai',
         priority=1, thinking_params={'reasoning_effort':'low'}),
    dict(name='WechatLLM-Free-Navy-DeepSeek-V4.1-Flash', channel='navy',
         model='deepseek-v4.1-flash', base_url='https://api.navy/v1', priority=3),
    dict(name='WechatLLM-Free-Navy-Gemini-3-Flash', channel='navy',
         model='gemini-3-flash-preview', base_url='https://api.navy/v1', priority=2),
]
ENV_KEYS = dict(google='GEMINI_API_KEY', navy='NAVY_API_KEY')


def load_key(channel, path=None):
    if path:
        text = Path(path).read_text(encoding='utf-8-sig').strip()
        match = re.search(r'^\s*(?:export\s+)?'+ENV_KEYS[channel]+r'\s*=\s*[\"\']?([^\s\"\']+)', text, re.M)
        return match.group(1) if match else text
    return os.environ.get(ENV_KEYS[channel], '').strip()


def stage(cfg):
    cfg = copy.deepcopy(cfg)
    apis = cfg['llm']['apis']
    for spec in PROFILES:
        api = next((a for a in apis if a.get('name') == spec['name']), None)
        if api is None:
            api = dict(enabled=False, api_key='')
            apis.append(api)
        api.update({k:v for k,v in spec.items() if k != 'channel'})
        api.update(protocol='openai', auth_mode='bearer', supports_images=True, timeout_seconds=90)
    names = {p['name'] for p in PROFILES}
    for api in apis:
        if 'integrate.api.nvidia.com' in str(api.get('base_url', '')):
            api.update(enabled=False, priority=900)
        elif api.get('name') not in names:
            priority = int(api.get('priority', 100))
            if not any(x in str(api.get('model', '')).lower() for x in ('deepseek','gemini')):
                api['priority'] = max(100, priority)
    apis.sort(key=lambda a:int(a.get('priority', 100)))
    return cfg


def check_navy_catalog(specs):
    response = requests.get('https://api.navy/v1/models', timeout=(8,20))
    response.raise_for_status()
    catalog = {m['id']:m for m in response.json()['data']}
    for spec in specs:
        model = catalog.get(spec['model'], {})
        if model.get('premium') is not False or model.get('supports_vision') is not True or model.get('supports_tools') is not True:
            raise ValueError('Navy model no longer advertises free-plan vision/tools; config untouched')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--provider', choices=ENV_KEYS)
    parser.add_argument('--key-file', type=Path)
    args = parser.parse_args()
    if args.key_file and not args.provider:
        parser.error('--key-file requires --provider')
    path = ROOT/'config.json'
    cfg = stage(json.loads(path.read_text(encoding='utf8')))
    enabled = []
    if args.provider:
        key = load_key(args.provider, args.key_file)
        if not key or any(x.isspace() for x in key) or len(key)<16:
            raise SystemExit('Missing/invalid local credential; config untouched')
        specs = [p for p in PROFILES if p['channel']==args.provider]
        if args.provider=='navy':
            check_navy_catalog(specs)
        from scripts.maibot_free_api_probe import run
        reports = [run(s['model'], base_url=s['base_url'], api_key=key,
                       extra_params=s.get('thinking_params', {}), max_tokens=8192) for s in specs]
        report_path = ROOT/'docs/maibot-wechat-acceptance'/('preferred-'+args.provider+'-contract-20261029.json')
        report_path.write_text(json.dumps(dict(synthetic_only=True, providers=reports),indent=2)+'\n',encoding='utf8')
        if not all(r['passed'] for r in reports):
            print(json.dumps(dict(config_untouched=True, authenticated_contract_passed=False, report=str(report_path))))
            return 1
        for api in cfg['llm']['apis']:
            if api.get('name') in {s['name'] for s in specs}:
                api.update(enabled=True, api_key=key)
                enabled.append(api['model'])
    backup = ROOT/'temp'/('preferred-free-api-backup-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    for name in ['config.json','config/model_config.toml','config/bot_config.toml']:
        target = backup/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/name,target)
    path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    from llm.core.maibot_native_config import sync_native_config
    native = sync_native_config()
    print(json.dumps(dict(staged_models=[p['model'] for p in PROFILES], newly_verified_enabled=enabled,
                         awaiting_keys=not bool(args.provider), native=native, backup=str(backup))))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
