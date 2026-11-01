# 统一搬屎系统安装记录

已安装三个原生 MaiBot 插件：

- `plugins/cateye_mai_forward`：QQ 原样消息读取、合并转发、消息打包与分享。
- `plugins/forward_messages_auto`：收到 QQ 合并转发后，由 Planner 判断是否跨群分享。
- `plugins/tieba_share`：贴吧帖子抓取、长图渲染、可选视频与去重。

来源为 GitHub 上游仓库，当前安装版本分别为 `0.1.3`、`0.2.12`、`1.1.8`。三个 `_manifest.json` 均通过本地 ManifestValidator，三个插件源码均可导入；Tieba 的 `httpx>=0.26` 已存在，浏览器渲染依赖也已存在。

## 调度组织

现有 `mai-repost-shit` 作为统一机会调度器：

- `plugins/mai-repost-shit/core/sources.py` 启动时发现已加载的来源工具。
- Bilibili 使用现有 `repost_shi` 管线。
- QQ 原样转发来源检测 `cateye_forward_messages`、`pack_chat_messages`、`share_message_pack`；cateye 原始的 `forward_messages` 已改成带来源前缀的 `cateye_forward_messages`，避免与 SnowLuma QQ 适配器同名工具冲突。
- 贴吧来源检测 `tieba_share`、`tieba_repost`、`tieba_post_share`、`repost_tieba`。
- Planner、低活跃补位、去重、目标群和统一分享限额由现有搬屎/自主生活链路负责。
- 来源不可用时不会猜工具名或模拟媒体发送成功。

外部插件配置已处理为：

- cateye：工具启用，名单默认不额外限制。
- ForwardMessagesAuto：启用，但关闭它自己的 source/target Planner 触发，避免与统一调度器重复决策；路由已准备为 QQ 群 1020993873。
- Tieba：插件和自主工具启用，独立定时任务关闭；目标 QQ 群加入白名单；默认使用长图/合并转发路径。

现有统一系统仍有全局自主分享上限，因此“每小时 2–5 条”作为候选机会频率，实际发送量仍受分享上限、媒体成功率、群聊权限和模型决策限制。没有突破原有全局限额，也没有启动三套独立广播器。

## 验收与限制

- 三个 manifest 通过兼容性检查。
- 三个插件源码导入通过。
- 组件清单检查通过；已处理 SnowLuma 与 cateye 的 `forward_messages` 同名冲突。
- Tieba 的实时抓取、长图渲染、QQ 原生合并转发和真实群媒体发送尚未执行生产验收；本轮没有向真实群发送测试媒体。
- 主 bot 尚未重启。当前运行进程不会立即加载新插件；用户手动重启后，WebUI 插件管理中才会显示三个新增条目，统一来源探测才会报告 QQ/贴吧可用。
