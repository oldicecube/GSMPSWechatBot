# GSMPS 壳子插件 WebUI 配置审计

审计范围：42 个 GSMPS 来源条目（命令、自动化和 Weflow 适配器）。

## 本轮发现并修复

1. `core/shell_plugin_config.settings()` 对没有 `config_path` 的壳子字段存在默认值错误：它会把整个 `config.json` 当作字段值，再交给类型校验。现在无 `config_path` 字段正确从 WebUI 控制状态读取，未覆盖时使用声明的 `default`。
2. `/qq`、`/wx` 已声明 `cooldown_seconds`，但原来没有真正执行冷却。现在按来源平台、目标平台和当前群聊限流；`max_chars` 仍控制正文长度。
3. `/lottery` 实际使用的 `option_count`、`winner_count`、`options`、`base_odds` 原来没有在 WebUI 声明，已补齐，并让 `base_odds` 使用 JSON 数组以兼容数字赔率。
4. `patpat` 新增的 QQ 递增禁言参数已声明并可配置：第三次概率、强制累计次数、最长时长、启用开关。
5. 低级但常用的 `repeat_echo`、`picture`、`player_monitor`、`tournament_monitor` 字段均能读取声明值；配置读取回归已覆盖。

## 当前配置项状态

- 纯行为/限流项（如 `/qq`、`/wx`、`repeat_echo`、`patpat`）使用 WebUI 管理状态，适合热更新。
- 真实外部服务连接项（Minecraft、RCON、兑换服务地址/令牌、Bilibili 下载限制）使用 `config_path`，明确标记为重启生效。
- 彩票的开奖时间、投注限制、选项数量和赔率已统一由 `lottery.*` 路径管理；列表/JSON 字段保留类型校验。
- 通用 `allow_groups`、`allow_users`、`agent_enabled` 仍由所有 GSMPS 条目继承；实际调用继续经过群白名单、平台能力和命令权限检查。
- 没有实际可配置项的简单命令继续声明空字段，避免在 WebUI 制造无效开关。

## 验收

- 42 个 GSMPS 条目的 `settings()` 默认值和类型读取全部通过。
- `lottery`、`qq`、`wx`、`patpat`、`repeat_echo`、`player_monitor`、`picture` 等修改文件编译通过。
- `config.json`、`config/model_config.toml` 和插件 TOML 均未被本次审计脚本自动重写。
- 主 bot 未重启；壳子代码和 WebUI 元数据需要用户手动重启后完整加载。
