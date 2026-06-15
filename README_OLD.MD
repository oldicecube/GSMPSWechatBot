# WeChat-MC-Robot 开发文档 & 使用手册
一款基于 WeFlow + WechatRobot 搭建的微信机器人，集成插件化命令系统、后台自动任务、Minecraft 服务器状态监控能力，轻量化、易扩展、支持热重载，适配群聊/私聊双场景。

## 项目简介
本项目为自用实验级微信机器人框架，采用分层架构设计，核心解耦、插件化开发，内置消息调度、事件监听、统一消息发送、MC 服务器查询等能力，可快速拓展自定义命令与后台定时任务。
> 提示：项目为早期实验版本，可能存在部分 Bug，生产环境请酌情使用。

## 一、环境部署
### 1. 前置依赖软件
需提前部署并保持常驻运行：
- WeFlow：https://github.com/hicccc77/WeFlow/tree/main
- WechatRobot：https://github.com/jankenliu/WechatRobot

### 2. Python 依赖安装
```bash
# 安装项目依赖（仅需一次）
pip install -r requirements.txt

# 或手动安装
pip install Pillow>=9.0.0 requests>=2.25.0
```

### 3. 基础配置与快速启动
#### 步骤一：编辑 config.json
打开 `config.json`，修改以下必需项：
```json
{
  "token": "从 WeFlow 设置页面复制的 API Token",
  "target_group": ["你的群聊名称"],
  "prefix": ["@bot"],
  "mc": {
    "host": "你的 MC 服务器地址",
    "port": 25565
  },
  "rcon": {
    "host": "RCON 服务器地址",
    "port": 25575,
    "password": "RCON 密码"
  }
}
```

补充说明：
1. `mc.port` 为可选项；不填写时会沿用 `mcstatus` 的默认解析逻辑。
2. `rate_limit.auto_blacklist_enabled` 用于开启“超限自动拉黑”。
3. `rate_limit.auto_blacklist_threshold` 表示单个 `wxid` 因个人速率限制被拒绝多少次后自动写入 `data/blacklist.json`。
4. `llm.admin_wxids` 为管理员 wxid 列表，管理员可使用特殊命令如 `/ban` 和 `/echo --delete`。

#### 步骤三：启动程序
```bash
# Windows 用户
double-click start.bat

# 或在命令行运行
python main.py

# Linux/Mac 用户
python3 main.py
```

#### 检查启动是否成功
启动后在控制台应看到类似输出：
```
[WORKER] 线程启动
[PLUGIN REGISTER] ping -> /ping
[AUTO REGISTER] random_reply
[PLUGIN] 初始化配置...
```

#### 测试机器人响应
在配置的目标群发送：
```
@bot /ping
```
机器人应回复：`pong`

#### 当前推荐的开箱即用配置方式
对于大多数直接运行机器人的用户，建议至少完成以下配置：

```json
{
  "token": "从 WeFlow 设置中复制",
  "target_group": ["机器人测试"],
  "prefix": ["@bot"],
  "prefix_mode": "only",
  "worker_num": 4,
  "time_slots": [{"start": "00:00", "end": "23:59"}],
  "rate_limit": {
    "messages_per_minute": 10,
    "global_messages_per_minute": 20,
    "auto_blacklist_enabled": false,
    "auto_blacklist_threshold": 5
  },
  "send_delay": {
    "enabled": false,
    "min_seconds": 2,
    "max_seconds": 10
  },
  "llm": {
    "enabled": false,
    "provider": "deepseek",
    "model": "deepseek-v4-pro",
    "api_key": "",
    "max_history": 100,
    "history_expire_ms": 600000,
    "group_message_limit": 20,
    "intercept_auto_plugins": ["patpat", "picture"],
    "prefix_bypass_wxids": ["wxid_youridhere"],
    "admin_wxids": ["wxid_youridhere"],
    "emoji_dir": "data/emoji",
    "assistant_nickname": "LLM"
  }
}
```

如果只想先验证基础机器人是否正常工作：
1. 先将 `llm.enabled` 设为 `false`
2. 先验证 `/ping`、`/help`、`/status` 等普通命令
3. 确认 WeFlow 与 WechatRobot 链路正常后，再开启 `llm.enabled`

#### 配置项详解
详见项目根目录 `config.json`（配置文件模板，包含完整注释说明），或参考下方"核心功能改进说明"章节。

### 4. 运行要求
1. WechatRobot 全程后台运行，禁止关闭主程序
2. 保持微信客户端正常登录在线
3. 依赖 Python 环境，直接双击 `start.bat` 即可启动项目

## 二、项目整体架构
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

