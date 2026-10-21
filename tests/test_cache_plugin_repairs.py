"""Regression coverage for cache stability and multi-step plugin development."""
import queue  # Keep stdlib queue loaded before project imports modify sys.path.
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import llm.ai_plugin_manager as pm
from llm.ai_plugin_manager import AIPluginManager, validate_plugin_source, validate_plugin_result
from llm.ai_plugin_runtime import Interpreter
from llm.cache_diagnostics import CacheDiagnostics
from llm.core.llm_service import LLMService
from llm.web_tools import AI_PLUGIN_TOOL, execute_tool
from llm.prompt.prompt_builder import build_batch_user_messages, build_system_prompt, _group_context_message_items, build_memory_curation_messages
from core.dispatcher import Dispatcher
from llm.learning.learner import _normalize_reply_style_card
from llm.learning.profile_store import ProfileStore

SOURCE='COMMAND = "/greeting_ai"\nALIASES = ["/gai"]\ndef handle(content, context):\n    return {"messages": ["\\u4f60\\u597d\\uff0c" + context.get("user", "friend") + (content or "")], "mode": "wechat_text"}\n'

class PluginRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.patches=[patch.object(pm,'AI_PLUGIN_DIR',root/'plugins'),patch.object(pm,'AI_PLUGIN_MANIFEST',root/'registry.json')]
        for p in self.patches:p.start()
        self.manager=AIPluginManager()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def create(self,source=SOURCE):return self.manager.handle_tool({'action':'create','name':'greeting','source':source,'confirm':True})
    def test_guide_and_validate_new_source_before_file_exists(self):
        guide=self.manager.handle_tool({'action':'guide'})
        self.assertTrue(guide['ok']);self.assertIn('for loops',guide['guide']);self.assertNotIn('???',guide['guide'])
        result=self.manager.handle_tool({'action':'validate','name':'greeting','source':SOURCE})
        self.assertTrue(result['ok'],result);self.assertFalse((pm.AI_PLUGIN_DIR/'greeting.py').exists())
    def test_crud_unicode_exact_bytes_and_execution(self):
        self.assertTrue(self.create()['ok'])
        original=(pm.AI_PLUGIN_DIR/'greeting.py').read_bytes()
        self.assertFalse(original.startswith(b'\xef\xbb\xbf'));self.assertEqual(original,SOURCE.encode('utf-8'))
        self.assertEqual(self.manager.handle_tool({'action':'get','name':'greeting'})['source'],SOURCE)
        self.assertTrue(self.manager.list_plugins()['plugins'][0]['ai_developed'])
        result=self.manager.execute('greeting','\u4e16\u754c',{'user':'\u6d4b\u8bd5\u7528\u6237','raw':{'token':'private'}})
        self.assertTrue(result['ok'],result);self.assertIn('\u4f60\u597d',result['value']['messages'][0])
        updated=SOURCE.replace('friend','guest')
        self.assertTrue(self.manager.handle_tool({'action':'update','name':'greeting','source':updated,'confirm':True})['ok'])
        self.assertTrue(self.manager.handle_tool({'action':'delete','name':'greeting','confirm':True})['ok'])
        self.assertFalse(self.manager.list_plugins()['plugins'])
    def test_invalid_update_preserves_original(self):
        self.create();before=(pm.AI_PLUGIN_DIR/'greeting.py').read_bytes()
        bad=SOURCE+'\nimport os\n'
        r=self.manager.handle_tool({'action':'update','name':'greeting','source':bad,'confirm':True})
        self.assertFalse(r['ok']);self.assertEqual(r['error_code'],'VALIDATION_FAILED')
        self.assertEqual((pm.AI_PLUGIN_DIR/'greeting.py').read_bytes(),before)
    def test_path_traversal_and_real_builtin_conflicts(self):
        self.assertFalse(self.manager.handle_tool({'action':'get','name':'../ping'})['ok'])
        for command in ('/ping','/help'):
            r=self.create(SOURCE.replace('/greeting_ai',command));self.assertFalse(r['ok'])
            self.assertIn('Command conflict',str(r))
    def test_alias_conflict_and_invalid_command(self):
        for source in (SOURCE.replace('/gai','/help'),SOURCE.replace('/greeting_ai','/bad name')):
            self.assertFalse(self.create(source)['ok'])
    def test_loops_supported_with_bound(self):
        source='COMMAND="/count_ai"\ndef handle(content, context):\n    words=[]\n    for word in content.split():\n        words.append(word.upper())\n    return " ".join(words)\n'
        result=validate_plugin_source('counter',source);self.assertTrue(result['ok'],result)
        source=source.replace('content.split()','range(999999999)')
        self.assertFalse(validate_plugin_source('counter',source)['ok'])
    def test_dangerous_capabilities_and_top_level_calls_rejected(self):
        attacks=[
            'import os\n'+SOURCE,
            SOURCE.replace('context.get("user", "friend")','open("config.json").read()'),
            SOURCE.replace('context.get("user", "friend")','str.__class__.__mro__'),
            'COMMAND="/escape_ai"\nx=eval("1+1")\ndef handle(content, context):\n    return "x"\n',
            SOURCE.replace('return {','while True: pass\n    return {'),
        ]
        for source in attacks:self.assertFalse(validate_plugin_source('blocked',source)['ok'],source)
    def test_shared_nested_values_have_aggregate_resource_limit(self):
        source = 'COMMAND="/bounded_ai"\ndef handle(content, context):\n    values=["x" * 8000]\n    for i in range(5):\n        values=[values] * 256\n    return str(values)\n'
        result = validate_plugin_source('bounded', source)
        self.assertFalse(result['ok'])
        self.assertIn('size limit', str(result))
    def test_helper_maps_are_allowed_and_non_text_commands_fail_cleanly(self):
        source = 'COMMAND="/helper_ai"\ndef mapping():\n    return {"greeting":"hello"}\ndef handle(content, context):\n    return mapping().get("greeting")\n'
        self.assertTrue(validate_plugin_source('helper', source)['ok'])
        result = validate_plugin_source('helper', source.replace('"/helper_ai"', '[]'))
        self.assertFalse(result['ok'])
        self.assertIn('COMMAND', str(result))

    def test_no_exec_and_recursion_budget(self):
        source='COMMAND="/recur_ai"\ndef handle(content, context):\n    return handle(content,context)\n'
        self.assertFalse(validate_plugin_source('bounded',source)['ok'])
        self.assertNotIn('exec(',Path(pm.__file__).read_text(encoding='utf-8'))
    def test_return_contract_preflight_and_oversize(self):
        for expr in ('{"file_path":"config.json"}','42','"x" * 100000000','{"content":"x" * 5000}','{"delay_seconds":0}','{"messages":["/danger"]}'):
            s='COMMAND="/return_ai"\ndef handle(content, context):\n    return '+expr+'\n'
            self.assertFalse(validate_plugin_source('returns',s)['ok'],expr)
        self.assertFalse(validate_plugin_result({'messages':['a'],'delay_seconds':float('nan')})['ok'])
    def test_rollback_after_reload_failure(self):
        self.assertTrue(self.create()['ok']);before=self.manager._read_source('greeting')
        self.manager.set_reload_callback(lambda:{'ok':False})
        self.assertFalse(self.manager.handle_tool({'action':'update','name':'greeting','source':SOURCE.replace('friend','guest'),'confirm':True})['ok'])
        self.assertEqual(self.manager._read_source('greeting'),before)
    def test_silent_plugin_is_not_unknown_and_group_allowlist_enforced(self):
        self.assertTrue(self.create(SOURCE.replace('return {"messages": ["\\u4f60\\u597d\\uff0c" + context.get("user", "friend") + (content or "")], "mode": "wechat_text"}','return None'))['ok'])
        d=Dispatcher();d.llm_service=SimpleNamespace(ai_plugin_manager=self.manager)
        self.assertEqual(d._dispatch_ai_plugin('/greeting_ai','',{'group':'test'})['messages'],[])
        d.config={'group_features':{'test':{'plugin_allowlist':['other']}}}
        self.assertEqual(d._dispatch_ai_plugin('/greeting_ai','hi',{'group':'test'})['messages'],[])
    def test_real_tool_loop_repairs_and_completes_all_crud_beyond_three_calls(self):
        service=LLMService.__new__(LLMService)
        service.config={'ai_plugin_max_calls':12,'ai_plugin_tool_timeout_seconds':240}
        service.ai_plugin_manager=self.manager;service.style_learner=None;service.long_term_memory=None;service.provider=None
        service._assert_final_prompt_size=lambda *a,**kw:None
        service._build_request_tools=lambda **kw:[AI_PLUGIN_TOOL]
        now=[1000];results=[]
        operations=[{'action':'guide'},{'action':'validate','name':'greeting','source':SOURCE+'\nimport os'},
                    {'action':'validate','name':'greeting','source':SOURCE},
                    {'action':'create','name':'greeting','source':SOURCE,'confirm':True},
                    {'action':'get','name':'greeting'},
                    {'action':'update','name':'greeting','source':SOURCE.replace('friend','guest'),'confirm':True},
                    {'action':'list'},{'action':'delete','name':'greeting','confirm':True},{'action':'list'}]
        calls=[0]
        def provider(messages,**kwargs):
            if calls[0]:
                result=json.loads(messages[-1]['content']);results.append(result)
                self.assertIs(kwargs.get('tools')[0],AI_PLUGIN_TOOL)
            index=calls[0];calls[0]+=1;now[0]+=80 if index==0 else 12
            if index==len(operations):return SimpleNamespace(content='{"messages":["done"],"animation":null}',tool_calls=[])
            return SimpleNamespace(content=None,tool_calls=[SimpleNamespace(id=str(index),type='function',function=SimpleNamespace(name='manage_ai_plugin',arguments=json.dumps(operations[index])))])
        service._provider_call_chat=provider
        with patch('llm.core.llm_service.time.monotonic',side_effect=lambda:now[0]):
            final=service._complete_with_tools([{'role':'system','content':build_system_prompt() }]+build_batch_user_messages({'group_messages':[], 'batch_messages':[{'content':'Please develop a greeting text plugin and test it','sender_nickname':'tester'}]}),memory_group_id='test',thinking={})
        self.assertIn('done',final)
        self.assertFalse(results[1]['ok']);self.assertTrue(all(r['ok'] for i,r in enumerate(results) if i!=1),results)
        self.assertFalse(self.manager.list_plugins()['plugins'])

