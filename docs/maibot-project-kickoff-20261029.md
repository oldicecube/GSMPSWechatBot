# 自选项目启动与权限申请

后续修正：API已恢复为经过验证的免费路由，项目引导已改成一次性初始引导，进度分享并入自主生活。当前行为与最新验收以 [免费API与生活合并记录](maibot-free-api-life-merge-20261029.md) 为准；下文保留前一阶段的实施经过。

已直接补代码和配置。生产进程未重启，未给真实微信群发送测试消息。用户手动重启后无需另发启动提示词；当前委托会在插件首次加载时登记，首次检查提前到30秒后，仍遵守休息时段、每日预算和API错误退避。当前所有聊天模型仍受提供商配额限制，所以不能承诺重启后立即产生实际研究或代码。

## 行为

当前 `project.objective` 要求麦麦自行选择有兴趣、有实际用途的小项目，优先参考近期热门开源方向。第一步必须实际搜索公开资料，随后由模型自行选题、缩小范围，在现有标准库 AppContainer 沙盒内逐步写、运行、保存最小原型。没有预先指定某一个所谓热门项目，也不会将旧知识当作当前热度。

每次考虑只执行一个有界动作，后续仍按10–30分钟节奏推进。相同 `request_id` 只登记一次，重启保留研究状态、项目进度和申请记录。仅有计划或研究结果不能将程序委托标记为完成。项目后续只更新研究笔记时保留之前的源码。

必要能力不足时先考虑受限替代方案，可以申请依赖、沙盒网络、宿主文件/数据、部署或资源额度。实际 Python `ModuleNotFoundError` 和权限拒绝也会触发持久化暂挂。普通语法错误仍按现有的一次修复处理；模型429和隔离能力不可用不会被误当作权限申请。

通知使用原生聊天 Replyer 和原生 At，接收人固定为现有管理员微信ID。已用当前 `config.json` 管理员配置、原生 `person_info` 及同群真实消息交叉确认对应“陈年老冰块”，不根据可随意更改的昵称授权。

申请同样遵守全局每日4次主动分享、2小时间隔及同人72小时@冷却；额度或冷却不允许时申请保留等待。单次委托最多记录8个不同申请，同一项目/能力去重，未知发送结果不重试。因此“直接申请”不意味着可以绕过当前防刷屏限制。原生生成失败或放弃发送也会结束该次已保留的通知尝试；可用只读状态工具确认阻塞状态，不保证每次申请都能送达。

申请不会授予能力，机器人不会自行安装宿主依赖、修改系统权限、读取密钥或退出沙盒。现有沙盒只适合标准库小原型，不能直接启动完整热门仓库或部署服务。如果确实要增加能力，由操作者实际配置/开发后，在群内明确要求“继续自主项目”（或 resume project）。只说“同意”不改变任何实际权限。仍然缺权限时再次暂挂，同一能力不重复@。

新增 `autonomous_project_control` 原生 Tool 支持 pause/resume。Host写入并覆盖真实请求身份，插件再核验同群原始消息；仅固定操作者的明确暂停/继续消息可以改变调度。普通群友和模型伪造的 `_request_context` 无效。恢复只改变项目状态，不改变能力。已完成或放弃的委托不能用 resume 重复启动；需要新任务时更换配置 `request_id`。

## 修改文件

- `plugins/autonomous_life/project.py`：持久化一次性委托、调研优先、权限识别/申请/去重、暂停恢复和操作者核验。
- `plugins/autonomous_life/plugin.py`：原生配置Schema、启动与后台步骤接入、权限错误处理、只读状态及控制Tool。
- `plugins/autonomous_life/store.py`：更新项目笔记时保留已有源码。
- `plugins/autonomous_life/config.toml`：登记本次自选项目和经核验的固定操作者。
- `plugins/autonomous_life/_manifest.json`：1.1.0，声明原生原始消息读取能力。
- `plugins/autonomous_life/README.md`：使用方式、限制和配置说明。
- `src/plugin_runtime/component_query.py`：在现有Host请求身份注入白名单加入该插件，覆盖模型伪造身份；不修改原生提示词。
- `scripts/maibot_project_acceptance.py`：新项目验收。
- `scripts/maibot_autonomous_life_acceptance.py`：通用回归显式关闭委托，避免混入启动流程。
- `scripts/fixtures/maibot_autonomous_fixture.py`、`scripts/maibot_bridge_acceptance.py`：隔离原生RPC权限申请、微信At与操作者控制验收；fixture没有安装到生产插件。

## 验收

- `scripts/maibot_project_acceptance.py`：38/38，通过真正的 AppContainer 执行标准库原型并保存成果；验证启动登记、真实研究步骤、缺依赖、原生At、恢复后仍受限、冷却、未知结果不重发、跨群/身份伪造拒绝和完成状态。
- `scripts/maibot_autonomous_life_acceptance.py`：63/63，自主生活、隔离边界、限额、上下文和原生风格回归。
- `scripts/maibot_bridge_acceptance.py --plugin-source plugins --test-enable-life --test-agent-tools --test-autonomous-life`：90/90，其中普通转译单元套件另含53项。新增5项通过真实原生Host→Runner→Replyer→微信发送转译验证权限申请及暂停恢复；所有微信发送均被截获。
- AST解析、配置读取、生产fixture排除和 `git diff --check` 通过；Git仅报告原有LF/CRLF提示。
- 最后一次公开搜索探针成功返回5项；python.org页面读取一次超时。三个聊天模型均为 `429 PROMOTION_MODEL_LIMIT_REACHED`。探针只发合成健康检查内容，没有发送群聊天记录。

结果：`docs/maibot-wechat-acceptance/project-kickoff-20261029.json`、`project-kickoff-bridge-20261029.json`、`autonomous-life-20261029.json`、`autonomous-life-live-probe-20261029.json`。本次验收使用合成LLM响应和截获的微信发送；当前无法完成真实模型驱动的生产项目，也未做24小时生产运行验收。重启不能消除提供商配额限制，需恢复额度或配置一个可用的聊天模型API。
