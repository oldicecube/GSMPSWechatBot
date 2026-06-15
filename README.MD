# 🤖 WeChat Bot Framework

一个基于 [WeFlow](https://github.com/hicccc77/WeFlow) + [WechatRobot](https://github.com/jankenliu/WechatRobot) 的**插件化微信机器人框架**，支持命令交互、LLM 对话、Minecraft 服务器监控、后台自动任务，适配群聊 / 私聊双场景。

> 🧪 实验阶段项目，可能存在 Bug，生产环境请酌情使用。

> 喜欢我Vibe Coding吗

---

## ✨ 特性

- 🔌 **插件化架构** — 新增功能只需新建一个 `.py` 文件，无需修改主程序
- 🧠 **LLM 集成** — 内置 DeepSeek JSON 输出模式，支持表情包自动回复
- ⛏️ **Minecraft 监控** — 服务器状态查询、玩家列表、RCON 远程指令
- 🔄 **热重载** — 任意 `.py` 文件变更后自动重启进程
- 🎯 **多前缀 / 时间段过滤 / 速率限制** — 灵活的消息路由控制
- 📦 **开箱即用** — 编辑 `config.json` 即可启动

---

## 🚀 快速开始

### 前置依赖

确保以下软件已安装并保持运行：

| 软件 | 说明 |
|------|------|
| [WeFlow](https://github.com/hicccc77/WeFlow) | 微信消息推送框架 |
| [WechatRobot](https://github.com/jankenliu/WechatRobot) | 微信消息发送服务 |
| Python 3.9+ | 运行环境 |

### 安装

```bash
git clone <your-repo-url>
cd <your-repo>

或者直接去release下也可以

# 安装依赖
pip install -r requirements.txt
```

### 配置

编辑 `config.json`，填入必要信息：

```json
{
  "token": "从 WeFlow 设置中复制的 API Token",
  "target_group": ["你的群聊名称"],
  "prefix": ["@bot"]
}
```

> 完整配置项说明见 [config.json](config.json) 及下方[配置参考](#-配置参考)。

### 启动

```bash
# Windows
双击 start.bat

# 或命令行
python main.py
```

启动成功后会看到：

```
[WORKER] 线程启动
[PLUGIN REGISTER] ping -> /ping
[PLUGIN] 初始化配置...
```

### 测试

在目标群发送：

```
@bot /ping
```

机器人回复 `pong` 即表示运行正常。

---

## 📁 项目结构

```
├── config.json              # 配置文件
├── main.py                  # 程序入口
├── launcher.py              # 进程守护（崩溃自动重启）
├── start.bat                # Windows 启动脚本
│
├── core/                    # 核心框架
│   ├── weflow_client.py     # WeFlow SSE 消息接收
│   ├── router.py            # 消息路由 / 命令解析
│   ├── dispatcher.py        # 插件调度 / 事件广播
│   ├── worker.py            # 多线程任务处理器
│   ├── sender.py            # 统一消息发送接口
│   ├── queue.py             # 异步消息队列
│   ├── followup.py          # 下一条消息公用通道
│   └── project_reloader.py  # 热重载
│
├── plugins/                 # 命令插件（/命令）
│   ├── ping.py              # /ping - 测试延迟
│   ├── help.py              # /help - 帮助
│   ├── status.py            # /status - MC服务器状态图
│   ├── bind.py              # /bind - 绑定玩家
│   ├── sign.py              # /sign - 每日签到
│   └── ...
│
├── auto/                    # 后台自动插件
│   ├── patpat.py            # 拍一拍回复
│   ├── picture.py           # 图片对称处理
│   ├── player_monitor.py    # 玩家上下线监控
│   └── random_reply.py      # 随机复读
│
├── llm/                     # LLM 模块
│   ├── provider/            # DeepSeek 接口
│   ├── prompt/              # Prompt 构造
│   ├── memory/              # 对话记忆管理
│   └── security/            # 表情索引
│
├── services/                # 业务服务层
│   ├── mc_api.py            # MC 服务器状态查询
│   └── mc_image.py          # 状态图片生成
│
├── utils/                   # 工具库
├── data/                    # 运行时数据（gitignore）
├── assets/                  # 静态资源
│   ├── fonts/
│   └── images/
└── output/                  # 输出文件（gitignore）
```

---

## 🔌 插件开发

### 命令插件

在 `plugins/` 下新建 `.py` 文件：

```python
COMMAND = "/demo"

def handle(content, context):
    user = context["user"]
    return f"你好 {user}，这是 /demo 命令"
```

返回值规范：
- `str` — 发送文本消息
- `list[str]` — 多条文本分段发送
- `dict` — 结构化返回 `{"messages": [...], "animation": "doge"}`
- `None` — 不响应

### 自动插件（auto/）

**消息触发型**（每条消息调用一次）：

```python
def handle_auto(context):
    if "关键词" in context.get("content", ""):
        return "自动回复"
    return None
```

**后台常驻型**（启动时创建后台线程）：

```python
import threading
import time

def start(sender):
    def loop():
        while True:
            sender("定时播报")
            time.sleep(60)
    threading.Thread(target=loop, daemon=True).start()
```

> 完整插件开发指南见 [README_OLD.MD](README_OLD.MD#五插件开发指南)

---

## ⚙️ 配置参考

### config.json 完整结构

```json
{
  "token": "WeFlow API Token",
  "target_group": ["群聊名称"],
  "prefix": ["@bot"],
  "prefix_mode": "mixed",
  "worker_num": 4,

  "time_slots": [
    {"start": "08:00", "end": "02:00"}
  ],

  "rate_limit": {
    "messages_per_minute": 10,
    "global_messages_per_minute": 20,
    "auto_blacklist_enabled": true,
    "auto_blacklist_threshold": 5
  },

  "send_delay": {
    "enabled": true,
    "min_seconds": 1,
    "max_seconds": 5
  },

  "llm": {
    "enabled": true,
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    "api_key": "sk-your-api-key",
    "max_history": 100,
    "history_expire_ms": 600000,
    "group_message_limit": 20,
    "intercept_auto_plugins": [],
    "prefix_bypass_wxids": [],
    "admin_wxids": [],
    "emoji_dir": "data/emoji",
    "assistant_nickname": "Bot",
    "identity": {
      "name": "My Bot",
      "role": "微信群聊聊天Bot",
      "style": "自然、简短、像真人微信聊天",
      "rules": ["优先保持简短自然"]
    }
  },

  "mc": {
    "host": "your-mc-server.com",
    "fake_name": "Anonymous Player"
  },

  "rcon": {
    "host": "your-rcon-host.com",
    "port": 25575,
    "password": "your-rcon-password"
  },

  "comfy": {
    "url": "http://127.0.0.1:8188",
    "output_dir": "/path/to/ComfyUI/output"
  }
}
```

### 关键配置项

| 配置 | 说明 |
|------|------|
| `prefix_mode` | `"only"` 仅响应前缀 / `"mixed"` 前缀+无前缀混合 / `"off"` 全响应 |
| `time_slots` | 响应时间段，支持跨午夜，空数组 = 全天 |
| `llm.intercept_auto_plugins` | LLM 拦截判定参与的 auto 插件列表 |
| `llm.prefix_bypass_wxids` | 无需前缀即可触发 LLM 的白名单 wxid |
| `llm.admin_wxids` | 管理员 wxid，可使用 `/ban`、`/echo --delete` |
| `send_delay` | 全局发送延迟，模拟真人打字 |
| `rate_limit.auto_blacklist_enabled` | 超限后自动拉黑 |

---

## 🏗️ 架构概览

```
WeFlow SSE 推送
      │
      ▼
WeFlowClient ──► Queue ──► Worker 线程池
                              │
                              ▼
                          Router
                     (prefix / 时间 / 限流)
                              │
                              ▼
                        Dispatcher
                     ┌───────┼───────┐
                     ▼       ▼       ▼
                  Auto插件  命令插件   LLM
                     │       │       │
                     └───────┼───────┘
                             ▼
                      Sender（统一发送）
```

---

## 📋 内置命令

| 命令 | 说明 |
|------|------|
| `/ping` | 测试延迟 |
| `/help` | 获取帮助 |
| `/status` | MC 服务器状态（图片） |
| `/list` | 在线玩家列表 |
| `/player [玩家名]` | 玩家信息 / 运势 / 估值 |
| `/bind <玩家名>` | 绑定玩家名到微信 |
| `/unbind` | 解绑 |
| `/sign` | 每日签到 |
| `/seed` | MC 地图种子 |
| `/echo [内容]` | 回声树洞 |
| `/guide` | 新手指南 |
| `/home` | 服务器导航 |
| `/notice` | 群公告 |
| `/ban <wxid>` | 拉黑（管理员） |
| `/对称 <方向>` | 图片对称处理 |
| `/anima <提示词>` | AI 图像生成 |
| `/points --help` | 积分系统帮助 |

---

## ❓ 常见问题

**消息没有响应？**
1. 检查当前时间是否在 `time_slots` 内
2. 检查是否超过 `rate_limit` 限制
3. 检查前缀是否正确 / 群名是否在 `target_group` 中

**LLM 不工作？**
- 确认 `llm.enabled = true`
- 确认 `llm.api_key` 正确
- 消息必须命中 prefix（或发送者 wxid 在 `prefix_bypass_wxids` 中）

**如何禁用某项功能？**
```json
"time_slots": [],          // 全天响应
"rate_limit": {},          // 不限流
"llm": { "enabled": false } // 关 LLM
```

---

## 📖 详细文档

更完整的开发文档（消息上下文、事件监听、LLM Prompt 设计、Follow-up 通道等）见：

👉 **[README_OLD.MD](README_OLD.MD)**

---

## 🙏 致谢

本项目基于以下开源项目构建：

- [WeFlow](https://github.com/hicccc77/WeFlow) — 微信消息 SSE 推送
- [WechatRobot](https://github.com/jankenliu/WechatRobot) — 微信消息发送

---

## 📄 License

MIT