## 三、目录结构说明
```
├── config.json          # 全局配置文件（Token、MC服务器、自定义参数）
├── main.py              # 项目程序入口
├── start.bat            # Windows 一键启动脚本
├── assets/              # 静态资源目录
│   ├── fonts/           # 字体文件（图片渲染/文字绘制）
│   └── images/          # 静态图片资源
├── auto/                # 后台自动任务插件目录
├── core/                # 核心框架
│   ├── dispatcher.py    # 插件调度中心、事件广播系统
│   ├── router.py        # 消息路由、命令解析
│   ├── queue.py         # 异步消息队列
│   ├── worker.py        # 多线程任务处理器
│   ├── sender.py        # 统一消息发送接口
│   ├── weflow_client.py # WeFlow SSE 消息接收
│   ├── weflow_media.py  # WeFlow 历史消息/媒体下载工具
│   ├── followup.py      # 命令后的下一条消息公用通道
│   └── project_reloader.py # 监控任意 .py 变更并重启主程序
├── llm/                 # LLM 核心目录
│   ├── config.py        # LLM 配置读取
│   ├── core/            # LLM 服务与响应解析
│   ├── memory/          # LLM / 群聊上下文存储
│   ├── prompt/          # System Prompt / User Prompt 构造
│   ├── provider/        # DeepSeek / OpenAI SDK 转发层
│   └── security/        # 表情索引与辅助安全逻辑
├── plugins/             # 命令行插件目录
│   └── ping.py          # 官方示例测试插件
├── services/            # 业务服务层
│   ├── mc_api.py        # Minecraft 服务器状态查询
│   ├── mc_image.py      # 服务器状态图片生成
│   └── __init__.py
├── utils/               # 通用工具库
│   └── dedup.py         # 数据去重工具
├── data/                # 本地数据存储目录
└── output/              # 图片、文件输出目录
```

## 四、消息上下文 Context 参数
所有插件、监听函数统一接收 `context` 上下文参数，包含完整消息元数据。

### 1. 完整参数结构
```python
context = {
    "user": "用户名",
    "group": "群名",
    "sessionId": "会话ID",
    "type": "消息类型",
    "content": "命令参数",
    "wxid": "wxid_xxxxxx",
    "text": "清洗后正文内容",
    "is_group": True,
    "is_private": False,
    "event": "message.new",
    "sourceName": "发送者昵称",
    "groupName": "群聊名称",
    "sessionType": "group",
    "raw": {} # 原始完整消息对象
}
```

### 2. 常用字段说明
| 字段 | 说明 | 推荐场景 |
|------|------|----------|
| user | 消息发送者昵称 | 界面展示、日志输出 |
| group | 群聊名称 | 群聊场景判断、群专属功能 |
| content | 去除命令前缀的参数内容 | 命令参数解析 |
| wxid | 微信唯一标识 | 权限绑定、黑名单、用户数据存储 |
| text | 清洗后纯文本消息 | 关键词检测、内容过滤 |
| is_group | 布尔值，是否群聊消息 | 区分消息来源 |
| is_private | 布尔值，是否私聊消息 | 私聊专属功能限制 |
| raw | 原始消息对象 | 高级自定义拓展 |

### 3. 当前版本常见扩展字段
以下字段是当前仓库主链路中已经存在、开发时经常会用到的扩展字段：

| 字段 | 说明 |
|------|------|
| command | 当前命中的命令名，如 `/ping` |
| args | 命令参数 |
| auto_target | Follow-up 或原始消息命中的 auto 目标 |
| followup_payload | Follow-up 载荷 |
| prefix_used | 当前消息是否命中过 prefix |
| planned_send_delay_seconds | 当前这次回复预先计算出的全局发送延迟 |

### 4. 常用基础用法
```python
# 唯一用户身份识别（推荐）
wxid = context["wxid"]

# 获取发送者昵称
nickname = context["user"]

# 判断消息来源
if context["is_group"]:
    print("当前为群聊消息")
if context["is_private"]:
    print("当前为私聊消息")
```

## 五、插件开发指南
项目支持两类插件：**命令交互插件**、**后台自动插件**，内置项目级热重载。
当前热重载规则为：工程内任意 `.py` 文件发生变化后，程序会很快直接重启整个主进程，而不是只重载单个插件模块。

### 1. 命令插件（plugins 目录）
#### 基础规范
1. 插件文件存放于 `plugins/` 目录
2. 必须定义全局变量 `COMMAND = "/指令名"`
3. 必须实现 `handle(content, context)` 函数
4. 命令匹配规则：仅支持 `/字母/数字/下划线` 组合指令

#### 最简模板
```python
COMMAND = "/demo"

def handle(content, context):
    # 精准匹配指令
    if content.strip() != COMMAND:
        return None
    
    # 业务逻辑
    return f"你好 {context['user']}，指令调用成功"
```

#### 返回值规范
1. 字符串：自动以文本形式回复消息
2. 字符串列表：多条文本分段发送
3. None：不响应本次消息
4. 异常文案：自定义错误提示

#### 当前推荐的结构化返回协议
除了传统字符串返回外，当前仓库还支持更完整的 dict 返回协议，推荐新插件优先使用：

```python
return {
    "target": context["group"],      # 可选，默认当前群/用户
    "content": "普通文本内容",       # 文本发送时使用
    "mode": "wechat_text",           # wechat_text / wechat_file / rcon
    "delay_seconds": 0               # 可选，覆盖本次发送延迟
}
```

如果需要一次返回多条文本并在末尾附带一个表情/图片资源，可返回：

```python
return {
    "target": context["group"],
    "messages": ["第一句", "第二句"],
    "animation": "doge",             # 对应 llm.emoji_dir 中的文件名（不带扩展名）
    "mode": "wechat_text"
}
```

