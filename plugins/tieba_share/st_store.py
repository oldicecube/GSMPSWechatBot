"""麦麦贴吧搬石 —— 搬运记录。

只干一件事：记住「哪个吧的哪个帖子已经搬过了」，顺便记一下每个吧最后一次搬的时间。
存在插件自己的 data 目录里（ctx.paths.data_dir），不动宿主的数据库。

为什么不落数据库：这几条记录是纯粹的本机去重状态，没有跨会话共享的需求，
一个 JSON 文件足够；而且插件卸载、换版本都不用担心破坏宿主表结构。

写盘用「先写临时文件再改名」，避免进程被杀时留下半个 JSON。

这个文件是**纯本机状态**，可能被人手工编辑、被别的版本写过、被编辑器加上 BOM。
所以读进来一律先规整：坏掉的单条记录丢掉，坏掉的文件先另存一份再按空记录跑，
绝不让一条脏数据把整轮搬石或 /搬石 状态 打断。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA = 1


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_int(value: Any, default: int = 0) -> int:
    """把任意值转成 int，转不了就用默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_moved(value: Any) -> list[dict[str, Any]]:
    """把 moved 规整成 [{tid, title, at}]；不是 dict 的元素直接丢掉。"""
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        tid = item.get("tid")
        if tid is None:
            continue
        out.append(
            {
                "tid": str(tid),
                "title": str(item.get("title") or "")[:80],
                "at": str(item.get("at") or ""),
            }
        )
    return out


def _safe_bucket(value: Any) -> dict[str, Any]:
    """把某个吧的桶规整成 {moved, last_run, total}。"""
    bucket = value if isinstance(value, dict) else {}
    return {
        "moved": _safe_moved(bucket.get("moved")),
        "last_run": str(bucket.get("last_run") or ""),
        "total": max(0, _safe_int(bucket.get("total"), 0)),
    }


