"""Compatibility notice for the retired shell persona command.

Never read shell persona storage, spend points or overwrite native settings.
"""
COMMAND = '/persona'
WEBUI_CONFIG = {'version': 1, 'fields': {}}


def init(config):
    pass


def set_llm_service(service):
    pass


def handle(content, context):
    return '人格、昵称、行为和表达已统一由 MaiBot 原生管理，请在 MaiBot WebUI 的麦麦设置中修改。'
