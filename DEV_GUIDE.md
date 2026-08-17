# GSMPS WeChat Bot — 开发引导文档

> 快速上手请阅读 [README.MD](./README.MD)。本文档面向插件开发者，涵盖架构、API 参考与最佳实践。

---

## 项目简介

本项目基于 WeFlow 内核的微信机器人框架，采用分层架构设计，核心解耦、插件化开发，内置消息调度、事件监听、统一消息发送、MC 服务器查询等能力，可快速拓展自定义命令与后台定时任务。

---

## 一、项目整体架构

### 1. 分层结构

```
核心框架（core）
├─ 消息路由、任务调度、消息队列、API 封装
插件系统（plugins）
├─ 交互式命令插件，响应群聊/私聊指令
业务服务（services）
├─ 第三方业务能力封装（MC查询、图片生成等）
工具库（utils）
├─ 通用工具函数、数据处理方法
自动任务（auto）
├─ 后台常驻线程、定时任务、监控服务
```

### 2. 完整调用链

```
WeFlowClient
└─ Worker 工作线程
   └─ Router 消息解析
      └─ Dispatcher 调度分发
         ├─ 全局事件监听触发
         └─ 插件 handle 逻辑执行
            └─ 统一 sender 消息回复/推送
```

### 3. 目录结构

```
├── sample_config.json   # 配置模板
├── config.json          # 本地配置（不提交）
├── main.py              # 程序入口
├── start.bat            # Windows 一键启动
├── assets/              # 静态资源
│   ├── fonts/           # 字体文件
│   └── images/          # 静态图片
├── auto/                # 后台自动任务插件
├── core/                # 核心框架
│   ├── dispatcher.py    # 插件调度中心、事件广播
│   ├── router.py        # 消息路由、命令解析
│   ├── queue.py         # 异步消息队列
│   ├── worker.py        # 多线程任务处理器
│   ├── sender.py        # 统一消息发送接口
│   ├── weflow_client.py # WeFlow SSE 消息接收
│   ├── weflow_media.py  # 历史消息/媒体下载工具
│   ├── followup.py      # 命令后的下一条消息公用通道
│   ├── auto_registry.py # 原始消息命中注册
│   └── project_reloader.py # 监控 .py 变更并重启
├── llm/                 # LLM 核心目录
│   ├── config.py        # LLM 配置读取
│   ├── core/            # LLM 服务与响应解析
│   ├── memory/          # 群聊上下文存储
│   ├── prompt/          # System/User Prompt 构造
│   ├── provider/        # 多协议 API 池：openai / responses / anthropic
│   └── security/        # 表情索引与辅助逻辑
├── plugins/             # 命令行插件
├── services/            # 业务服务层
├── utils/               # 通用工具库
├── data/                # 本地数据存储
└── output/              # 图片、文件输出
```

`main.py` 启动时会先构建 Dispatcher，再扫描 `plugins/` 和 `auto/` 下的 Python 文件。`plugins/` 中有 `handle` 的模块按 `COMMAND`（缺省为文件名对应的 `/命令`）注册；`ALIASES` 可注册额外命令别名。`auto/` 模块按文件名注册，`MATCH_RAW_MESSAGE`、`FALLBACK_ONLY` 和 `INTERCEPT_LLM` 等模块级标记会改变其调度阶段。

`llm/` 是核心源码，必须和 `core/` 一起提交；它包含配置归一化、三种协议适配、提示词构建、记忆、学习与主动回复。`data/*.sqlite3`、`data/groups/`、`logs/`、`output/`、`config.json`、`prompt.txt`、`__pycache__/`、`weflow-core/node_modules/` 与 `weflow-core/dist/` 是运行时内容，不应提交。不要因名称含有 `llm`、`data` 或 `output` 就直接删除，应先用 `git ls-files -- <path>` 判断是否属于源码。

---

## 二、消息上下文 Context

所有插件统一接收 `context` 上下文参数：

```python
context = {
    "user": "用户名",
    "group": "群名",
    "sessionId": "会话ID",
    "type": "消息类型",
    "content": "命令参数（已去 prefix）",
    "wxid": "wxid_xxxxxx",
    "text": "清洗后正文内容",
    "is_group": True,
    "is_private": False,
    "raw": {}              # 原始完整消息对象
}
```

