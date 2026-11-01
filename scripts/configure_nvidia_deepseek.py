"""Stage NVIDIA DeepSeek first; enable only after authenticated contract checks.

No production process control or group messages. Credentials are never printed.
Use --key-file PATH (raw key or NVIDIA_API_KEY=...) or NVIDIA_API_KEY environment.
Without credentials, only stages a disabled route and demotes other models.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
BASE = 'https://integrate.api.nvidia.com/v1'
MODEL = 'deepseek-ai/deepseek-v4.1-flash'
NAME = 'WechatLLM-Free-NVIDIA-DeepSeek-V4.1-Flash'


def read_key(path=None):
    if not path:
        return os.environ.get('NVIDIA_API_KEY', '').strip()
    value = Path(path).read_text(encoding='utf-8-sig').strip()
    match = re.search(r'^\s*(?:export\s+)?NVIDIA_API_KEY\s*=\s*[\"\']?([^\s\"\']+)', value, re.M)
    return match.group(1) if match else value


def prepare_config(cfg, key='', verified=False):
    apis = cfg['llm']['apis']
    route = next((a for a in apis if a.get('name') == NAME), None)
    if route is None:
        route = dict(name=NAME, enabled=False, api_key='')
        apis.append(route)
    gemini_priorities = [int(a.get('priority', 1)) for a in apis
                         if a.get('enabled', True) and a.get('last_health', {}).get('passed') is not False
                         and 'gemini' in str(a.get('model', '')).lower()]
    route.update(model=MODEL, base_url=BASE, priority=max([2, *gemini_priorities]) + 1 if verified else 900, protocol='openai',
                 auth_mode='bearer', supports_images=True, timeout_seconds=90)
    # Only this exact authenticated four-part contract may enable this route.
    if verified and key:
        route.update(enabled=True, api_key=key)
        route['last_health'] = dict(passed=True, state='available', checked_at=datetime.now().isoformat())
    for api in apis:
        if api is not route and verified and not any(name in str(api.get('model', '')).lower() for name in ('gemini', 'deepseek')):
            priority = int(api.get('priority', 100))
            api['priority'] = max(100, priority)
    apis.sort(key=lambda a: (a['priority'], a.get('name', '')))
    return cfg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', type=Path)
    args = parser.parse_args()
    path = ROOT / 'config.json'
    cfg = json.loads(path.read_text(encoding='utf8'))
    key = read_key(args.key_file)
    if not key:
        key = next((a.get('api_key', '') for a in cfg['llm']['apis']
                    if a.get('name') == NAME), '')
    verified = False
    if key:
        if not re.fullmatch(r'nvapi-[A-Za-z0-9_-]{16,}', key):
            raise SystemExit('Invalid NVIDIA credential file; config untouched')
        from scripts.maibot_free_api_probe import run
        report = run(MODEL, base_url=BASE, api_key=key, extra_params={}, max_tokens=8192)
        report['synthetic_only'] = True
        report_path = ROOT / 'docs/maibot-wechat-acceptance/nvidia-deepseek-contract-20261029.json'
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
        verified = report['passed']
        if not verified:
            print(json.dumps(dict(enabled=False, config_untouched=True, report=str(report_path))))
            return 1
    backup = ROOT / 'temp' / ('nvidia-deepseek-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    for name in ['config.json', 'config/model_config.toml', 'config/bot_config.toml']:
        target = backup / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    prepare_config(cfg, key, verified)
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    from llm.core.maibot_native_config import sync_native_config
    native = sync_native_config()
    print(json.dumps(dict(model=MODEL, priority=next(a['priority'] for a in cfg['llm']['apis'] if a['name'] == NAME), enabled=next(a['enabled'] for a in cfg['llm']['apis']
                    if a['name'] == NAME), authenticated_contract_passed=verified,
                    awaiting_key=not bool(key), native=native, backup=str(backup))))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
