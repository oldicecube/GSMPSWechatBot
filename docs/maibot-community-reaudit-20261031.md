# 双端身份、旁群记忆与自主生活复验（2026-10-31）

本轮补齐实际发现的遗漏，代码与隔离验收通过。主进程 PID 13748 仍于 2026-10-30 23:58:45 启动，未由 agent 重启。核心模块修复须用户手动重启才能完整生效，不能将代码验收等同于当前旧进程已全部加载。未修改 `/player`，未在真实群发送测试消息、禁言、撤回或扣分，未修改实际用户绑定。

## 1. QQ 禁言与撤回

只读在线检查确认：机器人 3294652115 在线，在指定 QQ 群 1020993873 的角色为管理员，`mute`、`recall_message`、`forward_messages` 工具已启用。原生会话已有真实双向消息。

LLM 可以自行提出禁言对象与时长；工具先验证群白名单、实际权限、受保护成员及正整数时长，再将时长限制在配置的 60–2592000 秒（1 分钟至 30 天）。并非只使用一个固定时长。撤回支持实际平台消息 ID，能请求撤回自己或其他人的消息，最终受 QQ 角色和撤回时限约束；平台拒绝时明确返回失败。微信侧不支持禁言/群管理撤回，已有适配器过滤，不将这些字段作为正文发送。

上述授权与拒绝路径使用模拟适配器测试，线上只读确认工具、权限与历史，没有实际处罚任何人。

## 2. 已绑定身份、画像及抗昵称变化

- 在生产 SQLite 的只读快照上，实际账户 `oldicecube` 的 QQ 2103203812 与微信 wxid_0x1whr7w1tmv22 正确映射到同一绑定身份。
- 两端原生人物记录仍按 `md5(platform + '_' + user_id)` 索引，不按昵称。检查全部 73 条人物记录，索引正确；改显示名的测试不创建新人物或切断绑定。
- Planner 身份说明明确“同一人的不同平台账号、不同群的独立会话”。关联人物画像和记忆保留平台来源，不把另一端的发言冒充成本群即时对话。
- 修复 `src/maisaka/memory/person_profile.py` 的整块失败问题：本端画像、别名解析、对端画像逐项隔离，一端失败不再丢失另一端可用画像。
- `/bind` 已有另一端用户名和缓存真实印象回执；无印象时返回“暂无足够印象”，不临时调用 LLM 编造。修正验收脚本旧夹具，确保正式数据已有两端绑定时，第三个同平台账号仍被拒绝。

生产快照构造的是当前代码的真实提示词；运行中旧核心尚未加载本轮修复。最近 QQ Planner 预览已能看到关联身份提示，但不能据此宣称每轮都已经注入全部新能力。

## 3. 同名双群的上下文与记忆

严格配对以下公开群，并要求双方启用主动聊天：

- 微信：47447788043@chatroom。
- QQ：qq:3294652115:napcat-local:group:1020993873。
- 名称：GDUT SIE Minecraft Public Server。

两端原生 session 保持独立。`community_context` 为 Planner、Replyer 和自主生活提供独立的有来源参考，允许相关时自然提起隔壁见闻；没有另起一个广播定时器。保留平台/群名、说话人、时间，明确非当前会话，不携带可用于 @、撤回、回复定位或禁言的对端操作 ID；人物是否同一人只认已验证绑定。

本轮修复：

1. `peer_group_context(query)` 原来向当前群查记忆，现改为实际对端 session。
2. 自动参考增加最近话题的对端长期记忆；单次可选查询 1.5 秒，失败保留近期上下文，缓存刷新由 20 分钟缩短至 5 分钟。
3. 新增 `knowledge.search(include_shared=False)`，贯穿 capability、MemoryService、A_memorix host；阻止默认共享检索把其他群结果冒充指定对端记忆。默认查询行为保持兼容。
4. 宿主须回传匹配的 `search_scope` 确认。旧核心没有该确认时，插件安全退化为近期见闻，等待用户重启；不会将范围未知的记忆标为指定群来源。
5. 参考长度裁剪保留完整来源政策与有效 JSON，优先留下最新消息。

使用生产数据库快照和真实原生 `message.get_recent` 等 capability，两端均成功取得带正确来源的对端参考。向量检索的范围控制、超时与旧宿主降级另由隔离测试验证，未用生产模型接口制造请求。

## 4. Sleep 跨端和跨群

原有实现已按绑定后的规范身份保存状态，并在任意开启 sleep 的群检查稳定 wxid/QQ 号；普通文字及媒体消息均可触发，不局限最初入睡群。入睡、奖励、违约结算使用事务，避免两端同时消息重复结算；系统消息、机器人事件不处罚。

微信“26果园T&M动漫社”已按真实群 ID **47799335844@chatroom** 开启 sleep 自动处理；其他插件与 LLM 没有顺带开启。跨端处罚仍遵守各平台能力，微信不输出 QQ 专属禁言字段。

## 5. 为什么没看到主动分享

WebUI 只读查询确认自主生活、同名群参考、冷场活跃、生活日程和搬运五个插件均加载成功；加载成功不等于已经分享成功。

