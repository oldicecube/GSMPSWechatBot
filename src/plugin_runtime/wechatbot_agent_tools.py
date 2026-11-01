"""Narrow Host bridge for the native bounded RCON plugin.

Single-use grants bind a Runner invocation to its actual Host message. Neither
LLM arguments nor another plugin can select the requester or read credentials.
"""
from collections import OrderedDict
import asyncio
import secrets
import threading
import time

RCON_PLUGIN_ID = 'wechatbot.restricted-rcon'
_lock = threading.Lock()
_grants = OrderedDict()
_counts = OrderedDict()


def issue_rcon_grant(context):
    anchor = context.metadata.get('anchor_message') if context else None
    info = getattr(anchor,'message_info',None)
    group = getattr(info,'group_info',None)
    user = getattr(info,'user_info',None)
    request = {
        'message_id':str(getattr(anchor,'message_id','') or ''),
        'stream_id':str(getattr(context,'stream_id','') or ''),
        'platform':str(getattr(context,'platform','') or ''),
        'account_id':str(getattr(anchor,'account_id','') or ''),
        'scope':str(getattr(anchor,'scope','') or ''),
        'group_id':str(getattr(group,'group_id','') or ''),
        'group_name':str(getattr(group,'group_name','') or ''),
        'user_id':str(getattr(user,'user_id','') or ''),
    }
    if (getattr(anchor,'is_notify',True) or request['platform'] not in {'wechat','qq'}
        or not all(request[k] for k in ('message_id','stream_id','user_id','group_id'))):
        return ''
    now = time.monotonic()
    token = secrets.token_urlsafe(32)
    with _lock:
        while _grants and (next(iter(_grants.values()))[0] < now - 60 or len(_grants) >= 4096):
            _grants.popitem(last=False)
        _grants[token] = (now,request)
    return token


def _config():
    from llm.config import get_llm_config
    return get_llm_config().get('minecraft_rcon',{})


async def execute_rcon(plugin_id,args):
    if plugin_id != RCON_PLUGIN_ID:
        return {'success':False,'error':'capability is reserved for the restricted RCON plugin'}
    token = args.get('grant')
    if not isinstance(token,str):
        return {'success':False,'error':'missing Host request grant'}
    now = time.monotonic()
    with _lock:
        grant = _grants.pop(token,None)
    if grant is None or grant[0] < now - 60:
        return {'success':False,'error':'missing, expired or consumed Host request grant'}
    request = grant[1]
    cfg = _config()
    from llm.rcon_tools import execute_minecraft_rcon_action, rcon_tools_allowed
    # Preserve the old exact group-name allowlist using the Host-owned name;
    # also support stable IDs. Never use an LLM-supplied group name or wildcard.
    group = request['group_id']
    if request['platform']=='qq':
        from utils.platform_target import target_key
        if not request['account_id']:
            return {'success':False,'error':'QQ RCON requires an authenticated source account'}
        group = target_key('qq',request['account_id'],request['scope'],'group',group)
        request['user_id']='qq:'+request['user_id']
    elif not rcon_tools_allowed(cfg,group_id=group,user_wxid=request['user_id']):
        group = request['group_name']
    if not rcon_tools_allowed(cfg,group_id=group,user_wxid=request['user_id']):
        return {'success':False,'error':'RCON is not enabled for this group or requester'}
    payload = args.get('arguments')
    if not isinstance(payload,dict) or set(payload) - {'action','target','value','kind','number','pitch'}:
        return {'success':False,'error':'unsupported RCON arguments'}
    try:
        limit = max(1,min(2,int(cfg.get('max_calls_per_request',2))))
    except (ValueError,TypeError):
        limit = 2
    key = (request['stream_id'],request['message_id'])
    with _lock:
        while _counts and (next(iter(_counts.values()))[0] < now - 3600 or len(_counts) >= 4096):
            _counts.popitem(last=False)
        created,count = _counts.get(key,(now,0))
        if count >= limit:
            return {'success':False,'error':'RCON per-request call limit reached'}
        _counts[key] = (created,count+1)
    result = await asyncio.to_thread(execute_minecraft_rcon_action,payload,cfg,
                                     group_id=group,user_wxid=request['user_id'])
    # Server output is bounded untrusted data. Credentials never leave Host.
    return {'success':bool(result.get('ok')),**{k:v for k,v in result.items() if k != 'ok'}}
