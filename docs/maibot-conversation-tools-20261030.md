# 会话 Agent Tools 与联网查证验收（2026-10-30）

## 实际登记与适用范围

通过当前 bot 的 `http://127.0.0.1:8001/api/webui/plugins/installed` 和 `/api/webui/plugins/runtime/plugins/{plugin_id}/components` 只读查询，核验插件加载、启用情况；同时检查 SDK 声明、核心工具目录、Planner 可见性筛选和 Host 请求上下文注入。原始快照：`temp/agent-tools-installed.json`、`temp/agent-tools-running.json`、`temp/agent-tools-declarations.json`。

本次完成后，插件层登记启用15项 Tool，其中14项为普通/微信会话工具，另1项仅查询 QQ 消息身份。登记启用不代表任意群、任意用户可执行：现有平台、群白名单、真实消息、操作者和功能开关仍会在执行时核验。默认可见工具直接进入 Planner；延迟工具先通过 `tool_search` 发现。

| 工具名 | 功能 | 默认发现方式 / 边界 |
| --- | --- | --- |
| `internet_search` | 公开网络搜索，返回标题、网址、摘要 | 本次新增，直接可见 |
| `read_webpage` | 读取公开 HTTPS 正文、文档、JSON，按 offset 继续阅读 | 本次新增，直接可见 |
| `owned_plugins` | 列出/调用自己拥有的壳子与原生插件，读取结果 | 直接可见，普通 bot 身份，继承原权限；壳子32命令、7自动化为管理目录，非39个无条件可执行工具 |
| `restricted_python` | 数学、JSON、文本等受限 Python 子集 | 直接可见，无文件/网络/导入权限 |
| `sandbox_program` | Windows AppContainer 内运行标准库小程序、导出小成果 | 延迟发现，无网络/宿主配置权限 |
| `minecraft_rcon_action` | Minecraft 查询和批准的轻量操作 | 直接可见，群白名单、枚举动作，非任意 RCON |
| `manage_scheduled_reminder` | 当前会话请求者创建、查询、取消单次提醒 | 直接可见，明确用户请求、持久化 |
| `mai_life_query` / `mai_life_modify` | 查询/调整生活日程与当前对话的定时任务 | 直接可见，保留插件自身作用域和校验 |
| `repost_shi` / `repost_pretty` | B站整活/二次元视频检索、搬运 | 声明可见，保留微信上下文、群配置和配额；不额外启用独立主动调度 |
| `get_hot_topics` | 当前配置来源的热榜与热点 | 延迟发现；当前主要启用B站、抖音，不能把插件支持的所有来源说成已开启 |
| `autonomous_life_status` | 查询兴趣、实际经历、未完成项目 | 延迟发现，自主生活允许的真实群 |
| `autonomous_project_control` | 暂停/恢复自己的项目 | 延迟发现，固定操作者真实消息校验，不授予新权限 |
| `find_user_qq_id` | 按 QQ 消息 ID 查发送者身份 | 仅 QQ 消息；当前 QQ 通道尚未正常连接，不能当成微信认人工具 |

当前还登记了 `open_private_chat`、`get_qq_by_msg_id`、`send_poke`、`recall_message`、`mute`、`react_emoji`、`list_chat_streams`、`forward_messages`，运行时均为禁用。未将它们算作当前可用工具。

核心另外提供 `reply`、`wait`、`stop`、`query_memory`、`query_image_memory`、`query_person_profile`、`show_emoji_list`、`tool_search`、`subagent`、`subagent_status`、`subagent_stop`，以及延迟发现的 `view_forward_message`。当前记忆/图像记忆、子代理开关开启；表情包使用 `show_emoji_list` 新发送逻辑，因此不是旧 `send_emoji` 工具。`tool_search` 搜索的是已安装工具目录，并非搜索互联网。

源码自带 `web_search` / `web_fetch` Provider，但 `model_config.toml` 没有选择联网服务，因此原先不对会话开放；`read_tool_result` 在当前关闭结果压缩、未选择原生联网服务的配置下也不开放。此次新增工具采用独立名称和网页 offset，不依赖这个未启用的功能，不需要新填 API key 或改模型池。

## 实现

