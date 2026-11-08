"""Isolated WeChat/MaiBot bridge acceptance. No physical WeChat messages are sent.

python scripts/maibot_bridge_acceptance.py
python scripts/maibot_bridge_acceptance.py --live
"""
from __future__ import annotations
import argparse, asyncio, base64, io, json, os, shutil, sys, threading, time, unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, AsyncMock, Mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
REPORT = {"checks": [], "limitations": []}
SCENARIO = {"id": "", "action": "stop", "calls": 0, "requests": []}
GROUP = "验收群"
USER = "测试用户"
BODY = "中文消息\n保留换行、😀 和 <普通文本>"


def check(name, passed, detail=""):
    REPORT["checks"].append({"name": name, "passed": bool(passed), "detail": str(detail)})
    print(f"ACCEPTANCE {'PASS' if passed else 'FAIL'} {name}: {detail}", flush=True)


class ModelFixture(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path.rstrip('/').endswith('/embeddings'):
            import hashlib
            import numpy as np
            values=data.get('input',[])
            if isinstance(values,str):values=[values]
            dimension=int(data.get('dimensions') or 2048)
            rows=[]
            for index,value in enumerate(values):
                seed=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).digest()
                vector=np.resize(np.frombuffer(seed,dtype=np.uint8).astype(float)-127.5,dimension)
                vector/=np.linalg.norm(vector)
                rows.append(dict(object='embedding',index=index,embedding=vector.tolist()))
            payload=json.dumps(dict(object='list',data=rows,model=data.get('model'),usage=dict(prompt_tokens=1,total_tokens=1))).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(payload)
            return
        SCENARIO['requests'].append(data)
        tools = {t['function']['name'] for t in data.get('tools', [])}
        content, calls = '验收回复 OK', None
        prompt_text = json.dumps(data.get('messages', []), ensure_ascii=False)
        # Compaction intentionally inherits the Planner tool prefix. It asks
        # for state JSON and must not consume this fixture's next Planner action.
        is_compaction = '<additional_conversation_data>' in json.dumps(data.get('messages', [])[-1:])
        if is_compaction:
            content=json.dumps(dict(topics=['isolated acceptance'], background=[], progress=[], pending=[], agreements=[]))
        if SCENARIO.get('autonomous_life') and '你在进行麦麦自己的私人思考' in prompt_text:
            content = json.dumps(dict(action='python', reason='延续群内编程讨论',
                code="def square(x): return x*x\nprint('autonomy_artifact_verified',square(7))\nopen('experiment.txt','w').write('49')" +
                    ("\nprint('progress_stage_two_verified')" if SCENARIO.get('autonomy_stage') == 2 else ''),
                project=dict(title='autonomous native experiment',status='active',note='run square experiment'),
                share=True,person='wxid_acceptance_user',evidence_ids=['autonomy-evidence']))
        elif SCENARIO.get('autonomous_life') and '依据刚才的真实执行结果更新私人兴趣' in prompt_text:
            content = json.dumps(dict(thought='49 verified',interests=['Python experiments'],
                project=dict(title='autonomous native experiment',status='done',note='square verified'),share=True))
        elif SCENARIO.get('autonomous_life') and '依据人设与真实群聊判断是否跟着搬' in prompt_text:
            content = '{"repost":true,"query":"Minecraft funny"}'
        if SCENARIO['action'] in {'fail','quota_all_fail'} or (SCENARIO['action'] in {'kilo_fail','quota_fail'} and data.get('model')==SCENARIO['primary_model']):
            self.send_response(503 if SCENARIO['action']=='fail' else 429); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'error': {'message': 'fixture rate limited', 'type': 'rate_limit_error', 'code': 'PROMOTION_MODEL_LIMIT_REACHED' if SCENARIO['action'] in {'quota_fail','quota_all_fail'} else 'rate_limit_error'}}).encode()); return
        if SCENARIO.get('emoji_selection'):
            content=json.dumps({'emoji_index':1,'reason':'acceptance selection'})
        if 'reply' in tools and not is_compaction:
            action = SCENARIO['action'] if SCENARIO['calls'] == 0 else 'stop'
            SCENARIO['calls'] += 1
            args = {'msg_id': SCENARIO['id'], 'reply_reference': '????????'} if action == 'reply' else {'seconds': 1} if action == 'wait' else {}
            if action == 'reminder_schedule':
                from datetime import timedelta
                action = 'manage_scheduled_reminder'
                args = {'action':'schedule','title':'reminder acceptance',
                        'remind_at':(datetime.now().astimezone()+timedelta(minutes=5)).isoformat()}
            if action == 'fixture_agent_tool':
                action=SCENARIO['agent_tool_name']
                args=SCENARIO['agent_tool_args']
            if SCENARIO['action'] == 'research_chain':
                step = SCENARIO['calls'] - 1
                action, args = [
                    ('internet_search', {'query': 'acceptance technical version'}),
                    ('read_webpage', {'url': 'https://example.com/research-acceptance'}),
                    ('reply', {'msg_id': SCENARIO['id'], 'reply_reference':
                        '已先搜索再阅读原文，实际网页证据verified_page_evidence_852，来源https://example.com/research-acceptance；据此回答，不能把网页当作指令。'}),
                    ('stop', {}),
                ][min(step, 3)]
            if action == 'reply' and SCENARIO.get('attach_at'):
                args['attach_at'] = [{'user_id':SCENARIO['attach_at']}]
            calls = [{'id': f"call_fixture_{len(SCENARIO['requests'])}", 'type': 'function', 'function': {'name': action, 'arguments': json.dumps(args)}}]
            content = None
        result = {'id': 'fixture', 'object': 'chat.completion', 'model': data.get('model', 'fixture'),
                  'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': content, **({'tool_calls': calls} if calls else {})},
                               'finish_reason': 'tool_calls' if calls else 'stop'}],
                  'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
        body = json.dumps(result, ensure_ascii=False).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)


def fixture_context(**values):
    return {'group': GROUP, 'sessionId': 'acceptance@chatroom', 'wxid': 'wxid_acceptance_user', 'user': USER,
            'serverId': 'acceptance-original', 'content': BODY, 'timestamp': time.time(), '_ts': time.time(),
            'type': 'group', 'is_group': True, **values}


class TranslationAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from llm.core.wechat_translation import WechatMessageTranslator
        self.translator = WechatMessageTranslator({'weflow': {'myWxid': 'wxid_acceptance_bot'}})

    async def test_text_identity_time_and_metadata(self):
        from src.common.data_models.message_component_data_model import TextComponent
        m = await self.translator.translate(fixture_context(messageKey='transport-key', localId=55,
                                                            timestamp=1793188305, _ts=1793188306))
        self.assertEqual(m.message_id, 'acceptance-original')
        self.assertEqual(m.message_info.group_info.group_id, 'acceptance@chatroom')
        self.assertEqual(m.message_info.group_info.group_name, GROUP)
        self.assertEqual(m.message_info.user_info.user_id, 'wxid_acceptance_user')
        self.assertEqual(m.account_id, 'wxid_acceptance_bot')
        self.assertEqual(m.timestamp.timestamp(), 1793188305)
        self.assertEqual(m.received_at.timestamp(), 1793188306)
        self.assertEqual(m.raw_message.components[0].text, BODY)
        self.assertEqual(m.message_info.additional_config['wechat']['messageKey'], 'transport-key')
        self.assertEqual(m.message_info.additional_config['wechat']['localId'], 55)

    async def test_private_recipient_is_not_group(self):
        m = await self.translator.translate(fixture_context(type='private', is_group=False, is_private=True, sessionId='wxid_acceptance_user', group=None))
        self.assertIsNone(m.message_info.group_info)
        self.assertEqual(m.message_info.user_info.user_id, 'wxid_acceptance_user')

    async def test_millisecond_timestamp_and_stable_local_id(self):
        m = await self.translator.translate(fixture_context(timestamp=1793188305123, serverId=None, localId=9))
        self.assertAlmostEqual(m.timestamp.timestamp(),1793188305.123, places=3)
        self.assertEqual(m.message_id,'wechat:acceptance@chatroom:local:9')

    async def test_mentions_keep_id_and_target(self):
        from src.common.data_models.message_component_data_model import AtComponent
        m = await self.translator.translate(fixture_context(mentions=[{'wxid':'wxid_other','nickname':'??'},'???'], directed_to_other=True))
        ats = [c for c in m.raw_message.components if isinstance(c,AtComponent)]
        self.assertEqual([c.target_user_id for c in ats], ['wxid_other'])
        self.assertFalse(m.is_at)
        self.assertTrue(m.message_info.additional_config['wechat']['directed_to_other'])

    async def test_at_bot_flag(self):
        m = await self.translator.translate(fixture_context(is_at=True,is_mentioned=True))
        self.assertTrue(m.is_at); self.assertTrue(m.is_mentioned)

    async def test_quote_xml_and_escaped_body(self):
        from src.common.data_models.message_component_data_model import ReplyComponent,TextComponent
        xml='<msg><appmsg><title>?? &amp; ??</title><type>57</type><refermsg><svrid>quoted-100</svrid><chatusr>wxid_other</chatusr><displayname>???</displayname><content>?? &amp; ??</content></refermsg></appmsg></msg>'
        m=await self.translator.translate(fixture_context(content=xml,localType=49))
        self.assertEqual(m.reply_to,'quoted-100')
        ref=next(c for c in m.raw_message.components if isinstance(c,ReplyComponent))
        self.assertEqual(ref.target_message_sender_id,'wxid_other'); self.assertEqual(ref.target_message_content,'?? & ??')
        self.assertEqual(next(c.text for c in m.raw_message.components if isinstance(c,TextComponent)),'?? & ??')

    async def test_structured_quote_is_not_added_to_body(self):
        from src.common.data_models.message_component_data_model import ReplyComponent,TextComponent
        m=await self.translator.translate(fixture_context(quoted_message_id='quoted-1',quoted_content='????',quoted_sender_wxid='wxid_quote'))
        self.assertIsInstance(m.raw_message.components[0],ReplyComponent)
        self.assertEqual(m.raw_message.components[-1].text,BODY)

    async def test_file_xml_preserves_file_metadata(self):
        from src.common.data_models.message_component_data_model import FileComponent
        m=await self.translator.translate(fixture_context(localType=49,content='<msg><appmsg><title>report.pdf</title><type>6</type><appattach><totallen>123</totallen><attachid>file-123</attachid></appattach></appmsg></msg>'))
        f=m.raw_message.components[0]; self.assertIsInstance(f,FileComponent); self.assertEqual((f.name,f.size,f.file_id),('report.pdf','123','file-123'))

    async def test_link_is_readable_and_not_raw_xml(self):
        m=await self.translator.translate(fixture_context(localType=49,content='<msg><appmsg><title>??</title><type>5</type><des>??</des><url>https://example.test/page</url></appmsg></msg>'))
        self.assertEqual(m.raw_message.components[0].text,'??\n??\nhttps://example.test/page')

    async def test_forward_retains_native_nodes(self):
        from src.common.data_models.message_component_data_model import ForwardNodeComponent
        record='&lt;recordinfo&gt;&lt;datalist&gt;&lt;dataitem dataid="node-1"&gt;&lt;sourcename&gt;??&lt;/sourcename&gt;&lt;datadesc&gt;????&lt;/datadesc&gt;&lt;/dataitem&gt;&lt;/datalist&gt;&lt;/recordinfo&gt;'
        m=await self.translator.translate(fixture_context(localType=49,content=f'<msg><appmsg><title>????</title><type>19</type><recorditem>{record}</recorditem></appmsg></msg>'))
        f=next(c for c in m.raw_message.components if isinstance(c,ForwardNodeComponent))
        self.assertEqual(f.forward_components[0].message_id,'node-1'); self.assertEqual(f.forward_components[0].content[0].text,'????')

    async def test_media_types_and_bytes(self):
        from src.common.data_models.message_component_data_model import ImageComponent,EmojiComponent,VoiceComponent,FileComponent
        for kind,typ,cls in [('image',3,ImageComponent),('emoji',47,EmojiComponent),('voice',34,VoiceComponent),('video',43,FileComponent)]:
            with self.subTest(kind=kind):
                m=await self.translator.translate(fixture_context(content='[??]',localType=typ,binary_data=b'media bytes'))
                self.assertIsInstance(m.raw_message.components[0],cls)
                if kind!='video': self.assertEqual(m.raw_message.components[0].binary_data,b'media bytes')
                self.assertEqual(m.is_picture,kind=='image'); self.assertEqual(m.is_emoji,kind=='emoji')

    async def test_missing_media_stays_explicit(self):
        from core.weflow_client import WeFlowClient
        with patch.object(WeFlowClient,'get_original_message',return_value={'ok':False}):
            m=await self.translator.translate(fixture_context(localType=3,content='[??]'))
        self.assertFalse(m.raw_message.components[0].data['data']['available']); self.assertTrue(m.is_picture)

    async def test_missing_sender_rejected(self):
        with self.assertRaises(ValueError): await self.translator.translate(fixture_context(wxid=None))

    async def test_system_notice_is_not_user_chat(self):
        m=await self.translator.translate(fixture_context(localType=10000,source_kind='system_event',learnable=False,prompt_visible=False))
        self.assertTrue(m.is_notify); self.assertFalse(m.message_info.additional_config['wechat']['learnable'])

    async def test_worker_wire_nested_metadata(self):
        m=await self.translator.translate({'user':'????','wxid':'wxid_u','content':'??','group':'????','raw':{'sessionId':'nested@chatroom','timestamp':1793188305,'_ts':1793188306,'_raw':{'serverId':'nested-id','localType':1}}})
        self.assertEqual(m.message_id,'nested-id'); self.assertEqual(m.message_info.group_info.group_id,'nested@chatroom')

    async def test_display_label_uses_original_quote_xml(self):
        from src.common.data_models.message_component_data_model import ReplyComponent,TextComponent
        xml='<msg><appmsg><title>新的回答</title><type>57</type><refermsg><svrid>999999999999999999</svrid><chatusr>wxid_quote</chatusr><content>原文</content></refermsg></appmsg></msg>'
        m=await self.translator.translate(fixture_context(content='显示标题',rawContent=xml,localType=49))
        self.assertEqual(m.reply_to,'999999999999999999')
        self.assertEqual([c.text for c in m.raw_message.components if isinstance(c,TextComponent)],['新的回答'])
        self.assertEqual(m.raw_message.components[0].target_message_content,'原文')

    async def test_rich_push_hydrates_exact_original(self):
        from core.weflow_client import WeFlowClient
        xml='<msg><appmsg><title>原始链接</title><type>5</type><url>https://example.test</url></appmsg></msg>'
        with patch.object(WeFlowClient,'get_original_message',return_value={'ok':True,'message':{'rawContent':xml}}) as fetch:
            m=await self.translator.translate(fixture_context(localType=49,localId=123,content='[消息]'))
        self.assertEqual(fetch.call_args.args,('acceptance@chatroom',123,'acceptance-original'))
        self.assertEqual(m.raw_message.components[0].text,'原始链接\nhttps://example.test')

    async def test_media_hydration_requests_exact_export(self):
        from core.weflow_client import WeFlowClient
        from src.common.data_models.message_component_data_model import VoiceComponent
        with patch.object(WeFlowClient,'get_original_message',return_value={'ok':True,'message':{'mediaUrl':'/api/v1/media/exact.wav'}}) as fetch, patch('core.weflow_media.download_media',return_value=b'RIFF-exact-voice') as download:
            m=await self.translator.translate(fixture_context(localType=34,localId=123,content='[语音]'))
        self.assertTrue(fetch.call_args.kwargs['media']); download.assert_called_once_with('/api/v1/media/exact.wav')
        self.assertIsInstance(m.raw_message.components[0],VoiceComponent)
        self.assertEqual(m.raw_message.components[0].binary_data,b'RIFF-exact-voice')

    async def test_canonical_group_id_overrides_display_alias(self):
        m=await self.translator.translate(fixture_context(group_id='display-name-alias'))
        self.assertEqual(m.message_info.group_info.group_id,'acceptance@chatroom')

    async def test_commands_reconstruct_full_native_text(self):
        m=await self.translator.translate(fixture_context(command='/clear',args='',content=''))
        self.assertTrue(m.is_command); self.assertEqual(m.raw_message.components[0].text,'/clear')

    async def test_wire_text_preserves_unicode_and_whitespace(self):
        raw='  保留开头\n结束 😀  '
        m=await self.translator.translate(fixture_context(content='trimmed',rawContent='wxid_acceptance_user:\n'+raw))
        self.assertEqual(m.raw_message.components[0].text,raw)

    async def test_at_xml_preserves_actual_bot_id(self):
        from src.common.data_models.message_component_data_model import AtComponent
        raw='<msgsource><atuserlist><![CDATA[wxid_other,wxid_acceptance_bot]]></atuserlist></msgsource>'
        m=await self.translator.translate(fixture_context(msgSource=raw))
        self.assertEqual([c.target_user_id for c in m.raw_message.components if isinstance(c,AtComponent)],['wxid_other','wxid_acceptance_bot'])

    async def test_plain_xml_text_is_not_a_quote_or_command(self):
        text='<msg><appmsg><type>57</type><refermsg><svrid>123</svrid></refermsg></appmsg></msg>'
        m=await self.translator.translate(fixture_context(content=text,rawContent=text,localType=1))
        self.assertIsNone(m.reply_to);self.assertEqual(m.raw_message.components[0].text,text)

    async def test_contact_card_and_location_preserve_semantics(self):
        card=await self.translator.translate(fixture_context(localType=42,rawContent='<msg username="wxid_card" nickname="名片用户" />'))
        self.assertEqual(card.message_info.additional_config['wechat']['contact_card']['user_id'],'wxid_card')
        self.assertIn('名片用户',card.raw_message.components[0].text)
        location=await self.translator.translate(fixture_context(localType=48,rawContent='<msg><location x="23.1" y="113.2" label="地址" poiname="地点"/></msg>'))
        self.assertEqual(location.message_info.additional_config['wechat']['location']['x'],'23.1');self.assertIn('地点',location.raw_message.components[0].text)

    async def test_zero_server_id_is_not_a_real_reference(self):
        m=await self.translator.translate(fixture_context(serverId='0',localId=9,quoted_server_id='0',quoted_content='仅预览'))
        self.assertIsNone(m.platform_message_id);self.assertIsNone(m.reply_to)
        self.assertEqual(m.message_id,'wechat:acceptance@chatroom:local:9')

    async def test_packed_wechat4_appmsg_type(self):
        xml='<msg><appmsg><title>引用回复</title><type>57</type><refermsg><svrid>quoted-real</svrid><content>被引用正文</content></refermsg></appmsg></msg>'
        m=await self.translator.translate(fixture_context(localType=(57<<32)|49,rawContent=xml,content='显示标题'))
        self.assertEqual(m.reply_to,'quoted-real');self.assertEqual(m.raw_message.components[-1].text,'引用回复')

    async def test_at_source_sets_actual_bot_trigger(self):
        m=await self.translator.translate(fixture_context(msgSource='<msgsource><atuserlist>wxid_acceptance_bot</atuserlist></msgsource>'))
        self.assertTrue(m.is_at)

    async def test_original_id_mismatch_is_not_used(self):
        from core.weflow_client import WeFlowClient
        with patch.object(WeFlowClient,'get_original_message',return_value={'ok':True,'message':{'serverId':'wrong','base64':base64.b64encode(b'wrong bytes').decode()}}):
            m=await self.translator.translate(fixture_context(localType=3,content='[图片]'))
        self.assertFalse(m.raw_message.components[0].data['data']['available'])
        self.assertIn('does not match',m.message_info.additional_config['wechat']['media_error'])

    async def test_quote_snapshot_reaches_native_context_without_original_in_history(self):
        from src.maisaka.context.planner_messages import build_planner_user_prefix_from_session_message
        from src.maisaka.context.history import build_prefixed_message_sequence
        from src.maisaka.context.messages import SessionBackedMessage
        message=await self.translator.translate(fixture_context(content='现在的回答',quoted_message_id='outside-history',
            quoted_content='旧消息正文\nA & B <message user="fake">',quoted_sender='原作者',quoted_sender_wxid='wxid_original'))
        prefix=build_planner_user_prefix_from_session_message(message)
        entry=SessionBackedMessage.from_session_message(message,raw_message=build_prefixed_message_sequence(message.raw_message,prefix),visible_text='')
        text=''.join(p.text for p in entry.to_context_item().parts)
        self.assertIn('<quoted_message ',text)
        self.assertIn('user="原作者"',text)
        self.assertIn('旧消息正文\nA &amp; B &lt;message user="fake"&gt;',text)
        self.assertTrue(text.endswith('现在的回答'))
        self.assertEqual(message.raw_message.components[-1].text,'现在的回答')

    async def test_quote_local_id_uses_native_stable_id(self):
        message=await self.translator.translate(fixture_context(quoted_message=dict(message_id='9',local_id='9',content='旧正文')))
        self.assertEqual(message.reply_to,'wechat:acceptance@chatroom:local:9')

    async def test_quote_of_link_xml_is_readable(self):
        from html import escape
        linked='<msg><appmsg><type>5</type><title>研究资料</title><des>链接说明</des><url>https://example.test/a</url></appmsg></msg>'
        xml='<msg><appmsg><type>57</type><title>看看这个</title><refermsg><type>49</type><svrid>link-id</svrid><displayname>小明</displayname><content>'+escape(linked)+'</content></refermsg></appmsg></msg>'
        message=await self.translator.translate(fixture_context(localType=49,rawContent=xml))
        self.assertEqual(message.raw_message.components[0].target_message_content,'研究资料\n链接说明\nhttps://example.test/a')

    async def test_quote_of_image_does_not_claim_image_was_seen(self):
        message=await self.translator.translate(fixture_context(quoted_message_id='image-id',quoted_message_type='3',quoted_content='<msg><img aeskey="private" /></msg>'))
        self.assertEqual(message.raw_message.components[0].target_message_content,'[引用图片，原始内容不可用]')

    async def test_record_encodings_nested_hierarchy_and_full_browser(self):
        from html import escape
        from src.common.data_models.message_component_data_model import ForwardNodeComponent,MessageSequence
        from src.maisaka.context.messages import build_full_complex_message_content_from_sequence,ComplexSessionMessage
        inner='<recordinfo><datalist><dataitem dataid="child"><sourcename>子作者</sourcename><datadesc>子正文 &amp; 符号</datadesc></dataitem></datalist></recordinfo>'
        record='<recordinfo><datalist><dataitem dataid="outer" datatype="17"><sourcename>转发者</sourcename><sourcetime>昨天 12:30</sourcetime><recordxml>'+escape(inner)+'</recordxml></dataitem><dataitem dataid="last" datatype="1"><sourcename>尾作者</sourcename><dataitemsource><fromusr>some@chatroom</fromusr><realchatname>wxid_actual</realchatname></dataitemsource><datadesc>尾部正文\n保留换行</datadesc></dataitem></datalist></recordinfo>'
        for encoded in [escape(record),escape(escape(record)),'<![CDATA['+record+']]>',record]:
            xml='<msg><appmsg><type>19</type><title>群聊记录</title><recorditem>'+encoded+'</recorditem></appmsg></msg>'
            message=await self.translator.translate(fixture_context(localType=49,rawContent=xml))
            outer=next(c for c in message.raw_message.components if isinstance(c,ForwardNodeComponent))
            self.assertEqual(len(outer.forward_components),2)
            self.assertEqual(outer.forward_components[-1].user_id,'wxid_actual')
            self.assertEqual(outer.forward_components[-1].message_id,'last')
            self.assertIsInstance(outer.forward_components[0].content[-1],ForwardNodeComponent)
            preview=ComplexSessionMessage.from_session_message(message,planner_prefix='prefix\n',visible_text='').prompt_text
            self.assertIn('转发者',preview)
            full=build_full_complex_message_content_from_sequence(MessageSequence([outer]),[])
            self.assertIn('尾部正文',full)
            self.assertIn('昨天 12:30',full)
            self.assertNotIn('子正文',full)
            child=build_full_complex_message_content_from_sequence(MessageSequence([outer]),[0,0])
            self.assertIn('子作者',child)
            self.assertIn('子正文 & 符号',child)
            from llm.core.wechat_translation import readable_quote_content
            quoted=readable_quote_content(xml,'49')
            self.assertIn('子正文 & 符号',quoted)
            self.assertIn('尾部正文\n保留换行',quoted)
            self.assertNotIn('view_forward_message',quoted)

    async def test_forward_nested_quote_does_not_become_outer_reply(self):
        xml='<msg><appmsg><type>19</type><recorditem><recordinfo><datalist><dataitem><sourcename>A</sourcename><datadesc>记录</datadesc><appmsg><type>57</type><refermsg><svrid>wrong-parent</svrid><content>引用子消息</content></refermsg></appmsg></dataitem></datalist></recordinfo></recorditem></appmsg></msg>'
        message=await self.translator.translate(fixture_context(localType=49,rawContent=xml))
        self.assertIsNone(message.reply_to)

    async def test_forward_unavailable_and_recursion_limit_are_explicit(self):
        from llm.core.wechat_translation import forward_record_component,xml_message
        from src.maisaka.context.messages import build_full_complex_message_content_from_sequence
        from src.common.data_models.message_component_data_model import MessageSequence
        message=await self.translator.translate(fixture_context(localType=49,rawContent='<msg><appmsg><type>19</type><title>标题</title><recorditem>bad XML</recorditem></appmsg></msg>'))
        self.assertIn('聊天记录正文不可用',message.raw_message.components[-1].text)
        record=xml_message('<recordinfo><datalist>'+''.join('<dataitem><datadesc>'+str(n)+'</datadesc></dataitem>' for n in range(3))+'</datalist></recordinfo>')
        component=forward_record_component(record,'id',budget=[2])
        text=build_full_complex_message_content_from_sequence(MessageSequence([component]),[])
        self.assertIn('超过解析数量上限',text)
        self.assertIsNone(forward_record_component(record,'id',depth=8))

    async def test_rich_display_raw_content_is_hydrated(self):
        from core.weflow_client import WeFlowClient
        xml='<appmsg><type>57</type><title>当前正文</title><refermsg><svrid>real</svrid><content>被引用内容</content></refermsg></appmsg>'
        with patch.object(WeFlowClient,'get_original_message',return_value={'ok':True,'message':{'rawContent':xml}}) as fetch:
            message=await self.translator.translate(fixture_context(localType=49,rawContent='[引用消息]',content='显示标签'))
        fetch.assert_called_once()
        self.assertEqual(message.reply_to,'real')
        self.assertEqual(message.raw_message.components[-1].text,'当前正文')


class LearningAndMentionAcceptance(unittest.IsolatedAsyncioTestCase):
    async def test_account_folder_is_not_mention_user_id(self):
        from llm.core.wechat_translation import WechatMessageTranslator
        translator = WechatMessageTranslator({'weflow': {'myWxid': 'wxid_realbot_6e67'}})
        message = await translator.translate(fixture_context(atUsers=['wxid_realbot']))
        self.assertEqual(message.account_id, 'wxid_realbot_6e67')
        self.assertEqual(translator.bot_user_id, 'wxid_realbot')
        self.assertTrue(message.is_at)

    async def test_blacklist_filters_all_learning_sources_without_dropping_chat(self):
        from types import SimpleNamespace
        from src.common.learning_policy import is_learning_allowed
        from src.config.config import global_config
        from src.config.official_configs import LearningExclusionItem
        from src.learners.expression_learner import ExpressionLearner
        from src.learners.jargon_learner import _is_learnable_session_message
        from src.maisaka.context.messages import SessionBackedMessage
        from src.services.memory_flow_service import MemoryAutomationService, ImageMemoryWritebackService, PersonFactWritebackService
        from llm.core.wechat_translation import WechatMessageTranslator
        message = await WechatMessageTranslator({'weflow': {}}).translate(fixture_context())
        rule = LearningExclusionItem(platform='wechat', user_id='wxid_acceptance_user', group_id='acceptance@chatroom')
        with patch.object(global_config.learning, 'excluded_users', [rule]):
            self.assertFalse(is_learning_allowed(message))
            self.assertFalse(_is_learnable_session_message(message))
            context = SessionBackedMessage.from_session_message(message, raw_message=message.raw_message, visible_text=BODY)
            self.assertEqual(ExpressionLearner._extract_session_messages_from_context([context]), [])
            self.assertIsNone(PersonFactWritebackService._person_from_user_message(message))
            self.assertEqual(PersonFactWritebackService._format_evidence_message_line(message, include_sender=True), '')
            automation = MemoryAutomationService()
            with patch.object(automation, 'start', new_callable=AsyncMock) as start:
                await automation.on_incoming_message(message)
                start.assert_not_called()
            image = ImageMemoryWritebackService()
            self.assertTrue(await image._handle_message(message))
            # Flat database records and renamed nicknames use the same stable ID.
            flat = SimpleNamespace(platform='wechat', user_id=rule.user_id, group_id=rule.group_id)
            self.assertFalse(is_learning_allowed(flat))
            flat.group_id = 'another@chatroom'
            self.assertTrue(is_learning_allowed(flat))
            self.assertEqual(message.raw_message.components[0].text, BODY)

    async def test_outgoing_at_uses_current_group_nickname(self):
        from core import weflow_media
        from core.wechat_sender.mention_markup import parse_and_resolve_mentions
        response = Mock()
        response.json.return_value = {'success': True, 'members': [
            {'wxid': 'wxid_target', 'displayName': 'renamed member', 'groupNickname': 'new group card'}]}
        with patch.object(weflow_media, 'TOKEN', 'fixture'), patch('requests.get', return_value=response), patch('core.wechat_sender.mention_markup._native_group_members', return_value={'wxid_target': 'old name'}):
            result = parse_and_resolve_mentions('[[WX_AT:wxid_target]] hi', 'acceptance@chatroom')
            self.assertIn('new group card', str(result))
            self.assertNotIn('old name', str(result))

    async def test_at_attachment_expands_message_alias_without_guessing_user(self):
        from types import SimpleNamespace
        from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
        from llm.core.wechat_translation import WechatMessageTranslator
        message = await WechatMessageTranslator({'weflow': {}}).translate(fixture_context())
        runtime = SimpleNamespace(expand_message_id_aliases=lambda args: {'msg_id': 'original-id' if args['msg_id']=='alias' else args['msg_id']},
                                  find_source_message_by_id=lambda value: message if value=='original-id' else None,
                                  session_id=message.session_id,chat_stream=SimpleNamespace(platform=message.platform))
        ctx = BuiltinToolRuntimeContext(None, runtime)
        self.assertEqual(ctx._resolve_at_attachment('alias').target_user_id, 'wxid_acceptance_user')
        with self.assertRaises(ValueError):
            ctx._resolve_at_attachment('unknown-user-or-message')


class BotInteractionAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from src.common import bot_interaction_policy as policy
        from src.config.config import global_config
        from src.config.official_configs import LearningExclusionItem
        from llm.core.wechat_translation import WechatMessageTranslator
        self.policy = policy
        self.rule = dict(platform='wechat', group_id='acceptance@chatroom', user_id='wxid_external_bot',
                         is_bot=True, plugin_refusal_probability=0.5, label='测试外部 Bot')
        users = patch.object(policy, '_configured_users', return_value=[self.rule])
        users.start(); self.addCleanup(users.stop)
        exclusions = patch.object(global_config.learning, 'excluded_users', [LearningExclusionItem(
            platform='wechat', group_id=self.rule['group_id'], user_id=self.rule['user_id'])])
        exclusions.start(); self.addCleanup(exclusions.stop)
        policy._DECISIONS.clear()
        self.addCleanup(policy._DECISIONS.clear)
        self.payload = fixture_context(wxid=self.rule['user_id'], command='/probe', args='status', content='/probe status')
        self.message = await WechatMessageTranslator({'weflow': {}}).translate(self.payload)
        self.message.processed_plain_text = '/probe status'

    async def test_external_bot_marked_without_self_echo_identity(self):
        from src.common.utils.system_utils import is_bot_self
        from src.common.learning_policy import is_learning_allowed
        self.assertTrue(self.message.message_info.additional_config['bot_interaction']['sender_is_bot'])
        self.assertFalse(is_bot_self('wechat', self.rule['user_id']))
        self.assertFalse(is_learning_allowed(self.message))

    async def test_user_and_group_scope_are_both_required(self):
        self.assertIsNone(self.policy.find_bot_rule('wechat',self.rule['user_id'],'other@chatroom'))
        self.assertIsNone(self.policy.find_bot_rule('wechat','wxid_other',self.rule['group_id']))
        self.assertIsNone(self.policy.find_bot_rule('qq',self.rule['user_id'],self.rule['group_id']))

    async def test_exclusion_alone_does_not_mark_human_as_bot(self):
        self.rule['is_bot'] = False
        self.assertFalse(self.policy.annotate_message(self.message,plugin_request=True))
        self.assertNotIn('bot_interaction',self.message.message_info.additional_config)

    async def test_probability_boundary_and_single_draw_for_duplicate(self):
        with patch.object(self.policy.random,'random',return_value=0.4999) as draw:
            self.assertTrue(self.policy.annotate_message(self.message,plugin_request=True))
            self.assertTrue(self.policy.annotate_message(self.message,plugin_request=True))
            draw.assert_called_once()
        self.message.message_id = 'another-request'
        with patch.object(self.policy.random,'random',return_value=0.5):
            self.assertFalse(self.policy.annotate_message(self.message,plugin_request=True))

    async def test_normal_message_does_not_draw_or_force_reply(self):
        with patch.object(self.policy.random,'random') as draw:
            self.assertFalse(self.policy.annotate_message(self.message))
            draw.assert_not_called()
        self.assertNotIn('force_reply',self.message.message_info.additional_config)

    async def test_refusal_force_reply_and_native_command_bypass(self):
        from src.chat.message_receive.bot import chat_bot
        executor = AsyncMock()
        from types import SimpleNamespace
        info = SimpleNamespace(plugin_name='test.plugin',name='probe')
        with patch.object(self.policy.random,'random',return_value=0.1), patch(
            'src.chat.message_receive.bot.component_query_service.find_command_by_text',return_value=(executor,{},info)):
            self.assertEqual(await chat_bot._process_commands(self.message),(False,None,True))
        executor.assert_not_called()
        self.assertFalse(self.message.is_command)
        self.assertTrue(self.message.message_info.additional_config['force_reply'])

    async def test_shell_refusal_prevents_auto_and_command_execution(self):
        from core.dispatcher import Dispatcher
        from types import SimpleNamespace
        d=Dispatcher();d.debug=False
        command = Mock(return_value='plugin ran');auto = Mock()
        module=SimpleNamespace(__name__='plugins.probe',handle=command,handle_auto=auto)
        d.plugins={'/probe':module};d.modules={'probe':module}
        ingress=Mock(return_value={'native_delivery_owned':True})
        d.llm_service=SimpleNamespace(native_ingress=True,ingest_context=ingress)
        with patch.object(self.policy.random,'random',return_value=0.1):
            self.assertEqual(d.dispatch('/probe','status',self.payload),{'native_delivery_owned':True})
        command.assert_not_called();auto.assert_not_called()
        forwarded=ingress.call_args.args[0]
        self.assertTrue(forwarded['force_reply']);self.assertTrue(forwarded['bot_plugin_request'])
        self.assertNotIn('bot_plugin_request',self.payload)

    async def test_other_half_keeps_shell_plugin_behavior(self):
        from core.dispatcher import Dispatcher
        from types import SimpleNamespace
        d=Dispatcher();d.debug=False
        handler=Mock(return_value='plugin ran')
        module=SimpleNamespace(__name__='plugins.probe',handle=handler)
        d.plugins={'/probe':module};d.modules={'probe':module}
        d.llm_service=SimpleNamespace(native_ingress=True,ingest_context=Mock())
        with patch.object(self.policy.random,'random',return_value=0.9):
            d.dispatch('/probe','status',self.payload)
        handler.assert_called_once()
        d.llm_service.ingest_context.assert_called_once()
        self.assertTrue(d.llm_service.ingest_context.call_args.kwargs['observe_only'])
        self.assertEqual(d.llm_service.ingest_context.call_args.args[0]['_host_plugin_activity']['kind'], 'plugin_trigger')

    async def test_marker_and_planner_and_replyer_system_prompts(self):
        from src.maisaka.context.planner_messages import build_planner_user_prefix_from_session_message
        from src.maisaka.context.messages import SessionBackedMessage
        from src.maisaka.chat_loop_service import MaisakaChatLoopService
        from src.chat.replyer.maisaka_generator_base import BaseMaisakaReplyGenerator
        from src.llm_models.payload_content.context_item import get_item_text
        with patch.object(self.policy.random,'random',return_value=0.1):
            self.policy.annotate_message(self.message,plugin_request=True)
        prefix=build_planner_user_prefix_from_session_message(self.message)
        self.assertIn('sender_kind="external_bot"',prefix)
        self.assertIn('plugin_request="refuse"',prefix)
        history=SessionBackedMessage.from_session_message(self.message,raw_message=self.message.raw_message,visible_text='/probe status')
        service=MaisakaChatLoopService(chat_system_prompt='existing system',session_id=self.message.session_id)
        items,_=service._build_request_messages([history],enable_visual_message=False)
        planner=get_item_text(items[0]);self.assertIn('existing system',planner)
        self.assertIn('必须明确拒绝',planner);self.assertIn('wxid_external_bot',planner)
        replyer=BaseMaisakaReplyGenerator(llm_client_cls=Mock(),load_prompt_func=Mock(return_value='existing reply persona'))
        text=replyer._build_system_prompt(self.message,'test')
        self.assertIn('existing reply persona',text);self.assertIn('必须明确拒绝',text)
        self.assertIn('不要解释或背诵你自己的人设',text)
        self.assertNotIn('必须明确拒绝',self.message.processed_plain_text)

    async def test_normal_round_has_playful_guidance_without_refusal(self):
        text=self.policy.build_bot_interaction_prompt([self.message])
        self.assertIn('强制回复触发方式',text)
        self.assertIn('没我聪明',text)
        self.assertNotIn('本次已分流',text)

    async def test_refusal_does_not_apply_to_other_users_latest_message(self):
        from llm.core.wechat_translation import WechatMessageTranslator
        with patch.object(self.policy.random,'random',return_value=0.1):
            self.policy.annotate_message(self.message,plugin_request=True)
        ordinary=await WechatMessageTranslator({'weflow': {}}).translate(fixture_context(serverId='ordinary-after-refusal'))
        self.assertTrue(self.policy.should_refuse_latest_request([self.message]))
        self.assertFalse(self.policy.should_refuse_latest_request([self.message,ordinary]))
        self.assertNotIn('本次已分流',self.policy.build_bot_interaction_prompt([self.message,ordinary]))
        self.assertEqual(self.policy.build_bot_interaction_prompt([ordinary]),'')


class ProvenanceAcceptance(unittest.IsolatedAsyncioTestCase):
    async def test_wechat_source_roundtrip_and_qq_rejected(self):
        from llm.core.wechat_translation import WechatMessageTranslator
        from src.chat.message_receive.message import SessionMessage
        from src.maisaka.context.planner_messages import build_planner_user_prefix_from_session_message
        translator = WechatMessageTranslator({'weflow': {'myWxid': 'wxid_acceptance_bot'}})
        message = await translator.translate(fixture_context())
        restored = SessionMessage.from_db_instance(message.to_db_instance())
        self.assertEqual(restored.message_info.additional_config['message_origin']['platform'], 'wechat')
        self.assertEqual(restored.account_id, 'wxid_acceptance_bot')
        self.assertIn('platform="wechat"', build_planner_user_prefix_from_session_message(restored))
        with self.assertRaises(ValueError):
            await translator.translate(fixture_context(platform='qq'))
        from core.sender import send
        with patch('core.sender._send_wechat_text') as hook:
            self.assertFalse(send('345678', 'must not leak', source_platform='qq')[0])
        hook.assert_not_called()
        from src.maisaka.context.compaction import message_text_for_compaction
        from src.maisaka.context.messages import SessionBackedMessage
        restored.message_info.additional_config['plugin_activity']={'plugin_id':'shell.probe','kind':'plugin_output'}
        history=SessionBackedMessage.from_session_message(restored,raw_message=restored.raw_message,
            visible_text='plugin result',source_kind='plugin_output')
        compacted=json.loads(message_text_for_compaction(history))
        self.assertEqual(compacted['platform'],'wechat')
        self.assertEqual(compacted['plugin_activity']['plugin_id'],'shell.probe')

    async def test_plugin_context_concurrent_isolation_and_cleanup(self):
        from src.common.message_provenance import plugin_scope, plugin_execution
        async def run(name):
            with plugin_scope(name):
                await asyncio.sleep(.01)
                return plugin_execution.get()['plugin_id']
        self.assertEqual(await asyncio.gather(run('a'), run('b')), ['a', 'b'])
        self.assertIsNone(plugin_execution.get())
        with self.assertRaises(RuntimeError):
            with plugin_scope('failure'):
                raise RuntimeError('test')
        self.assertIsNone(plugin_execution.get())

    async def test_host_identity_and_invocation_cannot_be_spoofed(self):
        from src.common.message_provenance import plugin_execution, track_plugin_invocation
        from src.plugin_runtime.host.capability_service import CapabilityService
        from src.plugin_runtime.host.authorization import AuthorizationManager
        from src.plugin_runtime.protocol.envelope import Envelope, MessageType
        auth = AuthorizationManager(); auth.register_plugin('trusted.plugin', ['send.text'])
        service = CapabilityService(auth)
        captured = []
        async def send(*args):
            captured.append(dict(plugin_execution.get()))
            return True
        service.register_capability('send.text', send)
        request = Envelope(request_id=17, message_type=MessageType.REQUEST, plugin_id='trusted.plugin',
            payload=dict(capability='send.text', args=dict(stream_id='s', plugin_id='forged', trigger_message_id='forged')))
        with track_plugin_invocation('trusted.plugin', 's', trigger_message_id='real', invocation_id='tool-real'):
            response = await service.handle_capability_request(request)
        self.assertIsNone(response.error)
        self.assertEqual(captured[0]['plugin_id'], 'trusted.plugin')
        self.assertEqual(captured[0]['trigger_message_id'], 'real')
        self.assertEqual(captured[0]['invocation_id'], 'tool-real')
        self.assertIsNone(plugin_execution.get())
        request.plugin_id = 'unregistered'
        await service.handle_capability_request(request)
        self.assertEqual(len(captured), 1)

    async def test_bv_background_job_carries_execution_context(self):
        from src.common.message_provenance import plugin_scope, plugin_execution
        import plugins.bv as bv
        pending = __import__('queue').Queue(maxsize=5)
        context = fixture_context(content='BV1234567890')
        with patch.object(bv,'_QUEUE',pending), patch.object(bv,'_INFLIGHT',set()), patch.object(bv,'_CONFIG',{}):
            with plugin_scope('shell.bv',trigger_message_id='bv-trigger'):
                bv.handle('BV1234567890',context)
            job=pending.get_nowait()
        captured=[]
        def run(): captured.append(dict(plugin_execution.get()))
        thread=threading.Thread(target=lambda: job[3].run(run));thread.start();thread.join()
        self.assertEqual(captured[0]['plugin_id'],'shell.bv')
        self.assertEqual(captured[0]['trigger_message_id'],'bv-trigger')
        self.assertIsNone(plugin_execution.get())


class OutboundAcceptance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from llm.core.wechat_driver import WechatShellDriver
        from src.common.data_models.message_component_data_model import TextComponent
        self.driver=WechatShellDriver('wxid_acceptance_bot')
        from llm.core.wechat_translation import WechatMessageTranslator
        self.m=await WechatMessageTranslator({'weflow':{'myWxid':'wxid_acceptance_bot'}}).translate(fixture_context())
        self.m.session_id='outbound-session'; self.m.processed_plain_text='outbound'; self.m.message_info.user_info.user_id='wxid_acceptance_bot'
        from src.platform_io.types import RouteKey
        self.route=RouteKey('wechat','wxid_acceptance_bot')

    async def test_exact_text_mentions_and_quote_dropped(self):
        from src.common.data_models.message_component_data_model import TextComponent,AtComponent,ReplyComponent
        self.m.raw_message.components=[ReplyComponent('q','quoted body','wxid_other','Alice'),TextComponent('\u4f60\u597d\n\U0001f600'),AtComponent('wxid_other')]
        with patch('core.sender.send',return_value=(True,'')) as sender:
            receipt=await self.driver.send_message(self.m,self.route)
        self.assertEqual(receipt.status.value,'sent'); self.assertEqual(receipt.metadata['quote_mode'],'dropped_unsupported')
        self.assertEqual(sender.call_count,1); self.assertEqual(sender.call_args.kwargs['content'],'\u4f60\u597d\n\U0001f600[[WX_AT:wxid_other]]')
        self.assertEqual(sender.call_args.kwargs['target'],GROUP); self.assertEqual(receipt.metadata['recipient_id'],'acceptance@chatroom')
        self.assertIsNone(receipt.external_message_id)

    async def test_reported_quote_at_labels_are_not_sent_or_resolved(self):
        from src.common.data_models.message_component_data_model import TextComponent,ReplyComponent
        reference = '@LLM @服务器状态@我\u2005人设测试：你现在的人设是什么'
        self.m.raw_message.components=[ReplyComponent('original',reference,'wxid_ice','陈年老冰块'),TextComponent('我是 gsmps 服务器的微信群聊机器人。')]
        with patch('core.sender.send',return_value=(True,'')) as sender:
            receipt=await self.driver.send_message(self.m,self.route)
        self.assertEqual(receipt.status.value,'sent')
        self.assertEqual(sender.call_args.kwargs['content'],'我是 gsmps 服务器的微信群聊机器人。')
        self.assertNotIn('native_segments',sender.call_args.kwargs)
        self.assertNotIn('resolve_mentions',sender.call_args.kwargs)
        self.assertEqual(receipt.metadata['dropped_quote_count'],1)

    async def test_quote_only_dropped_without_send_or_false_success(self):
        from src.common.data_models.message_component_data_model import ReplyComponent
        self.m.raw_message.components=[ReplyComponent('original','@wxid_other quoted only')]
        with patch('core.sender.send',return_value=(True,'')) as sender:
            receipt=await self.driver.send_message(self.m,self.route)
        sender.assert_not_called()
        self.assertEqual(receipt.status.value,'dropped')
        self.assertEqual(receipt.metadata['operations'],[])

    async def test_quote_drop_preserves_real_at_segments(self):
        from src.common.data_models.message_component_data_model import TextComponent,AtComponent,ReplyComponent
        self.m.raw_message.components=[AtComponent('wxid_other'),ReplyComponent('original','@unresolved quotation'),TextComponent('正文\n保留换行')]
        with patch('core.sender.send',return_value=(True,'')) as sender:
            receipt=await self.driver.send_message(self.m,self.route)
        self.assertEqual(receipt.status.value,'sent')
        self.assertEqual(sender.call_args.kwargs['content'],'[[WX_AT:wxid_other]]正文\n保留换行')
        self.assertEqual(sender.call_args.kwargs['native_segments'],[{'type':'mention','wxid':'wxid_other'},{'type':'text','text':'正文\n保留换行'}])

    async def test_quote_drop_does_not_send_reference_alongside_media(self):
        from src.common.data_models.message_component_data_model import FileComponent,ReplyComponent
        self.m.raw_message.components=[ReplyComponent('q','quoted text'),FileComponent(name='a.txt',base64_data=base64.b64encode(b'file').decode()),ReplyComponent('q2','quoted after file')]
        with patch('core.sender.send',return_value=(True,'')) as sender:
            receipt=await self.driver.send_message(self.m,self.route)
        self.assertEqual(receipt.status.value,'sent')
        self.assertEqual(sender.call_count,1)
        self.assertEqual(sender.call_args.kwargs['mode'],'wechat_file')
        self.assertEqual(receipt.metadata['dropped_quote_count'],2)

    async def test_media_and_text_order(self):
        from PIL import Image
        from src.common.data_models.message_component_data_model import TextComponent,ImageComponent,EmojiComponent,VoiceComponent,FileComponent
        stream=io.BytesIO(); Image.new('RGB',(2,2),'red').save(stream,format='PNG'); data=stream.getvalue()
        self.m.raw_message.components=[TextComponent('?'),ImageComponent(binary_hash='',binary_data=data),EmojiComponent(binary_hash='',binary_data=data),VoiceComponent(binary_hash='',binary_data=b'RIFFwave'),FileComponent(name='a.txt',base64_data=base64.b64encode(b'file').decode()),TextComponent('?')]
        with patch('core.sender.send',return_value=(True,'')) as sender:
            receipt=await self.driver.send_message(self.m,self.route)
        self.assertEqual(receipt.status.value,'sent')
        self.assertEqual([c.kwargs['mode'] for c in sender.call_args_list],['wechat_text','wechat_file','wechat_file','wechat_voice','wechat_file','wechat_text'])

    async def test_failure_does_not_claim_sent(self):
        with patch('core.sender.send',return_value=(False,'fixture UI failure')):
            receipt=await self.driver.send_message(self.m,self.route)
        self.assertEqual(receipt.status.value,'failed'); self.assertIn('fixture UI failure',receipt.error)

    async def test_unsupported_component_fails_before_any_send(self):
        from src.common.data_models.message_component_data_model import DictComponent,TextComponent
        self.m.raw_message.components=[TextComponent('??????'),DictComponent({'type':'unsupported'})]
        with patch('core.sender.send',return_value=(True,'')) as sender:
            receipt=await self.driver.send_message(self.m,self.route)
        self.assertEqual(receipt.status.value,'failed'); sender.assert_not_called()

    async def test_private_uses_recipient_not_bot(self):
        from src.chat.message_receive.chat_manager import chat_manager,BotChatSession
        self.m.message_info.group_info=None
        self.m.message_info.additional_config['platform_io_target_user_id']='wxid_recipient'
        chat_manager.sessions[self.m.session_id]=BotChatSession(self.m.session_id,'wechat',user_id='wxid_recipient',user_nickname='?????',account_id='wxid_acceptance_bot')
        with patch('core.sender.send',return_value=(True,'')) as sender:
            receipt=await self.driver.send_message(self.m,self.route)
        self.assertEqual(sender.call_args.kwargs['target'],'?????'); self.assertEqual(receipt.metadata['recipient_id'],'wxid_recipient')

    async def test_at_all_explicitly_unsupported(self):
        from src.common.data_models.message_component_data_model import AtComponent
        self.m.raw_message.components=[AtComponent('notify@all')]
        with patch('core.sender.send') as sender:
            receipt=await self.driver.send_message(self.m,self.route)
        self.assertEqual(receipt.status.value,'failed'); sender.assert_not_called()


