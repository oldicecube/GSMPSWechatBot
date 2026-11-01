# 同名群跨端见闻

原生 SDK 插件，仅给 Planner、Replyer 和自主生活提供有来源的参考，不建立新的发言调度器。

`bridge.targets` 使用微信 chatroom ID 和 `qq:账号:连接:group:群号`。双方必须是已授权、唯一匹配、群名完全相同、原生回复频率大于零的群；私聊、其他账号、其他连接和同名歧义均不共享。当前只配置 GDUT SIE Minecraft Public Server 的微信／QQ 两端。

- `refresh_seconds=300`：近期见闻缓存五分钟；每次使用缓存仍复核群身份和开关。
- `message_limit=8`、`history_hours=12`、`context_chars=2400`：限制取材与注入规模。只取真人文字，排除 bot、系统事件、隐藏消息、过期和重复内容。保留来源并生成合法 JSON，不把旁群消息插入本群 User 历史。
- `automatic_memory=true`、`memory_limit=3`：根据旁群近期话题补充该旁群的长期记忆。
- `memory_timeout_seconds=1.5`：自动记忆查询超时就保留近期见闻；不为此额外调用生成模型。

`peer_group_context(query=...)` 的可选记忆查询准确定位对端 session。`knowledge.search(include_shared=False)` 阻止宿主的共享范围再扩展；只有宿主返回匹配的 `search_scope` 确认才接纳结果。旧进程没有该协议时安全降级，需用户手动重启核心加载更新，不把范围未确认的资料标作旁群记忆。

这些参考允许麦麦在话题相关时自然提及“隔壁 QQ／微信那边”，并不要求每轮提及或主动转发；不能把对方的话归给当前群。参考不提供消息定位 ID，不授予跨群 @、禁言或撤回权限。人物关联只认游戏账户绑定，不凭昵称判断。

验收：`scripts/community_context_acceptance.py`、`scripts/linked_identity_acceptance.py`；生产数据库副本和真实宿主读取能力的只读核验见 `scripts/community_runtime_audit.py`。