class CacheRegressionTests(unittest.TestCase):
    def test_endpoint_reasoning_override_does_not_change_global_or_other_endpoints(self):
        from llm.provider.deepseek_provider import DeepSeekProvider
        global_thinking = {'reasoning_effort':'high'}
        for override, expected in (({'reasoning_effort':'low'}, {'reasoning':{'effort':'low'}}), ({}, {}), (None, {'reasoning':{'effort':'high'}})):
            seen = []
            endpoint = SimpleNamespace(entry={'protocol':'responses','thinking_params':override}, supports_images=True, last_usage={},
                                       chat=lambda messages, **kwargs: seen.append(kwargs) or SimpleNamespace(content='{}'))
            provider = DeepSeekProvider.__new__(DeepSeekProvider)
            provider._endpoints = [endpoint]
            provider._available_indices_in_priority_order = lambda: [0]
            provider._record_success = lambda index: None
            provider.send_chat([{'role':'user','content':'json'}], thinking=global_thinking)
            self.assertEqual(seen[0]['thinking'], expected)
        self.assertEqual(global_thinking, {'reasoning_effort':'high'})
    def test_endpoint_vision_and_thinking_override_survive_normalization(self):
        from llm.config import _normalize_api_entries
        raw = {'apis':[{'name':'first','protocol':'responses','model':'gpt-6.1-sol','api_key':'test-only',
                        'base_url':'https://example.invalid','supports_images':True,'thinking_params':{'reasoning_effort':'low'}},
                       {'name':'second','model':'other','api_key':'test-only','base_url':'https://example.invalid'}]}
        entries = _normalize_api_entries(raw,30)
        self.assertTrue(entries[0]['supports_images'])
        self.assertEqual(entries[0]['thinking_params'], {'reasoning_effort':'low'})
        self.assertIsNone(entries[1]['thinking_params'])

    def test_response_cache_ttl_survives_config_normalization(self):
        from llm.config import _normalize_api_entries
        config = {'apis':[{'name':'test','protocol':'responses','model':'gpt-6.1-sol',
                           'api_key':'test-only','base_url':'https://example.invalid/v1',
                           'prompt_cache_ttl':'30m','responses_explicit_cache_breakpoint':True}]}
        entry = _normalize_api_entries(config, 30)[0]
        self.assertEqual(entry['prompt_cache_ttl'], '30m')
        self.assertTrue(entry['responses_explicit_cache_breakpoint'])
        self.assertEqual(entry['protocol'], 'responses')

    def test_responses_maps_chat_reasoning_and_uses_correct_cache_ttl(self):
        from llm.provider.deepseek_provider import ResponsesClient
        with patch.object(ResponsesClient, '_init_client'):
            client = ResponsesClient({'name':'test','model':'gpt-6.1-sol','timeout_seconds':30})
        captured = []
        response = SimpleNamespace(output=[SimpleNamespace(type='message',content=[SimpleNamespace(type='output_text',text='{}')])])
        client._client = SimpleNamespace(responses=SimpleNamespace(create=lambda **kwargs: captured.append(kwargs) or response))
        client._log_usage = lambda response: None
        client._chat_once([{'role':'user','content':'json please','cache_breakpoint':True}], thinking={'reasoning_effort':'high'})
        self.assertEqual(captured[0]['reasoning'], {'effort':'high'})
        self.assertNotIn('reasoning_effort', captured[0])
        self.assertEqual(captured[0]['extra_body']['prompt_cache_options'], {'mode':'implicit','ttl':'30m'})
        self.assertNotIn('prompt_cache_retention', captured[0])

    def test_history_anchor_survives_new_cycles_and_moves_after_eviction(self):
        import threading
        service = LLMService.__new__(LLMService)
        service._history_cache_breakpoints = {}
        service._history_cache_breakpoints_lock = threading.RLock()
        history = [{"message_id": str(i), "content": "record " + str(i)} for i in range(3)]
        first = service._history_cache_breakpoint_id("test", history, "chat", "cycle-1")
        appended = history + [{"message_id": "next", "content": "new record"}]
        self.assertEqual(first, service._history_cache_breakpoint_id("test", appended, "chat", "cycle-2"))
        after_eviction = service._history_cache_breakpoint_id("test", appended[-1:], "chat", "cycle-3")
        self.assertNotEqual(first, after_eviction)

    def test_wire_prefix_and_changed_tools_are_distinct_and_private(self):
        d=CacheDiagnostics();messages=[{'role':'system','content':'SYSTEM_PRIVATE'},{'role':'user','content':'CHAT_PRIVATE'}]
        with patch('llm.cache_diagnostics.trace_runtime') as trace:
            d.request(messages,[],'key','normal_reply','model')
            a=d.request(messages+[{'role':'user','content':'appended'}],[],'key','normal_reply','model')
            self.assertEqual(a['unchanged_messages'],2)
            self.assertGreater(a['common_prefix_chars'],len('SYSTEM_PRIVATE'))
            b=d.request(messages,[{'name':'changed'}],'key','normal_reply','model')
            self.assertEqual(b['first_change'],'tools_changed');self.assertEqual(b['common_prefix_chars'],0)
            self.assertNotIn('SYSTEM_PRIVATE',str(trace.call_args_list));self.assertNotIn('CHAT_PRIVATE',str(trace.call_args_list))
    def test_runtime_batch_number_changes_do_not_rewrite_history(self):
        a={'message_id':'id1','nickname':'a','wxid':'member','content':'unchanged','batch_index':1}
        self.assertEqual(_group_context_message_items([a]),_group_context_message_items([dict(a,batch_index=99)]))
    def test_memory_and_current_state_can_change_after_history_prefix(self):
        history=[{'message_id':str(i),'nickname':'test','content':'business '+str(i)} for i in range(50)]
        first=build_batch_user_messages({'group_messages':history,'batch_messages':history[-2:],'long_memory':'before'})
        second=build_batch_user_messages({'group_messages':history+[{'message_id':'new','content':'append'}],'batch_messages':history[-1:],'long_memory':'after','current_state':'changed'})
        self.assertEqual(first[:51],second[:51])

