# 配置参考（config.json）

项目启动时读取根目录 `config.json`。先复制 `sample_config.json` 再填写真实值；修改模板不会影响运行实例。

**安全提示**：`config.json` 可能包含 API Key、WeFlow 解密信息、RCON 密码和群成员 wxid，不能提交或公开。修改配置后重启 Bot。

## 1. 配置约定

- 路径可使用相对项目根目录的相对路径，也可使用绝对路径；Windows 反斜杠在 JSON 中写为 `\\`。
- 布尔值使用 `true` / `false`，数组使用 `[]`，对象使用 `{}`。
- 未声明或已废弃的字段不会改变运行行为。不要通过保留“假开关”表达注释。
- 以下默认值为字段省略时程序归一化后的值；最终仍以 `llm/config.py`、`llm/autonomy/config.py` 和调用方读取为准。

## 2. 顶层字段

| 字段 | 必填 | 说明 |
|---|---:|---|
| `token` | 是 | WeFlow/本地发送服务鉴权 token；缺失时 `main.py` 会生成并写回。 |
| `target_group` | 是 | 允许处理的群名数组；占位名会阻止启动。 |
| `prefix` | 是 | Bot 前缀数组，例如 `["@bot"]`。 |
| `prefix_mode` | 否 | `strict`：只放行前缀/@Bot/reply-to-Bot；`mixed`：普通消息也进入模型自主判断。默认 `strict`。 |
| `worker_num` | 否 | Worker 线程数。实际 provider 并发另受 `llm.max_concurrent_requests` 限制。 |
| `group_features` | 否 | 按群控制 `plugins_enabled`、`auto_plugins_enabled`、`llm_enabled`。 |
| `weflow` | 是 | WeFlow 的数据库、解密密钥、wxid、API 端口和资源路径。 |
| `time_slots` | 否 | 允许响应的时间段；空数组表示不限制。 |
| `rate_limit` | 否 | 消息、LLM、前缀和插件限流。 |
| `music` / `voice_sender` | 否 | 音乐下载与虚拟麦克风语音发送。 |
| `mc` / `rcon` / `rcon_tools` | 否 | Minecraft 查询与 LLM RCON 工具。 |
| `tournament` | 否 | 锦标赛统计配置。 |
| `print_server` | 否 | 打印兑换接口地址、token 和超时。 |

### 2.1 rate_limit

所有限流按“用户 + 分类”和“全局 + 分类”分别计数：

| 字段 | 默认 | 说明 |
|---|---:|---|
| `messages_per_minute` | `10` | 普通自动消息。 |
| `global_messages_per_minute` | `20` | 全局普通自动消息。 |
| `llm_messages_per_minute` | `10` | 普通 LLM 入口。 |
| `global_llm_messages_per_minute` | `20` | 全局普通 LLM 入口。 |
| `prefix_llm_messages_per_minute` | `10` | 前缀/@Bot/reply-to-Bot。 |
| `global_prefix_llm_messages_per_minute` | `20` | 全局前缀 LLM 入口。 |
| `plugin_messages_per_minute` | `10` | 命令插件。 |
| `global_plugin_messages_per_minute` | `20` | 全局命令插件。 |
| `auto_blacklist_enabled` | `false` | 超限后是否自动拉黑。 |
| `auto_blacklist_threshold` | `5` | 自动拉黑前的违例次数。 |

限流属于“是否放行”的本地边界。请求一旦放行，是否回复、回复内容和是否沉默仍由模型决定。

## 3. LLM 基础配置

| 字段 | 默认 | 说明 |
|---|---:|---|
| `llm.enabled` | `true`（必填） | 总开关。 |
| `llm.apis` | `[]` | 多协议 API 池，按 `priority` 从小到大调用。 |
| `llm.thinking_params` | `{}` | 主回复、主动批处理和工具循环使用的可见推理参数。 |
| `llm.group_message_max_records` | `5000` | 每群短期历史保留记录数，至少 100。 |
| `llm.context_window_tokens` | `200000` | 上下文窗口估算值。 |
| `llm.context_compression_target_tokens` | `160000` | 压缩目标预算。 |
| `llm.context_compression_retry_seconds` | `300` | 压缩失败后的按群冷却时间。 |
| `llm.cache_prefix_tokens` | `24000` | 稳定历史前缀预算。 |
| `llm.max_concurrent_requests` | `1` | provider 并发上限，归一化范围为 1 到 4。 |
| `llm.direct_request_wait_seconds` | `3` | direct 请求等待前台 provider 槽位的秒数。 |
| `llm.tool_loop_timeout_seconds` | `45` | 单次工具循环总超时。 |
| `llm.request_timeout_seconds` | `45` | 单次 HTTP 请求超时。 |
| `llm.identity` | `{}` | Bot 身份卡；`rules` 中可写主动行为方向、条件概率和边界。 |
| `llm.prompt` | `{}` | 消息数量、动画、Markdown、特殊规则等提示词约束。 |
| `llm.admin_wxids` | `[]` | 管理命令授权 wxid。 |
| `llm.intercept_auto_plugins` | `[]` | 可阻止前缀请求进入 LLM 的 auto 插件白名单。 |
| `llm.prefix_bypass_wxids` | `[]` | 允许这些 wxid 在无前缀时进入 LLM。 |

