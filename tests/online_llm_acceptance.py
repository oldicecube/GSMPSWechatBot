"""Private live acceptance; never imports main or sends WeChat messages."""
import queue
import argparse
import contextlib
import copy
import hashlib
import io
import json
import logging
import os
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from llm.config import get_llm_config
from llm.provider.deepseek_provider import OpenAICompatibleClient, ResponsesClient, _prompt_capture_scope
from llm.core.llm_service import LLMService
from llm.prompt.prompt_builder import build_system_prompt, build_batch_user_messages
from llm.ai_plugin_manager import AIPluginManager
import llm.ai_plugin_manager as plugin_module
from core.dispatcher import Dispatcher

REPORT = Path(r'C:\Users\Administrator\.codex\backups\wechatbot-20261020\online-acceptance-20261020.json')
logging.getLogger('httpx').setLevel(logging.WARNING)
CFG = get_llm_config()
ENTRY = next(e for e in CFG['apis'] if e.get('name') == 'WechatLLM-Grok0.08-3' and e.get('model') == 'grok-4.7')
CLIENT = ResponsesClient({**ENTRY,'protocol':'responses','responses_explicit_cache_breakpoint':True}) if os.getenv('ACCEPTANCE_PROTOCOL')=='responses' else OpenAICompatibleClient(ENTRY)
REPORT_DATA = json.loads(REPORT.read_text(encoding='utf-8')) if REPORT.exists() else {}
REPORT_DATA.update({'endpoint': ENTRY['base_url'], 'model': ENTRY['model'], 'timezone': 'Asia/Shanghai', 'contains_prompt_text': False, 'contains_credentials': False})