### 常用字段

| 字段 | 说明 | 推荐场景 |
|------|------|----------|
| `user` | 发送者昵称 | 界面展示、日志 |
| `group` | 群聊名称 | 群专属功能 |
| `content` | 去 prefix 后的参数 | 命令解析 |
| `wxid` | 微信唯一标识 | 权限绑定、黑名单、用户数据 |
| `is_group` / `is_private` | 消息来源 | 区分群聊/私聊 |
| `raw` | 原始消息对象 | 高级拓展 |

Worker 还会向 Context 传递 `is_at`、`is_mentioned`、`is_picture`、`is_emoji`、`is_voice`、`command`、`args`、`prefix_used`、`auto_target` 和 `followup_payload`。插件应读取这些字段，不要修改原始 `raw` 或共享 Context。

### 路由模式

`Router` 在进入 Dispatcher 前依次执行黑名单、时间段、群名白名单和速率限制检查。`prefix_mode` 支持：

| 值 | 行为 |
| --- | --- |
| `strict` | 只有命中 `prefix` 的消息进入所有处理流程；LLM 状态机关闭，prefix 普通消息强制回复 |
| `mixed` | 命中和未命中前缀的消息都进入路由；LLM 状态机默认开启，可由 `llm.auto_reply.enabled=false` 禁用 |

命令正则支持 ASCII 命令以及中文命令名。命令插件和普通自动插件优先执行；只有没有返回结果时，才会进入 LLM 或 `FALLBACK_ONLY` 自动插件。

---

## 三、命令插件开发（plugins/）

### 基础规范

1. 文件存放于 `plugins/` 目录
2. 定义全局变量 `COMMAND = "/指令名"`
3. 实现 `handle(content, context)` 函数
4. 可选实现 `init(config)` 用于启动时接收配置

### 最简模板

```python
COMMAND = "/demo"

def handle(content, context):
    if content.strip() != COMMAND:
        return None
    return f"你好 {context['user']}"
```

### 返回值规范

| 返回值 | 行为 |
|--------|------|
| `str` | 自动以文本回复 |
| `list[str]` | 多条文本分段发送 |
| `dict` | 结构化返回（推荐） |
| `None` | 不响应 |

### 结构化返回协议（推荐）

```python
return {
    "target": context["group"],      # 可选，默认当前群/用户
    "content": "文本内容",           # 文本发送
    "mode": "wechat_text",           # wechat_text / wechat_file / rcon
    "delay_seconds": 0               # 可选，覆盖本次发送延迟
}
```

多段文本 + 表情：

```python
return {
    "target": context["group"],
    "messages": ["第一句", "第二句"],
    "animation": "doge",             # emoji_dir 中的文件名（无扩展名）
    "mode": "wechat_text"
}
```

### 媒体发送

```python
from core.sender import send
from core.wechat_sender.file_down import download_voice_file

# 文本
send(target=context["group"], content="通知", mode="wechat_text")

# 图片/文件
send(target=context["group"], file_path="output/demo.png", mode="wechat_file")

# 语音：先用统一临时目录下载或生成文件，然后交给发送组件
voice_path = download_voice_file(audio_url, prefix="plugin")
send(
    target=context["group"],
    file_path=voice_path,
    mode="wechat_voice",
    duration=None,      # None = 裁剪后音频的实际时长，最多 60 秒
    voice_start=0.0,    # 可选起始秒，支持浮点
)
```

语音发送仅支持 Windows + VB-CABLE。发送组件会切换目标聊天后长按 Shift，等待 0.5 秒再向 `CABLE Input` 注入音频，完成后才松开 Shift。微信录音设备应设为 `CABLE Output`。不得绕过 `core.sender.send` 或直接向物理扬声器播放。

所有待发送语音必须位于 `core.wechat_sender.file_down.VOICE_TEMP_DIR`（默认 `%TEMP%\WechatRobot\voice`）。hook 服务仅会删除该目录内的文件；插件不得自行删除任意音频路径。设备名称可用 `WECHAT_VOICE_PLAYBACK_DEVICE` 和 `WECHAT_VOICE_CAPTURE_DEVICE` 环境变量覆盖。

---

## 四、自动插件开发（auto/）

### 插件模式

