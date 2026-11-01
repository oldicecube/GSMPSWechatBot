"""source Planner 成功触发记录的永久防重状态。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import time
from typing import Any

SOURCE_TRIGGER_STATE_VERSION = 1


class SourcePlannerTriggerStore:
    """永久保存已经成功触发过 source Planner 的消息键。"""

    def __init__(self, path: Path, logger: Any) -> None:
        """创建尚未读取磁盘的 source 触发状态存储。

        Args:
            path: 状态 JSON 文件完整路径，必须位于插件数据目录。
            logger: 用于记录状态读取失败的插件日志对象。
        """

        self._path = path
        self._logger = logger
        self._triggered_keys: set[str] = set()
        self._lock = asyncio.Lock()

    @staticmethod
    def build_key(stream_id: str, message_id: str) -> str:
        """生成不暴露聊天流或消息原值的永久触发键。

        Args:
            stream_id: source 聊天流 ID。
            message_id: source 平台消息 ID。

        Returns:
            由两项标识计算出的 ``source-trigger:<摘要>`` 稳定键。
        """

        digest = hashlib.sha256(f"{stream_id}\n{message_id}".encode()).hexdigest()[:24]
        return f"source-trigger:{digest}"

    async def load(self) -> None:
        """在线程中读取永久触发键；文件缺失或损坏时安全回退为空集合。"""

        await asyncio.to_thread(self._load_sync)

    async def save(self) -> None:
        """在锁保护下把当前永久触发键原子写回插件数据目录。"""

        async with self._lock:
            await asyncio.to_thread(self._save_sync)

    def contains(self, trigger_key: str) -> bool:
        """判断稳定键是否已经成功触发过 source Planner。

        Args:
            trigger_key: ``build_key`` 生成的稳定键。

        Returns:
            该键已持久化时返回 ``True``，否则返回 ``False``。
        """

        return trigger_key in self._triggered_keys

    async def mark_triggered(self, trigger_key: str) -> None:
        """登记一次成功触发并立即持久化，阻止未来重复触发。

        Args:
            trigger_key: 已经确认 Planner 主动任务入队的稳定键。
        """

        async with self._lock:
            if trigger_key in self._triggered_keys:
                return
            self._triggered_keys.add(trigger_key)
            await asyncio.to_thread(self._save_sync)

    def _load_sync(self) -> None:
        """同步读取并校验状态文件中的字符串触发键列表。"""

        if not self._path.is_file():
            self._triggered_keys = set()
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._logger.warning("读取 source Planner 触发状态失败，将使用空状态: %s", exc)
            self._triggered_keys = set()
            return
        raw_keys = payload.get("triggered_keys") if isinstance(payload, dict) else None
        self._triggered_keys = (
            {str(item) for item in raw_keys if isinstance(item, str) and item} if isinstance(raw_keys, list) else set()
        )

    def _save_sync(self) -> None:
        """同步原子写入触发键快照，写入失败时向异步调用方传播异常。

        Raises:
            OSError: 创建目录、写临时文件或替换正式文件失败时抛出。
        """

        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_suffix(".tmp")
        payload = {
            "version": SOURCE_TRIGGER_STATE_VERSION,
            "updated_at": time.time(),
            "triggered_keys": sorted(self._triggered_keys),
        }
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self._path)
