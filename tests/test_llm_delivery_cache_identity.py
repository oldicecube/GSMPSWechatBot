import copy
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from core.dispatcher import Dispatcher
from core.worker import Worker
from llm.identity import annotate_records
from llm.core.llm_service import LLMService
from llm.prompt.prompt_builder import build_batch_user_messages, build_system_prompt, _group_context_line, _target_message_block
from llm.provider.deepseek_provider import OpenAICompatibleClient, ResponsesClient, _responses_developer_role_unsupported, _responses_explicit_breakpoint_default, _to_responses_input
from llm.prompt_capture import capture_prompt, get_last_prompt_messages


class DeliveryTests(unittest.TestCase):
    def make_dispatcher(self, messages=None):
        dispatcher=Dispatcher()
        dispatcher._drain_deferred_proactive = lambda: None
        history=[]
        dispatcher.llm_service=SimpleNamespace(
            _effective_group_config=lambda group:{'auto_reply':{}},
            commit_assistant_delivery=lambda group,result,sent_messages,animation_sent: history.extend(sent_messages) or dict(result, messages=sent_messages),
            on_proactive_result=lambda *a,**kw:None,
        )
        calls=[]
        def generate(context, **kwargs):
            calls.append(list(history))
            return {'messages':['reply'] if messages is None else messages,'_llm_delivery':True,
                    '_delivery_group_id':context['group'],'_delivery_context':context,
                    '_delivery_is_batch':True,'_delivery_result':{'messages':['reply']}}
        dispatcher._dispatch_llm_unlimited=generate
        return dispatcher,calls,history

    def request(self, dispatcher, group='group', **kwargs):
        return dispatcher._dispatch_llm({'group':group,'content':'latest'},batch_messages=[{'content':'latest'}],**kwargs)

    def test_reply_generation_does_not_release_until_actual_delivery_and_history_commit(self):
        d,calls,history=self.make_dispatcher()
        result=self.request(d)
        self.assertIsNone(self.request(d))
        self.assertIsNone(self.request(d,'another-group')) # same background capacity still belongs to physical send
        self.assertEqual(len(calls),1)
        d.confirm_llm_delivery(result,['reply'])
        self.assertEqual(history,['reply'])
        following=self.request(d)
        self.assertEqual(calls[-1],['reply'])
        d.confirm_llm_delivery(following,[])
        self.assertFalse(d._proactive_deliveries)

    def test_sender_delay_partial_exception_and_double_confirmation_release_once(self):
        d,calls,history=self.make_dispatcher()
        result=self.request(d)
        entered=threading.Event(); finish=threading.Event()
        def slow_send(**kwargs):
            entered.set(); self.assertTrue(finish.wait(3)); return True,''
        with patch('core.dispatcher.send',side_effect=slow_send):
            thread=threading.Thread(target=d._deliver_background_result,args=({'group':'group'},result))
            thread.start();self.assertTrue(entered.wait(2));self.assertIsNone(self.request(d))
            finish.set();thread.join(3);self.assertFalse(thread.is_alive())
        d.confirm_llm_delivery(result,['reply'])
        self.assertEqual(history,['reply']) # memory/state commit is idempotent
        next_reply=self.request(d);d.confirm_llm_delivery(next_reply,[])
        self.assertFalse(d._proactive_deliveries)

    def test_sender_failure_silence_and_generation_exception_never_leak_lease(self):
        d,_,_=self.make_dispatcher(messages=[])
        self.request(d);self.assertFalse(d._proactive_deliveries)
        d,_,_=self.make_dispatcher();result=self.request(d)
        with patch('core.dispatcher.send',side_effect=RuntimeError('send failed')):
            d._deliver_background_result({'group':'group'},result)
        self.assertFalse(d._proactive_deliveries)
        d._dispatch_llm_unlimited=lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('provider failed'))
        with self.assertRaises(RuntimeError):self.request(d)
        self.assertFalse(d._proactive_deliveries)

    def test_helper_capacity_does_not_busy_spin_deferred_request_threads(self):
        d,_,_=self.make_dispatcher()
        d._drain_deferred_proactive = Dispatcher._drain_deferred_proactive.__get__(d)
        d._llm_slots.acquire()
        try:
            self.assertIsNone(self.request(d))
            self.assertIn('group',d._pending_proactive_dispatches)
            self.assertFalse(d._proactive_deliveries)
            with patch('core.dispatcher.threading.Thread') as worker:
                d._drain_deferred_proactive()
                worker.assert_not_called()
        finally: d._llm_slots.release()

    def test_deferred_batch_runs_after_send_commit_and_coalesces_duplicate_ids(self):
        d,calls,history=self.make_dispatcher()
        d._drain_deferred_proactive = Dispatcher._drain_deferred_proactive.__get__(d)
        result=self.request(d)
        next_context={'group':'group','content':'new turn'}
        d._dispatch_llm(next_context,batch_messages=[{'message_id':'m2','content':'new turn'}])
        d._dispatch_llm(next_context,batch_messages=[{'message_id':'m2','content':'new turn'},{'message_id':'m3','content':'latest'}])
        self.assertEqual(len(d._pending_proactive_dispatches['group']['options']['batch_messages']),2)
        finished=threading.Event()
        def record_send(**kwargs): finished.set(); return True,''
        with patch('core.dispatcher.send',side_effect=record_send):
            d.confirm_llm_delivery(result,['reply'])
            self.assertTrue(finished.wait(2))
            for _ in range(100):
                if not d._proactive_deliveries: break
                threading.Event().wait(0.01)
        self.assertEqual(calls[-1],['reply'])
        self.assertFalse(d._proactive_deliveries)
        self.assertFalse(d._pending_proactive_dispatches)

    def test_explicit_batch_in_direct_lane_still_blocks_second_proactive_group_request(self):
        d,_,_=self.make_dispatcher()
        first=self.request(d,force_reply=True)
        self.assertIsNone(self.request(d))
        d.confirm_llm_delivery(first,[])
        next_reply=self.request(d);d.confirm_llm_delivery(next_reply,[])

    def test_partial_send_and_failed_commit_release_capacity_without_recording_unsent_text(self):
        d,_,history=self.make_dispatcher(messages=['sent first','unsent second'])
        result=self.request(d)
        with patch('core.dispatcher.send',side_effect=[(True,''),RuntimeError('second failed')]):
            d._deliver_background_result({'group':'group'},result)
        self.assertEqual(history,['sent first'])
        self.assertFalse(d._proactive_deliveries)
        d,calls,_=self.make_dispatcher()
        result=self.request(d)
        d.llm_service.commit_assistant_delivery=lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('history failed'))
        d.confirm_llm_delivery(result,[])
        self.assertFalse(d._proactive_deliveries)
        d.llm_service._effective_group_config=lambda *a:(_ for _ in ()).throw(RuntimeError('config failed'))
        with self.assertRaises(RuntimeError):self.request(d)
        self.assertFalse(d._proactive_deliveries)

    def test_worker_finalizes_delivery_even_on_sender_exception(self):
        d,_,_=self.make_dispatcher();result=self.request(d)
        worker=Worker.__new__(Worker);worker.dispatcher=d
        with patch('core.worker.send',side_effect=RuntimeError('worker send failed')):
            with self.assertRaises(RuntimeError):worker._send_structured_result('group',result)
        self.assertFalse(d._proactive_deliveries)


