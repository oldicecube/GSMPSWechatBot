"""Retire duplicated shell settings after native ownership, with full backups.

The original shell plugins still own their business configuration. Only the
retired LLM subsystem's settings are removed. Run without --apply to preview.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RETIRED = {
    'apis', 'model', 'api_key', 'api_base', 'base_url', 'provider',
    'request_timeout_seconds', 'thinking_params',
    'context_window_tokens', 'context_compression_target_tokens',
    'context_compression_retry_seconds', 'cache_cost_ratio', 'cache_hit_rate',
    'cache_break_even_horizon', 'cache_prefix_tokens', 'group_message_max_records',
    'assistant_nickname', 'identity', 'memory', 'learning', 'prompt', 'emoji_dir',
    'image_tool_enabled', 'image_tool_max_calls', 'image_tool_max_bytes',
    'autonomy', 'reply_style', 'behavior_style', 'group_prompt', 'private_prompt',
    'behavior_boundaries', 'style_policy',
}
# Native ingestion owns density, batching, attention and reply penalties. These
# two fields remain transport admission controls used by ingest_context/main.
AUTO_ADMISSION = {'enabled', 'allowed_groups'}


def clean_config(config):
    result = copy.deepcopy(config)
    llm = result.get('llm') or {}
    if llm.get('model_config_source') != 'maibot':
        raise ValueError('MaiBot ownership must be established before cleanup')
    removed = []
    for key in sorted(RETIRED.intersection(llm)):
        del llm[key]
        removed.append('llm.' + key)
    scopes = [('llm', llm)]
    scopes.extend(('llm.group_profiles.' + name, profile)
                  for name, profile in llm.get('group_profiles', {}).items()
                  if isinstance(profile, dict))
    for path, scope in scopes:
        if path != 'llm':
            for key in sorted(RETIRED.intersection(scope)):
                del scope[key]
                removed.append(path + '.' + key)
        auto = scope.get('auto_reply')
        if isinstance(auto, dict):
            for key in sorted(set(auto) - AUTO_ADMISSION):
                del auto[key]
                removed.append(path + '.auto_reply.' + key)
        if path != 'llm' and scope.get('prompt') == {}:
            del scope['prompt']
            removed.append(path + '.prompt')
    return result, removed


def apply(root=ROOT, *, write=False):
    path = root / 'config.json'
    original_bytes = path.read_bytes()
    original = json.loads(original_bytes)
    cleaned, removed = clean_config(original)
    bot_path = root / 'config/bot_config.toml'
    model_path = root / 'config/model_config.toml'
    pending_path = root / 'config/gsmps_model_import_pending.json'
    bot_bytes = bot_path.read_bytes()
    model_bytes = model_path.read_bytes()
    pending_bytes = pending_path.read_bytes() if pending_path.exists() else None
    bot = tomllib.loads(bot_bytes.decode('utf8'))
    models = tomllib.loads(model_bytes.decode('utf8'))
    persona = bot.get('personality', {}).get('personality', '')
    if not persona or not models.get('models') or not models.get('api_providers'):
        raise ValueError('Native personality/model configuration is incomplete')
    llm = original['llm']
    identity = llm.get('identity') or {}
    rules = identity.get('rules') or []
    if isinstance(rules, str):
        rules = [rules]
    if not all(str(rule) in persona for rule in rules):
        raise ValueError('Some identity rules have not been preserved natively')
    for key in ('name', 'role', 'traits'):
        if identity.get(key) and str(identity[key]) not in persona:
            raise ValueError('Native personality lacks identity.' + key)
    if llm.get('assistant_nickname') and llm['assistant_nickname'] != bot['bot']['nickname']:
        raise ValueError('Native nickname differs from the shell nickname')
    if bot.get('webui', {}).get('enabled') and 'webui' in cleaned:
        # This was the retired shell WebUI, including its JWT credentials.
        # The native WebUI has independent settings in bot_config.toml.
        del cleaned['webui']
        removed.append('webui')

    # Preserve unimported endpoints exactly once. Never import deleted endpoints
    # on a subsequent cleanup/startup when llm.apis has already been removed.
    from scripts.adopt_maibot_model_pool import adopt, slug
    pending = json.loads(pending_bytes) if pending_bytes is not None else {'apis': []}
    existing = {p['name'] for p in models['api_providers']}
    candidates = list(pending['apis'])
    candidates += [api for api in llm.get('apis', [])
                   if 'WechatBot_' + slug(api['name']) not in existing]
    pending['apis'] = list({api['name']: api for api in candidates}.values())
    prospective = adopt({'llm': {'apis': pending['apis']}}, models)
    from src.config.config import ModelConfig
    ModelConfig.model_validate({k: v for k, v in prospective.items() if k != 'inner'})
    # Import is additive; active tasks/order and all existing model fields survive.
    if prospective['model_task_config'] != models['model_task_config']:
        raise ValueError('Endpoint preservation changed active tasks')
    result = dict(removed_count=len(removed), removed=removed, kept_llm_keys=list(cleaned['llm']),
                  pending_endpoints=len(pending['apis']),
                  native_persona_sha256=hashlib.sha256(persona.encode()).hexdigest(),
                  native_model_count_after_restart=len(prospective['models']),
                  native_provider_count_after_restart=len(prospective['api_providers']),
                  production_restart=False, applied=False)
    pending_unchanged = (json.loads(pending_bytes) == pending if pending_bytes is not None else not pending['apis'])
    if not write or (not removed and pending_unchanged):
        return result
    # Check all inputs again: do not overwrite a concurrent WebUI edit.
    if (path.read_bytes() != original_bytes or bot_path.read_bytes() != bot_bytes
            or model_path.read_bytes() != model_bytes
            or (pending_path.read_bytes() if pending_path.exists() else None) != pending_bytes):
        raise RuntimeError('Configuration changed during validation; rerun cleanup')
    backup = root / 'data/backups' / ('adopted-config-cleanup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    for source in (path, bot_path, model_path, pending_path):
        if source.exists():
            shutil.copy2(source, backup / source.name)
    if pending['apis']:
        temporary = pending_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(pending, ensure_ascii=False, indent=2), encoding='utf8')
        os.replace(temporary, pending_path)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    os.replace(temporary, path)
    result.update(applied=True, backup=str(backup))
    (backup / 'cleanup-report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(apply(write=args.apply), ensure_ascii=False, indent=2))
