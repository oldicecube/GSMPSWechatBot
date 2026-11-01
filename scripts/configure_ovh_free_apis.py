"""Enable only freshly verified anonymous OVH vision/tool fallback routes."""
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
PROFILES = [
    ('Qwen3.5-397B-A17B',200,'dsh-ovh-Qwen3.5-auto.json'),
    ('Qwen3.8-27B',210,'dsh-ovh-Qwen3.8.json'),
]


def configure():
    for model, _, report in PROFILES:
        path = ROOT/'docs/maibot-wechat-acceptance'/report
        data = json.loads(path.read_text(encoding='utf8'))
        required = {'planner_json','tool_call','tool_result_roundtrip','vision'}
        if (time.time()-path.stat().st_mtime>21600 or not data.get('passed') or data.get('model')!=model
                or {c['kind'] for c in data['checks'] if c.get('success')} != required):
            raise RuntimeError('Missing fresh successful contract: '+model)
    backup = ROOT/'temp'/('ovh-api-config-backup-'+datetime.now().strftime('%Y%m%d-%H%M%S'))
    for name in ['config.json','config/model_config.toml','config/bot_config.toml']:
        target = backup/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(ROOT/name,target)
    path = ROOT/'config.json'
    cfg = json.loads(path.read_text(encoding='utf8'))
    apis = cfg['llm']['apis']
    for model,priority,_ in PROFILES:
        name = 'WechatLLM-Free-OVH-'+model
        api = next((a for a in apis if a.get('name')==name),None)
        if api is None:
            api = {};apis.append(api)
        api.update(name=name,model=model,base_url='https://oai.endpoints.kepler.ai.cloud.ovh.net/v1',
                   protocol='openai',auth_mode='none',api_key='not-required',enabled=True,
                   supports_images=True,priority=priority,request_min_interval=32.0,max_images_per_request=5,
                   timeout_seconds=90,thinking_params={})
    apis.sort(key=lambda a:int(a.get('priority',100)))
    path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    from llm.core.maibot_native_config import sync_native_config
    result = sync_native_config()
    print(json.dumps(dict(backup=str(backup),native=result,enabled=[m for m,_,_ in PROFILES],restarted=False)))


if __name__=='__main__':
    configure()