- `plugins/autonomous_life/plugin.py`：增加 `ResearchSection` 原生配置 Schema、两个 `core_tool=True` 会话工具、关闭时的研究工作收尾。保留原生人格、Planner、Replyer 和自主调度。
- `plugins/autonomous_life/research.py`：会话上下文核验、查询预算、并发控制、短缓存、外部证据标记、检索时间和失败语义。真实请求由原有 Host 注入 `_request_context`，模型不能指定来源平台/群/消息身份。
- `plugins/autonomous_life/web.py`：复用已有 Bing RSS 与公开 HTTPS 读取；加总预算、分块限长、正文提取、标题/链接、Unicode URL 编码、分页、最终来源网址、搜索结果字段限长和格式过滤。
- `plugins/autonomous_life/config.toml`：启用 `[research]` 默认推荐值：每消息6次、等待20秒、正文10000字、同会话缓存120秒。
- `plugins/autonomous_life/_manifest.json`：版本1.3.0、更新功能描述，无新增依赖/Host 权限。
- `plugins/autonomous_life/README.md`：补充配置、使用、边界和验收入口。
- `scripts/maibot_research_acceptance.py`：网络读取、访问边界、SDK 注册、配置与预算测试及可选实网验收。
- `scripts/maibot_bridge_acceptance.py`、`scripts/fixtures/maibot_research_fixture.py`：隔离 Runner 网络夹具与搜索→阅读→原生回复链路验收。修复隔离目录中已复制 owned 插件后重复拷贝冲突，不改生产目录。

工具说明要求：不确定知识、陌生概念、近期事实、版本、新闻和需要核实时，先查证再决策/回答；搜索摘要不足时读取一手来源，核对日期；失败或资料不充分时承认不确定。该要求通过工具声明进入 Planner，不修改麦麦原生预设、身份或回复风格，也不强制日常闲聊都联网。是否正确选择查询及最终事实判断仍依赖当前模型能力。

复用原探索实现的优点是自主生活和会话查询共用网页访问规则，没有新增定时器、重复主动调度或额外模型请求。调研了支持多引擎及 MCP 的 [DDGS 官方仓库](https://github.com/deedy5/ddgs)；当前环境 DuckDuckGo HTML 试读超时，现有 Bing RSS 可用，故保留已有可用通道，未增加新依赖。

## 限制与保护

公开 HTTPS/443 GET，禁止认证信息、内网/保留/回环地址；每次重定向重新解析和核验全部地址，并固定连接到已验证 IP，保留 TLS SNI/证书验证。无环境代理、Cookies、配置/群聊自动附带、页面脚本执行、登录、表单提交或 Python 沙盒网络授权。最多2次重定向、512KiB原始响应；页面截断会标记，不伪装完整读取。

会话/平台隔离缓存最多64项，消息预算最多记录1000项、超过1小时可重新计数；实际查询最多2并发。等待超时不会释放尚在运行的网络/DNS工作占位，防止重复调用堆积线程。正常网络共用16秒预算，工具等待默认20秒；底层系统DNS无法由线程强制取消，但仍占并发槽。缓存命中不占查询次数。并发/失败只返回明确失败，不反复立即重试，不阻塞其他普通聊天工作。

动态渲染、登录或反爬页面可能无法读取；搜索相关性和更新速度由搜索源决定，搜索摘要和检索时间都不能当作完整正文/发布日期。当前未配置付费或鉴权搜索源。

## 实测结果

- `python scripts/maibot_research_acceptance.py --live`：**19项测试通过**；英文 `asyncio TaskGroup`、中文 `我的世界` 均实际返回来源结果；实际读取 Python 官方 `asyncio-task.html`，取得45351字正文、标题和 `next_offset=10000`。报告 `temp/maibot-research-acceptance.json`。
- `python scripts/maibot_bridge_acceptance.py --plugin-source plugins --test-web-research`：**92项验收通过**（其中原有翻译/发送单元套件65项，作为一项套件检查汇总）；新增7项联网链路检查通过：默认直接可见、先查证指导进入实际提示词、真实 SDK Runner 搜索结果进入 Planner、正文结果进入 Planner、外部资料标记、读完后原生回复、Replyer 收到查证资料。LLM 和网页由确定性夹具模拟，微信出口拦截，未向真实群发送测试消息。报告 `temp/maibot-bridge-acceptance-/20261030-170958/acceptance.json`。
- `python scripts/maibot_autonomous_life_acceptance.py`：**65项检查通过**，包括现有自主生活预算、项目/兴趣持久化、统一分享限额、实际 AppContainer 及人格/原生风格回归。报告 `docs/maibot-wechat-acceptance/autonomous-life-20261029.json`（脚本沿用原报告名）。
- 当前生产 bot **PID17104、12:47:47启动**未手动重启。8001监听归属同一进程，插件 Runner 为其子进程；已有运行时自动热加载后，WebUI运行时组件接口已返回两个新工具 `enabled=true`，插件加载状态为success。未来手动重启也会照常加载，无需给 bot 额外发送初始化提示词。

验收覆盖可见性、工具返回和消息出口的协议链路；没有把确定性 LLM 夹具测试当成当前在线模型必然选择联网、对所有网站都可读或实群物理投递成功的证明。QQ 未连接与其适配器重连告警仍属于现有状态，本次未改其配置。
