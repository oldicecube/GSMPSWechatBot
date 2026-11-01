"""仅微信：/qq <正文>，转发到已放行且开启主动回复的 QQ 群。"""
COMMAND = '/qq'
SUPPORTED_PLATFORMS = ['wechat']
WEBUI_CONFIG = {'version':1,'fields':{
    'max_chars':{'type':'int','default':2000,'min':1,'max':8000,'label':'转发正文最大长度','description':'超过长度的 /qq 内容会被拒绝。','reload':'hot'},
    'cooldown_seconds':{'type':'int','default':5,'min':0,'max':3600,'label':'转发冷却（秒）','description':'按来源平台、目标平台和当前群聊限流。','reload':'hot'},
}}


def handle(content, context):
    if (context.get('platform') or 'wechat') != 'wechat':
        return '/qq 仅限微信使用'
    from core.cross_platform_relay import relay_sync
    return relay_sync(content,context,'qq')
