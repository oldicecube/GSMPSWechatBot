# GSMPS WeChat Bot v2

一个基于 [WeFlow](https://github.com/Double6ix/WeFlow) 的 Windows 微信机器人框架。它负责接收微信消息、执行路由和插件、调用 LLM，并通过统一发送接口回复文本或文件。

> 仓库中的插件和业务服务主要是 GSMPS / Minecraft 群聊场景的示例实现。部署前请逐项检查群名、服务器地址、账号标识、API Key 和本地路径；不需要的示例插件应禁用或移除。

## 功能概览

- WeFlow 消息接入与微信消息发送
- 精确群聊白名单、前缀路由、命令分发和速率限制
- `plugins/` 命令插件与 `auto/` 自动事件插件
- DeepSeek LLM 转发、JSON 响应解析、群聊上下文和表情发送
- LLM 内置主动回复状态机：工作期、关注期、空闲期和批量情景判断
- DeepSeek 标准网页工具调用，可由模型自主决定是否读取网页
- Minecraft 状态查询、RCON、玩家监控和 The Room 锦标赛示例
- ComfyUI 图片生成与媒体发送示例
- 本地 JSON / SQLite 工具、管理员命令和项目级 Python 热重载

## 运行边界

- 当前项目面向 Windows 微信客户端，微信需要保持登录。
- `target_group` 是精确匹配的群聊白名单；空数组或未匹配群名会被路由器直接忽略。
- `prefix_mode` 默认为 `only`：只有带配置前缀的消息才进入路由；`mixed` 和 `off` 允许无前缀内容继续参与命令/自动插件处理。
- 插件和自动插件优先于 LLM；普通群聊消息由 LLM 内置主动回复机制按状态机批量判断。
- 当前 LLM 实现是 `llm/core/llm_service.py` 调用 DeepSeek API，历史记录保存在 `data/groups/<群名>/` 下的 JSON 文件，并按字符预算裁剪。

## 环境要求

- Windows 10/11
- Python 3.10 或更高版本
- Node.js LTS（用于构建 `weflow-core`）
- 已登录的微信客户端
- 可选：DeepSeek API Key、Minecraft 服务器/RCON、ComfyUI

## 快速开始

### 1. 安装依赖并构建 WeFlow

推荐首次运行直接执行：

```powershell
.\start.bat
```

脚本会在缺少 `weflow-core/dist/index.js` 时执行 `npm install` 和构建，安装 Python 依赖，然后启动机器人。

也可以手动执行：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

Push-Location weflow-core
npm ci
npm run build
Pop-Location
```

### 2. 创建配置

```powershell
Copy-Item sample_config.json config.json
```

至少修改以下字段：

| 字段 | 说明 |
| --- | --- |
| `target_group` | 允许处理的群聊名称，必须与微信中的群名完全一致 |
| `prefix` | 触发前缀，可填写字符串或字符串数组 |
| `prefix_mode` | `only`、`mixed` 或 `off` |
| `weflow.dbPath` | 微信文件目录 |
| `weflow.decryptKey` | 64 位十六进制解密密钥 |
| `weflow.myWxid` | 当前微信账号 wxid |
| `llm.api_key` | 启用 LLM 时填写 DeepSeek API Key |

缺少 WeFlow 配置时，`main.py` 会尝试启动配置向导。也可以在 WeFlow 已构建后手动执行：

```powershell
python setup_wizard.py
```

### 3. 启动与验证

```powershell
# 直接运行，适合调试
python main.py

# 带热重载守护，适合开发
python launcher.py
```

启动后，在目标群使用配置的前缀发送 `/ping`。如果能收到回复，说明 WeFlow、消息接收、路由和发送链路基本正常。

`start.bat` 会在进程退出后自动重启；开发时如需查看真实异常，优先使用 `python main.py` 或 `python launcher.py`。

## 配置说明

### 路由与群聊边界

`target_group` 支持字符串或数组，比较时会去除首尾空格但不会模糊匹配。群名修改后必须同步修改配置。`time_slots` 可限制允许处理消息的时间段，并支持跨午夜区间，例如 `22:00` 到 `02:00`。

`prefix_mode` 的含义：

| 值 | 行为 |
| --- | --- |
| `only` | 仅处理带前缀的消息；默认值 |
| `mixed` | 带前缀和不带前缀的消息都交给路由 |
| `off` | 关闭前缀要求 |

### 速率限制

当前 `core/router.py` 实际执行的是：

- `rate_limit.messages_per_minute`：单个 wxid 每分钟上限
- `rate_limit.global_messages_per_minute`：全局每分钟上限
- `rate_limit.auto_blacklist_enabled` 与 `auto_blacklist_threshold`：超限后自动写入 `data/blacklist.json`

示例配置中名为 `llm_*`、`prefix_llm_*` 和 `plugin_*` 的字段目前不会被当前 Router 独立计数；如需启用独立限流，需要先修改实现，不应仅修改配置后假设它们已生效。

### LLM

LLM 由 `llm.enabled` 控制，当前 Provider 为 DeepSeek：

```json
{
  "llm": {
    "enabled": true,
    "provider": "deepseek",
    "model": "deepseek-chat",
    "api_key": "sk-...",
    "max_history_chars": 5000,
    "history_expire_ms": 600000,
    "group_message_limit_chars": 2000,
    "intercept_auto_plugins": ["picture"],
    "web_fetch_enabled": true,
    "web_fetch_max_calls": 3,
    "web_fetch_timeout_seconds": 15,
    "web_fetch_max_chars": 24000,
    "original_message_enabled": true,
    "original_message_max_calls": 2,
    "original_message_timeout_seconds": 8,
    "original_message_max_chars": 16000,
    "prefix_bypass_wxids": [],
    "auto_reply": {
      "enabled": true,
      "idle_min_seconds": 1200,
      "idle_max_seconds": 2400,
      "work_min_seconds": 120,
      "work_max_seconds": 300,
      "batch_interval_seconds": 10,
      "attention_min_seconds": 120,
      "attention_max_seconds": 300,
      "attention_message_limit": 10,
      "attention_no_reply_limit": 3
    }
  }
}
```

`max_history_chars` 是对话历史字符上限，`group_message_limit_chars` 是群聊最近消息字符上限；两者统计消息 `content` 字段字符数，不再按消息条数限制。`history_expire_ms` 仍控制对话历史过期时间。转发给 LLM 时从最新消息向前累积，纳入刚好跨过上限的完整消息，不拆分消息。

### 主动回复状态机

主动回复已经从 `auto/` 插件迁移到 `llm/proactive_reply.py`，由 `llm.auto_reply.enabled` 控制：

| 状态 | 行为 |
| --- | --- |
| 空闲期 | 随机持续 20–40 分钟，结束后先进入关注期 |
| 工作期 | 随机持续 2–5 分钟，每 10 秒整理期间消息交给 LLM 判断 |
| 关注期 | 空闲期或工作期结束后，每随机 2–5 分钟检查群聊最新 10 条消息 |
| 回退空闲 | 连续 3 次判断没有值得回复的内容后进入空闲期 |

关注期发现值得回复的消息会恢复工作期。被 @ 触发时会跳过当前冷却，强制进入 3 分钟工作期并要求 LLM 回复。工作期中 LLM 成功主动回复且剩余时间不足 3 分钟时，工作期重置为 3 分钟。

主动回复的批量 JSON 包含消息 ID、时间、群名、发送人昵称、发送人 wxid、正文、消息类型和 @ 状态。工作期内消息统一先进入十秒批次，即使批次中包含多条 @ 或网址消息，也由一次 LLM 请求结合起来判断和生成回复；@ 和白名单网址只会将该批次标记为必须处理，不会逐条单独请求。LLM 返回 `should_reply`、`reply_to`、`messages` 和 `animation`，由程序决定是否实际发送。
启用非 prefix 自主回复后，主动回复状态机会接管这些消息；即使发送者配置了 `prefix_bypass_wxids`，也不会绕过工作期批处理而逐条直接请求 LLM。
主动回复会先让 LLM 结合完整上下文自行判断是否正在与 bot 互动，而不是只匹配 bot 名称或关键词；无法确认时默认保持旁观。只有长时间延续的同一个乐子话题才允许偶尔自然补一句，不会参与每一轮。

群聊回复会在生成提示中进行发送前自审：避免露骨描述性内容；涉及不适合大群的话题时改用简短、中性的概括或礼貌拒答，不重复具体细节。

### 群聊风格与黑话长期学习

LLM 内置了轻量化的本地学习模块，代码位于 `llm/learning/`。它不为每条消息调用 LLM，而是由后台有界队列异步提取消息长度、提问/回复比例、短句比例和跨发送人的重复表达，保存到 `data/llm_learning.sqlite3`。只有达到最低出现次数、具有足够发送人覆盖且未命中敏感词的表达，才会作为“高置信度常用表达”注入 Prompt；模型仍需自行判断是否自然使用，不会为了模仿群友强行复读。

根目录的 JSONL 群聊导出可以用于初始化画像：

```powershell
python -m llm.learning --input "群聊_GDUT SIE Minecraft Public Server.jsonl" --group "GDUT SIE Minecraft Public Server" --replace
```

学习默认排除 `/` 命令、以 `@服务器状态@我` 开头的消息和 bot 自己的消息。bot wxid 可填入 `llm.bot_wxids`；WeFlow 的 `myWxid` 会自动加入。实时学习只落本地派生统计，不发送原始聊天到外部服务。可在 `llm.learning` 中调整 `enabled`、`min_term_count` 和注入 Prompt 的 `prompt_max_chars`。

### LLM 网页工具

LLM 使用 DeepSeek 官方 OpenAI 兼容的 `tools` / `tool_choice="auto"` 协议调用 `fetch_webpage`。模型可以自主决定是否读取公开 HTTP/HTTPS 网页；程序会限制请求超时、正文长度，并拒绝本机和内网地址。工具结果会作为不可信网页数据返回给模型，不会被当作系统指令执行。主动回复收到只包含 `bilibili.com` 或 `b23.tv`（含子域名）网址的消息时，会抓取网页并返回简要内容梗概；其他网址不会触发该规则，抓取失败时不会编造内容。
如果 LLM 返回内容不是合法 JSON 或 JSON 字段格式错误，程序会直接发送完整原文；如果 API 返回 HTTP 402，则发送“余额不足，请检查llm余额是否充足”。

### LLM 原始消息查询工具

当消息记录看起来像聊天记录、转发摘要、引用或被截断的 app 消息时，LLM 可以自主调用 `fetch_original_message`，使用消息 JSON 中已有的 `session_id` 与 `local_id`/`server_id` 查询 WeFlow 的消息。该接口调用 WeFlow 自带的 `chatService` 解析流程，返回完整的解码/解析消息对象（包括聊天记录列表和类型特定字段），而不是直接返回 WCDB 未解码行。工具只允许查询当前会话，禁止猜测 ID，并限制调用次数和超时；返回内容只作为不可信上下文，不会被当作指令执行。该接口由 WeFlow 的鉴权 HTTP API `/api/v1/messages/original` 提供。

### 外部服务

- `mc` / `rcon`：仅为 Minecraft 示例功能服务；`/rcon <指令>` 原样发送到 RCON，仅 `llm.admin_wxids` 中的管理员可用。
- `comfy`：`anima/` 和图片插件需要自行部署 ComfyUI、准备工作流和输出目录。
- `tournament`：需要 Minecraft 数据包 Storage、RCON 和 `usercache.json` 映射，是特定服务器场景的示例。

## 插件开发

### 命令插件

将 `.py` 文件放入 `plugins/`。启动时会自动扫描，默认使用文件名生成 `/命令名`，也可以显式声明 `COMMAND` 和 `ALIASES`：

```python
COMMAND = "/hello"
ALIASES = ["/hi"]

def handle(content, context):
    return f"你好，{context['user']}"
```

`handle` 可以返回字符串、结构化字典或 `None`。结构化返回示例：

```python
return {
    "target": context["group"],
    "messages": ["第一段", "第二段"],
    "animation": "doge",
    "mode": "wechat_text"
}
```

### 自动插件

将 `.py` 文件放入 `auto/`，按需实现：

- `init(config)`：接收配置并初始化资源
- `handle_auto(context)`：处理消息事件
- `start(sender)`：启动后台任务
- `MATCH_RAW_MESSAGE = True`：声明接收无前缀原始消息
- `FALLBACK_ONLY = True`：仅在前面的处理均未返回时执行

主动回复不再属于自动插件；自动插件仅用于拍一拍、图片 follow-up 等事件。插件加载、Context 字段、返回值协议、Follow-up、媒体下载和热重载约定见 [DEV_GUIDE.md](DEV_GUIDE.md)。

## 项目结构

```text
main.py / launcher.py   程序入口与热重载守护
start.bat               Windows 构建、依赖安装和循环启动脚本
config.json             本地配置，不应提交
core/                   路由、队列、Worker、Dispatcher、WeFlow 和发送接口
plugins/                命令插件示例
auto/                   自动事件/后台任务插件示例
services/               Minecraft、图片和锦标赛业务服务
llm/                    DeepSeek Provider、Prompt、主动回复、上下文和记忆
utils/                  SQLite、积分、去重等通用工具
data/                   运行时数据和本地群聊历史
assets/                 字体与图片资源
anima/                  ComfyUI 工作流示例
weflow-core/            随仓库提供的 WeFlow Node.js 内核源码
```

## 开发文档

- [DEV_GUIDE.md](DEV_GUIDE.md)：架构、消息 Context、插件 API、LLM 调用链和开发规范
- [sample_config.json](sample_config.json)：v2 配置模板
- [admin_commands.txt](admin_commands.txt)：预置管理员命令说明
- [requirements.txt](requirements.txt)：Python 依赖
- [weflow-core/package.json](weflow-core/package.json)：WeFlow 构建脚本和 Node.js 依赖

## 测试与检查

仓库当前没有统一的 CI 测试入口。修改插件后至少建议执行：

```powershell
python -m compileall core plugins auto services llm utils
```

然后使用 `/ping`、`/help` 和实际需要的插件做一次手工验证。涉及 LLM、RCON 或 ComfyUI 的功能需要对应外部服务可用。

## 许可证与致谢

根目录许可证见 [LICENSE](LICENSE)。项目集成或参考了 WeFlow、WechatRobot、DeepSeek、ComfyUI、mcstatus、mcrcon 等组件；分发和部署时请遵守各依赖项目的许可证及使用要求。
