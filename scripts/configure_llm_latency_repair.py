"""Apply verified native routes; retain special vector tasks and persona bytes."""
import copy
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import tomlkit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage-priority-profiles',action='store_true',help='Require a manual restart before applying the complete disabled-profile priority list')
    args=parser.parse_args()
    model_path=ROOT/'config/model_config.toml'
    bot_path=ROOT/'config/bot_config.toml'
    model_before=model_path.read_bytes();bot_before=bot_path.read_bytes()
    doc=tomlkit.parse(model_before.decode('utf8'))
    report=json.loads((ROOT/'docs/maibot-wechat-acceptance/llm-new-models-20261031.json').read_text())
    verified=next(m for m in report['models'] if m['name']=='candidate_ovh_mistral32')
    assert verified['passed'], 'Mistral synthetic contracts must pass before enabling'
    models={m['name']:m for m in doc['models']}
    providers={p['name']:p for p in doc['api_providers']}
    before_special={k:copy.deepcopy(v.unwrap()) for k,v in doc['model_task_config'].items()
                    if k not in ('planner','replyer','utils','learner','memory','vlm','web_search')}
    name='wechat_mistral_small_3_2_24b'
    pname='FreeOVH_MistralSmall32'
    if pname not in providers:
        p=tomlkit.table()
        p.update(name=pname,base_url=verified['base_url'],api_key='',client_type='openai',auth_type='none',
                 timeout=30,max_retry=1,retry_interval=1,request_min_interval=32.0,max_images_per_request=5,
                 system_messages_at_start=True)
        doc['api_providers'].append(p);providers[pname]=p
    if name not in models:
        m=tomlkit.table()
        m.update(model_type='llm',model_identifier=verified['model'],name=name,api_provider=pname,visual=True,
                 force_stream_mode=False)
        doc['models'].append(m);models[name]=m
    # Preserve definitions for the dashboard; strict-system errors and repeated
    # bounded read timeouts currently disqualify these routes from automatic use.
    slow=('wechat_qwen3_5_397b_a17b','wechat_qwen3_8_27b')
    for n in slow:
        providers[models[n]['api_provider']]['enabled']=False
    for m in doc['models']:
        if m.get('model_type')!='llm':continue
        p=providers[m['api_provider']]
        if 'endpoints.kepler.ai.cloud.ovh.net' in p['base_url']:
            p['system_messages_at_start']=True
        if p.get('enabled',True):p['timeout']=30;p['max_retry']=1
    # No keyless Gemini or restricted DeepSeek proxy is silently enabled.
    # Family preference is applied to all actually enabled, usable definitions.
    order=['wechat_nvidia_nemotron_3_5_super_vl_free','wechat_dots_studio_dots_3_note_preview_free',name]
    from scripts.maibot_api_pool_check import family, capability_rank
    preferred=[n for n,m in models.items() if m.get('model_type')=='llm'
        and providers[m['api_provider']].get('enabled',True)
        and family({'model':m['model_identifier']})<2]
    # Do not automatically activate or trust unverified new preferred routes.
    if preferred:raise RuntimeError('New Gemini/DeepSeek route needs contract verification; configuration left untouched')
    # Do not add disabled profiles to the live task lists: the user's current
    # process predates the disabled-profile fallback fix and must not be
    # restarted by this script. Their provider/model definitions remain in WebUI.
    if args.stage_priority_profiles:
        # The native search schema already requires a restart; the old process
        # cannot load this staged schema, retaining its last valid live snapshot.
        assert doc.get('web',{}).get('services',{}).get('Parallel-Free-Search',{}).get('adapter')=='parallel_mcp'
        order += [n for n in ('wechat_gemini_3_8_flash','wechat_gemini_3_flash_preview',
            'wechat_deepseek_v4_1_flash','wechat_deepseek_ai_deepseek_v4_1_flash', *slow) if n in models]
    order=sorted(order,key=lambda n:(family({'model':models[n]['model_identifier']}),capability_rank({'model':models[n]['model_identifier']})))
    for task in ('planner','replyer','utils','learner','memory','vlm'):
        doc['model_task_config'][task]['model_list']=order
        doc['model_task_config'][task]['selection_strategy']='sequential'
        # Includes the 32-second anonymous OVH pacing wait. The old 90s per
        # candidate multiplied into several minutes across four routes.
        doc['model_task_config'][task]['hard_timeout']=45.0
    after_special={k:v.unwrap() for k,v in doc['model_task_config'].items() if k in before_special}
    assert before_special==after_special
    # Validate the exact serialized native schema before touching production.
    from src.config.config import ModelConfig
    serialized=tomlkit.dumps(doc)
    validation=tomlkit.parse(serialized).unwrap()
    validation.pop('inner',None)
    ModelConfig.model_validate(validation)
    if model_path.read_bytes()!=model_before or bot_path.read_bytes()!=bot_before:
        raise RuntimeError('Operator changed configuration; nothing written')
    backup=ROOT/'data/backups'/('llm-latency-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(parents=True)
    shutil.copy2(model_path,backup/model_path.name)
    pending=model_path.with_suffix('.repair.tmp');pending.write_text(serialized,encoding='utf8',newline='');os.replace(pending,model_path)
    assert bot_path.read_bytes()==bot_before
    result=dict(at=datetime.now().isoformat(),model_order=order,disabled_slow_routes=list(slow),
        special_tasks_unchanged=True,persona_config_unchanged=True,backup=str(backup),main_process_restarted=False)
    result['requires_user_main_process_restart']=args.stage_priority_profiles
    result['active_model_order']=[n for n in order if providers[models[n]['api_provider']].get('enabled',True)]
    (ROOT/'docs/maibot-wechat-acceptance/llm-config-repair-20261031.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    print(json.dumps(result),flush=True)

if __name__=='__main__':main()
