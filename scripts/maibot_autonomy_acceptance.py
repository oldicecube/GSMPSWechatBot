"""Validate installed proactive configs and actual plugin routing; all sends mocked."""
from __future__ import annotations
import asyncio
from datetime import datetime
import importlib
import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
from types import ModuleType, SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
ROWS=[]


def check(name,passed,detail=None):
    ROWS.append(dict(name=name,passed=bool(passed),detail=detail))
    print('AUTONOMY',name,bool(passed),detail,flush=True)


def load(folder):
    name='autonomy_accept_'+folder.replace('-','_')
    package=ModuleType(name);package.__path__=[str(ROOT/'plugins'/folder)];sys.modules[name]=package
    return importlib.import_module(name+'.plugin')


async def run(isolated):
    from maibot_autonomy_config import FOLDERS,selected_groups
    config=json.loads((ROOT/'config.json').read_text(encoding='utf8'))
    applied=json.loads((ROOT/'docs/maibot-wechat-acceptance/autonomy-config-20261029.json').read_text(encoding='utf8'))
    groups=applied['groups'];ids=[g['group_id'] for g in groups]
    typed=['wechat:group:'+i for i in ids]
    streams=[dict(platform='wechat',group_id=i,session_id='test-session-'+str(n),chat_type='group') for n,i in enumerate(ids)]
    streams.append(dict(platform='wechat',group_id='excluded@chatroom',session_id='excluded-session',chat_type='group'))
    modules={};plugins={}
    for folder in FOLDERS:
        mod=load(folder);plugin=mod.create_plugin()
        plugin.set_plugin_config(tomllib.loads((ROOT/'plugins'/folder/'config.toml').read_text(encoding='utf8')))
        modules[folder]=mod;plugins[folder]=plugin
        check(folder+'.schema_enable_policy',plugin.config.plugin.enabled == (folder not in {'mai-life','idle_proactive_chat'}))
    repost=plugins['mai-repost-shit'].config
    life=plugins['mai-life'];hot=plugins['hot-topics'];idle=plugins['idle_proactive_chat']
    check('targets.repost',list(repost.target.groups)==ids)
    check('targets.life_schedule_and_shares',list(life.config.schedule.allowed_streams)==typed and
          [p.stream for p in life.config.share.stream_profiles]==typed)
    check('targets.hot_no_private_or_global',list(hot.config.ambient.group_ids)==ids and
          not hot.config.ambient.proactive_groups and not hot.config.ambient.apply_to_all and not hot.config.ambient.private_ids)
    check('targets.idle',list(idle.config.targets.target_chats)==typed)
    check('personality.uninstalled_source',not (ROOT/'plugins/cateye_mai_personality_swap/_manifest.json').exists()
          and not (ROOT/'plugins/cateye_mai_personality_swap/plugin.py').exists())

    # Disabled profiles do not require resolving an unseen group; old name-based rows cannot create duplicate targets.
    sample={'target_group':['on','off'], 'group_features':{'default':{'llm_enabled':True}},
            'llm':{'auto_reply':{'enabled':True},'group_profiles':{'off':{'auto_reply':{'enabled':False}}}}}
    resolved=selected_groups(sample,[dict(platform='wechat',group_id='on',group_name=None),
        dict(platform='wechat',group_id='on@chatroom',group_name='on')])
    check('selection.excludes_disabled_and_legacy_name_ids',resolved==[{'name':'on','group_id':'on@chatroom'}])
    sample['llm']['enabled']=False
    check('selection.global_llm_disabled',not selected_groups(sample,[]))

    async def resolve(gid,*,platform):
        assert platform=='wechat'
        return next(s for s in streams if s['group_id']==gid)
    ctx=NS(chat=NS(get_stream_by_group_id=AsyncMock(side_effect=resolve),get_all_streams=AsyncMock(return_value=streams)),
        logger=logging.getLogger('autonomy_acceptance'),message=NS(get_by_time_in_chat=AsyncMock(return_value=[])),
        maisaka=NS(context=NS(append=AsyncMock()),proactive=NS(trigger=AsyncMock())))
    hot._set_context(ctx)
    targets,_=await hot._resolve_target_streams()
    expected=[s['session_id'] for s in streams[:-1]]
    check('hot.real_config_resolves_only_allowed',targets==expected,targets)
    with patch.object(modules['hot-topics'].random,'random',return_value=0):
        await hot._deliver_ambient([{'title':'Minecraft synthetic test update'}],'test')
    check('hot.context_and_intent_routed_correctly',
        [c.args[0] for c in ctx.maisaka.context.append.await_args_list]==expected and
        not ctx.maisaka.proactive.trigger.await_count)

    life._set_context(ctx);life._cached_streams=AsyncMock(return_value=streams)
    life._now=lambda:datetime.now().astimezone()
    await life._resolve_history()
    check('life.history_resolves_real_session_ids',
        [c.args[0] for c in ctx.message.get_by_time_in_chat.await_args_list]==expected)
    stream_module=importlib.import_module(modules['mai-life'].__package__+'.streams')
    check('life.excluded_group_not_injected',
        [s['session_id'] for s in stream_module.filter_streams(streams,life.config.schedule.allowed_streams)]==expected)
    check('idle.typed_target_parser',all(modules['idle_proactive_chat'].parse_target_chat(t).target_id==i for t,i in zip(typed,ids)))
    check('frequency.repost_limits',repost.target.mode=='planner' and repost.tool.max_count==1 and
          repost.tool.min_gap_seconds==600 and repost.pretty.min_gap_seconds==600)
    check('frequency.single_coordinator_no_timer_conflicts', not life.config.plugin.enabled and
          not life.config.share.enabled and not hot.config.ambient.enabled and hot.config.ambient.proactive_chance==0
          and not idle.config.plugin.enabled and repost.planner.coordinator)
    # External read-only sources: no media download or message delivery.
    core=importlib.import_module(modules['hot-topics'].__package__+'.hot_core')
    with patch.object(core,'_http_get_json',side_effect=[{'code':500,'status':200,'data':[{'title':'must reject'}]},
            {'status':200,'data':[{'title':'accepted fallback','url':'https://example.com'}]}]):
        items=core.fetch_platform('bili',base_urls=['https://first.invalid','https://second.invalid'],use_cache=False)
        check('hot.source_status_envelope_and_error_fallback',[r['title'] for r in items]==['accepted fallback'])
    with patch.object(core,'_http_get_json',return_value={'code':200,'data':[]}):
        try:
            core.fetch_platform('bili',base_urls=['https://empty.invalid'],use_cache=False)
            check('hot.source_empty_success_rejected',False)
        except RuntimeError:
            check('hot.source_empty_success_rejected',True)
    for key in hot.config.ambient.pool_platforms:
        try:
            items=await asyncio.to_thread(core.fetch_platform,key,base_urls=list(hot.config.sources.api_base_urls),
                timeout=hot.config.sources.request_timeout,use_cache=False)
            check('source.'+key,bool(items),{'items':len(items)})
        except Exception as exc:
            check('source.'+key,False,str(exc)[:250])


def main():
    with tempfile.TemporaryDirectory(prefix='maibot-autonomy-') as isolated:
        cfg=Path(isolated)/'config';cfg.mkdir()
        for name in ['bot_config.toml','model_config.toml']:shutil.copy2(ROOT/'config'/name,cfg/name)
        os.environ['MAIBOT_INSTANCE_DIR']=isolated
        os.environ['MAIBOT_INTERCEPT_SEND']='1'
        try:asyncio.run(run(Path(isolated)))
        except Exception:
            import traceback
            check('acceptance.exception',False,traceback.format_exc())
    result={'passed':all(r['passed'] for r in ROWS),'checks':ROWS,'delivery':'mocked, no external messages sent'}
    (ROOT/'docs/maibot-wechat-acceptance/autonomy-routing-20261029.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