| 模式 | 函数 | 触发时机 |
|------|------|----------|
| 消息触发型 | `handle_auto(context)` | 每条消息到达时 |
| 后台常驻型 | `start(sender)` | 程序启动时（需自建线程） |
| Follow-up | `handle_auto` + `core.followup` | 命令后等待下一条消息 |
| 原始消息 | `MATCH_RAW_MESSAGE = True` + `handle_auto` | 无 prefix 的消息 |

### 消息触发型模板

```python
def handle_auto(context):
    content = context.get("content", "")
    if "关键词" not in content:
        return None
    return "自动回复内容"
```

### 原始消息声明

```python
MATCH_RAW_MESSAGE = True

def handle_auto(context):
    content = (context.get("content") or "").strip()
    if "拍了拍" not in content:
        return None
    return "事件已命中"
```

### LLM 拦截声明

```python
INTERCEPT_LLM = True

def allow_llm(context):
    # True: 放行到 LLM / False: 拦截
    return True
```

> 需同时在 `config.json → llm.intercept_auto_plugins` 中列出插件名。

### 后台常驻型模板

```python
import threading
import time

def start(send):
    def loop():
        while True:
            send("定时任务触发")
            time.sleep(10)
    threading.Thread(target=loop, daemon=True).start()
```

### Follow-up 通道

```python
from core.followup import register, consume

def handle_auto(context):
    session_id = context.get("sessionId")
    text = (context.get("content") or "").strip()

    if text == "/demo":
        register(session_id=session_id, target="my_plugin", ttl=30)
        return None

    state = consume(session_id)
    if not state or state.get("target") != "my_plugin":
        return None
    # 处理下一条消息...
```

### Fallback-only 模式

```python
FALLBACK_ONLY = True

def handle_auto(context):
    # 仅在无其他插件处理时才执行
    return "兜底回复"
```

### WeFlow 媒体下载

```python
from core.weflow_media import extract_timestamp, find_image_url_by_timestamp, download_image

def handle_auto(context):
    raw = context.get("raw") or {}
    session_id = context.get("sessionId")
    target_ts = extract_timestamp(raw) or extract_timestamp(context)

    image_url, diff = find_image_url_by_timestamp(session_id, target_ts, limit=20)
    if not image_url:
        return None

    img = download_image(image_url)
    # img 是 PIL.Image 对象
```

---

## 五、消息旁路处理

当前 `Dispatcher` 没有公开的 `on_message` 全局事件注册 API。需要监听消息时，应在 `auto/` 中实现 `handle_auto(context)`；需要监听无前缀原始消息时，额外声明 `MATCH_RAW_MESSAGE = True`。如果任务需要长期运行，在 `start(sender)` 中创建 daemon 线程，并避免阻塞 Worker。

---

## 六、MC 服务器 API

```python
from services.mc_api import status, player_list

data = status()
# {"online": True, "latency_ms": 42.5, "online_players": 3, "max_players": 20, "players": ["A", "B"]}

players = player_list()
# ["玩家A", "玩家B"]
```

---

## 七、LLM 开发参考

### 当前 LLM 调用链

```
WeFlow SSE → WeFlowClient → task_queue → Worker
→ Router（黑名单/时间段/群名/限流/前缀）
→ Dispatcher（auto → command → LLM 主动回复/普通 LLM）
→ LLMService → ProactiveReplyManager / DeepSeekProvider → response_parser
→ Worker → core.sender → 微信发送服务
```

LLM 由 `llm/core/llm_service.py` 实现，`llm/provider/deepseek_provider.py` 保留历史文件名，实际是多路 API 池适配层。它统一支持 `openai`（Chat Completions）、`responses`（OpenAI Responses）和 `anthropic`（Anthropic Messages）协议，再由 `response_parser` 归一为 `messages` 和可选的 `animation`。API 按 `priority` 升序选择。每次 LLM 请求都从最高优先级的未停用 API 开始；某路失败时，会在同次请求内按优先级继续尝试下一个可用 API，直到成功或全部失败。每路的错误数在当前 bot 响应期内累计，成功不清零；累计 5 次错误后停用至下一个不响应期边界。边界会清空计数、恢复所有 API，后续请求重新从最高优先级开始。没有配置不响应时间时，边界默认为每日 00:00。当前运行时不会启动模型子进程、Planner/Replyer 或 embedding 服务。

