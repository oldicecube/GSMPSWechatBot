"""Register tested free routes without changing the active vector space."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tomlkit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    path = ROOT / 'config/model_config.toml'
    document = tomlkit.loads(path.read_text(encoding='utf8')).unwrap()
    legacy = json.loads((ROOT / 'config.json').read_text(encoding='utf8'))
    backup = ROOT / 'data/backups' / ('embedding-routes-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True)
    shutil.copy2(path, backup / path.name)
    # A still-running older validator does not know enabled=False. Defer only
    # those newly imported, unreferenced endpoints whose credentials are empty.
    dormant = {p['name'] for p in document['api_providers'] if p.get('enabled') is False
               and not p.get('api_key') and p.get('auth_type', 'bearer') != 'none'}
    affected = {m['name'] for m in document['models'] if m['api_provider'] in dormant}
    for task in document['model_task_config'].values():
        if isinstance(task, dict) and affected.intersection(task.get('model_list', [])):
            raise ValueError('A deferred endpoint is referenced by a task')
    from scripts.adopt_maibot_model_pool import slug
    present = {p['name'] for p in document['api_providers']}
    apis = [a for a in legacy.get('llm', {}).get('apis', []) if 'WechatBot_' + slug(a['name']) in dormant or 'WechatBot_' + slug(a['name']) not in present]
    if apis:
        if len([a for a in apis if 'WechatBot_' + slug(a['name']) in dormant]) != len(dormant):
            raise ValueError('Cannot preserve all deferred endpoint declarations')
        pending = path.parent / 'gsmps_model_import_pending.json'
        if pending.exists():
            shutil.copy2(pending, backup / pending.name)
            apis = list({a['name']: a for a in [*json.loads(pending.read_text(encoding='utf8'))['apis'], *apis]}.values())
        temporary = pending.with_suffix('.tmp')
        temporary.write_text(json.dumps({'apis': apis}, ensure_ascii=False, indent=2), encoding='utf8')
        os.replace(temporary, pending)
        document['api_providers'] = [p for p in document['api_providers'] if p['name'] not in dormant]
        document['models'] = [m for m in document['models'] if m['name'] not in affected]
    for provider in document['api_providers']:
        if provider['name'] == 'FreeEmbeddingKilo':
            provider['embedding_fallback_urls'] = ['https://ai-gateway.kilo.ai/api/v1']
            provider['request_min_interval'] = 1.5
            provider['max_retry'] = 2
    name = 'FreeEmbeddingOVH'
    if not any(p['name'] == name for p in document['api_providers']):
        document['api_providers'].append(dict(name=name, base_url='https://oai.endpoints.kepler.ai.cloud.ovh.net/v1',
            auth_type='none', api_key='', client_type='openai', timeout=40, max_retry=1, retry_interval=2,
            request_min_interval=31.0))
    if not any(m['name'] == 'free_bge_m3_ovh_1024' for m in document['models']):
        document['models'].append(dict(model_type='embedding', model_identifier='bge-m3',
            name='free_bge_m3_ovh_1024', api_provider=name, price_in=0.0, price_out=0.0))
    from src.config.config import ModelConfig
    ModelConfig.model_validate({k: v for k, v in document.items() if k != 'inner'})
    temporary = path.with_suffix('.tmp')
    temporary.write_text(tomlkit.dumps(document), encoding='utf8')
    os.replace(temporary, path)
    print(json.dumps(dict(backup=str(backup), deferred=len(apis), active_embedding_pool=document['model_task_config']['embedding']['model_list'],
        alternative_model='free_bge_m3_ovh_1024', restart_required_for_transport=True)))


if __name__ == '__main__':
    main()
