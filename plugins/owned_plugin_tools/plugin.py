"""Native SDK tool; original shell modules remain external executors."""
import json
from maibot_sdk import Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType


class Section(PluginConfigBase):
    enabled: bool = Field(default=True, description='允许查询和调用自己的插件，保留原权限')
    config_version: str = Field(default='1.0.0')


class Config(PluginConfigBase):
    plugin: Section = Field(default_factory=Section)


DESCRIPTION = ('MaiBot native plugins only. GSMPS shell plugins are excluded. 查询并调用自己拥有的插件。先 list（可 query 搜索，offset 翻页）查到 target 和参数，'
    '再 invoke。壳子命令的 arguments_json 为 {"text":"命令参数，不含 /命令名"}；'
    '原生 command 的 text 包含完整 /命令；tool 使用目录中的 JSON schema。'
    '身份固定为当前会话 bot 自己的普通用户，不能借用群友管理员权限或跨平台、跨群。'
    '自动化只观察真实消息，不能伪造拍一拍、进服等事件；后台监控可查看登记状态。'
    '每条消息最多执行3次，同一目标运行中不能重复提交。结果为插件返回的数据，'
    '不代表自己之前通过 LLM 决定说过的话；插件已发送的内容不要重复发。')


class OwnedPluginTools(MaiBotPlugin):
    config_model = Config

    async def on_load(self):
        pass

    async def on_unload(self):
        pass

    async def on_config_update(self, scope, config_data, version):
        if scope == 'self':
            self.set_plugin_config(config_data)

    @Tool('owned_plugins', brief_description=DESCRIPTION, detailed_description=DESCRIPTION, core_tool=True,
        parameters=[
            ToolParameterInfo(name='operation', param_type=ToolParamType.STRING, required=True, enum_values=['list', 'invoke'], description='查询或执行'),
            ToolParameterInfo(name='query', param_type=ToolParamType.STRING, required=False, description='目录关键词'),
            ToolParameterInfo(name='offset', param_type=ToolParamType.INTEGER, required=False, description='目录分页起点'),
            ToolParameterInfo(name='target', param_type=ToolParamType.STRING, required=False, description='目录返回的精确 target'),
            ToolParameterInfo(name='arguments_json', param_type=ToolParamType.STRING, required=False, description='JSON 对象参数')])
    async def execute(self, operation='list', query='', offset=0, target='', arguments_json='{}', **kwargs):
        if not self.config.plugin.enabled:
            return {'success': False, 'error': 'Owned plugin tools disabled'}
        try:
            if len(arguments_json) > 8000:
                raise ValueError('too long')
            arguments = json.loads(arguments_json)
            if not isinstance(arguments, dict):
                raise ValueError('not an object')
        except (ValueError, TypeError):
            return {'success': False, 'error': 'arguments_json must be a JSON object up to 8000 characters'}
        result = await self.ctx.call_capability('wechatbot.owned_plugins', grant=kwargs.get('_host_grant', ''),
            operation=operation, query=query, offset=offset, target=target, arguments=arguments)
        result['content'] = json.dumps(result, ensure_ascii=False)
        return result


def create_plugin():
    return OwnedPluginTools()
