# 双端身份、积分、转发、QQ 权限与原生人格管理验收

日期：2026-10-30。未停止、重启 bot 或 QQ。当前 bot 进程中的核心 Python 模块仍需用户手动重启后加载；以下区分线上观测与隔离测试。

## 已完成的行为

- 未绑定游戏账户的用户不能领取签到、积分奖励、睡眠奖励、投注、兑换或使用其他消费积分的业务。底层钱包拒绝未绑定用户的增减和消费，不仅在命令提示中限制。历史未绑定余额保留，绑定时再归并。
- 微信、QQ 分别发送 `/bind 游戏用户名` 即可关联同一个游戏账号，无绑定码；用户名必须已有玩家记录，匹配不区分大小写。每个游戏账号最多关联一个微信和一个 QQ 账号，同平台其他账号不能覆盖。已有双端绑定不能切换到另一个游戏账号。
- 积分合并一次、后续共用钱包；跨端每日签到只能领取一次，并发扣分不会透支。彩票历史、保护状态、睡眠记录和游戏记录共用身份，历史记录保留原群及来源 UID。两个历史活跃睡眠记录同时归并时只保留一个有效记录，防止重复奖励。
- 人物身份以平台和稳定用户 ID 关联，改昵称不会产生新身份；关联账号的人物记忆共同检索，人物画像明确标注另一平台的来源。即时聊天流、群权限、管理员身份和 @ 目标仍按原始平台身份隔离。
- 微信 `/qq 正文`、QQ `/wx 正文` 转发到对端已放行、原生主动回复频率大于零的群。微信还尊重现有 `llm_enabled` 开关。使用真实平台、账号、连接及会话路由，不按同名群猜测目标。
- 转发格式为 `[QQ | 名字]正文` 或 `[微信 | 名字]正文`，只生成文本组件。正文里的 `@all` 或 `/ban` 不能借此变成控制操作。插件输出附带来源平台、账号、用户、源消息、源会话信息，并标记为插件执行输出，不冒充 LLM 自由回复。
- 转发默认最长 2000 字、冷却 5 秒，可在原生 WebUI 的两个 GSMPS 插件条目调整。单次最多 50 个目标群；原始消息和目标会话共同去重。失败或结果不确定时不自动重发。
- `attach_at` 先查本轮历史及消息缓存，再查当前会话、当前平台最近 1000 条持久化消息，支持展示短 ID。无法定位的 @ 单独丢弃并记录警告，正文保留；不从其他群、另一平台或相似昵称猜人。
- MaiBot 能看到当前适配器能力。微信侧的 QQ 禁言、撤回、踢人等管理控制段被丢弃，合法正文和 @ 保留；其他未知或不完整媒体继续在发送前明确失败。微信引用控制段仍直接丢弃。
- QQ 禁言执行前核对当前机器人账号在该群的实际角色，只有群主/管理员才允许；目标群主/管理员及保护名单用户不可禁言。该判断与 GSMPS bot 管理员身份无关。

用户名绑定按用户指定的方式实现，不代表额外验证了账号所有权。绑定不会把一端管理员权限授予另一端。

## 人格、昵称、行为、表达的唯一来源

统一由 MaiBot 原生 WebUI 和 `config/bot_config.toml` 管理。现有原生人设原文、昵称及原生风格配置保持不变。

- 启动同步只同步微信传输账号，不从壳子导入任何人格、昵称或表达字段，即使模型配置仍处于旧导入模式也不能覆盖原生人格。
- 壳子的全局和群级旧提示词不再注入；历史桥接专属 `[WechatBot chat profile]` 提示条目在启动时清理，原生 WebUI 创建的群提示词保留。
- 壳子配置归一化及辅助生成入口不再回退读取旧人格字段，也不会补出可注入的旧风格默认值。`/persona` 仅提示前往 MaiBot WebUI，不读写旧人格库、不消费积分。
- 再次清理了残留 `llm.style_policy`，配置清理脚本也会清理群级退休字段。完整原配置保存在本机 `data/backups/adopted-config-cleanup-20261030-210334-414600`，其中可能含凭据，不应上传。
- 40 项配置与真实原生 Planner/Replyer 提示词拼接检查通过；分别验证群聊和私聊、自定义人设保留、原生语气要求、无旧壳子哨兵注入。额外用恶意旧人设配置验证启动不会覆盖原生配置。