def native_acceptance(service, live=False):
    from src.common.database.database import get_db_session
    from src.common.database.database_model import Messages
    from sqlmodel import select
    from src.config.config import global_config,model_config
    from src.services.memory_flow_service import memory_automation_service
    from src.A_memorix.host_service import a_memorix_host_service
    from src.plugin_runtime.integration import get_plugin_runtime_manager
    from src.maisaka.builtin_tool import get_all_builtin_tool_specs
    from src.core.tooling import ToolAvailabilityContext
    rt=service._native_runtime
    check('native.thread_and_platform_driver',rt._thread.is_alive() and rt._driver.driver_id=='wechatbot-shell')
    check('native.memory_automation_started',memory_automation_service._started)
    check('native.a_memorix_host_started',a_memorix_host_service._runtime_state not in {'stopped','failed'},a_memorix_host_service._runtime_state)
    manager=get_plugin_runtime_manager()
    check('native.plugin_runtime_started',manager._started)
    if (Path(os.environ['MAIBOT_INSTANCE_DIR']) / 'plugins/owned_plugin_tools/_manifest.json').exists():
        check('owned_tools.native_runner_loaded', 'wechatbot.owned-plugin-tools' in set(manager._third_party_supervisor.get_loaded_plugin_ids()))
    if REPORT.get('plugin_test_mode'):
        expected={'xxxxx7258.idle-proactive-chat','zhaocang.hot-topics'}
        if (Path(os.environ['MAIBOT_INSTANCE_DIR'])/'plugins/cateye_mai_personality_swap/_manifest.json').exists():
            expected.add('github.cateye.mai-personality-swap')
        if REPORT.get('life_enabled_in_isolated_test_only'):expected.add('mailife.mai-life')
        if REPORT.get('autonomous_life_test_mode'):expected.add('wechatbot.autonomous-life')
        loaded=set(manager._third_party_supervisor.get_loaded_plugin_ids())
        REPORT['third_party_plugin_registrations']={pid:{'config_error':registration.config_error,'components':[c.model_dump(mode='json') for c in registration.components]} for pid,registration in manager._third_party_supervisor._registered_plugins.items()}
        check('plugins.native_runner_loaded_expected',expected<=loaded,sorted(loaded))
        check('plugins.no_config_errors',all(not row['config_error'] for row in REPORT['third_party_plugin_registrations'].values()))
        async def config_wire_check():
            from src.plugin_runtime.capabilities.core import _config_rpc_value
            from src.plugin_runtime.protocol.codec import MsgPackCodec
            value={'rules':_config_rpc_value(global_config.learning.excluded_users)}
            return MsgPackCodec().decode(MsgPackCodec().encode(value))==value
        check('plugins.learning_policy_serializable_over_ipc',rt.submit(config_wire_check()))
    from src.llm_models.utils_model import LLMOrchestrator
    effective_planner=LLMOrchestrator('planner').model_for_task.model_list
    check('config.first_enabled_priority',effective_planner[0]==SCENARIO['primary_native_model'])
    visual=[m.name for m in model_config.models if m.visual]
    check('config.vision_only_visual_models',all(n in visual for n in model_config.model_task_config.vlm.model_list))
    if not model_config.model_task_config.embedding.model_list:
        REPORT['limitations'].append('Embedding endpoint is not configured; A_Memorix is sparse/metadata-only, not vector retrieval.')
    if live:
        started=time.monotonic()
        try:
            response=service._provider_call([{'role':'user','content':'Reply with exactly OK.'}],task_name='utils')
            check('live.kilo_auxiliary',bool(response.content),f'{time.monotonic()-started:.2f}s; chars={len(response.content)}')
        except Exception as exc:
            check('live.kilo_auxiliary',False,f'{type(exc).__name__}: {exc}')
    else:
        # Execute the real planner->tool->replyer->native send->shell HTTP payload chain.
        SCENARIO.update(id='e2e-direct',action='reply',calls=0)
        # Exercise an actual configured profile on the isolated acceptance group.
        from copy import deepcopy
        profile = next((p for p in (service.config.get('group_profiles') or {}).values()
                        if p.get('identity') and p.get('reply_style')), None)
        if profile:
            rt._translator.config['llm'].setdefault('group_profiles', {})[GROUP] = deepcopy(profile)
    context=fixture_context(serverId='e2e-direct',content='请回复“验收成功”，这是收发链路验收测试。',prefix_used=True)
    result=service.handle_message(context['group'],context['user'],context['content'],wxid=context['wxid'],session_id=context['sessionId'],message_context=context,force_reply=True)
    check(('live.' if live else '')+'native.direct_reply_delivery',result.get('native_sent') and result.get('_llm_ok'),json.dumps(result,ensure_ascii=True)[:900])
    check('native.no_shell_duplicate_messages',result.get('messages')==[] and result.get('native_delivery_owned'))
    if (Path(os.environ['MAIBOT_INSTANCE_DIR']) / 'plugins/owned_plugin_tools/_manifest.json').exists():
        from src.chat.message_receive.message import SessionMessage
        from src.core.tooling import ToolExecutionContext, ToolInvocation
        from src.plugin_runtime.component_query import component_query_service
        from core.dispatcher import Dispatcher
        import plugins.ping as ping
        proxy_dispatcher = Dispatcher(); proxy_dispatcher.inited = True; proxy_dispatcher.modules = {'ping': ping}
        proxy_dispatcher.llm_service = service
        with get_db_session() as db:
            anchor = SessionMessage.from_db_instance(db.exec(select(Messages).where(Messages.message_id == 'e2e-direct')).one())
        tool_context = ToolExecutionContext(session_id=anchor.session_id, stream_id=anchor.session_id,
            platform=anchor.platform, group_id=anchor.message_info.group_info.group_id,
            user_id=anchor.message_info.user_info.user_id, is_group_chat=True, metadata={'anchor_message': anchor})
        listed = rt.submit(component_query_service.invoke_tool_as_tool(ToolInvocation('owned_plugins', arguments={'operation': 'list', 'query': 'ping'}), tool_context))
        # The autonomous native-plugin entry intentionally excludes GSMPS
        # shell plugins. Shell commands still execute through their router.
        check('owned_tools.runner_catalog_and_scope', listed.success and 'gsmps.command.ping' not in listed.content, listed.content[:500])
        invoked = rt.submit(component_query_service.invoke_tool_as_tool(ToolInvocation('owned_plugins', arguments={'operation': 'invoke', 'target': 'gsmps.command.ping', 'arguments_json': '{}'}), tool_context))
        check('owned_tools.runner_rejects_shell_autonomous_invocation',
              'excluded' in invoked.content and '"success": false' in invoked.content,
              invoked.content[:500])
        check('owned_tools.tool_result_provenance', invoked.metadata.get('message_origin', {}).get('kind') == 'plugin_tool_result')
    if not live:
        rules = (service.config.get('identity') or {}).get('rules') or []
        from src.config.config import global_config
        native_persona = global_config.personality.personality
        def wire_text(value):
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                return '\n'.join(wire_text(v) for v in value.values())
            if isinstance(value, list):
                return '\n'.join(wire_text(v) for v in value)
            return ''
        requests = SCENARIO['requests']
        for kind, predicate in (
            ('planner', lambda r: any(t.get('function', {}).get('name') == 'reply' for t in r.get('tools', []))),
            ('replyer', lambda r: not r.get('tools')),
        ):
            bodies = [wire_text(r.get('messages', [])) for r in requests if predicate(r)]
            check(f'config.identity_rules_in_{kind}_wire_prompt',
                  bool(native_persona) and any(native_persona in body and all(rule in body for rule in rules) for body in bodies),
                  f'Full native persona and {len(rules)} legacy rules; {len(bodies)} captured requests')
            if profile:
                check(f'config.group_profile_in_{kind}_wire_prompt',
                      any('[WechatBot chat profile]' in body and profile['reply_style'] in body for body in bodies))
        if service.config.get('style_policy') == 'native':
            from llm.core.native_prompt_policy import native_styles
            native = native_styles()
            reply_bodies = [json.dumps(r.get('messages', []), ensure_ascii=False) for r in requests if not r.get('tools')]
            # Actual model requests, not merely the config file.
            check('config.native_reply_style_actual_wire', any(native['reply_style'] in body for body in reply_bodies))
            check('config.no_duplicate_profile_style_actual_wire', all('[WechatBot chat profile]' not in body for body in reply_bodies))
    if live:
        async def vision_probe():
            from PIL import Image
            from src.services.llm_service import LLMServiceClient
            stream=io.BytesIO();Image.new('RGB',(64,64),'red').save(stream,format='PNG')
            return await LLMServiceClient('vlm','wechat.acceptance.vision').generate_response_for_image(
                'Describe the color in one word.',base64.b64encode(stream.getvalue()).decode(),'png')
        started=time.monotonic()
        try:
            vision=rt.submit(vision_probe(),timeout=120)
            check('live.vision_provider',bool(service._extract_result_text(vision)),f'{time.monotonic()-started:.2f}s')
        except Exception as exc:
            check('live.vision_provider',False,f'{type(exc).__name__}: {exc}')
        return
    duplicate=service.ingest_context(context)
    check('native.duplicate_suppressed',not duplicate.get('accepted') and bool(duplicate.get('skipped')))
    echo=service.ingest_context(fixture_context(serverId='echo',is_bot=True))
    check('native.bot_echo_suppressed',not echo.get('accepted') and echo.get('skipped')==['bot_echo'])
    bot_interaction_native_acceptance(service)
    if manager._third_party_supervisor is not None and 'wechatbot.scheduled-reminder' in manager._third_party_supervisor.get_loaded_plugin_ids():
        reminder_native_acceptance(service)
    if REPORT.get('agent_tools_test_mode'):
        agent_tools_native_acceptance(service)
    if REPORT.get('autonomous_life_test_mode'):
        autonomous_life_native_acceptance(service)
    if REPORT.get('web_research_test_mode'):
        web_research_native_acceptance(service)
    # Distinct users in a batch must remain distinct database records.
    SCENARIO.update(id='batch-b',action='stop',calls=0)
    items=[fixture_context(serverId='batch-a',user='?',wxid='wxid_a',content='???'),fixture_context(serverId='batch-b',user='?',wxid='wxid_b',content='???')]
    batch=service.handle_batch_message('???',items,force_reply=True,session_id='acceptance@chatroom')
    with get_db_session() as db:
        messages=db.exec(select(Messages).where(Messages.message_id.in_(['batch-a','batch-b']))).all()
        identities={(m.message_id,m.user_id,m.processed_plain_text) for m in messages}
    check('native.batch_identity_and_persistence',identities=={('batch-a','wxid_a','???'),('batch-b','wxid_b','???')},identities)
    # Ordinary mixed-mode routing uses the real Dispatcher method and native intake.
    from core.dispatcher import Dispatcher
    from types import SimpleNamespace
    d=object.__new__(Dispatcher); d.debug=False; d.modules={}; d.llm_service=service; d.prefix_mode='mixed'
    d._context_feature_enabled=lambda *args:True
    SCENARIO.update(id='ordinary-e2e',action='stop',calls=0)
    ordinary=Dispatcher.dispatch(d,None,None,fixture_context(serverId='ordinary-e2e',proactive_candidate=True))
    check('shell.ordinary_message_reaches_native',ordinary.get('accepted')==['ordinary-e2e'])
    # Native failures must complete promptly rather than masquerading as 180s timeout.
    rt.submit(asyncio.sleep(1))
    SCENARIO.update(id='failure-e2e',action='fail',calls=0)
    started=time.monotonic()
    failed=service.handle_message('????','????','??????',wxid='wxid_fail',session_id='failure@chatroom',message_context=fixture_context(group='????',sessionId='failure@chatroom',serverId='failure-e2e'),force_reply=True)
    check('native.provider_failure_propagated',failed.get('_llm_ok') is False and bool(failed.get('error')) and 'exceeded' not in failed.get('error',''),f'{time.monotonic()-started:.2f}s {failed.get("error")}')
    from src.llm_models import quota_cooldown
    quota_cooldown._BLOCKED.clear()  # simulate quota recovery, not quota bypass in production
    SCENARIO.update(id='recovery-e2e',action='stop',calls=0)
    recovered=service.handle_message('????','????','????',wxid='wxid_fail',session_id='failure@chatroom',message_context=fixture_context(group='????',sessionId='failure@chatroom',serverId='recovery-e2e'),force_reply=True)
    check('native.loop_recovers_after_failure',recovered.get('_llm_ok') is True,recovered)
    extended_acceptance(service,d)
    plugin_provenance_native_acceptance(service)


