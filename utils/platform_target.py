"""Explicit platform targets for shell compatibility and native plugin settings."""
from urllib.parse import quote, unquote


def target_key(platform, account_id, scope, kind, target_id):
    if platform != 'qq' or kind not in {'group', 'private'} or not account_id or not target_id:
        raise ValueError('Explicit QQ account and destination required')
    return ':'.join(['qq', quote(str(account_id), safe=''), quote(str(scope or ''), safe=''), kind,
                     quote(str(target_id), safe='')])


def parse_target(value):
    value = str(value or '').strip()
    if value.startswith('qq:'):
        parts = value.split(':')
        if len(parts) != 5 or parts[3] not in {'group', 'private'}:
            raise ValueError('QQ target format: qq:account:connection:group|private:id')
        account, scope, target = unquote(parts[1]), unquote(parts[2]), unquote(parts[4])
        if any(not x.isascii() or not x.isdecimal() or int(x) <= 0 for x in (account, target)):
            raise ValueError('QQ account and target must be positive numeric IDs')
        return dict(platform='qq', account_id=account, scope=scope, kind=parts[3], target_id=target)
    return dict(platform='wechat', account_id=None, scope=None,
                kind='group' if value.endswith('@chatroom') else 'private', target_id=value)


def matches_target(message, value):
    target = parse_target(value)
    info = message.get('message_info') or {}
    actual_id = (info.get('group_info') or {}).get('group_id') if target['kind']=='group' else (info.get('user_info') or {}).get('user_id')
    return (message.get('platform') == target['platform'] and str(actual_id or '') == target['target_id']
            and (target['account_id'] is None or str(message.get('account_id') or '') == target['account_id'])
            and (target['scope'] is None or str(message.get('scope') or '') == target['scope']))


def is_group_target(value):
    try:
        return parse_target(value)['kind'] == 'group'
    except ValueError:
        return False


async def resolve_configured_stream(chat, value):
    target = parse_target(value)
    if target['platform'] == 'wechat':
        getter = chat.get_stream_by_group_id if target['kind']=='group' else chat.get_stream_by_user_id
        return await getter(target['target_id'], platform='wechat')
    result = await chat.get_all_streams(platform='qq')
    rows = result.get('streams', []) if isinstance(result, dict) else result
    matches = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        info = {'group_info': {'group_id': row.get('group_id')}, 'user_info': {'user_id': row.get('user_id')}}
        if (bool(row.get('group_id')) == (target['kind']=='group')
                and matches_target({**row, 'message_info': info}, value)):
            matches.append(row)
    return matches[0] if len(matches)==1 else None
