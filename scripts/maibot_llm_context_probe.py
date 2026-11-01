"""Synthetic longer-context latency check. No production conversations leave disk."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
import tomllib
import requests

ROOT=Path(__file__).resolve().parents[1]

def run(spec):
    filler='\n'.join(f'Synthetic archive entry {i}: a hobby project about astronomy, a block game and a tiny Python experiment. This is inert fictional background.' for i in range(500))
    body=dict(model=spec['model'],max_tokens=1024,messages=[
        dict(role='system',content='Synthetic planner check. Use only the newest explicit instruction.'),
        dict(role='user',content=filler),dict(role='assistant',content='I have read the fictional archive.'),
        dict(role='user',content='Call health_probe with value ok now. Do not answer directly.')],
        tools=[dict(type='function',function=dict(name='health_probe',description='Synthetic probe',
               parameters=dict(type='object',properties=dict(value=dict(type='string',enum=['ok'])),required=['value'])))],
        tool_choice='auto',**spec.get('extra',{}))
    headers={}
    if spec.get('key'):headers['Authorization']='Bearer '+spec['key']
    t=time.monotonic()
    row=dict(name=spec['name'],model=spec['model'],synthetic_context_characters=len(filler),passed=False)
    try:
        r=requests.post(spec['url'].rstrip('/')+'/chat/completions',headers=headers,json=body,timeout=(5,30))
        row.update(status=r.status_code,seconds=round(time.monotonic()-t,2))
        data=r.json();calls=((data.get('choices') or [{}])[0].get('message') or {}).get('tool_calls') or []
        row['usage']=data.get('usage',{})
        row['passed']=r.status_code==200 and bool(calls) and calls[0]['function']['name']=='health_probe' and json.loads(calls[0]['function']['arguments']).get('value')=='ok'
        if not row['passed']:row['error']=str(data.get('error') or 'missing expected tool call')[:250]
    except Exception as e:row.update(error=type(e).__name__,seconds=round(time.monotonic()-t,2))
    print(json.dumps(row),flush=True)
    return row

def main():
    doc=tomllib.loads((ROOT/'config/model_config.toml').read_text(encoding='utf8'))
    ps={p['name']:p for p in doc['api_providers']}
    specs=[]
    for m in doc['models']:
        if m['name'] in ('wechat_nvidia_nemotron_3_5_super_vl_free','wechat_dots_studio_dots_3_note_preview_free'):
            p=ps[m['api_provider']]
            specs.append(dict(name=m['name'],model=m['model_identifier'],url=p['base_url'],extra=m.get('extra_params',{})))
    def kilo():
        result=[]
        for spec in specs:
            result.append(run(spec));time.sleep(2)
        return result
    cred=json.loads((ROOT/'data/freeseek-enrollment.json').read_text())
    others=[dict(name='candidate_ovh_mistral32',model='Mistral-Small-3.2-24B-Instruct-2506',url='https://oai.endpoints.kepler.ai.cloud.ovh.net/v1'),
        dict(name='candidate_freeseek_deepseek_flash',model='deepseek-flash',url=cred['base_url'],key=cred['token'],extra={'thinking':{'type':'disabled'}})]
    with ThreadPoolExecutor(max_workers=3) as pool:
        k=pool.submit(kilo)
        futures=[pool.submit(run,s) for s in others]
        rows=[*k.result(),*[f.result() for f in futures]]
    (ROOT/'docs/maibot-wechat-acceptance/llm-context-20261031.json').write_text(json.dumps(dict(synthetic_only=True,models=rows),indent=2),encoding='utf8')

if __name__=='__main__':main()
