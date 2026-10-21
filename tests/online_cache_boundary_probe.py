"""Compare cache boundaries without changing production config."""
import queue
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import contextlib, copy, io, json, time
from types import SimpleNamespace
from llm.config import get_llm_config
from llm.provider.deepseek_provider import OpenAICompatibleClient, ResponsesClient
from llm.core.llm_service import LLMService
from llm.prompt.prompt_builder import build_system_prompt, build_batch_user_messages
import llm.prompt.prompt_builder as builder

cfg=get_llm_config();entry=next(e for e in cfg['apis'] if e.get('base_url','').rstrip('/')=='https://uuapi.net/v1')
REPORT=Path(r'C:\Users\Administrator\.codex\backups\wechatbot-20261020\cache-boundary-probe.json')
report=json.loads(REPORT.read_text(encoding='utf-8')) if REPORT.exists() else {'production_config_changed':False,'results':[]}
source_path=max((Path('data/groups')).glob('*/group_messages.json'),key=lambda p:p.stat().st_size)
raw=json.loads(source_path.read_text(encoding='utf-8'));group=source_path.parent.name
original=builder._group_context_message_items

def packed(records,checkpoint_id=''):
    items=original(records,checkpoint_id)
    output=[];chunk=[]
    for item in items:
        chunk.append(item)
        if len(chunk)>=8 or item.get('cache_breakpoint'):
            output.append({'role':'user','content':'\n'.join(m['content'] for m in chunk),**({'cache_breakpoint':True} if item.get('cache_breakpoint') else {})});chunk=[]
    if chunk:output.append({'role':'user','content':'\n'.join(m['content'] for m in chunk)})
    return output

for variant in sys.argv[1:] or ['chat-small-append','chat-packed','responses-explicit']:
    client=ResponsesClient({**entry,'protocol':'responses','responses_explicit_cache_breakpoint':True}) if variant.startswith('responses-') else OpenAICompatibleClient(entry)
    obj=LLMService.__new__(LLMService);obj.config=cfg;obj.ai_plugin_manager=SimpleNamespace();obj.style_learner=SimpleNamespace();obj.long_term_memory=SimpleNamespace(enabled=True);obj.provider=SimpleNamespace(supports_images=client.supports_images)
    import threading
    obj._history_cache_breakpoints={};obj._history_cache_breakpoints_lock=threading.RLock()
    records=obj._annotate_prompt_records(raw,cfg)
    tools=obj._build_request_tools(current_session_id='online-acceptance-session',memory_group_id=group,behavior_lookup_enabled=True)
    previous=[]
    for extra in ((0,5,20,20) if variant=='responses-production' else (0,0,5) if variant=='chat-repeat' else (0,5,20)):
        subset=records[:4039+extra]
        fitted=obj._fit_group_messages_for_prompt(subset,budget_tokens=cfg['reply_history_token_budget'],cache_stable_prefix=True,cache_group_id=group)
        data={'group_messages':fitted,'batch_messages':[dict(m,batch_index=i+1) for i,m in enumerate(subset[-3:])],
              'history_cache_breakpoint_id':obj._history_cache_breakpoint_id(group,fitted,'proactive-reply',cycle_id='variant-'+str(extra)),
              'force_reply':False,'conversation_priority':'MAY_REPLY','trigger_source':'density','identity':cfg.get('identity'),'llm_config':cfg,'prefixes':cfg.get('prefixes')}
        builder._group_context_message_items=packed if variant=='chat-packed' else original
        messages=[{'role':'system','content':build_system_prompt(cfg.get('prompt'),identity=cfg.get('identity'),prefixes=cfg.get('prefixes'))}]+build_batch_user_messages(data)
        builder._group_context_message_items=original
        if variant=='responses-production' and extra==20 and previous and previous_extra==20:
            messages=copy.deepcopy(previous)
        same=0
        for a,b in zip(previous,messages):
            if a!=b:break
            same+=1
        row={'variant':variant,'extra_records':extra,'history_records':len(fitted),'messages':len(messages),'unchanged_messages':same}
        started=time.monotonic()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                reply=client.chat(messages,tools=tools,prompt_cache_key=obj._responses_prompt_cache_key(group,'boundary-'+variant),thinking=cfg.get('thinking_params'))
            usage=dict(client.last_usage);hit=usage.get('cache_hit',usage.get('cache_read'))
            row.update(ok=True,usage=usage,measured_cached_input_ratio=hit/usage['prompt'] if hit is not None and usage.get('prompt') else None)
            if variant.startswith('responses-'):row.update(markers_supported=client._explicit_cache_breakpoints_enabled,ttl_supported=client._prompt_cache_options_supported)
        except Exception as exc:
            row.update(ok=False,error_type=type(exc).__name__,http_status=getattr(exc,'status_code',None),error_code=getattr(exc,'code',None))
        row['duration_s']=round(time.monotonic()-started,3)
        report['results'].append(row);REPORT.write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
        print('BOUNDARY_RESULT',json.dumps(row),flush=True)
        previous=copy.deepcopy(messages)
        previous_extra=extra
        if not row['ok']:break
    client._client.close()
