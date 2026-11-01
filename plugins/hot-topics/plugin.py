"""MaiBot 热榜感知插件（hot-topics）。

把全网热榜（微博/知乎/抖音/B站/贴吧等）包装成 MaiBot Tool，
经雷区过滤 + 人设兴趣加权后，以"素材"形式交给 Planner/Replyer 拟人化转述。
数据源：开源 60s API（多实例自动切换，地址可配置），支持自定义 RSS 源。
"""

from __future__ import annotations

import random
import re
from . import hot_core

from typing import Any, Literal

from maibot_sdk import API, Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

PLUGIN_VERSION = "0.7.6"
SUPPORTED_CONFIG_VERSION = PLUGIN_VERSION


# ---------------- 配置模型 ----------------

class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_order__ = 0
    enabled: bool = Field(
        default=True,
        description="是否启用插件",
        json_schema_extra={"label": "启用插件", "order": 0},
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="配置版本",
        json_schema_extra={"hidden": True, "disabled": True, "label": "配置版本"},
    )


class SourcesSectionConfig(PluginConfigBase):
    __ui_label__ = "信息源"
    __ui_order__ = 1
    api_base_urls: list[str] = Field(
        default_factory=lambda: list(hot_core.DEFAULT_BASE_URLS),
        description="按顺序尝试，失败自动切换；可填你自部署的地址",
        json_schema_extra={
            "label": "API 实例地址",
            "hint": "默认是 60s API 的公共社区实例（取自官方实例列表），免费但有限流；"
                    "想更稳定可在自己服务器 docker 部署一个 60s 服务（一行命令）并替换成你的地址",
        },
    )
    request_timeout: float = Field(
        default=8.0, ge=2.0, le=30.0,
        description="单次请求超时（秒）",
        json_schema_extra={"label": "请求超时（秒）", "step": 1.0},
    )
    enable_weibo: bool = Field(default=False, description="微博热搜",
                               json_schema_extra={"label": "微博热搜"})
    enable_zhihu: bool = Field(default=False, description="知乎热榜",
                               json_schema_extra={"label": "知乎热榜"})
    enable_douyin: bool = Field(default=True, description="抖音热点",
                                json_schema_extra={"label": "抖音热点"})
    enable_toutiao: bool = Field(default=False, description="头条热榜",
                                 json_schema_extra={"label": "头条热榜"})
    enable_bili: bool = Field(default=True, description="B站热搜",
                              json_schema_extra={"label": "B站热搜"})
    enable_baidu_tieba: bool = Field(default=False, description="贴吧话题榜",
                                     json_schema_extra={"label": "贴吧话题榜"})
    enable_baidu_hot: bool = Field(default=False, description="百度热搜",
                                   json_schema_extra={"label": "百度热搜"})
    enable_baidu_teleplay: bool = Field(default=False, description="百度电视剧榜",
                                        json_schema_extra={"label": "百度电视剧榜"})
    enable_it_news: bool = Field(default=False, description="IT之家热榜",
                                 json_schema_extra={"label": "IT之家热榜"})
    enable_dongchedi: bool = Field(default=False, description="懂车帝热搜",
                                   json_schema_extra={"label": "懂车帝热搜"})
    enable_quark: bool = Field(default=False, description="夸克热点",
                               json_schema_extra={"label": "夸克热点"})

    def enabled_keys(self) -> list[str]:
        mapping = {
            "weibo": self.enable_weibo,
            "zhihu": self.enable_zhihu,
            "douyin": self.enable_douyin,
            "toutiao": self.enable_toutiao,
            "bili": self.enable_bili,
            "baidu_tieba": self.enable_baidu_tieba,
            "baidu_hot": self.enable_baidu_hot,
            "baidu_teleplay": self.enable_baidu_teleplay,
            "it_news": self.enable_it_news,
            "dongchedi": self.enable_dongchedi,
            "quark": self.enable_quark,
        }
        return [key for key, on in mapping.items() if on]


class FilterSectionConfig(PluginConfigBase):
    __ui_label__ = "过滤与兴趣"
    __ui_order__ = 2
    blocklist: list[str] = Field(
        default_factory=lambda: list(hot_core.DEFAULT_BLOCKLIST),
        description="最高优先级：命中即拦截，这些内容永远不会出现在返回里",
        json_schema_extra={
            "label": "雷区关键词",
            "hint": "可自由增删；填了就绝对生效，优先级压过一切",
        },
    )
    interests: list[str] = Field(
        default_factory=list,
        description="命中的话题会被置顶并标注 ★",
        json_schema_extra={
            "label": "人设兴趣关键词",
            "hint": "例：卡拉彼丘、二次元、原神、游戏。命中即置顶，是'她的口味'",
            "placeholder": "输入一个关键词后回车",
        },
    )
    max_items: int = Field(
        default=10, ge=3, le=30,
        description="单次返回的条目上限",
        json_schema_extra={"label": "返回条数上限"},
    )
    strict_interests: bool = Field(
        default=False,
        description="全局白名单：开启后所有路径（热榜/漫步/订阅）只保留命中兴趣词的内容；关闭则命中置顶、其余保留",
        json_schema_extra={
            "label": "全局白名单（只收兴趣命中）",
            "hint": "默认关闭——真人刷热搜是'都扫一眼'；开启=她只'刷到'兴趣相关的内容（与雷区黑名单相对）",
        },
    )


