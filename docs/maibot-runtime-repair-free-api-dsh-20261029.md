# 运行异常修复与免费 API 实测（2026-10-29）

已接入 OVHcloud 两个匿名视觉模型作为低优先级兜底。没有手动重启、停止或向生产进程注入代码；主进程仍为 PID 14880，启动时间 21:52:20。QQ 接入研究按要求暂停。

## 免费渠道调查与接入

| 渠道 | 本机实际结果 | 处理 |
| --- | --- | --- |
| OVHcloud Qwen3.5-397B-A17B | 文本 JSON、自动工具调用、工具结果回传、图片识别通过；MaiBot 原生工具与六图输入测试通过 | 已启用，priority=200 |
| OVHcloud Qwen3.8-27B | 同上；曾有一次原生调用 90 秒超时，随后两次复测通过 | 已启用，priority=210，作为更后面的兜底 |
| Kilo Nemotron VL、Dots、Auto | 22:05 实测 HTTP 429 / PROMOTION_MODEL_LIMIT_REACHED | 保留原线路，额度冷却后探测，不宣称恢复额度 |
| OpenCode DeepSeek V4.1 Flash、GLM、MiMo 免费线路 | 22:05 实测 HTTP 429 / Free usage limit reached | 不把同一额度池当作新增渠道 |
| LLM7 MiniMax M2.7 | 简单文本请求 HTTP 200；目录中的免费模型没有所需视觉能力，DeepSeek/Gemini 为收费档 | 未接入聊天链路 |
| Navy DeepSeek/Gemini | 需要用户自己的有效 API Key；当前没有认证凭据 | 保留之前建立的禁用配置，优先级 2、3 |
| Google Gemini | 当前没有可用认证凭据 | 保留之前建立的禁用配置，优先级 1 |
| Pollinations DeepSeek | 目录有非 paid-only 路由，但请求要求 API Key；匿名请求 401 | 不把目录存在当作已经可用的免费额度 |
| OpenRouter | 当前公开模型目录未发现输入、输出价格均为零的 DeepSeek/Gemini | 未接入 |
| Ollama Cloud DeepSeek | 当前是按量付费，需要 Key | 未作为免费渠道接入 |

DeepSeek/Gemini 的优先位置没有让给其他模型，但禁用配置没有参与实际调用。此次没有找到新的、无需认证且本机成功调用的免费 DeepSeek/Gemini 渠道。

已阅读 DSH 插件仓库及实际路由说明：