### LLM 放行条件

1. 消息通过 Router 的黑名单、时间段、群名和速率限制。
2. 消息没有被命令插件直接处理。
3. 普通 LLM 消息带有配置前缀，或发送者 wxid 在 `llm.prefix_bypass_wxids` 中；主动回复候选消息由 `llm.auto_reply.enabled` 和内部状态机决定。
4. `llm.enabled == true`，且 API Key 和模型配置有效。
5. `llm.intercept_auto_plugins` 中的每个自动插件都存在、声明 `INTERCEPT_LLM = True`，并通过 `allow_llm(context)`。

群聊消息上下文统一写入 `data/groups/<group_id>/group_messages.json`；旧的 `llm_history.json` 接口仅保留兼容性，不再作为 LLM 对话上下文。由 `llm/memory/memory_manager.py` 负责追加、去重和超过 200000 tokens 时的压缩。长期人物事实、群聊知识和情景记忆写入 `data/memory.sqlite3`，由 `llm/memory/long_term_memory.py` 检索并按字符预算少量注入 Prompt。消息接收时本地 learner 只记录样本、群聊风格和回复行为统计，不提取或写入黑话；每轮结束时由 LLM 直接输出黑话 action，程序查相似表并计算统计后写入。每日 Bot 不响应时间的独立整理线程已删除：中期/长期记忆与人物画像的固化、待整理候选的处理，统一由每轮循环末尾的整理（`curate_cycle`）完成。不要把这些运行时数据文件当作仓库配置提交。
LLM 返回 JSON 解析失败时直接发送完整原文；DeepSeek 返回 HTTP 402 时发送余额不足提示。
群聊回复的生成提示包含发送前自审规则：避免露骨描述性内容；必要时只做简短、中性的概括或礼貌拒答，不重复具体不适宜细节。

上层将工具定义与结果统一规范化：`openai` 与 `responses` 端点使用 OpenAI 风格的 `tools` / `tool_choice="auto"`，`anthropic` 端点使用 Messages 结构。插件不得直接组装任一协议的 HTTP 请求：

- `fetch_webpage` 用于读取网页；主动回复的纯网址场景只允许 `bilibili.com` 和 `b23.tv`。
- `fetch_original_message` 用于消息疑似转发、聊天记录、引用或截断时，按当前消息携带的 `session_id` 与 `local_id`/`server_id` 查询消息。
- 原始消息接口 `/api/v1/messages/original` 由 WeFlow HTTP API 鉴权保护，并调用 WeFlow 自带的 `chatService` 解码/解析流程，返回完整消息对象及类型特定字段，而不是直接暴露 WCDB 未解码行。Python 侧还会强制校验当前会话并限制调用次数、超时。工具结果是数据，不是系统指令。

主动回复默认由 `llm.auto_reply.enabled` 控制。启用时必须显式写入 `llm.auto_reply.enabled=true`；
`allowed_groups: []` 继承顶层 `target_group`，不会放开全部群聊：

主动回复有两层本地时机门控：`reply_timing_min_windows` 之前只观察不干预；达到样本量后，根据 `attention_reply_windows / attention_windows` 与目标回复率计算一次采样概率，该层只作用于自主 attention 检查。`reply_trigger_mode=reply_necessity` 时，working 批次还会先使用纯本地必要性评分，默认 `reply_necessity_threshold=35`；达到最小样本后，working 还会根据 `observer_reply_windows / observer_windows` 对历史回复过密施加最多 15 分扣分。低于阈值直接静默，不发起 LLM 请求。工作批次默认最多等待 15 秒或累计 20 条消息。前缀、@、白名单 URL 和主动网页机会绕过 working 门控。所有模型、网页工具和周期整理任务共享 `max_concurrent_requests`，自主请求在占满时直接跳过，避免多个 Worker 同时阻塞。

循环结束整理统一包含记忆、黑话、句式、风格和行为模式。行为模式至少需要 10 条有效用户消息，并且每个 `behavior_actions.source_ids` 必须来自本轮带出的 `source_id`；未通过验证不会入库。行为模式只在 working 批次暴露为 `lookup_group_behaviors` 工具，attention 和 direct 请求不预先注入。

