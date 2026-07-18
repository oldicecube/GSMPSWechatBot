# GSMPS WeChat Bot

一个基于 WeFlow 的 Windows 微信群机器人框架。项目提供消息路由、命令分发、插件加载、LLM 接入、媒体处理、定时/事件任务和本地 SQLite 存储等基础能力，方便按自己的群聊或服务器需求扩展。

> **重要：仓库内的所有插件均为预置/示例实现。** 它们包含 GSMPS、Minecraft、特定群规则、特定第三方服务和本地路径等上下文；请在实际部署前自行阅读、修改、启用或删除，不应假设任何示例插件可直接用于你的环境。

## 环境要求

- Windows 10/11
- Python 3.10+
- Node.js（`weflow-core/` 运行依赖）
- 已登录的微信客户端

## 快速开始

1. 安装 Python 依赖：

   ```powershell
   pip install -r requirements.txt
   ```

2. 创建本地配置：

   ```powershell
   Copy-Item sample_config.json config.json
   ```

3. 编辑 `config.json`，至少填写：

   | 字段 | 作用 |
   | --- | --- |
   | `target_group` | 允许处理的群聊名称 |
   | `prefix` | 命令或 LLM 的触发前缀 |
   | `weflow` | 微信数据库、密钥与账号信息 |

4. 启动：

   ```powershell
   python main.py
   ```

   也可以使用 `start.bat`。缺少 WeFlow 配置时，程序会启动内置配置向导。

5. 在目标群用配置的前缀发送 `/ping`，确认路由和发送链路正常。

`config.json`、本地数据库、运行日志与生成内容均应仅保留在本机，默认不提交到 Git。

## 项目结构

```text
core/          消息路由、调度、队列、发送与 WeFlow 客户端
plugins/       命令插件示例
auto/          自动任务/事件插件示例
services/      可复用业务服务示例
utils/         SQLite 与通用工具
llm/           LLM 配置、上下文、Provider 与安全辅助逻辑
anima/         ComfyUI 绘图示例资源
data/          运行时数据目录
weflow-core/   已集成的 WeFlow Node.js 内核
```

## 插件机制

插件是本项目的主要扩展点。所有仓库内插件均以示例形式提供；复制、改造或移除它们前，请先检查其依赖的配置、外部服务和数据格式。

### 命令插件

位于 `plugins/`，最小形式：

```python
COMMAND = "/example"

def handle(content, context):
    return "hello"
```

可选实现 `init(config)` 读取配置。`context` 中包含用户、群聊、wxid、原始消息、是否使用前缀等信息。

### 自动插件

位于 `auto/`，可按需要实现：

- `init(config)`：读取配置和初始化资源
- `start(sender)`：启动后台任务
- `handle_auto(context)`：处理自动事件

详见 [DEV_GUIDE.md](DEV_GUIDE.md)。

## 预置示例插件

以下内容仅用于展示可扩展能力，并非通用产品功能：

| 类别 | 示例 | 部署前需要做的事 |
| --- | --- | --- |
| 基础命令 | `ping`、`help`、`echo`、`sign`、`points` | 按群规则修改文案、积分与数据策略 |
| LLM | `llm/`、`plugins/player.py` 的部分功能 | 配置 API Key、模型、提示词与 Provider |
| Minecraft | `status`、`list`、`player_monitor`、`theroom` | 配置服务器地址/RCON；删除 GSMPS 专用逻辑或按自己的服务器改造 |
| The Room 锦标赛 | `services/tournament/`、`plugins/theroom.py` | 需要匹配的 Minecraft 数据包 Storage、RCON 与 `usercache.json` 映射 |
| 图像 | `anima`、`ba`、`picture`、`patpat` | 准备 ComfyUI、模型、工作流与输出路径 |

建议首次部署时先仅保留 `/ping` 等最简单的插件，确认基础链路后逐个启用和改造其他示例。

## 可选服务配置

### WeFlow

`weflow-core/` 已随仓库提供。`weflow` 配置缺失时会进入向导；微信数据库路径可在微信的“设置 → 账号与存储 → 存储位置”中获取。

### LLM

默认示例使用 DeepSeek。其他模型需在 `llm/provider/` 中自行实现 Provider。不要将 API Key 提交到仓库。

### Minecraft / RCON

`mc` 和 `rcon` 均为可选配置。仓库中的 Minecraft 状态、玩家监控和 The Room 功能都是服务器特化示例；不使用时应在配置中留空，或移除相关插件。

### ComfyUI

`anima/` 不包含 ComfyUI 本体。若要使用绘图示例，请自行部署 ComfyUI，并配置服务 URL、工作流与输出目录。

## 配置与安全

- 从 `sample_config.json` 创建 `config.json`，不要提交真实配置。
- `sample_config.json` 中的 RCON、ComfyUI、Tournament 路径均为示例值，必须按自己的环境修改或移除。
- `data/` 下的数据库、用户数据、群消息、黑名单等为运行时数据，不应作为公共仓库的配置来源。
- 管理员 wxid 配置在 `llm.admin_wxids`；预置管理员命令说明见 [admin_commands.txt](admin_commands.txt)。
- 发布前请检查所有示例插件中是否有服务器地址、群名称、账号标识、密钥或本地路径。

## 开发文档

- [DEV_GUIDE.md](DEV_GUIDE.md)：架构、上下文格式、插件接口与开发建议
- [sample_config.json](sample_config.json)：配置字段示例
- [admin_commands.txt](admin_commands.txt)：预置示例管理员命令

## 致谢

本项目集成并依赖 WeFlow / WechatRobot 等开源组件。请在分发和部署时遵守各依赖项目的许可证与使用要求。
