# 2026-10-31 模型延迟、优先级与原生联网修复

本次没有重启生产主进程，没有发送测试群消息，没有修改 `/player`。源码与原生联网配置需要用户手动重启加载；实测在隔离实例运行。

## 日志结论

- 原任务每个候选允许 90 秒硬超时，多个备用串行等待会累计数分钟。`llm_usage` 的总耗时还包含前面失败线路及限流排队，不能直接当作最后成功端点的纯生成耗时。
- OVH Qwen 多次返回 `400: System message must be at the beginning.`：原生上下文中的多个 system 参考项不符合该端点模板。
- Kilo 匿名免费线路存在真实的 `429 PROMOTION_MODEL_LIMIT_REACHED`。小请求成功不代表额度持续可用，后续约 1.5 万 token 的合成上下文测试中，Nemotron 与 DOTS 都触发了免费额度限制。
- 原配置 `max_retry=1` 已经表示总共一次尝试，OpenAI SDK 内层重试为 0；不是嵌套重试导致。
- OVH 免费线路的 32 秒请求间隔必须保留，高并发后台学习、表情包描述与聊天仍可能排队。免费池没有长期可用性保证。

## 已修改的行为

1. 所有通用任务 `planner/replyer/utils/learner/memory/vlm` 使用同一顺序与 `sequential`：Gemini → DeepSeek → Nemotron Super VL → DOTS → Qwen 397B → Qwen 27B → Mistral Small。后半段为当前部署的能力偏好，不宣称通用智力跑分。
2. 缺少凭据的 Gemini/DeepSeek 与当前实测失败的两条 Qwen 保留定义和优先位置，但提供商停用。新增原生调度修复：自动选择跳过停用提供商，启用有效凭据后可恢复原优先位置；显式点名模型不隐式换模型。
3. 当前实际启用的生成模型顺序为 **Nemotron Super VL → DOTS → Mistral Small 3.2**。均声明视觉能力；新增 Mistral 通过真实图片和工具回传测试。
4. 通用任务硬超时从每候选 90 秒改为 45 秒；启用提供商的 HTTP 超时为 30 秒、总尝试次数为 1。45 秒包含 OVH 的排队间隔，并非全部候选合计 45 秒。
5. 新增提供商配置 `system_messages_at_start`。仅在对应 OpenAI Chat Completions 出口合并 system 内容，保留所有原始指令、非 system 消息顺序、工具 ID、参数与返回。OVH 开启此兼容。未修改原生人设或风格提示词。WebUI 提供商高级设置新增对应开关。
6. 保留跨任务 429 退避、额度耗尽冷却及取消清理。没有通过更换身份、绕过认证或增加请求次数躲避上游限制。
7. 文本 embedding 与图片 embedding 的模型、维度、任务配置和向量空间保持原样。

## 免费渠道实测

测试均为合成提示词、合成红色图片与虚拟工具，没有向新渠道重放私人群聊。

| 渠道/模型 | 结果 | 处理 |
|---|---|---|
| Kilo Nemotron 3.5 Super VL | 五项小请求契约全通过，约 1.25–1.94 秒；后续真实额度限制 429 | 保留高优先能力顺序，运行时退避 |
| Kilo DOTS 3 Note | 五项全通过，约 1.74–5.27 秒；后续额度限制 429 | 保留备用与退避 |
| OVH Mistral Small 3.2 24B | 五项全通过，约 2.28–4.62 秒；约 1.5 万 token 工具选择 12.83 秒 | 已加入正式免费池 |
| OVH Qwen 397B / 27B | 约 26 秒读取超时，历史还有 strict-system 400 | 保留定义、暂停自动使用 |
| OVH Qwen 9B | 工具回传超时 | 不加入正式池 |
| Kilo Nemotron Omni | 小请求成功，工具回传与后续请求 429 | 保持停用 |
| OpenCode DeepSeek free | 429，提示免费模型需在 OpenCode 内使用 | 不伪装客户端，不启用 |
| Navy / Pollinations Gemini | 无凭据请求 401 | 不启用 |
| Gemini 官方 / NVIDIA DeepSeek | 没有有效本地凭据 | 保留停用模板，未宣称测试通过 |
| freeseek DeepSeek Flash | 五项复测通过，约 2.48–4.70 秒；长上下文 10.16 秒 | 服务明确仅供个人评测，不允许持续服务后端，因此不加入正式池；评测凭据只保存在被 git 忽略的本地数据目录 |
| 麦块云免费 DeepSeek | 官网为预约、尚未公布可调用接口 | 不添加虚构端点 |

已查询的其他正规免费渠道（Google AI Studio、SiliconFlow、Gitee AI、OpenRouter 等）仍需用户自己的凭据；没有注册账户、购买额度或启用计费。

原始证据：`docs/maibot-wechat-acceptance/llm-latency-20261031.json`、`llm-new-models-20261031.json`、`llm-new-channels-20261031.json`、`llm-context-20261031.json`、`freeseek-retest-20261031.json`。

## 原生联网