说明：
1. `messages` 会被框架按顺序拆分发送
2. 结构化多段消息只会在第一条发送前应用一次全局发送延迟
3. `animation` 会在所有文本发送后，以图片/文件形式发送对应资源

#### 媒体资源发送
统一使用框架内置发送接口，禁止直接调用第三方请求：
```python
from core.sender import send

# 发送纯文本
send(
    target=context["group"],
    content="通知文本内容",
    mode="wechat_text"
)

# 发送图片/文件
send(
    target=context["group"],
    file_path="output/demo.png",
    mode="wechat_file"
)
```

#### echo 插件当前约束
`/echo` 插件目前带有以下内置限制，二次开发或联调时建议一并知晓：

1. 写入内容不能为空白。
2. 写入内容长度上限为 60 个字符。
3. 按 `wxid` 限流：每个 `wxid` 每 10 秒最多请求一次 `/echo`。
4. **相似度检查**（消息长度 < 27 字符）：如果新消息与任意已存在消息的相似度超过 75%，禁止存入。
5. **连续字符检查**（消息长度 ≥ 27 字符）：去掉空格和回车后，如果有超过连续 20 个字符的内容相同，禁止存入。
6. **管理员删除命令**：管理员可使用 `/echo --delete` 删除最后一条存入的消息；普通用户使用此命令不会有任何反应。

说明：
1. “同一个人”优先按 `wxid` 判断；旧数据没有 `wxid` 时回退到 `user` 名称判断。
2. `/echo` 的随机读取与写入共用同一条 10 秒限流规则。

### 2. 自动插件（auto 目录）
用于后台常驻任务、定时检测、循环服务。当前项目里的 auto 插件分为两种模式：

1. `handle_auto(context)`：
每收到一条消息时都会被 Dispatcher 调用一次，适合关键词监听、轻量状态机、拍一拍回复等“依赖消息触发”的逻辑。

2. `start(sender)`：
程序启动时由框架自动调用一次，插件内部自己创建后台线程并循环执行，适合玩家监控、定时播报、轮询外部服务等“与新消息无关”的任务。

3. Follow-up 公用通道：
适合“当前命令执行后，只把下一条消息交给指定插件处理一次”的场景，例如 `/对称` 后等待下一张图片。

4. 原始消息命中声明：
适合 `patpat` 这类被动事件消息。它们通常不带 prefix，也不会先被命令化处理；插件可自行声明需要命中这类原始消息。

#### 消息触发型模板
```python
def handle_auto(context):
    content = context.get("content", "")

    if "关键词" not in content:
        return None

    return "自动回复内容"
```

#### 原始消息声明模板
```python
MATCH_RAW_MESSAGE = True


def handle_auto(context):
    content = (context.get("content") or "").strip()

    if "拍了拍" not in content:
        return None

    return "事件已命中"
```

#### 当前 auto 插件参与 LLM 拦截的声明方式
如果 auto 插件需要参与“是否允许当前消息继续转发到 LLM”的判定，必须显式声明：

```python
INTERCEPT_LLM = True


def allow_llm(context):
    # 返回 True: 允许继续放行到 LLM
    # 返回 False: 阻止当前消息进入 LLM
    return True
```

重要说明：
1. 只有同时满足 `INTERCEPT_LLM = True` 且实现了 `allow_llm(context)` 的插件，才可参与 LLM 放行判断
2. 是否参与由 `config.json -> llm.intercept_auto_plugins` 决定
3. 任一参与拦截的 auto 插件返回 `False`，当前消息都不会进入 LLM
4. 这套接口与 `handle_auto(context)` 分离，避免因 auto 插件“是否刚好返回消息”造成 LLM 拦截失效

#### 定时/常驻型模板
```python
import threading
import time

def start(send):
    def loop():
        while True:
            # 自定义业务逻辑
            send("定时任务触发")
            time.sleep(10)

    threading.Thread(target=loop, daemon=True).start()
```

#### Follow-up 模板
```python
from core.followup import register, consume


def handle_auto(context):
    session_id = context.get("sessionId")
    text = (context.get("content") or "").strip()

    if text == "/demo":
        register(
            session_id=session_id,
            target="my_auto_plugin",
            ttl=30,
            payload={"step": "wait_next"}
        )
        return None

    state = consume(session_id)
    if not state or state.get("target") != "my_auto_plugin":
        return None

    payload = state.get("payload") or {}
    print("收到下一条消息，开始处理", payload)
    return None
```

#### WeFlow 媒体模板
```python
from core.weflow_media import extract_timestamp
from core.weflow_media import find_image_url_by_timestamp
from core.weflow_media import download_image


def handle_auto(context):
    raw = context.get("raw") or {}
    session_id = context.get("sessionId")
    target_ts = extract_timestamp(raw) or extract_timestamp(context)

    image_url, diff = find_image_url_by_timestamp(session_id, target_ts, limit=20)
    if not image_url:
        return None

    img = download_image(image_url)
    print("图片已获取", diff, img.size)
    return None
```

