"""Capabilities of the two installed transports, independent of persona/style."""


def message_capabilities(platform: str, *, is_group: bool) -> dict:
    if platform == 'wechat':
        return dict(adapter='GSMPS Wechat Weflow', reply='drop', at_user=is_group, at_all=False,
                    image='file', emoji='native_asset_as_file', voice='shell_voice', video='file',
                    file=True, forward=False, mute=False, recall=False, kick=False, unsupported='reject_before_send', unsupported_admin='drop_control_segment')
    if platform == 'qq':
        return dict(adapter='QQ OneBot (NapCat/SnowLuma)', reply='native_platform_id', at_user=is_group,
                    at_all='requires_group_permission' if is_group else False,
                    image='native', emoji='native_image', voice='record', video='native', file=True,
                    forward='native', mute='requires_group_admin' if is_group else False,
                    recall='self_or_group_admin', kick='requires_group_admin' if is_group else False, unsupported='reject_before_send')
    return {}


def planner_transport_hint(message) -> str:
    platform = str(message.platform)
    if platform not in {'wechat', 'qq'}:
        return ''
    group = message.message_info.group_info is not None
    scope = '群聊' if group else '私聊'
    if platform == 'wechat':
        features = ('引用控制段会直接丢弃，不能把引用格式/引用内容拼成回复；'
                    '图片、原生表情包和视频通过文件发送，语音交给微信语音出口；'
                    '不支持@全体、QQ表情/戳一戳/撤回/禁言/踢人或发送合并转发；'
                    'QQ管理工具不对微信开放，误输出的管理控制段会丢弃，不能声称已执行。')
    else:
        features = ('支持结构化引用、图片、表情包、语音、视频、文件和合并转发；'
                    '引用只使用当前QQ会话可定位的实际消息ID；'
                    '@全体仍需群权限，不因适配器支持就视为已授权；'
                    'QQ专用管理能力还需对应工具已启用。')
    return ('[当前消息适配器能力] 来源平台=' + platform + '，' + scope + '。' + features
            + ('真实@使用本群的平台用户ID，不从昵称猜测或使用另一个平台的ID。' if group else '私聊不得插入群@控制段。')
            + '当前群的即时上下文独立；跨群记忆、表达或黑话须结合来源理解，不能当作本群刚发生的事。'
            + '发送沿当前会话的平台、账号和连接作用域路由，不回退到另一个平台；不支持或缺失媒体的字段须明确失败，不能声称已发送。')


def dropped_admin_control(component):
    from src.common.data_models.message_component_data_model import DictComponent
    if not isinstance(component,DictComponent):
        return False
    data=component.data
    types={'mute','ban','group_ban','set_group_ban','set_group_whole_ban','kick','set_group_kick',
           'recall','recall_message','delete_message','poke','send_poke','set_group_admin'}
    kind=str(data.get('type') or '')
    if kind in types:
        return True
    payload=data.get('data') or {}
    return kind in {'action','operation','qq_action'} and isinstance(payload,dict) and payload.get('action') in types
