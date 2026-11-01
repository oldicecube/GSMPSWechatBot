# 麦麦自主念头与项目生活

原生 manifest v2 / SDK 2.16 插件。使用当前 MaiBot 的人格、Planner 模型和聊天 Replyer，不向普通聊天队列塞入私人工作。

每 10–30 分钟考虑一次，由真实同群消息、人格、兴趣与项目进度决定休息、思考、查资料、娱乐、编程实验或主动社交。每日参考目标 8 次、硬上限 12 次，允许低于目标。08:30–00:00 之外休息。不会为凑数编造经历。

复用 `hot_topics.read`、`python_sandbox.execute`、`repost.autonomous_one` 原生插件 API。公开网络探索只读 HTTPS，禁止本地地址、凭据和代理，验证并固定 DNS 地址；每次最多两次重定向、128KB响应，不访问账号或发表评论。B站热榜读取不代表看过视频。

Python 使用 `restricted_python` 的 Windows AppContainer；缺失隔离能力时拒绝执行。允许标准库、函数和临时文件，无网络和宿主配置权限；2秒CPU、5秒墙钟、128MB、单进程，输出8000字。导出有限的 txt/json/csv/md/svg 数据，不自动执行或渲染成果。

所有自主分享共用每日最多4次、至少2小时间隔的预算，失败、未知结果和放弃发送均占用名额。用户明确请求的普通回复、点播和定时提醒不计入自主预算。一次自主分享只发送一个原生消息；搬屎的一份说明+一个MP4算一个分享事件。主动@需要同群真实消息依据，同一成员至少间隔72小时；尊重不想被@的消息并持久化，不@全体，不触发其他bot。

`life.sqlite3` 保存兴趣、实际事件、项目和限额。保留90天事件/去重记录，每群最多16个项目、12个兴趣和20份编程成果；源码与结果在 `artifacts/`。这不是自我改写系统提示词的接口。模型输出及网页均按数据处理。

`autonomous_life_status` 是当前流绑定的只读 Agent Tool，可查询兴趣与项目状态。

`project` 的初始目标只在下一次启动时登记一次引导。当前目标是自行调研近期热门开源方向、选择感兴趣的小项目，在标准库沙盒内实现并保存最小原型。首次登记后将下一次检查提前至30秒内，仍遵守休息时段、每日预算和API错误退避。第一次有效决策会消费引导并准备公开研究，此后不再重复注入初始目标、不强制优先编程，项目融入原有10–30分钟的兴趣、经历和正常生活决策。

消费标记与初始群集合在同一个SQLite事务中写入，重启、修改群列表或后来增加群不会再次启动相同 `request_id` 的引导。保留项目与经历便于自然续做，不等于重复启动引导。配置的 `enabled` 同时控制项目权限工作流，因此不会在消费引导后关闭整个功能。

有真实发现、作品或进展时，由模型判断是否提交 `share=true` 候选，再由原生 Replyer 判断此时是否值得分享。候选包含实际做了什么、到哪一步及下一步计划，不能把计划当成完成结果或编造完成百分比。它与聊天发现、社交和搬屎共用同一个协调器、同一套每日4次/冷却限额，没有另起项目汇报定时器。

冷却期间按项目保留最新候选，每流最多8项，3天过期，不积压旧进度逐条刷屏。源码、私人反思不写入候选；同一段代码/结果/阶段仅改写措辞不会重复分享。发送结果未知或已提交的候选不在重启后重发。暂停或暂挂项目不发布其旧进度，但不冻结其他兴趣探索和自主生活。

需要第三方依赖、沙盒网络、宿主数据、部署或更高资源时，先考虑受限替代方案，必要时暂挂项目并向固定真实微信ID申请。实际 Python 缺依赖/权限拒绝也会记录并触发申请；语法错误和API 429不会被当作权限申请。通知通过原生 Replyer 和真实 At 发送，计入共享每日4次/2小时/同人72小时限额；冷却内等待下一轮，提交结果未知不重复发送。同一项目同一能力只申请一次，每项委托最多8个不同申请。

`autonomous_project_control` 工具仅接受固定操作者当前真实群消息中明确的“暂停/停止”或“继续/恢复”（也支持 pause/resume），Host覆盖请求身份，插件再核验原始消息。说“同意”不会自动授予权限。继续只恢复受限调度；确实需要的新能力须人工实现/配置，再要求 bot 继续项目。相同能力再次被拒绝会暂挂，不重复@。已完成/放弃的委托不重复启动；需要新委托时更换配置中的 `request_id`。

配置在 `config.toml`，字段均有原生配置 Schema：

