# Wechat Bot 本地适配说明

上游：https://github.com/erhan111119/mai-repost-shit

版本 1.2.2，固定提交 `c0b9e0d06ee1ffc9331bf093469793536be0fcb1`，GPL-v3.0-or-later。
来自 Mai-with-u/plugin-repo 的条目 `github.erhan111119.mai-repost-shit`。
保留上游 LICENSE、源文件和说明；本文件记录本地改动，更新上游时应重新合并。

“搬屎/搬史”在该插件中指寻找抽象、整活视频转发进群。
它按 B 站评论含 @ 的比例筛选候选：默认至少 30 条评论、含 @ 比例至少 50%；
按配置检索、判定、去重、下载，提供 `repost_shi` 和上游的 `repost_pretty` 工具、命令及定时巡检。
这是一种筛选信号，不保证内容质量。

本地改动：

- 查询聊天流使用 `wechat`，用户白名单支持 `wxid_...`。
- 工具采用当前 Planner 请求由 Host 覆盖注入的身份/会话，避免最近发言者缓存错位，
  也不采用模型指定的 stream_id。保留缓存供诊断及旧命令路径使用。
- wxSend 无法发送 QQ 合并转发：改为一次 SDK `send.hybrid`，包含标题/链接文本及原生 MP4 文件。
  文字中的 @ 保持文字，不自动转换成群友提醒；不添加引用格式。视频通过本地文件路径传递，
  避免大体积 base64 超过 Runner RPC 帧限制。需 Host/Runner 共享本机文件系统。
- 点播后的回复抑制只针对原请求 message_id，不吞掉另一个群友或另一个插件的回复。
- 补全 httpx Python 包依赖和 send.hybrid capability 声明。

**未改上游配置默认值**：`plugin.enabled=false`，`target.groups=[]`。
已安装不代表已开始自动搬运；此次未替用户开启或指定群。默认开启的原生工具可能被首次注册，
调用会明确返回功能未开。后续启动按 Host 的 enabled 开关跳过已禁用插件。
开启后定时每天 3 次；上游还有二次元视频频道、口语命令和权限/最小间隔配置，详见 README 和模板。

完整集成测试仅在独立实例开启插件，B 站 API、视频内容和 RCON 使用测试桩，
微信物理发送被拦截。没有向真实群搬运视频，外网 B 站接口可能仍受风控影响。
