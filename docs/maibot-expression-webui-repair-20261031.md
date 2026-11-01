# 表达方式 WebUI 显示修复

## 根因

`data/MaiBot.db` 的 `expressions` 表中有 663 个旧记录的 `last_active_time` / `create_time` 仍是 Unix 整数时间戳。当前 SQLModel 字段是 `DateTime`，SQLAlchemy 在加载列表前就会对整数调用 `fromisoformat`，导致 `/api/webui/expression/list`、审核列表等接口返回 500，页面因此无法显示。

## 修复

- 已先备份数据库：[MaiBot-before-expression-time-repair-20261031.db](</D:/Wechat Bot/temp/MaiBot-before-expression-time-repair-20261031.db>)。
- 已将 663 个旧时间戳转换成当前使用的 ISO 时间字符串；没有删除表达方式、修改内容或审核状态。
- 在数据库初始化流程加入 `repair_legacy_expression_timestamps`，以后旧导入数据也会自动修复。
- 原有 `modified_by` 兼容修复保持不变。

## 验收

- 生产只读 HTTP 验收：`GET /api/webui/expression/list` 返回 200，当前返回 561 条记录；`groups`、`chats`、统计、审核列表、审核日志和聚类接口均返回 200。
- 前端表达方式相关测试：4 个测试文件、66 项通过。
- Python 编译通过。

主 bot 没有重启；本次数据库修复已立即生效，初始化保护逻辑会在后续重启继续兜底。
