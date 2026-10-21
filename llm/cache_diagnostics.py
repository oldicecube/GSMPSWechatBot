"""Privacy-preserving, request-level cache diagnostics (no prompt text on disk)."""
from collections import OrderedDict
import hashlib
import json
import threading
import time
from utils.runtime_trace import trace_runtime


def packed(value):return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),default=str)
def digest(value):return hashlib.sha256(packed(value).encode('utf-8')).hexdigest()[:16]

class CacheDiagnostics:
    def __init__(self):self.lock=threading.RLock();self.previous=OrderedDict()

    def request(self,messages,tools,route_key,kind,model,settings=None):
        # These serialized strings are retained in process memory only, bounded
        # to 8 routes. The persistent trace contains hashes, counts and lengths.
        records=[packed(m) for m in messages]
        tool_hash=digest(tools or [])
        settings_hash=digest(settings or {})
        key=digest([model,route_key or '',kind])
        now=time.monotonic()
        with self.lock:
            old=self.previous.pop(key,None)
            same=0;prefix=0;changed='first_request';gap=None
            if old:
                gap=round(now-old['at'],2)
                if old['tools']!=tool_hash:changed='tools_changed'
                elif old['settings']!=settings_hash:changed='generation_settings_changed'
                else:
                    changed='appended_or_tail_changed'
                    for a,b in zip(old['records'],records):
                        if a==b:same+=1;prefix+=len(b);continue
                        n=0
                        for ca,cb in zip(a,b):
                            if ca!=cb:break
                            n+=1
                        prefix+=n;changed='message_changed';break
            self.previous[key]={'records':records,'tools':tool_hash,'settings':settings_hash,'at':now}
            while len(self.previous)>8:self.previous.popitem(last=False)
        total=sum(map(len,records))
        info={'request_id':digest([key,time.time_ns()]),'route_hash':key,'kind':kind,
              'model':model,'tools_sha':tool_hash,'settings_sha':settings_hash,'prompt_sha':digest(records),
              'message_count':len(records),'wire_chars':total,'unchanged_messages':same,
              'common_prefix_chars':prefix,'local_prefix_ratio':round(prefix/max(1,total),4),
              'first_change':changed,'first_changed_index':same if old else None,'changed_tail_messages':len(records)-same,'gap_seconds':gap}
        trace_runtime('llm_cache_request',**info)
        print('[LLM CACHE DIAGNOSTIC] '+packed(info),flush=True)
        return info

    def usage(self,request,usage):
        trace_runtime('llm_cache_usage',request_id=request['request_id'],route_hash=request['route_hash'],
                      kind=request['kind'],prompt_tokens=usage.get('prompt'),
                      completion_tokens=usage.get('completion'),cached_tokens=usage.get('cache_hit',usage.get('cache_read')),
                      cache_telemetry_available=('cache_hit' in usage or 'cache_read' in usage))