### 3.1 llm.apis[]

| 字段 | 说明 |
|---|---|
| `name` | 日志中的端点名。 |
| `protocol` | `openai`、`responses` 或 `anthropic`。`chat_completions` 等别名会归一到 `openai`。 |
| `model` / `api_key` / `base_url` | 必填连接信息。 |
| `priority` | 越小越先调用；缺省按数组顺序。 |
| `timeout_seconds` | 单端点超时，归一化到 10 到 120 秒。 |
| `max_tokens` | 可选输出上限；为 0 时使用协议默认值。 |
| `supports_images` | 是否允许把图片内容和图片工具暴露给该端点。 |
| `cache` / `cache_scope` | 是否启用缓存，以及 `full`、`system`、`none` 范围。 |
| `cache_ttl` / `prompt_cache_retention` | Anthropic/Responses 的缓存时长声明。 |
| `prompt_cache_key` | 可选的稳定缓存路由键覆盖值。 |
| `responses_explicit_cache_breakpoint` | Responses 显式断点兼容开关，默认关闭。 |

单次请求内，端点失败后继续尝试下一优先级端点。连续失败达到阈值后，该端点临时降到队尾，恢复成功后会回到原优先级。

### 3.2 thinking_params

`thinking_params` 支持两种写法：

```json
{
  "thinking_params": {
    "reasoning_effort": "high"
  }
}
```

该简写会按当前端点协议转换：

- `openai`：原样发送 `reasoning_effort`。
- `responses`：转换为 `reasoning: {"effort": "high"}`。
- `anthropic`：转换为启用 thinking 和受 `max_tokens` 限制的 `budget_tokens`。

也可以显式按协议配置：

```json
{
  "thinking_params": {
    "openai": {"reasoning_effort": "high"},
    "responses": {"reasoning": {"effort": "high"}},
    "anthropic": {"thinking": {"type": "enabled", "budget_tokens": 3072}}
  }
}
```

Provider 返回的可见 reasoning 会完整写入控制台：

```text
[LLM REASONING] api=... protocol=... chars=...
...
[/LLM REASONING]
```

支持的字段包括 Chat Completions 的 `reasoning_content` / `reasoning`、Responses reasoning output/summary，以及 Anthropic `thinking` block。模型若只返回隐藏推理，网关不会提供原文，系统不会伪造；content 中显式 `<think>`、`<analysis>`、`<reasoning>` 块也会单独显示并从协议正文剥离。

## 4. 记忆与学习

| 字段 | 默认 | 说明 |
|---|---:|---|
| `llm.memory.enabled` | `true` | 长期记忆开关。 |
| `llm.memory.db_path` | `data/memory.sqlite3` | 长期记忆 SQLite。 |
| `llm.memory.curator_interval_seconds` | `60` | 记忆整理间隔。 |
| `llm.learning.enabled` | `true` | 风格、表达、黑话和行为学习开关。 |
| `llm.learning.db_path` | `data/llm_learning.sqlite3` | 学习库 SQLite。 |
| `llm.learning.expression_prompt_max_items` | `3` | 每次注入表达数量。 |
| `llm.learning.expression_prompt_max_chars` | `1400` | 表达上下文最大字符数。 |
| `llm.learning.expression_eval_enabled` | `false` | 是否评估表达实际使用效果。 |
| `llm.learning.min_term_count` | `2` | 新黑话进入候选前的最低出现次数。 |
| `llm.learning.slang_curator_interval_seconds` | `86400` | 黑话整理间隔。 |
| `llm.learning.learning_write_enabled` | `true` | 是否向 LLM 暴露 `record_learning` 主动学习写入工具（记忆/句式/黑话候选/句式对比）。 |
| `llm.learning.sentence_compare_learning_enabled` | `true` | 是否暴露 `learn_sentence_reply_compare` 句式对比学习工具。 |
| `llm.learning.learning_write_max_chars` | `2000` | 单次主动学习写入的内容上限。 |
| `llm.learning.idle_content_shadow_mode` | `true` | true 时只记录 idle-content 选择，不发送。 |
| `llm.learning.idle_content_cache_max_age_seconds` | `43200` | 缓存帖子最长有效期。 |
| `llm.learning.idle_content_prefetch_min_interval_seconds` | `1800` | 预取最小间隔。 |
| `llm.learning.idle_content_prefetch_min_items` | `6` | 达到多少候选项后停止预取。 |