生产自主生活日志显示 10 月 30 日有 **14 次思考、1 次成功 Python 实践、1 次失败/未知动作，分享表为空**。两个项目仍处于 active。旧日志把部分 think 记成 action，当前计数排除了这些私人思考，避免假耗尽行动预算。日程已生成当天 12 个活动；独立分享定时器关闭是统一分享协调器接管后的预期配置。冷场触发记录只表示排队，不能算成功送达。夜间有静默时段，模型接口失败也仍可能限制执行。

本轮新增：

- 决策与状态工具显示真实行动数、连续只想不做次数、今日实际分享数、剩余额度、上次分享间隔。
- 让模型在连续思考或一直未分享时重新评估小步骤、真实成果、进展和相关旁群见闻；仍允许休息，不把想法伪称成果。
- 保存安全的最后动作类型及是否请求分享，便于区分“未决定分享”和“发送失败”，不公开私人思考。
- 搬运考虑机会由 10:00–22:00 延至 **09:00–24:00**；仍由群消息密度及 Planner 决定，不定时强发。补齐午夜 24 点计划计算，避免日期替换异常。
- 全局主动分享每天最多 4 次、间隔/去重/权限限制保持；一次性初始项目引导已消费标记未重置。

不保证模型每天必然发送四次；应在用户重启后、非静默时段观察新 `activity_budget` 和 `last_decision`，才能验收实际长期行为。免费 API 不稳定仍是外部限制。

## 6. 搬运插件调查（本轮未安装）

- [cateye_mai_forward](https://github.com/cateyemizuki/cateye_mai_forward)：原样合并/逐条转发、复读、消息打包与分享，适合 QQ 聊天记录和原媒体。原生 NapCat 转发可保留媒体，宿主降级路径不保证视频/音频仍可播放。需处理与现有 `forward_messages` 同名工具及统一分享限额。
- [ForwardMessagesAuto](https://github.com/papaya0481/maibot-ForwardMessagesAuto-plugin)：更偏向已有 QQ 合并转发记录，由 Planner 判断是否搬到目标群；作者实测 SnowLuma，当前本地 NapCat 路径仍需真正的媒体转发验收。
- [maibot-tieba-share](https://github.com/mizuku003/maibot-tieba-share)：贴吧帖子长图，可选合并、视频、去重；有反爬失败冷却。声明的 MaiBot/SDK 版本覆盖当前版本，但需要长图渲染、真实贴吧可用性及媒体发送验收；若后续接入应关闭独立广播，由现有自主分享协调器统一限额。

以上是源码/文档兼容性调查，不表示安装或实际群媒体发送已经验收。

## 7. 修改文件与测试证据

功能文件：

- `plugins/community_context/{plugin.py,config.toml,_manifest.json,README.md}`。
- `src/services/memory_service.py`、`src/A_memorix/host_service.py`、`src/plugin_runtime/capabilities/data.py`、`src/maisaka/memory/person_profile.py`。
- `plugins/autonomous_life/{plugin.py,store.py,_manifest.json,README.md}`。
- `plugins/mai-repost-shit/config.toml`、`plugins/mai-repost-shit/core/state.py`。
- `DEV_GUIDE.md`：来源确认协议、故障隔离与真实行动计数规范。

本轮验收脚本新增/扩充：`community_runtime_audit.py`、`community_context_acceptance.py`、`linked_identity_acceptance.py`、`bind_entry_acceptance.py`、`identity_relay_acceptance.py`、`maibot_repost_planner_acceptance.py`。

| 验收范围 | 通过项数 |
|---|---:|
| /bind 入口与已绑定第三账户拒绝 | 8 |
| 关联画像、抗昵称变化、精确记忆范围 | 14 |
| 同名旁群参考、来源、超时与旧宿主降级 | 17 |
| Sleep 双端状态、跨群、并发结算 | 8 |
| 双端身份转发、管理权限、禁言时长、撤回拒绝 | 22 |
| 自主生活、实际隔离 Python、分享限制 | 67 |
| 项目连续性、一次性引导、权限请求与发件箱 | 57 |
| 双端插件匹配矩阵 | 119 |
| 双适配器消息与能力降级 | 54 |
| QQ/NapCat 适配器 | 30 |
| 搬运 Planner、晚间窗口与午夜计算 | 28 |
| 原生核心启动、插件/工具桥接与正常关闭 | 118 |

这些套件部分覆盖重叠，未累计成不重复功能数量。配置 TOML 全部解析通过。

证据：

- `docs/maibot-wechat-acceptance/community-reaudit-summary-20261031.json`：本轮汇总。
- `docs/maibot-wechat-acceptance/community-runtime-reaudit-20261031.json`：生产只读检查及真实数据快照，7 项通过。
- `docs/maibot-wechat-acceptance/qq-readonly-reaudit-20261031.json`：QQ 在线、角色、工具和原生收发历史。
- `temp/maibot-bridge-acceptance-/20261031-031020/acceptance.json`：最终 118 项原生桥接检查。

待用户手动重启加载核心和壳子更新；此后实际主动分享和跨端群管理行为仍以新进程日志为准，本轮未通过实际处罚或骚扰群友来模拟验收。
