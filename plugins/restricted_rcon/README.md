# 受限 Minecraft RCON Agent Tool

插件 ID `wechatbot.restricted-rcon`，核心工具 `minecraft_rcon_action`。
复用旧 `llm/rcon_tools.py` 的 19 个固定动作与根配置 `rcon`、`rcon_tools`，
不复制密码，不把任意 `/rcon` 命令暴露给模型。

插件向 Host 的 `wechatbot.minecraft_rcon` capability 提交固定动作参数。
Host 为真实微信群消息生成 60 秒、单次使用的随机授权，覆盖模型伪造值，
仅当前插件可兑换。权限按照真实群 ID 或 Host 消息中的群名称，以及真实用户 ID
精确匹配旧白名单；空名单拒绝，保留旧群名白名单语义。可自行将群名配置换成稳定群 ID，
本次未改原配置。每条消息最多 2 次，原配置较低上限优先生效。

保留只读查询及轻量整活动作；禁止 op、ban、kick、stop、改方块、函数、任意命令链。
target 仅玩家名或简单 @a/@p/@s/@r/@e；带条件选择器不开放。
输入控制字符、非有限数值被拒绝，数字限幅，错误仅返回异常类型，避免泄露连接信息。
服务器响应最多 4000 字，是未经信任的数据。RCON 的 @s 没有玩家执行者，通常无目标。

这项 Host capability 和真实请求授权需要用户手动重启主进程后生效。