def save():
    REPORT_DATA['updated_at'] = datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    REPORT.write_text(json.dumps(REPORT_DATA, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

def say(label, data):
    print(label + ' ' + json.dumps(data, ensure_ascii=True), flush=True)

def service(manager=None):
    obj = LLMService.__new__(LLMService)
    obj.config = copy.deepcopy(CFG)
    obj.provider = SimpleNamespace(supports_images=CLIENT.supports_images)
    obj.ai_plugin_manager = manager
    obj.style_learner = SimpleNamespace(store=SimpleNamespace())
    obj.long_term_memory = SimpleNamespace(enabled=True)
    obj._history_cache_breakpoints = {}
    obj._history_cache_breakpoints_lock = threading.RLock()
    obj._provider_call_chat = CLIENT.chat
    return obj

def system_prompt():
    return build_system_prompt(CFG.get('prompt'), identity=CFG.get('identity'), prefixes=CFG.get('prefixes'))

def cache_acceptance():
    obj = service(SimpleNamespace())
    paths = list((ROOT / 'data' / 'groups').glob('*/group_messages.json'))
    pairs = [(p, json.loads(p.read_text(encoding='utf-8'))) for p in paths]
    path, raw = max(pairs, key=lambda pair: len(pair[1]))
    group = path.parent.name
    history = obj._annotate_prompt_records(raw, CFG)
    results = []
    all_messages = []
    tools = obj._build_request_tools(current_session_id='online-acceptance-session', memory_group_id=group, behavior_lookup_enabled=True)
    for index, omitted in enumerate((60, 40, 20, 0)):
        records = history[:-omitted] if omitted else history[:]
        fitted = obj._fit_group_messages_for_prompt(records, budget_tokens=CFG['reply_history_token_budget'], cache_stable_prefix=True, cache_group_id=group)
        batch = [dict(m, batch_index=i+1) for i,m in enumerate(records[-3:])]
        data = {'group_messages': fitted, 'batch_messages': batch, 'group_name': group,
                'history_cache_breakpoint_id': obj._history_cache_breakpoint_id(group, fitted, 'proactive-reply', cycle_id='live-' + str(index)),
                'force_reply': False, 'conversation_priority': 'MAY_REPLY', 'trigger_source': 'density'}
        messages = [{'role': 'system', 'content': system_prompt()}] + build_batch_user_messages(data)
        unchanged = 0
        if all_messages:
            for a,b in zip(all_messages[-1], messages):
                if a != b: break
                unchanged += 1
        row = {'step': index, 'new_history_records': 0 if index == 0 else 20,
               'source_history_records': len(records), 'selected_history_records': len(fitted),
               'message_count': len(messages), 'wire_content_chars': sum(len(str(m.get('content') or '')) for m in messages),
               'unchanged_prefix_messages': unchanged, 'tools_count': len(tools)}
        started = time.monotonic()
        try:
            with contextlib.redirect_stdout(io.StringIO()), _prompt_capture_scope('online-cache-acceptance'):
                reply = CLIENT.chat(messages, tools=tools, prompt_cache_key=obj._responses_prompt_cache_key(group, 'online-cache-acceptance'), thinking=CFG.get('thinking_params'))
            row.update(ok=True, usage=dict(CLIENT.last_usage), response_chars=len(reply.content or ''), returned_tool_calls=len(reply.tool_calls or []))
            cached = row['usage'].get('cache_hit')
            total = row['usage'].get('prompt')
            row['measured_cached_input_ratio'] = cached / total if cached is not None and total else None
        except Exception as exc:
            row.update(ok=False, error_type=type(exc).__name__, http_status=getattr(exc, 'status_code', None), error_code=getattr(exc, 'code', None))
        row['duration_s'] = round(time.monotonic()-started, 3)
        results.append(row); all_messages.append(messages)
        REPORT_DATA['cache'] = {'source': 'actual_business_history_and_production_builders', 'results': results}
        save(); say('CACHE_RESULT', row)
        if not row['ok']: break


def plugin_acceptance():
    report = {'source': 'actual_production_prompt_and_tool_loop_with_live_model', 'production_files_touched': False, 'wechat_messages_sent': 0, 'protocol': 'responses' if isinstance(CLIENT,ResponsesClient) else 'openai', 'turns': [], 'tool_actions': []}
    REPORT_DATA['plugin'] = report
    with tempfile.TemporaryDirectory(prefix='wechatbot-online-plugins-') as folder:
        with patch.object(plugin_module, 'AI_PLUGIN_DIR', Path(folder) / 'plugins'), patch.object(plugin_module, 'AI_PLUGIN_MANIFEST', Path(folder) / 'manifest.json'):
            manager = AIPluginManager(CFG)
            obj = service(manager)
            captured = []
            obj._record_responses_tool_tail = lambda group, namespace, messages: captured.__setitem__(slice(None), copy.deepcopy(messages))
            obj._provider_call_chat = lambda messages, **kwargs: call_provider(messages, **kwargs)
            requests = [
                ('create', '\u8bf7\u5e2e\u6211\u5f00\u53d1\u4e00\u4e2a\u6587\u672c\u63d2\u4ef6\uff0c\u540d\u79f0 online_echo\uff0c\u547d\u4ee4 /online_echo\uff0c\u522b\u540d /oecho\u3002\u6709\u53c2\u6570\u65f6\u539f\u6837\u56de\u590d\u201c\u6536\u5230\uff1a\u201d\u52a0\u53c2\u6570\uff0c\u7a7a\u53c2\u6570\u65f6\u56de\u590d\u201c\u6536\u5230\uff1a\u7a7a\u6d88\u606f\u201d\u3002\u4e0d\u7528\u6587\u4ef6\u3001\u7f51\u7edc\u6216\u989d\u5916\u4f9d\u8d56\u3002\u8bf7\u5148\u8bfb\u53d6\u5f00\u53d1\u89c4\u8303\u5e76\u6821\u9a8c\u6e90\u7801\uff0c\u518d\u521b\u5efa\uff0c\u786e\u8ba4\u6210\u529f\u540e\u56de\u590d\u6211\u3002'),
                ('read', '\u8bf7\u5217\u51fa\u73b0\u6709 AI \u63d2\u4ef6\uff0c\u5e76\u8bfb\u53d6 online_echo \u7684\u5b8c\u6574\u6e90\u7801\uff0c\u786e\u8ba4\u547d\u4ee4\u548c\u522b\u540d\u3002'),
                ('update', '\u8bf7\u4fee\u6539 online_echo \u63d2\u4ef6\uff1a\u6709\u53c2\u6570\u65f6\u56de\u590d\u201c\u5df2\u66f4\u65b0\uff1a\u201d\u52a0\u539f\u53c2\u6570\uff0c\u7a7a\u53c2\u6570\u65f6\u56de\u590d\u201c\u5df2\u66f4\u65b0\uff1a\u7a7a\u6d88\u606f\u201d\u3002\u4fdd\u7559\u547d\u4ee4\u548c\u522b\u540d\uff0c\u6821\u9a8c\u540e\u4fdd\u5b58\u3002'),
                ('delete', '\u8bf7\u5220\u9664 online_echo \u63d2\u4ef6\uff0c\u7136\u540e\u518d\u67e5\u4e00\u6b21\u5217\u8868\uff0c\u786e\u8ba4\u5df2\u5220\u9664\u3002'),
            ]
            conversation = []
            usage_rows = []
            def call_provider(messages, **kwargs):
                started = time.monotonic()
                reply = CLIENT.chat(messages, **kwargs)
                usage_rows.append({'usage':dict(CLIENT.last_usage), 'duration_s':round(time.monotonic()-started,3)})
                return reply
            def isolated_tool(name, arguments, **kwargs):
                args = arguments if isinstance(arguments, dict) else json.loads(arguments or '{}')
                if name != 'manage_ai_plugin':
                    return json.dumps({'ok':False,'error_code':'ACCEPTANCE_TOOL_ISOLATED','error':'This acceptance run only executes message-plugin development tools.'})
                result = manager.handle_tool(args)
                report['tool_actions'].append({'action':args.get('action'), 'ok':result.get('ok'), 'error_code':result.get('error_code'), 'errors':(result.get('errors') or (result.get('validation') or {}).get('errors',[]))[:5]})
                save()
                return json.dumps(result, ensure_ascii=False)
            for index,(action,text) in enumerate(requests):
                row = {'action':action}; started=time.monotonic(); before=len(usage_rows)
                current = {'message_id':'acceptance-'+str(index), 'nickname':'acceptance-user', 'wxid':'acceptance-user', 'content':text, 'batch_index':index+1, 'role':'user', 'is_bot':False, 'directed_to_bot':True}
                if not conversation:
                    conversation=[{'role':'system','content':system_prompt()}]+build_batch_user_messages({'group_messages':[], 'batch_messages':[current], 'force_reply':True, 'conversation_priority':'MUST_REPLY', 'trigger_source':'prefix'})
                else:
                    conversation += build_batch_user_messages({'group_messages':[], 'batch_messages':[current], 'force_reply':True, 'conversation_priority':'MUST_REPLY', 'trigger_source':'prefix'})
                try:
                    with contextlib.redirect_stdout(io.StringIO()), patch('llm.core.llm_service.execute_tool', side_effect=isolated_tool), _prompt_capture_scope('online-plugin-acceptance'):
                        final = obj._complete_with_tools(conversation, memory_group_id='online-acceptance', cache_namespace='online-plugin-acceptance', thinking=CFG.get('thinking_params'))
                    conversation = copy.deepcopy(captured) if captured else conversation
                    conversation.append({'role':'assistant','content':final})
                    row['response_valid_json'] = isinstance(json.loads(final),dict)
                    if action in ('create','update'):
                        source=manager._read_source('online_echo'); raw=(plugin_module.AI_PLUGIN_DIR/'online_echo.py').read_bytes()
                        prefix='\u6536\u5230\uff1a' if action=='create' else '\u5df2\u66f4\u65b0\uff1a'
                        sample='\u4e2d\u6587\U0001f600'
                        expected=prefix+sample
                        output=manager.execute('online_echo', sample, {'user':'acceptance-user','group':'online-acceptance'})
                        value=output.get('value'); texts=value.get('messages',[value.get('content','')]) if isinstance(value,dict) else [value]
                        row.update(utf8_exact_bytes=raw==source.encode('utf-8'), no_bom=not raw.startswith(b'\xef\xbb\xbf'), execution_ok=output.get('ok') is True and texts==[expected])
                        dispatcher=Dispatcher(); dispatcher.llm_service=SimpleNamespace(ai_plugin_manager=manager)
                        routed=dispatcher._dispatch_ai_plugin('/oecho',sample,{'user':'acceptance-user','group':'online-acceptance'})
                        row['alias_routes']=bool(routed) and expected in json.dumps(routed,ensure_ascii=False)
                        row['ok']=all(row[k] for k in ('utf8_exact_bytes','no_bom','execution_ok','alias_routes','response_valid_json'))
                    elif action=='read':
                        actions=report['tool_actions']; row['ok']=any(a['action']=='get' and a['ok'] for a in actions) and row['response_valid_json']
                    else:
                        row['files_deleted']=not (plugin_module.AI_PLUGIN_DIR/'online_echo.py').exists()
                        row['registry_empty']=not manager.list_plugins()['plugins']
                        row['ok']=row['files_deleted'] and row['registry_empty'] and row['response_valid_json']
                except Exception as exc:
                    row.update(ok=False,error_type=type(exc).__name__,http_status=getattr(exc,'status_code',None),error_code=getattr(exc,'code',None))
                row['provider_requests']=len(usage_rows)-before;row['duration_s']=round(time.monotonic()-started,3)
                report['turns'].append(row);report['provider_usage']=usage_rows
                save();say('PLUGIN_RESULT',row)
                if not row['ok']:break
            report['all_crud_passed']=len(report['turns'])==4 and all(r['ok'] for r in report['turns'])
            save()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=('cache','plugin','all'));args=parser.parse_args()
    try:
        if args.phase in ('cache','all'):cache_acceptance()
        if args.phase in ('plugin','all'):plugin_acceptance()
    finally:
        CLIENT._client.close()
        save()
