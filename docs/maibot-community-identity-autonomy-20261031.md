# 双端身份、状态与自主行为修复验收

未手动重启 bot 主进程，未修改 `/player`，未向真人发送测试禁言、撤回、扣分或聊天消息。插件源码由现有监听器重载；用户在 10 月 30 日 23:58 手动启动的新主进程是 PID 13748，后续主进程修改需在用户下次手动重启后完整加载。

## 绑定与人物记忆

原始数据库确认 QQ `2103203812` 在 22:54:55、微信 `wxid_0x1whr7w1tmv22` 在 22:55:06 都请求 `/bind oldicecube`，已到达 shell.bind 后返回“插件执行异常”。旧进程没有保存完整 traceback，不能事后断言唯一异常类型。当前 Router → Dispatcher → bind 完整双端入口与生产数据库副本均已通过测试。

移除绑定流程对可选印象函数的按名导入依赖，隔离已提交事务与可选昵称/印象展示，防止附加信息失败后把成功绑定误报为失败。SQLite 异常给出可重试提示；Dispatcher 增加完整异常日志。昵称/印象仅取稳定 ID 对应的原生昵称和现有缓存，没有资料时明确提示“暂无足够印象”。

按两条已验证的原始请求补办 `oldicecube` 的真实关联，保留原微信 canonical UID。结果与备份路径见 `binding-live-repair-20261031.json`。未虚构账号，未发送消息。两端积分/业务身份相同，两个原生人物 ID 及原始来源分别保存并双向检索。实库 70 条人物记录全部符合 `md5(platform + '_' + user_id)`；昵称不参与 ID 计算。

修复手动画像工具、兼容画像 API 仅查单端的遗漏。两份结构化画像分别压缩后加来源，避免标题覆盖和长本群画像挤掉关联身份说明。记忆查询保留原聊天来源，一端异常仍保留另一端成功结果。自动与手动路径都明确“同一人、不同平台经历、非本群即时对话”。人物修改操作不自动复制到另一账号。

## /sleep 跨端、跨群

睡眠状态按 canonical actor 持久化，不依赖发起群；所有启用 sleep 的准入群中，同一 wxid/QQ 号发出普通文本或媒体消息都会查询同一状态，回复当前发言群。系统通知和 bot 事件不冒充本人发言。

保留原规则：开始后 4 小时内发言失效并扣 10 分；4 小时后结算；超过 12 小时按 5 小时结算。新增原子开始/结算：状态转换与共享积分同一事务提交，防止双端并发消息重复奖惩，同睡眠时段只能建立一条记录。短期交互等待状态仍属于实际会话。

WeFlow 已核实目标为 **26果园T&M动漫社**，ID **47799335844@chatroom**。按实际名字与稳定 ID 配置仅启用 sleep 自动处理，未开启其他命令或 LLM；Router/Dispatcher 对稳定 ID 的准入/白名单同步支持，群改名后仍能匹配。配置备份：`data/backups/sleep-groups-20261030-230033`。

## 同名群跨端见闻

新增原生 SDK 插件 `plugins/community_context/`，仅连接微信 `47447788043@chatroom` 和 QQ `qq:3294652115:napcat-local:group:1020993873`，精确匹配群名 `GDUT SIE Minecraft Public Server`。核对来源账号、scope 与原生回复频率；歧义、私聊、错误路由、关闭回复的群均不提供参考。

Planner 使用独立参考 item，Replyer 使用 extra_prompt，旁群消息不混入本群即时历史，也不暴露可用于 @、撤回、禁言的远端定位 ID。`peer_group_context` 可按需查关联记忆；只读 `community_context.reference` 为自主生活提供相同参考。最多 12 小时、8 条、2400 字，缓存 20 分钟。插件不独立广播、不新建 LLM 调度。

## 自主行为根因与修复

10 月 30 日中午旧 12 次行动额度已耗尽，其中 11 次只是私人 think、1 次失败，后续探索/社交/搬运被挡。旧日志不删，兼容计数时排除 think；今后单独记 thought。真实动作、模型调用和每日全局 4 次主动分享限额继续有效。social 意图仍须同群真实证据和统一分享限额。