#### 重要经验
1. 只有定义了 `handle_auto(context)` 的插件，才会在“收到新消息”时执行。
2. 只有定义了 `start(sender)` 的插件，才会在程序启动时被框架自动启动。
3. `start(sender)` 本身不要直接写死循环，应该在函数内部再起一个 `daemon=True` 的线程，否则会阻塞初始化流程。
4. 如果需求是“过一段时间自动进入某状态，然后等下一条消息触发”，不要只在 `handle_auto()` 里检查时间；应该自己启动一个后台定时线程维护状态。
5. `sender` 是框架注入的统一发送函数，直接 `sender("文本")` 即可发到当前配置的目标群。
6. 如果需求是“命令后等待下一条消息”，不要在 `Router` 里为单个插件写特判；应使用 `core.followup.register()` 登记会话，并在插件里用 `consume()` 消费一次。
7. Follow-up 通道是“一次性”的。无论下一条消息是否符合预期，只要插件调用了 `consume()`，等待状态都会被释放，不会长期占住会话。
8. 当前主路由默认必须先命中 prefix；只有 follow-up 已登记的会话，下一条无 prefix 消息才会被定向放行给对应插件。
9. 媒体类 follow-up 推荐用下一条消息自己的时间戳去 WeFlow API 反查资源，而不是简单抓“最近一张图”，这样更稳。
10. WeFlow 媒体下载不要直接 `requests.get(mediaUrl)`；有些地址需要带 `Authorization: Bearer <token>`，核心里的 `core.weflow_media.download_image()` 已经统一处理了。
11. `core.weflow_media` 的 token 由程序启动时从 `config.json` 注入，插件侧不要再各自保存 token 常量，避免配置改了以后出现 401。
12. 当前项目的热重载是“整进程重启”模型，不再是插件级 `importlib.reload()`。只要工程中任意 `.py` 文件变化，主程序就会自动重启并重新初始化所有线程、插件和配置。
13. 当前热重载通过 `os.execv(...)` 直接替换当前 Python 进程镜像，所以旧主线程和旧工作线程会随原进程一起结束，不会与新主线程并行共存。
14. 需要跨消息维护状态的插件，不要依赖模块热更新保留内存状态；因为整进程重启后，内存态会被重新初始化。
15. `player_monitor` 首次启动时只建立“当前在线玩家基线”和上线起始时间，不发送“玩家进服”消息；后续轮询才开始正常检测上下线变化。
16. 如果 auto 插件需要命中“未经过 prefix 命令化处理的原始消息”，应在插件内声明 `MATCH_RAW_MESSAGE = True`，而不是继续在 `Router` 里为单个插件写死特判。
17. 当前 LLM 转发必须命中 prefix；即使 `prefix_mode` 配成 `mixed` 或 `off`，进入 LLM 的消息仍然必须先匹配 prefix。
18. 当前消息在真正转发到 LLM 前，会自动剥离 prefix；本地记录到 `llm_history.json` 的 LLM 对话内容也使用去 prefix 后的文本。
19. `group_messages.json` 不记录 LLM 对话本身，而是由 `Worker` 对目标群的普通群聊文本做被动监听，自动维护最近 20 条“其他群聊消息”。

#### 适用场景建议
1. `patpat` 这类依赖消息文本匹配的插件，用 `handle_auto(context)`。
2. `player_monitor` 这类轮询 MC 玩家列表的插件，用 `start(sender)`。
3. `random_reply` 这类“定时进入待机，再由下一条消息触发”的插件，推荐 `init(config)` + 后台线程 + `handle_auto(context)` 组合写法。
4. `picture` 这类“命令后等待下一条图片消息”的插件，推荐 `handle_auto(context)` + `core.followup` 公用通道组合写法。
5. `patpat`、系统通知、拍一拍提醒这类无 prefix 的被动事件，推荐 `MATCH_RAW_MESSAGE = True` + `handle_auto(context)` 组合写法。

## 六、全局事件监听系统
Dispatcher 内置轻量事件广播系统，旁路监听所有消息，不影响主命令流程，适用于日志、风控、关键词监控。

### 1. 注册方式
```python
from core.dispatcher import dispatcher

# 装饰器注册（推荐）
@dispatcher.on_message
def message_logger(context):
    print(f"[{context['user']}] 发送消息：{context['text']}")

# 函数直接注册
def keyword_monitor(context):
    if "违规词" in context["text"]:
        print("检测到敏感内容")

dispatcher.on_message(keyword_monitor)
```

### 2. 核心特性
1. 全量消息触发，群聊/私聊统一监听
2. 多监听器并行执行，互不阻塞
3. 无返回值依赖，仅做旁路处理
4. 禁止在监听函数中执行阻塞、IO 耗时操作

### 3. 适用场景
- 全局消息日志记录
- 敏感关键词过滤
- 用户行为统计分析
- 全局调试信息输出

## 七、Minecraft 服务器监控 API
内置封装 MC 服务器状态查询服务，支持 Java 版服务器在线状态、延迟、玩家列表、人数统计。

### 1. 接口引入
```python
from services.mc_api import status, player_list
```