class IdentityTests(unittest.TestCase):
    config={'bot_wxids':['wxid_self'],'prefixes':['@BotNickname'],'identity':{'name':'GSMPS Bot'},'assistant_nickname':'LLM'}

    def test_self_other_bot_alias_collision_quote_and_string_false_are_distinct(self):
        records=[
            {'wxid':'wxid_self','nickname':'BotNickname','content':'I said this'},
            {'wxid':'wxid_other_bot','nickname':'OtherBot','is_bot':True,'content':'another robot'},
            {'wxid':'wxid_member','nickname':'LLM','is_bot_self':'false','content':'nickname collision'},
            {'wxid':'wxid_member','nickname':'Member','quoted_sender_wxid':'wxid_self','quoted_sender':'BotNickname','quoted_content':'I said this','content':'you said this?'},
            {'role':'assistant','nickname':'LLM','content':'stored delivered self reply'},
            {'nickname':'BotNickname','content':'legacy bot nickname'},
        ]
        result=annotate_records(records,self.config)
        self.assertEqual([row['is_bot_self'] for row in result],[True,False,False,False,True,True])
        self.assertTrue(result[1]['is_bot']);self.assertEqual(result[1]['role'],'user')
        self.assertTrue(result[3]['quoted_is_bot']);self.assertTrue(result[3]['reply_to_bot'])
        line=_group_context_line(result[3])
        self.assertIn('[identity=OTHER]',line);self.assertIn('[identity=BOT_SELF]',line)
        self.assertIn('[addressed_to=bot]',line)
        self.assertEqual(records[1].get('role'),None) # persisted input not rewritten

    def test_native_pat_actor_and_self_recipient_do_not_change_message_authorship(self):
        item=annotate_records([{'wxid':'wxid_member','nickname':'Member','content':'“Member”拍了拍“BotNickname”的肩膀'}],self.config)[0]
        self.assertFalse(item['is_bot_self'])
        self.assertEqual(item['action_actor'],'Member')
        self.assertTrue(item['action_recipient_is_self'])
        self.assertIn('[recipient_identity=BOT_SELF]',_group_context_line(item))
        other=annotate_records([{'wxid':'wxid_member','nickname':'Member','content':'“Member”拍了拍“OtherMember”'}],self.config)[0]
        self.assertFalse(other['action_recipient_is_self'])
        discussion=annotate_records([{'nickname':'Member','content':'听说有人拍了拍BotNickname'}],self.config)[0]
        self.assertNotIn('action_type',discussion)

    def test_weflow_clean_wxid_and_profile_directory_suffix_share_self_identity(self):
        config={**self.config,'bot_wxids':['wxid_account_ab12']}
        rows=annotate_records([{'wxid':'wxid_account','nickname':'bot','content':'own'},
            {'wxid':'wxid_other_ab12','nickname':'LLM','content':'other'}],config)
        self.assertTrue(rows[0]['is_bot_self']);self.assertFalse(rows[1]['is_bot_self'])
        worker=Worker.__new__(Worker);worker.config={'weflow':{'myWxid':'wxid_account_ab12'}}
        self.assertTrue(worker._is_bot_wxid('wxid_account'))
        self.assertFalse(worker._is_bot_wxid('wxid_other_ab12'))

    def test_current_target_cannot_reclassify_verified_member_with_bot_display_name(self):
        for name in ['GSMPS Bot', '@BotNickname', 'LLM']:
            target={'wxid':'wxid_member','nickname':name,'content':'I am another participant','is_bot':True}
            block=_target_message_block({'current_message':target,'identity':self.config['identity'],
                'llm_config':self.config,'bot_identity':{'name':'GSMPS Bot','assistant_nickname':'LLM','bot_wxids':['wxid_self'],'prefixes':['@BotNickname']}})
            self.assertIn('is_bot_self=false',block)
            self.assertIn('role=user',block)

    def test_self_identity_anchor_stays_stable_and_explains_actor_vs_recipient(self):
        data={'group_messages':annotate_records([{'wxid':'wxid_member','nickname':'member','content':'patting BotNickname'}],self.config),
              'batch_messages':[],'llm_config':self.config,'identity':self.config['identity'],'prefixes':self.config['prefixes']}
        messages=build_batch_user_messages(data)
        self.assertIn('BOT_SELF IDENTITY ANCHOR',messages[0]['content'])
        self.assertIn('wxid_self',messages[0]['content'])
        self.assertIn('BotNickname',messages[0]['content'])
        system=build_system_prompt(identity=self.config['identity'],prefixes=self.config['prefixes'])
        self.assertIn('Author, addressee, quoted author and action recipient',system)