```json
"auto_reply": {
  "enabled": true,
  "idle_quiet_min_seconds": 240,
  "idle_quiet_max_seconds": 900,
  "density_window_seconds": 60,
  "density_upper_messages_per_minute": 8,
  "density_attention_check_interval_seconds": 30,
  "density_attention_half_life_seconds": 300,
  "density_attention_curve_power": 1.7,
  "proactive_web_min_seconds": 7200,
  "proactive_web_max_seconds": 10800,
  "proactive_web_reset_after_bot_messages": 10,
  "work_min_seconds": 120,
  "work_max_seconds": 300,
  "batch_debounce_seconds": 15,
  "batch_max_messages": 20,
  "attention_min_seconds": 120,
  "attention_max_seconds": 300,
  "attention_message_limit": 10,
  "attention_no_reply_limit": 3,
  "allowed_groups": ["Exact group name"],
  "exclude_commands": false,
  "exclude_media": false
}
```

空闲期使用最近一分钟滑动窗口消息数判断：超过预设上界 8 条时直接进入关注期；未超过上界时按密度 hazard 概率进入关注期。弱智吧主动转发使用独立随机 2--3 小时计时器：到期后在 Bot 不处于工作期时直接切换 `working`，让 LLM 现场调用网页工具判断是否主动转发；工作期未结束则在退出后立即调度。计时期间 Bot 每成功发送一条分开的群文本即计一条，超过 10 条时立即重新随机并清零计数。计时器不依赖 idle/attention 阶段，也不使用固定模板或把内部机会事件写入群聊记忆。工作期消息先进入缓冲窗口，默认最多 15 秒或达到 20 条后集中处理；随后先经本地必要性门控，低于阈值的批次直接静默，不发起 LLM 请求，高价值批次再交给 LLM 结合历史上下文判断。@ 和白名单网址会立即刷出当前缓冲批次并要求处理。每次主动判断的结果还会更新本地回复时机统计，空闲期复盘时替换动态风格卡。连续三次没有值得回复的内容才回到空闲期。主动回复实现位于 `llm/proactive_reply.py`，不再属于 `auto/` 插件。
正常状态路径是 `idle -> attention -> working -> attention -> idle`；滑动窗口超过上界可将 idle 直接切入 attention；独立主动转发计时器到期时可将 idle 或 attention 直接切入 working；@ 或白名单网址属于强制触发例外，可以跳过当前冷却直接进入 working。
主动回复会先让 LLM 结合完整上下文自行判断是否正在与 bot 互动，而不是只匹配 bot 名称或关键词；无法确认时默认保持旁观。只有长时间延续的同一个乐子话题才允许偶尔自然补一句。
主动回复遇到只包含 `bilibili.com` 或 `b23.tv`（含子域名）的网址时，会强制调用网页工具并生成简要梗概；其他网址不会触发该规则，且该次主动网页工具调用会拒绝非白名单域名及其跳转目标。

