# Python 沙盒、定时提醒、受限 RCON 与搬史插件

日期：2026-10-29。安装和验收已完成，生产激活等待用户手动重启。
未手动停止、重启、reload 主进程，也未向真实群发送消息或连接真实 RCON 执行验收。

## 安装结果

| 插件目录 / ID | 原生 Agent Tool | 状态 |
| --- | --- | --- |
| `plugins/restricted_python` / `wechatbot.restricted-python` | `restricted_python` | 新增，默认启用，受限 Python 子集，不是完整 CPython |
| `plugins/scheduled_reminder` / `wechatbot.scheduled-reminder` | `manage_scheduled_reminder` | 保留上一轮独立迁移，创建/查询/取消、持久化与到期主动提醒回归通过 |
| `plugins/restricted_rcon` / `wechatbot.restricted-rcon` | `minecraft_rcon_action` | 新增迁移，默认启用，仍须原 `rcon_tools` 开关和白名单允许 |
| `plugins/mai-repost-shit` / `github.erhan111119.mai-repost-shit` | `repost_shi`、`repost_pretty` | 安装上游1.2.2并完成微信适配，保留默认关闭、目标群为空 |

工具直接使用 MaiBot SDK 的 Tool 注册，Python/RCON/提醒为 `core_tool=True`。
不增加第二套 LLM/自主循环，也不要求启用原生 ACP Agent 服务。

## Python 限制

提交代码只进入白名单 AST 解释器，不调用 `eval/exec/compile`。
支持变量、条件、有限循环、列表推导、基础内置函数、限定字符串方法、math/json。
拒绝 import、函数/类定义、任意属性、文件、网络、系统命令、环境变量及 Bot 配置读取。

每次独立 `python -I -S` 子进程，移除继承的 API 环境变量；Windows Job Object
限制128MB内存、2秒CPU和单进程，另有3秒墙钟超时、50000步、2000个AST节点、
4096位整数、10000项容器/字符串上限、8000字符输出，以及单份代码并发限制。
先设置 Job 限制再传入代码，设置失败即拒绝运行。仅支持当前 Windows 部署。

## RCON 权限与旧配置

复用 `llm/rcon_tools.py` 的19个动作及 `llm.config.get_llm_config()['minecraft_rcon']`，
凭据仅留在 Host，不放入插件配置或模型上下文。Host 新增窄 capability `wechatbot.minecraft_rcon`，
仅 `wechatbot.restricted-rcon` 可调用，并校验真实 Planner 消息对应的随机、60秒、单次授权。
Host 覆盖模型伪造授权；模型指定的用户/群/会话不作为权限依据。

现有白名单写的是群名称，Host 以真实消息的群名兼容，同时支持群ID精确匹配；
空名单不放行，未擅自换群、增加成员或改成通配。原配置上限较低时保留，每条请求最多2次。
不暴露旧壳子 `/rcon` 的任意命令执行功能。选择器仅玩家名或简单 `@a/@p/@s/@r/@e`，
不允许带条件选择器；增加控制字符、NaN/Infinity 拒绝及错误脱敏。

## 搬史含义、来源与微信适配

此处“搬屎/搬史”是把抽象、鬼畜、整活内容搬到群内分享。
官方社区索引 `Mai-with-u/plugin-repo` 收录的 [B站搬史机](https://github.com/erhan111119/mai-repost-shit)
正对应此意：按评论含 @ 的比例筛选视频，默认样本至少30条、含 @ 比例至少50%，再下载转发。
这是筛选启发式，不保证每条内容符合所有人的口味。

固定上游提交 `c0b9e0d06ee1ffc9331bf093469793536be0fcb1`，保留 GPL 许可证和上游文档。
补全已安装 httpx 的依赖声明。没有安装另一个 AstrBot 的同名插件。

原版面向 QQ 合并转发，当前 wxSend 不能表示该格式。本地适配为一次 `send.hybrid`：
标题/数据/链接文字 + 原生 `FileComponent` MP4，通过本机文件路径传递而非大体积 base64。
说明文字里的 @ 保持普通字符，不制造群友提醒，不添加 Reply 引用组件。
聊天流查询改为 wechat、用户白名单支持 wxid，Tool 使用 Host 覆盖注入的真实身份/会话；
点播后的回复抑制按原请求 message_id 生效，避免吞掉其他消息或插件的正常回复。
具体改动见插件 `WECHAT_PORT.md` 和 `docs/maibot-repost-wechat-20261029.patch`。

按之前“安装后暂时不要修改配置”的要求，未开启搬史、未指定目标群，也未改动已有插件配置。
运行时目录监视器随后自动生成了三个新插件的默认配置：Python/RCON为true，搬史为false、
target.groups仍为空；没有手工编辑这些配置，生产测试桩文件也未安装。
上游其他功能（包括二次元视频频道和口语命令）保留，启用前可查看原 README/模板。

## 验收证据与边界

- `agent-tools-unit-20261029.json`：安装目录上的24项专项测试全部通过，包含真实沙盒子进程、
  越权/资源滥用拒绝、RCON白名单与注入拒绝、单次授权/请求限额、组件重名检查、微信文件语义。
- `agent-tools-reminder-regression-20261029.json`：18项提醒专项回归全部通过。
- `agent-tools-bridge-20261029.json`：正式目录源码复制到独立实例，实际 Planner/SDK Runner，
  75项检查74项通过，内含53项收发/学习/身份单测与9项新增工具链路检查，新增项全部通过。
  唯一未通过项为原有 `native.tool.query_image_memory`：未配置 image_embedding 模型。

测试中的 LLM 为本地 HTTP 桩，B站搜索/评论/播放/下载为独立实例测试桩，RCON连接为测试桩，
微信物理发送被拦截。验证了原生调用、权限及消息载荷，不代表已完成真实外网视频或微信界面上传验收。
真实模型可用性/429配额、B站风控及服务器实际命令兼容性不由这些桩测试保证。
首次桥接试跑暴露测试桩在上一轮reply送达、最终stop完成之前切换情景的竞争；
已让测试等待前一轮结束，正式目录复跑仅剩上述已知配置缺项。

安装前后 `config.json`、bot/model TOML及已有插件配置 SHA256 一致。
源文件 SHA256 与上游提交记录在 `agent-tools-provenance-20261029.json`。
生产主进程仍为 PID15256、启动于10:26:08；目录监视器可自行记录变化，但新的Host授权和
工具上下文代码必须在用户手动重启后才完整生效。未主动热加载或重启生产进程。

生产日志补查：截至12:25，仍有上游429及 `PROMOTION_MODEL_LIMIT_REACHED` 配额耗尽，
包括12:24的失败记录；近期错误中未检出这几个新插件ID相关错误。
这不等同于新代码已在生产成功调用，实际模型恢复情况须在手动重启后另行观察。

所有 JSON 证据位于 `docs/maibot-wechat-acceptance/`。
