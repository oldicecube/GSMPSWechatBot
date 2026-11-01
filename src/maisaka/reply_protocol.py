"""Recognize missing Planner action calls without treating analysis as speech."""
import re

_DECORATION = re.compile(r"[*`#_]")
_SILENCE = re.compile(r"(?:暂不|无需|不需要|不要|不再|不用|不应|可以不|不)(?:进行|继续|任何)?(?:回复|发言|回应|调用)|保持(?:安静|沉默|静默)|等待(?:新消息|进一步互动)")
_REPLY = re.compile(
    r"(?:直接|准备|决定|应该|需要|必须|将要|可以)(?:进行|给出|发送|进入|调用|用|他|她|我|群友|用户|一条|一个|自然地|自然的|自然|地|\s|[：:])*"
    r"(?:回复|回应|reply(?:er)?)|调用\s*reply\b|reply\s*[：:]\s*(?:必须|需要|调用)|回复即可"
    r"|(?:决定|判断|结论)\s*[：:]\s*(?:纯接话|延伸话题).*?replyer",
    re.IGNORECASE,
)
_OTHER_ACTION = re.compile(r"(?:先|准备|决定|需要|必须|开始|接下来|然后|再)\s*(?:使用|调用|用|查询|检索|搜索|获取)", re.IGNORECASE)


def planner_text_action(text: str) -> str:
    """Return reply/action/none from explicit decisions; never invent an action.

    Prefer the final explicit silence decision over options discussed earlier.
    Only a reply decision without a later pending query may use Replyer recovery.
    """
    clean = _DECORATION.sub('', str(text or ''))
    silences = list(_SILENCE.finditer(clean))
    # “不需要调用其他工具，直接回复” excludes other tools, not reply.
    silences = [m for m in silences if not (
        '调用' in m.group() and re.match(r'(?:其他|其它)工具', clean[m.end():])
    )]
    replies = [m for m in _REPLY.finditer(clean) if not any(
        m.start() < negative.end() and m.end() > negative.start() for negative in silences
    )]
    final_reply = replies[-1].start() if replies else -1
    if silences and silences[-1].start() > final_reply:
        return 'none'
    actions = []
    for match in _OTHER_ACTION.finditer(clean):
        if clean[max(0, match.start() - 2):match.start()].endswith(('不', '无')):
            continue
        if any(match.start() < n.end() and match.end() > n.start() for n in silences):
            continue
        suffix = clean[match.end():].lstrip()
        # A native reply call is itself the speech decision, not a pending
        # information-gathering step. Negated other-tool clauses are ignored.
        if re.match(r'reply\b', suffix, re.I) or re.search(r'reply\s*[:：]\s*$', clean[:match.start()], re.I):
            continue
        actions.append(match)
    if replies:
        if actions:
            return 'action'
        return 'reply'
    return 'action' if actions and not silences else 'none'