`llm.identity`、`llm.prompt`、`admin_wxids`、`emoji_dir` 等字段会进入当前 Prompt 构建流程；`llm.memory.enabled` 控制 SQLite 长期记忆，`llm.learning.enabled` 控制风格学习，`llm.auto_reply.enabled` 控制主动回复状态机。`engine`、`behavior_style`、`proactive` 及分类型 `rate_limit` 字段仍需结合对应模块实现，新增配置时必须同步修改对应代码。`llm.memory.short_memory_max_tokens`（默认 1000）控制短期记忆硬上限：超限时整理循环会优先压缩最早的短期记忆，并把有价值内容下沉到中长期记忆（无论 LLM 是否产生压缩动作都会在写库后执行硬上限截断）。长/中/短期记忆在每轮循环内保持不变，因此注入到群聊历史之前，使缓存前缀更稳定。
黑话场景库使用同一个 learning SQLite 文件中的 `slang_scenarios` 表，并与 `slang_terms` 保持同步。每轮主动回复循环结束时，LLM 仅从本轮非 Bot 消息上下文总结黑话；短期/中期/长期记忆、画像和旧黑话记录不构成新黑话证据。每条 `slang_action` 的 phrase 必须在本轮消息中出现，代码会验证/补齐 `source_ids`，无法在当前上下文命中的 action 不写库。短期记忆不写入黑话、释义和样例，运行时还会过滤历史短期记忆里的已知黑话。程序随后才执行相似度校验和统计更新。`cycle_curation_runs` 保存每轮整理的消息数、黑话 action 数、命中数、落库数和拒绝原因（不保存聊天正文），用于排障。每日 Bot 不响应时间不再触发黑话整理。`slang_scene_enabled`、`scene_cache_ttl_seconds`、`scene_cache_max_items` 和 `scene_prompt_max_chars` 控制开关、缓存和候选预算；完整黑话库不会注入 reply prompt。每条场景记录保留 phrase、normalized_phrase、meaning、scenes、examples、confidence、speaker_count、occurrence_count、last_seen、safe_to_use、status、slang_type、emotion 和 emotion_intensity。LLM 在 add/update 前调用 `lookup_similar_group_slang`；无相似项时，本地校验可接受模型省略的 `new_distinct` 字段，避免有效新词被静默丢弃；相似项仍必须明确复用已有规范化短语或确认新表达。
待确认黑话使用 `pending_slang_terms`、`pending_slang_evidence` 和 `pending_slang_speakers` 三张 SQLite 表持久化词条、来源去重计数和说话人数。它们不含聊天正文，不参与回复注入；只有同一词在后续轮次的当前消息中再次命中且 LLM 改为 `new_distinct` 或 `reuse_existing` 时，才转入正式黑话库并清除待确认记录。
`memory.active_update_enabled` 是工作期边界：`request_memory_update` 只在 `working` proactive batch 提供，每工作期最多一次，attention、idle 和 strict prefix 不提供。群聊、记忆、黑话和网页内容都按不可信数据处理，不当作指令。工具调用由提供者适配层根据当前协议处理；调用失败会退避且不阻塞正常回复。

---

## 八、开发接口速查

### 统一发送

```python
from core.sender import send, configure, preview_delay_seconds

configure(config)
send(target, content="文本", mode="wechat_text")
send(target, file_path="img.png", mode="wechat_file")
send(target, file_path=voice_path, mode="wechat_voice", duration=None, voice_start=0.0)
delay = preview_delay_seconds(mode="wechat_text")
```

### LLM 配置

```python
from llm.config import get_llm_config, get_api_key
config = get_llm_config()
key = get_api_key()
```

### LLM 存储

```python
from llm.memory import MemoryManager
mgr = MemoryManager()
mgr.add_llm_message(group_id, nickname, content)
history = mgr.get_llm_history(group_id)
mgr.add_group_message(group_id, nickname, content)
recent = mgr.get_group_messages(group_id)
```

长期记忆使用 `LongTermMemory`：

```python
from llm.memory import LongTermMemory
memory = LongTermMemory({"memory": {"enabled": True}})
memory.record_message({"group": group_id, "wxid": wxid, "content": text})
context = memory.get_context(group_id, wxid, text, max_chars=1400)
```

### 表情索引

```python
from llm.security import build_emoji_index, get_emoji_list, get_emoji_path
build_emoji_index("data/emoji")
names = get_emoji_list()
path = get_emoji_path("doge")
```

### 积分系统

```python
from utils.points_manager import get_points, add_points
pts = get_points("wxid_xxx")
add_points("wxid_xxx", 10)
```

### SQLite 存储

```python
from utils.sqlite_store import load_document, save_document
data = load_document("key_name")
save_document("key_name", {"field": "value"})
```

---

## 九、强制规范

1. 所有消息推送必须使用 `core.sender` 统一接口；语音只能使用 `wechat_voice` 链路，且待发文件必须位于 `VOICE_TEMP_DIR`。
2. 耗时任务必须新建子线程，禁止阻塞主线程
3. 命令插件指令必须以 `/` 开头
4. 禁止修改原始 `context` 上下文对象
5. 插件逻辑轻量化，复杂业务拆分至 services 层
6. 严格区分自动插件与命令插件使用场景

---

## 十、仓库卫生与可选裁剪

先区分“运行产物”和“可选功能源码”。运行产物可以清理，但不应进入 Git；可选功能只能在确认没有路由、命令、配置或跨模块导入依赖后再删除。

### 可安全清理且不提交的运行产物

