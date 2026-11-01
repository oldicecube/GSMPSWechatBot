"""插件配置模型。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from maibot_sdk import Field, PluginConfigBase

PLUGIN_VERSION = "0.2.12"
CONFIG_VERSION = "0.2.10"


class GroupIdList:
    """清洗 QQ 群号并保持首次出现顺序。"""

    @staticmethod
    def normalize(values: Iterable[Any]) -> list[str]:
        """将配置中的任意群号值规范化为有序字符串列表。

        每个值都会先转换为字符串并去除首尾空白。空值和重复值会被忽略，
        重复项保留第一次出现的位置。

        Args:
            values: 可迭代的群号配置值，元素可以是字符串、整数或空值。

        Returns:
            去空、去重且保持首次出现顺序的 QQ 群号字符串列表。

        Examples:
            ``GroupIdList.normalize([" 100 ", 200, "100"])`` 返回
            ``["100", "200"]``。
        """

        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            group_id = str(value or "").strip()
            if not group_id or group_id in seen:
                continue
            seen.add(group_id)
            result.append(group_id)
        return result


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=False, description="是否启用自主跨群转发")
    version: str = Field(default=PLUGIN_VERSION, description="插件版本")
    config_version: str = Field(default=CONFIG_VERSION, description="配置版本")


class RoutingConfig(PluginConfigBase):
    """QQ 群白名单配置。"""

    __ui_label__ = "群聊路由"
    __ui_icon__ = "route"
    __ui_order__ = 1

    source_groups: list[str] = Field(
        default_factory=list,
        description="允许触发自主转发的 QQ 群号列表",
    )
    target_groups: list[str] = Field(
        default_factory=list,
        description="允许接收自主转发的 QQ 群号列表，列表顺序即处理顺序",
    )


class BehaviorConfig(PluginConfigBase):
    """转发行为配置。"""

    __ui_label__ = "转发行为"
    __ui_icon__ = "message-square-share"
    __ui_order__ = 2

    view_failure_fallback_threshold: int = Field(
        default=2,
        ge=1,
        description="允许降级前 view_forward_message 的连续可重试故障次数",
    )
    trigger_target_planner: bool = Field(
        default=True,
        description="发送成功后是否触发目标群 Planner 自主决定是否评论",
    )
    trigger_source_planner: bool = Field(
        default=True,
        description="source 白名单群收到合并转发时是否强制触发本群 Planner，默认开启",
    )


class ForwardMessagesAutoConfig(PluginConfigBase):
    """插件完整配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    behavior: BehaviorConfig = Field(default_factory=BehaviorConfig)
