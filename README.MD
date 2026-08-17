# GSMPS WeChat Bot v3

基于 WeFlow 的 Windows 微信群聊机器人。项目负责接收群消息、执行插件、调用 LLM（多路 API 池），并通过统一发送接口回复文本、文件或语音。`core/` 负责路由和队列，`plugins/` 负责命令，`auto/` 负责后台事件，`llm/` 负责模型、提示词、主动回复、记忆和学习。

## 快速开始

环境要求：Windows 10/11、Python 3.10+、Node.js LTS，以及已登录的 WeChat/WeFlow 客户端。

```powershell
Copy-Item sample_config.json config.json
.\start.bat
```

也可以手动安装并运行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Push-Location weflow-core
npm ci
npm run build
Pop-Location
python main.py
```

至少需要配置 `target_group`、`prefix`、WeFlow 数据库路径、`weflow.myWxid` 和 `llm.apis`（多路 LLM API 池）。`llm.api_key` / `llm.model` / `llm.api_base` 保留为兼容别名，取 `apis` 第一路。`prefix` 是机器人的身份前缀；提示词和群聊记录中的 `<prefix>` 都明确表示 Bot 已经说过的内容，或当前消息正在指向 Bot。

## 仓库边界、数据与密钥

以下目录是需要版本控制的程序源码，其中 `llm/` 并非误推送的缓存或外部副本：`core/`、`llm/`、`plugins/`、`auto/`、`services/`、`utils/`、`assets/` 和 `weflow-core/` 中受 Git 管理的资源。删除远端顶层 `llm/` 会使 `core/dispatcher.py`、`core/worker.py` 和多个插件的导入失败，Bot 无法启动。

下列内容是本机运行数据，已由 `.gitignore` 排除，**不得提交**：`config.json`（密钥）、`prompt.txt`、`logs/`、`output/`、`data/*.sqlite3`、`data/groups/`、Python `__pycache__/`、`llm/logs/`、`weflow-core/node_modules/`、`weflow-core/dist/`，以及 `%TEMP%\WechatRobot\voice` 中的待发送语音。清理前先停止 Bot；只删除运行数据，不要按目录名猜测后删除源码。提交前应执行 `git status --ignored`，确认没有密钥、聊天记录、数据库或临时媒体进入暂存区。

## 路由与主动回复状态机

`prefix_mode=strict` 只处理命中 prefix 的消息；`mixed` 允许普通群消息进入 LLM 主动回复状态机。插件和明确命令优先于 LLM。

主动回复循环为：

```text
空闲期 -> 关注期 -> 工作期 -> 关注期 -> 空闲期
```

- 最近一分钟滑动窗口内消息数超过上界（默认 8 条，`density_upper_messages_per_minute`）时直接进入关注期；未超过上界时按密度概率进入关注期，随机静默超时仍未进入工作期则直接切入工作期并现场调用网页工具决定是否主动转发。
- `density_window_seconds` 控制滑动窗口；`idle_min_seconds` / `idle_max_seconds`（或新名称 `idle_quiet_min_seconds` / `idle_quiet_max_seconds`）复用原空闲期随机冷却配置。
- 从空闲期进入关注期时读取最新约 1000 tokens 群聊消息（`attention_context_token_budget`）。
- 工作期结束回到关注期时保留工作期完整上下文，并在进入关注期的瞬间重新抽取主动转发随机冷却。
- 关注期允许参与任何仍有连贯性的群聊话题，不要求 prefix；明显结束、已经转题或无法形成连贯贡献的碎片可以保持旁观。
- 关注期默认有 30% 的无厘头插话机会（`attention_nonsense_probability`）；连续约 2 小时没有 Bot 回复（`attention_boost_after_seconds`）时显著提高参与积极度。
- 每次关注期拉取后按 n*10% 递增概率触发一次黑话情绪回复（未触发计数器 n 在命中后清零，默认步长 0.1，配置 `attention_slang_emotional_step`）。
- 弱智吧主动转发使用独立的随机 2--3 小时计时器（`proactive_web_min_seconds` / `proactive_web_max_seconds`），到期后在当前工作期结束时立即进入一次主动网页工作期。等待期间 Bot 实际成功发送超过 10 条分开的文本消息（`proactive_web_reset_after_bot_messages`）时，立即重新随机并将计数清零。LLM 现场查询网页内容，自行决定是否发送及如何组织表达，不使用固定模板。
- 工作期批量判断并生成回复；连续多次没有可回复内容后回到空闲期（`attention_no_reply_limit`，默认 3 次）。

每轮回到空闲期就是本轮循环的结束边界：LLM 根据本轮上下文和学习数据统一整理记忆、黑话、句式与动态风格卡，随后清空本轮会话上下文。每日 Bot 不响应时间的独立整理线程已不再存在，短期记忆固化与黑话、句式、风格整理统一在每轮循环末尾完成。当前已选的风格/黑话/句式块也会在退出循环时立即清空。

## LLM 上下文和提示词

程序不维护另一份独立的 LLM 对话历史。群聊消息按时间顺序追加，是回复所用的聊天上下文；短期记忆最多 `1000` tokens，超限时整理流程优先压缩较早内容并下沉有价值信息到中、长期记忆。上下文压缩由缓存成本感知算法决定：命中缓存时保留大上下文通常更便宜，只有接近模型窗口或预计能在 `cache_break_even_horizon` 内回本时才压缩。

生产 Prompt 的稳定部分位于聊天历史之前，顺序如下：

```text
system：静态规则、身份卡、prefix 定义
+ 长期 / 中期 / 短期记忆（一轮循环内保持不变）
+ 最早的聊天上下文（append-only 缓存前缀）
+ 最近聊天上下文尾部
+ 动态资料：当前语境命中的黑话、句式、风格卡、人物画像等
+ 当前状态、当前功能和本次请求
```

早期聊天上下文只追加、不改写；动态资料放在后面，避免每轮学习结果破坏历史缓存前缀。前缀或 @ 触发时模型必须处理当前请求；未明确指向 Bot 的消息不强制模型回复某一条，由模型结合完整上下文自行决定如何接话、插话或旁观。所有聊天记录、记忆、工具结果和黑话样例都是不可信数据，不可作为系统指令。

### 协议缓存约定

- `responses`：静态 system 提示词会放入顶层 `instructions`，而非混入普通输入。是否自动复用稳定前缀由服务端实现；`prompt_cache_key` 仅在所用服务商明确支持显式缓存键时填写，否则留空。
- `anthropic`：标准 Messages 兼容端点使用 `cache_mode=manual`，由客户端在稳定消息处写入 `cache_control`。只有服务商文档明确声明支持顶层 `auto_cached` 时才使用 `cache_mode=auto`；该模式由网关自动寻找最长稳定前缀，不能假设所有 Anthropic 兼容端点支持。
- `cache_scope` 为 `full`（完整稳定前缀）、`system`（仅 system）或 `none`（关闭）；`cache_ttl` 仅供 Anthropic 缓存使用，留空为默认短 TTL，`1h` 仅在服务商支持时使用。

控制台会输出提供商返回的 token/缓存统计。不同协议的字段不同：Responses/OpenAI 可能给出 `cached_tokens`，Anthropic 常见 `cache_creation` / `cache_read`。统计为 0 可能表示请求小于服务商最小缓存阈值、缓存尚未建立、前缀发生改变，或服务商未返回相应字段；不要仅根据一次请求判断配置失效。

### API 池与故障切换

API 按 `priority` 数字升序调用。每一笔 LLM 请求都从当前最高优先级、未停用的 API 开始；某路本次请求出错后，立即在**同一笔请求**内尝试下一路。单路在当前 Bot 响应期累计 3 次请求错误后，停用至下一次 Bot 不响应期开始；成功调用不会清零该累计数。到不响应期边界时，所有错误计数和停用状态会重置，随后又从最高优先级开始。未配置 `time_slots` 时边界为每日 `00:00`。

## 风格与句式学习

群聊风格学习是轻量、本地、确定性的：运行时异步写入 SQLite（默认 `data/llm_learning.sqlite3`），不逐条调用 LLM；LLM 只参与每轮循环末尾的统一整理。

- 动态群聊风格卡（`style_cards`）：每轮循环结束由 LLM 根据本轮上下文、已有风格卡和统计证据决定 `replace` 或 `keep`。风格卡只记录抽象语气、整体节奏、避免模式和回复时机，不写入具体黑话、句式、昵称、人物名、服务器名、表情或样本原句。
- 本地风格统计（`group_profiles`）：样本数、平均字数、短消息占比、标点占比、碎片短句占比、句子断点、互动方式学习（接话/旁观/直接互动窗口）等，作为只读参考注入提示词。
- 句式库（`style_expressions`，maibot 式 situation→pattern）：每轮循环结束由 LLM 从本轮真实出现的表达中提取“当X情况用Y句式”，写入前先对比已有句式库，已存在的场景更新计数不重复新增。频率与召回排序由本地维护，置信度不来自 LLM。
- 回复时按当前语境命中注入：关键词/分词重叠命中优先，再用计数加权随机补足候选池（`expression_pool_size` 等配置），提示词标注“场景自然命中且合适时可用，不自然就忽略，不要硬套”。
- 使用计量：注入过的句式/黑话会在后续回复中统计是否真的被使用，循环末整理据此调整或删除“长期注入却从未使用”的条目。
- 行为模式库（`behavior_patterns`）：循环结束时从至少 10 条有效用户消息中学习“场景→行为→结果”，严格校验 `source_id`；工作期内 LLM 才能按需调用 `lookup_group_behaviors`，不会把行为模式预先塞进每次 Prompt。

## 风格切换（style_switch）

回复 JSON 支持可选的 `style_switch` 字段，让模型自己从场景/类型库中选择和切换黑话、句式、风格：

```json
{"messages": ["试试刷新一下吧"], "style_switch": {"scene": "游戏开服", "situation": "开服炸服", "slang_type": "游戏", "emotion": "吐槽"}}
```

- 切换目标必须来自已注入的候选或类型库（句式场景、黑话类型、情绪），程序会校验，无法匹配的切换会被忽略，模型不能凭空造条目。
- 切换成功后，相关条目以“当前已选风格/黑话/句式”块注入动态区，在后续回复中保持，直到模型再次切换或本循环结束。
- 已选块只在当前循环内有效，退出循环（回到空闲期）时立即清空，不会跨循环残留。
- 切换有冷却（默认 120 秒，`style_switch_cooldown_seconds`），避免模型频繁切换；`style_switch_enabled` 可整体开关。
- 纯黑话情绪回复、嵌入黑话、弱智吧搬运等“是否行动”仍由本地机会门（概率/冷却）控制，与风格切换互不干扰。

## 黑话学习

黑话数据库默认是 `data/llm_learning.sqlite3`，主要使用以下表：

- `slang_terms`：LLM 确认写入后的统计主表，保存规范化表达、出现次数、说话人数、时间、样例和本地置信度。
- `slang_scenarios`：同一条 LLM 确认记录的语义表，保存含义、场景、状态、类型、情绪、情绪强度、样例和可用资格。
- `pending_slang_terms` / `pending_slang_evidence` / `pending_slang_speakers`：不确定词的持久化待确认队列，只保存词、来源去重计数和说话人数，不保存聊天正文，也不参与回复注入。

同一群组中相同的 `(group_id, normalized_phrase)` 使用 SQLite 主键更新原记录，不会产生精确重复。项目不维护覆盖关系、共享子串关系、排除表、Trie/重叠分析等事后重复黑话维护。

每轮循环结束时，LLM 只从本轮非 Bot 对话上下文总结黑话，并通过 `slang_actions` 整理。短期/中期/长期记忆、人物画像和旧黑话说明都不能作为新黑话证据；每个 action 的短语必须能在本轮消息中找到，`source_ids` 用于记录来源。每个 add/update action 会查询 `lookup_similar_group_slang`，使用无 Embedding 的规范化文本匹配，返回相同、包含或共享明显文本的候选及其含义和样例。

- `reuse_existing`：填写已有候选的 `canonical_normalized_phrase`，更新原记录。
- `new_distinct`：确认它与候选是不同的群体表达后才新增。
- `uncertain`：本轮不进入可用黑话库；程序只保存来源已校验的词、出现次数和说话人数到独立待确认队列。该词以后再次出现在当前轮消息时，LLM 会看到累计证据并重新确认；待确认项不注入回复、不写入短期记忆，也不保存聊天正文。

模型也可以调用 `lookup_group_slang` 查询未直接注入的记录。只有 `active`、安全、置信度达标且命中当前上下文的黑话才直接进入回复提示词；类型、情绪和情绪强度的去重值放在身份卡后供模型选择。

置信度与安全判定由本地出现频率算法（出现次数、说话人数等）计算，LLM 只负责决定黑话是否入库及其含义、场景、示例，不输出 confidence 或 safe_to_use 字段。

短期记忆只保存事实、进展与话题摘要，不写入黑话、释义或样例；历史短期记忆中的已知黑话也会在运行时过滤。只有循环末尾的当前消息上下文通过来源、相似度和本地校验后，才会写入黑话库。学习库的 `cycle_curation_runs` 会记录每轮黑话 action、上下文命中、落库数和拒绝原因，不保存聊天正文。

## 记忆

- 短期记忆只在末尾追加；中期、长期记忆和人物画像的固化由每轮循环末尾的统一整理（`curate_cycle`）直接完成。
- 中期记忆表示本群近期高热度话题。
- 长期记忆表示本群稳定特色、事实和交流风格。
- 人物画像只保存消息明确表达或重复确认的事实，不推断敏感属性。
- 每类记忆没有硬字数上限，但提示词建议控制在 3000 字以内。
- 每轮循环结束都由 LLM 决定记忆与黑话、句式的新增、更新、删除和固化内容。

记忆数据库默认是 `data/memory.sqlite3`；群聊上下文保存在 `data/groups/<群名>/`。

## 工具和命令

LLM 可按需调用：

- `lookup_group_slang`：查询当前群黑话。
- `lookup_similar_group_slang`：写入前查询相同或相似黑话。
- `lookup_group_memory`：查询当前群记忆和人物画像。
- `lookup_group_behaviors`：工作期按当前场景查询最多 3 条只读互动行为模式。
- `fetch_original_message`：读取当前会话中疑似截断消息的原文。
- `fetch_webpage`：读取允许的公开网页。
- `fetch_tieba_hot_post`：读取百度贴吧（默认弱智吧）当前热门内容，供独立主动转发计时器到期后的工作期按需搬运。

管理员可使用 `/prompt` 将最近一次完整 LLM 提示词写入根目录 `prompt.txt` 并发送到群聊；`prompt.txt` 是忽略的本地调试产物，不应提交。管理员身份由 `llm.admin_wxids` 控制。其他插件命令见 [admin_commands.txt](admin_commands.txt) 和 [DEV_GUIDE.md](DEV_GUIDE.md)。

## 语音发送与音乐命令

语音发送只支持 Windows。需先以管理员身份运行 `scripts\install_vb_cable.bat` 安装 VB-CABLE，安装后重启 Windows。将微信的录音/通话麦克风设为 **CABLE Output**；Bot 仅向 **CABLE Input** 写入音频，不会将语音直接输出到物理扬声器。

- 发送组件会先切换到目标群聊，**长按 Shift**，等待 0.5 秒后将音频注入虚拟麦克风，录入结束后再松开 Shift。
- 默认使用裁剪后音频的实际时长；最多 60 秒。`duration` 为空时自动按音频实际长度处理。
- 所有待发送语音统一存在 `%TEMP%\WechatRobot\voice`，发送组件在流程结束后自动删除该目录内的语音文件。不要把其他业务文件放入此目录。
- 如果本机设备名不同，可用环境变量 `WECHAT_VOICE_PLAYBACK_DEVICE` 和 `WECHAT_VOICE_CAPTURE_DEVICE` 覆盖 `CABLE Input` / `CABLE Output` 关键字。

音乐插件只提供 `/song` 和 `/music` 两个别名，会从网易云音乐搜索并将第一条结果作为语音发送：

```text
/song <歌曲名>
<起始秒> <终止秒>
```

第二行可选。不填时发送前 59.5 秒；只填起始秒时发送从该位置开始的 59.5 秒。秒数支持浮点，非法起始值按 0 处理，非法或缺失的终止值按默认跨度处理。终止时间会截断至音乐实际时长，且与起始时间的跨度不超过 59.5 秒。

## 配置参考

`llm` 配置的主要分组：

- `identity`：Bot 名称、角色、风格、额外规则。
- `prompt`：最大回复条数、是否允许表情、是否禁止 Markdown 与解释、表情提示语、兜底回复、特殊规则与话题转移规则。
- `learning`：本地学习开关与参数（`style_card_max_chars`、`expression_pool_size`、`expression_selector_enabled`、`slang_min_occurrences`、`slang_emotional_pool_rotation`、`style_switch_enabled`、`style_switch_cooldown_seconds` 等）。
- `auto_reply`：主动回复状态机参数（各阶段时长、关注期上下文预算、无厘头/黑话情绪/弱智吧机会概率与冷却、批量窗口等）；工作期默认最多等待 15 秒或累计 20 条消息后处理，`reply_trigger_mode=reply_necessity` 时先用本地必要性评分过滤低价值插话，默认阈值 `reply_necessity_threshold=35`。
- `memory`：长期记忆与人物画像参数。
- `web_fetch_*` / `original_message_*`：网页与原始消息工具的上限与超时。
- `max_concurrent_requests` 默认为 1：Worker、主动回复和周期整理共享同一个 LLM/网页工具并发槽；忙时自主请求跳过，前缀/@ 请求最多等待 `direct_request_wait_seconds`。
- `request_timeout_seconds` 与 `tool_loop_timeout_seconds`：分别限制单次模型 HTTP 请求和一轮工具调用链，避免网页工具连续失败时长期占用 Worker。
- `auto_reply.reply_timing_*`：至少积累 20 个关注期判断后，按历史关注期回复率本地降低后续插话机会；直接前缀、@ 和白名单网址不受影响。
- `apis`：多路 LLM API 池。每路字段：`name`、`protocol`（`openai`=Chat Completions 兼容、`responses`=OpenAI Responses、`anthropic`=Anthropic Messages）、`model`、`api_key`、`base_url`、`timeout_seconds`（单次请求超时，限制为 10~120 秒）、`priority`（数字越小越优先）、`cache`、`cache_scope`（`full` / `system` / `none`）、`cache_ttl`、`cache_mode`（Anthropic：标准端点用 `manual`；仅已验证支持顶层 `auto_cached` 的网关用 `auto`；`off` 关闭）和可选 `prompt_cache_key`（仅在服务商明确支持时填写）。`apis` 存在时以最小 `priority` 作为实际主路；顶层 `model` / `api_key` / `api_base` 只为兼容旧配置保留镜像。故障时当前请求内立即按优先级回退；单路当前响应期累计 3 次错误后停用到下一个不响应期边界，届时恢复并清零计数。
- `cache_cost_ratio`（命中:未命中价格比，默认 5.428）、`cache_hit_rate`（完整缓存端点命中率估计，默认 0.85）、`cache_break_even_horizon`（压缩回本请求数上限，默认 40）：驱动成本感知的自动上下文压缩——命中缓存时保持大上下文更便宜，仅在硬上限或能在有限请求数内回本时才压缩。

完整示例见 [sample_config.json](sample_config.json)。

## 测试和检查

```powershell
python -m compileall -q core plugins auto services llm utils main.py
git diff --check
```

涉及 LLM API（OpenAI 兼容 / Responses / Anthropic Messages）、WeFlow、RCON 或 ComfyUI 的功能还需要对应外部服务可用后进行实际验证。提交前还应检查：

```powershell
git status --short
git diff --cached --check
git grep -n "sk-" -- ':!sample_config.json'
```

最后一条若命中真实凭据，应先从暂存区和历史变更中移除；`sample_config.json` 只能保留占位符。

项目结构、插件 API 和 LLM 运行约定见 [DEV_GUIDE.md](DEV_GUIDE.md)。

### 主动参与的成本控制

`auto_reply.reply_trigger_mode=conversation_pulse` 使用本地 `skip` / `defer` / `plan` 调度：只有 `plan` 才会发起一次正常的主动 LLM 调用。直接 @/前缀不受影响；持续话题会优先进入语义判断；碎片闲聊则需要群仍活跃、Bot 已静默足够久、群级冷却结束且概率命中。可用 `pulse_plan_cooldown_seconds`、`fragmented_chat_min_silence_seconds`、`fragmented_chat_min_interval_seconds` 和 `fragmented_chat_plan_probability` 调整成本与参与度。弱智吧搬运由独立的 2--3 小时随机计时器限频，并在 Bot 成功发送第 11 条分开文本消息时重新计时。
