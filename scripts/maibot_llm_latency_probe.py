"""Bounded synthetic contracts, grouped by gateway; never send real chat data."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import io
import json
from pathlib import Path
import time
import tomllib

from PIL import Image
import requests

ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'docs/maibot-wechat-acceptance/llm-latency-20261031.json'
TIMEOUT=25


def probe(api):
    checks=[];last=0
    def post(kind,**body):
        nonlocal last
        delay=float(api.get('interval',2))-(time.monotonic()-last)
        if delay>0:time.sleep(delay)
        last=time.monotonic()
        headers={'User-Agent':'GSMPS-Synthetic-Contract/1.0'}
        if api.get('key'):headers['Authorization']='Bearer '+api['key']
        try:
            r=requests.post(api['url'].rstrip('/')+'/chat/completions',headers=headers,
                json={'model':api['model'],'max_tokens':1024,**api.get('extra',{}),**body},timeout=(5,TIMEOUT))
            try:data=r.json()
            except ValueError:data={}
            choice=((data.get('choices') or [{}])[0].get('message') or {})
            row={'kind':kind,'status':r.status_code,'seconds':round(time.monotonic()-last,2),'passed':False}
            if r.status_code!=200:
                error=str(data.get('error') or data.get('detail') or data.get('message') or r.text[:250])
                row['error']=error.replace(api.get('key',''), '[redacted]')[:350] if api.get('key') else error[:350]
            checks.append(row)
            return choice,row
        except requests.RequestException as e:
            row={'kind':kind,'seconds':round(time.monotonic()-last,2),'passed':False,'error':type(e).__name__}
            checks.append(row);return {},row
    def parse(text):return json.loads(str(text).strip().removeprefix('```json').removeprefix('```').removesuffix('```').strip())
    msg,row=post('planner_json',messages=[{'role':'system','content':'Synthetic test. Return JSON only.'},
        {'role':'user','content':'Return {"action":"rest","value":49}.'}])
    try:row['passed']=row.get('status')==200 and parse(msg.get('content'))['value']==49
    except (ValueError,TypeError,KeyError):pass
    if row['passed']:
        prompt=[{'role':'user','content':'Synthetic test. Call health_probe with value ok; do not answer directly.'}]
        msg,row=post('tool_call',messages=prompt,tools=[{'type':'function','function':{'name':'health_probe','description':'Synthetic probe',
            'parameters':{'type':'object','properties':{'value':{'type':'string','enum':['ok']}},'required':['value']}}}],tool_choice='auto')
        calls=msg.get('tool_calls') or []
        try:row['passed']=row.get('status')==200 and calls[0]['function']['name']=='health_probe' and parse(calls[0]['function']['arguments'])['value']=='ok'
        except (ValueError,TypeError,KeyError,IndexError):pass
        if row['passed']:
            answer={k:v for k,v in msg.items() if k in {'content','tool_calls','reasoning_content'}}
            msg,row=post('tool_roundtrip',messages=[*prompt,{'role':'assistant',**answer},
                {'role':'tool','tool_call_id':calls[0]['id'],'content':'{"value":49}'},
                {'role':'user','content':'Return only JSON {"value":49} from the result.'}])
            try:row['passed']=row.get('status')==200 and parse(msg.get('content'))['value']==49
            except (ValueError,TypeError,KeyError):pass
        if api.get('vision',True):
            buf=io.BytesIO();Image.new('RGB',(48,48),(255,0,0)).save(buf,format='PNG')
            msg,row=post('vision',messages=[{'role':'user','content':[{'type':'text','text':'Synthetic vision test: name the dominant color in English.'},
                {'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()}}]}])
            row['passed']=row.get('status')==200 and 'red' in str(msg.get('content','')).lower()
        # Native history uses multiple system items. This compatible wire shape
        # keeps all system references at the beginning while retaining tool order.
        msg,row=post('multi_turn',messages=[{'role':'system','content':'Synthetic test. Return JSON only.\nAdditional context: the number is 49.'},
            {'role':'user','content':'Hello'},{'role':'assistant','content':'{"greeting":"hello"}'},
            {'role':'user','content':'Return {"value":49}.'}])
        try:row['passed']=row.get('status')==200 and parse(msg.get('content'))['value']==49
        except (ValueError,TypeError,KeyError):pass
    result={'name':api['name'],'model':api['model'],'base_url':api['url'],'checks':checks,
        'passed':len(checks)==(5 if api.get('vision',True) else 4) and all(r['passed'] for r in checks)}
    result['max_seconds']=max(r['seconds'] for r in checks)
    print(json.dumps(result,ensure_ascii=False),flush=True)
    return result


def candidates():
    doc=tomllib.loads((ROOT/'config/model_config.toml').read_text(encoding='utf8'))
    ps={p['name']:p for p in doc['api_providers']};result=[]
    for m in doc['models']:
        if m.get('model_type','llm')!='llm':continue
        p=ps[m['api_provider']]
        if p.get('enabled',True) or ('deepseek' in m['name'] and p.get('api_key')):
            result.append({'name':m['name'],'model':m['model_identifier'],'url':p['base_url'],
                'key':p.get('api_key','') if p.get('auth_type','bearer')!='none' else '',
                'extra':m.get('extra_params') or {},'vision':m.get('visual',False),
                'interval':p.get('request_min_interval',2)})
    # Official public free catalog; verify new vision-capable alternatives only.
    catalog=json.loads((ROOT/'temp/llm-catalog-20261031/kilo.json').read_text(encoding='utf8'))['data']
    for mid in ('thinkingmachines/inkling:free','thinkingmachines/inkling-small:free','google/gemma-4-31b-it:free'):
        m=next((m for m in catalog if m['id']==mid),None)
        if m and 'image' in (m.get('architecture') or {}).get('input_modalities',[]):
            result.append({'name':'candidate_'+mid.split('/')[1].replace(':','_'),'model':mid,
                'url':'https://ai-gateway.kilo.ai/api/v1','extra':{'reasoning':{'enabled':False}},'interval':2})
    return result


def main():
    groups={}
    for api in candidates():groups.setdefault(api['url'],[]).append(api)
    def run_group(rows):return [probe(a) for a in rows]
    rows=[]
    with ThreadPoolExecutor(max_workers=3) as pool:
        for group in pool.map(run_group,groups.values()):rows.extend(group)
    report={'at':datetime.now().isoformat(),'synthetic_only':True,'physical_messages':False,'timeout_seconds':TIMEOUT,'models':rows}
    DEST.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print('REPORT',DEST,flush=True)


if __name__=='__main__':main()
