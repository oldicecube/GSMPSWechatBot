"""Native emoji storage, catalog, transport and legacy-data acceptance.

Uses an isolated instance. Captures wxSend requests without posting to WeChat.
"""
from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
instance = Path(tempfile.mkdtemp(prefix='emoji-acceptance-', dir=ROOT / 'temp'))
(instance / 'config').mkdir()
for name in ['bot_config.toml', 'model_config.toml']:
    shutil.copy2(ROOT / 'config' / name, instance / 'config' / name)
os.environ['MAIBOT_INSTANCE_DIR'] = str(instance)
os.environ.pop('MAIBOT_INTERCEPT_SEND', None)

from PIL import Image
from sqlmodel import select
from fastapi.testclient import TestClient
from src.common.database.database import get_db_session, initialize_database, engine
from src.common.database.database_model import Images, ImageType
from src.common.data_models.message_component_data_model import EmojiComponent, TextComponent
from src.config.config import global_config
from llm.core.maibot_emoji import get_native_emoji_path, native_emoji_catalog, native_emoji_file
from llm.core.wechat_driver import WechatShellDriver
from llm.core.wechat_translation import WechatMessageTranslator
from src.platform_io.types import RouteKey
from scripts.maibot_emoji_migrate import repair_legacy_emoji_rows

initialize_database()
results = []


def check(name, value):
    results.append({'name': name, 'passed': bool(value)})
    if not value:
        raise AssertionError(name)


def fixture(name, fmt='PNG', color='red', registered=True, banned=False):
    buffer = BytesIO()
    if fmt == 'GIF':
        Image.new('RGB', (12, 12), color).save(buffer, format=fmt, save_all=True,
            append_images=[Image.new('RGB', (12, 12), 'blue')], duration=100, loop=0)
    else:
        Image.new('RGB', (12, 12), color).save(buffer, format=fmt)
    data = buffer.getvalue()
    path = instance / 'data' / 'emoji' / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    with get_db_session() as session:
        row = Images(image_hash=digest, description='native fixture',
            full_path=path.relative_to(instance).as_posix(), image_type=ImageType.EMOJI,
            is_registered=registered, is_banned=banned)
        session.add(row)
        session.flush()
        identifier = row.id
    return identifier, digest, path, data


