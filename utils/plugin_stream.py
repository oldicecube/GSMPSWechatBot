"""Resolve SDK stream identities without importing Host process registries."""

async def stream_for(ctx, stream_id):
    payload = await ctx.chat.get_all_streams(platform='all_platforms')
    rows = payload.get('streams', []) if isinstance(payload, dict) else payload
    for row in rows or []:
        if isinstance(row, dict) and str(row.get('session_id') or row.get('stream_id') or '') == str(stream_id):
            if row.get('platform') in {'qq', 'wechat'} and row.get('account_id') and row.get('group_id'):
                return row
    return None