- [dsh-our-free-model](https://github.com/Ebony-Vinyl/dsh-our-free-model) 的两个公开免密来源是 OpenCode 和 Kilo，与已有线路共享上游限制。其 EAC 渠道另外需要 GitHub 授权；没有绕过授权或提取封装凭据。
- [dsh-vision-router](https://github.com/ysr666/dsh-vision-router) 提供了 OVHcloud 匿名接口线索；已直接适配 MaiBot 的原生 OpenAI 兼容接口，没有把 DSH 插件当作 MaiBot 插件安装。
- [OVHcloud 官方说明](https://docs.ovhcloud.com/en/guides/public-cloud/ai-machine-learning/ai-endpoints-getting-started) 明确匿名限制为每 IP、每模型每分钟 2 次。配置使用无认证模式，没有开通收费项目。
- [Navy 免费计划](https://api.navy/) 当前标注每日 150K tokens、20 RPM；仍需账号 Key 和实际权限验证。[LLM7 限额](https://docs.llm7.io/limits) 与可用模型档位分别核对，未将收费 DeepSeek/Gemini 误作免费。

OVH 基础地址为 `https://oai.endpoints.kepler.ai.cloud.ovh.net/v1`。每模型跨任务共享 32 秒请求起始间隔，聊天优先于尚未发出的后台任务；接口硬超时仍为 90 秒。此限速仅覆盖本进程，其他同公网 IP 客户端仍可能消耗额度。

397B 不接受强制命名函数形式的 `tool_choice`；MaiBot 实际使用的 `auto` 已通过。未修改原生工具选择行为。

## 日志问题及修复

1. **历史表达式回填反复崩溃**：数据库有 415 条 `modified_by='legacy'`，原生枚举只接受 AI、USER 或空值。先做 SQLite 在线完整备份和逐行审计，再把这些来源未知的标记改为 NULL；正文、计数、审核状态均保留。新增启动兼容修复，处理已知旧标记 legacy/learned/空字符串，不伪造 AI 或人工来源。
2. **额度耗尽反复重启思考循环**：显式额度 429 跨任务冷却 600 秒；普通限流至少退避 30 秒，并尊重数值 Retry-After。所有候选模型均受限时抛出可识别的冷却状态，思考消费任务等待恢复，不再每十几秒崩溃重启；新凭据或新线路热更新可以结束等待。壳子收到失败及 retry_after，不会把暂停误报为成功。已执行过工具的失败轮次不会自动重放，避免重复发送或重复执行。
3. **新线路群聊 400**：22:47 生产日志显示 `At most 5 image(s) may be provided in one prompt`。原生普通图片预算为 64，OVH 硬限制为 5。新增端点字段 `max_images_per_request=5`，在图片缩放、切图后复用原生图片限制器；保留最新图片，旧图转为文本占位，保留消息身份、正文和工具调用关联。其他端点默认值 0，维持其原有行为。
4. **FX 行情源重复 429/超时**：取消失败后立即进行的相同 GET；每 URL 独立指数退避，保留有效缓存，尊重数值 Retry-After。外部服务可用性仍由上游决定。

**已经在当前进程生效的部分**：在线数据修复后没有再出现 legacy 枚举解析异常；22:33:33 日志确认表达式索引追平，共 482 条，deferred=0、isolated=0；22:41:52 再次确认无待补项。没有替换 embedding 模型或混用向量空间。

**需要用户手动重启的部分**：新增 Python 代码，包括冷却等待、共享节流和端点图片上限。配置文件可能由现有监视器自动读取，但旧进程仍运行旧代码，不能据此宣称生产 400/429 已全部消失。生产状态证据见 [运行复核](maibot-wechat-acceptance/runtime-production-recheck-20261029.json)。

## 本次修改文件

- `src/common/database/expression_compat.py`、`src/common/database/database.py`：旧表达式来源兼容与启动修复。
- `src/llm_models/exceptions.py`、`src/llm_models/quota_cooldown.py`、`src/llm_models/request_pacing.py`、`src/llm_models/utils_model.py`：限额识别、退避、共享节流、端点图片上限与错误传播。
- `src/maisaka/reasoning_engine.py`、`llm/core/maibot_runtime.py`：保持消费任务、等待恢复、向壳子报告真实失败。
- `src/config/model_configs.py`、`llm/core/maibot_native_config.py`：两个可选端点字段及配置同步。
- `plugins/fx.py`：行情请求退避。
- `config.json`、`config/model_config.toml`：添加两个已验证的 OVH 视觉兜底。同步器也写入 `config/bot_config.toml`，没有借此更改自定义人设或插件行为配置。
- `scripts/configure_ovh_free_apis.py`：验证新鲜的四项接口测试报告后备份、写入、同步配置。
- `scripts/maibot_free_api_probe.py`：支持真实请求间隔与 auto 工具选择测试。
- `scripts/maibot_ovh_native_probe.py`、`scripts/maibot_runtime_repair_acceptance.py`、`scripts/maibot_bridge_acceptance.py`：原生验证和回归。桥接脚本修复无插件模式检查，并以确定性图片嵌入传输替代真实限额接口；实际图片工具、资产、探测和索引路径继续运行。

已有其他未提交改动保留，没有提交或覆盖历史工作。

## 验收与证据

- 修复回归 **9/9 通过**：数据库兼容与数据保留、额度到期/凭据隔离、普通与显式 429、Retry-After、思考任务恢复及取消、节流队列取消清理、图片请求投影上限与原上下文不变、FX 退避。[报告](maibot-wechat-acceptance/runtime-repair-20261029.json)
- 消息桥接 **59/59 项通过**，其中转译和发送单测 **53 个通过**；覆盖入站字段、真实 @ 身份、引用丢弃、富媒体顺序、原生工具、发送回执、限流失败传播及恢复、权限和正常关闭。[报告](maibot-wechat-acceptance/runtime-repair-bridge-20261029.json)
- 两个 OVH 模型均通过四项真实 HTTP 合约测试：[397B](maibot-wechat-acceptance/dsh-ovh-Qwen3.5-auto.json)、[27B](maibot-wechat-acceptance/dsh-ovh-Qwen3.8.json)。
- 最终 MaiBot 原生验证两个模型均通过工具调用、图片识别；每次图片输入为 **6 张**，实际发送经端点上限处理，服务返回成功。[最终原生报告](maibot-wechat-acceptance/ovh-native-image-cap-20261029.json)
- 保留 27B 首次原生复测超时记录，避免掩盖波动：[首次记录](maibot-wechat-acceptance/ovh-native-20261029.json)、[单独复测](maibot-wechat-acceptance/ovh-native-27b-recheck-20261029.json)。
- 16 个本次 Python 文件编译检查通过。
- API 测试仅使用合成提示词和纯色图片；未重放群聊快照，未向真实群聊发送验收消息。桥接测试拦截实际微信发送，因此不等同于实机微信投递验收。
- 图片嵌入外部线路在桥接回归中使用本地确定性传输，不宣称该外部 API 已重新完成可用性测试。表达式文本 embedding 的生产回填成功有独立日志证据。

数据备份及审计：`temp/expression-source-repair-20261029-220814/`。
配置备份：`temp/ovh-api-config-backup-20261029-223632/`、`temp/ovh-api-config-backup-20261029-225403/`。备份含原有凭据，应继续仅保存在本机。

## 遗留限制

上游已耗尽的免费额度无法由本地代码恢复。新 OVH 线路是真实可用的有限额兜底，单请求也存在时延波动，不应理解为无限免费或服务承诺。DeepSeek/Gemini 新渠道仍缺有效认证；相关优先位置已保留。生产代码最终验收需要用户手动重启后观察，当前没有执行这一步。