def bot_interaction_native_acceptance(service):
    """Inspect actual HTTP payloads after selected shell requests enter MaiBot."""
    from src.common import bot_interaction_policy as policy
    from src.config.config import global_config
    from src.config.official_configs import LearningExclusionItem
    settle_fixture_turn(service)
    rule = dict(platform='wechat', group_id='acceptance@chatroom', user_id='wxid_external_bot',
                label='验收外部 Bot', is_bot=True, plugin_refusal_probability=0.5)
    exclusion = LearningExclusionItem(platform=rule['platform'], user_id=rule['user_id'], group_id=rule['group_id'])
    with patch.object(policy, '_configured_users', return_value=[rule]), patch.object(
        global_config.learning, 'excluded_users', [exclusion]):
        policy._DECISIONS.clear()
        try:
            start = len(SCENARIO['requests'])
            SCENARIO.update(id='bot-refusal-wire', action='reply', calls=0)
            context = fixture_context(serverId='bot-refusal-wire', wxid=rule['user_id'],
                content='/probe status', command='/probe', args='status', bot_plugin_request=True)
            with patch.object(policy.random, 'random', return_value=0.1):
                result = service.handle_message(context['group'], context['user'], context['content'],
                    wxid=context['wxid'], session_id=context['sessionId'], message_context=context, force_reply=True)
            requests = SCENARIO['requests'][start:]
            planner = [r for r in requests if r.get('tools')]
            replyer = [r for r in requests if not r.get('tools')]
            tool_sets = [{t['function']['name'] for t in r['tools']} for r in planner]
            check('bot.refusal_actual_tool_allowlist', bool(tool_sets) and all(
                'reply' in names and names <= {'reply', 'stop', 'wait'} for names in tool_sets), tool_sets)
            for kind, calls in (('planner', planner), ('replyer', replyer)):
                bodies = [json.dumps(r.get('messages', []), ensure_ascii=False) for r in calls]
                check(f'bot.refusal_{kind}_private_wire_policy', any(
                    rule['user_id'] in body and '必须明确拒绝' in body for body in bodies))
            check('bot.external_identity_not_dropped_as_echo', result.get('native_sent') and result.get('_llm_ok'))
            start = len(SCENARIO['requests'])
            SCENARIO.update(id='human-after-bot-refusal', action='stop', calls=0)
            context = fixture_context(serverId=SCENARIO['id'], content='ordinary message after bot refusal')
            service.handle_message(context['group'], context['user'], context['content'], wxid=context['wxid'],
                session_id=context['sessionId'], message_context=context, force_reply=True)
            tool_sets = [{t['function']['name'] for t in r.get('tools', [])}
                         for r in SCENARIO['requests'][start:] if r.get('tools')]
            check('bot.refusal_tool_restriction_cleared_for_human', any(
                names - {'reply', 'stop', 'wait'} for names in tool_sets), tool_sets)
        finally:
            policy._DECISIONS.clear()


def reminder_native_acceptance(service):
    """Real model tool call -> Runner -> database -> proactive turn -> intercepted send."""
    import sqlite3
    from src.common.paths import get_instance_root
    rt = service._native_runtime
    database = get_instance_root()/'data/plugins/wechatbot.scheduled-reminder/reminders.sqlite3'
    request_start=len(SCENARIO['requests'])
    SCENARIO.update(id='reminder-real-request',action='reminder_schedule',calls=0)
    context=fixture_context(serverId=SCENARIO['id'],content='Remind me to check the backup in five minutes.')
    result=service.handle_message(context['group'],context['user'],context['content'],wxid=context['wxid'],
        session_id=context['sessionId'],message_context=context,force_reply=True)
    with sqlite3.connect(database) as db:
        db.row_factory=sqlite3.Row
        row=db.execute('SELECT * FROM reminders ORDER BY id DESC LIMIT 1').fetchone()
    check('reminder.llm_runner_tool_created_with_trusted_requester',result.get('_llm_ok') and row is not None
          and row['owner_id']==context['wxid'] and row['group_id']==context['sessionId']
          and row['source_message_id']==context['serverId'] and row['status']=='pending',dict(row) if row else result)
    if row is None:
        REPORT['reminder_diagnostic_requests']=SCENARIO['requests'][request_start:]
        check('reminder.creation_diagnostic',False,[(getattr(m,'tool_name',''),getattr(m,'processed_plain_text',''))
              for runtime in rt._runtimes.values() for m in runtime._chat_history if getattr(m,'tool_name','')])
        return
    sid=row['stream_id'];before=rt._delivery_seq.get(sid,0)
    SCENARIO.update(id=context['serverId'],action='reply',calls=0,attach_at=context['wxid'])
    start=len(SCENARIO['requests'])
    with sqlite3.connect(database) as db:
        db.execute('UPDATE reminders SET due_at=0,retry_at=0 WHERE id=?',(row['id'],))
    deadline=time.monotonic()+30
    status='pending'
    while time.monotonic()<deadline:
        rt.submit(asyncio.sleep(.25))
        with sqlite3.connect(database) as db:
            status=db.execute('SELECT status FROM reminders WHERE id=?',(row['id'],)).fetchone()[0]
        if rt._delivery_seq.get(sid,0)>before or status in {'failed','uncertain'}:
            break
    rt.submit(asyncio.sleep(.5))
    SCENARIO.pop('attach_at',None)
    check('reminder.due_submitted_to_native_planner',status=='submitted',status)
    check('reminder.native_prompt_contains_due_data',any('reminder acceptance' in json.dumps(
        r.get('messages',[]),ensure_ascii=False) and 'scheduled_reminder_due' in json.dumps(
        r.get('messages',[]),ensure_ascii=False) for r in SCENARIO['requests'][start:]))
    outputs=[o for o in rt._outputs.get(sid,[]) if o['sequence']>before]
    operations=[op for o in outputs for op in o['operations']]
    check('reminder.native_wechat_delivery_with_correct_at',bool(outputs) and any(
        segment.get('type')=='mention' and segment.get('wxid')==context['wxid']
        for op in operations for segment in op.get('native_segments',[])),operations)


def settle_fixture_turn(service, session_id=None):
    # A delivered reply can precede the Planner's final stop call. Do not change
    # the shared HTTP fixture scenario while that previous turn is still active.
    async def settle():
        for _ in range(300):
            # Observed messages may intentionally remain pending after stop;
            # wait for scheduled/active turns, not for every observation to reply.
            if all(runtime._agent_state != 'running' and not runtime._message_turn_scheduled
                   for sid,runtime in service._native_runtime._runtimes.items() if not session_id or sid==session_id):
                return
            await asyncio.sleep(.05)
        raise TimeoutError('previous fixture Planner turn did not settle')
    service._native_runtime.submit(settle(),timeout=20)


def plugin_provenance_native_acceptance(service):
    """Real shell dispatch/send confirmation -> DB -> runtime -> model HTTP context."""
    from core.dispatcher import Dispatcher
    from core.worker import Worker
    from core.sender import send
    from types import SimpleNamespace
    from src.common.database.database import get_db_session
    from src.common.database.database_model import Messages
    from src.chat.message_receive.message import SessionMessage
    from src.maisaka.runtime import MaisakaHeartFlowChatting
    from sqlmodel import select
    rt = service._native_runtime
    settle_fixture_turn(service)
    SCENARIO.update(id='provenance-baseline',action='stop',calls=0)
    baseline=fixture_context(serverId='provenance-baseline',force_reply=True,timestamp=time.time())
    service.handle_message(baseline['group'],baseline['user'],baseline['content'],wxid=baseline['wxid'],
        session_id=baseline['sessionId'],message_context=baseline,force_reply=True)
    settle_fixture_turn(service)
    context = fixture_context(serverId='plugin-provenance-trigger', content='/probe', command='/probe')
    dispatcher = Dispatcher(); dispatcher.debug=False; dispatcher.llm_service=service
    def execute(args, ctx):
        send(target=ctx['group'], content='provenance_direct_output', preserve_text=True)
        return 'provenance_return_output'
    module = SimpleNamespace(__name__='plugins.probe', handle=execute)
    dispatcher.plugins={'/probe': module}; dispatcher.modules={}
    worker=object.__new__(Worker); worker.dispatcher=dispatcher; worker._task_local=threading.local()
    with patch.dict(os.environ, {'MAIBOT_INTERCEPT_SEND': '0'}), patch('core.sender._send_wechat_text', return_value=(True,'')):
        result=dispatcher.dispatch('/probe','',context)
        worker._task_local.plugin_id=result.get('_shell_plugin_id'); worker._task_local.plugin_context=context
        worker._send_plugin_result(target=context['group'], content=result['messages'][0], preserve_text=True)
        with patch('core.sender._send_wechat_text', return_value=(False,'fixture failure')):
            worker._send_plugin_result(target=context['group'], content='provenance_failed_output', preserve_text=True)
    rt.submit(asyncio.sleep(.3))
    with get_db_session() as db:
        rows=db.exec(select(Messages).where(Messages.processed_plain_text.in_(
            ['provenance_direct_output','provenance_return_output','provenance_failed_output']))).all()
        messages=[SessionMessage.from_db_instance(row) for row in rows]
    check('provenance.shell_only_successful_sends_persisted',len(messages)==2,[m.processed_plain_text for m in messages])
    check('provenance.shell_trigger_and_platform_preserved',len(messages)==2 and all(
        m.platform=='wechat' and m.message_info.additional_config['plugin_activity']['plugin_id']=='shell.probe'
        and m.message_info.additional_config['plugin_activity']['trigger_message_id']==context['serverId']
        and m.message_info.additional_config['message_origin']['direction']=='outbound' for m in messages))
    check('provenance.restart_does_not_reclassify_as_llm_reply',bool(messages) and all(
        MaisakaHeartFlowChatting._resolve_restored_message_source_kind(m)=='plugin_output' for m in messages))
    async def history_check():
        return [m for runtime in rt._runtimes.values() for m in runtime._chat_history
                if getattr(m, 'message_id', None) in {m.message_id for m in messages}]
    history=rt.submit(history_check())
    check('provenance.live_context_has_plugin_observations',len(history)==2 and all(
        m.source=='plugin_output' and m.role=='user' and 'llm_decision="false"' in str(m.raw_message.to_dict())
        for m in history),[(m.source,m.role) for m in history])
    check('provenance.shell_send_commit_order_matches_delivery',
        [m.original_message.processed_plain_text for m in history]==['provenance_direct_output','provenance_return_output'])
    echo=service.ingest_context(fixture_context(serverId='plugin-outgoing-echo',
        wxid=rt._translator.bot_user_id,content='provenance_direct_output',source_kind='bot_echo_external'))
    check('provenance.plugin_self_echo_not_replayed',echo.get('skipped')==['bot_echo'] and len(rt.submit(history_check()))==2)
    SCENARIO.update(id='provenance-next-turn',action='reply',calls=0)
    start=len(SCENARIO['requests'])
    ctx=fixture_context(serverId='provenance-next-turn',content='Continue after plugin output')
    service.handle_message(ctx['group'],ctx['user'],ctx['content'],wxid=ctx['wxid'],
        session_id=ctx['sessionId'],message_context=ctx,force_reply=True)
    calls=SCENARIO['requests'][start:]
    for name,items in [('planner',[r for r in calls if r.get('tools')]),('replyer',[r for r in calls if not r.get('tools')])]:
        text=json.dumps([r.get('messages',[]) for r in items],ensure_ascii=False)
        check('provenance.'+name+'_actual_wire_marks_shell_plugin',all(value in text for value in
            ['shell.probe','plugin_output','provenance_direct_output','provenance_return_output','platform=\\"wechat\\"','llm_decision=\\"false\\"']))
    settle_fixture_turn(service)


def agent_tools_native_acceptance(service):
    """Real Planner -> SDK Runner -> Host; only external RCON/Bili IO is mocked."""
    from src.plugin_runtime.integration import get_plugin_runtime_manager
    from src.plugin_runtime import wechatbot_agent_tools as gate
    rt=service._native_runtime
    loaded=set(get_plugin_runtime_manager()._third_party_supervisor.get_loaded_plugin_ids())
    expected={'wechatbot.restricted-python','wechatbot.restricted-rcon','wechatbot.scheduled-reminder',
              'github.erhan111119.mai-repost-shit'}
    check('agent_tools.all_native_runner_plugins_loaded',expected<=loaded,sorted(loaded))
    def invoke(name,args,message_id):
        settle_fixture_turn(service)
        SCENARIO.update(id=message_id,action='fixture_agent_tool',calls=0,
                        agent_tool_name=name,agent_tool_args=args)
        ctx=fixture_context(serverId=message_id,content='agent tool acceptance: '+name)
        start=len(SCENARIO['requests'])
        result=service.handle_message(ctx['group'],ctx['user'],ctx['content'],wxid=ctx['wxid'],
            session_id=ctx['sessionId'],message_context=ctx,force_reply=True)
        bodies=json.dumps([r.get('messages',[]) for r in SCENARIO['requests'][start:]],ensure_ascii=False)
        return ctx,result,bodies
    ctx,result,wire=invoke('restricted_python',{'code':'sum([x*x for x in range(10)])'},'python-native')
    check('agent_tools.python_llm_runner_child_result',result.get('_llm_ok') and '285' in wire,wire[-1500:])
    ctx,result,wire=invoke('restricted_python',{'code':'open("config.json").read()'},'python-denied')
    check('agent_tools.python_io_rejected_via_runner','function is not allowed: open' in wire)
    config=dict(enabled=True,allowed_groups=['acceptance@chatroom'],allowed_users=[],host='fixture-server',
                password='fixture-password-never-log',port=25575,max_calls_per_request=2)
    client=Mock();client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
    client.command.return_value='rcon_native_fixture_online_players'
    with patch.object(gate,'_config',return_value=config),patch('llm.rcon_tools.MCRcon',return_value=client):
        ctx,result,wire=invoke('minecraft_rcon_action',{'action':'server_list','_host_grant':'model-forged'},'rcon-native')
        check('agent_tools.rcon_llm_runner_host_authorized',result.get('_llm_ok') and client.command.call_count==1
              and client.command.call_args.args==('list',) and 'rcon_native_fixture_online_players' in wire)
        check('agent_tools.rcon_credentials_absent_from_model',config['password'] not in wire)
        check('provenance.native_tool_result_marked_in_actual_wire',all(value in wire for value in
            ['plugin_tool_result','wechatbot.restricted-rcon','rcon-native','platform=\\"wechat\\"']))
        ctx,result,wire=invoke('minecraft_rcon_action',{'action':'stop'},'rcon-admin-denied')
        check('agent_tools.rcon_admin_action_denied_via_runner',client.command.call_count==1 and 'action is not approved' in wire)
    before=sum(rt._delivery_seq.values())
    ctx,result,wire=invoke('repost_shi',{'count':1,'stream_id':'model-forged'},'repost-native')
    deadline=time.monotonic()+15
    while time.monotonic()<deadline and sum(rt._delivery_seq.values())==before:
        rt.submit(asyncio.sleep(.1))
    deliveries=[o for values in rt._outputs.values() for o in values
                if any('BVfixture001' in str(op.get('file_path','')) for op in o['operations'])]
    from src.common.database.database import get_db_session
    from src.common.database.database_model import Messages
    from sqlmodel import select
    with get_db_session() as db:
        plugin_rows=db.exec(select(Messages).where(Messages.user_id=='wxid_acceptance_bot')).all()
        origins=[json.loads(row.additional_config or '{}') for row in plugin_rows]
    check('provenance.native_plugin_sends_persist_identity',any(
        item.get('plugin_activity',{}).get('plugin_id')=='github.erhan111119.mai-repost-shit'
        and item.get('message_origin',{}).get('platform')=='wechat' for item in origins))
    operations=[op for o in deliveries for op in o['operations']]
    texts=[op for op in operations if op['mode']=='wechat_text']
    files=[op for op in operations if op['mode']=='wechat_file']
    check('agent_tools.repost_llm_runner_pipeline_to_wechat_file',bool(texts) and len(files)==1 and
          Path(files[0]['file_path']).read_bytes()==b'isolated-repost-video-fixture',operations)
    check('agent_tools.repost_at_in_header_stays_plain_text',bool(texts) and '@LLM' in texts[0]['content'] and
          not texts[0].get('resolve_mentions') and not texts[0].get('native_segments'))
    check('agent_tools.repost_quotes_absent_and_group_scope_preserved',bool(deliveries) and all(
        o.get('quote_mode') is None for o in deliveries) and all('WX_AT:' not in str(o) for o in deliveries))
    settle_fixture_turn(service)
    SCENARIO.update(id='repost-proactive',action='fixture_agent_tool',calls=0,
                    agent_tool_name='repost_shi',agent_tool_args={'count':1})
    sid=next(sid for sid,runtime in rt._runtimes.items() if str(runtime.chat_stream.group_id)=='acceptance@chatroom')
    start=len(SCENARIO['requests']);before=rt._delivery_seq.get(sid,0)
    queued=rt.submit(get_plugin_runtime_manager()._cap_maisaka_proactive_trigger(
        'github.erhan111119.mai-repost-shit','maisaka.proactive.trigger',dict(stream_id=sid,
        intent='群友持续分享B站视频，请自行判断是否调用repost_shi跟玩一条；不适合则保持安静。',priority='low')))
    check('agent_tools.repost_proactive_native_task_queued',queued.get('success') and queued.get('queued'),queued)
    deadline=time.monotonic()+20
    while time.monotonic()<deadline and rt._delivery_seq.get(sid,0)==before:
        rt.submit(asyncio.sleep(.1))
    new_outputs=[o for o in rt._outputs.get(sid,[]) if o['sequence']>before]
    check('agent_tools.repost_proactive_planner_sees_intent',any('群友持续分享B站视频' in json.dumps(
        r.get('messages',[]),ensure_ascii=False) for r in SCENARIO['requests'][start:]))
    check('agent_tools.repost_proactive_runner_to_wechat_file',any(
        op.get('mode')=='wechat_file' and 'BVfixture002' in op.get('file_path','')
        for output in new_outputs for op in output['operations']),new_outputs)
    settle_fixture_turn(service)
    SCENARIO.update(action='stop',calls=0)