- 配置原生 MCP 连接 `Parallel-Free-Search`，地址 `https://search.parallel.ai/mcp`，无需认证。
- 原生 `web.services.Parallel-Free-Search.adapter = "parallel_mcp"`，并分配到 `model_task_config.web_search`。该任务列表引用搜索服务名称，不是聊天模型名称；决定是否搜索及理解检索证据由统一 Planner 模型池负责。
- 薄适配器复用 MaiBot 进程级 MCP 连接，映射 Parallel 的 `objective/search_queries/urls/excerpts` 协议；同一真实会话使用稳定 SHA-256 标识，不向服务暴露原群 ID。
- 原生 `web_search/web_fetch` 保留来源、外部资料标记、正文摘录语义、原文编号、4000 字预览和回查。明确错误不会冒充搜索成功。
- 只隐藏被统一联网服务接管的同一 MCP 服务器的原始重复工具，不隐藏其他 MCP 服务或其他工具。
- 已撤销自主生活插件 `internet_search/read_webpage` 的 LLM 工具注册。保留自主生活、后台安全网页探索与内部兼容方法，不删除项目或分享状态。
- WebUI 搜索服务弹窗新增 Parallel MCP，正确显示已有 MCP 连接选择，无需填写不存在的 API Key。

真实验收已完成：原生 MCP 搜索 Python TaskGroup 官方文档 → 读取网页 → 实际 MaiBot OpenAI 适配器选择 `web_search` → 取回真实来源 → 工具结果回传 → 模型带官方文档网址回答。没有实群发送。详见 `native-search-20261031.json`。

## 修改文件

- `config/model_config.toml`：统一任务顺序、停用失败线路、新增 Mistral、超时、严格 system 开关、原生联网功能分配。
- `config/bot_config.toml`：仅添加原生 MCP 连接；人设、昵称、行为和表达设置保持原义。
- `src/config/model_configs.py`、`src/llm_models/openai_compat.py`、`src/llm_models/model_client/openai_client.py`：提供商兼容字段与出口序列化。
- `src/llm_models/utils_model.py`：自动选择跳过停用提供商。
- `src/web_search/config.py`、`service.py`、`provider.py`、`adapters/parallel.py`、`adapters/stepsearch.py`：原生搜索协议和通用错误处理。
- `src/mcp_module/provider.py`：只隐藏被原生联网接管的重复工具。
- `plugins/autonomous_life/plugin.py`、`_manifest.json`、`README.md`：撤销重复会话工具注册、保留自主生活功能。
- `dashboard/src/routes/config/webSearch/types.ts`、`WebSearchDialog.tsx`：Parallel 服务编辑。
- `dashboard/src/routes/config/modelProvider/types.ts`、`ProviderForm.tsx`：严格 system 兼容开关。
- 新增探测、配置应用与回归脚本：`scripts/maibot_llm_latency_probe.py`、`maibot_llm_context_probe.py`、`maibot_native_search_acceptance.py`、`maibot_llm_format_acceptance.py`、`configure_llm_latency_repair.py`、`configure_native_web_search.py`。
- 更新 `scripts/maibot_api_pool_check.py`：健康状态 → Gemini/DeepSeek 偏好 → 部署能力顺序；不再按延迟提升弱模型。
- 更新原有桥接及研究验收，避免被停用模板或已撤销的旧工具影响；确定性回归不连接真实 MCP 服务。

## 已知限制与生效

最终验收：原生桥接 118 项、消息格式/优先级/原生搜索协议 9 项、真实原生联网链路 7 项、旧研究辅助方法 19 项、限流恢复等运行修复 9 项、WebUI 提供商表单 27 项均通过。TypeScript 检查和 Vite 生产构建通过，仅有既存的 bundle 大小提示。配置文件最终 TOML 往返解析通过；六个通用任务顺序一致，embedding 和人设保留断言通过。

生产主进程仍为用户原进程，源码不会因改文件自动替换已导入的核心模块。旧进程不认识新的 `parallel_mcp` 配置枚举，会保留上一次合法配置快照；需要用户手动重启一次加载源码与完整配置。不要把已写入磁盘的配置和隔离实例测试误报成现有进程已全部生效。

Gemini/DeepSeek 的首选顺序已经配置；缺少可用于正式运行的免费凭据时会跳过。当前匿名免费池仍受上游真实额度与排队限制，无法保证全天高并发低延迟。若将来在 WebUI 给对应停用提供商填入合法免费 Key，并启用端点，任务优先位置已经预留。

备份路径、统一顺序、运行模式与最终验收结果集中记录在 `docs/maibot-wechat-acceptance/llm-repair-summary-20261031.json`。

## 公开一手资料

- [Kilo 匿名认证与限制](https://kilo.ai/docs/gateway/authentication)
- [OpenCode 推理 API 限制](https://dev.opencode.ai/v2/docs/console/api/inference)
- [Parallel Search MCP 官方接入与匿名免费说明](https://docs.parallel.ai/integrations/mcp/search-mcp)
- [DeepSeek 官方 API 接入](https://api-docs.deepseek.com/zh-cn/)
- [freeseek 评测配额与禁止生产后端的条款](https://freeseek.1lm.io/terms)
- [麦块云免费 API 公告](https://minekuai.cn/api.html)
