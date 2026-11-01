# 自主生活与提示词统一：实施和验收

日期：2026-10-29。当前 Host 1.3.15，SDK 2.16.0，Windows 10。未手动启动、停止或重启生产bot；验收实例使用独立配置、数据库、插件数据和发送拦截。

## 已实现

新增原生插件 `plugins/autonomous_life/{_manifest.json,plugin.py,store.py,web.py,config.toml,README.md}`。不使用旧插件系统，不修改原生提示词预设。

- 10–30分钟一次私人考虑；按人格、同群真实历史、兴趣与项目决定行动或休息。每日弹性目标8次、硬上限12次。不会把随机定时事件伪装成亲身经历。
- 公开HTTPS搜索和网页读取；复用热点插件的只读API读取B站/抖音素材。支持自主研究和娱乐，不声称看过只读取标题的视频。
- 通过原生插件API运行AppContainer Python标准库程序，支持函数、实验、一次有上限的修复、保存源码和数据成果。
- 兴趣、已执行经历、未完成项目、暂停/完成/放弃状态持久化并进入之后的考虑；按真实会话隔离，不把其他群聊天当成当前群经历。
- 真正对外文本走原生**聊天**Replyer，再通过SDK `send.hybrid` 发送；原生At组件承载真实成员ID，不用文本模拟@。
- 跨目标群共用每日最多4次主动分享，至少2小时一次；同一真人主动@至少72小时。拒绝@全体、无依据身份、bot目标，持久化不想被打扰者。正常请求回复、点播和明确要求的提醒仍保留。
- 发送前事务预占限额与指纹；崩溃/超时结果按未知处理，不自动重发。单实例文件锁、每日模型调用上限、输入输出限长、失败退避、事件和成果清理。

当前白名单只包含 `47447788043@chatroom`（GDUT SIE Minecraft Public Server）；外壳另一个关闭LLM主动回复的群不参与。`scripts/maibot_autonomy_config.py` 已适配统一调度，更新白名单不会重新开启旧定时器。

## 复用与冲突处理

`hot-topics/plugin.py` 增加只读 `hot_topics.read`；保留用户工具，关闭ambient独立调度。`mai-repost-shit/plugin.py` 增加 `planner.coordinator` 与单次授权API：仍由真实消息密度提供机会，统一Planner模型判断是否搬运，经过每日预算和一次性授权才搬一条。保留原有人工点播及旧planner模式兼容。

关闭旧 `mai-life` 自动生活生成/分享与 `idle_proactive_chat`，保留源码和已有数据；新插件接管有真实执行依据的自主生活。用户定时提醒、受限RCON和已有记忆/embedding功能不改动。旧生活插件仍可在隔离测试中正常加载，未删除数据或改变其格式。

`restricted_python/appcontainer.py` 新增每次独立SID的Windows AppContainer，无网络capability、无继承句柄/密钥环境，仅授权只读运行时和临时目录；Job Object硬限制2秒CPU、128MB、单进程、5秒墙钟终止。原 `restricted_python` AST子集工具保留兼容，新增 `sandbox_program` 和 `python_sandbox.execute`。成果只导出有限常规文本文件；拒绝重解析点，结束销毁每次profile和临时目录。磁盘采用巡检，**不是NTFS硬配额**，存在短暂超过1MB的突发写入窗口。

原 `maisaka.reply.generate` 实际只走长文模板，不适合日常主动消息。必要的Host能力适配限于 `src/plugin_runtime/capabilities/core.py`：增加可选 `reply_mode=chat`，使用原生 `MaisakaReplyGenerator`，默认document兼容不变；`src/chat/replyer/document_generator.py` 修正空写作要求导致空文本片段异常。没有修改任何原生提示词文本。

## 提示词逐项处理

实际比较源：`src/config/official_configs.py` 的 PersonalityConfig、ChatReplyStyleConfig，以及真实Replyer拼接和模型请求。

| 原Config注入 | 处理 |
| --- | --- |
| identity.style 的句式、长短、表达切换要求 | 去掉表达指令；“自然、随和”等性格归入traits |
| llm.reply_style / behavior_style | 删除自定义覆盖，动态继承当前源码原生风格与自主行为规定 |
| group_prompt / private_prompt | 删除自定义覆盖，使用原生群聊读空气/话题交互和私聊节奏规定 |
| prompt.prefer_short_reply / forbid_markdown / forbid_explanation | 删除，不与原生模板争夺长度/格式/解释策略 |
| 群profile重复身份、风格与三条英文special_rules | 删除冗余；身份、bot边界、可靠性、@权限保留在唯一全局人设中 |
| 旧“Python尚未接入，执行概率0%” | 改为已经接入隔离沙箱，只有真实执行成功才能声称完成 |
| 插件style_hint、生活注入中的语气/播报要求 | 移除对外表达要求，只保留事实依据、权限和不编造经历的边界 |

