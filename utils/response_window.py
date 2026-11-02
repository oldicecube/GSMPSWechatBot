"""The shell's response window gates LLM replies and automatic reposts only."""
from datetime import datetime
from functools import lru_cache
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo


@lru_cache(maxsize=4)
def _read(path, stamp, size):
    value = json.loads(Path(path).read_text(encoding='utf8'))
    return value.get('time_slots') or []


def response_allowed(slots=None, *, now=None):
    if slots is None:
        root = Path(os.environ.get('MAIBOT_INSTANCE_DIR') or Path(__file__).resolve().parents[1])
        path = root/'config.json'
        try:
            stat = path.stat(); slots = _read(str(path), stat.st_mtime_ns, stat.st_size)
        except FileNotFoundError:
            slots = []
        except (OSError, ValueError, TypeError):
            return False  # A temporarily invalid window must not enable autonomous posts.
    if not slots:
        return True
    current = now or datetime.now(ZoneInfo('Asia/Shanghai'))
    if current.tzinfo:
        current = current.astimezone(ZoneInfo('Asia/Shanghai'))
    minute = current.hour*60+current.minute
    for slot in slots:
        try:
            start = datetime.strptime(slot.get('start', '00:00'), '%H:%M').time()
            end = datetime.strptime(slot.get('end', '23:59'), '%H:%M').time()
        except (AttributeError, TypeError, ValueError):
            continue
        a, b = start.hour*60+start.minute, end.hour*60+end.minute
        within = a <= minute <= b if a <= b else minute >= a or minute <= b
        if within:
            return True
    return False
