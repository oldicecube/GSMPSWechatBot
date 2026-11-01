# MaiBot 配置归属、壳子管理接入与验收

日期：2026-10-30。没有重启生产 bot，没有向真实微信群发送测试消息。当前生产进程仍为 12:47:47 启动的 PID 17104；新增代码需要用户手动重启加载。

## 本次配置清理

- 从 `config.json` 删除了 101 处旧 LLM 配置路径，以及旧 `webui` 段和其中的 JWT，共 102 处。包括旧模型池、人设副本、上下文、记忆、学习、自主调度、表情包目录、图片工具和旧聊天节奏参数。
- 原生自定义人设的 11 条规则均保留；人格正文 SHA256 仍为 `776ab6c455f16a2fa560c93ffa25187e7a874d345b3291b317764ca152a01d2f`。
- 保留微信连接、消息路由、群开关、管理员权限、发送限制和壳子插件业务参数。`llm.auto_reply` 仅保留实际在转译入口使用的 `enabled` 与 `allowed_groups`，两个实际目标群的启用状态未改。
- 人设、风格、记忆、学习等由 `config/bot_config.toml` 管理；原生插件使用各自 `config.toml`；模型端点、密钥、任务列表及顺序由 `config/model_config.toml`/MaiBot WebUI 管理。
- 旧进程无法完整识别新版本的停用端点声明，9 个尚未登记的备用/停用端点已保存在 `config/gsmps_model_import_pending.json`。下次启动合并后删除；隔离测试确认，合并后为 16 个模型、15 个提供商，现有任务顺序不变。此文件不是长期反向同步来源。
- 以后通过 WebUI 删除的端点不会在启动时恢复。正常启动也不会再用壳子缺省人设覆盖 MaiBot 人设，或额外启动旧 WebUI。

完整原始配置备份：`data/backups/adopted-config-cleanup-20261030-162947-371372`。第二次移除旧 WebUI 的备份：`data/backups/adopted-config-cleanup-20261030-163452-738647`。备份包含配置和清理报告，未包含到公开提交内容中。

## WebUI 与壳子插件

32 个命令插件、7 个自动化插件及微信适配器分别登记，共 40 个外部管理项。界面标记来源 **GSMPS Wechatbot**；原生插件标记 **maibot**。壳子插件仍在原 Dispatcher 执行，没有为了展示而迁移成原生插件。

每个壳子插件均声明 `WEBUI_CONFIG`；新配置经过类型、范围和交叉字段校验，支持密钥脱敏、原始 TOML、重置备份及共享配置路径。管理覆盖值保存在 `config/gsmps_management.json`，不把 WebUI 覆盖直接写回原始部署配置。权限、群白名单、禁用状态和冷却在实际调用与后台广播时执行。

推荐可配置项如下；界面会说明即时生效或重启生效，不自动重启。

| 插件 | 可配置内容 | 生效时机 |
|---|---|---|
| 全部命令/自动化插件 | 冷却、允许群、允许用户、Agent 调用开关 | 后续调用 |
| patpat | 拍一拍回复文本，默认“拍你吗” | 后续消息 |
| repeat_echo | 复读阈值、特殊回复开关 | 后续消息 |
| player_monitor | 轮询间隔 | 后续轮询 |
| picture | 跟进消息有效期 | 后续消息 |
| lottery | 启用、下注上下限、每日限额、开奖/封盘时间、排行榜、播报群、LLM 文案 | 后续使用 |
| bv | 文件大小、时长、质量、超时、队列容量 | 重启 |
| player / rcon | MC 地址、名称、RCON 地址/端口/密码 | 重启 |
| redeem / theroom / tournament_monitor | 服务地址、令牌、超时、锦标赛参数 | 重启 |

开发规范已写入 `DEV_GUIDE.md` 第 2.9 节，包含字段声明、校验、共享路径、密钥、热更新/重启语义及测试要求；配置归属和模型调用规范在第 4.5 节及 `config_guide.md`。

其他接入：退役 `/clear` 与 `/management`；首页资讯模块移除；适配器标识 **GSMPS Wechat Weflow**；`owned_plugin_tools` 通过可信 Host 授权，以 bot 普通用户身份调用原生/壳子插件并读取返回值，保留结果来源标记与原有权限。`/jane` 与 `/emoji` 的辅助模型请求直接使用原生模型池，不再为每次请求初始化整个服务。

## 微信身份与 embedding

身份以 `platform + 完整 wxid` 生成稳定人物 ID，昵称仅作显示/别名。同一 wxid 更名更新当前昵称并保留原人物及记忆；显式 ID 优先于名字，同名多人不任意选一个，QQ 与微信身份隔离。人物查询不能把未知目标名字退回成提问者自身。画像上下文包含内部稳定 ID 与当前名字。

embedding 历史失败主要来自昨日历史表达向量补建期间的 429；当前进程观察到的 3 次文本失败是连接错误。旧统计不会因修复被清零。

