"""One-time endpoint import; later WebUI edits are authoritative, even deletions."""
from pathlib import Path
import copy
from datetime import datetime
import json
import os
import re
import shutil
import sys
import tomlkit

ROOT = Path(__file__).resolve().parents[1]


def slug(value):
    return re.sub(r'[^A-Za-z0-9]+', '_', str(value)).strip('_').lower() or 'model'


def adopt(legacy, native):
    result = copy.deepcopy(native)
    if (legacy.get('llm') or {}).get('model_config_source') == 'maibot':
        return result
    models = result.setdefault('models', [])
    providers = result.setdefault('api_providers', [])
    provider_names = {p['name'] for p in providers}
    names = {m['name'] for m in models}
    pairs = {(m['api_provider'], m['model_identifier']) for m in models}
    for api in (legacy.get('llm') or {}).get('apis', []):
        provider_name = 'WechatBot_' + slug(api['name'])
        if provider_name not in provider_names:
            provider = dict(name=provider_name, enabled=bool(api.get('enabled', True)), base_url=str(api.get('base_url') or ''),
                api_key=str(api.get('api_key') or ''), client_type=api.get('protocol') or 'openai',
                auth_type=api.get('auth_mode') or 'bearer', timeout=int(api.get('timeout_seconds') or 90),
                max_retry=1, retry_interval=1, request_min_interval=float(api.get('request_min_interval') or 0),
                max_images_per_request=int(api.get('max_images_per_request') or 0))
            for key in ('default_headers', 'default_query', 'auth_header_name', 'auth_header_prefix', 'auth_query_name'):
                if key in api:
                    provider[key] = api[key]
            providers.append(provider)
            provider_names.add(provider_name)
        identifier = str(api.get('model') or api['name'])
        pair = (provider_name, identifier)
        if pair in pairs:
            continue
        name = 'wechat_' + slug(identifier)
        if name in names:
            name += '__' + slug(api['name'])
        suffix = 2
        candidate = name
        while candidate in names:
            candidate = name + '_' + str(suffix)
            suffix += 1
        name = candidate
        model = dict(model_type='llm', model_identifier=identifier, name=name, api_provider=provider_name,
                     price_in=0.0, price_out=0.0, cache=bool(api.get('cache', True)), visual=bool(api.get('supports_images', False)))
        if isinstance(api.get('max_tokens'), int):
            model['max_tokens'] = api['max_tokens']
        if api.get('thinking_params'):
            model['extra_params'] = copy.deepcopy(api['thinking_params'])
        models.append(model)
        names.add(name)
        pairs.add(pair)
    # Task lists and their order are intentionally kept verbatim. A dormant
    # endpoint is visible for management, but cannot be selected by any task.
    return result


def main():
    path = ROOT / 'config.json'
    model_path = ROOT / 'config/model_config.toml'
    legacy = json.loads(path.read_text(encoding='utf8'))
    native = tomlkit.loads(model_path.read_text(encoding='utf8')).unwrap()
    if legacy['llm'].get('model_config_source') == 'maibot':
        print('Already managed by MaiBot; no endpoints reimported.')
        return
    imported = adopt(legacy, native)
    from src.config.config import ModelConfig
    ModelConfig.model_validate({k: v for k, v in imported.items() if k != 'inner'})
    backup = ROOT / 'data/backups' / ('native-model-authority-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True)
    for source in (path, model_path):
        shutil.copy2(source, backup / source.name)
    temporary = model_path.with_suffix('.tmp')
    temporary.write_text(tomlkit.dumps(imported), encoding='utf8')
    os.replace(temporary, model_path)
    legacy['llm']['model_config_source'] = 'maibot'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding='utf8')
    os.replace(temporary, path)
    print(json.dumps({'models': len(imported['models']), 'providers': len(imported['api_providers']),
                      'task_order_preserved': imported['model_task_config'] == native['model_task_config'], 'backup': str(backup)}))


if __name__ == '__main__':
    sys.path.insert(0, str(ROOT))
    main()