async def main():
    fixtures = [fixture('native.png'), fixture('animated.gif', 'GIF'),
                fixture('renamed.jpeg', 'JPEG', 'green'), fixture('native.webp', 'WEBP', 'yellow')]
    pending = fixture('pending.png', color='pink', registered=False)
    banned = fixture('banned.png', color='black', banned=True)
    missing = fixture('missing.png', color='orange')
    missing[2].unlink()
    from llm.security import build_emoji_index, get_emoji_list, get_emoji_path
    old_dir = instance / 'legacy'
    old_dir.mkdir()
    shutil.copy2(fixtures[0][2], old_dir / 'rogue.png')
    check('catalog contains only four usable native records', len(native_emoji_catalog()) == 4)
    check('old emoji_dir ignored', len(build_emoji_index(str(old_dir))) == 4 and get_emoji_path('rogue') is None)
    check('legacy import returns native hashes', set(get_emoji_list()) == {f[1] for f in fixtures})
    for item in [pending, banned, missing]:
        check('unavailable native emoji excluded ' + item[2].name, get_native_emoji_path(item[1]) is None)
    check('old suffix not guessed', get_native_emoji_path('renamed.jpg') is None)
    check('actual native suffix resolved', get_native_emoji_path('renamed.jpeg') == str(fixtures[2][2]))
    check('arbitrary path not accepted as animation', get_native_emoji_path(str(fixtures[0][2])) is None)

    # Observe current native DB state immediately, without rebuilding any index.
    with get_db_session() as session:
        row = session.get(Images, fixtures[0][0]); row.is_banned = True; session.add(row)
    check('native ban applied immediately', get_native_emoji_path(fixtures[0][1]) is None)
    with get_db_session() as session:
        row = session.get(Images, fixtures[0][0]); row.is_banned = False; session.add(row)
    check('native unban applied immediately', get_native_emoji_path(fixtures[0][1]) == str(fixtures[0][2]))

    translator = WechatMessageTranslator({'weflow': {'myWxid': 'wxid_fixture_bot'}})
    message = await translator.translate({'platform': 'wechat', 'group': 'fixture-group', 'group_id': 'fixture@chatroom',
        'user': 'fixture-user', 'wxid': 'wxid_fixture', 'content': 'fixture', 'type': 'group', 'is_group': True,
        'sessionId': 'fixture@chatroom', 'serverId': 'emoji-fixture', 'localType': 1})
    message.session_id = 'emoji-fixture-session'
    message.message_info.group_info.group_name = 'fixture-group'
    driver = WechatShellDriver('wxid_fixture_bot')
    route = RouteKey('wechat', 'wxid_fixture_bot')
    # Exercise native components -> driver -> sender -> exact wxSend file field.
    for identifier, digest, path, data in fixtures:
        for inline in [False, True]:
            message.raw_message.components = [EmojiComponent(binary_hash=digest, binary_data=data if inline else None)]
            with patch('core.sender._post_wechat_hook', return_value=(True, 'fixture')) as post, \
                 patch('core.sender._apply_send_delay'), patch('core.sender._notify_send_listeners'):
                receipt = await driver.send_message(message, route)
            check(f'{path.suffix} inline={inline} delivered', receipt.status.value == 'sent')
            check(f'{path.suffix} inline={inline} native wxSend file', Path(post.call_args.args[0]['file']) == path)
            check(f'{path.suffix} inline={inline} exact bytes', path.read_bytes() == data)
    with Image.open(fixtures[1][2]) as image:
        check('GIF animation frames preserved', image.n_frames == 2)

    previous = Path.cwd()
    try:
        os.chdir(instance / 'temp' if (instance / 'temp').is_dir() else instance)
        check('native path independent of cwd', get_native_emoji_path(fixtures[0][1]) == str(fixtures[0][2]))
        check('hash-only component independent of cwd', await native_emoji_file(EmojiComponent(binary_hash=fixtures[0][1])) == fixtures[0][2])
    finally:
        os.chdir(previous)

    # A new plugin emoji is persisted by MaiBot, not an outbound/legacy cache.
    buffer = BytesIO(); Image.new('RGB', (9, 9), 'purple').save(buffer, format='PNG')
    component = EmojiComponent(binary_hash='', binary_data=buffer.getvalue())
    path = await native_emoji_file(component)
    check('new emoji stored under MaiBot data/emoji', path.parent == instance / 'data' / 'emoji')
    with get_db_session() as session:
        row = session.exec(select(Images).filter_by(image_hash=component.binary_hash, image_type=ImageType.EMOJI)).first()
        check('new emoji cached without bypassing native registration', row is not None and not row.is_registered)

    for component in [EmojiComponent(binary_hash=banned[1]), EmojiComponent(binary_hash=missing[1]),
                      EmojiComponent(binary_hash='', binary_data=b'not an image')]:
        message.raw_message.components = [TextComponent('must not partially send'), component]
        with patch('core.sender.send') as send:
            receipt = await driver.send_message(message, route)
        check('invalid/banned/missing fails before any send ' + component.binary_hash[:8], receipt.status.value == 'failed' and not send.called)

    # Native management and original-image route point to the same catalog.
    from src.webui.app import create_app
    global_config.webui.auth_enabled = False
    with TestClient(create_app(), client=('127.0.0.1', 1234)) as client:
        response = client.get('/api/webui/emoji/list', params={'page_size': 100})
        check('native WebUI emoji manager available', response.status_code == 200)
        image = client.get(f'/api/webui/emoji/{fixtures[1][0]}/thumbnail', params={'original': True})
        check('native WebUI serves exact animated original', image.status_code == 200 and image.content == fixtures[1][3])
        from src.emoji_system.emoji_manager import emoji_manager
        response = client.patch(f'/api/webui/emoji/{fixtures[0][0]}', json={'is_banned': True})
        check('WebUI ban changes native send lookup', response.status_code == 200 and get_native_emoji_path(fixtures[0][1]) is None)
        check('WebUI ban removes native Planner candidate', emoji_manager.get_emoji_by_hash(fixtures[0][1]) is None)
        response = client.patch(f'/api/webui/emoji/{fixtures[0][0]}', json={'is_banned': False, 'description': 'updated native description'})
        check('WebUI unban restores native send lookup', response.status_code == 200 and get_native_emoji_path(fixtures[0][1]) == str(fixtures[0][2]))
        check('WebUI unban restores native Planner candidate', emoji_manager.get_emoji_by_hash(fixtures[0][1]) is not None)
        check('WebUI description reaches native Planner candidate', emoji_manager.get_emoji_by_hash(fixtures[0][1]).description == 'updated native description')

    # Old lower-case record and stale .jpg path duplicate an existing native hash.
    database = instance / 'data' / 'MaiBot.db'
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        native = dict(connection.execute('SELECT * FROM images WHERE id=?', (fixtures[2][0],)).fetchone())
        legacy = dict(native); legacy.pop('id'); legacy['image_type'] = 'emoji'; legacy['full_path'] = 'legacy/renamed.jpg'
        columns = ','.join(legacy)
        connection.execute(f'INSERT INTO images ({columns}) VALUES ({",".join("?" for _ in legacy)})', list(legacy.values()))
    dry = repair_legacy_emoji_rows(database, instance)
    check('legacy migration dry run finds duplicate', len(dry['removed_duplicate_ids']) == 1 and not dry['applied'])
    repaired = repair_legacy_emoji_rows(database, instance, apply=True)
    check('legacy migration archives removed row', len(repaired['removed_duplicate_ids']) == 1 and Path(repaired['archive']).is_file())
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        after = dict(connection.execute('SELECT * FROM images WHERE id=?', (fixtures[2][0],)).fetchone())
    check('native metadata preserved during duplicate cleanup', native == after)
    check('legacy migration idempotent', not repair_legacy_emoji_rows(database, instance, apply=True)['removed_duplicate_ids'])
    report = ROOT / 'docs' / 'maibot-wechat-acceptance' / 'emoji-native-20261030.json'
    report.write_text(json.dumps({'passed': len(results), 'physical_wechat_send': False,
        'results': results}, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': len(results), 'report': str(report)}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        asyncio.run(main())
    finally:
        engine.dispose()