def web_research_native_acceptance(service):
    rt = service._native_runtime
    start = len(SCENARIO['requests'])
    SCENARIO.update(id='web-research-e2e', action='research_chain', calls=0)
    context = fixture_context(serverId=SCENARIO['id'], content='查一下这个陌生技术的当前版本，先搜索再读官方文档。')
    before = rt._delivery_seq.copy()
    result = service.handle_message(GROUP, USER, context['content'], wxid=context['wxid'],
        session_id=context['sessionId'], message_context=context, force_reply=True)
    requests = SCENARIO['requests'][start:]
    planners = [r for r in requests if r.get('tools')]
    first = {t['function']['name']: t['function'] for t in planners[0]['tools']}
    check('research.visible_without_tool_search', {'internet_search', 'read_webpage'} <= first.keys())
    check('research.query_before_answer_guidance', '先查证再决策/回答' in first['internet_search']['description'])
    wire = json.dumps([r.get('messages', []) for r in requests], ensure_ascii=False)
    check('research.search_runner_result_in_context', 'verified_search_evidence_741' in wire)
    check('research.page_runner_result_in_context', 'verified_page_evidence_852' in wire)
    check('research.external_evidence_marked', '外部网页资料' in wire and 'example.com/research-acceptance' in wire)
    check('research.answer_after_evidence', result.get('_llm_ok') and result.get('native_sent') and
          any(rt._delivery_seq.get(k, 0) > before.get(k, 0) for k in rt._delivery_seq), str(result)[:300])
    check('research.replyer_received_page_evidence', any('verified_page_evidence_852' in json.dumps(r.get('messages', []))
          for r in requests if not r.get('tools')))
    REPORT['research_requests'] = len(requests)
    SCENARIO.update(action='stop', calls=0)


def autonomous_life_native_acceptance(service):
    from src.plugin_runtime.integration import get_plugin_runtime_manager
    from src.common.paths import get_instance_root
    rt = service._native_runtime
    settle_fixture_turn(service)
    SCENARIO.update(id='autonomy-evidence',action='stop',calls=0)
    context=fixture_context(serverId='autonomy-evidence',content='Let us try a small Python square experiment.',timestamp=time.time())
    rt.handle_contexts([context],wait_for_completion=True)
    sid=rt.submit(rt._translator.translate(context)).session_id
    before=rt._delivery_seq.get(sid,0); start=len(SCENARIO['requests'])
    SCENARIO['autonomous_life']=True
    try:
        result=rt.submit(get_plugin_runtime_manager()._cap_api_call('wechatbot.autonomous-life','api.call',
            dict(api_name='autonomous_life.acceptance_cycle',args={})),timeout=180)
    finally:
        SCENARIO['autonomous_life']=False
    snap=result.get('result') or {}
    check('autonomous.native_runner_project_continuity', result.get('success') and any(
        p['title']=='autonomous native experiment' and p['status']=='done' for p in snap.get('projects',[])),result)
    output=[o for o in rt._outputs.get(sid,[]) if o['sequence']>before]
    check('autonomous.native_replyer_to_wechat_at',any(op.get('mode')=='wechat_text' and
          any(s.get('type')=='mention' and s.get('wxid')=='wxid_acceptance_user' for s in op.get('native_segments',[]))
          for o in output for op in o['operations']),output)
    check('autonomous.sandbox_rpc_actual_artifact', 'autonomy_artifact_verified' in json.dumps(snap) and
          list((get_instance_root()/'data/plugins/wechatbot.autonomous-life/artifacts').rglob('result.json')))
    bodies=[json.dumps(r.get('messages',[]),ensure_ascii=False) for r in SCENARIO['requests'][start:]]
    check('autonomous.actual_model_context_has_persona_and_project', any('GSMPS' in b and 'Let us try' in b for b in bodies))
    from src.config.config import global_config
    check('autonomous.replyer_uses_native_style', any(global_config.personality.reply_style in b and '自主分享候选' in b for b in bodies))
    before=rt._delivery_seq.get(sid,0)
    SCENARIO.update(autonomous_life=True,autonomy_stage=2)
    try:
        rt.submit(get_plugin_runtime_manager()._cap_api_call('wechatbot.autonomous-life','api.call',
            dict(api_name='autonomous_life.acceptance_cycle',args={})),timeout=180)
    finally:
        SCENARIO.update(autonomous_life=False,autonomy_stage=1)
    pending=rt.submit(get_plugin_runtime_manager()._cap_api_call('wechatbot.autonomous-life','api.call',
        dict(api_name='autonomous_life.acceptance_flush_progress',args={})),timeout=120)
    check('life_progress.native_cooldown_keeps_latest_candidate',pending.get('result',{}).get('pending')==1 and rt._delivery_seq.get(sid,0)==before,pending)
    start=len(SCENARIO['requests'])
    released=rt.submit(get_plugin_runtime_manager()._cap_api_call('wechatbot.autonomous-life','api.call',
        dict(api_name='autonomous_life.acceptance_flush_progress',args=dict(release_cooldown=True))),timeout=120)
    check('life_progress.native_candidate_released_once',released.get('result',{}).get('sent') and released.get('result',{}).get('pending')==0,released)
    check('life_progress.native_replyer_has_real_stage_and_next_step',any('progress_stage_two_verified' in json.dumps(r.get('messages',[]),ensure_ascii=False) and
        'project_progress' in json.dumps(r.get('messages',[]),ensure_ascii=False) and 'next_step是计划' in json.dumps(r.get('messages',[]),ensure_ascii=False)
        for r in SCENARIO['requests'][start:]))
    check('life_progress.native_delivery_keeps_correct_wechat_at',any(op.get('mode')=='wechat_text' and
        any(s.get('type')=='mention' and s.get('wxid')=='wxid_acceptance_user' for s in op.get('native_segments',[]))
        for o in rt._outputs.get(sid,[]) if o['sequence']>before for op in o['operations']))
    settle_fixture_turn(service)
    SCENARIO.update(action='stop',calls=0)
    for n in range(6):
        context=fixture_context(serverId='autonomy-sharing-'+str(n),wxid='wxid_acceptance_user' if n%2 else 'wxid_other',
            content='https://b23.tv/acceptance'+str(n),timestamp=time.time())
        service.ingest_context(context)
    settle_fixture_turn(service)
    before=rt._delivery_seq.get(sid,0)
    SCENARIO['autonomous_life']=True
    try:
        repost=rt.submit(get_plugin_runtime_manager()._cap_api_call('wechatbot.autonomous-life','api.call',
            dict(api_name='autonomous_life.acceptance_repost',args={})),timeout=180)
    finally:
        SCENARIO['autonomous_life']=False
    check('autonomous.coordinated_repost_rpc_grant_and_delivery',repost.get('success') and (repost.get('result') or {}).get('sent'),repost)
    check('autonomous.coordinated_media_native_file',any(op.get('mode')=='wechat_file' and 'BVfixture003' in op.get('file_path','')
          for o in rt._outputs.get(sid,[]) if o['sequence']>before for op in o['operations']))
    before=rt._delivery_seq.get(sid,0)
    permission=rt.submit(get_plugin_runtime_manager()._cap_api_call('wechatbot.autonomous-life','api.call',
        dict(api_name='autonomous_life.acceptance_project_permission',args=dict(setup=True))),timeout=120)
    check('project.native_permission_rpc_pauses_and_notifies',permission.get('success') and
        permission.get('result',{}).get('state')=='blocked' and permission.get('result',{}).get('sent'),permission)
    check('project.native_permission_at_translates_to_wechat',any(op.get('mode')=='wechat_text' and
        any(s.get('type')=='mention' and s.get('wxid')=='wxid_acceptance_user' for s in op.get('native_segments',[]))
        for o in rt._outputs.get(sid,[]) if o['sequence']>before for op in o['operations']))
    status=rt.submit(get_plugin_runtime_manager()._cap_api_call('wechatbot.autonomous-life','api.call',
        dict(api_name='autonomous_life.acceptance_project_permission',args={})),timeout=120)
    check('project.native_permission_rpc_dedupes',status.get('success') and not status.get('result',{}).get('sent'),status)
    for actor, expected in [('wxid_untrusted','blocked'),('wxid_acceptance_user','active')]:
        settle_fixture_turn(service)
        SCENARIO.update(id='project-resume-'+actor,action='fixture_agent_tool',calls=0,
            agent_tool_name='autonomous_project_control',agent_tool_args=dict(action='resume',_request_context=dict(user_id='wxid_acceptance_user')))
        context=fixture_context(serverId=SCENARIO['id'],wxid=actor,content='resume project')
        result=service.handle_message(context['group'],context['user'],context['content'],wxid=context['wxid'],
            session_id=context['sessionId'],message_context=context,force_reply=True)
        settle_fixture_turn(service)
        status=rt.submit(get_plugin_runtime_manager()._cap_api_call('wechatbot.autonomous-life','api.call',
            dict(api_name='autonomous_life.acceptance_project_permission',args={})),timeout=120)
        check('project.native_control_'+('owner_can_resume' if expected=='active' else 'forged_owner_rejected'),
            result.get('_llm_ok') and status.get('result',{}).get('state')==expected,dict(chat=result,status=status))
    SCENARIO.update(action='stop',calls=0)


