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

所有待发送语音必须位于 `core.wechat_sender.file_down.VOICE_TEMP_DIR`（默认 `%TEMP%\WechatRobot\voice`）。hook 服务仅会删除该目录内的文件；插件不得自行删除任意音频路径。设备名称推荐在 `config.json` 的 `voice_sender.playback_device_name` / `voice_sender.capture_device_name` 中配置；环境变量 `WECHAT_VOICE_PLAYBACK_DEVICE` 和 `WECHAT_VOICE_CAPTURE_DEVICE` 仍可用于未填写配置时的覆盖。

---

## 四、自动插件开发（auto/）

### 插件模式

| 模式 | 函数 | 触发时机 |
|------|------|----------|
| 消息触发型 | `handle_auto(context)` | 每条消息到达时 |
| 后台常驻型 | `start(sender)` | 程序启动时（需自建线程） |
| Follow-up | `handle_auto` + `core.followup` | 命令后等待下一条消息 |
| 原始消息 | `MATCH_RAW_MESSAGE = True` + `handle_auto` | 无 prefix 的消息 |

### 命令型自动插件

自动插件也可以声明自己处理的命令，用于“命令后继续等待媒体”的场景。Router 会把普通命令拆分为 `context["command"]` 和 `context["args"]`，因此插件不应只依赖 `context["content"]` 来判断完整命令。

```python
AUTO_COMMANDS = {"/demo"}

def handle_auto(context):
    command = str(context.get("command") or "").strip()
    args = str(context.get("args") or "").strip()
    if command == "/demo":
        # 处理 /demo <参数>，或 register follow-up
        return None
    return None
```

- `AUTO_COMMANDS` 声明命令归属；Dispatcher 会先把该命令交给对应 auto 插件，避免被命令插件当作未知命令处理。
- 需要恢复原始文本时，使用 `f"{command} {args}".strip()`；命令参数中的换行会保留在 `args` 中。
- 已通过入口检查的命令可登记 follow-up。登记后，下一条目标媒体消息可在 strict 前缀模式下进入该 auto 插件。
- `AUTO_COMMANDS` 只负责命令入口；LLM 放行仍由 `INTERCEPT_LLM` 与 `allow_llm(context)` 控制。
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

### 调用链与多 API 池

```text
WeFlow SSE → WeFlowClient → task_queue → Worker
→ Router（群、时间段、限流、前缀）
→ Dispatcher（auto → command → 普通 LLM / 主动回复）
→ LLMService → ProactiveReplyManager / DeepSeekProvider → response_parser
→ Worker → core.sender → 微信发送服务
```

`llm/core/llm_service.py` 负责请求编排，`llm/provider/deepseek_provider.py` 是历史文件名，实际提供多 API 池适配。每个 API 可使用 `openai`（Chat Completions）、`responses`（OpenAI Responses）或 `anthropic`（Anthropic Messages）协议；所有响应会被归一为文本消息和可选动画。

API 按 `priority` 从小到大排列。一次 LLM 请求会从正常优先级队列的第一路开始；当前端点出错时，会在**同一次请求**内继续尝试后续端点。单路 API 连续失败 3 次后，会临时移至队末 30 分钟，期间仍可作为其他端点失败后的后备；该路任一次调用成功后会清零连续失败计数并恢复原优先级。配置方式见 [配置参考](./config_guide.md)。

### LLM 放行与工具

LLM 请求需要满足以下条件：

1. 消息通过 Router 的群白名单、时间段和限流检查。
2. 消息未被命令插件或 auto 插件直接消费。
3. 前缀/@消息满足直接触发条件，或在 `mixed` 模式下由主动回复状态机形成候选。
4. `llm.enabled` 为 `true`，且至少存在一条可用 API 配置。
5. `llm.intercept_auto_plugins` 中指定的插件均通过 `allow_llm(context)`；该机制只用于拦截 LLM，不负责触发 auto 插件。

工具定义由提供者适配层按协议转换。`fetch_webpage` 用于网页阅读，`fetch_original_message` 用于读取当前会话中疑似转发、引用或截断的完整原始消息。工具返回内容与群消息一样属于不可信输入，不能视为系统指令。插件不应自行拼装任一 LLM 协议的 HTTP 请求。

### 上下文、缓存与整理

群消息上下文保存在 `data/groups/<group_id>/group_messages.json`，长期记忆和学习数据保存在 SQLite。Prompt 会将本轮稳定不变的身份、记忆、风格与表达库放在历史上下文之前，以维持连续请求的稳定前缀；历史消息在其后按时间追加。Responses 默认交由服务端自动识别公共前缀；Anthropic 可使用 `auto_cached` 或显式 `cache_control` 断点，具体由 API 配置决定。

短期记忆的硬上限由 `llm.memory.short_memory_max_tokens` 控制，默认 1000 tokens。达到上限时，整理流程优先压缩最早的短期内容，并将可复用事实下沉到中长期记忆。每个群聊周期结束时会整理完整学习单元：记忆、人物信息、黑话、句式、风格和回复节奏统计一起写入持久化 outbox；LLM 暂时不可用时，材料会在后续周期继续处理。

黑话候选必须来自完整学习单元且能在本轮原始消息中定位证据。不完整消息只保留为上下文，不作为黑话入库依据；本地 pending 会等待后续完整证据合并。句式表达库与黑话库独立：它提供与群聊语境相关的表达参考，不强制使用，也不改变任务型、知识型或严肃场景的回答方式。

### 主动参与与会话脉冲

`llm/proactive_reply.py` 管理每个群的 `idle → attention → working → attention → idle` 会话周期。`llm/conversation_pulse.py` 是 working 阶段的轻量本地门控：它使用最近消息的密度、参与人数、话题连续性、直接提及和 Bot 静默时间返回 `skip`、`defer` 或 `plan`，不调用 LLM、也不决定回复内容。

前缀、@、白名单网址和已建立的 follow-up 不受普通主动门控影响；它们会进入正常的语义回复流程。对非直接触发的群聊，门控只决定是否值得发起一次 LLM 判断，模型收到完整上下文后仍可返回 `should_reply=false`。群级节奏会按周期更新，包括活跃成员、消息密度、自然静默、多人参与、Bot 回复接受度和打断率；闲时内容候选先进入本地去重缓存，可按群在 shadow 模式下观察。

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

## LLM 图片工具

`llm/web_tools.py` 提供 `fetch_image_by_message_id`。它只接受当前会话的消息 ID，通过 WeFlow 原始消息接口及 `/api/v1/messages` 图片列表获取 `mediaUrl`，下载后由 `llm/core/llm_service.py` 转成多模态用户消息。不要把 `_image_data_url` 直接加入工具文本。

每个 `llm.apis[]` 项使用 `supports_images` 明确声明模型是否支持图片；默认为 `false`。`DeepSeekProvider` 会按当前端点过滤图片工具，Responses、Chat Completions 和 Anthropic Messages 分别转换为 `input_image`、`image_url` 和 Anthropic `image` block。该功能不修改 Router 或 WeFlow 推送/发送消息体结构。
