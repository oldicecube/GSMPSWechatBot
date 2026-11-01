# 模型池优先级与 system prompt 兼容性检查

## 结论

Gemini 和 DeepSeek **确实排在所有通用任务池的最前面**，包括 planner、replyer、utils、learner、memory 和 vlm。但它们当前对应的 provider 都是 `enabled = false`，因此运行时会在请求前过滤掉，不会产生实际请求。当前有效顺序是：

```text
Nemotron → Dots → Mistral
```

这就是日志里几乎看不到 Gemini/DeepSeek 的原因。排序正确，凭证/端点状态使其没有进入有效候选池。当前 Gemini provider 没有 API Key，DeepSeek 的免费端点此前验证不可用，所以没有强行启用。

## system prompt 探测

对配置中的全部 LLM 模型逐个检查了 provider，并对当前启用端点发送了最小请求：

- Nemotron：请求到达，但返回 429 免费额度限制。
- Dots：请求到达，但返回 429 免费额度限制。
- Mistral Small 3.2：system + user 消息请求成功，返回正常内容。
- Gemini、DeepSeek、Qwen、Kilo、GLM、Mimo：provider 当前禁用，标记为 skipped_disabled_provider，没有伪造“测试通过”。

所有 16 个 provider 已显式设置 `system_messages_at_start = true`，确保 system 消息被整理为首个 system message。探测脚本和结果：

- `scripts/model_system_prompt_probe.py`
- `docs/maibot-wechat-acceptance/model-system-prompt-probe-20261031.json`

## 400 请求格式修复

近期日志的 400 主要不是 system prompt，而是模型返回了带换行的工具函数名，例如：

```text
stop\\n\\n\\n\\n
```

上游随后拒绝该 assistant tool call，因为函数名只能包含字母、数字、下划线和连字符。已在 `src/llm_models/payload_content/tool_option.py` 的 `ToolCall` 构造处统一清洗：`stop\\n...` 会恢复为 `stop`，非法字符会被安全移除。这样 OpenAI、Gemini 兼容解析器和 DeepSeek 解析器都会经过同一层规范化。

## 当前配置

所有通用任务池仍保留 Gemini → DeepSeek → 其他模型的顺序；learner 也已恢复同样的优先级，不再单独把 Mistral 放到第一位。只有 provider 通过可用性/凭证检查后才会实际调用。

本轮没有擅自填写 API Key，也没有把已知不可用的免费端点强行打开。主 bot 未重启；provider 配置和工具名修复需要手动重启后完整加载。