学习反馈只在发送确认成功后提交。silence 记录为 no-reply 决策，不会伪造 Bot 消息。

## 5. auto_reply

`llm.auto_reply` 控制 mixed 模式的 batch 和可选频控。标准模式为 `proactive_decision_by_llm=true`：本地只安排 batch 何时到达模型，模型通过 `should_reply` 决定回复或沉默；请求放行后不再由本地改写回复文本。

| 字段 | 默认 | 说明 |
|---|---:|---|
| `enabled` | 全局可为 true | mixed 模式的主动状态机开关。 |
| `reply_trigger_mode` | `conversation_pulse` | `frequency`、`reply_necessity` 或 `conversation_pulse`。 |
| `proactive_decision_by_llm` | `true` | 普通消息进入模型决策，不在本地预先固定回复率。 |
| `reply_frequency_gate_enabled` | `false` | 可选的放行前频控；不作用于前缀、@Bot、reply-to-Bot 等 direct release。 |
| `batch_debounce_seconds` / `batch_max_messages` | `8` / `20` | batch 刷新和上限。 |
| `conversation_lease_seconds` / `conversation_lease_direct_seconds` | `90` / `180` | 普通与 direct 会话租约时长。 |
| `conversation_lease_max_turns` | `2` | 租约最多延长轮数；到期只停止本地优先标记，不强制沉默或回复。 |
| `idle_min_seconds` / `idle_max_seconds` | `120` / `300` | 状态机 idle 阶段范围。 |
| `work_min_seconds` / `work_max_seconds` | `60` / `180` | working 阶段范围。 |
| `attention_min_seconds` / `attention_max_seconds` | `60` / `180` | attention 阶段范围。 |
| `work_extend_seconds` / `work_extend_threshold_seconds` | `180` / `180` | 工作期延长。 |
| `attention_message_limit` / `attention_no_reply_limit` | `10` / `3` | attention 上下文和静默轮次限制。 |
| `reply_cooldown_seconds` | `30` | 可选频控的最小间隔。 |
| `reply_burst_window_seconds` / `reply_burst_limit` | `90` / `2` | 短时回复数量限制。 |
| `reply_penalty_seconds` | `90` | 触发频控后的惩罚时长。 |
| `reply_penalty_direct_bypass` | `true` | direct release 绕过普通回复惩罚。 |
| `adaptive_idle_content_enabled` | `false` | 自适应 idle content 机会开关。 |
| `pulse_plan_cooldown_seconds` | `15` | pulse plan 冷却。 |
| `pulse_plan_threshold` | `24` | pulse plan 阈值。 |
| `fragmented_chat_plan_probability` | `0.25` | 碎片聊天放行概率。 |
| `fragmented_chat_min_silence_seconds` / `fragmented_chat_min_interval_seconds` | `120` / `180` | 碎片聊天限制。 |
| `reply_timing_probability_floor` | `0.3` | 回复时机学习最低概率。 |

`prefix_mode=strict` 下，前缀、@Bot、reply-to-Bot 直接进入完整模型回复链；本地只决定请求何时放行。`strict` 不再强制模型必须发送文本，模型返回 `messages=[]` 或安全 silent 结果时保持沉默。

## 6. group_profiles

`llm.group_profiles.<群名>` 只覆盖群级身份与节奏，常用字段：

- `identity`、`reply_style`、`behavior_style`、`group_prompt`、`private_prompt`
- `prefixes`
- `prompt`
- `auto_reply`

未提供的字段继承全局 `llm` 配置。旧 `group_profiles.<群名>.proactive` 已废弃。

## 7. autonomy

Autonomy 是独立侧脑链。只有同时满足以下条件才允许真实发送：

- `llm.autonomy.enabled=true`
- `shadow_mode=false`
- `direct_send_enabled=true`
- action kind 在 `allowed_action_kinds`
- 安全分类通过、预算未耗尽