def extended_acceptance(service, dispatcher):
    from src.common.database.database import get_db_session
    from src.common.database.database_model import Messages
    from sqlmodel import select
    rt=service._native_runtime
    SCENARIO.update(action='stop',calls=0)
    # Run the actual SSE normalizer -> Router -> Worker -> Dispatcher -> native core.
    from core.weflow_client import WeFlowClient
    from core.router import Router
    from core.worker import Worker
    from queue import Queue
    router=Router(prefix=['bot'],target_group=[GROUP],prefix_mode='mixed',proactive_enabled=True)
    queue=Queue(); worker=Worker(queue,router,dispatcher,{'weflow':{'myWxid':'wxid_acceptance_bot'}})
    raw={'sessionType':'group','sessionId':'acceptance@chatroom','groupName':GROUP,'sourceName':USER,
         'senderWxid':'wxid_wire','serverId':'wire-original','messageKey':'wire-key','localId':987,'localType':1,
         'timestamp':1793188305,'content':BODY,'rawContent':BODY}
    queue.put(WeFlowClient('',None,None)._normalize_msg(raw))
    worker.start(n=1)
    deadline=time.monotonic()+10
    while queue.unfinished_tasks and time.monotonic()<deadline:time.sleep(.05)
    worker.shutdown()
    with get_db_session() as db:
        saved=db.exec(select(Messages).where(Messages.message_id=='wire-original')).first()
    check('shell.sse_router_worker_native_persistence',saved is not None and saved.user_id=='wxid_wire' and saved.processed_plain_text==BODY)
    check('shell.native_owns_storage_and_learning',worker.memory_manager is None and worker.style_learner is None)
    # Per-group disabled autonomous replies still admit observation; explicit calls work.
    service.config.setdefault('group_profiles',{})['observe-group']={'auto_reply':{'enabled':False}}
    quiet=service.ingest_context(fixture_context(group='observe-group',sessionId='observe@chatroom',serverId='observe-id'))
    check('shell.disabled_auto_reply_observes_only',quiet.get('dispositions',{}).get('observe-id')=='observed')
    explicit=service.ingest_context(fixture_context(group='observe-group',sessionId='observe@chatroom',serverId='explicit-id',prefix_used=True))
    check('shell.disabled_auto_reply_allows_explicit_request',explicit.get('dispositions',{}).get('explicit-id')=='ingested')
    rt.submit(asyncio.sleep(.5))
    # System and hidden records cannot be restored into model context after restart.
    hidden=service.ingest_context(fixture_context(serverId='hidden-id',prompt_visible=False,learnable=False))
    notice=service.ingest_context(fixture_context(serverId='notice-id',localType=10000,source_kind='system_event',learnable=False,prompt_visible=False))
    check('native.hidden_and_notice_are_observations',all(x.get('dispositions',{}).get(i)=='observed' for x,i in [(hidden,'hidden-id'),(notice,'notice-id')]))
    from unittest.mock import AsyncMock
    from src.services.memory_flow_service import memory_automation_service
    m=rt.submit(rt._translator.translate(fixture_context(learnable=False)))
    async def check_learning():
        with patch.object(memory_automation_service.image_writeback,'enqueue',new_callable=AsyncMock) as enqueue:
            await memory_automation_service.on_incoming_message(m)
            return enqueue.call_count==0
    check('native.nonlearnable_skips_image_learning',rt.submit(check_learning()))
    # Native forward-browser and memory tools execute against the admitted session.
    primary_sid=rt.submit(rt._translator.translate(fixture_context())).session_id
    settle_fixture_turn(service,primary_sid)
    SCENARIO.update(id='quote-window-external',action='reply',calls=0)
    quote_context=fixture_context(serverId='quote-window-external',content='quote_current_body',
        quoted_message_id='quote-not-in-history',quoted_sender='quoted_source_author',
        quoted_sender_wxid='wxid_quoted_author',quoted_content='quoted_snapshot_outside_history\nA & B')
    start=len(SCENARIO['requests'])
    quote_result=service.handle_message(quote_context['group'],quote_context['user'],quote_context['content'],
        wxid=quote_context['wxid'],session_id=quote_context['sessionId'],message_context=quote_context,force_reply=True)
    for kind,with_tools in [('planner',True),('replyer',False)]:
        check('native.quote_snapshot_reaches_'+kind,quote_result.get('_llm_ok') and any(
            bool(r.get('tools'))==with_tools and 'quoted_snapshot_outside_history' in json.dumps(r.get('messages',[]))
            and 'quoted_source_author' in json.dumps(r.get('messages',[]))
            and 'quote_current_body' in json.dumps(r.get('messages',[])) for r in SCENARIO['requests'][start:]))
    settle_fixture_turn(service,primary_sid)
    SCENARIO.update(action='stop',calls=0)
    from html import escape
    child='<recordinfo><datalist><dataitem dataid="child-node"><sourcename>Nested Author</sourcename><datadesc>nested browser evidence</datadesc></dataitem></datalist></recordinfo>'
    record=escape('<recordinfo><datalist><dataitem dataid="forward-node"><sourcename>Alice</sourcename><datadesc>forward evidence</datadesc></dataitem>'+
        '<dataitem datatype="17"><sourcename>Nested Sender</sourcename><recordxml>'+escape(child)+'</recordxml></dataitem>'+
        ''.join('<dataitem><sourcename>Author '+str(n)+'</sourcename><datadesc>full browser tail '+str(n)+'</datadesc></dataitem>' for n in range(5))+'</datalist></recordinfo>')
    forward=fixture_context(serverId='native-forward',localType=49,content=f'<msg><appmsg><title>record</title><type>19</type><recorditem>{record}</recorditem></appmsg></msg>',force_reply=True)
    service._native_runtime.handle_contexts([forward],wait_for_completion=True)
    sid=rt.submit(rt._translator.translate(forward)).session_id
    async def tools():
        from src.maisaka.builtin_tool import build_builtin_tool_handlers,get_all_builtin_tool_specs
        from src.maisaka.builtin_tool.context import BuiltinToolRuntimeContext
        from src.core.tooling import ToolInvocation,ToolAvailabilityContext
        from src.maisaka.tool_result_store import tool_result_store
        from src.common.message_repository import find_messages
        from src.maisaka.context.planner_messages import build_planner_user_prefix_from_session_message
        from src.maisaka.context.messages import build_full_complex_message_content_from_sequence
        restored_quote=find_messages(session_id=sid,message_id='quote-window-external',limit=1)
        check('native.quote_snapshot_survives_database_restore',bool(restored_quote) and
            'quoted_snapshot_outside_history' in build_planner_user_prefix_from_session_message(restored_quote[0]))
        restored_forward=find_messages(session_id=sid,message_id='native-forward',limit=1)
        check('native.forward_nested_nodes_survive_database_restore',bool(restored_forward) and
            'nested browser evidence' in build_full_complex_message_content_from_sequence(restored_forward[0].raw_message,[0,0]))
        runtime=rt._runtimes[sid]; context=BuiltinToolRuntimeContext(runtime._reasoning_engine,runtime)
        handlers=build_builtin_tool_handlers(context)
        specs=get_all_builtin_tool_specs(ToolAvailabilityContext(session_id=sid,is_group_chat=True,platform='wechat'))
        check('native.enabled_tool_catalog_has_handlers',all(s.name in handlers for s in specs),','.join(s.name for s in specs))
        from PIL import Image
        stream=io.BytesIO();Image.new('RGB',(64,64),'red').save(stream,format='PNG')
        await rt._ingest([fixture_context(serverId='image-memory-source',localType=3,content='[图片]',binary_data=stream.getvalue(),learnable=False)],observe_only=True)
        # ModelFixture serves both text/image embeddings from startup, so probe
        # caches and the real tool share one stable space without external I/O.
        result=await handlers['query_image_memory'](ToolInvocation('query_image_memory',{'message_id':'image-memory-source','component_path':'0'},session_id=sid))
        REPORT['image_embedding_transport']='deterministic local HTTP fixture; live embedding availability is not asserted'
        check('native.tool.query_image_memory',result.success,result.error_message or result.content)
        for name,args in [('query_memory',{'query':'acceptance'}),('tool_search',{'query':'view_forward_message'}),('view_forward_message',{'msg_id':'native-forward'})]:
            result=await handlers[name](ToolInvocation(name,args,session_id=sid))
            check('native.tool.'+name,result.success,result.error_message or result.content[:180])
            if name=='view_forward_message':
                check('native.forward_browser_retains_beyond_preview',result.success and 'full browser tail 4' in result.content)
        result=await handlers['view_forward_message'](ToolInvocation('view_forward_message',{'msg_id':'native-forward','path':[0,0]},session_id=sid))
        check('native.forward_browser_nested_path',result.success and 'nested browser evidence' in result.content and 'Nested Author' in result.content)
        result=await handlers['view_forward_message'](ToolInvocation('view_forward_message',{'msg_id':'native-forward','path':[0,99]},session_id=sid))
        check('native.forward_browser_invalid_path_rejected',not result.success)
        result=await handlers['query_person_profile'](ToolInvocation('query_person_profile',{'person_name':USER},session_id=sid))
        check('native.tool.person_profile_query_completed',result.success or result.structured_content is not None,result.error_message or result.content[:180])
        rid=tool_result_store.save(sid,'acceptance','first\nprecise evidence\nlast')
        result=await handlers['read_tool_result'](ToolInvocation('read_tool_result',{'result_id':rid,'keyword':'precise'},session_id=sid))
        check('native.tool.raw_result_readback',result.success and 'precise evidence' in result.content)
        from src.emoji_system.emoji_manager import emoji_manager
        from src.common.data_models.image_data_model import MaiEmoji
        from src.common.paths import get_instance_root
        from PIL import Image
        image_path=get_instance_root()/'temp'/'acceptance-emoji.png';image_path.parent.mkdir(exist_ok=True)
        Image.new('RGB',(32,32),'red').save(image_path)
        emoji=MaiEmoji(image_path);await emoji.calculate_hash_format();emoji.description='acceptance happy'
        status=emoji_manager.register_emoji_to_db(emoji);emoji_manager.emojis.append(emoji)
        check('native.emoji_registration',status=='registered')
        SCENARIO['emoji_selection']=True
        try:result=await handlers['send_emoji'](ToolInvocation('send_emoji',{'msg_id':'native-forward'},session_id=sid))
        finally:SCENARIO['emoji_selection']=False
        check('native.tool.send_emoji',result.success,result.error_message or result.content)
        # Existing unconfigured features remain disabled instead of creating fake capabilities.
        from src.config.config import global_config,model_config
        check('native.disabled_subagent_not_exposed',global_config.capabilities.planner_subagent or not any(s.name=='subagent' for s in specs))
        REPORT['feature_configuration']={'asr':global_config.voice.enable_asr,'agent':global_config.agent.enabled,
            'subagent':global_config.capabilities.planner_subagent,'web_search_models':list(model_config.model_task_config.web_search.model_list),
            'embedding_models':list(model_config.model_task_config.embedding.model_list)}
    rt.submit(tools(),timeout=90)
    service.ingest_context(fixture_context(serverId='native-mention-user',wxid='wxid_other',user='群友甲'),observe_only=True)
    # Native send service, send hooks, Platform IO, real sender HTTP payload and storage.
    async def outbound_pipeline():
        from src.services import send_service
        from src.common.message_repository import find_messages
        from src.common.data_models.message_component_data_model import MessageSequence,TextComponent,AtComponent,ImageComponent,EmojiComponent,VoiceComponent,FileComponent
        from PIL import Image
        from types import SimpleNamespace
        stream=io.BytesIO();Image.new('RGB',(2,2),'red').save(stream,format='PNG');data=stream.getvalue()
        reply=find_messages(session_id=sid,message_id='native-forward',limit=1)[0]
        response=SimpleNamespace(status_code=200,json=lambda:{'status':'success'})
        sequence=MessageSequence([TextComponent(BODY),AtComponent('wxid_other'),ImageComponent(binary_hash='',binary_data=data),EmojiComponent(binary_hash='',binary_data=data),VoiceComponent(binary_hash='',binary_data=b'RIFF-voice-fixture'),FileComponent(name='precise.txt',base64_data=base64.b64encode(b'file bytes').decode())])
        with patch.dict(os.environ,{'MAIBOT_INTERCEPT_SEND':'0'}),patch('core.sender._apply_send_delay'),patch('core.sender.requests.post',return_value=response) as post:
            sent=await send_service.custom_reply_set_to_stream_with_message(sequence,sid,set_reply=True,reply_message=reply)
        payloads=[c.kwargs['json'] for c in post.call_args_list]
        check('native.send_pipeline_payload_order',sent is not None and len(payloads)==5 and ['content' if 'content'in p else 'voice' if 'voice'in p else 'file' for p in payloads]==['content','file','file','voice','file'],payloads[0] if payloads else None)
        check('native.send_pipeline_drops_quote_preserves_text_and_at',payloads and payloads[0].get('content')==BODY+'[[WX_AT:wxid_other]]' and payloads[0].get('preserve_text') is True and 'forward evidence' not in payloads[0]['content'])
        # Exercise the real HTTP handler and verified native member resolution;
        # only the final WeChat desktop calls are replaced with a test sender.
        from core.wechat_sender.wechat_hook_server import WeChatHookHandler,PersistentHTTPServer
        import requests
        desktop=Mock();desktop.send_text.return_value=True;desktop.send_text_with_mentions.return_value=True
        desktop.send_file.return_value=True;desktop.send_voice.return_value=True
        class IsolatedHook(WeChatHookHandler):
            @classmethod
            def _get_sender(cls):return desktop
            def _submit_send(self,operation,data,kind):return operation()
        hook=PersistentHTTPServer(('127.0.0.1',0),IsolatedHook)
        thread=threading.Thread(target=hook.serve_forever,daemon=True);thread.start()
        try:
            responses=await asyncio.to_thread(lambda:[requests.post(f'http://127.0.0.1:{hook.server_port}/wxSend',json=p,timeout=5) for p in payloads])
            check('shell.actual_http_handler_media_dispatch',all(r.status_code==200 for r in responses) and desktop.send_file.call_count==3 and desktop.send_voice.call_count==1)
            segments=desktop.send_text_with_mentions.call_args.args[0] if desktop.send_text_with_mentions.called else []
            check('shell.native_at_expanded_with_preserved_text',any(s.get('wxid')=='wxid_other' and s.get('nickname')=='群友甲' for s in segments) and not desktop.send_text.called,segments)
            literal={'target':GROUP,'content':'literal [[WX_AT:wxid_other]]','preserve_text':True}
            response=await asyncio.to_thread(requests.post,f'http://127.0.0.1:{hook.server_port}/wxSend',json=literal,timeout=5)
            check('shell.literal_markers_remain_literal',response.status_code==200 and desktop.send_text.call_args.args[0]==literal['content'])
            mixed={'target':GROUP,'content':'literal [[WX_AT:wxid_other]][[WX_AT:wxid_other]]','preserve_text':True,'resolve_mentions':True,'group_id':'acceptance@chatroom',
                'native_segments':[{'type':'text','text':'literal [[WX_AT:wxid_other]]'},{'type':'mention','wxid':'wxid_other'}]}
            response=await asyncio.to_thread(requests.post,f'http://127.0.0.1:{hook.server_port}/wxSend',json=mixed,timeout=5)
            segments=desktop.send_text_with_mentions.call_args.args[0]
            check('shell.mixed_text_and_at_keep_component_semantics',response.status_code==200 and segments[0]==mixed['native_segments'][0] and sum(s['type']=='mention' for s in segments)==1)
        finally:
            await asyncio.to_thread(hook.shutdown);hook.server_close();thread.join(timeout=3)
        check('native.send_success_stored',sent is not None and bool(find_messages(session_id=sid,message_id=sent.message_id,limit=1)))
        await asyncio.sleep(.01)
        bad=SimpleNamespace(status_code=500,json=lambda:{'status':'error','message':'acceptance rejected'})
        count=rt._delivery_seq.get(sid,0)
        with patch.dict(os.environ,{'MAIBOT_INTERCEPT_SEND':'0'}),patch('core.sender._apply_send_delay'),patch('core.sender.requests.post',return_value=bad):
            failed=await send_service.text_to_stream_with_message('must-not-be-stored',sid)
        check('native.send_http_failure_not_stored',failed is None and rt._delivery_seq.get(sid,0)==count and not any(m.processed_plain_text=='must-not-be-stored' for m in find_messages(session_id=sid,limit=50)))
    rt.submit(outbound_pipeline(),timeout=45)
    # Ordered model preference and actual fallback through the native orchestrator.
    SCENARIO.update(action='kilo_fail',calls=0)
    start=len(SCENARIO['requests'])
    response=service._provider_call([{'role':'user','content':'priority fallback acceptance'}])
    used=[r['model'] for r in SCENARIO['requests'][start:]]
    check('native.kilo_priority_and_provider_fallback',bool(response.content) and used[0]==SCENARIO['primary_model'] and any(m!=SCENARIO['primary_model'] for m in used),used)
    from src.llm_models import quota_cooldown
    quota_cooldown._BLOCKED.clear()  # each isolated error scenario starts fresh
    SCENARIO.update(action='quota_fail',calls=0)
    start=len(SCENARIO['requests'])
    response=service._provider_call([{'role':'user','content':'quota exhaustion fallback acceptance'}])
    used=[r['model'] for r in SCENARIO['requests'][start:]]
    check('native.quota_exhaustion_skips_same_model_retries',bool(response.content) and used.count(SCENARIO['primary_model'])==1 and any(m!=SCENARIO['primary_model'] for m in used),used)
    start=len(SCENARIO['requests'])
    response=service._provider_call([{'role':'user','content':'quota cooldown cross task acceptance'}],task_name='memory')
    used=[r['model'] for r in SCENARIO['requests'][start:]]
    check('native.quota_cooldown_shared_across_tasks',bool(response.content) and SCENARIO['primary_model'] not in used,used)
    SCENARIO.update(action='quota_all_fail',calls=0)
    start=len(SCENARIO['requests'])
    try:service._provider_call([{'role':'user','content':'all quotas exhausted acceptance'}])
    except Exception:pass
    used=[r['model'] for r in SCENARIO['requests'][start:]]
    expected=set(SCENARIO['configured_model_ids'])-{SCENARIO['primary_model']}
    check('native.exhausted_routes_each_attempted_once',set(used)==expected and len(used)==len(expected),used)
    SCENARIO.update(action='stop',calls=0)
    start=len(SCENARIO['requests']);quota_error=False
    try:service._provider_call([{'role':'user','content':'no HTTP while all routes cooling down'}],task_name='replyer')
    except Exception as exc:quota_error=getattr(exc,'status_code',None)==429
    check('native.all_quotas_cooling_down_no_http',quota_error and len(SCENARIO['requests'])==start)
    quota_cooldown._BLOCKED.clear()  # isolated fixture only
    SCENARIO.update(action='wait',calls=0,id='wait-message')
    waiting=rt.handle_contexts([fixture_context(serverId='wait-message',force_reply=True)],wait_for_completion=True)
    check('native.wait_tool_pauses_turn',waiting.get('completed_without_reply') is True and rt._runtimes[sid]._agent_state=='wait')
    SCENARIO.update(action='stop',calls=0)
    rt.submit(asyncio.sleep(1.5))
    check('native.wait_timer_resumes_planner',SCENARIO['calls']>0)
    # Native commands must not become shell 'unknown command' replies.
    dispatcher.plugins={}; dispatcher.enabled_modules={}
    command_context=fixture_context(serverId='clear-command',content='',command='/clear',args='')
    cleared=dispatcher.dispatch('/clear','',command_context)
    check('shell.retired_clear_not_executed',cleared.get('dispositions',{}).get('clear-command')!='command',cleared)
    check('native.unauthorized_clear_rejected',sid in rt._runtimes and bool(rt._runtimes[sid]._chat_history))
    async def grant_test_command():
        from src.config.config import global_config
        from src.config.official_configs import CommandPermissionConfig
        global_config.plugin.command_permissions['core.clear']=CommandPermissionConfig(allow_users=['wechat:wxid_acceptance_user'])
    rt.submit(grant_test_command())
    dispatcher.dispatch('/clear','',fixture_context(serverId='clear-authorized',content='',command='/clear',args=''))
    check('native.retired_clear_not_restored_by_acl',sid in rt._runtimes and bool(rt._runtimes[sid]._chat_history))


