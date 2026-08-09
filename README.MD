# GSMPS WeChat Bot v3

基于 WeFlow 的 Windows 微信群聊机器人。项目负责接收群消息、执行插件、调用 DeepSeek，并通过统一发送接口回复文本或文件。`core/` 负责路由和队列，`plugins/` 负责命令，`auto/` 负责后台事件，`llm/` 负责模型、提示词、主动回复、记忆和学习。

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

至少需要配置 `target_group`、`prefix`、WeFlow 数据库路径、`weflow.myWxid` 和 `llm.api_key`。`prefix` 是机器人的身份前缀；提示词和群聊记录中的 `<prefix>` 都明确表示 Bot 已经说过的内容，或当前消息正在指向 Bot。

## 路由与主动回复状态机

`prefix_mode=strict` 只处理命中 prefix 的消息；`mixed` 允许普通群消息进入 LLM 主动回复状态机。插件和明确命令优先于 LLM。

主动回复循环为：

```text
空闲期 -> 关注期 -> 工作期 -> 关注期 -> 空闲期
```

- 空闲期随机等待（默认 20–40 分钟，`idle_min_seconds` / `idle_max_seconds`）后进入关注期。
- 从空闲期进入关注期时读取最新约 1000 tokens 群聊消息（`attention_context_token_budget`）。
- 从工作期进入关注期时保留工作期完整上下文，并叠加最新约 1000 tokens 消息。
- 关注期允许参与任何仍有连贯性的群聊话题，不要求 prefix；明显结束、已经转题或无法形成连贯贡献的碎片可以保持旁观。
- 关注期默认有 30% 的无厘头插话机会（`attention_nonsense_probability`）；连续约 2 小时没有 Bot 回复（`attention_boost_after_seconds`）时显著提高参与积极度。
- 每次关注期拉取后按 n*10% 递增概率触发一次黑话情绪回复（未触发计数器 n 在命中后清零，默认步长 0.1，配置 `attention_slang_emotional_step`）。
- 关注期按冷却与概率提供弱智吧热门内容搬运机会（默认至少间隔 6 小时、概率 0.5，`tieba_repost_min_interval_seconds` / `tieba_repost_probability`）。
- 工作期批量判断并生成回复；连续多次没有可回复内容后回到空闲期（`attention_no_reply_limit`，默认 3 次）。

每轮回到空闲期就是本轮循环的结束边界：LLM 根据本轮上下文和学习数据统一整理记忆、黑话、句式与动态风格卡，随后清空本轮会话上下文。每日 Bot 不响应时间的独立整理线程已不再存在，短期记忆固化与黑话、句式、风格整理统一在每轮循环末尾完成。当前已选的风格/黑话/句式块也会在退出循环时立即清空。

## LLM 上下文和提示词

程序不维护独立的 LLM 对话历史。群聊消息是唯一对话上下文，用户消息和 Bot 自己生成的消息按顺序追加；估算超过 200,000 tokens 时调用 LLM 压缩上下文。

生产提示词的大体顺序为：

```text
静态前缀区（缓存可命中）：基本提示词 + 身份卡 + 黑话字段参考（类型/情绪/情绪强度去重值）+ prefix 定义
+ 长期记忆 + 中期记忆
+ 群聊消息上下文（缓存前缀在此断裂；其后为每轮变动的动态区）
+ 动态区：短期记忆 + 动态风格卡 + 本轮人物画像/额外记忆 + 当前上下文命中的黑话
  + 黑话使用学习 + 当前语境可参考的句式表达 + 当前已选风格/黑话/句式块
+ 当前状态 + 当前功能
```

静态前缀区在单轮关注期-工作期循环内几乎不变，以命中前缀缓存；群聊消息上下文之后是每轮变动的动态区。

整理模式会把短、中、长期记忆再次放到提示词末尾作为只读资料，模型只能通过 JSON action 修改记忆，避免把记忆正文误当成指令。所有历史消息、记忆、工具结果和黑话样例都是不可信数据。

控制台会输出每次 DeepSeek 转发的 token 和缓存统计：

```text
[LLM TOKENS] prompt=... completion=... total=... cache_hit=... cache_miss=...
```

## 风格与句式学习

群聊风格学习是轻量、本地、确定性的：运行时异步写入 SQLite（默认 `data/llm_learning.sqlite3`），不逐条调用 LLM；LLM 只参与每轮循环末尾的统一整理。

