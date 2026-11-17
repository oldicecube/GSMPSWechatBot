"""Bounded transport adapter for the upstream formatter's actual Danbooru MCP tools."""
import json
import copy
import logging
import threading
import time

from anima.vendor.newbie_formatter import tools as upstream

LOG=logging.getLogger(__name__)
_CACHE={}
_LOCK=threading.Lock()
# Dedicated vendored module, not the bot's global MCP service. Keep original
# handshake/protocol functions, but do not inherit 90s x retries x endpoints.
upstream._TIMEOUT=4


def _one_call(name,arguments):
    result=upstream._rpc("tools/call",{"name":name,"arguments":arguments})
    if isinstance(result.get("structuredContent"),dict):return result["structuredContent"]
    for block in result.get("content",[]):
        if isinstance(block,dict) and block.get("type")=="text":
            value=json.loads(block.get("text",""))
            if isinstance(value,dict):return value
    raise ValueError("Danbooru MCP returned no structured evidence")


# Reuse execute_search_tags/get_related_tags/get_artist_profile while bounding
# only their transport. Original source files remain unmodified on disk.
upstream._call_mcp=_one_call


def call_tag_tool(name,arguments):
    if name not in {"search_tags","get_related_tags","get_artist_profile"}:
        return {"error":"Tool not permitted"}
    key=name+":"+json.dumps(arguments,sort_keys=True,ensure_ascii=False)
    with _LOCK:
        cached=_CACHE.get(key)
        if cached and time.monotonic()-cached[0]<21600:
            return {"tool":name,"arguments":arguments,"result":copy.deepcopy(cached[1]),"elapsed_seconds":0,"cache_hit":True}
    start=time.monotonic()
    try:
        if name=="search_tags":
            text=upstream.execute_search_tags(**arguments,show_nsfw=False,include_wiki=True)
        elif name=="get_related_tags":
            text=upstream.execute_get_related_tags(**arguments,show_nsfw=False,include_wiki=True)
        else:
            text=upstream.execute_get_artist_profile(**arguments,show_nsfw=False)
        result=json.loads(text)
        for _ in range(3):
            if isinstance(result,dict) and isinstance(result.get("result"),str):
                result=json.loads(result["result"])
            else:break
        if not isinstance(result,dict):raise ValueError("Unexpected tool result")
        if "error" not in result:
            with _LOCK:
                if len(_CACHE)>128:_CACHE.pop(next(iter(_CACHE)))
                _CACHE[key]=(time.monotonic(),result)
        return {"tool":name,"arguments":arguments,"elapsed_seconds":round(time.monotonic()-start,3),"result":copy.deepcopy(result)}
    except Exception as exc:
        LOG.info("Danbooru lookup unavailable: %s",type(exc).__name__)
        return {"tool":name,"arguments":arguments,"error":type(exc).__name__,"elapsed_seconds":round(time.monotonic()-start,3)}
