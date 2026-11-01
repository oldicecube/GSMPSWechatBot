"""Synthetic-only live embedding probes; never read chat contents or images."""
from __future__ import annotations
import base64
import hashlib
import io
import json
import math
from pathlib import Path
import time
import requests
from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
BASE='https://api.kilo.ai/api/gateway'
MODEL='nvidia/nemotron-3.5-embed-1b:free'
REPORT={'endpoint':BASE,'model':MODEL,'checks':[],'probes':[]}


def check(name,passed,detail=None):
    REPORT['checks'].append(dict(name=name,passed=bool(passed),detail=detail))
    print(name,bool(passed),detail,flush=True)


def cos(a,b):
    return sum(x*y for x,y in zip(a,b))/math.sqrt(sum(x*x for x in a)*sum(y*y for y in b))


def request(label,payload):
    start=time.monotonic()
    response=requests.post(BASE+'/embeddings',json=payload,timeout=30)
    data=response.json()
    rows=sorted(data.get('data',[]),key=lambda r:r.get('index',0))
    vectors=[r.get('embedding',[]) for r in rows]
    row=dict(label=label,status=response.status_code,seconds=round(time.monotonic()-start,3),
             model=payload['model'],dimensions=[len(v) for v in vectors],
             error=str(data.get('error',''))[:250],usage=data.get('usage'))
    REPORT['probes'].append(row)
    check(label+'.http',response.status_code==200,row)
    check(label+'.finite_nonzero',bool(vectors) and all(v and all(math.isfinite(x) for x in v)
         and sum(x*x for x in v)>1e-6 for v in vectors))
    return vectors


def image_uri(color,shape):
    im=Image.new('RGB',(192,192),'white');draw=ImageDraw.Draw(im)
    (draw.rectangle if shape=='square' else draw.ellipse)((25,25,165,165),fill=color)
    out=io.BytesIO();im.save(out,format='PNG')
    return 'data:image/png;base64,'+base64.b64encode(out.getvalue()).decode('ascii')


def main():
    texts=['Minecraft服务器今天正常运行，没有故障。','游戏服务器一切正常，可以进去玩。',
           '我想吃一碗牛肉面，再加一个鸡蛋。','明天要下雨，出门记得带伞。']
    payload={'model':MODEL,'input':texts,'dimensions':1024}
    vectors=request('text_cn',payload)
    if len(vectors)==4:
        related=cos(vectors[0],vectors[1]);unrelated=cos(vectors[0],vectors[2])
        check('text_cn.semantic_order',related>unrelated+.1,dict(related=related,unrelated=unrelated))
        check('text_cn.dimension_1024',all(len(v)==1024 for v in vectors))
    repeated=request('text_repeat',dict(payload,input=[texts[0]]))
    if repeated and vectors:
        check('text_repeat.deterministic',cos(repeated[0],vectors[0])>.9999)
    red=image_uri('red','square');blue=image_uri('blue','circle')
    images=[{'content':[{'type':'image_url','image_url':{'url':uri}}]} for uri in [red,red,blue]]
    iv=request('image_shapes',{'model':MODEL,'input':images})
    if len(iv)==3:
        same=cos(iv[0],iv[1]);different=cos(iv[0],iv[2])
        check('image_shapes.dimension_2048',all(len(v)==2048 for v in iv))
        check('image_shapes.identical_above_different',same>.9999 and same>different+.005,
              dict(identical=same,different=different))
    for model in ['liquid/lfm-2.5-embedding-350m:free','nvidia/llama-nemotron-embed-vl-1b-v2:free']:
        alt=request(model,{'model':model,'input':[texts[0],texts[1],texts[2]]})
        if len(alt)==3:
            check(model+'.semantic_order',cos(alt[0],alt[1])>cos(alt[0],alt[2])+.1)
    catalog=json.loads((ROOT/'temp/embedding-catalog-openrouter.json').read_text(encoding='utf8'))
    entry=next(m for m in catalog['data'] if m['id']==MODEL)
    REPORT['catalog_metadata']={k:entry.get(k) for k in ['id','name','pricing','architecture','context_length']}
    check('catalog.free_text_and_image',float(entry['pricing']['prompt'])==0 and
          {'text','image'}<=set(entry['architecture']['input_modalities']))
    REPORT['passed']=all(c['passed'] for c in REPORT['checks'])
    target=ROOT/'docs/maibot-wechat-acceptance/embedding-live-probe-20261029.json'
    target.write_text(json.dumps(REPORT,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if REPORT['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