class WatchSectionConfig(PluginConfigBase):
    __ui_label__ = "订阅查询词"
    __ui_order__ = 3
    queries: list[str] = Field(
        default_factory=list,
        description="关注的话题而不是账号：每轮随机挑一个词全网搜索，结果作为'刷到'素材",
        json_schema_extra={
            "label": "订阅查询词",
            "hint": "例：卡拉彼丘 最新动态。不上热搜的小众圈子靠这个刷到；需要填下方 API 密钥",
        },
    )
    ds_api_key: str = Field(
        default="",
        description="DeepSeek API 密钥（留空则订阅查询功能不生效，不影响其他功能）",
        json_schema_extra={"label": "DeepSeek API 密钥", "hint": "不填的话「订阅查询词」不会生效，热榜等其他功能不受影响"},
    )
    ds_base_url: str = Field(
        default="https://api.deepseek.com/anthropic",
        description="DeepSeek Anthropic 兼容接口地址",
        json_schema_extra={"label": "接口地址（一般不用改）"},
    )
    ds_model: str = Field(
        default="deepseek-v4-flash",
        description="搜索用的模型",
        json_schema_extra={"label": "搜索模型"},
    )
    ds_max_uses: int = Field(
        default=2, ge=1, le=5,
        description="单次搜索的最大次数（越大越全面，费用越高）",
        json_schema_extra={"label": "单次搜索次数上限"},
    )
    ds_timeout: float = Field(
        default=60.0, ge=15.0, le=180.0,
        description="单次搜索超时（秒）",
        json_schema_extra={"label": "搜索超时（秒）"},
    )
    max_age_days: int = Field(
        default=30, ge=0, le=365,
        description="新鲜度校验：结果里日期超过这个天数的条目会被丢弃（0=不校验）",
        json_schema_extra={
            "label": "新鲜度阈值（天）",
            "hint": "搜索结果要求带日期，插件按日期自动过滤旧闻；0 关闭校验",
        },
    )