### 2. 核心接口
#### 全量状态查询 status()
```python
data = status()
```
返回结构：
```python
{
    "online": True,                # 服务器在线状态
    "latency_ms": 42.5,            # 网络延迟
    "online_players": 3,           # 当前在线人数
    "max_players": 20,             # 服务器最大容纳人数
    "players": ["玩家A", "玩家B"]   # 在线玩家列表
}
```

#### 轻量玩家列表 player_list()
```python
players = player_list()
# 返回：["玩家A", "玩家B"]
```

### 3. 完整使用示例
```python
from services.mc_api import status

COMMAND = "/mc"

def handle(content, context):
    data = status()
    if not data:
        return "❌ 服务器连接超时或离线"

    status_text = "🟢 在线" if data["online"] else "🔴 离线"
    msg = (
        f"===== MC服务器状态 =====\n"
        f"运行状态：{status_text}\n"
        f"网络延迟：{data['latency_ms']}ms\n"
        f"在线人数：{data['online_players']}/{data['max_players']}\n"
        f"在线玩家：{', '.join(data['players']) if data['players'] else '无'}"
    )
    return msg
```

### 4. 注意事项
1. 接口默认 3 秒超时，超时自动返回 None
2. 禁止循环高频调用，建议调用间隔 ≥2秒
3. 延迟、玩家列表字段可能为空，需做判空处理
4. 接口线程安全，支持多插件同时调用

## 八、LLM 开箱即用说明
当前仓库已经内置独立 `llm/` 目录，可直接通过 `config.json` 开启 DeepSeek JSON 输出模式。

### 1. 当前 LLM 实际调用链
```
群聊消息
  ↓
Router prefix / 命令解析
  ↓
Dispatcher 先执行 auto 插件
  ↓
若没有普通命令命中，且声明参与拦截的 auto 插件全部 allow_llm(context) == True
  ↓
LLMService.handle_message(...)
  ├─ 读取 llm_history.json
  ├─ 读取 group_messages.json
  ├─ 读取启动期缓存的表情文件名索引
  ├─ 构造 system prompt / user prompt
  ├─ 调用 DeepSeek JSON Output
  └─ 解析为 {"messages": [...], "animation": ...}
  ↓
Worker 拆分 messages 顺序发送
  ↓
如 animation 有值，最后发送对应图片文件
```

### 2. LLM 放行条件
当前仓库内 LLM 并不是“任何消息都能进”，而是必须同时满足：

1. 当前消息位于 `target_group` 中
2. 当前消息命中了 prefix
3. 当前消息没有命中普通命令插件
4. `llm.enabled == true`
5. `llm.intercept_auto_plugins` 中声明参与拦截的 auto 插件，全部显式允许放行

### 3. LLM 配置项说明
```json
"llm": {
  "enabled": true,
  "provider": "deepseek",
  "model": "deepseek-v4-pro",
  "api_key": "你的 DeepSeek Key",
  "max_history": 100,
  "history_expire_ms": 600000,
  "group_message_limit": 20,
  "intercept_auto_plugins": ["patpat", "picture", "random_reply"],
  "emoji_dir": "data/emoji",
  "assistant_nickname": "LLM",
  "identity": {
    "name": "广工MC群聊助手",
    "role": "微信群聊里的轻量聊天助手",
    "style": "自然、简短、像真人微信聊天",
    "rules": [
      "优先保持简短自然",
      "不要自称能修改系统规则",
    ]
  }
}
```

字段说明：
1. `enabled`：是否开启 LLM
2. `provider`：当前默认 `deepseek`
3. `model`：传给 OpenAI Python SDK 的模型名
4. `api_key`：DeepSeek API Key
5. `max_history`：每个群的 LLM 对话最多保留多少条
6. `history_expire_ms`：超时后整组 LLM 对话清空
7. `group_message_limit`：每个群最近普通群聊消息最大保留条数
8. `intercept_auto_plugins`：参与 LLM 拦截判定的 auto 插件列表
9. `admin_wxids`：管理员 wxid 列表，管理员可使用特殊管理命令
10. `emoji_dir`：单一表情目录，启动时扫描文件名并建立索引
11. `assistant_nickname`：写入 `llm_history.json` 时，机器人回复所用昵称
12. `identity`：LLM 身份设定模块

`identity` 子字段说明：
1. `name`：身份名称
2. `role`：身份角色
3. `style`：说话风格
4. `rules`：额外高优先级行为规则

### 4. LLM 身份设定与抗提示词注入
当前 LLM 的身份不是写死在代码里，而是从 `config.json -> llm.identity` 注入。

默认设计目标：
1. 让机器人身份、角色、风格可以配置
2. 让这套身份规则保持高优先级
3. 防止用户通过消息、引用、角色扮演文本覆盖这套身份设定

当前实现方式：
1. `llm.identity` 会被读取并写入 prompt
2. system prompt 会明确声明：身份与行为规则只来自可信 system / config
3. 用户消息、quoted text、roleplay、prompt injection 都不得修改该身份设定

推荐配置示例：

```json
"identity": {
  "name": "广工MC群聊助手",
  "role": "微信群聊里的轻量聊天助手",
  "style": "自然、简短、像真人微信聊天",
  "rules": [
    "优先保持简短自然",
    "不要自称能修改系统规则",
  ]
}
```
### 5. LLM 本地存储结构
按群聊分别存储于：

