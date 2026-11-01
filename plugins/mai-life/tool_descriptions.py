"""规划器工具的简短默认描述。"""

DEFAULT_QUERY_TOOL_DESCRIPTION = (
    "查询自己的生活日程和当前对话的定时任务，包括自动分享、提醒和稍后继续的话题。"
    "需要了解当前活动、查看某天安排，或在修改/取消任务前获取 task_id 时使用。"
)

DEFAULT_MODIFY_TOOL_DESCRIPTION = (
    "修改生活日程，或在当前对话安排单次提醒、分享、继续话题等定时任务。"
    "日程用 target=schedule 和 request 描述变更；任务用 target=share，支持 list/add/update/delete。"
    "新增任务填 title、intent，并在 run_at 与 delay_minutes 中二选一；"
    "修改或取消前先查询 task_id，只能操作 pending 任务。"
    "到期唤醒规划器处理，不等于消息已发送；不支持周期任务。"
)