`llm/core/native_prompt_policy.py` 从当前源码AST读取原生默认值，不加载另一份runtime配置，不复制硬编码风格文本。原生behavior_style重复的“女大学生”默认身份前缀被剥离，仅继承原生行为要求，避免覆盖GSMPS身份。`maibot_native_config.py` 在 `style_policy=native` 时强制继承；`maibot_runtime.py` 的群profile只注入自定义身份差异和边界，并能移除遗留marker。

保留GSMPS Minecraft机器人身份、群昵称服务器状态@我、随和性格、群文化、服务器归属和管理员背景、可靠事实与命令边界、token/捐助玩笑、bot/taffy良性互动、自主性、记忆、贴吧真实来源和无害玩笑等核心含义。没有继承原生默认女大学生身份。旧百分比倾向统一为上下文决策，不再与新系统限额冲突；50%其他bot插件拒绝机制仍由现有可信策略保留。

## 推荐插件调查

读取了以下仓库main分支README、入口代码和清单，未直接执行其安装代码。GitHub REST返回403限流，git访问github.com失败；raw文件可访问。证据记录下载文件SHA256，**没有伪称已固定上游commit**。

| 仓库/实测清单版本 | 采用结论 |
| --- | --- |
| [Attention Seeker v2 / 2.0.0](https://github.com/clfr41/attention-seeker-mai) | SDK兼容范围覆盖当前环境；其核心是主动意图触发，会与现有主动调度重复；采用统一人格的思路，不另装调度器 |
| [Life Simulation / 2.0.0](https://github.com/SkonJIYHD/LifeSimulationPlugin) | 原生SDK入口，但清单仅声明send_message且入口使用绝对core/components导入、工作目录data路径；需完整适配，睡眠拦截正常聊天不适合本任务，未并装 |
| [oh-mai-agent / 0.1.0](https://github.com/Dreamwxz/oh-mai-agent) | 原生SDK，含MCP、任务、调度和跨插件调用；额外croniter/jinja2/MCP依赖和独立任务系统，与当前受限工具/调度重复；采用原生跨插件API路线，不启用宽泛MCP工具 |
| [Plastic Memory / 0.4.8](https://github.com/yufei-pan/MaiBot-plastic-memory-plugin) | SDK范围覆盖当前；核心是可写并注入演化指令，和本任务保持人格/统一风格有额外风险。复用已有原生记忆，自主项目存数据而非自改系统提示 |
| [Bilibili Autopilot / 0.8.0](https://github.com/namchengwuqiu/bilibili_autopilot) | 原生SDK，但账号登录、评论和QQ群分享不适合当前只读微信路线；未取得B站账号授权，不启用评论；复用已安装热点/搬运能力 |
| [Hot Topics / 0.7.6](https://github.com/ZhaoCang-QWQ/hot-topics) | 已安装并适配；直接复用只读信息源和过滤，关闭重复主动调度 |
| [Quick E2B / 1.0.10](https://github.com/sansenjian/quick-e2b-sandbox) | 入口仍导入src.plugin_system旧接口，需要E2B凭据；不直接兼容当前原生Runner。采用本机真正AppContainer隔离，不引入付费云凭据 |

静态调查不代表对这些上游全部功能完成了运行兼容性认证。

## 验收与当前阻塞

测试结果：自主生活与沙箱专项63项、真实Host/Runner桥接85项（内部另跑53项收发单元用例）、既有主动插件路由23项、搬屎Planner回归26项，均通过。详细结果和最终文件哈希见[验收清单](maibot-wechat-acceptance/autonomous-life-provenance-20261029.json)。测试包括：真实AppContainer正例/越权/网络/子进程/资源限制；自主考虑、休息、兴趣和项目连续性、单次调试、文件锁、状态恢复、额度和去重；原生Host→Runner→Python沙箱→原生聊天Replyer→微信At字段/文件转译；真实模型请求中保留自定义身份并使用原生回复风格；现有RCON、提醒、引用丢弃、点播、学习策略及普通聊天回归。

真实外网探测：Bing RSS搜索返回5条，python.org读取成功。三个现有Kilo聊天模型都返回 **429 / PROMOTION_MODEL_LIMIT_REACHED**。这是当前线上自主思考的外部阻塞，代码不能恢复上游额度；未将fixture结果冒充真实LLM可用。使用同一原生模型路由和其回退，后台另有失败退避。

未向真实微信群发送验收消息，发送层使用拦截验证。未开展真实24小时自主运行；需要用户手动重启后，在上游模型额度恢复或配置可用模型的前提下观察。不能保证每天一定行动6–12次，因为允许休息、额度不足和失败跳过。

本次配置备份：`temp/autonomous-life-backup-20261029/`（包含原配置，请勿公开）。无手动重启步骤已执行。
