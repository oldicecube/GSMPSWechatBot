"""Audit the exact live QQ group. --send-test sends one explicitly authorized text.

The single test uses native Broker -> plugin driver -> actual QQ codec -> OneBot.
It does not restart any process or replay historical ingress into the live bot.
"""
import argparse
import asyncio
from datetime import datetime
import importlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from types import ModuleType,SimpleNamespace as NS
import tomllib
from uuid import uuid4
from aiohttp import ClientSession,ClientTimeout

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
GROUP='1020993873'
NAME='GDUT SIE Minecraft Public Server'
REPORT=ROOT/'docs/maibot-wechat-acceptance/qq-live-20261030.json'
INTENT=ROOT/'temp/qq-authorized-test-once-20261030.json'
TEXT='【双端接入测试】QQ 消息接收与发送链路已接入，微信与 QQ 按来源独立路由。本条为管理员授权的验收测试。'


async def main(send_test=False):
    config=tomllib.loads((ROOT/'plugins/MaiBot-SnowLuma-Adapter/config.toml').read_text(encoding='utf8'))
    client=config['client'];report={'at':datetime.now().isoformat(),'group_id':GROUP,'group_name':NAME,'restarted':False,'sent_test':False}
    async with ClientSession(timeout=ClientTimeout(total=20)) as session:
        async with session.ws_connect(f"ws://{client['server']}:{client['port']}",headers={'Authorization':'Bearer '+client['token']}) as socket:
            async def call(action,params=None):
                echo=uuid4().hex
                await socket.send_json(dict(action=action,params=params or {},echo=echo))
                async with asyncio.timeout(18):
                    async for frame in socket:
                        data=json.loads(frame.data)
                        if data.get('echo')==echo:
                            if data.get('retcode')!=0:raise RuntimeError('OneBot '+action+' retcode='+str(data.get('retcode')))
                            return data
                raise TimeoutError(action)
            async def call_data(action,params):return (await call(action,params)).get('data') or {}
            login=await call_data('get_login_info',{})
            status=await call_data('get_status',{})
            groups=await call_data('get_group_list',{})
            matches=[g for g in groups if g.get('group_name')==NAME]
            if len(matches)!=1 or str(matches[0]['group_id'])!=GROUP:raise RuntimeError('Target group is ambiguous or changed')
            members=await call_data('get_group_member_list',{'group_id':int(GROUP)})
            own=next(m for m in members if str(m.get('user_id'))==str(login['user_id']))
            report.update(online=status.get('online'),account_id=str(login['user_id']),bot_group_role=own.get('role'),member_count=len(members),
                          bot_nickname=own.get('nickname') or login.get('nickname') or '', bot_group_cardname=own.get('card') or '')
            history=(await call_data('get_group_msg_history',{'group_id':int(GROUP),'count':5})).get('messages',[])
            report['read_history_count']=len(history)
            with sqlite3.connect('file:'+str(ROOT/'data/MaiBot.db').replace('\\','/')+'?mode=ro',uri=True) as db:
                report['native_inbound_count']=db.execute("SELECT count(*) FROM mai_messages WHERE platform='qq' AND group_id=? AND user_id<>?",(GROUP,report['account_id'])).fetchone()[0]
                report['native_outbound_count']=db.execute("SELECT count(*) FROM mai_messages WHERE platform='qq' AND group_id=? AND user_id=?",(GROUP,report['account_id'])).fetchone()[0]
                report['native_sessions']=[dict(session_id=r[0],platform=r[1],group_id=r[2],account_id=r[3],scope=r[4]) for r in db.execute("SELECT session_id,platform,group_id,account_id,scope FROM chat_sessions WHERE platform='qq' AND group_id=?",(GROUP,))]
            async with session.get('http://127.0.0.1:8001/api/webui/plugins/runtime/plugins/maibot-team.snowluma-adapter/components') as response:
                result=await response.json()
                report['component_response_keys']=list(result)
                # Save only component names and enable flags, no full configuration.
                def flags(value):
                    rows=[]
                    if isinstance(value,dict):
                        if 'enabled' in value and ('name' in value or 'component_name' in value):rows.append({k:value[k] for k in ('name','component_name','enabled') if k in value})
                        for child in value.values():rows.extend(flags(child))
                    elif isinstance(value,list):
                        for child in value:rows.extend(flags(child))
                    return rows
                report['components']=flags(result)
            if send_test:
                if INTENT.exists():raise RuntimeError('A test send has already been claimed; inspect the receipt instead of replaying')
                if not report['online'] or not report['native_sessions']:raise RuntimeError('QQ native session is not ready')
                # Load only codecs/driver types against an isolated native instance.
                instance=Path(tempfile.mkdtemp(prefix='qq-live-send-',dir=ROOT/'temp'));(instance/'config').mkdir()
                for name in ('bot_config.toml','model_config.toml'):shutil.copy2(ROOT/'config'/name,instance/'config'/name)
                os.environ['MAIBOT_INSTANCE_DIR']=str(instance)
                pkg=ModuleType('qq_live_codec');pkg.__path__=[str(ROOT/'plugins/MaiBot-SnowLuma-Adapter')];sys.modules[pkg.__name__]=pkg
                load=lambda name:importlib.import_module(pkg.__name__+'.'+name)
                profile=load('profile')
                codec=load('codecs.outbound').QQOutboundCodec(profile.ProfileState(profile.NAPCAT_PROFILE))
                plugin=load('plugin').create_plugin()
                scope=report['native_sessions'][0]['scope']
                plugin._require_runtime_bundle=lambda:NS(runtime_state=NS(account_id=report['account_id'],scope=scope),outbound_codec=codec,action_service=NS(call_action=call))
                plugin._debug_logger=None
                class Supervisor:
                    async def invoke_message_gateway(self,**kwargs):
                        return NS(payload={'success':True,'result':await plugin.handle_snowluma_gateway(**kwargs['args'])})
                from src.platform_io.drivers.plugin_driver import PluginPlatformDriver
                from src.platform_io.manager import PlatformIOManager
                from src.platform_io.types import RouteBinding
                from src.plugin_runtime.host.message_utils import PluginMessageUtils as U
                driver=PluginPlatformDriver('gateway:maibot-team.snowluma-adapter:snowluma_gateway','qq',Supervisor(),'snowluma_gateway',supports_send=True,
                    account_id=report['account_id'],scope=scope,plugin_id='maibot-team.snowluma-adapter')
                broker=PlatformIOManager();await broker.add_driver(driver)
                route=driver.descriptor.route_key
                broker.bind_send_route(RouteBinding(route,driver.driver_id,driver.descriptor.kind))
                message=U._build_session_message_from_dict(dict(platform='qq',account_id=report['account_id'],scope=scope,message_id=uuid4().hex,
                    message_info={'user_info':{'user_id':report['account_id'],'user_nickname':'LLM','platform':'qq'},
                                  'group_info':{'group_id':GROUP,'group_name':NAME,'platform':'qq'}},raw_message=[{'type':'text','data':TEXT}]))
                with INTENT.open('x',encoding='utf8') as intent:
                    json.dump({'status':'pending','at':report['at'],'group_id':GROUP},intent)
                batch=await broker.send_message(message,route)
                report['send_receipts']=[{'status':str(r.status),'message_id':r.external_message_id,'metadata':r.metadata,'error':r.error} for r in batch.receipts]
                if not batch.has_success:raise RuntimeError('Native QQ delivery failed; do not retry automatically')
                receipt=batch.sent_receipts[0]
                mid=receipt.external_message_id
                actual=await call_data('get_msg',{'message_id':int(mid)})
                segments=actual.get('message') or []
                text=''.join(str(s.get('data',{}).get('text','')) for s in segments if s.get('type')=='text') if isinstance(segments,list) else str(segments)
                report.update(sent_test=True,test_message_id=str(mid),test_message_exact_match=text==TEXT,
                              test_group_exact_match=str(actual.get('group_id') or GROUP)==GROUP,send_path='native Broker -> PluginPlatformDriver -> actual QQ codec -> live OneBot')
                INTENT.write_text(json.dumps({'status':'sent','message_id':str(mid),'exact_match':text==TEXT,'at':report['at']}),encoding='utf8')
    report['passed']=bool(report.get('online') and report.get('native_inbound_count') and report.get('native_outbound_count')
                          and (not send_test or report.get('test_message_exact_match')))
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--send-test',action='store_true');args=parser.parse_args()
    raise SystemExit(0 if asyncio.run(main(args.send_test)) else 1)