| 字段 | 默认 | 说明 |
|---|---:|---|
| `enabled` | `false` | 总开关。 |
| `shadow_mode` | `true` | true 时只思考，不执行动作。 |
| `direct_send_enabled` | `false` | 真实发送二次开关。 |
| `check_interval_seconds` | `15` | 活跃/积压时的后台检查间隔（秒）。 |
| `idle_check_interval_seconds` | `1800` | 长时间无消息时的检查间隔上限（秒）；空转退避后的冷群慢基线，无消息也不会完全不思考。 |
| `backlog_urgent_threshold` | `3` | 待处理消息达到该数量时按最短间隔检查。 |
| `backlog_idle_grace_seconds` | `45` | 距最后一条消息低于该秒数视为活跃。 |
| `backlog_idle_scale` | `0.5` | 空闲超过宽限后，每空闲一秒叠加的检查间隔系数。 |
| `idle_backoff_factor` | `2.0` | 连续空转（无新消息且未行动）时每次乘以的退避系数（指数退避）。 |
| `idle_backoff_max_steps` | `6` | 指数退避最大步数，超过后保持慢基线不再倍增。 |
| `min_interval_seconds` | `60` | 同群机会最小间隔。 |
| `cooldown_seconds` | `300` | 记录机会后的冷却时间。 |
| `max_opportunities_per_hour` | `6` | 非 shadow 的每小时机会上限；0 关闭。 |
| `shadow_max_opportunities_per_hour` | `1` | shadow 每小时机会上限。 |
| `max_actions_per_hour` | `2` | 每小时真实动作上限；0 禁止动作。 |
| `max_duration_seconds` | `30` | 单次 autonomy 模型调用超时。 |
| `allowed_action_kinds` | `["reply"]` | 可执行动作类型。 |
| `allow_other_bot_commands` | `false` | 是否允许调用其他机器人命令。 |
| `other_bot_command_allowlist` | `[]` | 允许命令白名单。 |
| `other_bot_command_denylist` | 常见危险命令 | 强制拒绝名单。 |
| `thinking_params` | `{}` | autonomy 模型推理参数。 |
| `effect_judge_enabled` | `true` | 动作效果评估。 |
| `effect_judge_thinking_params` | `{}` | 效果评估推理参数。 |
| `effect_window_seconds` | `120` | 效果观察窗口。 |
| `autonomy_cycle_retention_days` | `30` | cycle 保留时间。 |
| `log_json_console` / `log_json_file` | `true` / `false` | 结构化日志输出。 |
| `log_file_path` | `logs/autonomy.jsonl` | JSONL 日志路径。 |
| `log_sample_rate` | `1.0` | 日志采样率。 |
| `guidance_examples` | `[]` | 行为方向示例和条件概率提示，不是硬编码配额。 |
| `system_guidance` | `""` | 额外系统提示。 |

`sample_config.json` 当前使用高频真实执行测试值，仅用于验证，不建议长期直接用于生产。六个主动行为参考已写入 `llm.identity.rules`；其中的概率是测试倾向，不会保证行为发生。当前没有 Python 沙盒执行链，模型不得声称已经运行代码。

## 8. 可选工具与服务

### 8.1 RCON 工具

- `rcon_tools.enabled=false` 时完全不向 LLM 暴露 RCON 工具。
- `allowed_groups` 和 `allowed_users` 必须显式配置；空数组表示拒绝。
- `max_calls_per_request` 默认 2，`timeout_seconds` 默认 8。

### 8.2 打印兑换

`print_server.redeem_url`、`redeem_token`、`timeout_seconds` 供 `/redeem` 使用。token 为空时相关命令应明确失败，不应伪造成功。

### 8.3 语音

`voice_sender.playback_device_name`、`capture_device_name` 对应 CABLE 虚拟设备。真实语音发送需要在目标 Windows 环境验证。

## 9. 已移除或不再读取的字段

以下字段不产生运行行为，已从模板和当前配置中移除：

- `llm.history_expire_ms`
- `llm.max_history_chars`
- `llm.group_message_limit_chars`
- `llm.learning.expression_selector_enabled`
- `llm.auto_reply.proactive_local_reply_filter_enabled`（请求放行后不再由本地改写模型回复）
- `llm.group_profiles.<群名>.proactive`
- `llm.auto_reply.idle_enabled` 等旧拆分开关

历史长度使用 `group_message_max_records`；主动链路使用 `auto_reply`；学习表达选择使用当前学习库配置。

## 10. 验证