class AmbientSectionConfig(PluginConfigBase):
    __ui_label__ = "随机漫步"
    __ui_order__ = 4
    enabled: bool = Field(
        default=False,
        description="模拟真人刷手机：定期'刷到'非头条内容，之后聊到相关话题能自然想起",
        json_schema_extra={"label": "启用随机漫步", "order": 0},
    )
    mode: Literal["random", "llm"] = Field(
        default="random",
        description="random=纯随机挑；llm=按人设挑'她会点进去看的'",
        json_schema_extra={"label": "筛选模式", "hint": "random 免费；llm 更贴合人设但要消耗少量 token"},
    )
    interval_minutes: int = Field(
        default=90, ge=1, le=720,
        description="采样间隔（分钟），建议 60~180，别太频繁",
        json_schema_extra={"label": "采样间隔（分钟）", "hint": "测试时可调到 1；日常建议 360~720，太频繁会像刷屏机器人"},
    )
    sample_count: int = Field(
        default=3, ge=1, le=5,
        description="每次刷到的条数",
        json_schema_extra={"label": "每次刷到条数"},
    )
    skip_top: int = Field(
        default=3,
        description="跳过榜单前几名（榜首人人都看得到，不算'刷到'）",
        json_schema_extra={"label": "跳过榜首条数"},
    )
    pool_platforms: list[str] = Field(
        default_factory=lambda: ["bili", "douyin"],
        description="随机漫步从这些平台的热榜里'刷到'内容，支持中文或拼音",
        json_schema_extra={
            "label": "采样池平台",
            "hint": "可填：B站、抖音、微博、知乎、头条、贴吧（中文拼音均可），默认 B站+抖音",
        },
    )
    include_watch: bool = Field(
        default=True,
        description="把订阅查询词也纳入采样——小众圈子的动态不上热榜，全靠这个才刷得到",
        json_schema_extra={"label": "同时从订阅查询词采样"},
    )
    llm_task: str = Field(
        default="utils",
        description="LLM 模式使用的宿主模型任务名（utils/replyer 等）",
        json_schema_extra={"label": "LLM 任务名"},
    )
    group_ids: list[str] = Field(
        default_factory=list,
        description="直接填 QQ 群号——刷到的内容会自动注入这些群，她聊到相关话题时能自然想起",
        json_schema_extra={
            "label": "QQ 群号（推荐）",
            "hint": "填群号即可，不用查 stream_id；这是'她刷到过'的主要入口",
        },
    )
    chat_blocklist: list[str] = Field(
        default_factory=list,
        description="按聊天设置黑名单：格式「群号或QQ号:关键词1,关键词2」。命中的内容不注入该聊天",
        json_schema_extra={
            "label": "聊天黑名单（可选）",
            "hint": "例：210589508:政治,八卦。与全局雷区叠加生效（全局雷区先行）",
        },
    )
    group_topics: list[str] = Field(
        default_factory=list,
        description="按聊天定制内容：格式「群号或QQ号:关键词1,关键词2」。对应的群/私聊只会注入命中关键词的'刷到'内容",
        json_schema_extra={
            "label": "聊天内容定制（可选）",
            "hint": "例：210589508:卡拉彼丘,游戏 或 123456(某人的QQ号):二次元。命中才注入；不配置的聊天收全部内容",
        },
    )
    apply_to_all: bool = Field(
        default=False,
        description="全局生效模式：开启后无需填写群号/私聊号，'刷到'自动应用到她所有的群聊和私聊",
        json_schema_extra={
            "label": "全局生效模式（啥都不用填）",
            "hint": "开启后自动覆盖她所有的群聊和私聊；聊天内容定制/黑名单仍可按群号或QQ号单独配置。默认关闭",
        },
    )
    private_ids: list[str] = Field(
        default_factory=list,
        description="填 QQ 号——刷到的内容同样注入和这些人的私聊（前提：和她有过私聊会话）",
        json_schema_extra={
            "label": "私聊 QQ 号（可选）",
            "hint": "填对方 QQ 号即可；只支持聊过天的对象（没聊过的先私聊一句建立会话）",
        },
    )
    style_hint: str = Field(
        default="",
        description="她提起'刷到'内容时的表达约束（选填）。例：谈论游戏或社区话题时保持客观友善，吐槽点到为止，不贬低官方或任何玩家群体",
        json_schema_extra={
            "label": "表达约束（选填）",
            "hint": "留空=不限制，她想怎么说就怎么说；填了会作为软约束附加到注入和主动提起里",
        },
    )
    proactive_chance: int = Field(
        default=50, ge=0, le=100,
        description="每次'刷到'后尝试主动提起的概率（%）。0=不主动；100=每次都尝试唤醒。建议 30~60，太高会显得刻意",
        json_schema_extra={
            "label": "主动提起概率（%）",
            "hint": "刷到了也不一定说——按这个概率决定要不要尝试开话题。0=从不主动",
        },
    )
    proactive_priority: Literal["low", "normal", "high"] = Field(
        default="low",
        description="写入意图的优先级：low=轻声建议（Planner 常因气氛不合适沉默）；normal/high 更容易真的开口",
        json_schema_extra={
            "label": "主动提起优先级",
            "hint": "想让她更容易主动聊，改 normal 或 high（太高的代价是可能显得突兀）",
        },
    )
    proactive_target_count: int = Field(
        default=0, ge=0, le=50,
        description="每次主动提起覆盖几个群：0=所有已填的群都尝试；N=随机挑 N 个",
        json_schema_extra={
            "label": "主动提起群数",
            "hint": "0=全部群都尝试（更活跃）；填 1~2 则每次随机挑几个（更克制）",
        },
    )
    proactive_groups: list[str] = Field(
        default_factory=list,
        description="填 QQ 群号（可多个）——刷到内容后随机挑一个群写入意图唤醒 Planner，是否主动开口由她自己决定",
        json_schema_extra={
            "label": "允许她主动提起（可选）",
            "hint": "可填多个群号；每次覆盖几个群由上方「主动提起群数」决定（0=全部）。没人跟她说话时她也可能自己开话题，慎用",
        },
    )


class DebugSectionConfig(PluginConfigBase):
    __ui_label__ = "调试"
    __ui_order__ = 4
    log_fetch: bool = Field(
        default=False,
        description="开启后，抓取结果末尾会附上诊断统计（原始/保留/兴趣命中/雷区拦截条数）",
        json_schema_extra={"label": "抓取诊断（验证过滤效果）"},
    )


class HotTopicsConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    sources: SourcesSectionConfig = Field(default_factory=SourcesSectionConfig)
    filter: FilterSectionConfig = Field(default_factory=FilterSectionConfig)
    custom: WatchSectionConfig = Field(default_factory=WatchSectionConfig)
    ambient: AmbientSectionConfig = Field(default_factory=AmbientSectionConfig)
    debug: DebugSectionConfig = Field(default_factory=DebugSectionConfig)