## 当前 QQ 线上验证

- 机器人账号 `3294652115` 在线；精确目标群 `GDUT SIE Minecraft Public Server`，群号 `1020993873`，140 名成员，机器人真实角色为 `admin`。
- 通过原生 WebUI 适配器策略仅放行该目标群，其他 QQ 群及私聊维持原策略。原先默认全部阻止是最初未接收该群消息的原因之一。
- 21:25 左右验收时，生产数据库中该 QQ 群已有 **24 条非机器人入站消息、7 条机器人出站记录**。独立聊天流携带 `platform=qq`、账号 `3294652115`、`scope=napcat-local`，并非只读取了 NapCat 群列表。
- 运行时组件接口确认 `mute`、`recall_message`、`send_poke`、`react_emoji`、`forward_messages`、`list_chat_streams`、`find_user_qq_id` 已启用。主动开启私聊保持关闭。
- 经用户授权发送 **一条**测试文本：原生 Broker → PluginPlatformDriver → 实际 QQ 编码器 → 在线 OneBot，回执 **SENT**，QQ 消息 ID **133438971**。再调用 `get_msg` 核对正文及群号，完全一致。未向微信发送测试消息，未执行真实禁言或撤回。
- 该人工验收消息不依赖一次 LLM 决策；线上正常收发记录与隔离 Planner/工具链测试分别提供证据，不能把测试文本发送成功解释为所有模型均健康。

线上验收记录：[qq-live-20261030.json](maibot-wechat-acceptance/qq-live-20261030.json)。一次发送意图与回执另保存在 `temp/qq-authorized-test-once-20261030.json`；脚本防止重复执行。

## NapCat 内网免登录

入口：**http://192.168.31.75:6099/webui/**，允许 `192.168.31.0/24` 内网访问，不需要用户输入密码或令牌。

NapCat 原服务仍在 `127.0.0.1:6099`，内网同端口由专门代理监听，代理自动取得短期原生会话并处理 HTTP、上传、WebSocket 及前端登录状态。原生后端内部令牌保留；“免登录”指内网入口对用户取消鉴权，不是声称修改了 NapCat 不存在的关闭鉴权选项。

已创建限定内网网段的 Windows 防火墙规则 `GSMPS-NapCat-LAN-WebUI`。代理配置在 `depends-data/napcat/config/lan-webui.json`，启动脚本会在用户以后启动 bot/NapCat 时自动启动它，重复监听会退出，不重新启动现有 QQ。

线上无 Authorization 请求页面和登录状态 API 均返回 200；隔离 HTTP、WebSocket、上传、自动登录、CIDR、Origin、转发头检查 9 项通过。既有快速登录及失败二维码逻辑的 14 项回归也通过。QQ 登录状态失效或被风控时仍可能需要人工扫码，不能保证每次永久免扫码。

## 本轮文件

