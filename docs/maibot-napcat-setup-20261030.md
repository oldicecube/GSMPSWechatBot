# NapCat 登录与双平台启用（2026-10-30）

QQ 使用 `plugins/MaiBot-SnowLuma-Adapter/` 原生连接到同一个 MaiBot 核心，微信保留 GSMPS Wechat Weflow 转译出口。QQ 适配器配置已启用，NapCat Shell 4.18.41 / QQ NT 9.9.33-52230 已准备好；本次只读检查显示 NapCat 正在等待扫码、二维码已就绪。没有重启 bot，也没有向真实群发送验收消息。

## 首次操作

1. 用户自行启动或重启 bot。`main.py` 会后台检查 NapCat；已在运行则复用，否则启动。
2. 首次没有可用登录态，或快捷登录失败时，会自动打开本机 NapCat 二维码页面。使用手机 QQ 扫码并完成腾讯要求的确认。也可双击根目录 `open_napcat_webui.bat` 主动打开页面，无需复制或提交 token。
3. 登录后，在 MaiBot 原生 WebUI 的适配器管理中放行 **QQ 侧**的 `GDUT SIE Minecraft Public Server`，再在聊天流/命令管理中按需设置 LLM 活跃度及插件命令。当前 QQ 适配器策略仍默认拒绝群聊和私聊，登录成功本身不会放开所有群。

不需要另建壳子 QQ 群名单，也不需要把账号、群号发给助手。原生访问策略最终按平台和真实群 ID 保存，群名仅供选择、展示。两个平台同名的群会显示为独立会话，不能只凭显示名选目标。

## 以后每次重启

- NapCat 仍在线：直接复用，bot 重新连接 OneBot，不需要再次扫码。
- NapCat 未启动：bot 后台启动本地 QQ/NapCat，使用记住的真实账号和原生登录态尝试快捷登录。
- 登录失败，或约 15 秒后仍未恢复登录：自动打开一次本机二维码页面；扫码已接受或客户端正在初始化时不会刷新二维码打断登录。
- 登录成功：保存实际 QQ 号并设置 NapCat 原生快捷登录账号，下次继续使用。保留 `depends-data/napcat/` 中的配置和客户端登录数据。

自动登录取决于 QQ 登录态是否有效；首次授权、登录态失效及腾讯要求的安全确认仍需要本人完成。自动弹页需要运行 bot 的 Windows 用户有可见桌面和默认浏览器。恢复助手每次启动最多观察 10 分钟，不会反复弹多个页面；需要时可手动打开上面的批处理入口。

## 实现及诊断

- `core/napcat_boot.py`：由 `main.main()` 调用，后台启动检查；不启动或重启 bot 自身。
- `scripts/start_napcat.ps1`：复用已有客户端，或隐藏启动；保存账号通过 `-q` 传给官方启动器。
- `scripts/napcat_login_watch.py`：本机鉴权、登录状态检查、二维码恢复和登录账号记忆。单实例锁防止重复监视器。
- `depends-data/napcat/config/maibot-launch.json`：`autostart` 和成功登录后保存的账号；不包含 QQ 密码。
- `depends-data/napcat/config/bootstrap.log`、`login-watch-status.json`：启动及恢复结果。

只读状态检查：

```powershell
python -X utf8 scripts/napcat_login_watch.py
python -X utf8 scripts/napcat_health.py
```

前者通过本机 NapCat WebUI 查询登录阶段；后者检查 OneBot 连接，尚未登录时 OneBot 不可用是预期状态。WebUI 为 `127.0.0.1:6099`，OneBot 正向 WS 为 `127.0.0.1:13001`，与微信端口分开。令牌仅存本地配置，不写入文档。

登录恢复状态测试：`python -X utf8 scripts/napcat_login_acceptance.py`，**14/14 通过**。测试使用模拟状态及启动器 `-PlanOnly`，未拉起客户端或浏览器；实际扫码、账号登录及真实群收发仍待首次接入后验证。

更多消息及插件验收见 [双端插件与登录恢复验收](maibot-dual-plugin-login-20261030.md)。官方启动方式参考 [NapCat Windows Shell](https://napneko.github.io/guide/boot/Shell)。
