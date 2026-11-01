"""Read-only production audit; identity prompt checks run on SQLite snapshots.

No model calls, group messages, punishments, account changes or process restart.
"""
from datetime import datetime
import asyncio
import importlib
import logging
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import tomllib
from types import ModuleType, SimpleNamespace as NS

import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
INSTANCE=Path(tempfile.mkdtemp(prefix='community-ro-audit-',dir=ROOT/'temp'))
os.environ['MAIBOT_INSTANCE_DIR']=str(INSTANCE)
(INSTANCE/'config').mkdir()
(INSTANCE/'data').mkdir()
for name in ('bot_config.toml','model_config.toml'):
    shutil.copy2(ROOT/'config'/name,INSTANCE/'config'/name)


def ro(path):
    return sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)


def main():
    now=datetime.now()
    report=dict(at=now.isoformat(),production_read_only=True,physical_messages=False,
        model_calls=False,main_process_restarted=False,checks=[])
    def check(name,passed,**detail):
        report['checks'].append(dict(name=name,passed=bool(passed),**detail))
        print(name,bool(passed),flush=True)
    for name in ('bot.sqlite3','MaiBot.db'):
        with ro(ROOT/'data'/name) as source,sqlite3.connect(INSTANCE/'data'/name) as target:
            source.backup(target)
    # Current code, isolated copy of the real binding data and native people.
    from utils import account_identity as identity
    from src.maisaka.context.planner_messages import build_planner_user_prefix_from_session_message
    from src.plugin_runtime.host.message_utils import PluginMessageUtils
    with ro(INSTANCE/'data/bot.sqlite3') as db:
        accounts=db.execute('SELECT a.id,a.player_name,a.canonical_uid FROM linked_accounts a '
            'JOIN account_links l ON l.account_id=a.id GROUP BY a.id HAVING count(distinct l.platform)>1').fetchall()
        report['linked_account_count']=len(accounts)
        checks=[]
        with ro(INSTANCE/'data/MaiBot.db') as native:
            for account,player,canonical in accounts:
                members=[row[0] for row in db.execute('SELECT uid FROM account_links WHERE account_id=?',(account,))]
                for uid in members:
                    platform,user_id=identity.platform_user(uid)
                    person_id=hashlib.md5((platform+'_'+user_id).encode()).hexdigest()
                    people=native.execute('SELECT person_id FROM person_info WHERE platform=? AND user_id=?',(platform,user_id)).fetchall()
                    message=PluginMessageUtils._build_session_message_from_dict(dict(platform=platform,
                        account_id='3294652115' if platform=='qq' else 'wxid_audit_bot',scope='napcat-local' if platform=='qq' else '',
                        message_id='audit-local-only',message_info=dict(user_info=dict(user_id=user_id,user_nickname='改名测试显示名'),
                            group_info=dict(group_id='1020993873' if platform=='qq' else '47447788043@chatroom',group_name='GDUT SIE Minecraft Public Server')),
                        raw_message=[dict(type='text',data='本地只读身份检查')]))
                    prefix=build_planner_user_prefix_from_session_message(message)
                    checks.append(dict(platform=platform,user_id=user_id,canonical=identity.canonical_user(uid),
                        native_person_present=bool(people),native_person_id_valid=all(row[0]==person_id for row in people),
                        no_duplicate_native_person=len(people)<=1,same_person_marker='这些账号属于同一人' in prefix,
                        source_boundary_marker='当前平台会话仍独立' in prefix,
                        renamed_display_preserved='改名测试显示名' in prefix,
                        aliases=len(identity.memory_aliases(platform,user_id))))
        report['linked_people']=checks
        check('production_binding_same_person_prompt_and_rename',bool(checks) and all(
            row['same_person_marker'] and row['source_boundary_marker'] and row['renamed_display_preserved']
            and row['native_person_id_valid'] and row['no_duplicate_native_person'] and row['aliases']>=2 for row in checks))
    with ro(ROOT/'data/MaiBot.db') as db:
        native_people=db.execute('SELECT platform,user_id,person_id FROM person_info').fetchall()
        check('all_native_person_ids_depend_on_platform_and_stable_uid',all(
            hashlib.md5((p+'_'+u).encode()).hexdigest()==pid for p,u,pid in native_people),records=len(native_people))
        sessions=db.execute("SELECT session_id,platform,account_id,scope,group_id,group_name FROM chat_sessions WHERE "
            "(platform='wechat' AND group_id='47447788043@chatroom') OR (platform='qq' AND group_id='1020993873')").fetchall()
        report['paired_sessions']=[dict(zip(['session_id','platform','account_id','scope','group_id','group_name'],r)) for r in sessions]
        wx=[r for r in sessions if r[1]=='wechat'];qq=[r for r in sessions if r[1]=='qq' and r[2]=='3294652115' and r[3]=='napcat-local']
        check('paired_groups_have_distinct_real_native_sessions',len(wx)==len(qq)==1 and wx[0][0]!=qq[0][0] and wx[0][5]==qq[0][5])
        report['latest_prompt_markers']={}
        for platform,folder in [('wechat','wechat_group_47447788043_chatroom'),('qq','qq_group_1020993873')]:
            files=sorted((ROOT/'logs/maisaka_prompt/planner'/folder).glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:3]
            data=[json.dumps(json.loads(p.read_text(encoding='utf8')),ensure_ascii=False) for p in files]
            report['latest_prompt_markers'][platform]=dict(examined=len(files),
                peer_reference=any('隔壁同名群的有来源参考' in s for s in data),
                linked_identity_hint=any('这些账号属于同一人' in s for s in data))
    async def actual_peer_capabilities():
        from src.chat.message_receive.chat_manager import chat_manager
        from src.plugin_runtime.capabilities.data import RuntimeDataCapabilityMixin
        await chat_manager.load_all_sessions_from_db()
        host=RuntimeDataCapabilityMixin()
        name='runtime_audit_peer'
        package=ModuleType(name);package.__path__=[str(ROOT/'plugins/community_context')];sys.modules[name]=package
        module=importlib.import_module(name+'.plugin')
        plugin=module.create_plugin()
        cfg=tomllib.loads((ROOT/'plugins/community_context/config.toml').read_text(encoding='utf8'))
        cfg['bridge']['automatic_memory']=False  # Read-only history audit never invokes embedding/network.
        plugin.set_plugin_config(cfg)
        async def all_streams(platform):
            return await host._cap_chat_get_all_streams('audit','chat.get_all_streams',{'platform':platform})
        async def capability(capability,**kwargs):
            methods={'frequency.get_current_talk_value':host._cap_frequency_get_current_talk_value,
                     'message.get_recent':host._cap_message_get_recent}
            return await methods[capability]('audit',capability,kwargs)
        plugin._set_context(NS(chat=NS(get_all_streams=all_streams),call_capability=capability,
            logger=logging.getLogger('runtime-audit-peer')))
        rows=[]
        for current in report['paired_sessions']:
            reference=await plugin.guarded_context(current['session_id'])
            rows.append(dict(platform=current['platform'],has_actual_source_reference=bool(reference),
                correct_other_platform=bool(reference) and ('"source_platform": "'+('qq' if current['platform']=='wechat' else 'wechat')+'"') in reference,
                reference_characters=len(reference or '')))
        report['actual_native_peer_capability_checks']=rows
        check('paired_context_from_real_native_history_capabilities',len(rows)==2 and all(
            row['has_actual_source_reference'] and row['correct_other_platform'] for row in rows))
    asyncio.run(actual_peer_capabilities())
    response=requests.get('http://127.0.0.1:8001/api/webui/plugins/installed',timeout=(3,10))
    response.raise_for_status()
    ids={'wechatbot.autonomous-life','wechatbot.community-context','xxxxx7258.idle-proactive-chat','mailife.mai-life','github.erhan111119.mai-repost-shit'}
    plugins=[{k:p.get(k) for k in ('id','loaded','load_status','load_error')} for p in response.json().get('plugins',[]) if p.get('id') in ids]
    report['live_plugins']=plugins
    check('live_plugins_loaded',len(plugins)==len(ids) and all(p['loaded'] for p in plugins))
    shell=json.loads((ROOT/'config.json').read_text(encoding='utf8'))
    groups=shell.get('group_features',{})
    sleep=groups.get('47799335844@chatroom',{})
    check('anime_group_enables_sleep_only',sleep.get('auto_plugins_enabled') and 'sleep' in sleep.get('plugin_allowlist',[]) and not sleep.get('llm_enabled'),stable_group_id='47799335844@chatroom')
    cfg=tomllib.loads((ROOT/'plugins/autonomous_life/config.toml').read_text(encoding='utf8'))['life']
    clock=now.strftime('%H:%M')
    report['autonomy_awake_now']=cfg['wake']<=clock<cfg['sleep'] if cfg['wake']<cfg['sleep'] else clock>=cfg['wake'] or clock<cfg['sleep']
    with ro(ROOT/'data/plugins/wechatbot.autonomous-life/life.sqlite3') as db:
        report['autonomy_event_counts']=[dict(day=d,kind=k,state=s,count=c) for d,k,s,c in db.execute(
            'SELECT day,kind,state,count(*) FROM events GROUP BY day,kind,state ORDER BY day DESC')]
        report['autonomy_share_counts']=[dict(day=d,state=s,count=c) for d,s,c in db.execute('SELECT day,state,count(*) FROM shares GROUP BY day,state')]
        report['actual_action_counts']={}
        for day,kind,data in db.execute("SELECT day,kind,data FROM events WHERE kind IN ('action','thought')"):
            payload=json.loads(data);action=payload.get('action') or (payload.get('decision') or {}).get('action') or 'failed_or_unknown'
            key=day+':'+action
            report['actual_action_counts'][key]=report['actual_action_counts'].get(key,0)+1
        report['autonomy_meta']={key:json.loads(value) for key,value in db.execute('SELECT key,value FROM meta')
            if key in {'next_check','backoff_until','failures','group_index'}}
        report['bootstrap_consumed_markers']=db.execute("SELECT count(*) FROM meta WHERE key LIKE 'bootstrap_consumed:%'").fetchone()[0]
        report['project_status_counts']=[dict(state=s,count=c) for s,c in db.execute('SELECT status,count(*) FROM projects GROUP BY status')]
        check('initial_guide_stays_consumed',report['bootstrap_consumed_markers']>0)
    adapter=tomllib.loads((ROOT/'plugins/MaiBot-SnowLuma-Adapter/config.toml').read_text(encoding='utf8'))
    report['qq_management_config']={**adapter['mute'],
        'mute_enabled':adapter['chat_abilities']['enable_mute_tool'],
        'recall_enabled':adapter['chat_abilities']['enable_recall_tool']}
    report['reference_freshness_seconds']=tomllib.loads(
        (ROOT/'plugins/community_context/config.toml').read_text(encoding='utf8'))['bridge']['refresh_seconds']
    report['passed']=all(c['passed'] for c in report['checks'])
    report['scope_note']='Production data are read-only. Prompt construction uses current code and copied production databases, not the old in-memory core.'
    destination=ROOT/'docs/maibot-wechat-acceptance/community-runtime-reaudit-20261031.json'
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    print('REPORT',destination,report['passed'],flush=True)
    return report['passed']


if __name__=='__main__':raise SystemExit(0 if main() else 1)