class StoneStore:
    """搬运记录的读写。构造后先 load()，退出前 flush()。"""

    def __init__(self, path: Path, *, keep_per_forum: int = 500) -> None:
        self.path = Path(path)
        self.keep = max(20, int(keep_per_forum or 500))
        self._data: dict[str, Any] = {"version": _SCHEMA, "forums": {}}
        # 读不动又备份不了时置真：此时宁可不落盘，也不能用空记录覆盖掉原文件
        self._no_flush = False

    # -- 读写 -----------------------------------------------------------------

    def load(self) -> None:
        """读记录文件。读不到或坏了都当成空记录（不抛异常，插件照常能用）。"""
        try:
            # utf-8-sig：记事本存过的文件带 BOM，用 utf-8 解会让 json.loads 直接失败
            raw = self.path.read_bytes().decode("utf-8-sig")
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("[搬石/记录] 读取失败，按空记录处理：%s", exc)
            return
        try:
            obj = json.loads(raw or "{}")
        except Exception as exc:  # noqa: BLE001
            self._quarantine(exc)
            return
        forums = obj.get("forums") if isinstance(obj, dict) else None
        if isinstance(forums, dict):
            clean: dict[str, Any] = {}
            for forum, bucket in forums.items():
                if isinstance(bucket, dict):
                    clean[str(forum)] = _safe_bucket(bucket)
            self._data = {"version": _SCHEMA, "forums": clean}

    def _quarantine(self, exc: Exception) -> None:
        """解析失败时先另存一份，再按空记录继续。

        直接按空记录跑，下一次 flush 就会把原文件覆盖掉，历史记录再也回不来。
        先复制成 原文件名.broken-时间戳，至少还留着人工抢救的余地。
        """
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = self.path.with_name("%s.broken-%s" % (self.path.name, stamp))
        try:
            shutil.copy2(self.path, backup)
        except Exception as copy_exc:  # noqa: BLE001
            self._no_flush = True
            logger.warning(
                "[搬石/记录] 内容不是合法 JSON（%s），且备份失败（%s）；本次运行不落盘，以免覆盖原文件",
                exc,
                copy_exc,
            )
            return
        logger.warning(
            "[搬石/记录] 内容不是合法 JSON（%s），已另存为 %s，按空记录继续", exc, backup.name
        )

    def flush(self) -> None:
        """落盘。失败只记日志 —— 记录写不进去不该影响搬运本身。"""
        if self._no_flush:
            logger.warning("[搬石/记录] 原文件读不动且备份失败，本次跳过落盘")
            return
        payload = json.dumps(self._data, ensure_ascii=False, separators=(",", ":"))
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_bytes(payload.encode("utf-8"))
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[搬石/记录] 写入失败（本轮去重可能不准）：%s", exc)

    # -- 查询与标记 -----------------------------------------------------------

    def _bucket(self, forum: str) -> dict[str, Any]:
        forums = self._data.setdefault("forums", {})
        bucket = forums.get(forum)
        if not isinstance(bucket, dict):
            bucket = {"moved": [], "last_run": "", "total": 0}
            forums[forum] = bucket
            return bucket
        if not isinstance(bucket.get("moved"), list):
            bucket["moved"] = []
        return bucket

    def _peek(self, forum: str) -> dict[str, Any]:
        """只读地看一眼某个吧的桶，不存在就给个空的（**不写进数据里**）。

        _bucket 是「要写」的入口，它会把不存在的吧补进 forums；
        查询路径必须走这里，否则 /搬石 状态 一列就凭空多出一堆「最近 0 条」的幽灵吧。
        """
        forums = self._data.get("forums")
        bucket = forums.get(forum) if isinstance(forums, dict) else None
        if not isinstance(bucket, dict):
            return {"moved": [], "last_run": "", "total": 0}
        if not isinstance(bucket.get("moved"), list):
            bucket = dict(bucket)
            bucket["moved"] = []
        return bucket

    def known(self, forum: str, tid: str) -> bool:
        tid = str(tid)
        return any(
            isinstance(item, dict) and str(item.get("tid")) == tid
            for item in self._peek(forum)["moved"]
        )

    def mark(self, forum: str, tid: str, title: str = "") -> None:
        """记一条「搬过了」，并把老记录裁到 keep 条。"""
        bucket = self._bucket(forum)
        tid = str(tid)
        if any(
            isinstance(item, dict) and str(item.get("tid")) == tid for item in bucket["moved"]
        ):
            return
        bucket["moved"].append({"tid": tid, "title": str(title or "")[:80], "at": _now()})
        bucket["total"] = max(0, _safe_int(bucket.get("total"), 0)) + 1
        bucket["last_run"] = _now()
        if len(bucket["moved"]) > self.keep:
            bucket["moved"] = bucket["moved"][-self.keep :]

    def mark_run(self, forum: str) -> None:
        """就算这一轮没搬新帖，也记一下「这个吧我来看过了」。"""
        self._bucket(forum)["last_run"] = _now()

    def reset(self, forum: str = "") -> int:
        """清空记录（不给吧名就清全部），返回清掉多少条。"""
        forums = self._data.setdefault("forums", {})
        if forum:
            bucket = forums.get(forum)
            if not isinstance(bucket, dict):
                return 0
            count = len(_safe_moved(bucket.get("moved")))
            bucket["moved"] = []
            return count
        count = 0
        for bucket in forums.values():
            if isinstance(bucket, dict):
                count += len(_safe_moved(bucket.get("moved")))
                bucket["moved"] = []
        return count

    def stats(self) -> dict[str, dict[str, Any]]:
        """给 /搬石 状态 用：每个吧搬过多少条、最后一次看是什么时候。"""
        out: dict[str, dict[str, Any]] = {}
        forums = self._data.get("forums") or {}
        if not isinstance(forums, dict):
            return out
        for forum, bucket in forums.items():
            if not isinstance(bucket, dict):
                continue
            out[str(forum)] = {
                "recent": len(_safe_moved(bucket.get("moved"))),
                "total": max(0, _safe_int(bucket.get("total"), 0)),
                "last_run": str(bucket.get("last_run") or ""),
            }
        return out
