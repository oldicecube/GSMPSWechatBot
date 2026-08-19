# GSMPS WeChat Bot

一个运行在 Windows 上、基于 WeFlow 的微信机器人框架。它将群消息接收、命令插件、自动事件、LLM 对话和统一消息发送组合在一起，可用于构建具备上下文、记忆与群聊互动能力的微信机器人。

## 核心能力

- **微信消息接入与发送**：通过 WeFlow 接收群聊和私聊消息，统一发送文字、图片、文件与语音。
- **命令插件**：`plugins/` 中的插件提供群管理、查询、游戏服务、图片与实用工具等命令能力。
- **自动插件**：`auto/` 中的事件插件可处理媒体 follow-up、消息监听和后台任务。
- **多协议 LLM API 池**：支持 OpenAI Chat Completions、OpenAI Responses 与 Anthropic Messages；可按优先级配置多路模型并在单次请求内自动回退。
- **群聊对话与主动参与**：支持前缀唤起、@ 唤起和可选的群聊上下文参与；本地门控用于控制不必要的模型请求与发言频率。
- **记忆与群聊学习**：按群维护短期上下文、长期记忆、人物信息、黑话和句式表达库，并在群聊周期结束时整理新学习材料。
- **语音与点歌**：音频通过 Windows 虚拟麦克风录入微信语音；`/song`、`/music` 由网易云搜索歌曲，再通过 Node.js 长驻守护进程执行 `music-source/` 下的洛雪自定义源脚本解析 URL（init 一次、多次复用，支持 `updateAlert` 自动更新脚本），失败时回退网易云。
- **可选服务集成**：仓库包含 Minecraft/RCON、ComfyUI、网页工具等可选能力，按配置启用即可。

## 项目结构

| 目录/文件 | 说明 |
|---|---|
| `core/` | 消息路由、队列、调度、发送与 WeFlow 接入 |
| `plugins/` | 显式命令插件 |
| `auto/` | 自动事件、媒体 follow-up 与后台监听插件 |
| `llm/` | LLM API 适配、提示词、上下文、记忆、学习与主动回复 |
| `services/` | 外部业务服务封装，例如音乐、MC、RCON、ComfyUI |
| `utils/` | 通用工具、SQLite 与数据处理模块 |
| `weflow-core/` | WeFlow 相关 Node.js 工程 |
| `assets/` | 图片、字体等静态资源 |
| `sample_config.json` | 可复制的配置模板 |

## 快速开始

### 环境要求

- Windows 10/11
- Python 3.10 或更高版本
- Node.js LTS
- 已安装并登录的微信客户端，以及可用的 WeFlow 环境

### 安装与启动

复制配置模板并填写运行所需参数：

```powershell
Copy-Item sample_config.json config.json
```

安装 Python 与 WeFlow 依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Push-Location weflow-core
npm ci
npm run build
Pop-Location
```

完成 `config.json` 后启动：

```powershell
.\start.bat
```

也可以直接运行：

```powershell
python main.py
```

首次使用至少需要填写目标群、机器人前缀、WeFlow 账号数据和 `llm.apis` 中的模型配置。完整字段说明见 [config_guide.md](./config_guide.md)。

## 基本使用

- **前缀与 @**：在 `prefix_mode: "strict"` 下，以配置的前缀或 @ 机器人触发 LLM；`"mixed"` 还允许机器人在合适的群聊上下文中自主参与。
- **普通命令**：使用 `/<命令> <参数>` 调用 `plugins/` 中的功能。可用命令以实际启用的插件为准。
- **图片 follow-up**：例如先发送 `/对称 左右`，再发送一张图片，机器人会处理后续图片。
- **点歌语音**：`/song <歌曲名>` 或 `/music <歌曲名>` 发送歌曲前 59.5 秒；可换行指定片段：

  ```text
  /song 歌曲名
  12.5 50
  ```

  只填写起始秒时，终止位置默认为起始秒后 59.5 秒；单段语音最长不超过 59.5 秒。

  音源脚本放在 `music-source/`；顺序由 `source_order.txt` 或文件名排序决定（见 [config_guide.md](./config_guide.md)），无需在代码里写死。

## LLM、上下文与学习

LLM 请求会根据 API 优先级选择可用端点，并支持同一请求内依次尝试后备端点。机器人将稳定提示词、群记忆和历史上下文按适合缓存的顺序组织；不同协议的缓存参数由 API 配置控制。

群聊学习围绕完整消息单元进行：短期上下文、记忆、黑话、句式和风格数据按群保存，未完成的学习材料会留待后续周期处理。表达库可以为回复提供群聊中常用的自然句式，但不会替代任务要求或正常回答。

有关 API 协议、缓存、提示词结构、主动参与门控和学习链路的开发说明见 [DEV_GUIDE.md](./DEV_GUIDE.md)。

## 文档索引

- [配置参考](./config_guide.md)：`config.json` 的字段、取值和示例。
- [开发指南](./DEV_GUIDE.md)：架构、插件开发、自动插件、LLM 调用链、发送接口与调试约定。
- [管理命令](./admin_commands.txt)：已提供的管理类命令说明。
- [配置模板](./sample_config.json)：可直接复制并填写的最小配置示例。

## 许可证

本项目使用仓库根目录 [LICENSE](./LICENSE) 中的许可证。
