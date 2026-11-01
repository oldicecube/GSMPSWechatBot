# 免费API修复、进度分享与一次性引导

生产bot未重启，未向真实微信群发送验收消息。项目初始引导仍只在用户下一次手动重启后登记一次；持续的自主生活、进度分享和权限处理能力会保留。

## API实际结果

当前已启用两条经过验证的免费视觉路由：

| 优先级 | 模型 | 地址 | 实测 |
| --- | --- | --- | --- |
| 1 | nvidia/nemotron-3.5-super-vl:free | https://ai-gateway.kilo.ai/api/v1 | JSON、工具调用、工具结果往返、图片识别均通过 |
| 2 | dots-studio/dots-3-note-preview:free | 同上 | 四项均通过 |

这两个模型的八次契约测试均为HTTP200，接口使用记录中的cost为0。SDK→实际Host能力层→外部API另外七次实测通过，覆盖两条模型及Planner/Replyer/Memory、工具和视觉模式。没有发送生产群聊天内容，仅使用合成健康检查、工具结果和人工生成的红色图片。

旧入口 `https://api.kilo.ai/api/gateway` 的聊天调用此前反复返回 `PROMOTION_MODEL_LIMIT_REACHED`。本次切到[官方当前网关和匿名免费访问方式](https://kilo.ai/docs/gateway/authentication)，没有更换IP、伪装客户端或绕过限额。当前可调用不代表永久不限量，仍须遵守网关限流和原有核心退避。

Omni虽然普通JSON与图片测试通过，但工具调用没有获得有效结果，因此不再启用。额外候选Beam、Lightning与自动免费路由只做了普通JSON探针，没有满足本次完整视觉/工具验收，不设为生产路由。没有启用付费模型或隐藏付费回退。

DeepSeek优先调查结果：

- OpenCode公开目录仍有 `deepseek-v4.1-flash-free`，但[当前官方文档](https://opencode.ai/v2/docs/console/models/)明确免费聊天档要求OpenCode客户端。本机以真实WechatBot身份实测429 `FreeUsageLimitError`，不启用现有停用配置，不伪装其客户端。
- Token Harbor的[官方FAQ](https://tokenharbor.ai/faq)提供OpenAI兼容API及有额度的`:free`路由，但需要个人API密钥。本机模型目录及DeepSeek候选请求均返回401，无法确认该账号当前是否有可用DeepSeek免费额度，因此未接入。已向用户询问是否有本机密钥配置路径；不把未经认证验证的模型标为可用。
- Kilo及OpenRouter当前公开目录中的DeepSeek条目均显示非零价格，未调用这些付费条目。OpenRouter免费档同样需要个人密钥。

因此目前实际接入的是两条可用的免费视觉模型，尚未接入可用的免费DeepSeek。这是凭据/免费档资格的限制，不能用虚构密钥解决。

独立embedding保持原配置不动：旧入口与新入口的1024维合成文本embedding均实测成功。没有改变现有记忆向量维度、数据或图像embedding模型。

`config.json`为主配置，已通过既有同步器更新 `config/model_config.toml`；两条模型用于普通任务与视觉任务，按顺序回退。原有人设、原生风格继承、其他停用配置和独立embedding保留。备份在 `temp/free-api-life-backup-20261029-181340/`，包含本机配置，不应公开。

## 与自主生活合并

没有新增独立的“项目汇报定时器”。发现、研究、作品和项目进展都由 `autonomous_life` 单一协调器处理：

1. Planner按兴趣、真实经历和现有项目决定下一步，也可休息或做其他活动。
2. 实际工具执行后，模型判断进展是否值得分享；值得时提交候选。
3. 候选包含项目名、真实步骤、结果/阶段和下一步计划，原生Replyer再结合群聊判断是否发送，保留原有人设和风格，不编造进度百分比或把计划当已完成。
4. 仍共用每天最多4次、至少2小时间隔的主动分享预算；主动@使用现有真实成员证据和72小时冷却。
5. 冷却时按项目保存最新候选，每流最多8项，3天过期。相同代码/结果/阶段仅改写措辞不重复分享；不积压旧进度补发。候选不保存源代码或私人反思。
6. 提交结果未知、跳过或已消费的分享不会在重启后重发。项目暂挂/暂停时不发布其旧进度，也不冻结其他兴趣和正常自主生活。

权限不足依旧暂挂项目并按现有限额向固定操作者真实@申请；没有授予新能力，现有AppContainer隔离边界保持不变。

## 下一次启动的一次性引导

`project.request_id=self-chosen-project-20261029`和初始目标在下一次启动登记；消费标记与该次初始群集合通过同一个SQLite事务写入。重启、配置重新加载、增加群都不会重复登记同一标识。

首次有效决策会消费引导，准备一次公开研究。之后不再注入初始目标作为优先指令，项目变成正常兴趣/经历数据，由麦麦自主决定继续、分享、暂停或放弃。即使第一步网络失败，也会保留“尚未执行完成”的普通项目意图，不重新强制引导，也不丢失想做的事情。

持久化的项目、成果和经历会继续保留；这是长期连续性，不是每次启动重放初始指令。`project.enabled`不自动关闭，因为权限请求与操作者暂停/继续工具仍需要工作。若未来明确要登记一个全新的引导，须由操作者改变 `request_id`。

## 本次修改

- `config.json`、`config/model_config.toml`：已验证的免费路由与顺序；同步器同时保持 `config/bot_config.toml`的原生风格配置。
- `plugins/autonomous_life/project.py`：一次性引导消费、引导后的普通项目意图、只暂停受阻项目。
- `plugins/autonomous_life/store.py`：原子启动消费标记、统一进度候选、去重、最新进展合并与过期。
- `plugins/autonomous_life/plugin.py`：正常生活调度与进度分享接入、真实进度素材、共享预算。
- `plugins/autonomous_life/config.toml`、`_manifest.json`、`README.md`：进度分享开关和1.2.0说明。
- `scripts/maibot_free_api_probe.py`、`configure_free_api_routes.py`：合成契约验证和只启用已验证免费视觉路由的配置脚本。
- `scripts/maibot_project_acceptance.py`、`scripts/maibot_bridge_acceptance.py`、隔离fixture：一次性引导与冷却进度验收，并去除旧“三条路由”测试假设。

## 测试与范围

- 两条启用路由各4项真实外部API契约通过；第三条候选因工具失败被排除。
- SDK→Host→外部模型实测7项通过。
- 项目/生活合并专项57项通过，包括初始网络失败恢复、后加群不重放、受阻项目不冻结其他生活、进度跨重启与共享限额。
- 自主生活原有专项63项通过。
- 完整原生桥接94项通过，另含53项消息转译单元测试。新增真实Host/Runner调用、第二阶段AppContainer运行、冷却候选释放、原生Replyer进度数据和正确微信At验收。模型响应为确定性fixture，微信发送均被截获。
- 原生API实测是隔离实例的SDK/Host能力调用，不是生产群的实发验收。未完成24小时真实自主生活运行，也没有让生产bot提前执行本次引导。

报告位于 `docs/maibot-wechat-acceptance/`：`free-api-contract-20261029.json`、`free-api-native-20261029.json`、`deepseek-free-probe-20261029.json`、`free-api-life-bridge-20261029.json`、`project-kickoff-20261029.json`、`autonomous-life-20261029.json`。