模型决定不搬不再扣行动/分享额度，同一机会去重；真实发送仍先持久化预留，未知送达不重试。实际项目成果在后续总结 API 失败时保留；若已选择分享则保留进展候选，恢复后处理，不虚构进度。一次性启动引导标记不重置。

日程原来关闭，已启用生成/注入/缺失补生成，保持独立日程分享定时器关闭。冷场阈值从两小时调整为 40 分钟，保留静默与退避。自主生活、搬运、冷场、日程补齐指定 QQ 群，补上 scoped QQ 查询需要的 chat.get_all_streams 能力。热点不独立广播，作为统一协调器的数据源。

线上仍出现过 429、90 秒超时和 QQ 图片源 400，不能伪称上游已全部恢复。结构/额度修复不保证免费服务持续可用。只读 WebUI 检查确认相关五个插件已加载。

## QQ 管理能力

指定 QQ 群已启用 mute / recall_message，模型可自行决定目标和禁言时长，执行层限制 60–2592000 秒，校验机器人实际群管理员、群白名单及被禁言者保护/管理员身份。撤回自己消息通常受 QQ 两分钟窗口限制；撤回他人消息需群管理权限，最终由 OneBot/QQ 校验。微信不支持这些控制并丢弃误输出。未对真人实际测试管理操作。

## 搬运插件调查（未安装）

- [cateye_mai_forward](https://github.com/cateyemizuki/cateye_mai_forward)：QQ 原消息打包、收藏、复读和分享，最接近聊天记录/图片/视频搬运。NapCat 直转优先保留媒体；宿主回退时语音/视频/文件可能仅保留摘要。直连需接入统一额度和来源权限，工具名 forward_messages 与现有适配器重叠。微信不能直接接收 QQ 合并记录，需图片/文件降级。
- [maibot-tieba-share](https://github.com/mizuku003/maibot-tieba-share)：专门搬贴吧帖子、长图、视频，带 /搬石、/搬视频、筛选、去重、白名单、静默和抓取退避。声明兼容范围包含本机 1.3.15；依赖 httpx 与 html2png。后续接入应关闭独立定时广播，使用统一协调器，微信发送图/文件，QQ 可原生转发。此处是代码/声明兼容性调查，未宣称实际媒体链路验收通过。
- [ForwardMessagesAuto](https://github.com/papaya0481/maibot-ForwardMessagesAuto-plugin)：偏向收到 QQ 合并聊天记录后的自动分享机会，不是所有普通媒体的采集器；作者只验证 SnowLuma，本机组合仍须媒体验收。

## 文件与测试

实现：`utils/account_identity.py`、`plugins/bind.py`、`src/services/memory_service.py`、`src/maisaka/memory/person_profile.py`、`src/maisaka/builtin_tool/query_person_profile.py`、`auto/sleep.py`、`utils/sqlite_store.py`、`core/router.py`、`core/dispatcher.py`、`plugins/community_context/`、`plugins/autonomous_life/{plugin.py,store.py,_manifest.json,config.toml}`，以及 idle/mai-life/repost 配置、能力清单、`config.json` 和 `DEV_GUIDE.md`。

专项：绑定入口 7、联合身份 10、sleep 8、旁群参考 11；既有身份/积分 17、身份/路由 19、双端插件矩阵 119、自主生活 67、项目引导/权限/恢复 57、原生桥接 125 项（含 65 项消息转换单元测试）均通过。原生桥接最终报告：`temp/maibot-bridge-acceptance-/20261031-003245/acceptance.json`。其余报告位于 `docs/maibot-wechat-acceptance/`；精确路径和状态另记本次汇总 JSON。

测试使用隔离实例、固定 API、真实 AppContainer 和拦截发送。没有把隔离测试称为真人群聊的物理送达验收；原生主进程文件的最后修改仍需由用户按原要求手动重启生效。
