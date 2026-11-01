# 群聊跟随搬屎与贴吧插件调研（2026-10-29）

## 已接入的行为

当前 `plugins/mai-repost-shit/config.toml` 使用 `target.mode="planner"`。原每天 6 次的随机计划不再直接搬运；每分钟从原生历史中读取目标群最近 10 分钟最多 200 条消息，不携带图片二进制，且过滤自身发言。这样外壳已经执行 B 站解析、仅观察入库的消息也能被看到。

默认机会门槛：至少 2 位真实群友、6 条消息、2 条图像/表情/视频/B站链接/BV号/搬屎话题信号，且最新分享不早于 120 秒前。图片只算“分享信号”，是否抽象整活由核心结合图片理解和对话判断；意图明确要求不把工作截图、普通照片当成发屎。普通活跃至少间隔 600 秒；达到 20 条消息或 4 条分享时，机会间隔与搬屎工具冷却可缩短至 180 秒。每次最多搬 1 条；二创频道点播仍为 600 秒。

机会通过声明过的 `maisaka.proactive.trigger` 进入真实核心，planner 结合近期上下文自行选择跟玩、聊天或保持安静。统计只包含计数，不把群友的任意文本拼成系统指令。工具说明已补充“可以自主跟随群友分享”，避免只有明确点播时才调用。

活动时间沿用 10:00–22:00；目标仍仅是主动回复开启的 `47447788043@chatroom`。消息计数去重并排除跨群、通知、旧消息、自己、标记为 bot 的用户和忽略名单；引用节点、旧转发节点及无法解析引用的文本预览不算新分享。最新决策与分享水位随现有状态一起持久化，避免重载和超时后的重复请求。保留原内容评论门槛、视频大小限制与发送成功后的 BVID 去重。

## 百度贴吧插件

找到 [mizuku003/maibot-tieba-share](https://github.com/mizuku003/maibot-tieba-share)，插件 ID `mizuku003.tieba-share`。只读拉取源码至 `temp/tieba-share-review-20261029/` 进行静态检查，未复制到生产插件目录、未启用、未执行其代码。

本次读取的 main 提交为 `6a962ccc35ca4fdb2f4729b9b1bdb98ea8889120`，清单版本 1.1.8。市场历史发布条目 [#909](https://github.com/Mai-with-u/plugin-repo/issues/909) 为 1.1.5，不能把该审核条目当作当前 main 的审核证明。

上游 [README](https://github.com/mizuku003/maibot-tieba-share/blob/6a962ccc35ca4fdb2f4729b9b1bdb98ea8889120/README.md) 与 [源码](https://github.com/mizuku003/maibot-tieba-share/blob/6a962ccc35ca4fdb2f4729b9b1bdb98ea8889120/plugin.py) 表明：

- 可指定贴吧、过滤回复/点赞数与标题词，保存已搬帖记录；支持整帖长图、楼中楼与视频。
- 提供 `tieba_share` 工具，允许模型选择吧、数量、图文或视频；也有 `/搬石`、`/搬视频` 和定时任务。
- 清单声明 httpx，整帖图片由宿主 `render.html2png` 渲染；上游说明为免登录公开页读取，遇验证暂停冷却。
- 自动目标枚举写死 `platform="qq"`；`_clean_id()` 会删除非数字字符，因此微信 `@chatroom` 与 wxid 必须先适配。
- 默认合并转发、QQ 自定义视频与当前微信的原生图片/文件能力不同，需改成普通图片/文件并验收大小、长图切片、路径与失败回执。
- 当前生产没有 `tieba_share` 同名工具；是否与自然语言入口、其他插件权限或渲染依赖产生运行冲突尚未验收。

现有热点插件虽有 `baidu_tieba` 话题榜源，但那是榜单标题/链接，不等于整帖搬运。建议如后续安装贴吧插件，先用图文长图、单帖、相同目标群、关闭独立定时，再将 `tieba_share` 作为 planner 的另一种跟玩工具。以上兼容性结论来自静态源码检查，未验证贴吧实时抓取、反爬恢复或真实微信投递。

## 验证范围

活动统计与调度测试：[25/25](maibot-wechat-acceptance/repost-planner-20261029.json)，覆盖密度、B站链接、原生视频与图片、跨群排除、bot 标记、旧引用、冷却、重载与 RPC 超时去重。另有配置路由 [22/22](maibot-wechat-acceptance/autonomy-routing-20261029.json)、工具契约 [24/24](maibot-wechat-acceptance/repost-planner-tools-20261029.json)、原生桥接 [78/78](maibot-wechat-acceptance/repost-planner-bridge-20261029.json)。

桥接新增验证真实核心接收主动任务、planner 提示包含该意图、工具经 Runner 后产出微信文字和 MP4 文件载荷；B站候选与聊天模型使用确定性 fixture，embedding 沿用真实端点，物理微信发送被拦截。没有用这些测试证明真实 LLM 一定会选择搬运。最初桥接 fixture 按每次搜索递增 BVID，导致多关键词搜索后的文件名断言不匹配；改为每次完成下载递增后，最终 78/78。fixture 只复制到隔离实例，没有安装进生产插件。

隔离测试不向实际微信群发消息，不触碰生产运行状态，也没有手动重启 Bot。改动保存后，完整加载仍由用户手动重启。
