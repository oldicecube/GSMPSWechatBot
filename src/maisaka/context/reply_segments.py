"""Optional conversational message boundaries chosen by Replyer, not Planner."""
import re

_FORMATTED_BLOCK = re.compile(r'(?m)^\s*(?:```|~~~|#{1,6}\s|>\s|[-+*]\s|\d+[.)、]\s|\|)|\$\$')


def message_layout_instruction(max_messages: int) -> str:
    maximum = min(8, max(1, int(max_messages)))
    if maximum <= 1:
        return ''
    return (
        f'沿用当前语气和表达习惯。自然适合连续发两三条时，可用空行分隔独立消息，最多{maximum}条；'
        '每条表达一个完整意思，不按标点硬拆，也不为凑条数加话。一条就够时照常只写一条，'
        '代码、列表和长文保持完整。仍只输出发言正文。'
    )


def split_reply_messages(text: str, max_messages: int) -> list[str]:
    """Preserve blank-line boundaries of ordinary chat; protect formatted output.

    Overflow joins the tail, preserving all content without creating a burst.
    No JSON or reserved visible marker is required from the model.
    """
    maximum = min(8, max(1, int(max_messages)))
    body = str(text or '').strip()
    if not body or maximum <= 1 or _FORMATTED_BLOCK.search(body):
        return [body] if body else []
    parts = [part.strip() for part in re.split(r'\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*', body) if part.strip()]
    if len(parts) > maximum:
        parts = [*parts[:maximum - 1], '\n\n'.join(parts[maximum - 1:])]
    return parts