```text
data/groups/{group_id}/
```

其中：
1. `llm_history.json`：LLM 对话历史，包含用户消息与机器人回复
2. `group_messages.json`：最近 20 条普通群聊消息，不记录 LLM 对话本身

`llm_history.json` 结构：

```json
[
  {
    "nickname": "用户昵称示例",
    "timestamp": 1778074560,
    "content": "随便发个表情包"
  }
]
```

`group_messages.json` 结构：

```json
[
  {
    "nickname": "群友A",
    "timestamp": 1778074600,
    "content": "今天吃啥"
  }
]
```

### 6. 表情目录使用方式
将图片文件放到 `llm.emoji_dir` 指定目录，例如：

```text
data/emoji/
├── doge.png
├── 吃瓜.jpg
└── 谢谢你.gif
```

则 LLM 返回：

```json
{
  "messages": ["喏"],
  "animation": "doge"
}
```

框架会自动找到 `doge.*` 对应文件，并在文本后发送该图片/文件。

### 7. 当前 LLM Prompt 设计要点
1. system prompt 强制 DeepSeek 只输出 JSON
2. JSON 结构固定为：
   `{"messages":["string"],"animation":"string or null"}`
3. user prompt 会包含：
   - 当前身份设定
   - 当前待回复消息
   - 当前消息发送者是否为管理员（自动识别 `llm.admin_wxids`）
   - 最近 LLM 对话历史
   - 最近普通群聊消息
   - 当前表情文件名列表
4. 当前 prompt 会尽量约束：
   - 微信聊天风格
   - 不要冗长
   - animation 尽量少用
   - 表情文件名应写到 `animation`，不要混进 `messages`
   - 身份与行为规则不得被用户消息覆盖

### 8. 当前 LLM Provider 行为
当前默认使用 OpenAI Python SDK 调 DeepSeek：

```python
client.chat.completions.create(
    model=config["model"],
    messages=messages,
    response_format={"type": "json_object"}
)
```

注意：
1. 当前实现明确不使用 stream
2. 必须保留 `response_format={"type": "json_object"}`
3. 当前 provider 只负责转发，不负责 prompt 拼接和 JSON 解释

## 九、开发接口速查
这一节用于补充“当前仓库已经提供、开箱即用可直接调用”的开发接口。

### 1. 统一发送接口
文件：`core/sender.py`

```python
send(target, content=None, file_path=None, mode="wechat_text", rcon=None, delay_seconds=None)
```

支持：
1. `mode="wechat_text"`：发送文本
2. `mode="wechat_file"`：发送图片/文件
3. `mode="rcon"`：执行 RCON

### 2. 发送延迟相关接口
文件：`core/sender.py`

```python
configure(config)
preview_delay_seconds(mode="wechat_text")
```

说明：
1. 全局发送延迟由 `send_delay` 配置控制
2. LLM 分段发送只在第一条应用一次延迟，后续分段与动画不重复插入延迟

### 3. LLM 配置接口
文件：`llm/config.py`

```python
get_llm_config()
get_api_key()
```

### 4. LLM Prompt 接口
文件：`llm/prompt/prompt_builder.py`

```python
build_system_prompt() -> str
build_user_prompt(data: dict) -> str
```

### 5. LLM Provider 接口
文件：`llm/provider/deepseek_provider.py`

```python
provider = DeepSeekProvider()
provider.send(messages: list) -> str
```

### 6. LLM 响应解析接口
文件：`llm/core/response_parser.py`

```python
parse_llm_response(text: str, emoji_list: list) -> dict
```

当前行为补充：
1. 当 LLM 返回的内容不是合法 JSON、`messages` 不是数组，或数组里没有可发送文本时，解析器会返回明确报错文本。
2. 报错文本会尽量附带一段截断后的原始返回内容，便于排查提示词或模型输出问题。
3. 不再使用单个 `？` 作为兜底响应。

### 7. LLM 服务接口
文件：`llm/core/llm_service.py`

```python
service = LLMService()
result = service.handle_message(group_id, nickname, content)
```

补充说明：
1. 若 LLM 未启用、记忆模块不可用、Provider 调用异常，服务层同样会返回结构化错误文本。
2. 这类错误仍然遵循统一返回结构：`{"messages": [...], "animation": None}`。

返回结构：

```python
{
    "messages": ["第一句", "第二句"],
    "animation": "doge"  # or None
}
```

### 8. LLM 存储接口
文件：`llm/memory/memory_manager.py`

```python
manager = MemoryManager()
manager.add_llm_message(group_id, nickname, content)
manager.get_llm_history(group_id)
manager.add_group_message(group_id, nickname, content)
manager.get_group_messages(group_id)
```

### 9. 表情索引接口
文件：`llm/security/emoji_index.py`

```python
build_emoji_index(emoji_dir=None)
get_emoji_list()
get_emoji_path(name)
```

## 十、开发强制规范
1. 所有消息推送必须使用 `core.sender` 统一接口，禁止原生请求
2. 耗时任务、循环任务必须新建子线程，禁止阻塞主线程
3. 命令插件指令必须以 `/` 开头，命名仅使用字母、数字、下划线
4. 禁止修改原始 `context` 上下文对象，避免全局异常
5. 插件逻辑轻量化，复杂业务拆分至 services 服务层
6. 严格区分自动插件与命令插件使用场景，职责解耦

