# GSMPS WeChat Bot

一个基于 [WeFlow](https://github.com/Double6ix/WeFlow) 的 Windows 微信机器人框架。它负责接收微信消息、执行路由和插件、调用 LLM，并通过统一发送接口回复文本或文件。

> 仓库中的插件和业务服务主要是 GSMPS / Minecraft 群聊场景的示例实现。部署前请逐项检查群名、服务器地址、账号标识、API Key 和本地路径；不需要的示例插件应禁用或移除。

## 功能概览

- WeFlow 消息接入与微信消息发送
- 精确群聊白名单、前缀路由、命令分发和速率限制
- `plugins/` 命令插件与 `auto/` 自动事件插件
- DeepSeek LLM 转发、JSON 响应解析、群聊上下文和表情发送
- Minecraft 状态查询、RCON、玩家监控和 The Room 锦标赛示例
- ComfyUI 图片生成与媒体发送示例
- 本地 JSON / SQLite 工具、管理员命令和项目级 Python 热重载

## 运行边界

- 当前项目面向 Windows 微信客户端，微信需要保持登录。
- `target_group` 是精确匹配的群聊白名单；空数组或未匹配群名会被路由器直接忽略。
- `prefix_mode` 默认为 `only`：只有带配置前缀的消息才进入路由；`mixed` 和 `off` 允许无前缀内容继续参与命令/自动插件处理。
- 插件和自动插件优先于 LLM。未命令命中、且满足前缀/随机回复等条件的消息才会转发到 LLM。
- 当前 LLM 实现是 `llm/core/llm_service.py` 调用 DeepSeek API，历史记录保存在 `data/groups/<群名>/` 下的 JSON 文件。

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
    "max_history": 100,
    "history_expire_ms": 600000,
    "group_message_limit": 20,
    "intercept_auto_plugins": ["picture"],
    "prefix_bypass_wxids": []
  }
}
```

LLM 的基本放行条件是：消息通过路由、没有被命令插件处理、满足前缀或配置的 `prefix_bypass_wxids`，且 `llm.enabled` 为 `true`。配置在 `llm.intercept_auto_plugins` 中的自动插件还必须实现 `INTERCEPT_LLM = True` 和 `allow_llm(context)`，否则会阻断转发。

`llm.random_reply.enabled` 默认关闭。开启后，`auto/random_reply.py` 会在冷却结束时等待下一条符合条件的群消息，并将其转发给 LLM；它仍受 `target_group` 限制，并默认排除命令、@消息和媒体消息。

### 外部服务

- `mc` / `rcon`：仅为 Minecraft 示例功能服务，不使用时留空并禁用相关插件。
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

插件加载、Context 字段、返回值协议、Follow-up、媒体下载和热重载约定见 [DEV_GUIDE.md](DEV_GUIDE.md)。

## 项目结构

```text
main.py / launcher.py   程序入口与热重载守护
start.bat               Windows 构建、依赖安装和循环启动脚本
config.json             本地配置，不应提交
core/                   路由、队列、Worker、Dispatcher、WeFlow 和发送接口
plugins/                命令插件示例
auto/                   自动事件/后台任务插件示例
services/               Minecraft、图片和锦标赛业务服务
llm/                    DeepSeek Provider、Prompt、上下文和记忆
utils/                  SQLite、积分、去重等通用工具
data/                   运行时数据和本地群聊历史
assets/                 字体与图片资源
anima/                  ComfyUI 工作流示例
weflow-core/            随仓库提供的 WeFlow Node.js 内核源码
```

## 开发文档

- [DEV_GUIDE.md](DEV_GUIDE.md)：架构、消息 Context、插件 API、LLM 调用链和开发规范
- [sample_config.json](sample_config.json)：配置模板
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
