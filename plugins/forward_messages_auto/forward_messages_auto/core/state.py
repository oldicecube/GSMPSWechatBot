"""转发任务阶段的持久化。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import time
from typing import Any

from .models import ForwardJob, TargetStage


class ForwardStateStore:
    """原子保存各目标群阶段，并提供幂等完成判断。"""

    def __init__(self, path: Path, logger: Any) -> None:
        """创建任务状态存储。

        Args:
            path: 状态 JSON 文件的完整路径，通常位于插件数据目录下。
            logger: 用于记录状态文件读取失败的日志对象。

        Note:
            构造方法不会读取磁盘；调用方必须在使用状态前等待 ``load``。
        """

        self._path = path
        self._logger = logger
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def load(self) -> None:
        """在线程中读取状态文件并替换当前内存状态。

        文件不存在时初始化为空状态；JSON 损坏或读取失败时记录警告并使用
        空状态。磁盘 I/O 通过 ``asyncio.to_thread`` 执行，避免阻塞事件循环。
        """

        await asyncio.to_thread(self._load_sync)

    async def save(self) -> None:
        """在异步锁保护下原子保存当前内存状态。

        方法等待其他状态更新完成，再在线程中将完整快照写入临时文件并替换
        正式文件。
        """

        async with self._lock:
            await asyncio.to_thread(self._save_sync)

    @staticmethod
    def build_state_key(stream_id: str, message_id: str) -> str:
        """生成不受 target 路由变化影响的永久任务键。

        Args:
            stream_id: source 聊天流 ID。
            message_id: source 合并转发消息 ID。

        Returns:
            ``"forward:<24位摘要>"`` 格式的稳定键。摘要只包含 source
            stream 与 ``msg_id``，各 target 的永久阶段保存在同一任务下。
        """

        digest = hashlib.sha256(f"{stream_id}\n{message_id}".encode()).hexdigest()[:24]
        return f"forward:{digest}"

    def is_complete(
        self,
        state_key: str,
        target_group_ids: list[str],
        required_stage: TargetStage,
    ) -> bool:
        """判断指定任务的所有目标是否达到要求阶段。

        Args:
            state_key: 由 source stream 和消息 ID 生成的永久幂等键。
            target_group_ids: 当前路由要求覆盖的目标 QQ 群号列表。
            required_stage: 每个目标至少需要达到的阶段；是否触发 Planner
                会影响调用方选择的阶段。

        Returns:
            任务存在且每个目标阶段均不低于 ``required_stage`` 时返回
            ``True``。状态缺失、格式异常或任一目标未完成时返回 ``False``。
        """

        job_state = self._jobs.get(state_key)
        if not isinstance(job_state, dict):
            return False
        target_states = job_state.get("targets")
        if not isinstance(target_states, dict):
            return False
        return all(
            TargetStage.from_value(str(target_states.get(group_id) or "pending")) >= required_stage
            for group_id in target_group_ids
        )

    def get_target_stage(self, state_key: str, target_group_id: str) -> TargetStage:
        """读取单个目标群已经完成的最高阶段。

        Args:
            state_key: 转发任务的稳定幂等键。
            target_group_id: 要查询的目标 QQ 群号。

        Returns:
            已持久化的 ``TargetStage``。任务、目标或阶段值不存在或无效时
            返回 ``TargetStage.PENDING``。
        """

        job_state = self._jobs.get(state_key)
        if not isinstance(job_state, dict):
            return TargetStage.PENDING
        target_states = job_state.get("targets")
        if not isinstance(target_states, dict):
            return TargetStage.PENDING
        return TargetStage.from_value(str(target_states.get(target_group_id) or "pending"))

    def get_target_message_id(self, state_key: str, target_group_id: str) -> str | None:
        """读取单个目标群已经持久化的平台最终消息 ID。

        Args:
            state_key: 转发任务的稳定幂等键。
            target_group_id: 要查询的目标 QQ 群号。

        Returns:
            已持久化的非空目标消息 ID；旧状态或无详细发送结果时返回
            ``None``。
        """

        job_state = self._jobs.get(state_key)
        if not isinstance(job_state, dict):
            return None
        target_message_ids = job_state.get("target_message_ids")
        if not isinstance(target_message_ids, dict):
            return None
        message_id = str(target_message_ids.get(target_group_id) or "").strip()
        return message_id or None

    def is_forwarded_target_message(self, target_group_id: str, message_id: str) -> bool:
        """判断一条入站消息是否是插件此前发送到该 target 的真实消息。

        该查询使用已经持久化的平台最终目标消息 ID，可在适配器未提供
        ``self_id`` 标记时继续拦截已知的机器人转发回声。旧 Host 未返回
        最终 ID 时无法命中，本方法不会根据内容或 source 消息 ID 猜测。

        Args:
            target_group_id: 当前入站消息所在的 QQ 群号。
            message_id: 当前入站消息的平台最终消息 ID。

        Returns:
            任一历史任务在该 target 群保存了相同消息 ID 时返回 ``True``；
            参数为空或没有匹配记录时返回 ``False``。
        """

        normalized_group_id = str(target_group_id or "").strip()
        normalized_message_id = str(message_id or "").strip()
        if not normalized_group_id or not normalized_message_id:
            return False
        for job_state in self._jobs.values():
            target_message_ids = job_state.get("target_message_ids")
            if not isinstance(target_message_ids, dict):
                continue
            stored_message_id = str(target_message_ids.get(normalized_group_id) or "").strip()
            if stored_message_id == normalized_message_id:
                return True
        return False

    async def ensure_job(self, job: ForwardJob) -> None:
        """确保任务元数据存在，并将状态写入磁盘。

        已存在任务的目标列表会合并但不会覆盖阶段，因此新增 target、重试
        或插件重载都能保留各目标已经完成的阶段。

        Args:
            job: 即将执行的不可变转发任务快照。
        """

        async with self._lock:
            job_state = self._jobs.setdefault(job.state_key, self._new_job_state(job))
            self._merge_job_metadata(job_state, job)
            await asyncio.to_thread(self._save_sync)

    async def advance_target(
        self,
        job: ForwardJob,
        target_group_id: str,
        stage: TargetStage,
    ) -> None:
        """单向提升目标群阶段，并在发生变化时持久化。

        该方法不会回退阶段。传入阶段不高于当前阶段时不写磁盘，从而避免
        重复 capability 回调破坏幂等状态。

        Args:
            job: 目标所属的转发任务快照。
            target_group_id: 已完成某一步骤的目标 QQ 群号。
            stage: 本次确认完成的新阶段。
        """

        async with self._lock:
            job_state = self._jobs.setdefault(job.state_key, self._new_job_state(job))
            self._merge_job_metadata(job_state, job)
            target_states = job_state.setdefault("targets", {})
            current_stage = TargetStage.from_value(str(target_states.get(target_group_id) or "pending"))
            if stage > current_stage:
                target_states[target_group_id] = stage.storage_value
                job_state["updated_at"] = time.time()
                await asyncio.to_thread(self._save_sync)

    async def record_target_sent(
        self,
        job: ForwardJob,
        target_group_id: str,
        target_message_id: str | None,
    ) -> None:
        """原子记录目标已发送阶段及平台最终消息 ID。

        新版 Host 提供目标消息 ID 时，本方法将 ID 与 ``SENT`` 阶段放在同一
        次状态写入中；旧 Host 未提供 ID 时仍只推进阶段，避免兼容发送被
        重复执行。

        Args:
            job: 目标所属的转发任务快照。
            target_group_id: 已完成物理发送的目标 QQ 群号。
            target_message_id: 平台最终目标消息 ID；旧 Host 未提供时为
                ``None``。
        """

        normalized_message_id = str(target_message_id or "").strip()
        async with self._lock:
            job_state = self._jobs.setdefault(job.state_key, self._new_job_state(job))
            self._merge_job_metadata(job_state, job)
            target_states = job_state.setdefault("targets", {})
            current_stage = TargetStage.from_value(str(target_states.get(target_group_id) or "pending"))
            changed = False
            if current_stage < TargetStage.SENT:
                target_states[target_group_id] = TargetStage.SENT.storage_value
                changed = True
            if normalized_message_id:
                target_message_ids = job_state.setdefault("target_message_ids", {})
                if target_message_ids.get(target_group_id) != normalized_message_id:
                    target_message_ids[target_group_id] = normalized_message_id
                    changed = True
            if changed:
                job_state["updated_at"] = time.time()
                await asyncio.to_thread(self._save_sync)

    @staticmethod
    def _new_job_state(job: ForwardJob) -> dict[str, Any]:
        """根据任务快照构造新的 JSON 可序列化状态。

        Args:
            job: 用于复制 source、消息和目标路由元数据的转发任务。

        Returns:
            包含创建/更新时间、空目标阶段映射和任务元数据的状态字典。
        """

        now = time.time()
        return {
            "job_id": job.job_id,
            "source_stream_id": job.source_stream_id,
            "source_group_id": job.source_group_id,
            "source_message_id": job.source_message_id,
            "target_group_ids": list(job.target_group_ids),
            "created_at": now,
            "updated_at": now,
            "targets": {},
            "target_message_ids": {},
        }

    @staticmethod
    def _merge_job_metadata(job_state: dict[str, Any], job: ForwardJob) -> None:
        """把任务快照中的新 target 合并进已有永久状态。

        Args:
            job_state: 将被原地更新的已持久化任务字典。
            job: 当前请求生成的任务快照。
        """

        existing_targets = job_state.get("target_group_ids")
        target_group_ids = (
            [str(group_id) for group_id in existing_targets] if isinstance(existing_targets, list) else []
        )
        seen = set(target_group_ids)
        for group_id in job.target_group_ids:
            if group_id not in seen:
                target_group_ids.append(group_id)
                seen.add(group_id)
        job_state["target_group_ids"] = target_group_ids
        job_state["job_id"] = job.job_id
        job_state["source_stream_id"] = job.source_stream_id
        job_state["source_group_id"] = job.source_group_id
        job_state["source_message_id"] = job.source_message_id

    def _load_sync(self) -> None:
        """同步读取并校验状态文件的顶层任务映射。

        文件不存在或读取失败时将内存状态设置为空字典。格式合法时只保留
        键可转为字符串且值为字典的任务记录，忽略其他损坏条目。
        """

        if not self._path.is_file():
            self._jobs = {}
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self._logger.warning("读取转发状态失败，将使用空状态: %s", exc)
            self._jobs = {}
            return
        raw_jobs = payload.get("jobs") if isinstance(payload, dict) else None
        loaded_jobs = (
            {str(key): value for key, value in raw_jobs.items() if isinstance(value, dict)}
            if isinstance(raw_jobs, dict)
            else {}
        )
        self._jobs = self._migrate_jobs(loaded_jobs)

    def _migrate_jobs(
        self,
        loaded_jobs: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        """把旧路由级任务合并为 source 消息级永久状态。

        `v0.1.11` 的键包含完整 target 集合，因此路由调整可能产生多个任务。
        迁移依据每条记录内的 ``source_stream_id`` 和 ``source_message_id``
        生成新键，并逐 target 保留最高阶段。缺少这两个字段的未知记录保持
        原键，避免静默丢弃用户状态。

        Args:
            loaded_jobs: 从状态文件读取并完成顶层类型校验的任务字典。

        Returns:
            使用新稳定键归并后的任务字典。
        """

        migrated: dict[str, dict[str, Any]] = {}
        for original_key, raw_job in loaded_jobs.items():
            source_stream_id = str(raw_job.get("source_stream_id") or "").strip()
            source_message_id = str(raw_job.get("source_message_id") or "").strip()
            if not source_stream_id or not source_message_id:
                migrated[original_key] = dict(raw_job)
                continue

            state_key = self.build_state_key(source_stream_id, source_message_id)
            normalized = self._normalize_loaded_job(raw_job, state_key)
            existing = migrated.get(state_key)
            if existing is None:
                migrated[state_key] = normalized
            else:
                self._merge_loaded_job(existing, normalized)
        return migrated

    @staticmethod
    def _normalize_loaded_job(
        raw_job: dict[str, Any],
        state_key: str,
    ) -> dict[str, Any]:
        """规范化一条旧任务并改写为新任务 ID。

        Args:
            raw_job: 从旧状态文件读取的单条任务字典。
            state_key: 已根据 source stream 和消息 ID 生成的新键。

        Returns:
            可写入新版状态文件的浅拷贝，目标列表和阶段映射已经清洗。
        """

        normalized = dict(raw_job)
        raw_targets = raw_job.get("targets")
        targets = (
            {str(group_id): str(stage) for group_id, stage in raw_targets.items()}
            if isinstance(raw_targets, dict)
            else {}
        )
        raw_target_ids = raw_job.get("target_group_ids")
        target_group_ids = [str(group_id) for group_id in raw_target_ids] if isinstance(raw_target_ids, list) else []
        for group_id in targets:
            if group_id not in target_group_ids:
                target_group_ids.append(group_id)
        normalized["job_id"] = state_key.rsplit(":", 1)[-1]
        normalized["target_group_ids"] = target_group_ids
        normalized["targets"] = targets
        raw_target_message_ids = raw_job.get("target_message_ids")
        normalized["target_message_ids"] = (
            {
                str(group_id): str(message_id)
                for group_id, message_id in raw_target_message_ids.items()
                if message_id is not None and str(message_id).strip()
            }
            if isinstance(raw_target_message_ids, dict)
            else {}
        )
        return normalized

    @staticmethod
    def _merge_loaded_job(
        destination: dict[str, Any],
        incoming: dict[str, Any],
    ) -> None:
        """把同一 source 消息的另一条旧路由记录合并到目标状态。

        Args:
            destination: 将被原地更新的新格式任务状态。
            incoming: 要合并的另一条规范化旧任务状态。
        """

        destination_targets = destination.setdefault("targets", {})
        incoming_targets = incoming.get("targets")
        if isinstance(destination_targets, dict) and isinstance(incoming_targets, dict):
            for group_id, stage_value in incoming_targets.items():
                current = TargetStage.from_value(str(destination_targets.get(group_id) or "pending"))
                incoming_stage = TargetStage.from_value(str(stage_value))
                if incoming_stage > current:
                    destination_targets[group_id] = incoming_stage.storage_value

        destination_message_ids = destination.setdefault("target_message_ids", {})
        incoming_message_ids = incoming.get("target_message_ids")
        if isinstance(destination_message_ids, dict) and isinstance(incoming_message_ids, dict):
            for group_id, message_id in incoming_message_ids.items():
                normalized_message_id = str(message_id).strip()
                if normalized_message_id and not str(destination_message_ids.get(group_id) or "").strip():
                    destination_message_ids[group_id] = normalized_message_id

        destination_ids = destination.setdefault("target_group_ids", [])
        incoming_ids = incoming.get("target_group_ids")
        if isinstance(destination_ids, list) and isinstance(incoming_ids, list):
            seen = {str(group_id) for group_id in destination_ids}
            for group_id in incoming_ids:
                normalized_group_id = str(group_id)
                if normalized_group_id not in seen:
                    destination_ids.append(normalized_group_id)
                    seen.add(normalized_group_id)

        destination["created_at"] = min(
            ForwardStateStore._timestamp(destination.get("created_at"), time.time()),
            ForwardStateStore._timestamp(incoming.get("created_at"), time.time()),
        )
        destination["updated_at"] = max(
            ForwardStateStore._timestamp(destination.get("updated_at"), 0.0),
            ForwardStateStore._timestamp(incoming.get("updated_at"), 0.0),
        )

    @staticmethod
    def _timestamp(value: Any, default: float) -> float:
        """把状态文件中的时间字段安全转换为浮点数。

        Args:
            value: 可能来自旧状态文件的任意时间字段值。
            default: 缺失、非数字或不可转换时采用的回退值。

        Returns:
            可用于比较的浮点时间戳。
        """

        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _save_sync(self) -> None:
        """同步地以临时文件替换方式保存状态快照。

        该方法创建父目录，将 JSON 写入同目录下的 ``.tmp`` 文件，再调用
        ``Path.replace`` 替换正式文件，降低进程中断留下半写文件的概率。

        Raises:
            OSError: 当目录创建、临时文件写入或原子替换失败时向调用方传播。
        """

        self._path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self._path.with_suffix(".tmp")
        payload = {
            "version": 3,
            "updated_at": time.time(),
            "jobs": self._jobs,
        }
        temp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temp_path.replace(self._path)