| 位置 | 说明 | 处理方式 |
|------|------|------|
| `config.json`、`prompt.txt` | 本机密钥和调试提示词 | 保留在本机，绝不提交或复制进 sample |
| `logs/`、`llm/logs/`、`output/` | 日志、生成图片/文件 | 停止 Bot 后按需删除 |
| `data/*.sqlite3`、`data/groups/`、`data/pics/` | 记忆、学习库、群历史和缓存 | 备份后按需删除；会丢失学习/历史 |
| `__pycache__/`、`*.pyc` | Python 缓存 | 可随时删除 |
| `weflow-core/node_modules/`、`weflow-core/dist/` | Node 依赖和构建产物 | 可删除后用 `npm ci` / `npm run build` 重建 |
| `%TEMP%\WechatRobot\voice` | 待发送/已发送语音临时文件 | 发送组件自动清理；异常残留可在停机后清理 |

### 可选功能裁剪流程

1. 先用 `git grep -n "模块名" -- core plugins auto services main.py` 查找导入、命令注册和配置读取。
2. 再检查 `sample_config.json`、README、DEV_GUIDE 和 `requirements.txt`，同步删除对应配置和说明。
3. 删除后运行 `python -m compileall -q core plugins auto services llm utils main.py`，并启动到不连接真实微信发送的测试环境验证。
4. 不要删除 `llm/`、`core/`、`services/`、`utils/` 或受 Git 管理的 `weflow-core/resources/`；它们是框架运行依赖，不是缓存目录。

如果只是通用部署，可在完成上述检查后按需裁剪 GSMPS/MC、锦标赛、ComfyUI 或某个命令插件；但 `auto/player_monitor.py` 与 `auto/tournament_monitor.py`、`plugins/theroom.py` 等存在交叉调用，必须作为一个依赖集合评估。

### 开发新插件可用的框架特性

| 特性 | 位置 | 用途 |
|------|------|------|
| 原始消息命中注册 | `core/auto_registry.py` | 声明 `MATCH_RAW_MESSAGE = True` |
| Follow-up 公用通道 | `core/followup.py` | 命令后等待下一条消息 |
| 媒体时间戳反查 | `core/weflow_media.py` | 根据消息时间戳找图片/文件 |
| 结构化返回协议 | `worker.py` | dict 返回支持 target/content/mode/animation |
| LLM 拦截判定 | `INTERCEPT_LLM` + `allow_llm()` | auto 插件控制消息是否进入 LLM |
| Fallback 兜底 | `FALLBACK_ONLY = True` | 只在无其他处理时才执行 |
| 消息旁路处理 | `auto/*.py` 的 `handle_auto` | 按消息触发自动逻辑 |
| 积分系统 | `utils/points_manager.py` | 用户积分增减/查询 |
| SQLite 文档存储 | `utils/sqlite_store.py` | 通用 KV 持久化 |
| 发送延迟控制 | `send_delay` 配置 | 随机延迟模拟真人 |

---

## 十一、热重载说明

- 工程内任意 `.py` 文件变化时，`core/project_reloader.py` 让 `main.py` 以退出码 `100` 退出
- `launcher.py` 捕获退出码 `100` 并重新启动 `main.py`；`start.bat` 则对所有退出都循环重启
- 重启后所有插件、配置、线程重新初始化
- 不要依赖模块级内存状态跨重启保留

---

> 特别感谢：WeFlow、WechatRobot、ComfyUI、mcstatus、DeepSeek 等开源项目。

## 十二、会话脉冲门控

`llm/conversation_pulse.py` 是 working 期的纯本地调度器：读取最近最多 120 条、最多约三分钟的现有群消息，计算一分钟密度、参与人数、二元片段重叠、直接提及和 Bot 静默时间，返回 `skip`、`defer` 或 `plan`。它不调用 LLM、不做 embedding，也不决定回复内容；只有 `plan` 才会调用一次既有的批量 LLM 流程。模型收到完整上下文及脉冲摘要后，仍可返回 `should_reply=false`，并应自行选择连贯话题而不是默认回复批次最后一条。

碎片闲聊的 `plan` 是低频采样机会：默认要求一分钟至少 3 条人类消息、Bot 静默 6 分钟、同群距离上次该机会至少 6 分钟，并以 12% 概率抽样。`attention_nonsense_probability` 默认同样降为 12%。弱智吧内容只在独立随机 2--3 小时主动网页机会中现场拉取；若计时期间 Bot 成功发送超过 10 条分开的群文本，计时器立即重新随机。
