"""Offline patpat escalation acceptance; no real QQ actions or messages."""
import asyncio
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
pat = importlib.import_module('auto.patpat')


def check(name, value):
    print(name, bool(value), flush=True)
    assert value, name


def main():
    cfg = dict(q_punishment_probability_at_third=0.25, q_punishment_guarantee_count=10,
               q_punishment_max_seconds=3600)
    check('first_two_are_immune', pat._mute_plan(1, cfg) == (False, 0) and pat._mute_plan(2, cfg) == (False, 0))
    with patch.object(pat.random, 'random', return_value=0.99):
        check('third_is_probability_only', pat._mute_plan(3, cfg) == (False, 60))
    with patch.object(pat.random, 'random', return_value=0.0), patch.object(pat.random, 'randint', return_value=60):
        check('third_can_only_be_one_minute', pat._mute_plan(3, cfg) == (True, 60))
    with patch.object(pat.random, 'random', side_effect=[0.0, 0.99]), patch.object(pat.random, 'randint', return_value=3599):
        muted, duration = pat._mute_plan(9, cfg)
        check('unpunished_count_raises_probability_and_duration', muted and 60 < duration <= 3600)
    with patch.object(pat.random, 'randint', return_value=2400):
        muted, duration = pat._mute_plan(10, cfg)
        check('tenth_is_guaranteed_30_to_60_minutes', muted and 1800 <= duration <= 3600)

    async def fake_executor(args):
        check('mute_tool_receives_verified_poke_actor', args['user_id'] == '2103203812' and
              args['group_id'] == '1020993873' and args['duration'] == 60 and not args['msg_id'])
        return {'success': True, 'content': 'muted'}

    async def run():
        loop = asyncio.get_running_loop()
        context = dict(platform='qq', is_notify=True, account_id='3294652115', user_id='2103203812',
                       group_id='1020993873', native_session_id='qq-session', serverId='qq-message-1',
                       _native_loop=loop, qq_notice={'notice_type':'notify','sub_type':'poke',
                       'self_id':'3294652115','target_id':'3294652115','user_id':'2103203812'})
        fake_components = NS(get_tool_executor=lambda name: fake_executor if name == 'mute' else None)
        with patch('src.plugin_runtime.component_query.component_query_service', fake_components):
            result = await asyncio.to_thread(pat._mute_qq, context, 60)
        check('native_mute_result_is_returned', result.get('success') is True)
    asyncio.run(run())
    print('PATPAT PUNISHMENT ACCEPTANCE PASS', flush=True)


if __name__ == '__main__':
    main()