```powershell
python -m json.tool config.json
python -m json.tool sample_config.json
python -m compileall -q llm core main.py tests
python -m unittest discover -s tests -t . -q
```

JSON 校验、单元测试和 compile 不等于真实微信、RCON 或打印端到端验证。外部集成必须在目标环境单独确认。


## 本地资源与发布边界

- `mc.host` / `mc.server` 必须自行填写；未配置时 Minecraft 查询不会连接任何默认生产服务器。
- `llm.learning.bot_names`、`llm.identity.name` 与配置前缀用于识别本部署的机器人，源码不包含特定群的昵称默认值。
- 离线导入可重复使用 `python -m llm.learning --input <本地导出> --group <本地群名> --bot-name <机器人昵称>`，导出文件和生成的学习库不提交。
- `plugins/jane_prompt.txt` 是可选本地风格提示词；缺失时使用虚构的通用校园段子风格。
- 音乐源脚本放在 `music.script_dir`，需自行取得授权并配置访问凭据；实际脚本不随 Git 分发。
- Otto 资源布局见 `assets/otto/README.md`，音频语料和索引只保留本机。
- 发布前检查 `git diff --cached`，不要使用 `git add -f` 上传被忽略的配置、用户数据、测试材料或第三方私有资源。


## 三段式主动请求调度

普通群消息先由本地 `idle / attention / working` 状态和密度门控聚合，再决定是否调用 LLM。模型仍决定回复内容或静默；显式前缀、@、回复 Bot 和有界会话续接不被低密度门控阻塞。

配置位于 `llm.auto_reply`，可在 `llm.group_profiles.<群名>.auto_reply` 覆盖：

| 字段 | 默认 | 含义 |
| --- | ---: | --- |
| `density_gate_enabled` | `true` | 开启本地请求准入；关闭时回到旧的候选链路 |
| `density_low_messages_per_minute` | `3` | 低密度上界，按滚动窗口归一为每分钟消息数 |
| `density_high_messages_per_minute` | `8` | 高密度下界，中间为中密度 |
| `density_low_accumulate_messages` | `4` | 低密度先积累4条，单条长文本不直接唤醒模型 |
| `density_low_accumulate_seconds` | `600` | 待请求消息最长保留10分钟；完整聊天历史由原存储保留 |
| `density_low_cooldown_seconds` | `600` | 低密度新窗口至少间隔10分钟 |
| `density_medium_batch_messages` | `3` | 中密度开启窗口前所需积累数 |
| `density_high_batch_messages` | `2` | 高密度开启窗口及窗口内新批次的最少消息数 |
| `density_burst_seconds` | `120` | 固定活跃窗口长度，不因每条消息无限延长 |
| `density_burst_idle_seconds` | `60` | 无新消息60秒则结束窗口 |
| `density_request_interval_seconds` | `20` | 窗口内普通请求至少间隔20秒，并保留既有 debounce |
| `density_burst_max_requests` | `4` | 每窗口最多4次普通批次准入，模型静默也计入 |
| `density_burst_cooldown_seconds` | `180` | 窗口结束或预算用尽后冷却3分钟 |

默认聚合把有新消息的请求集中在短窗口内，增加稳定前缀仍被服务端缓存的机会；不发送空批次、保活或虚构“暖缓存”请求。窗口聚合不能保证上游缓存寿命或命中率，长模型响应、工具循环和显式请求仍可能拉长实际间隔。

启用门控时不再独立发起闲置 attention/周期末模型整理；已有记忆、学习的本地观察和按需上下文压缩继续保留，避免稀疏群聊每次周期结束额外调用模型。独立计划提醒、命令插件及 FX 新闻调用有各自入口，不属于普通主动批次的4次预算。

## FX 模拟波动与事件

铁锭、绿宝石、钻石使用外汇基准标准差的 **50倍**：每分钟约 `1.36% / 2.25% / 2.31%`。这是收益标准差放大，不是把价格乘50，也不是将所有价格变动强制设为50%。单分钟对数收益限幅随之扩大为 `±0.10`，事件可额外改变趋势与波动。金锭 XAU/USD、铜锭 USD/JPY 不参与模拟放大。

事件包括趋势增强、均值回归、流动性收缩、阶段性反转、振荡、平静期、板块轮动和冲击衰减。部分事件持续240～480个开盘分钟，LLM 事件最多720个开盘分钟；闭市暂停、次日恢复，进度存储于SQLite。重启及补算不重置事件或原有持仓。此模式波动与杠杆风险高于真实外汇基准，不将其描述为真实外汇日常波动。