class CacheTests(unittest.TestCase):
    def test_memory_and_runtime_changes_do_not_mutate_history_prefix(self):
        config=IdentityTests.config
        history=[{'message_id':str(i),'nickname':'member','wxid':'wxid_member','content':'business history '+str(i)} for i in range(150)]
        base={'group_messages':annotate_records(history,config),'batch_messages':history[-2:],'long_memory':'old memory','short_memory':'old short memory',
              'llm_config':config,'identity':config['identity'],'prefixes':config['prefixes']}
        first=build_batch_user_messages(base)
        second=build_batch_user_messages({**base,'long_memory':'new curated memory','short_memory':'updated short memory'})
        mismatch=next(i for i,(a,b) in enumerate(zip(first,second)) if a!=b)
        self.assertGreaterEqual(mismatch,151) # identity anchor + all 150 history records
        self.assertTrue(second[mismatch]['content'].startswith('长期记忆'))
        third=build_batch_user_messages({**base,'group_messages':annotate_records(history+[{'message_id':'new','nickname':'member','content':'new message'}],config)})
        self.assertEqual(first[:151],third[:151])
        self.assertEqual(sum('old memory' in row['content'] for row in first),1)

    def test_full_proactive_window_keeps_oldest_cache_prefix_and_latest_message_on_append(self):
        service=LLMService.__new__(LLMService);service.config={'cache_prefix_tokens':1800,'context_window_tokens':200000}
        records=[{'message_id':str(i),'nickname':'member','content':('long real chat-style evidence ' * 40)+str(i)} for i in range(100)]
        first=service._fit_group_messages_for_prompt(records,budget_tokens=5000,cache_stable_prefix=True)
        latest={'message_id':'latest','nickname':'member','content':'latest user request'}
        second=service._fit_group_messages_for_prompt(records+[latest],budget_tokens=5000,cache_stable_prefix=True)
        self.assertEqual(first[0]['message_id'],second[0]['message_id'])
        self.assertEqual(first[0],records[0]);self.assertEqual(second[-1],latest)
        self.assertLessEqual(service._estimate_rendered_group_history_tokens(second),5100)
        self.assertTrue(any(row.get('message_id')=='cache-window-gap' for row in second))
        self.assertEqual(len(records),100)

    def test_repeated_message_append_never_collapses_an_earlier_cached_history_item(self):
        service=LLMService.__new__(LLMService);service.config={'context_window_tokens':200000}
        first=[{'message_id':str(i),'nickname':'member','content':'same repeated chat'} for i in range(12)]
        initial=service._fit_group_messages_for_prompt(first,budget_tokens=80000,cache_stable_prefix=True)
        next_items=first+[{'message_id':'next','nickname':'member','content':'same repeated chat'}]
        following=service._fit_group_messages_for_prompt(next_items,budget_tokens=80000,cache_stable_prefix=True)
        self.assertEqual(following[:12],initial)

    def test_full_window_appends_with_identical_cached_tail_until_slack_is_consumed(self):
        service=LLMService.__new__(LLMService);service.config={'cache_prefix_tokens':1800,'context_window_tokens':200000}
        records=[{'message_id':str(i),'nickname':'member','content':('business history ' * 45)+str(i)} for i in range(100)]
        first=service._fit_group_messages_for_prompt(records,budget_tokens=5000,cache_stable_prefix=True,cache_group_id='test-group')
        next_record={'message_id':'next','nickname':'member','content':'new real user turn'}
        second=service._fit_group_messages_for_prompt(records+[next_record],budget_tokens=5000,cache_stable_prefix=True,cache_group_id='test-group')
        self.assertEqual(second[:-1],first)
        self.assertEqual(second[-1],next_record)
        filled=records+[{'message_id':'extra'+str(i),'nickname':'member','content':'more history '*65} for i in range(70)]
        third=service._fit_group_messages_for_prompt(filled,budget_tokens=5000,cache_stable_prefix=True,cache_group_id='test-group')
        self.assertEqual(third[0],records[0]);self.assertEqual(third[-1],filled[-1])
        self.assertLess(service._estimate_rendered_group_history_tokens(third),5000)
        rewritten=[{**item,'content':'canonical history was rewritten'} for item in records]
        changed=service._fit_group_messages_for_prompt(rewritten,budget_tokens=5000,cache_stable_prefix=True,cache_group_id='test-group')
        self.assertNotEqual(changed[0]['content'],first[0]['content'])

    def test_memory_curation_keeps_transcript_anchor_but_compaction_resets_it(self):
        service=LLMService.__new__(LLMService)
        service._history_cache_breakpoints_lock=threading.RLock();service._history_cache_breakpoints={}
        service._responses_prompt_streams_lock=threading.RLock();service._responses_prompt_streams={}
        service._proactive_history_window_lock=threading.RLock();service._proactive_history_windows={('g',80000):{'anchor':1}}
        service._clear_history_cache_breakpoints('g')
        self.assertIn(('g',80000),service._proactive_history_windows)
        service._clear_history_cache_breakpoints('g',clear_history_window=True)
        self.assertFalse(service._proactive_history_windows)

    def test_chat_protocol_routes_grok_cache_and_does_not_send_local_checkpoint_fields(self):
        requests=[]
        client=OpenAICompatibleClient.__new__(OpenAICompatibleClient)
        client.model='grok-4.6';client.name='test';client.entry={'cache':True,'cache_scope':'full'}
        client._client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw: requests.append(kw) or SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))]))))
        client._log_usage=lambda response:None
        original=[{'role':'user','content':'immutable business','cache_breakpoint':True}]
        client._chat_once(original,prompt_cache_key='stable-group-flow')
        self.assertEqual(requests[0]['extra_headers'],{'x-grok-conv-id':'stable-group-flow'})
        self.assertNotIn('cache_breakpoint',requests[0]['messages'][0]);self.assertTrue(original[0]['cache_breakpoint'])
        client.model='gpt-test';client._chat_once(original,prompt_cache_key='stable-group-flow')
        self.assertEqual(requests[1]['prompt_cache_key'],'stable-group-flow')

    def test_responses_gpt56_marks_stable_history_and_system_instructions_are_not_duplicated(self):
        self.assertTrue(_responses_explicit_breakpoint_default('gpt-5.6-terra'))
        self.assertFalse(_responses_explicit_breakpoint_default('gpt-5.5'))
        self.assertFalse(_responses_explicit_breakpoint_default('grok-4.6'))
        with patch.object(ResponsesClient,'_init_client',return_value=None):
            client=ResponsesClient({'name':'test','model':'gpt-5.6-terra','timeout_seconds':30})
            self.assertTrue(client._explicit_cache_breakpoints_enabled)
            self.assertEqual(client._responses_system_mode,'instructions')
            disabled=ResponsesClient({'name':'test','model':'gpt-5.6-terra','timeout_seconds':30,'responses_explicit_cache_breakpoint':False})
            self.assertFalse(disabled._explicit_cache_breakpoints_enabled)
        records=[{'role':'system','content':'trusted self identity'},{'role':'user','content':'stable history','cache_breakpoint':True},{'role':'user','content':'dynamic tail'}]
        items=_to_responses_input(records,include_cache_breakpoints=True,include_system=False)
        self.assertEqual(len(items),2)
        self.assertEqual(items[0]['content'][0]['prompt_cache_breakpoint'],{'mode':'explicit'})
        self.assertNotIn('prompt_cache_breakpoint',items[-1]['content'][0])

    def test_responses_marker_rejection_preserves_top_level_identity_without_duplication(self):
        client=ResponsesClient.__new__(ResponsesClient)
        client.entry={'cache':True};client.name='test';client.model='gpt-5.6-terra';client.cache_scope='full'
        client._explicit_cache_breakpoints_enabled=True;client._responses_system_mode='instructions'
        client.max_output_tokens=128;client._prompt_cache_options_supported=False;client.prompt_cache_ttl='30m'
        calls=[]
        response=SimpleNamespace(output=[SimpleNamespace(type='message',content=[SimpleNamespace(type='output_text',text='{}')])])
        def create(**kwargs):
            calls.append(kwargs)
            if len(calls)==1:raise RuntimeError('unknown prompt_cache_breakpoint unsupported')
            return response
        client._client=SimpleNamespace(responses=SimpleNamespace(create=create));client._log_usage=lambda response:None
        messages=[{'role':'system','content':'trusted self identity'},{'role':'user','content':'history','cache_breakpoint':True},{'role':'user','content':'new turn'}]
        client._chat_once(messages,prompt_cache_key='same-key')
        self.assertFalse(client._explicit_cache_breakpoints_enabled)
        self.assertEqual(calls[-1]['instructions'],'trusted self identity')
        self.assertTrue(all(item.get('role')!='developer' for item in calls[-1]['input']))
        self.assertTrue(all('prompt_cache_breakpoint' not in block for item in calls[-1]['input'] for block in item.get('content',[])))

    def test_cloudflare_502_documentation_url_cannot_change_responses_system_role(self):
        bad_gateway=RuntimeError('Error code: 502 - https://developers.cloudflare.com invalid response unknown origin')
        bad_gateway.status_code=502
        self.assertFalse(_responses_developer_role_unsupported(bad_gateway))
        role=RuntimeError("Invalid input role 'developer' unsupported")
        role.status_code=400
        self.assertTrue(_responses_developer_role_unsupported(role))
        self.assertFalse(_responses_developer_role_unsupported(RuntimeError('developers.cloudflare.com invalid response')))

    def test_prompt_capture_structured_normal_copy_ignores_auxiliary_overwrite(self):
        original=[{'role':'user','content':'normal business'}]
        capture_prompt(original,kind='proactive_reply');original[0]['content']='mutated'
        capture_prompt([{'role':'user','content':'helper'}],kind='auxiliary')
        self.assertEqual(get_last_prompt_messages()[0]['content'],'normal business')
        copy1=get_last_prompt_messages();copy1[0]['content']='another mutation'
        self.assertEqual(get_last_prompt_messages()[0]['content'],'normal business')


if __name__=='__main__':unittest.main()
