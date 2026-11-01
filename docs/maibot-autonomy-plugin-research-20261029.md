# MaiBot 自主生活、热点与偶发“发癫”插件研究

研究日期：2026-10-29。对象：本仓库 Wechat Bot 壳子内运行的 MaiBot 1.3.15。结论依据为上游仓库、下载的 README/manifest/部分实现、本地 Host 源码及只读接口探测。

本次检查了 22 份插件 manifest，没有安装或启用生产插件，没有向群聊发送测试消息。Manifest 通过仅表示静态声明通过；没有执行第三方插件导入、定时任务或微信端到端验收。

## 推荐组合

第一阶段采用 **tsuiraku9/mai-life + hot-topics 的微信适配版 + idle_proactive_chat + 夺舍麦麦的微信适配版**。生活日程只保留一个主管；热点、静默与临时情绪产生意图，由原有 Maisaka Planner 决定是否表达。第二阶段添加日记、关系状态或写作兴趣。

| 插件及研究时版本 | 对应需求 | 本机接入判断 |
|---|---|---|
| [tsuiraku9/mai-life](https://github.com/tsuiraku9/mai-life) 1.3.0 | 每日日程、当前在做什么、分享、一段时间后继续聊天、提醒等一次性自主任务 | 首选生活主管。manifest 覆盖 Host 1.3.15 / SDK 2.7.1；聊天流设计支持平台标识。仍须验证微信真实 session、任务去重和实际投递。 |
| [ZhaoCang-QWQ/hot-topics](https://github.com/ZhaoCang-QWQ/hot-topics) 0.7.6 | 热榜、兴趣过滤、随机上网、静默注入、热点分享、可选订阅搜索 | 功能合适，但自主目标解析硬编码 QQ，需要修改后接微信。普通热榜不要求 DeepSeek 搜索密钥；订阅搜索是另一项能力。 |
| [YukiSakiko/idle_proactive_chat](https://github.com/YukiSakiko/idle_proactive_chat) 1.0.5 | 群安静后主动找话题，无人回应则减少打扰 | 优先于直接另开 LLM 并自行发消息的插件。源码传递 platform，使用真实 session；默认 2 小时静默、指数退避、夜间免打扰。 |
| [cateyemizuki/cateye_mai_personality_swap](https://github.com/cateyemizuki/cateye_mai_personality_swap) 1.4.7，夺舍麦麦 | 条件/概率/权重切换临时人格，让 Planner 和 Replyer 偶尔戏精、抽风、假装失控 | 最贴近新增需求。支持按流、限时恢复与脚本，但微信管理员鉴权和 bot 身份识别需适配；不自带“疯狂 @”执行工具。 |
| [orge-8/better-diary](https://github.com/orge-8/better-diary) 1.7.1 | 从真实聊天整理经历，形成日记、反思和连续性 | SDK 最低 2.8.0，高于现装 2.7.1；目标与过滤需要微信适配。先关闭 QQ 空间发布，并将日记摘要真正接入生活决策上下文。 |
| [ZhaoCang-QWQ/social-state](https://github.com/ZhaoCang-QWQ/social-state) 0.4.7 | 关系、情绪、未完话题、社交记忆 | 可做状态输入。自身主动中心主要面向私聊，不能称为现成群聊点名引擎；不要与其他关系系统重复安装。 |
| [yufei-pan/maibook-plugin](https://github.com/yufei-pan/maibook-plugin) 0.1.6 | 写小说、维护笔记、后台写作完成后回来分享，形成实际爱好 | SDK 最低 2.8.1、最高 2.16.99。适合作为“找事做”；需限制写作 token、任务数量，并验收微信长文/图片发送。 |
| [NetaJuutilainen/Maibot_fun_pack_plugin](https://github.com/NetaJuutilainen/Maibot_fun_pack_plugin) 1.6.4 | 喜报、悲报、锦旗、答案之书、今天吃什么、早晚安 | 适合作为玩梗道具。上游 LLM 工具说明偏向用户明确要求时调用；主动玩梗须单独调整工具描述和策略，不能装上就宣称会自主整活。SDK 上限 2.16.99。 |

有真实阅读、写作、互动和可回忆的经历，再用作息调整节奏，会比只生成“刚吃饭、刚睡醒”更有连续性。生活模拟状态应与实际执行证据分开存储，避免把计划当成已经做过的事。

## “偶尔发癫”的落地方式

夺舍麦麦已经具有临时人格、概率条件、按聊天流作用域、`duration_minutes` 到期恢复。其 `maisaka.planner.before_request` 与 `maisaka.replyer.before_model_request` 在本地 Host 有对应入口，但请求内容改写仍需回放测试，不能以 Hook 同名代替行为验收。

建议三个临时预设：

- **戏精失控**：围绕眼前群聊突然上演短剧，例如“警报，我的摸鱼模块接管大脑了”，结束后自然恢复。
- **热梗上头**：读到实际热点后产生夸张联想、梗图或喜报，保留来源，避免把表演当作真实故障。
- **点名拉人玩**：从当前群中最近活跃、身份能准确解析的成员里选人，点名接梗、接龙或小投票。

这些是拟议配置/扩展，并非上游已实现的整套功能。推荐实现一个小型行为 Tool，让核心自行选择 `normal / playful / dramatic` 等状态，记录原因、目标 session、成员 ID、截止时间和剩余动作数。候选情绪可以随机出现，但是否行动仍由 Planner 根据群聊上下文决定；不要让概率定时器直接绕过核心连续发消息。

建议首轮参数（需要适配层实现统一预算，不能假定每个插件原生都有这些配置）：

- 在符合条件的活跃对话决策中，约 1%–3% 概率产生一次表演候选，不对每条消息重复抽签。
- 一次表演持续 1–3 分钟、最多 2–4 条消息，总计点名 1–3 名成员；同一成员在一次表演中至多点名一次。允许戏剧化连续短句，次数由执行状态保证。
- 表演结束冷却 6 小时、每天最多一次；首轮仅开放当前指定群，保留停止口令。持续无人回应、群友明确要求停止或模型/API 失败时结束。
- 全部主动功能共享群级总预算，起步可设每日 2–4 次主动发起、一般间隔 60–120 分钟。表演里的多条消息计入单独动作上限，不能被多个插件分别绕过。

### 微信适配点

1. **管理员**：`cateye_mai_personality_swap/admin_util.py:plain_id()` 最终要求 `isdigit()`，会拒绝 `wxid_...`。应保留 `(platform, user_id)` 的身份语义，按微信管理员精确鉴权。不要用“把整个群设为管理员”代替修复；该选项会让群内所有成员获得管理权。
2. **机器人身份**：上游读取 `bot.qq_account`，微信端须使用已绑定的真实 bot user ID，避免脚本把机器人自己的消息当成新诱因而循环触发。
3. **群隔离**：上游状态键可能落在裸 group_id/user_id。适配时优先用真实 session 或带平台的类型化标识，防跨平台串人格。
4. **真 @**：行为 Tool 必须使用当前群内的真实成员 ID，形成核心 `AtComponent(target_user_id=wxid)`，与 `TextComponent` 保持正确顺序，再走 `WechatShellDriver` 的 native segments。单纯输出 `@昵称` 不等于微信通知；不向文本塞 QQ CQ 码。
5. **发送语义**：目标群 ID、session ID、成员 wxid、展示昵称分别承载各自功能。发送前重新校验成员归属和改名，重名不能随机选人。当前壳子明确不支持 `@all`；表演设计采用具体成员。
6. **恢复**：所有临时状态必须有绝对截止时间。重启后过期状态立即恢复，清空未发送动作；停止口令也必须取消已排队动作，不能只恢复文风。

人格预设结构示例，需在微信适配和回放后使用；它仅定义风格，不授予发送次数和 @ 能力：

```toml
name = "戏精失控"
persona_name = ""
expression = """短暂戏精上身，围绕当前话题说夸张但能接得上梗的短句，偶尔自嘲。"""
persona = """保留原有身份和关系记忆；把这次变化当作短暂群聊表演。"""
behavior = """结合当前群气氛决定是否接梗或邀请群友参与。点名必须调用结构化行为工具，使用真实群成员 ID；剩余预算为零、无人回应或收到停止要求时结束。"""
duration_minutes = 2
```

适配后的管理员可先手动回放 `/mps swap 戏精失控 2`、`/mps status`、`/mps swap`，再开放核心自主触发。以上命令来自上游，用在当前未适配微信环境时不能保证鉴权通过。

## 当前安装前置问题

- 本地 Host 是 **1.3.15**，实际可导入 SDK 为 **2.7.1**，而仓库 `pyproject.toml` 要求 **>=2.16.0**。这是现有依赖偏差。应先在隔离环境用同一 Host 对齐并验证 SDK，建议候选基线 **2.16.0**；不能直接升级 latest 并宣称全部兼容。
- 某些插件仅 SDK 越界时本机校验器会警告放行。**放行不是功能兼容证明**。例如 maibook / fun-pack 声明 SDK 上限 2.16.99；life-frequency 上限 2.13.99，不能同时宣称满足仓库 >=2.16 与它的受支持范围。
- 当前内嵌运行路径已有 Plugin Host/Runner 和原生发信链路，具备接插件的基础。独立核心 WebUI 没有随壳子启动，本地也没有完整的外壳 `webui/`；不能照上游 README 直接假定现有网页里有插件商店。
- 不要另起 `bot.py` 和现有 `main.py` 并行运行来获得 WebUI；这会产生第二套核心。沿用当前 `start.bat` 托管进程，或另行把核心 WebUI 生命周期集成进壳子。
- 现有模型限流/额度错误会影响主动任务。应先保证 Planner 与辅助模型可用，再增加日程、日记和写作调用量；主动性插件无法修复 429。
- 核心 `model_task_config.web_search` 没有配置搜索模型，当前 Host 没有 `web.search` / `web.fetch` capability 注册；MCP 开关开着也不等于已有搜索服务。热榜公共接口能单独工作，不能据此宣称通用联网搜索已打通。

## 安装路径与顺序

官方插件采用 [Host/Runner IPC 体系](https://docs.maibot.chat/plugin/)。本项目的 MaiBot 第三方插件扫描根是 `D:\Wechat Bot\plugins`，新插件须放**各自的子目录**，包含完整源码和 `_manifest.json`、`plugin.py`。同根下现有扁平 `.py` 多为外部 Wechat Bot 插件，不是同一套接口。

```text
D:\Wechat Bot\plugins\
  mai-life\_manifest.json + plugin.py + 其余上游模块
  hot-topics\_manifest.json + plugin.py + 其余模块/微信适配
  idle_proactive_chat\_manifest.json + plugin.py + 其余模块
  cateye_mai_personality_swap\_manifest.json + plugin.py + 其余模块/微信适配
```

通用顺序：备份当前配置与插件数据 → 在副本环境对齐 SDK → 下载完整仓库并固定提交 → 完成微信身份/发送/黑名单适配 → 以禁用主动发送的配置启动、生成 `config.toml` → 配置真实管理员和单群白名单 → 通过下方验收 → 由现有启动脚本托管重启并逐个开放。不要只拷贝此次研究缓存中的部分文件；它不是完整安装包。

当前核心按 manifest 插件 ID 为原生插件提供 `data/plugins/<id>` 数据空间；具体插件可能另有状态文件，备份须结合其实现。Runner 生成的插件 `config.toml` 位于插件目录，应合并配置，不要用示例覆盖整份配置。

首选源码固定记录：

| 插件 | 研究时提交 |
|---|---|
| tsuiraku9/mai-life | `64fbd743c8e8c02102323da78bcc66b680124d27` |
| hot-topics | `ed3807f521eefd881b52fc4b8c2e2237d65d8001` |
| idle_proactive_chat | `3d16ebeba44419d9816629c32a798223706400f1` |

候选 mai-life 配置片段：

```toml
[plugin]
enabled = true
admin_user_ids = ["wechat:<真实管理员wxid>"]

[schedule]
allowed_streams = ["wechat:group:47447788043@chatroom"]
stream_discovery_platform = "wechat"

[[share.stream_profiles]]
stream = "wechat:group:47447788043@chatroom"
count_min = 1
count_max = 2
extra_prompt = "只在有具体内容且适合当前群聊时分享；热点保留来源，未完成的计划不能描述为已经发生。"
```

群格式需由该固定版本的解析器在副本环境确认，绑定既有真实 session 后才能开启。静默插件的目标项为：

```toml
[targets]
target_chats = ["wechat:group:47447788043@chatroom"]
```

`hot-topics` 微信修改位置：`plugin.py` 的 `_resolve_target_streams()` 中 `get_all_streams("qq")`，群/私聊分支和主动发起分支没有显式传 platform 的查询，及部分响应读取遗漏 `session_id`。必须统一成配置平台与真实 session，不能只把 QQ 群号替换成微信群号。

本次只读探测了该插件使用的 B 站热榜路径 `/v2/bili`：`60s.crystelf.top` 返回 HTTP 200/50 条，`60s.7se.cn` 返回 HTTP 200/100 条，`60s.viki.moe` 返回 HTTP 500。仅代表探测当时状态；接入需超时、故障切换、缓存与去重。订阅搜索如启用，另行配置上游要求的正式 DeepSeek 搜索能力，普通兼容 API key 不保证可用。

## 其他候选与暂不直接安装项

| 项目 | 结论与理由 |
|---|---|
| [xuqian13/autonomous_planning_plugin](https://github.com/xuqian13/autonomous_planning_plugin) 4.4.6 | 可替代首选生活主管；支持每日活动和主动意图。选择它就不再并行启用 mai-life 的日程主管。`yumuq` 分支 4.4.5 的 Host 上限为 1.0.0，本机拒绝；别混淆同 ID 的分支。 |
| [orge-8/life-frequency](https://github.com/orge-8/life-frequency) 1.23.4 | 体力、睡眠、无聊、走神等更丰富，但 SDK 上限 2.13.99 与仓库要求冲突。适合作为后续迁移研究，先 dry_run，不直接接管频率。 |
| [octmicy/Mai_life](https://github.com/octmicy/Mai_life) 1.24.4 | 需要 Host >=1.3.16，本机 1.3.15 被拒绝；还依赖当前未注册的 web 能力和若干 QQ 行为。与 tsuiraku9 的 mai-life 不是同一项目。 |
| [SkonJIYHD/LifeSimulationPlugin](https://github.com/SkonJIYHD/LifeSimulationPlugin) 2.0.0 | manifest 虽通过，却声明宿主未注册的 `send_message`，源码使用的 `send.text` 等能力未正确声明，另有相对数据路径/导入问题；需修复后再评估。 |
| [NothingButSeaweedSoup/lonely-mai](https://github.com/NothingButSeaweedSoup/lonely-mai)、[yhny1001/proactive_talk_plugin](https://github.com/yhny1001/proactive_talk_plugin) | 研究版本仍为 manifest v1，本机 v2 loader 拒绝。另有 [lonely-mai-sdkv2](https://github.com/clfr41/lonely-mai-sdkv2) 分支可解析，但更倾向直接生成发送，优先选现有核心统一决策的 idle_proactive_chat。 |
| [private-humanizer](https://github.com/DeepSeek-V4-Pro/private-humanizer) | 主打私聊拟人，不能代替群聊自主行为。 |
| [maibot-affection-system](https://github.com/feiyunming/maibot-affection-system) | 可选好感度；不要与 social-state 重复堆叠。逐消息评估增加模型请求。 |
| [community.engagement-plus](https://github.com/MCYXG233/community.engagement-plus) | 多种群互动与人格命令的合集；静态可解析，但本次未做完整运行兼容审查，不能当作现成自主发癫引擎。 |
| [bilibili_autopilot](https://github.com/namchengwuqiu/bilibili_autopilot) 0.8.0 | 可形成真实浏览兴趣、阅读摘要、等待反馈；SDK >=2.12，需登录和单独平台适配验收。先预览/阅读，上游默认关闭公开发布。字幕/摘要读取不等于真的播放看完视频。 |
| [maibot-bililife](https://github.com/ANTINOVIA/maibot-bililife) 0.2.6 | 更重的视频理解和记忆路线，SDK 声明精确 2.16.0；需要额外账号、模型/ASR等条件。 |
| [maibot-plugin-douyin-surf](https://github.com/mailchunian8395-arch/maibot-plugin-douyin-surf) | 需要浏览器及媒体依赖、登录，视频分享偏 QQ；本微信壳子当前不能直接保证原生视频投递。 |
| [MaiTrace](https://github.com/Rabbit-Jia-Er/MaiTrace) | QQ 空间能力不能当成微信朋友圈能力。 |

## 学习黑名单与验收要求

当前“爱吃白饭的大肥鱼”的学习黑名单为微信群 `47447788043@chatroom` 内用户 `wxid_j4m6tul1dm7822`，由 `src/common/learning_policy.py` 支撑原生学习入口。**第三方的日记、关系、每日计划和语录独立存储不会自动继承**。需在插件历史查询和消息 Hook 上统一应用学习策略；黑名单用户在普通聊天里是否参与互动与能否被学习是两回事，不应擅自当成禁言或禁止 @。

安装后的必测项目：

1. **装载**：完整插件导入、能力授权、SDK RPC、定时器启动/卸载；同名 ID 不重复注册。
2. **消息字段**：微信 platform、account、群 ID、成员 ID、真实 session、消息 ID 不混用；文本/At/Image/Reply 的顺序与语义准确。
3. **点名**：改名、重名、退群、缓存过期、多个 AtComponent、混排文本；不存在成员、跨群成员、私聊 @ 均不误发。端到端需确认微信实际出现点名通知，而不只是发送请求成功。
4. **决策**：核心能拒绝无意义热点、静默意图和表演；同一热点不重复，多插件同时唤醒只发一次。`proactive.trigger` 返回成功只代表入队，不代表最终发送成功。
5. **人格**：普通群与目标群隔离；Planner/Replyer 都生效；超时、手动停止、重启后的过期恢复；不改变永久主人格配置。
6. **预算**：每日总量、表演动作数、每人点名数、冷却、安静时段、无人回应退避；模型 429/超时不导致重试刷屏。
7. **记忆**：日记与计划有真实来源；学习黑名单不进入持久关系/日记/语录，不通过摘要二次漏入；其他群不被误伤。
8. **投递**：收到任务、核心决策、调用工具、平台投递、实际微信展示逐段留痕；失败有明确状态，不把“已排队”记作“已说过”。

以上是安装后的验收清单，**本次研究未执行这些生产端到端测试**。

研究机器可读证据见同目录 `maibot-autonomy-plugin-evidence-20261029.json`；缓存位于忽略目录 `temp/maibot-plugin-research-20261029`。上游可能更新，正式安装应固定提交并重新审查。
