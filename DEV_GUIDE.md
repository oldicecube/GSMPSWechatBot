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
│   ├── provider/        # Provider 转发层（目前仅 DeepSeek）
│   └── security/        # 表情索引与辅助逻辑
├── plugins/             # 命令行插件
├── services/            # 业务服务层
├── utils/               # 通用工具库
├── data/                # 本地数据存储
└── output/              # 图片、文件输出
```

`main.py` 启动时会先构建 Dispatcher，再扫描 `plugins/` 和 `auto/` 下的 Python 文件。`plugins/` 中有 `handle` 的模块按 `COMMAND`（缺省为文件名对应的 `/命令`）注册；`ALIASES` 可注册额外命令别名。`auto/` 模块按文件名注册，`MATCH_RAW_MESSAGE`、`FALLBACK_ONLY` 和 `INTERCEPT_LLM` 等模块级标记会改变其调度阶段。

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
| `only` | 只有命中 `prefix` 的消息进入路由 |
| `mixed` | 命中和未命中前缀的消息都进入路由 |
| `off` | 关闭前缀要求 |

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

# 文本
send(target=context["group"], content="通知", mode="wechat_text")

# 图片/文件
send(target=context["group"], file_path="output/demo.png", mode="wechat_file")
```

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
→ Dispatcher（auto → command → LLM 或 fallback auto）
→ LLMService → DeepSeekProvider → response_parser
→ Worker → core.sender → 微信发送服务
```

LLM 由 `llm/core/llm_service.py` 实现，使用 `llm/provider/deepseek_provider.py` 调用 DeepSeek Chat Completions，以 JSON object 格式请求，并由 `response_parser` 转换为 `messages` 和可选的 `animation`。当前运行时不会启动模型子进程、Planner/Replyer 或 embedding 服务。

### LLM 放行条件

1. 消息通过 Router 的黑名单、时间段、群名和速率限制。
2. 消息没有被命令插件直接处理。
3. 消息带有配置前缀，或发送者 wxid 在 `llm.prefix_bypass_wxids` 中。
4. `llm.enabled == true`，且 API Key 和模型配置有效。
5. `llm.intercept_auto_plugins` 中的每个自动插件都存在、声明 `INTERCEPT_LLM = True`，并通过 `allow_llm(context)`。

LLM 历史和普通群聊上下文当前分别写入 `data/groups/<group_id>/llm_history.json` 与 `group_messages.json`，由 `llm/memory/memory_manager.py` 按数量和过期时间裁剪。不要把这些文件当作仓库配置提交。

random reply 默认关闭。启用时必须显式写入 `llm.random_reply.enabled=true`；
`allowed_groups: []` 继承顶层 `target_group`，不会放开全部群聊：

```json
"random_reply": {
  "enabled": true,
  "min_cooldown_seconds": 600,
  "max_cooldown_seconds": 1800,
  "allowed_groups": ["Exact group name"],
  "exclude_mentions": true,
  "exclude_commands": true,
  "exclude_media": true
}
```

`llm.identity`、`llm.prompt`、`admin_wxids`、`emoji_dir` 等字段会进入当前 Prompt 构建流程；`engine`、`behavior_style`、`proactive`、`memory` 及分类型 `rate_limit` 字段目前不是当前运行时的独立执行开关，新增配置时必须同步修改对应代码。

---

## 八、开发接口速查

### 统一发送

```python
from core.sender import send, configure, preview_delay_seconds

configure(config)
send(target, content="文本", mode="wechat_text")
send(target, file_path="img.png", mode="wechat_file")
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

1. 所有消息推送必须使用 `core.sender` 统一接口
2. 耗时任务必须新建子线程，禁止阻塞主线程
3. 命令插件指令必须以 `/` 开头
4. 禁止修改原始 `context` 上下文对象
5. 插件逻辑轻量化，复杂业务拆分至 services 层
6. 严格区分自动插件与命令插件使用场景

---

## 十、冗余功能清理建议

以下为通用部署时可考虑清理的 GSMPS 专用内容：

| 位置 | 说明 | 建议 |
|------|------|------|
| `auto/player_monitor.py` | GSMPS 专用计分板逻辑（The Room 积分、每日统计） | 通用部署建议删除或精简 |
| `data/groups/GDUT SIE Minecraft Public Server/` | GSMPS 群历史数据 | 删除 |
| `data/fortune_words.json` | 运势词库 | 按需保留 |
| `data/life_words.json` | 转生词库 | 按需保留 |
| `plugins/ba.py` | BA Logo 风格图片生成 | 如不需要可删除 |
| `plugins/seed.py` | MC 地图种子查询 | 如不需要可删除 |
| `plugins/player.py` | 含 `--fortune`、`--life`、`--value` 玩法 | 通用部署可去掉这些子命令 |

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
