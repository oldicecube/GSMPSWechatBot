"""Synthetic, read-only web/API probes. No real chat history or sends."""
import importlib
import json
from pathlib import Path
import sys
import time
from types import ModuleType
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
rows=[]
pkg=ModuleType('life_live_probe');pkg.__path__=[str(ROOT/'plugins/autonomous_life')];sys.modules[pkg.__name__]=pkg
web=importlib.import_module(pkg.__name__+'.web')
for query,url in [('Minecraft redstone clock design',''), ('','https://www.python.org/')]:
    start=time.monotonic()
    try:
        result=web.explore(query=query,url=url)
        rows.append(dict(kind='web_search' if query else 'web_read',success=bool(result.get('results') or result.get('text')),
                         seconds=round(time.monotonic()-start,2),items=len(result.get('results',[])),source=result.get('url','bing RSS')))
    except Exception as e:
        rows.append(dict(kind='web',success=False,error=type(e).__name__,seconds=round(time.monotonic()-start,2)))
cfg=json.loads((ROOT/'config.json').read_text(encoding='utf8'))['llm']
for api in sorted((a for a in cfg['apis'] if a.get('enabled',True)),key=lambda x:x.get('priority',100)):
    start=time.monotonic()
    headers={'Content-Type':'application/json',**api.get('default_headers',{})}
    if api.get('auth_mode','bearer')=='bearer' and api.get('api_key'):
        headers['Authorization']='Bearer '+api['api_key']
    payload=dict(model=api['model'],messages=[dict(role='user',content='Synthetic autonomous planner health probe, no real chat data. Output only JSON: {"action":"rest","reason":"health probe"}.')],max_tokens=300)
    payload.update(api.get('thinking_params') or {})
    try:
        response=requests.post(api['base_url'].rstrip('/')+'/chat/completions',headers=headers,json=payload,timeout=(8,35))
        data=response.json()
        content=((data.get('choices') or [{}])[0].get('message') or {}).get('content') or ''
        try:
            content=content.strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip()
            valid=json.loads(content).get('action')=='rest'
        except Exception:
            valid=False
        rows.append(dict(kind='llm',provider=api.get('name'),status=response.status_code,success=valid,
                         seconds=round(time.monotonic()-start,2),error_code=str((data.get('error') or {}).get('code',''))[:80]))
        if valid:break
    except Exception as e:
        rows.append(dict(kind='llm',provider=api.get('name'),success=False,error=type(e).__name__,seconds=round(time.monotonic()-start,2)))
path=ROOT/'docs/maibot-wechat-acceptance/autonomous-life-live-probe-20261029.json'
path.write_text(json.dumps(dict(probes=rows,synthetic_only=True,physical_messages=False),ensure_ascii=False,indent=2),encoding='utf8')
print(json.dumps(rows,ensure_ascii=False))
