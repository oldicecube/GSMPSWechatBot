"""Isolated activity-to-planner acceptance, never connects to production or sends messages."""
from __future__ import annotations

import asyncio
import copy
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

ROOT = Path(__file__).resolve().parents[1]
GROUP = '47447788043@chatroom'
ROWS = []


def check(name, passed):
    ROWS.append(dict(name=name, passed=bool(passed)))
    print(name, bool(passed), flush=True)


def message(index, stamp, text='普通聊天', user=None):
    return dict(message_id='synthetic-'+str(index), timestamp=str(stamp), platform='wechat',
        message_info=dict(user_info=dict(user_id=user or 'human-'+str(index % 2)),
                          group_info=dict(group_id=GROUP), additional_config={}),
        raw_message=[dict(type='text', data=text)], processed_plain_text=text)


async def run(isolated):
    pkg = ModuleType('repost_planner_acceptance')
    pkg.__path__ = [str(ROOT/'plugins/mai-repost-shit')]
    sys.modules[pkg.__name__] = pkg
    mod = importlib.import_module(pkg.__name__+'.plugin')
    activity = importlib.import_module(pkg.__name__+'.core.activity')
    state_mod = importlib.import_module(pkg.__name__+'.core.state')
    plugin = mod.create_plugin()
    plugin.set_plugin_config(tomllib.loads((ROOT/'plugins/mai-repost-shit/config.toml').read_text(encoding='utf8')))
    check('production_coordinator_enabled', plugin.config.planner.coordinator)
    # This fixture has only one WeChat stream/history. Cross-platform and empty
    # QQ windows are covered by repost_dual_delivery_acceptance.py separately.
    plugin.config.target.groups = [GROUP]
    # Exercise the supported native Planner fallback mode in isolation.
    plugin.config.planner.coordinator = False
    plugin.config.target.active_hours = [0, 24]
    store = state_mod.StateStore(isolated/'state.json', logging.getLogger('test'))
    plugin._store = store
    plugin._pipeline = NS(_resolve_stream=AsyncMock(return_value=('actual-session', '群')))
    plugin._run_and_report = AsyncMock()
    trigger = AsyncMock(return_value={'queued': True})
    query = AsyncMock()
    ctx = NS(logger=logging.getLogger('test'), call_capability=query,
             maisaka=NS(proactive=NS(trigger=trigger)))
    plugin._set_context(ctx)
    now = datetime.now().timestamp()
    messages = [message(i, now-8+i, 'https://b23.tv/Synthetic'+str(i) if i < 2 else '普通聊天') for i in range(6)]
    query.return_value = {'messages': messages}
    await plugin._tick()
    check('active_bili_sharing_wakes_planner_once', trigger.await_count == 1)
    check('planner_tick_never_searches_or_sends', plugin._run_and_report.await_count == 0)
    args = query.call_args
    check('query_catches_shell_consumed_history_without_binary', args.args == ('message.get_recent',) and
          args.kwargs['chat_id'] == 'actual-session' and args.kwargs['filter_mai'] and not args.kwargs['include_binary_data'])
    check('intent_keeps_choice_and_actual_target', trigger.call_args.args[0] == 'actual-session' and
          '保持安静' in trigger.call_args.args[1] and 'repost_shi' in trigger.call_args.args[1])
    await plugin._tick()
    check('same_batch_and_cooldown_do_not_requeue', trigger.await_count == 1)
    loaded = state_mod.StateStore(isolated/'state.json', logging.getLogger('test')); loaded.load()
    plugin._store = loaded
    await plugin._tick()
    check('reload_preserves_intent_watermark', trigger.await_count == 1)
    # Even when the cooldown elapsed, the same old shares cannot generate another intent.
    loaded.record_planner('actual-session', now-1000, now-7)
    await plugin._tick()
    check('old_shares_not_replayed_after_cooldown', trigger.await_count == 1)
    messages[0]['timestamp'] = str(now)
    await plugin._tick()
    check('fresh_sharing_can_offer_a_new_decision', trigger.await_count == 2)
    cfg = plugin.config.planner
    summary = lambda items: activity.summarize(items, now=now, window=600, group_id=GROUP, ignored_users=cfg.ignored_user_ids)
    check('duplicate_deliveries_count_once', summary(messages+messages)['messages'] == 6)
    check('all_text_chatter_is_not_sharing', not activity.eligible(summary([message(i,now) for i in range(40)]), cfg, now))
    single_user = [message(i,now,'https://b23.tv/test',user='one') for i in range(20)]
    check('one_sender_cannot_fake_group_density', not activity.eligible(summary(single_user), cfg, now))
    bots = [message(i,now,'https://b23.tv/test',user='wxid_j4m6tul1dm7822') for i in range(20)]
    check('known_other_bot_does_not_raise_density', summary(bots)['messages'] == 0)
    marked = message(900,now,'https://b23.tv/bot')
    marked['message_info']['additional_config']['bot_interaction'] = {'sender_is_bot':True}
    check('host_bot_identity_flag_respected', summary([marked])['messages'] == 0)
    wrong = copy.deepcopy(messages)
    for m in wrong: m['message_info']['group_info']['group_id'] = 'excluded@chatroom'
    check('cross_group_history_ignored', summary(wrong)['messages'] == 0)
    old = [message(i,now-601,'https://b23.tv/test') for i in range(20)]
    check('stale_sharing_ignored', summary(old)['messages'] == 0)
    quoted = message(80,now)
    quoted['raw_message'] = [dict(type='reply',data={'target_message_content':'https://b23.tv/old'}),dict(type='text',data='收到')]
    quoted['processed_plain_text'] = '引用 https://b23.tv/old 收到'
    check('quoted_link_not_counted_as_fresh_share', summary([quoted])['shares'] == 0)
    quoted['raw_message'] = [dict(type='text',data='[引用预览：群友：https://b23.tv/old]'), dict(type='text',data='收到')]
    check('unresolved_quote_preview_not_counted', summary([quoted])['shares'] == 0)
    file = message(81,now)
    file['raw_message'] = [dict(type='file',data={'name':'synthetic.mp4','url':'not-opened'})]
    check('native_video_file_is_a_share', summary([file])['shares'] == 1)
    file['raw_message'] = [dict(type='image',data='synthetic image description, no binary')]
    check('image_shares_are_offered_for_planner_judgment', summary([file])['shares'] == 1)
    hot = [message(i,now,'https://b23.tv/test' if i < 4 else '哈哈') for i in range(8)]
    plugin._activity['actual-session'] = summary(hot)
    check('busy_sharing_shortens_tool_cooldown', plugin._tool_gap('actual-session') == 180)
    plugin._activity['actual-session']['newest_share'] = now-121
    check('faded_activity_restores_normal_cooldown', plugin._tool_gap('actual-session') == 600)
    # Timeout may already have queued a decision: reserve the watermark before RPC and avoid duplicates.
    messages[0]['timestamp'] = str(now+1)
    loaded.record_planner('actual-session', now-1000, now-10)
    trigger.side_effect = TimeoutError('synthetic timeout')
    await plugin._tick(); count = trigger.await_count
    await plugin._tick()
    check('uncertain_rpc_not_repeated', trigger.await_count == count)
    plugin.config.target.active_hours = [(datetime.now().hour+1) % 24, datetime.now().hour]
    await plugin._tick()
    check('outside_active_hours_no_decision', trigger.await_count == count)
    plugin.config.target.active_hours = [0,23]
    plugin.config.target.mode = 'scheduled'
    store = state_mod.StateStore(isolated/'scheduled.json', logging.getLogger('test')); plugin._store = store
    store.ensure_plan(datetime.now(), 6, 0, 23)
    store._plan_times = [datetime.now().timestamp()-1]
    await plugin._tick()
    check('legacy_scheduled_mode_explicitly_available', plugin._run_and_report.await_count == 1)
    midnight_store = state_mod.StateStore(isolated/'midnight-end.json', logging.getLogger('test'))
    midnight_store.ensure_plan(datetime.now(), 6, 9, 24)
    midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    check('24_hour_end_is_valid_and_stays_in_day',len(midnight_store._plan_times)==6 and
          all(midnight+9*3600 <= t < midnight+24*3600 for t in midnight_store._plan_times))
    late = datetime.now().replace(hour=23, minute=15, second=0, microsecond=0)
    plugin.config.target.mode = 'planner'
    plugin.config.target.active_hours = [9,24]
    plugin._store = state_mod.StateStore(isolated/'late-planner.json', logging.getLogger('test'))
    query.return_value = {'messages':[message(i+1000,late.timestamp()-8+i,
        'https://b23.tv/LateSynthetic'+str(i) if i<2 else '普通聊天') for i in range(6)]}
    trigger.side_effect=None
    previous=trigger.await_count
    with patch.object(mod,'datetime',NS(now=lambda:late)):
        await plugin._tick()
    check('late_evening_sharing_can_offer_planner_decision',trigger.await_count==previous+1)
    manifest = json.loads((ROOT/'plugins/mai-repost-shit/_manifest.json').read_text(encoding='utf8'))
    check('proactive_capability_declared', 'maisaka.proactive.trigger' in manifest['capabilities'])


def main():
    sys.path.insert(0,str(ROOT))
    logging.basicConfig(stream=sys.stdout,level=logging.WARNING)
    with tempfile.TemporaryDirectory(prefix='repost-planner-') as temp:
        isolated = Path(temp)
        (isolated/'config').mkdir()
        for name in ['bot_config.toml','model_config.toml']:
            shutil.copy2(ROOT/'config'/name, isolated/'config'/name)
        os.environ['MAIBOT_INSTANCE_DIR'] = temp
        os.environ['MAIBOT_INTERCEPT_SEND'] = '1'
        asyncio.run(run(isolated))
    result = dict(passed=all(r['passed'] for r in ROWS), checks=ROWS,
                  delivery='synthetic messages, mocked planner and media delivery; production state untouched')
    (ROOT/'docs/maibot-wechat-acceptance/repost-planner-20261029.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