| 文件 | 修改用途 |
| --- | --- |
| `utils/account_identity.py` | 新增双端游戏用户名绑定、身份映射、一次性历史归并、记忆别名 |
| `utils/sqlite_store.py`、`utils/points_manager.py` | 底层绑定门禁、共用钱包、原子签到、积分及业务记录统一 |
| `plugins/bind.py` | 用户名绑定和状态查询 |
| `plugins/sign.py`、`points.py`、`player.py`、`redeem.py`、`lottery.py`、`anima.py`、`echo.py`、`auto/sleep.py` | 积分门禁与关联身份业务接入 |
| `core/community_streams.py`、`core/cross_platform_relay.py` | 原生目标群筛选、文本转发、去重及来源信息 |
| `plugins/qq.py`、`plugins/wx.py` | 平台限定的独立插件与 WebUI 配置声明 |
| `core/maibot_shell_registry.py`、`src/plugin_runtime/owned_plugin_tools.py` | 平台匹配声明及 agent 可调用项过滤 |
| `src/common/message_provenance.py`、`src/maisaka/context/planner_messages.py` | 关联身份与转发来源上下文标记 |
| `src/A_memorix/host_service.py`、`src/services/memory_service.py`、`src/maisaka/builtin_tool/query_memory.py`、`src/maisaka/memory/person_profile.py` | 活跃社群记忆共享、人物别名、来源标注与画像联查 |
| `src/maisaka/runtime.py`、`src/maisaka/builtin_tool/context.py` | @ 短 ID、缓存、持久化查找及失效附件降级 |
| `src/platform_io/message_capabilities.py`、`llm/core/wechat_driver.py` | 适配器能力提示与微信管理控制段丢弃 |
| `plugins/MaiBot-SnowLuma-Adapter/plugin.py`、`config.toml` | 真实 QQ 群角色校验及已授权工具开关 |
| `scripts/napcat_lan_webui.py`、`scripts/start_napcat.ps1` | 内网免登录入口及后续启动集成 |
| `llm/core/maibot_native_config.py`、`maibot_runtime.py`、`maibot_service.py`、`native_prompt_policy.py`、`llm/config.py`、`plugins/persona.py` | 退休壳子人格读入和提示词覆盖路径 |
| `scripts/clean_adopted_shell_config.py`、本机 `config.json` | 清理退休配置；保留原生配置 |
| `scripts/account_identity_acceptance.py`、`identity_relay_acceptance.py`、`napcat_lan_acceptance.py`、`qq_live_acceptance.py` | 新增专项验收及线上审计 |
| `scripts/maibot_bridge_acceptance.py`、`maibot_dual_adapter_acceptance.py`、`maibot_dual_plugin_acceptance.py`、`maibot_style_acceptance.py`、`maibot_shell_config_acceptance.py` | 现有回归用例适配与扩展 |
| `DEV_GUIDE.md`、`config_guide.md` | 开发约束与原生人格管理说明 |

## 测试结果

所有确定性测试使用隔离实例和临时数据库，真实外发仅上述一条 QQ 测试。

| 验收 | 结果 |
| --- | --- |
| 绑定、积分并发、共享业务记录 | 17 项通过 |
| @、跨端转发、记忆/画像、原生人格权威、QQ 群角色 | 19 项通过 |
| 原生完整聊天、工具、自主生活、联网工具链 | 125 项检查通过，包含 65 个消息/转译单元测试 |
| 双端壳子插件与命令管理 | 119 项通过 |
| Broker、账号作用域、媒体、引用、@、管理字段丢弃 | 54 项通过 |
| 壳子插件 WebUI 配置管理 | 141 项通过 |
| 原生人格风格及 Planner/Replyer 实际提示词 | 40 项通过 |
| NapCat 内网免登录 / 快速登录与二维码回归 | 9 项 / 14 项通过 |
| 原生 QQ 适配器协议与来源回归 | 30 项通过 |
| 配置清理与原生模型池权威 | 7 项通过；1 项跳过（生产一次性导入已完成） |

主要报告位于 `docs/maibot-wechat-acceptance/`；完整链路报告为 `temp/maibot-bridge-acceptance-/20261030-211442/acceptance.json`。

## 仍需注意的实际限制

1. **用户手动重启后**，绑定/积分、转发、@ 恢复、记忆及人格读取核心修改才能在主进程完整生效。本轮未自动重启。
2. 实际日志仍有免费模型上游额度耗尽、429 限流和 90 秒超时，造成部分 Planner/Replyer 轮次及表情包维护失败。已能正常处理部分 QQ 消息，但不能宣称所有 LLM 请求都可用。没有通过提高其他模型优先级或恢复失效端点来掩盖此问题。
3. 未在真实群成员身上执行禁言/撤回等破坏性管理测试；已确认线上角色与工具开关，并通过执行前权限、目标保护的隔离测试。
4. 未对游戏兑换后台、付费图片服务等每个外部业务执行真实副作用操作；“双端通过”指路由、匹配、配置、权限与协议验收，不等于所有外部服务永不故障。