# ---------------- 插件本体 ----------------

class HotTopicsPlugin(MaiBotPlugin):
    @API('hot_topics.read', description='只读获取已配置来源的兴趣过滤素材，不主动发言', public=True)
    async def read_api(self, platform: str = 'bili', **kwargs):
        import asyncio
        key = hot_core.resolve_platform(platform)
        if not self.config.plugin.enabled or key not in self.config.sources.enabled_keys():
            return {'success': False, 'error': 'source disabled or invalid'}
        label, material = await asyncio.to_thread(self._get_one_platform, key)
        return {'success': bool(material), 'source': label, 'material': (material or '')[:10000]}

    config_model = HotTopicsConfig

    async def on_load(self) -> None:
        import asyncio

        self.ctx.logger.info("热榜感知插件已加载（v%s）", PLUGIN_VERSION)
        self._ambient_task = asyncio.create_task(self._ambient_loop())

    async def on_unload(self) -> None:
        import asyncio

        task = getattr(self, "_ambient_task", None)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """配置热重载时执行（本插件每轮循环都重读配置，无需额外处理）。"""
        del scope, config_data, version

    # ---- 随机漫步（模拟真人刷到） ----

    def _seen_file_path(self):
        from pathlib import Path

        return Path(self.ctx.paths.data_dir) / "ambient_seen.json"

    def _load_seen(self) -> list[dict]:
        try:
            import json

            with open(self._seen_file_path(), encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception:  # noqa: BLE001 - 文件不存在/损坏时从空开始
            return []

    def _save_seen(self, entries: list[dict]) -> None:
        import json

        with open(self._seen_file_path(), "w", encoding="utf-8") as f:
            json.dump(entries[-10:], f, ensure_ascii=False, indent=1)

    async def _get_bot_persona(self) -> str:
        """通过官方能力读取麦麦人设摘要（合规通道，不碰宿主文件）。

        读不到就返回空串，LLM 筛选自动回退为纯兴趣关键词模式。
        """
        parts: list[str] = []
        for key in ("personality.personality", "personality.reply_style"):
            try:
                value = await self.ctx.config.get(key, "")
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
            except Exception:  # noqa: BLE001 - 能力不可用/键不存在都不致命
                continue
        return " ".join(parts)[:500]

    async def _llm_curate(self, candidates: list[dict], n: int) -> list[dict]:
        """LLM 模式：按人设挑'她会点进去看'的条目；失败则回退纯随机。"""
        persona = await self._get_bot_persona() or "一个普通年轻用户"
        titles = "\n".join(f"{i}. {c['title']}" for i, c in enumerate(candidates, 1))
        prompt = (
            f"角色人设：{persona}\n\n"
            f"以下是刷到的一些内容标题：\n{titles}\n\n"
            f"从里面挑出这个人设最可能点进去看的 {n} 条，"
            f"只返回编号，格式如：1,4,7"
        )
        try:
            result = await self.ctx.llm.generate(prompt, model=self.config.ambient.llm_task)
            if not result.get("success"):
                raise RuntimeError(result.get("error", "llm 调用失败"))
            import re

            picked: list[dict] = []
            for num in re.findall(r"\d+", str(result.get("response", ""))):
                idx = int(num) - 1
                if 0 <= idx < len(candidates) and candidates[idx] not in picked:
                    picked.append(candidates[idx])
                if len(picked) >= n:
                    break
            return picked or candidates[:n]
        except Exception as exc:  # noqa: BLE001 - LLM 失败回退随机
            self.ctx.logger.warning("LLM 筛选失败，回退随机采样：%s", exc)
            import random as _random

            return _random.sample(candidates, min(n, len(candidates)))

    def _parse_group_topics(self, key_field: str = "group_topics") -> dict[str, list[str]]:
        """解析聊天定制配置：['群号或QQ号:词1,词2', ...] -> {ID: [词1, 词2]}。"""
        result: dict[str, list[str]] = {}
        for entry in getattr(self.config.ambient, key_field, []):
            entry = str(entry).strip()
            if not entry or ("=" not in entry and ":" not in entry and "：" not in entry):
                continue
            gid, _, kw_part = re.split(r"[:：]", entry, 1)[0], None, re.split(r"[:：]", entry, 1)[1]
            gid = gid.strip()
            kws = [k.strip() for k in re.split(r"[,，]", kw_part) if k.strip()]
            if gid and kws:
                result[gid] = kws
        return result

    async def _wechat_target_stream(self, target: str, *, group: bool):
        """Resolve typed targets without treating WeChat IDs as QQ accounts."""
        value = str(target).strip()
        if value.startswith('qq:') and len(value.split(':')) == 5:
            from utils.platform_target import parse_target, resolve_configured_stream
            if (parse_target(value)['kind']=='group') != group:
                return None
            return await resolve_configured_stream(self.ctx.chat, value)
        parts = value.split(":", 2)
        if len(parts) == 3 and parts[1] in {"group", "private"}:
            platform, kind, value = parts
            if (kind == "group") != group:
                return None
        elif len(parts) == 2 and parts[0] in {"wechat", "qq"}:
            platform, value = parts
        else:
            platform = "wechat" if value.endswith("@chatroom") or value.startswith("wxid_") else "qq"
        resolver = self.ctx.chat.get_stream_by_group_id if group else self.ctx.chat.get_stream_by_user_id
        return await resolver(value, platform=platform)

    async def _resolve_target_streams(self) -> tuple[list[str], dict[str, str]]:
        """把配置里的 QQ 群号/私聊 QQ 号解析成聊天流 ID，并返回 stream_id->群号 映射。"""
        targets: list[str] = []
        sid_to_gid: dict[str, str] = {}
        # 全局生效模式：自动发现所有聊天流（无需手动填列表）
        if getattr(self.config.ambient, "apply_to_all", False):
            try:
                streams = await self.ctx.chat.get_all_streams("all_platforms")
                for s in streams or []:
                    if isinstance(s, dict):
                        sid = s.get("stream_id") or s.get("session_id") or s.get("id")
                        gid = str(s.get("group_id") or s.get("user_id") or "").strip()
                    else:
                        sid = getattr(s, "stream_id", None) or getattr(s, "session_id", None)
                        gid = str(getattr(s, "group_id", "") or getattr(s, "user_id", "") or "").strip()
                    if sid:
                        sid = str(sid)
                        targets.append(sid)
                        if gid:
                            sid_to_gid[sid] = gid
                if targets:
                    self.ctx.logger.info("全局生效模式：发现 %d 个聊天流", len(targets))
                    return list(dict.fromkeys(targets)), sid_to_gid
                self.ctx.logger.warning("全局生效模式未发现任何聊天流，回退手动列表")
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("全局生效模式失败，回退手动列表：%s", exc)
        for gid in self.config.ambient.group_ids:
            gid = str(gid).strip()
            if not gid:
                continue
            try:
                stream = await self._wechat_target_stream(gid, group=True)
                sid = None
                if isinstance(stream, dict):
                    sid = stream.get("stream_id") or stream.get("session_id") or stream.get("id") or stream.get("stream")
                else:
                    sid = getattr(stream, "stream_id", None) or getattr(stream, "id", None)
                if sid:
                    targets.append(str(sid))
                    sid_to_gid[str(sid)] = gid
                else:
                    self.ctx.logger.warning("群 %s 未解析到 stream_id：%r", gid, stream)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("解析群 %s 的聊天流失败：%s", gid, exc)
        for uid in self.config.ambient.private_ids:
            uid = str(uid).strip()
            if not uid:
                continue
            try:
                stream = await self._wechat_target_stream(uid, group=False)
                sid = None
                if isinstance(stream, dict):
                    sid = stream.get("stream_id") or stream.get("session_id") or stream.get("id") or stream.get("stream")
                else:
                    sid = getattr(stream, "stream_id", None) or getattr(stream, "id", None)
                if sid:
                    targets.append(str(sid))
                    sid_to_gid[str(sid)] = uid
                else:
                    self.ctx.logger.warning("私聊 %s 未解析到 stream_id：%r", uid, stream)
            except Exception as exc:  # noqa: BLE001
                self.ctx.logger.warning("解析私聊 %s 的聊天流失败：%s", uid, exc)
        return list(dict.fromkeys(targets)), sid_to_gid  # 去重保序

    async def _deliver_ambient(self, picked: list[dict], label: str) -> None:
        """把'刷到'的内容投递出去：静默注入上下文（按群定制内容）/ 让她自己主动提起。"""
        cfg = self.config.ambient
        import re as _re

        # 静默注入上下文：她"自己知道刷到了"，聊到相关话题可自然想起
        inject_targets, sid_to_gid = await self._resolve_target_streams()
        if inject_targets:
            import time as _time

            now_str = _time.strftime("%m月%d日 %H:%M")
            hint = str(cfg.style_hint or "").strip()
            topics_map = self._parse_group_topics()
            block_map = self._parse_group_topics(key_field="chat_blocklist")
            all_titles = [p["title"] for p in picked]
            for sid in inject_targets:
                # 聊天内容定制（白名单）：配置了关键词的聊天只注入命中内容
                gid = sid_to_gid.get(sid)
                kws = topics_map.get(gid, []) if gid else []
                titles = hot_core.filter_by_keywords(all_titles, kws)
                # 聊天黑名单：命中的内容不注入该聊天
                blk = block_map.get(gid, []) if gid else []
                titles = hot_core.remove_by_keywords(titles, blk)
                if not titles:
                    self.ctx.logger.info(
                        "注入跳过（%s）：本群内容定制关键词无一命中", gid or sid
                    )
                    continue
                inject_text = (
                    f"（系统提示：{now_str}你在{label}刷到了这些内容，"
                    f"是随缘刷到的，不一定聊得上，但如果聊天中涉及可以自然提起；"
                    f"提起时可以用'我之前刷到过'这类说法）"
                    + "；".join(titles)
                )
                if hint:
                    inject_text += f"\n（表达约束：{hint}）"
                try:
                    await self.ctx.maisaka.context.append(
                        sid,
                        [{"type": "text", "content": inject_text}],
                        visible_text="",
                        source_kind="plugin",
                    )
                except Exception as exc:  # noqa: BLE001
                    self.ctx.logger.warning("上下文注入失败（%s）：%s", sid, exc)

        # 主动提起：按概率决定要不要尝试，写入意图唤醒 Planner，开不开口由她自己决定
        if cfg.proactive_groups:
            chance = max(0, min(100, int(cfg.proactive_chance)))
            if random.random() * 100 >= chance:
                self.ctx.logger.info(
                    "主动提起未触发（本次概率未命中 %d%%）", chance
                )
            else:
                candidates = [str(g).strip() for g in cfg.proactive_groups if str(g).strip()]
                random.shuffle(candidates)
                count = max(0, int(cfg.proactive_target_count))
                selected = candidates if count == 0 else candidates[:count]
                hint = str(cfg.style_hint or "").strip()
                intent = (
                    f"你刚才在{label}刷到了：{'、'.join(p['title'] for p in picked)}。"
                    f"如果适合当下的气氛，可以自然地聊聊其中一两个；不合适就不说。"
                )
                if hint:
                    intent += f"\n表达约束：{hint}"
                for gid in selected:
                    sid = None
                    try:
                        stream = await self._wechat_target_stream(gid, group=True)
                        if isinstance(stream, dict):
                            sid = stream.get("stream_id") or stream.get("session_id") or stream.get("id") or stream.get("stream")
                        else:
                            sid = getattr(stream, "stream_id", None) or getattr(stream, "id", None)
                    except Exception as exc:  # noqa: BLE001
                        self.ctx.logger.warning("主动提起解析群 %s 失败：%s", gid, exc)
                    if not sid:
                        continue
                    try:
                        await self.ctx.maisaka.proactive.trigger(
                            sid,
                            intent,
                            reason="热榜插件随机漫步刷到内容",
                            priority=str(cfg.proactive_priority or "low"),
                        )
                        self.ctx.logger.info(
                            "主动提起已写入意图（群 %s，优先级 %s）", gid, cfg.proactive_priority
                        )
                    except Exception as exc:  # noqa: BLE001
                        self.ctx.logger.warning("主动触发失败（群 %s）：%s", gid, exc)

    async def _ambient_loop(self) -> None:
        """后台低频循环：定期'刷到'一些内容存入记忆缓冲。"""
        import asyncio
        import time as _time

        await asyncio.sleep(20)  # 等插件完全启动
        while True:
            try:
                cfg = self.config.ambient
                if cfg.enabled:
                    pool = []
                    for name in cfg.pool_platforms:
                        key = hot_core.resolve_platform(str(name))
                        if key and key not in pool:
                            pool.append(key)
                    # 订阅查询词：全网搜索关注的话题（小众圈子不上热榜靠这个刷到）
                    watch = self.config.custom
                    watch_queries = [q for q in watch.queries if str(q).strip()] if watch.ds_api_key.strip() else []
                    if watch_queries and cfg.include_watch and (not pool or random.random() < 0.5):
                        # 轮换制：保证每条订阅查询词都能被均匀搜到，不靠运气
                        idx = getattr(self, "_watch_idx", 0) % len(watch_queries)
                        query = watch_queries[idx].strip()
                        self._watch_idx = idx + 1
                        try:
                            text = hot_core.search_deepseek(
                                query,
                                api_key=watch.ds_api_key.strip(),
                                base_url=watch.ds_base_url,
                                model=watch.ds_model,
                                max_uses=watch.ds_max_uses,
                                timeout=watch.ds_timeout,
                            )
                            import re as _re

                            lines = []
                            for ln in text.splitlines():
                                ln = (
                                    ln.strip()
                                    .lstrip("-•· ")
                                    .strip()
                                    .replace("**", "")
                                    .replace("##", "")
                                    .strip()
                                )
                                if not ln or len(ln) <= 6:
                                    continue
                                # 过滤模型的开场白/汇总句（不是实质内容）
                                if _re.search(r"以下是|检索结果|汇总[:：]?$|要点[:：]?$|近期动态与热门讨论", ln):
                                    continue
                                lines.append(ln)
                            seen_titles = {t for e in self._load_seen() for t in e.get("titles", [])}
                            picked = [ln for ln in lines if ln not in seen_titles]
                            # 雷区 + 兴趣 + 严格模式：与热榜同规则，全局生效
                            watch_items, blocked_n = hot_core.filter_and_rank(
                                [{"title": ln} for ln in picked],
                                blocklist=self.config.filter.blocklist,
                                interests=self.config.filter.interests,
                                max_items=cfg.sample_count,
                                strict=self.config.filter.strict_interests,
                            )
                            picked = [w["title"] for w in watch_items]
                            picked, stale = hot_core.filter_by_freshness(
                                picked, watch.max_age_days
                            )
                            picked = picked[: cfg.sample_count]
                            if stale or blocked_n:
                                self.ctx.logger.info(
                                    "订阅查询「%s」过滤：雷区拦截 %d 条，新鲜度丢弃 %d 条",
                                    query, blocked_n, stale,
                                )
                            if picked:
                                label = "搜索"
                                seen = self._load_seen()
                                seen.append(
                                    {
                                        "time": _time.strftime("%m-%d %H:%M"),
                                        "source": label,
                                        "titles": picked,
                                    }
                                )
                                self._save_seen(seen)
                                self.ctx.logger.info(
                                    "随机漫步（订阅查询「%s」）刷到 %d 条：%s",
                                    query, len(picked), "、".join(picked)[:100],
                                )
                                await self._deliver_ambient(
                                    [{"title": t} for t in picked], label
                                )
                            else:
                                self.ctx.logger.info("订阅查询「%s」无新内容", query)
                        except Exception as exc:  # noqa: BLE001
                            self.ctx.logger.warning("订阅查询采样失败（%s）：%s", query, exc)
                        await asyncio.sleep(self.config.ambient.interval_minutes * 60)
                        continue
                    random.shuffle(pool)
                    for key in pool:
                        try:
                            items = hot_core.fetch_platform(
                                key,
                                base_urls=self.config.sources.api_base_urls,
                                timeout=self.config.sources.request_timeout,
                            )
                        except Exception:  # noqa: BLE001 - 池内单平台失败换下一个
                            continue
                        # 雷区全局生效：漫步采样的内容同样过雷区
                        items, blocked_n = hot_core.blocklist_filter(
                            items, self.config.filter.blocklist
                        )
                        if blocked_n:
                            self.ctx.logger.info(
                                "随机漫步采样（%s）雷区拦截 %d 条", key, blocked_n
                            )
                        if self.config.filter.strict_interests:
                            items = hot_core.interest_strict_filter(
                                items, self.config.filter.interests
                            )
                            if not items:
                                continue
                        candidates = hot_core.random_sample(
                            items, cfg.sample_count * 3, skip_top=cfg.skip_top
                        )
                        if cfg.mode == "llm" and len(candidates) > cfg.sample_count:
                            picked = await self._llm_curate(candidates, cfg.sample_count)
                        else:
                            picked = candidates[: cfg.sample_count]
                        label = hot_core.SOURCES[key][0]
                        seen = self._load_seen()
                        seen.append(
                            {
                                "time": _time.strftime("%m-%d %H:%M"),
                                "source": label,
                                "titles": [p["title"] for p in picked],
                            }
                        )
                        self._save_seen(seen)
                        self.ctx.logger.info(
                            "随机漫步（%s）刷到 %d 条：%s",
                            label, len(picked), "、".join(p["title"] for p in picked)[:100],
                        )
                        await self._deliver_ambient(picked, label)
                        break  # 本轮只刷一个平台
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台任务永不致命
                self.ctx.logger.warning("随机漫步任务异常：%s", exc)
            await asyncio.sleep(self.config.ambient.interval_minutes * 60)

    # ---- 内部工具方法 ----

    def _get_one_platform(self, key: str) -> tuple[str, str | None]:
        """抓单平台并格式化为素材，返回 (label, text或错误)。"""
        label = hot_core.SOURCES[key][0]
        try:
            import time

            start = time.monotonic()
            items = hot_core.fetch_platform(
                key,
                base_urls=self.config.sources.api_base_urls,
                timeout=self.config.sources.request_timeout,
            )
            cost = (time.monotonic() - start) * 1000
            if self.config.debug.log_fetch:
                self.ctx.logger.info("热榜抓取 %s：%d 条，%.0fms", label, len(items), cost)
        except Exception as exc:  # noqa: BLE001 - 单源失败不影响整体
            self.ctx.logger.warning("热榜抓取失败（%s）：%s", label, exc)
            return label, None
        kept, blocked = hot_core.filter_and_rank(
            items,
            blocklist=self.config.filter.blocklist,
            interests=self.config.filter.interests,
            max_items=self.config.filter.max_items, strict=self.config.filter.strict_interests,
        )
        hits = hot_core.count_interest_hits(kept, self.config.filter.interests)
        # 兴趣/雷区统计始终写入日志，方便验证过滤是否真的生效
        self.ctx.logger.info(
            "热榜过滤 %s：原始%d条 → 保留%d条，兴趣命中%d条，雷区拦截%d条",
            label,
            len(items),
            len(kept),
            len(hits),
            len(blocked),
        )
        footer = ""
        if self.config.debug.log_fetch:
            footer = f"\n（诊断：原始{len(items)}条 → 保留{len(kept)}条；兴趣命中{len(hits)}条；雷区拦截{len(blocked)}条）"
        return label, hot_core.format_material(label, kept, self.config.filter.interests, len(blocked)) + footer

    def _gather(self, platform_key: str | None) -> str:
        cfg = self.config
        if platform_key and platform_key != "all":
            label, text = self._get_one_platform(platform_key)
            return text or f"{label}暂时抓不到（接口抖动或上游故障），可以稍后再试。"

        # all：遍历启用的平台
        parts: list[str] = []
        for key in cfg.sources.enabled_keys():
            label, text = self._get_one_platform(key)
            if text:
                parts.append(text)
        if not parts:
            return "所有热榜源暂时都抓不到，可能是网络或接口问题，稍后再试。"

        # 附上最近"刷到"的模糊印象（随机漫步缓冲）
        if self.config.ambient.enabled:
            seen = self._load_seen()[-3:]
            if seen:
                seen_lines = ["\n另外，最近一两天你自己在各个平台刷到过这些（模糊印象，可以自然提起）："]
                for entry in seen:
                    seen_lines.append(f"- {entry['time']} {entry['source']}：{'、'.join(entry['titles'])}")
                parts.append("\n".join(seen_lines))
        return "\n\n".join(parts)

    # ---- Tool：给 Planner 调用 ----

    @Tool(
        "get_hot_topics",
        description=(
            "获取当前全网热榜（微博/知乎/抖音/B站/贴吧等）。"
            "当用户问'最近有什么热点/热搜/大家都在聊什么/有什么新闻'，"
            "或聊天需要时效性话题时调用；单纯闲聊不需要调用。"
            "调用后你会拿到素材，请用你自己的话转述，"
            "千万不要把工具名或'工具调用'之类的字样写进回复。"
        ),
        parameters=[
            ToolParameterInfo(
                name="platform",
                param_type=ToolParamType.STRING,
                description=(
                    "平台名（微博/知乎/抖音/头条/bili/贴吧等），"
                    "留空或'all'返回全部启用平台的精选"
                ),
                required=False,
            ),
        ],
    )
    async def handle_get_hot_topics(self, platform: str = "", **kwargs: Any):
        del kwargs
        if not self.config.plugin.enabled:
            return {"name": "get_hot_topics", "content": "热榜插件已在配置中关闭。"}
        key = hot_core.resolve_platform(platform)
        if platform.strip() and key is None:
            return {
                "name": "get_hot_topics",
                "content": (
                    f"不支持的平台「{platform}」。"
                    f"可用：{', '.join(label for _, label in hot_core.SOURCES.values())}，或不传平台名看精选。"
                ),
            }
        content = self._gather(key)
        hint = (
            "\n（以上是你'刷到'的素材，用你自己的话自然转述即可；"
            "不要提到工具名，也不要输出'工具调用'、'【工具调用】'之类的字样。）"
        )
        return {"name": "get_hot_topics", "content": content + hint}

    # ---- Command：手动自检 ----

    @Command(
        "hot_topics_test",
        description="手动测试热榜抓取：/hot_topics_test [平台名]（大小写不限）",
        pattern=r"(?i)^/hot_topics_test(?:\s+.+)?$",
    )
    async def handle_hot_topics_test(self, stream_id: str = "", **kwargs: Any):
        text = str(kwargs.get("text") or "").strip()
        # 大小写不敏感：把命令前缀归一小写后再剥离参数
        arg = text.lower().removeprefix("/hot_topics_test").strip()
        platform = arg.split()[0] if arg.split() else ""
        key = hot_core.resolve_platform(platform) if platform else "all"
        if platform and key is None:
            await self.ctx.send.text(
                f"不支持的平台「{platform}」，可用：微博/知乎/抖音/bili/贴吧 等", stream_id
            )
            return True, f"不支持的平台：{platform}", True
        got_text = self._gather(key)
        await self.ctx.send.text(got_text[:2500], stream_id)
        return True, "热榜抓取测试完成", True


def create_plugin() -> HotTopicsPlugin:
    """创建插件实例（MaiBot 插件运行时入口）。"""
    return HotTopicsPlugin()
