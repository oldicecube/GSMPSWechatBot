# 自主跨群转发插件

[![Version](https://img.shields.io/github/v/tag/papaya0481/maibot-ForwardMessagesAuto-plugin?sort=semver&label=Version)](https://github.com/papaya0481/maibot-ForwardMessagesAuto-plugin/tags)
[![License](https://img.shields.io/github/license/papaya0481/maibot-ForwardMessagesAuto-plugin?label=License)](LICENSE)

让 MaiBot 像群聊成员一样，自主判断群聊中的消息是否值得分享，
再转发到其他群。群聊由配置白名单决定，插件暂时不会让模型自行指定要转发的目标群。

目前仅支持合并转发消息。后续可以添加更多类型的消息。

> [!WARNING]
> 目前只在 QQ 群聊的 SnowLuma Adapter 环境下完成验证。Napcat 理论可行，需要测试。

## 主要功能

- 收到**转发消息**的群 Planner 查看消息内容后，**自主决定是否分享**。若偶然漏看，
  插件会在实际发送前补做查看，不会直接绕过内容检查。
- 一个消息可按配置顺序发送到多个群；单个群失败不会阻止
  后续发送，已完成的投递不会因重试或路由调整而重复发送。
- 可独立配置是否在群收到合并转发后强制触发 Planner，以及是否在转发成功的群触发该群 Planner。

由于所有请求都会走 Planner，因此虽然可以直接 @ 麦麦让其转发，但不能保证 Planner 会通过用户请求（仍可以拒绝）。

## 安装要求

- MaiBot `1.2.0` 或更高兼容版本
- maibot-plugin-sdk `2.7.1` 或更高兼容版本

插件只使用 MaiBot 通用 capabilities，
因此适配器暂未加载不会阻止插件本身加载，但实际转发仍要求适配器运行环境提供相应能力。

## 安装步骤
将插件放入 MaiBot 的
`plugins/` 目录。

```
cd plugins
git clone https://github.com/papaya0481/maibot-ForwardMessagesAuto-plugin
```

## 配置

首次启动后，MaiBot 会在插件目录生成 `config.toml`。请保留自动生成的
`plugin.version` 和 `plugin.config_version`，并至少完成以下配置后再启用：

```toml
[plugin]
enabled = true

[routing]
source_groups = ["123456789"]
target_groups = ["234567890"]

[behavior]
trigger_source_planner = true
trigger_target_planner = true
```

| 配置名 | 类型 | 说明 |
| --- | --- | --- |
| `source_groups` | `list[str]` | 允许触发自主分享的 QQ 群号列表，默认 `[]`；群号使用字符串。例如：`["123456789", "888888"]`。 |
| `target_groups` | `list[str]` | 允许接收分享的 QQ 群号列表，默认 `[]`；列表顺序就是每次任务的发送顺序。允许与 `source_groups` 有交集。例如：`["234567890", "888888"]`。 |
| `view_failure_fallback_threshold` | `int` | 针对合并转发消息，完整内容查看连续发生可重试故障多少次后，才允许使用消息摘要或预览继续判断。默认值为 `2`。|
| `trigger_source_planner` | `bool` | 默认开启。source 白名单群收到真实合并转发时会强制触发一次本群 Planner；设为 `false` 可关闭，但不代表一定查看、转发或回复。例如：`false`。 |
| `trigger_target_planner` | `bool` | 默认开启。每个 target 发送成功后，插件会为该群安排一次 Planner 主动任务。例如：`false`。 |

> [!TIP]
> `source_groups` 与 `target_groups` 可以有交集。转发任务会排除自收自发的回环行为。

### 跨群分享与隐私边界

source 白名单和 target 白名单决定合并转发内容的跨群流向。请只填写已经同意
接收这类分享的群聊；插件不会让模型自行指定来源群或目标群。

## 运行方式

1. source 白名单群收到消息；默认启用 `trigger_source_planner`，插件会在
   MaiBot 可查询到该消息后触发本群 Planner，设为 `false` 可关闭。
2. source 群 Planner 查看完整内容并决定是否请求分享。合并转发内部节点最初
   来自哪个群不影响判断，真正的 source 始终是当前白名单群。
3. 插件校验 source 白名单、消息归属、消息类型和防重状态，再按
   `target_groups` 顺序逐个发送。
4. 每个 target 发送成功后，插件按配置决定是否触发目标群 Planner；所有 target
   都处理完毕后，source 群 Planner 才会收到本次任务的实际结果。

插件只保证“发送”和“主动任务入队”按 target 顺序发生。当前 SDK 无法等待一个
目标群 Planner 完整执行结束，因此不同目标群的 Planner 可能并发推理。

投递状态会按 source 消息和 target 群分别保存在 MaiBot 分配的插件数据目录中。
若发送成功但后续 Planner 入队失败，再次请求时会从未完成阶段继续；新增 target
群只会补发到新目标，不会重发已经完成的目标。

## 当前限制

- 目前只支持 QQ 合并转发消息，不支持普通文本、图片或内容平台分享卡片。未来考虑支持，可能需要上游 SDK 提供能力。
- source 侧看到的完整内容不会重复注入 target 群上下文。目标群 Planner 可以在
  能唯一定位真实目标消息时再次查看完整内容，但插件目前不强制这一步，也不能
  保证目标 Planner 在首次主动任务中已经取得完整内容。
- 目标群近期聊天会按 MaiBot 的正常上下文窗口参与判断；热运行和冷启动时，合并
  转发的初始展示详略可能不同。无法可靠确认目标消息或内容时，Planner 会保持
  沉默。

## 反馈与排障

请通过 [GitHub Issues](https://github.com/papaya0481/maibot-ForwardMessagesAuto-plugin/issues)
反馈问题。请附上插件、MaiBot、SDK 和 Adapter 版本，最好有脱敏后的相关配置，以及
触发时间和插件日志；非必要情况下不要提交原始合并转发内容、完整群号或其他隐私信息。
