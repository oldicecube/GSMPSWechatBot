"""Prepare or apply plugin targets from current WeChat proactive-reply settings.

No process control, plugin reload RPC, or message sending. Re-run after changing
group switches to refresh the explicit plugin allowlists; not a background sync.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import tomlkit

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ['mai-repost-shit', 'mai-life', 'hot-topics', 'idle_proactive_chat', 'autonomous_life']


def selected_groups(config, sessions):
    llm = config.get('llm', {})
    if not llm.get('enabled', True):
        return []
    targets = config.get('target_group', [])
    targets = [targets] if isinstance(targets, str) else targets
    result = []
    features = config.get('group_features', {})
    for target in targets:
        flags = features.get(target, features.get('default', {}))
        if not flags.get('llm_enabled', flags.get('llm', True)):
            continue
        profiles = llm.get('group_profiles', {})
        profile = profiles.get(target, profiles.get('default', {}))
        auto = {**llm.get('auto_reply', {}), **profile.get('auto_reply', {})}
        if not profile.get('enabled', True) or not auto.get('enabled', True):
            continue
        rows = [r for r in sessions if r['platform'] == 'wechat' and
                str(r['group_id'] or '').endswith('@chatroom') and
                target in {r['group_id'], r['group_name']}]
        ids = {r['group_id'] for r in rows}
        if len(ids) != 1:
            raise ValueError(f'群标识无法唯一解析为微信 ID: {target!r}')
        group_id = ids.pop()
        allowed = auto.get('allowed_groups') or []
        if allowed and target not in allowed and group_id not in allowed:
            continue
        if group_id not in {r['group_id'] for r in result}:
            result.append({'name': target, 'group_id': group_id})
    return result


def prepare(config, groups):
    docs = {f: tomlkit.parse((ROOT / 'plugins' / f / 'config.toml').read_text(encoding='utf8')) for f in FOLDERS}
    ids = [g['group_id'] for g in groups]
    typed = ['wechat:group:' + i for i in ids]
    repost = docs['mai-repost-shit']
    repost['plugin']['enabled'] = True
    repost['target'].update(groups=ids, mode='planner', daily_times=6, active_hours=[10, 22], catch_up_minutes=10)
    if 'planner' not in repost:
        repost['planner'] = {}
    repost['planner'].update(window_seconds=600, min_messages=6, min_users=2, min_shares=2,
        fresh_seconds=120, hot_messages=20, hot_shares=4, normal_gap_seconds=600, hot_gap_seconds=180)
    repost['download'].update(max_duration_seconds=120, max_size_mb=8, inline_video_max_mb=8)
    repost['tool'].update(min_gap_seconds=600, max_count=1)
    repost['pretty']['min_gap_seconds'] = 600
    repost['retry'].update(rounds=2, gap_seconds=30)

    life = docs['mai-life']
    life['plugin']['enabled'] = True
    # Empty means discover actual session IDs through schedule.allowed_streams.
    # Typed group targets are valid allowlist entries, not message-history session IDs.
    life['generation'].update(time='08:20', timezone='Asia/Shanghai', history_stream_ids=[])
    life['schedule'].update(allowed_streams=typed, stream_discovery_platform='wechat',
        activity_count_min=8, activity_count_max=12, wake_time='08:30', sleep_time='00:00')
    life['share'].update(cooldown_seconds=600, silence_start='00:00', silence_end='08:30')
    life['share']['stream_profiles'] = [dict(stream=target, count_min=2, count_max=3,
        extra_prompt='围绕服务器群的人设与近期聊天，分享游戏、维护服务器、吃饭休息等自然小事；不要编造真实服务器操作、群友经历或已核实的新闻，不与搬运视频、热榜分享重复；一次只聊一件事，群里正在认真讨论时可以不插话。') for target in typed]

    hot = docs['hot-topics']
    hot['plugin']['enabled'] = True
    hot['filter'].update(interests=['Minecraft', '我的世界', '游戏', '服务器', '开源', '科技', '整活', '鬼畜', '二次元'], max_items=8)
    hot['ambient'].update(enabled=True, mode='random', interval_minutes=180, sample_count=2,
        include_watch=False, group_ids=ids, private_ids=[], apply_to_all=False,
        proactive_chance=25, proactive_priority='low', proactive_target_count=1, proactive_groups=ids,
        style_hint='结合服务器群的人设和当下聊天，优先游戏、Minecraft、技术与轻松整活内容；只在08:30至23:00且气氛合适时考虑主动提起，一次一件事，避免新闻播报、重复视频和连续追问。保留来源与不确定性，不把热榜传闻当已核实事实，不随意@群友。')
    docs['idle_proactive_chat']['targets']['target_chats'] = typed
    # Autonomous life owns every background share. Never re-enable old timers
    # when refreshing target groups after a shell setting change.
    life['plugin']['enabled'] = False
    life['schedule']['enabled'] = False
    life['generation']['generate_on_load_if_missing'] = False
    life['share'].update(enabled=False, wake_planner=False)
    for profile in life['share']['stream_profiles']:
        profile['extra_prompt'] = '仅以真实执行和同群经历为依据，不编造服务器操作或线下经历。'
    hot['ambient'].update(enabled=False, proactive_chance=0, proactive_groups=[],
        style_hint='保留来源与不确定性，不把传闻当已核实事实；不编造视频观看经历，不自动触发其他bot。')
    docs['idle_proactive_chat']['plugin']['enabled'] = False
    repost['planner']['coordinator'] = True
    docs['autonomous_life']['life']['groups'] = ids
    return docs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    config = json.loads((ROOT / 'config.json').read_text(encoding='utf8'))
    database = (ROOT / 'data/MaiBot.db').resolve()
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        sessions = [dict(r) for r in db.execute('SELECT platform,group_id,group_name FROM chat_sessions WHERE group_id IS NOT NULL')]
    groups = selected_groups(config, sessions)
    docs = prepare(config, groups)
    report = {'groups': groups, 'applied': args.apply, 'date': datetime.now().isoformat()}
    if args.apply:
        backup = ROOT / 'temp/autonomy-config-backup-20261029'
        backup.mkdir(parents=True, exist_ok=True)
        for folder, doc in docs.items():
            path = ROOT / 'plugins' / folder / 'config.toml'
            original = backup / (folder + '.toml')
            if not original.exists():
                shutil.copy2(path, original)
            temporary = path.with_suffix('.toml.tmp')
            temporary.write_text(tomlkit.dumps(doc), encoding='utf8')
            temporary.replace(path)
        report['backup'] = str(backup)
        (ROOT / 'docs/maibot-wechat-acceptance/autonomy-config-20261029.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
