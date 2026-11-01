# MaiBot 配置加载与插件 LLM API 检查

检查时间：2026-10-29 02:17–02:25（Asia/Shanghai）。当前内嵌核心 MaiBot 1.3.15，实际 SDK 2.7.1。

结论：修复后，人设、16 条身份/行为规则以及群配置均进入原生 Planner / Replyer 请求；线上新进程已加载对应配置。插件通用文本和工具调用路径实测可用，但视觉输入、备用模型和向量记忆仍存在外部能力/配置缺口，不能判定为所有功能正常。

## 已发现并修复

1. `llm/core/maibot_native_config.py` 原先只同步 `identity.name/role/style`，漏掉 `identity.rules`，原生人格对 16 条规则的覆盖是 **0/16**。现同步完整规则，写入两个生成阶段都读取的原生人格。
2. `config.json` 中 `GDUT SIE Minecraft Public Server` 的 `identity.style`、`reply_style`、`group_prompt` 是连续 `?`，这是文件内容损坏，而非终端显示问题。已用同文件中完整的全局对应字段恢复，未改写其余自定义规则。原配置备份在忽略目录 `temp/config-api-check-backup/`。
3. 增加 `maibot_native_config_loaded` 启动日志，记录运行中核心实际读取的人设/风格 SHA-256、模型路由和学习黑名单条数；不记录 API key。

生产进程由既有 `start.bat` 自动重启为 **PID 8024**，两个 Runner 是 **12856 / 13584**；旧进程遗留的两个 Runner 已清理，WeFlow 未重启。02:23:04 出现 `maibot_native_ready`。

运行时人设、行为风格和回复风格的三个摘要均与当前磁盘配置相同；Planner 与 utils 首选仍是 `wechat_kilo_auto_free`。学习黑名单条数为 1，既有“大肥鱼”规则保留。

## 配置生效范围

| 项目 | 核对结果 |
|---|---|
| 身份、人设、行为/回复风格 | 已加载；修复后 16 条全局规则完整传入两个生成阶段 |
| 群专属配置 | 通过原生 per-chat prompt 路径传递；隔离验收分别捕获 Planner、Replyer 的实际 HTTP 请求，确认包含群配置和回复风格 |
| Plugin Host / Runner | 已启动；通用 LLM capability 已注册 |
| 原生学习、记忆服务 | 启动；学习黑名单保留。记忆向量通道因未配 embedding 降级，不能称为完整向量检索 |
| 主动插件 | 本次扫描 `plugins/*/_manifest.json` 为零；上一轮研究的第三方插件仍未安装，不能宣称插件自身任务已验收 |
| 旧外壳 `llm.autonomy` | 配置本身为 false；其旧调度参数不会自动成为 MaiBot 生活插件配置 |
| 旧 `llm.memory/learning` 参数 | 原生服务采用 `config/bot_config.toml` 的 A_Memorix / expression / jargon / learning 配置；旧数据库路径、阈值等未逐项映射，不应因 JSON 内仍有字段就认定它们生效 |
| ASR / Subagent / 联网搜索 | ASR、Subagent 关闭；MCP 无服务器，搜索模型未配置 |

当前 `assistant_nickname=LLM`，人设身份名为 `GSMPS Bot`；核心实际昵称为 LLM。这是当前配置值的差异，本次保留。临时说话风格候选仍存在，但 `multiple_probability=0`，不会随机选用。

## 真实 API 验证

测试路径：**已安装的 SDK `PluginContext.llm` → SDK 序列化 → 本地实际 Host capability handler → 原生 LLM Service → 配置中的外部 API**。中间使用进程内 RPC 适配器；这项测试不覆盖 Runner IPC 传输，不执行任何返回的工具，也不发送微信消息。

最终一轮使用各任务正常 token 配置，没有人为压到 128 tokens。第一次 128-token 探测曾让自动路由模型耗尽输出预算、返回空内容，不能将该探测归因于任务路由失效；最终正常预算下三个任务均成功。

| API / 测试 | 结果 |
|---|---|
| Kilo `kilo-auto/free`：utils 文本 | 成功，返回 `OK`，5.26 秒 |
| Kilo：Planner / Replyer / Memory 任务路由 | 分别成功，3.56 / 2.35 / 3.05 秒 |
| Kilo：`generate_with_tools` | 成功，实际返回 `diagnostic_ping(value=ok)` 工具调用，10.29 秒；未执行工具 |
| Kilo：64×64 红色测试图 | 失败，HTTP 400：`This model does not support image or file input. Remove it and retry.` |
| OpenCode DeepSeek V4.1 Flash Free | 失败，HTTP 429 `FreeUsageLimitError` |
| OpenCode GLM 5.3 Flash Free | 失败，HTTP 429 `FreeUsageLimitError` |
| OpenCode MiMo V2.6 Flash Free | 失败，HTTP 429 `FreeUsageLimitError` |

三个备用 API 的上游明确返回免费使用额度已到上限。这并非漏传密钥或插件 `task_name` 找不到。Kilo 文本和工具功能本次可用，不代表后续没有限流或自动选模波动。

所有模型的 `visual=true` 标记按既有要求保留，但它只允许核心发送图片，不能为上游增加视觉能力；目前 `kilo-auto/free` 的图片路由实测拒绝输入。要恢复视觉，须配置一个实际接受图像的可用模型/路由。

文本/图片 embedding 是独立接口。当前缺少 `embedding` / `image_embedding` 模型，不能把聊天视觉模型直接填进去冒充 embedding；这也不会由 Kilo 的文本调用成功自动解决。

## 验证结果与证据

- 桥接单元检查 38 项通过。
- 原生隔离功能检查 **49/50** 通过。新增检查验证全局规则和群配置分别出现在 Planner 与 Replyer 发往模型的请求中。
- 唯一未通过项：`native.tool.query_image_memory`，原因是 `image_embedding` 未配置，属已有能力缺口。
- 修改文件通过 Python 编译及 `git diff --check`；未运行物理微信群发送测试。

机器可读记录：

- [配置与真实 API 结果](maibot-wechat-acceptance/config-api-check-20261029.json)
- [隔离配置/功能验收](maibot-wechat-acceptance/config-load-acceptance-20261029.json)
- [修复前配置与初次 API 探测](maibot-wechat-acceptance/config-api-before-20261029.json)

可复用脚本：`python scripts/maibot_config_api_check.py` 只做文件/日志检查；加 `--live` 在隔离实例中调用真实 API。密钥只保留在原配置/忽略的隔离副本中，不写入本报告。后续第三方插件安装仍需对齐 SDK 依赖并验收其自身生命周期、权限和微信目标解析。
