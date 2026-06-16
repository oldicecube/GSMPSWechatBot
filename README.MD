# GSMPS Wechat Bot — 快速上手

基于 WeFlow 内核的微信机器人，内置插件化命令、LLM 对话、Minecraft 服务器状态监控。**WeFlow / WechatRobot 已集成于本项目，无需额外下载。**

---

## 一、环境要求

- Windows 10/11
- Python 3.10+
- 微信客户端（保持登录）
- [Node.js](https://nodejs.org/)（weflow-core 运行依赖）

---

## 二、首次配置（5 分钟）

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 创建配置文件

```bash
cd 你的工作目录
copy sample_config.json config.json
```

### 3. 填写 config.json 必填项

打开 `config.json`，按照 `sample_config.json` 中的 `_说明` 字段指引，逐项填入：

| 字段 | 说明 |
|------|------|
| `target_group` | 能够触发机器人回复的群聊名称列表 |
| `prefix` | 触发前缀列表，如 `["@bot"]` ，一般是“@<群聊中自己的昵称>”|
| `mc.host` | Minecraft 服务器地址（不用 MC 可留空） |
| `rcon` | RCON 配置（不用可留空） |

> 其他字段请根据需要补全

### 4. 启动

```bash
python main.py
# 建议直接双击 start.bat
```

看到 `[WORKER] 线程启动` 即表示成功。

### 5. 验证

在目标群发送 `@bot /ping`，应回复 `砰！(处理消息用时x.xxx秒，延迟x.xxx秒回复)`。

---

## 三、WeFlow 配置（重要）

WeFlow 是微信消息拦截/解密/注入的核心组件。**weflow-core 已集成在本项目的 `weflow-core/` 目录下，检测到缺失配置时自动拉起。**

### 获取 wxid 与 decryptKey

启动时检测到Weflow配置缺失时，控制台自动进入配置模式，根据指引完成配置即可

### 关键配置项

```json
{
  "weflow": {
    "dbPath": "微信的数据库地址",
    "decryptKey": "从工具获取的 64 位 hex",
    "myWxid": "wxid_xxxxxxxx",
    "wxidConfigs": {
      "wxid_xxxxxxxx": {
        "decryptKey": "同上",
        "imageXorKey": 53,
        "imageAesKey": "从工具获取",
        "updatedAt": 1750000000000
      }
    },
    "apiPort": 5031,
    "apiHost": "127.0.0.1",
    "resourcesPath": "./weflow-core/resources",
    "messagePushEnabled": true
  }
}
```

微信的数据库地址可以打开 `微信设置 -> 账号与存储 -> 存储位置` 来获取

自动获取密钥时，请先彻底退出微信，然后重新打开微信并在向导提醒：

```
Hook成功，现在登陆微信
```

后，开始扫码登录

> `apiPort` 默认为 5031，请勿与其他服务冲突。`resourcesPath` 指向 `weflow-core/resources` 即可。

---

## 四、LLM 对话（仅支持 DeepSeek）

当前 LLM 模块只对接了 **DeepSeek**。如需使用其他模型（OpenAI / Claude / 本地模型），需自行编写 `llm/provider/` 下的 Provider。

### 开箱启用

```json
{
  "llm": {
    "enabled": true,
    "provider": "deepseek",
    "model": "deepseek-v4-flash",
    "api_key": "sk-your-deepseek-api-key",
    "max_history": 100,
    "intercept_auto_plugins": ["patpat", "picture"],
    "emoji_dir": "data/emoji",
    "assistant_nickname": "LLM",
    "identity": {
      "name": "My Bot",
      "role": "微信群聊聊天Bot",
      "style": "自然、简短、像真人微信聊天",
      "rules": ["优先保持简短自然"]
    }
  }
}
```

> 将表情图片放入 `data/emoji/`，LLM 会自动选择发送。`intercept_auto_plugins` 中的插件可拦截消息使其不进入 LLM。

---

## 五、Anima 绘图（需自行搭建 ComfyUI）

`/anima` 命令依赖 **ComfyUI** 服务端。本项目不自带 ComfyUI，你需要：

1. 自行部署 ComfyUI（参考 [ComfyUI GitHub](https://github.com/comfyanonymous/ComfyUI)）
2. 将 `anima/workflow.json` 中的 workflow 导入 ComfyUI
3. 在 `config.json` 中配置 ComfyUI 地址：

```json
{
  "comfy": {
    "url": "http://127.0.0.1:8188",
    "output_dir": "D:\\ComfyUI\\output"
  }
}
```

> Anima 会排队生成，同一时间只处理一个人的请求。

---

## 六、预装可用命令一览

| 命令 | 功能 |
|------|------|
| `/ping` | 测试延迟 |
| `/help` | 查看帮助 |
| `/guide` | 新手指南（WeFlow 配置指引） |
| `/status` | 图片版 MC 服务器状态 |
| `/default_status` | 文字版 MC 服务器状态 |
| `/list` | 在线玩家列表 |
| `/player [玩家名]` | 玩家信息查询 |
| `/bind <玩家名>` | 绑定 MC 玩家名 |
| `/unbind` | 解绑玩家名 |
| `/sign` | 每日签到（积分） |
| `/points` | 积分查询 |
| `/echo [内容]` | 回声树洞（写入/随机读取） |
| `/anima <提示词>` | AI 绘图（消耗 8 积分，需 ComfyUI） |
| `/ba <文本1> <文本2>` | BA Logo 风格图片 |
| `/seed` | 服务器地图种子 |
| `/notice` | 最新群公告 |
| `/alert <内容>` | 转发文本至 MC 服务器 |
| `/对称 <方向>` | 发图后对称处理 |

> 管理员可用 `/ban` 拉黑用户、`/echo --delete` 删除回声。

---

## 七、⚠️ 使用前请阅读以下注意事项

建议翻完 **项目所有文件夹**，留意其中的说明文件：

| 位置 | 内容 |
|------|------|
| `auto/` | `player_monitor` 含 GSMPS 专用计分板逻辑，通用部署建议删除或改造 |
| `plugins/unbind.disabled` | 已禁用的解绑插件 |
| `data/` | 运行时数据，含黑名单 `blacklist.json`、群消息记录 |
| `llm/` | LLM 模块，仅支持 DeepSeek，其他 Provider 自行编写 |
| `anima/` | 依赖 ComfyUI + workflow.json，需自建 |
| `weflow-core/` | 微信内核，启动时自动拉起 Node 进程 |
| `config.json` | **已被 .gitignore 忽略**，不会提交到 Git |

---

## 八、插件开发速览

项目支持两类插件，均支持热重载（改动任意 `.py` 后自动重启进程）：

- **命令插件** (`plugins/`)：`COMMAND = "/xxx"` + `handle(content, context)`
- **自动插件** (`auto/`)：`handle_auto(context)` 消息触发 / `start(sender)` 后台线程

关键开发接口：

| 接口 | 用途 |
|------|------|
| `core.sender.send()` | 统一消息发送 |
| `core.followup.register/consume` | 命令后等待下一条消息 |
| `core.weflow_media.*` | 历史消息媒体下载 |
| `MATCH_RAW_MESSAGE = True` | 命中无 prefix 的原始消息 |
| `INTERCEPT_LLM = True` + `allow_llm()` | 拦截消息不进入 LLM |
| `FALLBACK_ONLY = True` | 兜底型 auto 插件 |

详细开发文档见 **[DEV_GUIDE.md](./DEV_GUIDE.md)**。

---

## 九、特别感谢

本项目基于以下开源项目集成开发：

- **WeFlow** — 微信消息拦截/解密/注入框架
- **WechatRobot** — 微信自动化发送框架

---

> 📖 开发者请阅读 [DEV_GUIDE.md](./DEV_GUIDE.md) 获取完整架构、API 参考与插件开发细节。