- Kilo 原入口和另一网关均实测成功；中文文本为 1024 维、图片为 2048 维，两入口对应向量完全一致。新增仅针对 `/embeddings` 的连接/5xx 故障切换，不把 400/401/403/429 作为绕限流切换理由。这是同一渠道的网关备用，不代表新增独立配额。
- 独立 OVH BGE-M3 实测通过中文语义检查，登记为 `free_bge_m3_ovh_1024`。它与 Nemotron 的向量空间不同，没有加入现有索引的自动降级列表。若改为主模型，需要规划独立索引/重建。匿名入口限制较低，配置请求间隔 31 秒；见 [OVH 模型说明](https://www.ovhcloud.com/de/public-cloud/ai-endpoints/catalog/bge-m3/)。
- Kilo 请求间隔 1.5 秒，网络失败允许一次额外尝试；排队请求在真正发出前重新检查共享 429 冷却，避免其他任务已触发冷却后继续排队发送。

## 修改文件

| 用途 | 主要文件 |
|---|---|
| 配置清理、一次性模型迁移、原生权威 | `config.json`、`llm/config.py`、`llm/core/maibot_native_config.py`、`scripts/clean_adopted_shell_config.py`、`scripts/adopt_maibot_model_pool.py`、`main.py` |
| 壳子配置/管理/权限 | `core/maibot_shell_registry.py`、`core/shell_plugin_config.py`、`core/dispatcher.py`、`src/webui/services/gsmps_management.py`、`src/webui/routers/plugin/*`、各 `plugins/*.py` / `auto/*.py` |
| 原生模型调用与 Agent 工具 | `llm/core/native_model_bridge.py`、`llm/provider/deepseek_provider.py`、`plugins/jane.py`、`plugins/emoji.py`、`plugins/owned_plugin_tools/*`、`src/plugin_runtime/owned_plugin_tools.py` |
| 模型健康检查 | `scripts/maibot_api_pool_check.py`：已适配原生池；不会重新写入旧 `llm.apis`，只排序任务中已有模型 |
| 微信稳定身份 | `src/person_info/person_info.py`、`src/maisaka/builtin_tool/query_memory.py`、`src/maisaka/memory/person_profile.py` |
| embedding | `src/config/model_configs.py`、`src/llm_models/model_client/openai_client.py`、`src/llm_models/model_client/embedding_failover.py`、`src/llm_models/utils_model.py`、`scripts/configure_embedding_backups.py`、`config/model_config.toml` |
| 前端与规范 | `dashboard/src` 相关插件/命令/适配器来源标记、配置编辑器、模型提供商表单、首页；`DEV_GUIDE.md`、`config_guide.md`、`.gitignore` |

## 验收结果与边界

| 验收 | 结果/记录 |
|---|---|
| 配置清理、备份、缺字段兼容、真实待导入池、单次导入及删除不恢复 | `scripts/adopted_config_acceptance.py`，8/8 |
| 壳子插件 schema、实际执行、ACL/后台群限制、密钥、HTTP 表单与原始配置保存 | `scripts/maibot_shell_config_acceptance.py`，137 项通过 |
| 普通用户工具调用、权限与来源、模型池接管、群数据备份清理 | `scripts/maibot_management_acceptance.py`，32 项通过 |
| 原生 Planner→工具→Replyer→微信发送转译、人物/引用/媒体/插件上下文等 | 78 项检查全部通过，含 65 项消息转译/发送单元测试；`temp/maibot-bridge-acceptance-/20261030-163251/acceptance.json` |
| 原生提示词真实拼接、群聊/私聊人格与风格、重启不覆盖 | `scripts/maibot_style_acceptance.py`，40 项通过 |
| wxid 改名保留记忆、同名不混淆、跨平台与画像注入 | `scripts/wechat_identity_acceptance.py`，19 项通过 |
| embedding 路由、错误类型、冷却、文本/图片兼容及独立模型语义 | `scripts/embedding_failover_acceptance.py`，21 项通过；实时探测记录在 `temp/embedding-*-live-20261030.json` |
| 前端配置编辑器、提供商表单/校验 | 相关 3 文件的 45 项先通过；新增匿名/停用端点与重启反馈测试后，涉及的 2 文件 20 项复验通过；另配置编辑器 hook 6 项通过 |
| 前端 TypeScript / Vite 构建 | 成功，`temp/dashboard-management-final-build.log`；仅有资源分包体积提示 |

前端没有宣称全仓所有测试通过：一次扩展运行遇到上游 `usePluginLifecycle` 的关闭/取消弹窗预期与其实现不符，未修改无关生产代码；相关配置编辑器子集已单独通过。

以上测试使用隔离配置、临时数据库、合成内容和拦截发送，没有认证真实微信界面最终送达。生产进程未重启，因此新版 WebUI 管理接口、wxid 修复、模型迁移等仍需用户下一次手动重启加载；外部 API 的长期可用性也不能由一次探测保证。没有为套用配置而重新启用已停用的独立主动调度插件。