class LearningBoundaryTests(unittest.TestCase):
    def test_reply_style_card_is_natural_language_and_drops_concrete_expression_fields(self):
        card = _normalize_reply_style_card({
            'tone': '轻松但不油腻',
            'attitude': '先解决问题，再在有表达需求时加入态度',
            'how_to_speak': '短句跟随节奏',
            'when_to_speak': '直接问题或需要纠正时',
            'when_to_stay_silent': '旁支、重复、无新增贡献时',
            'sentence_patterns': ['这波稳了'],
            'phrase': '具体黑话',
            'style_rules': ['避免机械复读'],
        }, 900)
        self.assertNotIn('sentence_patterns', card)
        self.assertNotIn('phrase', card)
        self.assertLessEqual(len(json.dumps(card, ensure_ascii=False, separators=(',', ':'))), 900)
        self.assertIn('when_to_speak', card)

    def test_curation_learning_units_keep_sources_without_duplicate_transcript_body(self):
        records = [{'message_id': 'human-1', 'nickname': 'human', 'wxid': 'human', 'content': 'unique-human-evidence', 'role': 'user', 'is_bot': False}]
        units = [{'unit_id': 'u1', 'source_ids': ['human-1'], 'speaker_id': 'human', 'content': 'unique-human-evidence', 'complete': True, 'candidate_allowed': True}]
        messages = build_memory_curation_messages({'cycle_messages': records, 'learning_units': units, 'memory_state': {}, 'identity': {}, 'prefixes': []})
        text = '\n'.join(str(item.get('content') or '') for item in messages)
        self.assertEqual(text.count('unique-human-evidence'), 1)
        self.assertIn('human-1', text)

    def test_person_profile_is_not_preloaded_into_cycle_context(self):
        service = LLMService.__new__(LLMService)
        service.style_learner = None
        service.long_term_memory = None
        service._cycle_context_cache = {}
        service._cycle_context_cache_lock = __import__('threading').RLock()
        context = service._get_cycle_context('group', 'direct', 'human', 'query', participants=[{'wxid': 'human', 'nickname': 'Human'}])
        self.assertEqual(context['person_profile_context'], '')

    def test_expression_write_requires_current_human_source_id(self):
        with tempfile.TemporaryDirectory() as folder:
            store = ProfileStore(str(Path(folder) / 'learning.sqlite3'))
            action = {'action': 'add', 'situation': 'casual success', 'pattern': 'short natural response', 'source_ids': ['human-1']}
            self.assertEqual(store.apply_expression_actions('group', [action]), 0)
            self.assertEqual(store.apply_expression_actions('group', [action], valid_source_ids=['human-1']), 1)

if __name__=='__main__':unittest.main()
