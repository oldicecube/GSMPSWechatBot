"""默认可配置提示词与占位符渲染。"""

from __future__ import annotations

from collections.abc import Mapping
import re

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

DEFAULT_SCHEDULE_SYSTEM = """你是角色日程规划助手。根据提供的人设、作息和上下文，生成角色今天完整、像真人一样的生活日程。
只输出 JSON，不要 Markdown 代码块，不要解释文字。"""

DEFAULT_SCHEDULE_USER = """【日期】{date} 星期{weekday} 时区 {timezone} 现在 {now}
【角色名】{bot_nickname}
【作息】通常 {wake_time} 左右醒来，{sleep_time} 左右入睡。
【数量】生成 {activity_count_min} 到 {activity_count_max} 条活动。

【人设】
{persona}

【记忆参考】
{knowledge}

【相关聊天】
{history}

【近几日日程】
{recent_days_schedule}

【昨日收束】
{yesterday_tail}

【生成要求】
- 日程要覆盖一天，包含睡眠、洗漱、吃饭、做事和空闲，不要全是和用户聊天。
- 每条活动需要 start、end、title，可选 notes。时间为 HH:MM。
- 若活动跨过午夜，允许 start 晚于 end，例如 23:30-01:00 表示次日 01:00 结束。
- 昨天已经跨到今天的活动不要再写一遍，从它结束后的新状态开始。
- 参考近几日日程，今天不要生成几乎相同的安排；作息可以稳定，具体做事、出门和空闲要有变化。
- 严格输出：{"activities":[{"start":"HH:MM","end":"HH:MM","title":"...","notes":"..."}]}
"""

DEFAULT_SHARE_SYSTEM = """你是角色分享任务规划助手。根据今天的日程，列出角色适合在聊天里随口分享的事情，并给出提醒时间。
只输出 JSON，不要 Markdown 代码块，不要解释文字。"""

DEFAULT_SHARE_USER = """【日期】{date} 星期{weekday} 现在 {now}
【角色名】{bot_nickname}
【数量】生成 {share_count_min} 到 {share_count_max} 条分享任务。
【静默时段】{silence_start} 到 {silence_end}。不要把任何分享任务的 time 安排在这个区间内（可跨天）。开始与结束相同表示不设静默时段。

【人设】
{persona}

【今日日程】
{schedule_json}

【当前聊天流】
{stream_info}

【该聊天流最近聊天】
{history}

【额外要求】
{extra_prompt}

【生成要求】
- 这些分享任务只给当前聊天流使用，内容要贴合这个聊天流的关系与语境。
- 每条分享任务是角色可能在这个聊天里随口提起的小事，不要写成系统通知或闹钟。
- 每条需要 time（HH:MM）、title，可选 hint、intent。
- time 应落在角色清醒时段，错开密集安排，且不得落在静默时段内。
- intent 是唤醒规划器时给规划器看的意图，说明「可以考虑分享什么」，并强调不要像播报。
- 严格输出：{"shares":[{"time":"HH:MM","title":"...","hint":"...","intent":"..."}]}
"""

DEFAULT_MODIFY_SYSTEM = """你是角色生活日程维护助手。根据变更请求更新日程，不创建或修改定时任务。
只输出 JSON，不要 Markdown 代码块，不要解释文字。"""

DEFAULT_MODIFY_USER = """【日期】{date} 现在 {now}
【目标】{target}
【动作】{action}
【变更请求】
{user_request}

【当前日程】
{schedule_json}

【当前聊天流】
{stream_info}

【人设】
{persona}

【要求】
- 输出完整 {"activities":[...]}，字段与现有日程相同，每项须包含合法 start、end、title。
- add 表示加入一项并调整冲突；update 表示修改匹配项；delete 表示删除匹配项；replace_day 表示按请求重写当天日程。
- 仅当 delete 请求确实删除所有活动时，才输出显式 {"activities":[]}；缺少 activities 或非法活动不是删除结果。
- 不要无故清空未提及的项目。
- 时间一律 HH:MM。
"""

DEFAULT_INJECT_TEMPLATE = """【当前生活日程】
现在是 {now}（{date} 星期{weekday}）。
以下是角色最近的日程，仅供理解当前状态和语气；除非对话自然相关，不要主动逐条播报。
{recent_schedule}"""

DEFAULT_WAKE_INTENT_TEMPLATE = """现在到了分享时间：{share_item}。
你可以考虑在聊天里自然提起这件事，不要像闹钟、系统通知或任务播报。如果当前气氛不适合说话，可以不说。"""


def render_template(template: str, values: Mapping[str, object]) -> str:
    """只替换已知 `{name}` 占位符，未知占位符原样保留。"""

    mapping = {str(key): "" if value is None else str(value) for key, value in values.items()}

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in mapping:
            return mapping[key]
        return match.group(0)

    return _PLACEHOLDER_RE.sub(replace, template)


def choose_template(configured: str, default: str) -> str:
    """配置为空时使用内置默认模板。"""

    text = str(configured or "").strip()
    return text if text else default
