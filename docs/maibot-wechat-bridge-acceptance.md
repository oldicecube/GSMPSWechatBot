# MaiBot 接入 Wechat Bot：修复与验收

验收日期：2026-10-28 至 2026-10-29。结论：消息桥接修复已部署；文字模型链路通过；**全功能验收尚未通过**，仍存在外部模型配置、额度和桌面投递验收缺口。

## 原因与修复

此前普通消息走到 `forward_to_llm` 后没有交给核心；显式请求遇到免费模型额度错误时，宿主又将核心异常掩盖成 180 秒超时。旧适配器还绕过官方收消息流程，丢失部分消息结构及核心生命周期。

现在入站走官方 `chat_bot.receive_message`，由原生 Heartflow/MaiSaka 负责规划、历史、工具和学习；出站走原生 Platform IO 驱动，再交给现有微信发送壳。壳层不再次发送已由核心投递的消息，失败回执不会记为发送成功。

配置中找到的是 **Kilo**（`kilo-auto/free`），没有独立 Kiro 配置。已将 Kilo 在壳配置和原生 planner/replyer/utils/learner/memory 任务中置于首位，保留原生故障切换。视觉任务仅选择声明支持图片的模型；实测 Kilo 返回不支持图片输入，不能作为视觉替代品。

## 消息字段契约

| 字段或功能 | 转译行为 |
| --- | --- |
| 身份与会话 | 实际微信账号、发送者 wxid、群 sessionId 和群显示名分别保存；私聊收件人不会误用机器人账号 |
| 消息 ID | 优先原始服务器 ID 字符串，避免 JS 数字精度丢失；无服务器 ID 时使用会话限定的 localId；不将 `0` 当作真实引用 |
| 时间 | 秒、毫秒、ISO 时间转为核心使用的本地时间；接收时间与消息时间分别保留 |
| 文本 | 保留 Unicode、换行和原文空白；普通文本中的 XML 不解释为引用；命令重建完整 `/command args` |
| 微信 4 类型 | 低 32 位为基础类型，高位为富消息子类型；原类型、子类型和原始 XML 保留 |
| 入站引用 | `ReplyComponent` 保存引用 ID、作者、预览；当前正文独立保存 |
| 入站 @ | 解码消息源的 atuserlist 并传递真实目标 ID；包含本账号或 notify@all 时标记原生触发 |
| 图片、表情、语音、视频 | 用精确消息 ID 获取原件并按类型构造组件；缺失或 ID 不匹配时明确标为不可用，不按邻近时间猜测 |
| 文件、链接、合并转发 | 保存文件元信息、可读链接和转发节点；原始 XML 留在消息元数据中。转发内复杂媒体、未提供可下载内容的加密文件不等于已取得二进制 |
| 名片、位置 | 保存账号、昵称或坐标、地址等专用元数据，并提供可读文本 |
| 系统与观察消息 | 保留 source_kind、learnable、prompt_visible；观察消息入库，不启动规划；隐藏记录不恢复为模型历史 |
| 出站 @ | 仅显式 AtComponent 转为原生 @；成员名称从核心实际接收记录解析；TextComponent 中的内部标记即使与 AtComponent 混排也保留为文本 |
| 出站引用 | 壳没有真实微信引用操作；出站丢弃 ReplyComponent，不生成引文文本，回执标记 `quote_mode=dropped_unsupported` |
| 投递 | 预校验整条组件序列，保持文本/媒体顺序；记录真实壳层成功或失败，不伪造微信服务器消息 ID；不支持的 @全体成员或组件明确失败 |

## 测试结果

- [隔离桥接报告](maibot-wechat-acceptance/bridge.json)：38 项转译/出站单元测试全部通过；46 项检查中 45 项通过。唯一失败为 `native.tool.query_image_memory`：缺少 `image_embedding` 模型。
- 覆盖真实 SSE 规范化 → Router → Worker → Dispatcher → 核心入库；普通消息、批量独立身份、去重、自身回声、按群策略、原生命令和权限、暂停/恢复、429 后恢复、Kilo 优先与故障切换、记忆/人物查询、转发浏览、工具结果回读、表情注册和发送、插件运行时、后台服务及清理退出。
- 出站使用原生发送服务和真实 HTTP Hook，验证文本/@/引用/图片/表情/语音/文件顺序、精确正文、发送失败不落为成功。该项替换了最终桌面发送器，不能等同于真实接收方收到消息。
- [真实模型报告](maibot-wechat-acceptance/live-models.json)：Kilo 辅助调用与原生规划 → 回复 → 发送回执均通过，生成“验收成功”；发送被拦截。视觉模型实测失败：DeepSeek/OpenCode 免费额度已耗尽（HTTP 429 / FreeUsageLimitError）。
- [部署检查](maibot-wechat-acceptance/deployment.json)：主进程 PID 1604，WeFlow PID 13056；两项本地健康检查 200；真实历史原件接口的发送者、服务器 ID 字符串、原始内容、基础类型、子类型和 atUsers 字段验证通过。
- [WeFlow 协议检查](maibot-wechat-acceptance/weflow-contract.json)：直接执行真实 TypeScript 数据库行解析器、SSE 构造器及原消息 API 投影，7 项检查全部通过；包括带 atuserlist 的消息源、超出 JS 安全整数范围的服务器 ID，以及微信 4 的复合类型。
- 线上首次重启后的自然流量已记录到原生 `maibot_native_ingested`：包含引用文本与图片。图片所在轮次随后受视觉 API 额度限制失败，不能据此声称该轮成功回复。
- `npm.cmd run build` 成功；19 个 Python 文件语法检查、Git 空白检查通过。完整 TypeScript 类型检查仍有仓库基线错误；[与 HEAD 的对比](maibot-wechat-acceptance/typescript-comparison.json) 未发现新增诊断。

