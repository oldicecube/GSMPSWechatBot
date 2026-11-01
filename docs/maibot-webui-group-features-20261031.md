# WebUI 逐群 LLM / 插件开关

入口：**WebUI → 插件管理（适配器）→ GSMPS Wechat Weflow 或 SnowLuma/NapCat → 黑白名单规则**。

当群聊 ID 被加入“接收消息的聊天 ID”后，群聊规则下会出现“群聊功能”卡片。每个白名单群独立显示：

- **LLM 聊天**：控制普通聊天、@ 必回、MaiBot 原生主动聊天和依赖 LLM 的跨群/自主行为。
- **插件**：控制命令插件、自动化插件和原生插件工具。关闭后不执行插件；适配器接收仍可保留。

两个开关相互独立，支持只开 LLM、只开插件、同时开启或全部关闭。群聊频率、单个插件的命令管理和适配器黑白名单仍分别生效；不接收名单优先于开关。

设置按完整适配器身份保存（平台、适配器、账号和 scope），所以同名 QQ 群和微信群可以有不同开关。保存格式位于 `config/adapter_policy.toml` 的对应 `[[adapters]]` 项，例如：

```toml
[[adapters]]
plugin_id = "maibot-team.snowluma-adapter"
platform = "qq"

[adapters.group]
default_action = "block"
allow_ids = ["1020993873"]
features = { "1020993873" = { llm_enabled = true, plugins_enabled = false } }
```

未设置 `features` 的群继承原来的行为，不会因为打开新页面而改变已有配置。`"*"` 可以作为白名单默认开关，具体群号配置优先。WebUI 会显示当前运行账号和真实群名，群名只用于展示，不参与匹配。

后端在入站、壳子命令、原生插件组件、Hook、自主群列表和出站投递处统一检查开关；关闭 LLM 时，@ 必回也不会绕过开关，关闭插件时仍可保留 LLM 对话。出站层再次检查，防止插件或自主任务绕过群聊设置。

旧核心未加载这项能力时，页面会显示“请手动重启 bot 加载逐群功能开关”并禁用 Switch，避免用户以为已保存。当前主进程没有被 agent 重启；完成本轮后请手动重启一次，再刷新 WebUI。

## 验收

- 后端隔离测试：17 项通过，覆盖持久化、QQ/微信同名群隔离、账号/scope 隔离、通配符、策略分组、并发保存、API 读写、壳子和原生插件/Hook/出站门禁。
- WebUI 测试：49 项通过（适配器策略面板与聊天管理 API）。
- Dashboard 构建：`npm.cmd run build` 成功；仅有现有的 bundle 体积提示。
- 未修改生产 `config/adapter_policy.toml`，未重启主 bot，未发送真实消息。