## 十一、补充说明
1. 热重载：工程内任意 `.py` 文件变化都会触发整进程重启，而不是仅重载单个插件
2. 权限体系：基于 wxid 唯一标识实现用户权限、黑名单拓展
3. 拓展性：可自行新增图片生成、RCON 远程指令、定时播报等功能
4. 依赖致谢：本项目基于 WeFlow、WechatRobot 开源项目二次开发

## 十二、核心功能改进说明
本章节说明近期新增的核心功能改进。所有改进均已集成到主分支，无需额外操作。

### 1. 多 Prefix 支持
**功能**：支持配置多个命令前缀，用户可用任一前缀触发命令。

**配置示例**：
```json
{
  "prefix": ["@bot", "@服务器状态@我", "~"]
}
```

**使用方式**：
```
@bot /status          # 使用第一个前缀
@服务器状态@我 /ping   # 使用第二个前缀
~ /help              # 使用第三个前缀
```

**向后兼容**：单个前缀字符串配置仍然支持：
```json
{
  "prefix": "@bot"    /* 自动转为数组 */
}
```

**核心改动**：
- `core/router.py`：新增 `_has_prefix()` 和 `_extract_after_prefix()` 方法
- `main.py`：传递 prefix 参数到 Router 初始化

---

### 2. 时间段消息过滤
**功能**：仅在配置的时间段内响应消息，其他时间直接拦截。支持多个时间段，精确到分钟。

**配置示例**：
```json
{
  "time_slots": [
    {"start": "09:00", "end": "22:30"},  /* 工作时间 */
    {"start": "22:45", "end": "23:59"}   /* 晚间时间 */
  ]
}
```

**特性**：
- 支持跨午夜时间段：`{"start": "22:00", "end": "06:00"}`
- 时间段为空时默认全天响应
- 精确到分钟（HH:MM 格式）

**处理流程**：
```
消息到达
  ↓
[黑名单检测] → 拦截
  ↓
[时间段检测] → 不在时间段内拦截
  ↓
[群组过滤] → 拦截
  ↓
[正常处理]
```

**核心改动**：
- `core/router.py`：新增 `_is_in_time_slot()` 方法，支持跨午夜逻辑

---

### 3. 按 wxid 限制响应速率
**功能**：每个用户（基于 wxid）每分钟消息数超过限制时被拒绝。每个用户独立计数。

**配置示例**：
```json
{
  "rate_limit": {
    "messages_per_minute": 10,          /* 每个用户每分钟最多 10 条 */
    "global_messages_per_minute": 20,   /* 可选：全局每分钟最多 20 条 */
    "auto_blacklist_enabled": true,     /* 可选：开启超限自动拉黑 */
    "auto_blacklist_threshold": 5       /* 可选：累计 5 次超限后拉黑 */
  }
}
```

**特性**：
- 基于发送者 wxid 独立追踪
- 每条消息的时戳被记录到对应 wxid 的列表
- 清理：移除所有超过 60 秒的时戳记录
- 超过限制时直接返回 null，用户无感知
- 可选自动拉黑：当 `auto_blacklist_enabled = true` 且单个 `wxid` 的个人速率超限次数达到 `auto_blacklist_threshold` 时，自动写入 `data/blacklist.json`

**示例**：
```
用户A 5秒内发送 11 条消息 → 第 11 条被拒绝
用户B 同时发送 15 条 → 独立计数，仅前 10 条处理
```

**核心改动**：
- `core/router.py`：新增 `_check_rate_limit()` 方法，维护每个 wxid 的时戳列表


用户B: /对称 上下
用户B: [发送图片2] ← 处理用户B的对称请求

结果：两个处理完全独立，互不影响
```

**核心改动**：
- `core/followup.py`：改用 `(session_id, wxid)` 元组作为 key，新增 `clear_all_by_session()` 方法
- `core/router.py`：添加 `consume_followup` 导入，实现命令优先释放逻辑
- `auto/picture.py`：调用 followup 时传递 wxid 参数

---

### 5. 图片处理 60 秒超时自动释放
**功能**：图片处理等待状态 60 秒内无消息输入时自动释放，用户可重新发起新请求。

**特性**：
- 注册 followup 时设置 `ttl=60`
- 无需手动清理，框架自动处理过期状态
- 避免用户被"卡住"无法继续使用

**示例**：
```
用户: /对称 左右
[等待 60 秒无消息]
[自动释放状态]
用户: /对称 上下   ← 可以重新发起请求
```

**核心改动**：
- `core/followup.py`：peek/consume 检查过期状态
- `auto/picture.py`：`register_followup(..., ttl=60, ...)`

---

### 6. 命令消息优先释放处理线程
**功能**：图片处理等待中收到命令消息时，先释放等待状态，再让 router 处理该命令。

**示例场景**：
```
用户: /对称 左右
[等待图片]
用户: /help         ← 命令消息
[自动释放对称等待]
[执行 /help 命令]
```

**特性**：
- 命令消息（以 / 开头）优先被识别和处理
- 保留"非图片非命令"消息的释放逻辑（如纯文本消息）
- 用户无感知，流程透明

**处理逻辑**：
```
消息到达
  ↓