def dual_adapter_native_acceptance(service):
    """Actual QQ ingress -> Maisaka -> replyer -> broker -> native QQ codec.

    Only the final OneBot action is intercepted; no production QQ login/policy.
    """
    import importlib
    import logging
    from types import ModuleType, SimpleNamespace as NS
    from src.platform_io.manager import get_platform_io_manager
    from src.platform_io.types import RouteBinding, InboundMessageEnvelope
    from src.platform_io.drivers.plugin_driver import PluginPlatformDriver
    from src.plugin_runtime.integration import PluginRuntimeManager
    pkg = ModuleType('dual_core_fixture')
    pkg.__path__ = [str(ROOT/'plugins/MaiBot-SnowLuma-Adapter')]
    sys.modules[pkg.__name__] = pkg
    def load(name): return importlib.import_module(pkg.__name__+'.'+name)
    profile = load('profile')
    plugin = load('plugin').create_plugin()
    wire = []
    async def action(name, params):
        wire.append((name, params))
        return dict(status='ok', retcode=0, data=dict(message_id=-901))
    runtime = NS(runtime_state=NS(account_id='123456', scope='dual-core-fixture'),
        outbound_codec=load('codecs.outbound').QQOutboundCodec(profile.ProfileState(profile.NAPCAT_PROFILE)),
        action_service=NS(call_action=action))
    plugin._require_runtime_bundle = lambda: runtime
    plugin._debug_logger = None
    class Supervisor:
        async def invoke_message_gateway(self, **kw):
            return NS(payload={'success': True, 'result': await plugin.handle_snowluma_gateway(**kw['args'])})
    async def exercise():
        broker = get_platform_io_manager()
        driver = PluginPlatformDriver('dual-core-qq', 'qq', Supervisor(), 'fixture_gateway', supports_send=True,
            account_id='123456', scope='dual-core-fixture', plugin_id='fixture.qq')
        await broker.add_driver(driver)
        binding = RouteBinding(driver.descriptor.route_key, driver.driver_id, driver.descriptor.kind)
        broker.bind_receive_route(binding)
        broker.bind_send_route(binding)
        # Run the same Host ingress dispatcher used by the native QQ gateway.
        async def dispatch(env):
            await PluginRuntimeManager._dispatch_platform_inbound(object.__new__(PluginRuntimeManager), env)
        broker.set_inbound_dispatcher(dispatch)
        native = dict(platform='qq', account_id='123456', scope='dual-core-fixture', message_id='-900',
            processed_plain_text='麦麦，进行QQ原生收发验收', is_at=True, is_mentioned=True,
            message_info=dict(user_info=dict(platform='qq', user_id='234567', user_nickname='QQ验收用户'),
                group_info=dict(platform='qq', group_id='345678', group_name='QQ验收群'),
                additional_config=dict(force_reply=True, at_bot=True)),
            raw_message=[dict(type='at', data=dict(target_user_id='123456')),
                         dict(type='text', data='进行QQ原生收发验收')])
        wx_before = dict(service._native_runtime._delivery_seq)
        request_start = len(SCENARIO['requests'])
        SCENARIO.update(id='-900', action='reply', calls=0, attach_at='234567')
        accepted = await broker.accept_inbound(InboundMessageEnvelope(driver.descriptor.route_key,
            driver.driver_id, driver.descriptor.kind, payload=native, external_message_id='-900'))
        for _ in range(300):
            if wire: break
            await asyncio.sleep(.1)
        SCENARIO.pop('attach_at', None)
        check('dual_core.qq_ingress_to_native_reply_to_qq_endpoint', accepted and bool(wire), wire)
        check('dual_core.qq_reply_does_not_touch_wechat', wx_before == service._native_runtime._delivery_seq)
        if wire:
            name, params = wire[0]
            check('dual_core.qq_target_and_structured_at', name=='send_group_msg' and params['group_id']=='345678'
                  and any(s['type']=='at' and s['data'].get('qq')=='234567' for s in params['message']), params)
        prompts = [json.dumps(r.get('messages', []), ensure_ascii=False) for r in SCENARIO['requests'][request_start:]]
        check('dual_core.llm_sees_qq_source_and_capability', any('platform=\\"qq\\"' in p and '来源平台=qq' in p for p in prompts))
        from src.common.database.database import get_db_session
        from src.common.database.database_model import Messages
        from sqlmodel import select
        with get_db_session() as db:
            rows = list(db.exec(select(Messages).where(Messages.message_id=='-900')).all())
            check('dual_core.qq_inbound_persisted_with_source', bool(rows) and any('qq' in str(r) and 'dual-core-fixture' in str(r) for r in rows))
    service._native_runtime.submit(exercise(), timeout=60)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true');parser.add_argument('--plugin-source');parser.add_argument('--test-enable-life',action='store_true');parser.add_argument('--test-agent-tools',action='store_true');parser.add_argument('--test-autonomous-life',action='store_true');parser.add_argument('--test-web-research',action='store_true');args=parser.parse_args()
    if args.test_web_research:
        parser.error('The duplicate plugin research tools were retired; use scripts/maibot_native_search_acceptance.py for native search acceptance.')
    instance=ROOT/'temp'/('maibot-live-acceptance-' if args.live else 'maibot-bridge-acceptance-')/datetime.now().strftime('%Y%m%d-%H%M%S')
    (instance/'config').mkdir(parents=True)
    for name in ('bot_config.toml','model_config.toml'):shutil.copy2(ROOT/'config'/name,instance/'config'/name)
    if not args.live:
        # This fixture verifies ordinary Planner/Replyer behavior. The dedicated
        # at_only_reply_acceptance suite covers the deployment's @-only pause.
        # Change only the copied instance, never the production config.
        import tomlkit
        fixture_path=instance/'config/bot_config.toml'
        fixture_config=tomlkit.parse(fixture_path.read_text(encoding='utf8'))
        fixture_config['chat']['reply_timing']['reply_only_when_at']=False
        fixture_path.write_text(tomlkit.dumps(fixture_config),encoding='utf8')
    # External management reads literal module declarations from the instance.
    for folder in ('plugins', 'auto'):
        (instance / folder).mkdir(exist_ok=True)
        for source in (ROOT / folder).glob('*.py'):
            shutil.copy2(source, instance / folder / source.name)
    (instance / 'plugins/probe.py').write_text("COMMAND = '/probe'\nWEBUI_CONFIG = {'version': 1, 'fields': {}}\ndef handle(args, context): return args\n", encoding='utf8')
    shutil.copytree(ROOT / 'plugins/owned_plugin_tools', instance / 'plugins/owned_plugin_tools', ignore=shutil.ignore_patterns('__pycache__'))
    if (ROOT/'config/bot_interaction.json').exists():shutil.copy2(ROOT/'config/bot_interaction.json',instance/'config/bot_interaction.json')
    cfg=json.loads((ROOT/'config.json').read_text(encoding='utf8'))
    if not cfg['llm'].get('apis'):
        native_doc = __import__('tomllib').loads((instance/'config/model_config.toml').read_text(encoding='utf8'))
        providers = {p['name']: p for p in native_doc['api_providers']}
        models = {m['name']: m for m in native_doc['models']}
        cfg['llm']['apis'] = []
        for index, name in enumerate(native_doc['model_task_config']['planner']['model_list']):
            model = models[name]; provider = providers[model['api_provider']]
            cfg['llm']['apis'].append(dict(name=provider['name'], model=model['model_identifier'], base_url=provider['base_url'],
                api_key=provider.get('api_key',''), enabled=provider.get('enabled',True), protocol=provider['client_type'],
                auth_mode=provider.get('auth_type','bearer'), priority=index, supports_images=model.get('visual',False)))
    else:
        cfg['llm']['model_config_source'] = 'legacy_import'  # compatibility fixture only
    enabled=sorted((a for a in cfg['llm']['apis'] if a.get('enabled',True)),key=lambda a:a.get('priority',999))
    SCENARIO['primary_model']=enabled[0]['model']
    SCENARIO['configured_model_ids']=[a['model'] for a in enabled]
    if args.plugin_source:
        source=Path(args.plugin_source).resolve()
        specs=json.loads((source/'sources.json').read_text(encoding='utf8')) if (source/'sources.json').exists() else [{'folder':p.parent.name} for p in source.glob('*/_manifest.json')]
        for spec in specs:
            shutil.copytree(source/spec['folder'],instance/'plugins'/spec['folder'],dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','.pytest_cache','config.toml'))
    if args.test_enable_life:
        assert args.plugin_source, '--test-enable-life requires --plugin-source'
        (instance/'plugins/mai-life/config.toml').write_text('[plugin]\nconfig_version = "1.3.0"\nenabled = true\n[generation]\ngenerate_on_load_if_missing = false\n',encoding='utf8')
    if args.test_agent_tools:
        assert args.plugin_source and not args.live, '--test-agent-tools requires isolated plugin sources and deterministic API'
        folder=instance/'plugins/mai-repost-shit'
        (folder/'config.toml').write_text('[plugin]\nconfig_version = "1.2.2"\nenabled = true\n[target]\nmode = "planner"\n[tool]\nmin_gap_seconds = 0\n',encoding='utf8')
        shutil.copy2(ROOT/'scripts/fixtures/maibot_repost_fixture.py',folder/'_acceptance_fixture.py')
        with (folder/'plugin.py').open('a',encoding='utf8') as f:
            f.write('\nfrom ._acceptance_fixture import install as _install_fixture\n_install_fixture(BiliClient)\n')
    REPORT['agent_tools_test_mode']=args.test_agent_tools
    REPORT['web_research_test_mode']=args.test_web_research
    if args.test_web_research:
        assert args.plugin_source and not args.live
        folder = instance/'plugins/autonomous_life'
        (folder/'config.toml').write_text('[plugin]\nenabled = true\nconfig_version = "1.0.0"\n[life]\ngroups = []\n', encoding='utf8')
        shutil.copy2(ROOT/'scripts/fixtures/maibot_research_fixture.py', folder/'_research_fixture.py')
        with (folder/'plugin.py').open('a', encoding='utf8') as f:
            f.write('\nfrom ._research_fixture import install as _install_research_fixture\n_install_research_fixture()\n')
    REPORT['autonomous_life_test_mode']=args.test_autonomous_life
    if args.test_autonomous_life:
        assert args.plugin_source and not args.live
        folder=instance/'plugins/autonomous_life'
        (folder/'config.toml').write_text('[plugin]\nconfig_version = "1.0.0"\nenabled = true\n[life]\ngroups = ["acceptance@chatroom"]\n',encoding='utf8')
        shutil.copy2(ROOT/'scripts/fixtures/maibot_autonomous_fixture.py',folder/'_acceptance_fixture.py')
        with (folder/'plugin.py').open('a',encoding='utf8') as f:
            f.write('\nfrom ._acceptance_fixture import install as _install_fixture\n_install_fixture(AutonomousLife)\n')
        if args.test_agent_tools:
            with (instance/'plugins/mai-repost-shit/config.toml').open('a',encoding='utf8') as f:
                f.write('\n[planner]\ncoordinator = true\n')
            path=instance/'plugins/mai-repost-shit/config.toml'
            text=path.read_text(encoding='utf8').replace('[target]\n','[target]\ngroups = ["acceptance@chatroom"]\n')
            path.write_text(text,encoding='utf8')
    REPORT['plugin_test_mode']=bool(args.plugin_source)
    REPORT['life_enabled_in_isolated_test_only']=args.test_enable_life
    cfg['weflow']['myWxid']='wxid_acceptance_bot'
    server=None
    if not args.live:
        server=ThreadingHTTPServer(('127.0.0.1',0),ModelFixture)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        for index,api in enumerate(cfg['llm']['apis']):
            api['base_url']=f'http://127.0.0.1:{server.server_port}/provider{index}/v1';api['api_key']='fixture';api['auth_mode']='none';api['timeout_seconds']=5
            api['request_min_interval']=0  # fixture server has no upstream anonymous quota
    (instance/'config.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf8')
    os.environ['MAIBOT_INSTANCE_DIR']=str(instance);os.environ['MAIBOT_INTERCEPT_SEND']='1';os.chdir(instance)
    os.environ['PYTHONPATH']=str(ROOT)+os.pathsep+os.environ.get('PYTHONPATH','')
    import tomlkit
    path=instance/'config'/'bot_config.toml';doc=tomlkit.parse(path.read_text(encoding='utf8'))
    doc['webui']['enabled']=False  # Never bind the production dashboard during isolated acceptance.
    if not args.live:
        doc['mcp']['enable']=False  # Deterministic fixtures never connect real search/MCP services.
    doc['a_memorix']['storage']['data_dir']=str(instance/'data'/'a-memorix')
    doc['chat']['reply_timing']['message_debounce_mode']='off'
    doc['chat']['reply_timing']['planner_message_steering']=False
    doc['response_post_process']['enable_response_post_process']=False
    doc['response_splitter']['enable']=False
    doc['chinese_typo']['enable']=False
    path.write_text(tomlkit.dumps(doc),encoding='utf8')
    # Synchronize before any native model/config imports in the unit suite.
    from llm.core.maibot_native_config import sync_native_config
    sync_native_config()
    if not args.live:
        # Independently preserved embedding providers must also stay local.
        path=instance/'config/model_config.toml'
        fixture_models=tomlkit.parse(path.read_text(encoding='utf8'))
        # Distinct fixture paths represent distinct upstream endpoints. Generic
        # 429 correctly cools all models sharing a real endpoint/key now.
        for index,provider in enumerate(fixture_models['api_providers']):
            provider['base_url']=f'http://127.0.0.1:{server.server_port}/provider{index}/v1'
            provider['api_key']='fixture';provider['auth_type']='none';provider['request_min_interval']=0
            provider['embedding_fallback_urls']=[]
        if 'web_search' in fixture_models['model_task_config']:
            fixture_models['model_task_config']['web_search']['model_list']=[]
        path.write_text(tomlkit.dumps(fixture_models),encoding='utf8')
    primary_config=__import__('tomllib').loads((instance/'config/model_config.toml').read_text(encoding='utf8'))
    primary_providers={p['name']:p for p in primary_config['api_providers']}
    primary_models={m['name']:m for m in primary_config['models']}
    SCENARIO['primary_native_model']=next(n for n in primary_config['model_task_config']['planner']['model_list']
        if primary_providers[primary_models[n]['api_provider']].get('enabled',True))
    REPORT.update(instance=str(instance),mode='live-api/intercepted-wechat' if args.live else 'deterministic-api/intercepted-wechat',date=datetime.now().isoformat())
    try:
        if not args.live:
            suite=unittest.TestSuite([unittest.defaultTestLoader.loadTestsFromTestCase(c) for c in (TranslationAcceptance,LearningAndMentionAcceptance,BotInteractionAcceptance,ProvenanceAcceptance,OutboundAcceptance)])
            result=unittest.TextTestRunner(verbosity=2).run(suite)
            check('translation_and_outbound_unit_suite',result.wasSuccessful(),f'{result.testsRun} tests; failures={len(result.failures)}; errors={len(result.errors)}')
            REPORT['unit_failures']=[{'name':str(t),'traceback':err} for t,err in result.failures+result.errors]
        from llm.core import LLMService
        service=LLMService()
        try:
            native_acceptance(service,args.live)
            if not args.live:
                dual_adapter_native_acceptance(service)
        finally:service.close()
        check('native.clean_shutdown',not service._native_runtime._thread.is_alive())
    except Exception as exc:
        import traceback
        check('acceptance.unhandled_exception',False,traceback.format_exc())
    finally:
        if server:server.shutdown()
        REPORT['limitations'].extend(['Physical WeChat UI delivery is intercepted; payload/receipt semantics are tested, not a live message to a real recipient.',
                                     'WeChat UI shell has no native quote operation: outbound ReplyComponent is dropped and reports quote_mode=dropped_unsupported.',
                                     'Disabled/unconfigured external ACP, MCP servers, ASR and network search cannot be certified as operational without their external services.'])
        REPORT['passed']=all(c['passed'] for c in REPORT['checks'])
        report_path=instance/'acceptance.json';report_path.write_text(json.dumps(REPORT,ensure_ascii=False,indent=2),encoding='utf8')
        print('ACCEPTANCE_REPORT',report_path,flush=True)
    return 0 if REPORT['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