## 尚未通过与未覆盖

1. 视觉 API 额度耗尽：影响图片理解及依赖视觉模型的选择、学习流程。需要可用且支持图片的模型配置。
2. `embedding` 与 `image_embedding` 未配置：文本记忆当前仅 sparse/metadata 降级，图片记忆查询失败，向量检索不能判为通过。
3. ASR、Agent、Subagent、Web Search、外部 MCP/ACP 服务未启用或未配置；已记录配置状态，不判为功能通过。
4. 尚未获得真实微信投递测试会话；未主动向任何人发送合成验收消息。旧线上发送日志还出现过微信窗口句柄失效，需要在指定测试会话中验证当前桌面文本、媒体和 @ 的实际投递。
5. 微信原生引用、@全体成员与复杂合并转发媒体仍受现有壳能力限制；出站引用直接丢弃，普通图片文件发送不等于原生表情包发送。

## 复验与运行记录

```powershell
python scripts/maibot_bridge_acceptance.py
python scripts/maibot_bridge_acceptance.py --live
# 兼容旧入口，执行同一套隔离验收
python scripts/llm_maibot_acceptance.py
node scripts/maibot_weflow_contract_acceptance.mjs
```

每次测试创建独立 `temp/maibot-*-acceptance-/<时间>/` 实例、数据库和日志，设置 `MAIBOT_INTERCEPT_SEND=1`；`--live` 使用配置中的真实提供方，但仍不进行桌面发送。存在功能依赖失败时程序返回非零，JSON 中 `passed=false`，不伪报全绿。

原进程 PID 12884 及其 WeFlow 子进程已停止，通过既有 `start.bat` 的守护循环加载修复。补齐微信 4 类型和 @ 源字段后再次重启至上述最终进程。重启前备份位于 `temp/maibot-deploy-backup/20261028-235801/`，包含配置与 SQLite 在线备份。当前 `logs/llm_runtime.jsonl` 带进程 PID，隔离验收日志写入各自实例，避免混淆线上运行证据。


## 2026-10-29 复查

已重新确认群成员 `爱吃白饭的大肥鱼` 为 `wxid_j4m6tul1dm7822`，仅在群 `47447788043@chatroom` 排除自动学习，覆盖表达、黑话、人物事实、图片写入、摘要及反馈纠正；保留正常消息接收和回复。所有 4 个现有模型均已配置 `visual=true`，VLM 按原优先级包含全部 4 个模型。

修复机器人账号目录后缀影响真实 @ 身份识别的问题，保留已有路由账号和会话 ID；发送 @ 使用 WeFlow 最新群名片，解析附件入口展开消息 ID 别名。已发送记录中的 `@wxid` 来自引用预览，未恢复到用户提到的历史解析异常完整堆栈，不能确认该次事故的唯一根因；新增附件失败结构化日志，保存目标参数及错误类型。

新增测试后 38 项单元测试通过；46 项集成检查通过 45 项，图片记忆检索仍缺少 image_embedding。最后的 4 项学习/@ 定向测试再次通过。重启至主进程 14388、WeFlow 8476，核心启动成功且收到真实群消息；重启后仍有上游请求频率限制，未宣称模型调用恢复正常。没有向真人发送合成验收消息。[复查记录](maibot-wechat-acceptance/recheck-20261029.json)。


## 微信出站引用丢弃修复（2026-10-29）

微信发送转译层现直接丢弃原生 `ReplyComponent`，不再生成 `「发送者：原文」` 文本。

用户报告的 `「陈年老冰块：@LLM @服务器状态@我 人设测试：你现在的人设是什么」` 属于此前的引用预览。引用内的 @ 标签现在不会进入微信发送内容或原生 @ 解析。正文、正文里的原生 AtComponent、文本与媒体的先后顺序继续保留。

只有引用、没有正文和媒体的消息返回 `dropped`，不执行物理发送，也不计为发送成功。正常发送回执标记 `quote_mode=dropped_unsupported`，并记录丢弃的引用组件数量。接收侧仍向 MaiBot 提供引用上下文。

新增回归覆盖用户报告的完整中文引文、引用中的多个 @、正文中真实 @、引用与媒体混排、仅有引用时的投递状态。原生发送服务集成检查同时断言：HTTP 发包仅包含正文和正文的原生 @ 标记，不包含被引用的旧消息内容。

所有验证均在隔离实例中运行，微信物理发送被拦截。本轮未修改配置，未重启或手动重载生产 Bot；主进程 PID 15256 保持不变，代码等待用户手动重启后完整生效。
