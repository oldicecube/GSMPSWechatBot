# 历次任务闭环复验（2026-10-30 18:15）

## 本轮结论

用户列出的六项均已有实际实现。本轮没有发现仍被模型故障卡住的开发项，重新完成了真实 embedding、当前首选 LLM、联网、WebUI 配置、微信身份和完整工具调用链验收。生产进程没有重启，也没有向真实联系人发送测试消息。

本轮新增 `scripts/embedding_route_health.py`，便于使用合成中文重测当前免费向量入口、检查语义区分和同模型网关等价性，报告不包含密钥或聊天内容；修复 `scripts/maibot_agent_tools_acceptance.py` 默认路径仍指向临时候选目录的问题，默认直接测试已安装的 `plugins`。

## 六项逐项结果

| 原请求 | 当前实现与复验 |
| --- | --- |
| 壳子插件可配置项及开发规范 | 32 个命令、7 个自动化和 1 个微信适配器，共 40 个独立管理项；顶层字面量 `WEBUI_CONFIG`，实际执行读取、类型范围校验、共享路径、密钥遮蔽、热更新/重启语义、后台 ACL 均已接入。`scripts/maibot_shell_config_acceptance.py` **137 项通过**；前端配置编辑器、提供商及来源显示相关 3 文件 **46 项通过**。规范见 [DEV_GUIDE.md 2.9](../DEV_GUIDE.md#29-将壳子插件配置接入-maibot-webui)。 |
| embedding 失败与免费入口 | 当前 Nemotron 中文 1024 维、图片 2048 维实时可用；两个 Kilo 入口返回相同向量，OVH BGE-M3 独立入口语义验证成功。原生服务、记忆入库/召回、图片索引/召回、故障恢复补建、表达索引 **27 项真实测试通过**；传输故障切换与限流保护 **21 项通过**。 |
| wxid 改名后认人 | `platform + 完整 wxid` 定位人物，名字仅为显示/别名。改名仍查询原稳定人物 ID、保留记忆，同名不任意匹配、微信/QQ 隔离、未知名字不回退到请求者。`scripts/wechat_identity_acceptance.py` **19 项通过**。 |
| 删除 Config 中 MaiBot 已接管项 | 原先清理的模型池、人设副本、记忆/学习/上下文等没有回流；本轮清理 dry-run **新增可删除项为 0**，配置归属及迁移幂等 **8 项通过**。保留确实仍由壳子执行的微信连接、路由、群开关、权限、排队限额等。`gsmps_model_import_pending.json` 的 9 个待迁移端点保留，供下次启动一次性合并，不应提前删除；合并后自动清理。 |
| /bv 支持普通消息 b23 短链 | 自动化扫描与命令共用原下载队列；无需 @ 或前缀，保留权限、群开关、质量/大小/时长/容量限制。提取、展开、分 P、失效/循环/超时、Router→Dispatcher、下载→发送→清理 **19 项通过**；真实短链再次展开为 `BV1GJ411x7h7`。 |
| 会话 Agent Tools 和先联网查证 | `internet_search`、`read_webpage` 默认直接可见，有先查证指导、真实 Host 会话上下文、限额/超时/缓存、外部证据标记；复用自主生活联网能力。工具单元 **19 项通过**，真实搜索两次、读取官方文档一次均成功。当前运行时只读查询确认两个工具已启用、插件已加载。完整原生核心回归 **127 项检查通过**，其中包含 **65 项消息转译单元测试**。 |

推荐可配置项：全部命令/自动化的冷却、允许群/用户、Agent 调用开关；patpat 回复文本；复读阈值；图片跟进有效期；玩家监控间隔；/bv 文件大小、时长、质量、超时和队列容量；彩票下注/日限额/开奖/播报；MC/RCON 与外部服务地址、端口、令牌和超时。具体设置及生效时机见 [管理验收文档](maibot-webui-shell-management-20261030.md)。

## embedding 失败解释与当前入口

历史失败计数不会因修复归零。现有日志中可确认之前有 429；本轮检查的生产日志还包含 15:52–15:56 的图片网络重试耗尽，以及 16:48 的图片请求超时。原进程尚未重启，因此不能将新 Host 传输故障切换已经被它加载视为事实。

18:06 本轮合成输入实测：

| 入口 | 结果 | 用途 |
| --- | --- | --- |
| `https://api.kilo.ai/api/gateway`，Nemotron 3.5 Embed 1B free | HTTP 200；1024 维；语义相关 0.587、不相关 0.081 | 当前文本向量主入口；图片原生服务另已完成 2048 维测试 |
| `https://ai-gateway.kilo.ai/api/v1`，同一模型 | HTTP 200；三个样本向量与上行一致 | 已配置的同模型网络/5xx 故障备用，不代表独立配额 |
| `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1`，BGE-M3 | HTTP 200；1024 维；语义相关 0.769、不相关 0.355 | 已登记 `free_bge_m3_ovh_1024`；独立空间备选 |
| OpenRouter，匿名调用同一免费模型 | HTTP 401 | 没有登记成可用入口；需要自有 API key |

BGE-M3 和 Nemotron 维度相同也不能混写同一索引，所以没有把它自动插入当前向量任务降级列表。当前模型恢复可用，无需为本次闭环更换模型并重建已有索引。

上游说明：[Kilo 当前网关](https://kilo.ai/docs/gateway)、[OVH BGE-M3](https://www.ovhcloud.com/de/public-cloud/ai-endpoints/catalog/bge-m3/)、[OpenRouter embeddings 鉴权](https://openrouter.ai/docs/api/api-reference/embeddings/create-embeddings)。实际可用性以本轮请求报告为准，免费入口未来仍可能限流或变动。

## 会话工具实际状态

当前生产 WebUI 的运行时组件接口返回 **23 个插件 Tool，15 个启用**。其中 `find_user_qq_id` 仅适用 QQ，QQ 通道尚未实号接入；其余仍受各自平台、群、权限和功能开关约束。

启用项：`internet_search`、`read_webpage`、`owned_plugins`、`restricted_python`、`sandbox_program`、`minecraft_rcon_action`、`manage_scheduled_reminder`、`mai_life_query`、`mai_life_modify`、`repost_shi`、`repost_pretty`、`get_hot_topics`、`autonomous_life_status`、`autonomous_project_control`、`find_user_qq_id`。

核心另有 `reply`、`wait`、`stop`、记忆/图片记忆/人物查询、表情包目录、工具发现与子代理等。完整用途、可见性和边界见 [工具清单](maibot-conversation-tools-20261030.md)。QQ 戳一戳、撤回、禁言等 8 个工具保持原禁用状态，没有为测试擅自开启。

本轮还实测当前首选免费 LLM `nvidia/nemotron-3.5-super-vl:free`：Planner JSON、自动工具调用、读取工具结果、视觉各 1 项，**4/4 均 HTTP 200**，返回用量显示 cost=0。未修改聊天模型任务顺序，仍保留原 Gemini/DeepSeek 偏好策略。

## 运行生效情况与边界

生产 bot 仍为 PID **17104**，**12:47:47** 启动。本轮不手动重启。

当前 WebUI `/api/webui/plugins/installed` 返回 **10 个原生插件**。磁盘新版接口已追加 40 个 GSMPS 管理项，隔离后端及前端测试通过；这个差异说明旧 Host 尚未加载该接口，不能宣称当前页面已经显示全部管理项。wxid 修复、Host 故障切换、新 WebUI 接口及一次性配置迁移，应在用户下次手动重启后再核验实际运行状态。两个联网工具已通过原生插件监视器加载，不需要另外向 bot 发送初始化提示词。

完整核心链路测试使用确定性 LLM/网页/下载夹具及拦截消息，测试到原生 Planner→Runner→工具结果→Replyer→微信/QQ 出站，并验证 AppContainer 实际运行及成果、定时提醒、RCON 权限、自主项目连续性及联网证据。它不等同于真实群物理送达或在线 LLM 每轮一定联网；真实 LLM 协议、网页和 embedding 已分别进行上述在线实测。

## 本轮报告位置

- `temp/pending-task-recheck-20261030/`：所有复验日志、免费向量入口报告 `embedding-route-health.json`、当前 LLM 实测 `active-planner-live-contract.json`、运行时工具快照 `runtime-tools.json`、Config 清理 dry-run。
- `docs/maibot-wechat-acceptance/embedding-native-20261029.json`：脚本历史文件名，本轮已更新，27 项均通过，实例 `temp/maibot-embedding-acceptance/20261030-180254`。
- `temp/maibot-bridge-acceptance-/20261030-180824/acceptance.json`：127 项完整核心检查均通过。
- `temp/maibot-agent-tools-unit.json`：24 项 Python/RCON/搬运权限与合同测试均通过。
- `temp/maibot-shell-config-acceptance.json`、`temp/wechat-identity-acceptance.json`、`temp/bv-short-link-acceptance.json`、`temp/maibot-research-acceptance.json`、`temp/embedding-failover-acceptance.json`：各专项报告，本轮已更新。