| 配置 | 默认/用途 |
| --- | --- |
| plugin.enabled | 启停；代码默认关闭，当前仓库配置开启 |
| life.groups | 明确的微信chatroom白名单，不支持私聊/全局广播 |
| timezone / wake / sleep | Asia/Shanghai、08:30、00:00 |
| min/max_interval_seconds | 600–1800，含重启持久化 |
| target_actions / daily_actions | 弹性8，最多12 |
| daily_llm_calls | 最多72次自主模型能力调用；原生内部表达选择/提供商回退仍由核心限制 |
| llm_timeout_seconds | 45秒；不立即重试，连续失败指数退避，最长6小时 |
| daily_shares / share_gap_seconds | 全群共用最多4，至少7200秒 |
| person_gap_seconds | 259200秒，不连续打扰同一人 |
| history_hours | 同群最多168小时，最多100条查询，进入模型的内容另有限长 |
| ignored_users | 不主动社交的bot/用户ID |
| interests | 初始兴趣，后续持久化学习；不覆盖人格 |
| web/python/social_enabled | 各项能力开关 |
| life.progress_sharing | 启用统一进度候选和冷却后的最新进展分享 |
| project.enabled | 项目工作流开关，包含一次性初始引导和持续的权限处理 |
| project.request_id / objective | 下一次启动的一次性引导标识与目标；更换标识才会明确登记新引导，不授予能力 |
| project.owner_user_id | 经核验的固定操作者微信ID；不按可改昵称查找 |

不要同时开启 `mai-life` 分享、`hot-topics` ambient、`idle_proactive_chat` 等独立主动调度。搬屎应设置 `planner.coordinator=true`；协调插件不可用时不主动搬。更新外壳群开关后运行 `python scripts/maibot_autonomy_config.py --apply` 刷新明确白名单。

会话联网（1.3.1）：已撤销重复的 `internet_search` / `read_webpage` 工具注册，改用 MaiBot 原生 `web_search` / `web_fetch`。`config/model_config.toml` 的 `[web.services]` 定义 `parallel_mcp` 服务，`model_task_config.web_search.model_list` 选择该服务；MCP 地址通过 `config/bot_config.toml` 的 `mcp.servers` 配置，可在 WebUI 中管理。联网检索无需另设生成模型，搜索决策和证据理解沿用 Planner 的统一模型池。原生工具保留来源标记、正文预览、原文编号和分页回查。

本插件后台自主探索仍复用公开网页读取实现与 Bing RSS，不新增主动调度器。正文支持公开 HTTPS HTML、JSON 和文本，不执行 JS、登录、提交表单或访问内网。每跳重新校验 DNS 并固定实际连接 IP，保持 TLS 主机名验证；只允许 443，最多两次重定向、512KiB 原始正文。探索结果仍进入自主生活的真实经历与统一分享限额。

旧 `[research]` 字段与未注册的内部辅助方法保留用于配置兼容及离线回归，不再对 LLM 开放工具入口，也不控制原生联网服务。原生联网开关及服务选择以模型管理中的“联网搜索”功能分配为准，`life.web_enabled` 控制本插件后台自主探索。未删除整个自主生活插件，避免破坏项目、日程与分享。

验收：`python scripts/maibot_research_acceptance.py` 检查旧内部辅助方法和读取边界；`python scripts/maibot_native_search_acceptance.py` 实测原生 MCP 搜索/网页读取 → 模型工具调用 → 证据回答，不向实群发消息。完整转译回归使用 `python scripts/maibot_bridge_acceptance.py --plugin-source plugins --test-agent-tools --test-autonomous-life --test-enable-life`。

限制：AppContainer 为Windows隔离，不支持Linux退回普通进程。磁盘是20ms巡检与导出限制，并非NTFS硬配额，单次突发写入可短暂超过1MB；CPU、内存和子进程由Job Object硬限制。网络及模型可用性依赖外部服务。生产 bot 由用户手动重启；常规后台考虑等待10–30分钟，下一次启动的一次性初始引导可提前，不补发历史事件。当前沙盒适合小型原型，不支持直接安装完整热门仓库、启动长期服务或自行部署。

1.3.2：决策上下文与只读状态工具增加真实的连续思考次数、今日实际分享数、剩余额度、最后分享距今时间及调度状态。已连续多轮思考或一天没有分享时，会明确让模型重新评估小步骤、成果与相关旁群见闻；允许休息和拒绝分享，不直接生成定时广播。`last_decision` 只展示动作种类与分享意图，不泄露私人思考。原每日全局四次分享、去重、未知送达不重试及一次性启动引导标记均保持。
