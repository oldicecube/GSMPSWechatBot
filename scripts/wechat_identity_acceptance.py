"""Isolated real translation/person DB/profile checks; no chat/API traffic."""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


async def main():
    checks = []
    def check(name, condition):
        checks.append(dict(name=name, passed=bool(condition)))
        assert condition, name
    with tempfile.TemporaryDirectory(prefix='wechat-identity-') as directory:
        base = Path(directory)
        (base / 'config').mkdir()
        for name in ('bot_config.toml', 'model_config.toml'):
            shutil.copy2(ROOT / 'config' / name, base / 'config' / name)
        with patch.dict(os.environ, {'MAIBOT_INSTANCE_DIR': directory}):
            from llm.core.wechat_translation import WechatMessageTranslator
            from src.common.database.database import engine, get_db_session, initialize_database
            from src.common.database.database_model import PersonInfo
            from src.person_info.person_info import Person, get_person_id, resolve_person_id_for_memory
            from src.maisaka.memory import person_profile
            from src.maisaka.builtin_tool.query_memory import _resolve_person_id
            from src.config.config import global_config
            from sqlmodel import select
            initialize_database()
            try:
                translator = WechatMessageTranslator({'weflow': {'myWxid': 'wxid_test_bot'}})
                def ctx(uid, nickname, mid):
                    return dict(wxid=uid, user=nickname, content='synthetic identity test', group='fixture', sessionId='test@chatroom', serverId=mid)
                old = await translator.translate(ctx('wxid_person_with_suffix', '旧昵称', '1'))
                new = await translator.translate(ctx('wxid_person_with_suffix', '新昵称', '2'))
                other = await translator.translate(ctx('wxid_other', '新昵称', '3'))
                check('rename keeps full wxid', old.message_info.user_info.user_id == new.message_info.user_info.user_id == 'wxid_person_with_suffix')
                check('rename keeps session', old.session_id == new.session_id)
                check('nickname is display data', new.message_info.user_info.user_nickname == '新昵称')
                check('same name different sender kept distinct', other.message_info.user_info.user_id != new.message_info.user_info.user_id)
                try:
                    await translator.translate(ctx('', '新昵称', '4'))
                except ValueError:
                    check('missing ID never replaced by nickname', True)
                else:
                    check('missing ID never replaced by nickname', False)
                first = Person.register_person('wechat', 'wxid_person_with_suffix', '旧昵称', 'test@chatroom', '旧群名片')
                first.memory_points = ['爱好:喜欢写代码:1']
                first.sync_to_database()
                renamed = Person.register_person('wechat', 'wxid_person_with_suffix', '新昵称', 'test@chatroom', '新群名片')
                check('rename retains person ID', first.person_id == renamed.person_id)
                check('rename retains established name', renamed.person_name == '旧昵称')
                check('rename updates current alias', renamed.nickname == '新昵称')
                restored = Person(platform='wechat', user_id='wxid_person_with_suffix')
                check('saved memory survives reload', restored.memory_points == first.memory_points)
                check('old alias still resolves', resolve_person_id_for_memory(person_name='旧昵称', platform='wechat') == first.person_id)
                check('new alias resolves', resolve_person_id_for_memory(person_name='新昵称', platform='wechat') == first.person_id)
                second = Person.register_person('wechat', 'wxid_other', '新昵称')
                check('ambiguous nickname rejected', resolve_person_id_for_memory(person_name='新昵称', platform='wechat') == '')
                check('explicit ID overrides conflicting nickname', resolve_person_id_for_memory(person_name='新昵称', platform='wechat', user_id='wxid_person_with_suffix') == first.person_id)
                check('constructor explicit ID wins', Person(platform='wechat', user_id='wxid_other', person_name='旧昵称').person_id == second.person_id)
                check('platform scopes identity', get_person_id('qq', 'wxid_person_with_suffix') != first.person_id)
                check('named query never falls back to requester', _resolve_person_id(person_name='不存在的人', platform='wechat', user_id='wxid_person_with_suffix', group_id='test@chatroom')[0] == '')
                with get_db_session() as db:
                    count = len(db.exec(select(PersonInfo)).all())
                check('rename creates no duplicate person', count == 2)
                global_config.a_memorix.integration.enable_person_profile_injection = True
                fake = AsyncMock(return_value={'success': True, 'person_name': '旧昵称', 'profile_text': '喜欢写代码'})
                with patch.object(person_profile.memory_service, 'profile_admin', fake):
                    prompt = '\n'.join(await person_profile.build_person_profile_injection_messages(anchor_message=new))
                check('profile queried by stable person ID', fake.call_args.kwargs['person_id'] == first.person_id)
                check('prompt links current name and stable identity', all(s in prompt for s in ('新昵称', '旧昵称', 'wxid_person_with_suffix', first.person_id, '喜欢写代码')))
            finally:
                engine.dispose()
    output = ROOT / 'temp/wechat-identity-acceptance.json'
    output.write_text(json.dumps(dict(passed=len(checks), checks=checks), ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps(dict(passed=len(checks), report=str(output))))


if __name__ == '__main__':
    asyncio.run(main())