有待处理的 followup?
  ├─ 是命令(/开头)
  │   ├─ consume() 释放等待状态
  │   └─ 继续处理命令
  └─ 非命令
      └─ 交给 followup 目标插件处理
```

**核心改动**：
- `core/router.py`：在 followup 检查后添加命令检测逻辑

---

### 7. 部署依赖说明
新增 `requirements.txt` 文件，记录所有 Python 依赖

**安装方式**：
```bash
pip install -r requirements.txt
```

---

## 十三、常见问题排查
### Q：消息被拦截，机器人没有响应？
**可能原因**：
1. 检查当前时间是否在 `time_slots` 配置的时间段内
2. 检查发送者 wxid 是否在一分钟内超过 `rate_limit` 的限制
3. 检查前缀是否在 `prefix` 配置列表中
4. 检查群名是否在 `target_group` 配置中
5. 如果是 LLM，检查消息是否真的命中了 prefix
6. 检查 `llm.intercept_auto_plugins` 中的 auto 插件是否返回了 `allow_llm(context) == False`

### Q：图片处理报错或处理失败？
**可能原因**：
1. Pillow 库版本过低，请升级：`pip install --upgrade Pillow`
2. GIF 处理时缺少 requests 库，请安装：`pip install requests`
3. 图片格式不支持或损坏
4. 如果是 LLM 表情发送失败，检查 `llm.emoji_dir` 目录是否存在，并确认文件名是否与返回的 `animation` 一致

### Q：同一群多用户操作图片时出现干扰？
**已解决**：新版本使用 `(session_id, wxid)` 元组作为 key，完全隔离不同用户的处理状态。

### Q：如何禁用某项功能？
**示例**：
```json
/* 禁用时间段过滤（全天响应） */
"time_slots": []

/* 禁用速率限制 */
"rate_limit": {}

/* 禁用多 prefix（使用单前缀） */
"prefix": "@bot"

/* 禁用 LLM */
"llm": {
  "enabled": false
}
```

### Q：config.json 当前是否已经覆盖新功能？
当前模板已经包含并说明了以下新增配置：
1. `prefix_mode`
2. `send_delay`
3. `llm.enabled / provider / model / api_key`
4. `llm.max_history / history_expire_ms / group_message_limit`
5. `llm.intercept_auto_plugins`
6. `llm.prefix_bypass_wxids`
7. `llm.emoji_dir`
8. `llm.assistant_nickname`

## 开发补充说明

### 1. LLM prefix 放行白名单
当前仓库支持在 `config.json -> llm.prefix_bypass_wxids` 中配置一组 `wxid`，用于放行“未命中 prefix 但仍可进入 LLM”的特例用户。

```json
{
  "llm": {
    "enabled": true,
    "intercept_auto_plugins": ["patpat", "picture"],
    "prefix_bypass_wxids": ["wxid_youridhere"]
  }
}
```

说明：
1. 默认情况下，LLM 转发仍要求消息命中 `prefix`
2. 当发送者 `wxid` 命中 `prefix_bypass_wxids` 时，即使没有 `prefix`，也允许继续进入 LLM
3. 该配置只影响 LLM 转发条件，不改变命令插件或普通 auto 插件的命中规则

### 2. Auto 插件与 LLM 拦截
只有同时满足以下条件的 auto 插件，才会参与“是否允许当前消息继续转发到 LLM”的判定：

```python
INTERCEPT_LLM = True


def allow_llm(context):
    return True
```

补充规则：
1. 仅声明 `handle_auto(context)` 不会自动参与 LLM 拦截
2. 是否实际参与，还取决于 `config.json -> llm.intercept_auto_plugins`
3. 如果某个 auto 插件不应该影响 LLM 放行，请显式保持 `INTERCEPT_LLM = False`，或不要声明该标志

### 3. Fallback-only auto 模式
当前 `Dispatcher` 支持一种仅在“前面没有任何处理结果”时才执行的兜底型 auto 插件模式，适合不应抢占命令、普通 auto 或 LLM 流程的插件。

```python
FALLBACK_ONLY = True


def handle_auto(context):
    return None
```

说明：
1. `FALLBACK_ONLY = True` 的 auto 插件不会参与前面的普通 auto 广播阶段
2. 只有在当前消息未命中普通 auto、未命中命令、且未被放行到 LLM 时，才会进入该阶段
3. 适合“最后兜底处理”而不是“优先抢占处理”的自动化逻辑

### 4. Windows 运行时防休眠
主程序启动时会启用 `core/windows_awake.py` 中的 Windows keep-awake 逻辑，运行期间持续阻止系统因空闲而：
1. 睡眠
2. 休眠
3. 关闭显示器
4. 进入屏幕保护程序

补充说明：
1. 该逻辑用于防止“空闲触发”的系统节能行为
2. 如果用户手动执行 Windows 锁屏，系统仍然会被锁定
3. 该模块属于运行时基础设施，不影响插件开发接口
