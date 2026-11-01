from __future__ import annotations
import json, os, threading, time
from pathlib import Path

_LOCK = threading.RLock()
_ROOT = Path(os.environ.get("MAIBOT_INSTANCE_DIR") or Path(__file__).resolve().parent.parent)
_PATH = _ROOT / "logs" / "llm_runtime.jsonl"

def trace_runtime(event, **fields):
    record={"ts":time.time(),"pid":os.getpid(),"event":str(event)}
    for key,value in fields.items():
        if isinstance(value,(str,int,float,bool)) or value is None:
            record[key]=value
        else:
            record[key]=str(value)[:500]
    try:
        _PATH.parent.mkdir(parents=True,exist_ok=True)
        with _LOCK:
            with _PATH.open("a",encoding="utf-8") as fh:
                fh.write(json.dumps(record,ensure_ascii=False,separators=(",",":"))+"\n")
    except Exception:
        pass
