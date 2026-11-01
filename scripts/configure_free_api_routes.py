"""Enable only free vision routes passing the synthetic API contract report."""
import json
from datetime import datetime
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    report = json.loads((ROOT/'docs/maibot-wechat-acceptance/free-api-contract-20261029.json').read_text(encoding='utf8'))
    catalog = json.loads((ROOT/'temp/free-api-research-20261029/kilo.json').read_text(encoding='utf8'))
    free = {m['id'] for m in catalog['data'] if all(float(m.get('pricing', {}).get(k, -1)) == 0 for k in ('prompt', 'completion'))
            and 'image' in m.get('architecture', {}).get('input_modalities', [])}
    passed = [p['model'] for p in report['providers'] if p['passed'] and p['model'] in free
              and p['base_url'] == 'https://ai-gateway.kilo.ai/api/v1']
    if not passed:
        raise RuntimeError('No verified free vision provider; config untouched')
    backup = ROOT/'temp'/('free-api-life-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    for name in ['config.json', 'config/model_config.toml', 'config/bot_config.toml', 'plugins/autonomous_life/config.toml']:
        target = backup/name; target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(ROOT/name, target)
    path = ROOT/'config.json'
    cfg = json.loads(path.read_text(encoding='utf8'))
    for api in cfg['llm']['apis']:
        if api['model'] in passed:
            api.update(enabled=True, base_url='https://ai-gateway.kilo.ai/api/v1', auth_mode='none',
                api_key='placeholder', supports_images=True, priority=100+passed.index(api['model']))
            api.pop('user_agent', None)
        elif api.get('enabled') and 'kilo.ai' in api.get('base_url', ''):
            api['enabled'] = False
    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2)+'\n', encoding='utf8')
    from llm.core.maibot_native_config import sync_native_config
    result = sync_native_config()
    print(json.dumps(dict(enabled_models=passed, native=result, backup=str(backup)), ensure_ascii=True))


if __name__ == '__main__':
    main()
