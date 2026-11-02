"""Bilibili Story feed, matching App authorization and weighted tag selection.

Protocol references: BewlyCat appClient/authProvider/appSign and
pskdje/bilibili-API-collect docs/video/recommend.md (Story, not homepage feed).
"""
import asyncio
import hashlib
import logging
import re
import time
from urllib.parse import urlencode
from uuid import uuid4

import httpx

from .judge import Candidate
from .bili_api import BiliError

# Public HD application identity used by BewlyCat/PiliPlus. User tokens remain
# local and are only accepted with the AppKey that issued them.
APPKEY = 'dfca71928277209b'
APPSEC = 'b5475a8825547a4fc26c7d518eaaa02e'


class _HideAppTokens(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        if any(name in message for name in ('access_key=', 'auth_code=', 'access_token=', 'refresh_token=')):
            record.msg = re.sub(r'((?:access_key|auth_code|access_token|refresh_token)=)[^&\s\"\']+', r'\1[redacted]', message)
            record.args = ()
        return True


def signed(params):
    values = {str(k): str(v) for k, v in params.items() if v is not None and v != '' and k != 'sign'}
    values.update(appkey=APPKEY)
    values.setdefault('ts', str(int(time.time())))
    query = urlencode(sorted(values.items()))
    return {**values, 'sign': hashlib.md5((query+APPSEC).encode()).hexdigest()}


def pick_batch(batch, weights, required):
    chosen, best = None, float('-inf')
    for item in batch:
        if not 0 < item.duration <= 120:
            continue
        matched = set(item.tags) & weights.keys()
        if len(matched) < required:
            continue
        score = sum(weights[tag] for tag in matched) + 2*len(matched)
        if score > best:
            chosen, best = item, score
    return chosen


class StoryClient:
    def __init__(self, store, bili, logger):
        self.store, self.bili, self.logger = store, bili, logger
        http_logger = logging.getLogger('httpx')
        if not any(isinstance(item, _HideAppTokens) for item in http_logger.filters):
            http_logger.addFilter(_HideAppTokens())
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=False, headers={
            'User-Agent': 'Mozilla/5.0 BiliDroid/2.0.1 os/android mobi_app/android_hd build/2001100',
            'Referer': 'https://www.bilibili.com', 'env': 'prod', 'app-key': 'android64'})
        self.lock = asyncio.Lock()
        self.auth_lock = asyncio.Lock()
        self.store.set('buvid', self.store.get('buvid') or 'XY'+uuid4().hex.upper())

    async def close(self):
        await self.client.aclose()

    async def request(self, host, path, params, post=False):
        # Endpoints are fixed internal constants; never accept a caller URL.
        headers = {'Buvid': self.store.get('buvid'), 'session_id': uuid4().hex}
        try:
            response = await self.client.request('POST' if post else 'GET', host+path,
                **({'data': signed(params)} if post and '/oauth2/' in path else {'params': signed(params)}), headers=headers)
        except httpx.HTTPError as exc:
            raise BiliError(f'B站App接口网络异常：{type(exc).__name__}') from None
        if response.status_code != 200:
            raise BiliError(f'B站 App 接口 HTTP {response.status_code}')
        payload = response.json()
        if not isinstance(payload, dict):
            raise BiliError('B站 App 接口响应格式错误')
        return payload

    async def verify(self, token, expected_uid=''):
        if token.get('appKey', APPKEY) != APPKEY:
            raise BiliError('AppKey 不匹配，请使用本页面扫码授权；其他客户端 Token 不可混用')
        access = token.get('accessToken') or token.get('access_token')
        if not access:
            raise BiliError('缺少有效 App access_token，UID 本身不能取得账号推荐')
        result = await self.request('https://app.bilibili.com', '/x/v2/account/myinfo', {'access_key': access})
        data = result.get('data') or {}
        uid = str(data.get('mid') or data.get('uid') or '')
        if result.get('code') != 0 or not uid:
            raise BiliError('App 授权验证失败，请重新扫码')
        if expected_uid and str(expected_uid) != uid:
            raise BiliError('授权账号与指定 UID 不一致')
        return {**token, 'appKey': APPKEY, 'accessToken': access, 'mid': uid, 'verified_at': time.time()}

    async def access(self, expected_uid=''):
        async with self.auth_lock:
            generation = self.store.get('auth_generation', 0)
            token = self.store.get('auth') or {}
            expires = float(token.get('accessTokenExpiresAt') or 0)
            if expires and expires < time.time()+600:
                refresh = token.get('refreshToken') or ''
                if not refresh:
                    raise BiliError('App 授权已过期，请重新扫码')
                for version in ('v3', 'v2'):
                    response = await self.request('https://passport.bilibili.com', f'/api/{version}/oauth2/refresh_token',
                        {'access_token': token.get('accessToken'), 'refresh_token': refresh}, post=True)
                    if response.get('code') == 0:
                        token = self.tokens(response.get('data') or {})
                        token['refreshToken'] = token.get('refreshToken') or refresh
                        break
                else:
                    raise BiliError('App Token 刷新失败，请重新扫码')
            if time.time()-float(token.get('verified_at') or 0) > 3600 or str(expected_uid or '') != str(token.get('mid') or '') and expected_uid:
                token = await self.verify(token, expected_uid)
                if self.store.get('auth_generation', 0) != generation:
                    raise BiliError('授权已撤销，本次请求取消')
                self.store.set('auth', token)
            if not token.get('accessToken'):
                raise BiliError('请先在 Story 设置页扫码授权账号')
            return token['accessToken']

    @staticmethod
    def tokens(data):
        info = data.get('token_info') or data
        return {'appKey': APPKEY, 'accessToken': info.get('access_token', ''),
                'refreshToken': (data.get('refresh_token_info') or {}).get('refresh_token') or info.get('refresh_token') or data.get('refresh_token', ''),
                'accessTokenExpiresAt': time.time()+int(info.get('expires_in') or 3600), 'verified_at': 0}

    async def qr_start(self):
        generation = self.store.get('auth_generation', 0)
        result = await self.request('https://passport.bilibili.com', '/x/passport-tv-login/qrcode/auth_code',
                                    {'local_id': '0', 'platform': 'android', 'mobi_app': 'android_hd'}, post=True)
        data = result.get('data') or {}
        if result.get('code') != 0 or not data.get('auth_code') or not data.get('url'):
            raise BiliError('二维码申请失败，请稍后重试')
        if self.store.get('auth_generation', 0) != generation:
            raise BiliError('二维码申请已取消')
        self.store.set('qr', {'auth_code': data['auth_code'], 'expires': time.time()+180})
        import base64
        import io
        import qrcode
        image = qrcode.make(data['url']); buffer = io.BytesIO(); image.save(buffer, format='PNG')
        return {'success': True, 'image': 'data:image/png;base64,'+base64.b64encode(buffer.getvalue()).decode(), 'expires_in': 180}

    async def qr_poll(self, expected_uid=''):
        qr = self.store.get('qr') or {}
        if qr.get('expires', 0) < time.time():
            return {'success': False, 'state': 'expired'}
        result = await self.request('https://passport.bilibili.com', '/x/passport-tv-login/qrcode/poll',
                                    {'auth_code': qr['auth_code'], 'local_id': '0'}, post=True)
        code = result.get('code')
        if code != 0:
            return {'success': True, 'state': 'expired' if code in (86038, -3) else 'waiting', 'code': code}
        token = await self.verify(self.tokens(result.get('data') or {}), expected_uid)
        current_qr = self.store.get('qr') or {}
        if current_qr.get('auth_code') != qr.get('auth_code'):
            return {'success': False, 'state': 'superseded'}
        self.store.set('auth', token); self.store.set('qr', None)
        return {'success': True, 'state': 'authorized', 'uid': token['mid']}

    async def feed(self, config, display_id):
        access = await self.access(config.expected_uid) if config.use_account else ''
        result = await self.request('https://app.bilibili.com', '/x/v2/feed/index/story', {
            'aid': config.seed_aid or 0, 'display_id': display_id, 'access_key': access,
            'mobi_app': 'android_hd', 'build': '2001100', 'platform': 'android', 'pull': 0,
            'ps': 10, 'statistics': '{"appId":5,"platform":3,"version":"2.0.1"}'})
        if result.get('code') != 0:
            raise BiliError(f'Story 推荐不可用，code={result.get("code")}；未替换为首页或搜索结果')
        data = result.get('data') or {}
        rows = data.get('items') or data.get('list') or []
        return rows if isinstance(rows, list) else []

    async def candidate(self, row):
        raw = row.get('archive') or row
        aid = raw.get('aid') or row.get('aid') or row.get('param') or row.get('id')
        bvid = raw.get('bvid') or row.get('bvid') or ''
        if not bvid and not str(aid or '').isdigit():
            return None
        if self.store.seen(aid or bvid):
            return None
        params = {'bvid': bvid} if bvid else {'aid': int(aid)}
        response = await self.bili._get_json(self.bili._bare_client, '/x/web-interface/view', params)
        info = response.get('data') or {}
        if self.store.seen(info.get('aid') or bvid):
            return None
        duration = int(info.get('duration') or 0)
        if not 0 < duration <= 120:
            return None
        tag_response = await self.bili._get_json(self.bili._bare_client, '/x/tag/archive/tags', {'aid': info['aid']})
        tags = [tag['tag_name'] for tag in tag_response.get('data') or [] if isinstance(tag, dict) and tag.get('tag_name')]
        owner, stat = info.get('owner') or {}, info.get('stat') or {}
        return Candidate(bvid=info['bvid'], aid=int(info['aid']), cid=int(info['cid']), title=info['title'],
            author=owner.get('name', ''), author_mid=int(owner.get('mid') or 0), play=int(stat.get('view') or 0),
            like=int(stat.get('like') or 0), favorite=int(stat.get('favorite') or 0), duration=duration,
            typename=info.get('tname', ''), tags=tags, channel='story')

    async def pick(self, config, filters=None):
        async with self.lock:
            weights, batches, seen = self.store.tags(), [], set()
            if not weights:
                raise BiliError('Story 标签为空，请先添加或导入标签')
            scanned, pending, empty_pages = 0, [], 0
            semaphore = asyncio.Semaphore(3)
            async def load(row):
                async with semaphore:
                    return await self.candidate(row)
            for page in range(1, 25):
                rows = await self.feed(config, page)
                unique = []
                for row in rows:
                    if not isinstance(row, dict) or row.get('ad_info') or row.get('goto') in {'ad', 'live', 'bangumi'}:
                        continue
                    key = str(row.get('aid') or row.get('bvid') or row.get('param') or row.get('id') or '')
                    if key and key not in seen:
                        seen.add(key); unique.append(row)
                pending.extend(unique)
                empty_pages = empty_pages+1 if not unique else 0
                while len(pending) >= 10:
                    chunk, pending = pending[:10], pending[10:]
                    result = await asyncio.gather(*(load(row) for row in chunk), return_exceptions=True)
                    errors = [item for item in result if isinstance(item, Exception)]
                    if errors:
                        self.logger.warning('Story 元数据请求失败：%s/%s，错误类型=%s', len(errors), len(chunk),
                                            sorted({type(item).__name__ for item in errors}))
                        if len(errors) == len(chunk):
                            raise BiliError('本批Story视频信息均未能取得，不能判定为没有匹配视频')
                    batch = [item for item in result if isinstance(item, Candidate)]
                    if filters is not None:
                        batch = [item for item in batch if not any(str(word).lower() in value.lower()
                            for words, value in ((filters.title_blacklist, item.title),
                                                 (filters.tag_blacklist, ' '.join(item.tags)),
                                                 (filters.up_blacklist, item.author))
                            for word in words if str(word).strip())]
                    batches.append(batch); scanned += len(chunk)
                    required = 3 if scanned < 20 else 2 if scanned < 50 else 1
                    # Try the stricter current batch first, then reconsider prior
                    # batches only when reaching a documented relaxation threshold.
                    chosen = pick_batch(batch, weights, 3 if scanned <= 20 else 2 if scanned <= 50 else 1)
                    if chosen is not None:
                        return chosen
                    if scanned >= 20:
                        for previous in batches:
                            if (chosen := pick_batch(previous, weights, required)) is not None:
                                return chosen
                    if scanned >= 60:
                        return None
                if empty_pages >= 2:
                    break
            return None
