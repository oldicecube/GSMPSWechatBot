"""Focused QQ self-identity and moderation-relation acceptance.

No production process, transport, message or moderation action is used.
"""
import asyncio
import importlib
import sys
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.platform_io.bot_identity import clear, get, remember
from src.common.data_models.message_component_data_model import MessageSequence, TextComponent, AtComponent, ReplyComponent
from src.common.data_models.mai_message_data_model import GroupInfo, MessageInfo, UserInfo
from src.chat.message_receive.message import SessionMessage
from src.maisaka.context.messages import SessionBackedMessage
from src.maisaka.context.planner_messages import build_planner_user_prefix_from_session_message
from src.maisaka.chat_loop_service import MaisakaChatLoopService

pkg = type(sys)('qq_identity_acceptance_plugin')
pkg.__path__ = ['plugins/MaiBot-SnowLuma-Adapter']
sys.modules[pkg.__name__] = pkg
QQNoticeTextRenderer = importlib.import_module(pkg.__name__ + '.codecs.notice.renderer').QQNoticeTextRenderer


def check(name, value):
    print(name, bool(value), flush=True)
    assert value, name


def main():
    clear()
    remember(platform='qq', account_id='3294652115', scope='napcat-local', group_id='1020993873',
             nickname='QQ bot', cardname='mahirunn')
    identity = get(platform='qq', account_id='3294652115', scope='napcat-local', group_id='1020993873')
    check('identity_registry_group_card', identity and identity['cardname'] == 'mahirunn')

    message = SessionMessage(message_id='outbound-fixture', timestamp=datetime.now(), platform='qq')
    message.account_id = '3294652115'
    message.scope = 'napcat-local'
    message.session_id = 'qq-session-fixture'
    message.message_info = MessageInfo(
        user_info=UserInfo('3294652115', 'QQ bot', 'mahirunn'),
        group_info=GroupInfo('1020993873', 'GDUT SIE Minecraft Public Server'),
        additional_config={'platform_bot_identity': identity},
    )
    message.raw_message = MessageSequence(components=[
        AtComponent('3294652115', 'mahirunn', 'mahirunn'),
        AtComponent('2103203812', 'oldicecube老冰块', '果园oldicecube'),
        ReplyComponent('quoted', '原文', '3294652115', 'mahirunn', 'mahirunn'),
        TextComponent(text='状态'),
    ])
    message.processed_plain_text = '状态'
    message.initialized = True
    entry = SessionBackedMessage.from_session_message(message, raw_message=message.raw_message, visible_text='状态')
    loop = NS(_session_id=message.session_id, _is_group_chat=True, _bot_platform='',
              _bot_platform_nickname='', _bot_group_cardname='',
              _localized_text=lambda values: values['zh-CN'])
    notice = MaisakaChatLoopService._build_bot_nickname_notice(loop, [entry])
    check('prompt_knows_qq_group_card', 'mahirunn' in notice and '\u672c\u7fa4' in notice)
    prefix = build_planner_user_prefix_from_session_message(message)
    check('outbound_history_marks_self', 'is_self_message="true"' in prefix)
    check('at_uses_stable_qq_id', '<at_ref platform="qq" user_id="2103203812"' in prefix)
    check('self_at_is_explicit', 'user_id="3294652115"' in prefix and 'is_bot_self="true"' in prefix)
    check('self_reply_is_explicit', '<reply_ref platform="qq" user_id="3294652115"' in prefix)

    async def run_renderer():
        renderer = QQNoticeTextRenderer()
        async def resolve(_group, user_id, _default):
            return {'2103203812': 'oldicecube', '3294652115': 'まひるん'}.get(str(user_id), 'other')
        text = await renderer._build_group_ban_notice_text(
            {'operator_id': '3294652115', 'user_id': '2103203812', 'duration': 60, 'sub_type': 'ban'},
            resolve, '1020993873', 'ban', '3294652115')
        check('ban_text_marks_bot_actor', '\u4f60\uff08\u673a\u5668\u4eba\uff09' in text)
        check('ban_text_keeps_target', 'oldicecube' in text)
        poke = await renderer.build_notice_text(
            {'notice_type': 'notify', 'sub_type': 'poke', 'group_id': '1020993873',
             'user_id': '2103203812', 'target_id': '3294652115', 'raw_info': []},
            resolve, '3294652115')
        check('poke_target_uses_canonical_bot_identity', 'GSMPS Bot' in poke and '\u307e\u3072\u308b\u3093' in poke)

    asyncio.run(run_renderer())
    print('QQ SELF CONTEXT ACCEPTANCE PASS', flush=True)


if __name__ == '__main__':
    main()