- 动态群聊风格卡（`style_cards`）：每轮循环结束由 LLM 根据本轮上下文、已有风格卡和统计证据决定 `replace` 或 `keep`。风格卡只记录抽象语气、整体节奏、避免模式和回复时机，不写入具体黑话、句式、昵称、人物名、服务器名、表情或样本原句。
- 本地风格统计（`group_profiles`）：样本数、平均字数、短消息占比、标点占比、碎片短句占比、句子断点、互动方式学习（接话/旁观/直接互动窗口）等，作为只读参考注入提示词。
- 句式库（`style_expressions`，maibot 式 situation→pattern）：每轮循环结束由 LLM 从本轮真实出现的表达中提取“当X情况用Y句式”，写入前先对比已有句式库，已存在的场景更新计数不重复新增。频率与召回排序由本地维护，置信度不来自 LLM。
- 回复时按当前语境命中注入：关键词/分词重叠命中优先，再用计数加权随机补足候选池（`expression_pool_size` 等配置），提示词标注“场景自然命中且合适时可用，不自然就忽略，不要硬套”。
- 使用计量：注入过的句式/黑话会在后续回复中统计是否真的被使用，循环末整理据此调整或删除“长期注入却从未使用”的条目。

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

黑话数据库默认是 `data/llm_learning.sqlite3`，主要使用两张表：

- `slang_terms`：LLM 确认写入后的统计主表，保存规范化表达、出现次数、说话人数、时间、样例和本地置信度。
- `slang_scenarios`：同一条 LLM 确认记录的语义表，保存含义、场景、状态、类型、情绪、情绪强度、样例和可用资格。

同一群组中相同的 `(group_id, normalized_phrase)` 使用 SQLite 主键更新原记录，不会产生精确重复。项目不维护覆盖关系、共享子串关系、排除表、Trie/重叠分析等事后重复黑话维护。

每轮循环结束时，LLM 读取黑话库和本轮对话上下文，直接通过 `slang_actions` 整理黑话。每个 add/update action 先调用 `lookup_similar_group_slang`，使用无 Embedding 的规范化文本匹配，返回相同、包含或共享明显文本的候选及其含义和样例。LLM 必须作出明确决定：

- `reuse_existing`：填写已有候选的 `canonical_normalized_phrase`，更新原记录。
- `new_distinct`：确认它与候选是不同的群体表达后才新增。
- `uncertain`：本轮不写入数据库。

模型也可以调用 `lookup_group_slang` 查询未直接注入的记录。只有 `active`、安全、置信度达标且命中当前上下文的黑话才直接进入回复提示词；类型、情绪和情绪强度的去重值放在身份卡后供模型选择。

置信度与安全判定由本地出现频率算法（出现次数、说话人数等）计算，LLM 只负责决定黑话是否入库及其含义、场景、示例，不输出 confidence 或 safe_to_use 字段。

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
- `fetch_original_message`：读取当前会话中疑似截断消息的原文。
- `fetch_webpage`：读取允许的公开网页。
- `fetch_tieba_hot_post`：读取百度贴吧（默认弱智吧）当前热门内容，供关注期机会自然搬运。

管理员可使用 `/prompt` 将最近一次完整 LLM 提示词写入根目录 `prompt.txt` 并发送到群聊。管理员身份由 `llm.admin_wxids` 控制。其他插件命令见 [admin_commands.txt](admin_commands.txt) 和 [DEV_GUIDE.md](DEV_GUIDE.md)。

## 配置参考

`llm` 配置的主要分组：

- `identity`：Bot 名称、角色、风格、额外规则。
- `prompt`：最大回复条数、是否允许表情、是否禁止 Markdown 与解释、表情提示语、兜底回复、特殊规则与话题转移规则。
- `learning`：本地学习开关与参数（`style_card_max_chars`、`expression_pool_size`、`expression_selector_enabled`、`slang_min_occurrences`、`slang_emotional_pool_rotation`、`style_switch_enabled`、`style_switch_cooldown_seconds` 等）。
- `auto_reply`：主动回复状态机参数（各阶段时长、关注期上下文预算、无厘头/黑话情绪/弱智吧机会概率与冷却、批量窗口等）。
- `memory`：长期记忆与人物画像参数。
- `web_fetch_*` / `original_message_*`：网页与原始消息工具的上限与超时。

完整示例见 [sample_config.json](sample_config.json)。

## 测试和检查

```powershell
python -m compileall -q core plugins auto services llm utils main.py
git diff --check
```

涉及 DeepSeek、WeFlow、RCON 或 ComfyUI 的功能还需要对应外部服务可用后进行实际验证。

项目结构、插件 API 和 LLM 运行约定见 [DEV_GUIDE.md](DEV_GUIDE.md)。
