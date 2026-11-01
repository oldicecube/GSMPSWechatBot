# API 池、Bot 互动和自主生活排查

## API 实测与排序

2026-10-29 23:21 的实测使用合成内容；可用线路进一步验证 Planner JSON、自动工具调用、工具结果回传和视觉输入。没有发送真实群聊内容，也没有购买配额。

| 线路 | 结果 | 处理 |
| --- | --- | --- |
| Kilo Nemotron 3.5 Super VL | 全部通过 | 优先级 10 |
| Kilo Dots 3 Note Preview | 全部通过 | 优先级 11 |
| OVH Qwen3.5 397B A17B | 全部通过 | 优先级 12 |
| OVH Qwen3.8 27B | 全部通过 | 优先级 13 |
| OpenCode DeepSeek / GLM / MiMo | 429 FreeUsageLimitError | 后移；保持原禁用状态 |
| Kilo Auto | 文本返回，但功能契约未全部通过 | 后移，未启用 |
| Kilo Nemotron Omni | HTTP 200 内承载上游 ResourceExhausted / 502，未返回有效正文 | 按失败后移，未启用 |
| Google Gemini、Navy Gemini / DeepSeek、NVIDIA DeepSeek | 缺少对应认证密钥 | 不能称为可用；保持禁用并后置 |

顺序按可用性分层，同层 **Gemini > DeepSeek > 其他模型**，相同家族保留既有相对优先级。没有密钥的 Gemini 无法成为当前可调用首选；不会假装已经接通。当前有 4 条通过全部契约的启用线路；免费接口仍可能随后出现配额、连接失败或长请求超时，健康结果代表测试时点。

已修改 `config.json` 和同步的 `config/model_config.toml`，保留独立 embedding 任务和所有模型视觉配置。`scripts/maibot_api_pool_check.py --apply` 可以重复实测并更新池顺序；配置合并前检查端点/模型/密钥未被并发改变，有备份。`scripts/configure_preferred_free_apis.py` 和旧 NVIDIA 配置辅助脚本已遵循 Gemini 优先约束。

证据：[完整 API 报告](maibot-wechat-acceptance/api-pool-20261029-232157.json)。备份位置见报告中的 backup。

## 互动没有明显表现的原因

- **不是全部功能都没加载。** 当前主进程 PID 14880 从 21:52:20 运行；23:19 的真实 Planner 和 Replyer 请求都包含外部 Bot 用户 ID、sender_kind=external_bot 和互动规则。22:50 的 `/status` 也确实抽中了 plugin_request=refuse 分流。学习黑名单与明确 Bot 身份配置匹配。
- **推理可用性影响表现。** 当前进程期间真实存在模型超时、429、连接异常和上下文压缩失败；之后也有成功回复。不能把“配置中有规则”当成“模型每次都执行且消息一定送出”。此前修改的冷却恢复、请求节流、图片限额等核心代码晚于主进程启动，仍需用户手动重启加载。
- **历史拒绝污染了后续轮次。** 原互动提示会把历史中抽中拒绝的请求一直描述成“本次已分流”，但工具限制已经只按最新消息计算，两者不一致。已让拒绝指令与工具限制采用同一最新消息范围，普通群友后续消息不会再被过期拒绝提示覆盖；Bot 身份和普通玩梗引导仍保留。回归已验证。
- 原生群聊 talk_value 已是允许范围的上限 **1**，普通聊天没有再被人为调低。本轮没有靠同时开启旧壳子主动调度来增加发言，避免多个调度器互刷。

## 自主生活的实际状态与修复

生产 `data/plugins/wechatbot.autonomous-life/life.sqlite3` 已记录 4 个行动事件（其中一个被卸载打断）、多次探索/思考和 18 次模型调用，但分享表为零。记录包含 Minecraft/PaperMC 相关研究与后续项目笔记，尚没有真实代码成果。没有删除这些状态或假造完成进度。

1. 原 SDK Runner 的能力 RPC 默认 **30 秒**，插件外层为 45 秒，免费线路等待或生成超过 30 秒会提前失败。插件现在显式传递 RPC 等待预算，后台单次总限额设为 **120 秒**，每日调用与行动预算仍受限。不增加无限重试，不阻塞正常聊天。
2. Bing 曾被查询中的“2026 年”带偏，返回日历/节假日，模型因此一直调研。`plugins/autonomous_life/web.py` 现在检查主题相关性，必要时只去掉日期限定重试一次；无相关结果就明确返回空结果，不把无关资料算作收获。不宣称搜索摘要证明了今日热度。
3. 决策上下文补充实际剩余行动预算，明确无需等群友提问才推进自己的兴趣；允许休息，不硬凑 6–12 次，也不强制分享。私人动作和对外分享仍分离，后者每日最多 4 次并保留去重/间隔。
4. 一次性初始项目引导的持久化消费标记已存在，guidance_pending=false；未更改 request_id 或清除状态，下次重启不会重新触发初始委托。
5. 当前生活休息段为 **00:00–08:30**；午夜后的无后台生活动作属于配置预期，不表示普通聊天停用。

主要修改：`plugins/autonomous_life/plugin.py`、其 `config.toml` 和 `web.py`；`src/common/bot_interaction_policy.py`；对应验收脚本。插件文件由已有监视器处理，未手动重载或重启生产进程；核心修改仍以手动重启为生效边界。

## 测试与限制

- 自主生活 **65/65**：[报告](maibot-wechat-acceptance/autonomous-life-20261029.json)。含真实 Windows AppContainer 隔离、宿主文件/网络拒绝、资源约束、限额、状态连续性、统一分享及 RPC 预算。
- 项目流程 **57/57**：[报告](maibot-wechat-acceptance/project-kickoff-20261029.json)。含一次性引导、实际成果、权限请求暂挂、去重、跨重启恢复和进度分享。
- 完整桥接 **101/101**（其中转译单元测试 61 个）：[报告](maibot-wechat-acceptance/runtime-api-autonomy-qq-20261030.json)。本地模型/embedding HTTP fixture 隔离外部额度，真实核心和工具流程执行，物理发送被拦截。
- QQ、双路由、权限策略、辅助配置与 API 排序策略 **25/25**：[报告](maibot-wechat-acceptance/qq-napcat-20261029.json)。

这些测试验证了功能链路与限制，不保证真实 LLM 每次都选择调侃或自主行动。待用户手动重启后，还需观察一个正常活动时段的真实动作与发言；未替用户重启，也未主动给真实群刷测试消息。QQ 环境与用户步骤见 [NapCat 接入说明](maibot-napcat-setup-20261030.md)。
