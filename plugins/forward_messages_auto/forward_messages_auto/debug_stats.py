"""仅供本地开发调试的合并转发触发统计。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from .source.authorization import (
    FORWARD_CONTEXT_TOKEN_ARGUMENT,
    FORWARD_TOOL_NAME,
)
from .source.message import SourceForwardMessageParser

DEBUG_STATS_ENV_VAR = "MAIBOT_FORWARD_DEBUG_STATS"
DEBUG_STATS_FILENAME = "debug_forward_stats.local.json"
DEBUG_STATS_SCHEMA_VERSION = 1
_ENABLED_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def debug_stats_enabled_from_environment() -> bool:
    """读取开发环境变量并判断是否启用本地调试统计。

    只有 ``1``、``true``、``yes`` 和 ``on``（不区分大小写）会启用统计；
    变量缺失、空值或其他值均保持默认关闭。

    Returns:
        当前进程是否显式启用了本地调试统计。
    """

    value = str(os.environ.get(DEBUG_STATS_ENV_VAR) or "").strip().lower()
    return value in _ENABLED_ENV_VALUES


@dataclass(frozen=True, slots=True)
class DebugForwardStatsSnapshot:
    """一个插件版本当前可展示的调试统计快照。"""

    version: str
    observed: int
    requested: int

    @property
    def ratio(self) -> float:
        """计算 Planner 发起请求数占已观察消息数的比例。

        Returns:
            分母非零时返回 ``requested / observed``；尚未观察到消息时返回
            ``0.0``。
        """

        if self.observed == 0:
            return 0.0
        return self.requested / self.observed

    def display(self) -> str:
        """生成设计文档约定的计数与百分比展示文本。

        整数百分比不显示小数；其他比例保留一位小数。该文本只用于本地调试
        日志，不会进入 Planner 上下文或 ToolResult。

        Returns:
            形如 ``13/25（52%）`` 的本地调试统计文本。
        """

        percentage = self.ratio * 100
        if percentage.is_integer():
            percentage_text = str(int(percentage))
        else:
            percentage_text = f"{percentage:.1f}".rstrip("0").rstrip(".")
        return f"{self.requested}/{self.observed}（{percentage_text}%）"


@dataclass(slots=True)
class _VersionStats:
    """一个插件版本用于跨重载去重的匿名消息键集合。"""

    observed_keys: set[str] = field(default_factory=set)
    requested_keys: set[str] = field(default_factory=set)


class DebugForwardStatsStore:
    """隔离调试统计的内存去重、后台持久化与安全降级。"""

    def __init__(
        self,
        path: Path,
        source_root: Path,
        plugin_version: str,
        logger: Any,
        *,
        enabled: bool | None = None,
    ) -> None:
        """创建尚未读取磁盘的调试统计存储。

        构造阶段不执行文件 I/O，也不创建后台任务。``start`` 会在显式启用
        时校验统计路径、加载旧版本桶并启动串行写入 worker；读取失败或路径
        位于源码树内时，本生命周期保持关闭。

        Args:
            path: 预期位于 MaiBot 插件运行时数据目录的统计文件路径。
            source_root: 当前插件源码工作树根目录，用于拒绝仓库内写入。
            plugin_version: 当前插件版本，作为观察与请求去重桶的名称。
            logger: MaiBot 插件日志对象，只记录本地调试摘要和降级警告。
            enabled: 显式启停覆盖；为 ``None`` 时读取
                ``MAIBOT_FORWARD_DEBUG_STATS`` 环境变量。
        """

        self._path = Path(path)
        self._source_root = Path(source_root)
        self._plugin_version = str(plugin_version or "").strip()
        self._logger = logger
        self._configured_enabled = debug_stats_enabled_from_environment() if enabled is None else bool(enabled)
        self._versions: dict[str, _VersionStats] = {}
        self._active = False
        self._started = False
        self._stopping = False
        self._dirty = False
        self._revision = 0
        self._save_event: asyncio.Event | None = None
        self._writer_task: asyncio.Task[None] | None = None
        self._deferred_warning = ""

    @property
    def enabled(self) -> bool:
        """返回统计是否已完成安全初始化且仍接受新事件。

        Returns:
            显式开关开启、路径和旧文件校验成功且 writer 未失败时返回
            ``True``。
        """

        return self._active and not self._stopping

    async def start(self) -> bool:
        """按需加载本地统计并启动后台写入 worker。

        默认关闭时方法不读取、创建或探测统计文件。显式启用后，读取和路径
        校验在线程中完成；任何异常只记录 warning 并关闭统计，不向插件加载
        流程传播。

        Returns:
            统计已经安全启用时返回 ``True``；默认关闭、重复启动或降级关闭时
            返回当前启用状态。
        """

        if self._started:
            return self.enabled
        self._started = True
        if not self._configured_enabled:
            return False
        if not self._plugin_version:
            self._log_warning("调试统计缺少插件版本，已跳过本生命周期统计")
            return False

        try:
            self._versions = await asyncio.to_thread(self._load_sync)
        except Exception as exc:
            self._log_warning(
                "调试统计加载失败，已跳过本生命周期统计且不会覆盖原文件: path=%s error=%s",
                self._path,
                exc,
            )
            return False

        self._save_event = asyncio.Event()
        self._active = True
        self._writer_task = asyncio.create_task(
            self._writer_loop(),
            name="forward-debug-stats-writer",
        )
        self._log_debug(
            "跨群转发调试统计已启用: version=%s stats=%s path=%s",
            self._plugin_version,
            self.snapshot().display(),
            self._path,
        )
        return True

    async def stop(self) -> None:
        """停止接受新事件，并等待已登记统计完成最后一次原子保存。

        默认关闭、加载失败或 writer 已经退出时允许重复调用。保存或 worker
        异常会在内部转成 warning，不影响插件卸载。
        """

        self._stopping = True
        task = self._writer_task
        event = self._save_event
        if task is None or event is None:
            self._active = False
            return

        event.set()
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log_warning("调试统计 worker 停止异常，已忽略: error=%s", exc)
        finally:
            self._writer_task = None
            self._save_event = None
            self._active = False

    def record_source_message(
        self,
        message: Any,
        source_groups: Iterable[str],
        is_forwarded_target_message: Callable[[str, str], bool],
    ) -> bool:
        """登记 source 白名单观察到的唯一外部 QQ 合并转发。

        解析、白名单和插件转发回声校验均在内存中完成，不等待消息落库，也不
        依赖 source Planner 强制触发开关。异常会关闭本生命周期统计并由后台
        worker 记录本地 warning，不影响原入站 Hook。

        Args:
            message: ``chat.receive.after_process`` 提供的序列化入站消息。
            source_groups: 当前配置中规范化后的 source QQ 群号。
            is_forwarded_target_message: 根据群号和消息 ID 识别插件历史转发
                回声的只读回调。

        Returns:
            当前版本首次观察到该 ``source stream + msg_id`` 时返回 ``True``；
            统计关闭、消息不符合口径、回声或重复事件返回 ``False``。
        """

        if not self.enabled:
            return False
        try:
            observation = SourceForwardMessageParser.parse(message)
            if observation is None:
                return False
            if observation.group_id not in set(source_groups):
                return False
            if is_forwarded_target_message(observation.group_id, observation.message_id):
                return False
            return self.record_observed(
                observation.stream_id,
                observation.message_id,
            )
        except Exception as exc:
            self._defer_warning_and_disable(
                f"记录合并转发观察统计失败，已关闭本生命周期统计且不影响入站处理: error={exc}"
            )
            return False

    def record_requested_calls(self, session_id: str, tool_calls: Any) -> int:
        """从 EARLY 清洗结果登记 Planner 原始转发请求。

        方法只扫描已由授权门清洗且成功签发一次性凭据的转发调用，并要求同一
        版本的 ``session_id + msg_id`` 已先进入 observed 集合。方法仅更新
        内存集合和唤醒事件，不读写文件、不等待后台 worker，也不写日志，因而
        可安全用于必须无 I/O 的 EARLY Hook。路径 B 后续由插件恢复的调用不会
        再经过该入口。

        Args:
            session_id: Host 提供的真实 Planner 会话 ID。
            tool_calls: ``ForwardInvocationGate`` 清洗并授权后的工具调用列表。

        Returns:
            本批次首次计入 requested 集合的唯一消息数量。
        """

        if not self.enabled or not isinstance(tool_calls, list):
            return 0
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return 0

        added = 0
        try:
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                if str(function.get("name") or "").strip() != FORWARD_TOOL_NAME:
                    continue
                arguments = function.get("arguments")
                if not isinstance(arguments, dict):
                    continue
                message_id = str(arguments.get("msg_id") or "").strip()
                authorization = str(arguments.get(FORWARD_CONTEXT_TOKEN_ARGUMENT) or "").strip()
                if not message_id or not authorization:
                    continue
                if self.record_requested(normalized_session_id, message_id):
                    added += 1
        except Exception as exc:
            self._defer_warning_and_disable(f"记录 Planner 转发请求统计失败，已关闭本生命周期统计: error={exc}")
        return added

    def record_observed(self, stream_id: str, message_id: str) -> bool:
        """把唯一消息加入当前版本 observed 集合并安排后台保存。

        Args:
            stream_id: 观察到消息的 source 聊天流 ID。
            message_id: Host 提供的 source 消息 ID。

        Returns:
            两个标识均有效且匿名键首次加入集合时返回 ``True``；统计关闭、
            输入为空或重复时返回 ``False``。
        """

        if not self.enabled:
            return False
        key = self._build_anonymous_key(stream_id, message_id)
        if not key:
            return False
        bucket = self._versions.setdefault(self._plugin_version, _VersionStats())
        if key in bucket.observed_keys:
            return False
        bucket.observed_keys.add(key)
        self._mark_dirty()
        return True

    def record_requested(self, stream_id: str, message_id: str) -> bool:
        """把已观察消息加入当前版本 requested 集合并安排后台保存。

        Args:
            stream_id: EARLY Hook 提供的真实 source Planner 会话 ID。
            message_id: Planner 转发调用声明且已绑定凭据的 source 消息 ID。

        Returns:
            匿名键已存在于同版本 observed 集合且首次加入 requested 时返回
            ``True``；未观察、重复、输入无效或统计关闭时返回 ``False``。
        """

        if not self.enabled:
            return False
        key = self._build_anonymous_key(stream_id, message_id)
        if not key:
            return False
        bucket = self._versions.setdefault(self._plugin_version, _VersionStats())
        if key not in bucket.observed_keys or key in bucket.requested_keys:
            return False
        bucket.requested_keys.add(key)
        self._mark_dirty()
        return True

    def snapshot(self, version: str | None = None) -> DebugForwardStatsSnapshot:
        """读取指定版本的聚合计数而不暴露匿名消息键。

        Args:
            version: 要读取的插件版本；省略或传入空值时使用当前插件版本。

        Returns:
            包含版本、唯一观察数和唯一请求数的不可变快照。未知版本返回零值。
        """

        normalized_version = str(version or self._plugin_version).strip()
        bucket = self._versions.get(normalized_version, _VersionStats())
        return DebugForwardStatsSnapshot(
            version=normalized_version,
            observed=len(bucket.observed_keys),
            requested=len(bucket.requested_keys),
        )

    def _mark_dirty(self) -> None:
        """推进内存修订号并唤醒后台 writer，不在调用线程执行 I/O。"""

        self._revision += 1
        self._dirty = True
        if self._save_event is not None:
            self._save_event.set()

    def _defer_warning_and_disable(self, warning: str) -> None:
        """无日志 I/O 地关闭统计，并让后台 worker 稍后记录警告。

        Args:
            warning: 待在 EARLY Hook 让出控制后写入本地日志的完整警告文本。
        """

        self._active = False
        self._dirty = False
        self._deferred_warning = warning
        if self._save_event is not None:
            self._save_event.set()

    async def _writer_loop(self) -> None:
        """串行保存内存修订，并在并发更新发生时继续写入最新快照。

        writer 只在事件循环重新取得控制权后调用线程执行磁盘 I/O。单次写入
        期间若有新事件推进修订号，旧快照完成后会立即再写一次；保存异常会
        关闭统计并记录 warning，不向 Hook、Tool handler 或卸载流程传播。
        """

        event = self._save_event
        if event is None:
            return
        try:
            while True:
                await event.wait()
                event.clear()

                if self._deferred_warning:
                    warning = self._deferred_warning
                    self._deferred_warning = ""
                    self._log_warning("%s", warning)
                if not self._active:
                    return

                while self._dirty:
                    revision = self._revision
                    payload = self._serialize()
                    try:
                        await asyncio.to_thread(self._write_sync, payload)
                    except Exception as exc:
                        self._active = False
                        self._dirty = False
                        self._log_warning(
                            "调试统计保存失败，已关闭本生命周期统计且不影响正常转发: path=%s error=%s",
                            self._path,
                            exc,
                        )
                        return
                    if revision == self._revision:
                        self._dirty = False
                    self._log_debug(
                        "跨群转发调试统计已更新: version=%s stats=%s",
                        self._plugin_version,
                        self.snapshot().display(),
                    )

                if self._stopping:
                    return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._active = False
            self._dirty = False
            self._log_warning(
                "调试统计 worker 异常，已关闭本生命周期统计且不影响正常转发: error=%s",
                exc,
            )

    def _load_sync(self) -> dict[str, _VersionStats]:
        """在工作线程中校验路径并读取全部历史版本桶。

        Returns:
            从安全路径解析出的版本到匿名键集合映射；文件不存在时返回空字典。

        Raises:
            OSError: 统计文件无法读取时抛出。
            ValueError: 路径落入源码工作树、JSON 损坏或数据结构不合法时抛出。
        """

        safe_path = self._resolved_safe_path()
        if not safe_path.exists():
            return {}
        with safe_path.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        return self._deserialize(payload)

    def _write_sync(self, payload: dict[str, Any]) -> None:
        """在工作线程中把统计快照原子写入安全运行时目录。

        方法先创建父目录，再次解析实际路径，随后写入同目录临时文件并通过
        ``Path.replace`` 原子替换正式文件。失败时尽力删除临时文件，原有统计
        文件保持不变。

        Args:
            payload: 已脱离活动内存集合的可 JSON 序列化统计快照。

        Raises:
            OSError: 创建目录、写入、同步或替换失败时抛出。
            ValueError: 保存时解析出的路径位于插件源码工作树内时抛出。
        """

        safe_path = self._resolved_safe_path()
        safe_path.parent.mkdir(parents=True, exist_ok=True)
        safe_path = self._resolved_safe_path()
        temporary_path = safe_path.with_name(f".{safe_path.name}.{uuid4().hex}.tmp")
        try:
            with temporary_path.open("x", encoding="utf-8") as file:
                json.dump(
                    payload,
                    file,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            temporary_path.replace(safe_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _resolved_safe_path(self) -> Path:
        """解析统计路径并确认它不等于或位于插件源码工作树内。

        Returns:
            已解析符号链接和相对段的统计文件绝对路径。

        Raises:
            ValueError: 统计路径落入插件源码工作树时抛出。
        """

        source_root = self._source_root.resolve(strict=False)
        path = self._path.resolve(strict=False)
        try:
            path.relative_to(source_root)
        except ValueError:
            return path
        raise ValueError(f"统计路径位于插件源码工作树内: {path}")

    def _serialize(self) -> dict[str, Any]:
        """把内存集合复制成包含聚合计数的稳定 JSON 数据结构。

        Returns:
            带 schema 版本和全部插件版本桶的字典。计数由匿名键集合长度实时
            计算，比例不持久化。
        """

        versions: dict[str, Any] = {}
        for version, bucket in sorted(self._versions.items()):
            observed_keys = sorted(bucket.observed_keys)
            requested_keys = sorted(bucket.requested_keys)
            versions[version] = {
                "observed": len(observed_keys),
                "requested": len(requested_keys),
                "observed_keys": observed_keys,
                "requested_keys": requested_keys,
            }
        return {
            "schema_version": DEBUG_STATS_SCHEMA_VERSION,
            "versions": versions,
        }

    @staticmethod
    def _deserialize(payload: Any) -> dict[str, _VersionStats]:
        """校验统计 JSON，并恢复各版本的匿名去重集合。

        Args:
            payload: ``json.load`` 返回的任意数据。

        Returns:
            校验通过的版本到匿名键集合映射。

        Raises:
            ValueError: schema 版本、版本桶、聚合计数、摘要格式或子集关系不合法
                时抛出。
        """

        if not isinstance(payload, dict):
            raise ValueError("统计文件根节点必须是对象")
        if payload.get("schema_version") != DEBUG_STATS_SCHEMA_VERSION:
            raise ValueError("统计文件 schema_version 不受支持")
        raw_versions = payload.get("versions")
        if not isinstance(raw_versions, dict):
            raise ValueError("统计文件缺少 versions 对象")

        versions: dict[str, _VersionStats] = {}
        for raw_version, raw_bucket in raw_versions.items():
            version = str(raw_version or "").strip()
            if not version or not isinstance(raw_bucket, dict):
                raise ValueError("统计文件包含非法版本桶")
            observed_keys = DebugForwardStatsStore._parse_anonymous_keys(
                raw_bucket.get("observed_keys"),
                "observed_keys",
            )
            requested_keys = DebugForwardStatsStore._parse_anonymous_keys(
                raw_bucket.get("requested_keys"),
                "requested_keys",
            )
            if not requested_keys.issubset(observed_keys):
                raise ValueError("requested_keys 必须是 observed_keys 的子集")
            if raw_bucket.get("observed") != len(observed_keys):
                raise ValueError("observed 计数与匿名键数量不一致")
            if raw_bucket.get("requested") != len(requested_keys):
                raise ValueError("requested 计数与匿名键数量不一致")
            versions[version] = _VersionStats(
                observed_keys=observed_keys,
                requested_keys=requested_keys,
            )
        return versions

    @staticmethod
    def _parse_anonymous_keys(value: Any, field_name: str) -> set[str]:
        """校验一个持久化匿名键列表并转换为集合。

        Args:
            value: 预期由 64 位小写十六进制摘要组成的 JSON 列表。
            field_name: 用于错误说明的字段名称。

        Returns:
            去重后的匿名摘要集合。

        Raises:
            ValueError: 输入不是列表、含重复项或摘要格式不正确时抛出。
        """

        if not isinstance(value, list):
            raise ValueError(f"{field_name} 必须是列表")
        keys: set[str] = set()
        for item in value:
            key = str(item or "")
            if len(key) != 64 or any(character not in "0123456789abcdef" for character in key):
                raise ValueError(f"{field_name} 包含非法匿名摘要")
            if key in keys:
                raise ValueError(f"{field_name} 包含重复匿名摘要")
            keys.add(key)
        return keys

    def _build_anonymous_key(self, stream_id: str, message_id: str) -> str:
        """为当前版本的 source 消息生成不可直接读取的稳定摘要。

        Args:
            stream_id: source 聊天流 ID。
            message_id: source 消息 ID。

        Returns:
            两个标识均有效时返回 64 位 SHA-256 十六进制摘要；否则返回空串。
        """

        normalized_stream_id = str(stream_id or "").strip()
        normalized_message_id = str(message_id or "").strip()
        if not normalized_stream_id or not normalized_message_id:
            return ""
        material = (
            f"maibot-forward-debug-stats-v{DEBUG_STATS_SCHEMA_VERSION}\0"
            f"{self._plugin_version}\0{normalized_stream_id}\0{normalized_message_id}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def _log_warning(self, message: str, *args: Any) -> None:
        """尽力写入统计 warning，避免日志替身异常影响正常流程。

        Args:
            message: 传给 logger 的格式字符串。
            *args: 由 logger 延迟插值的格式参数。
        """

        try:
            self._logger.warning(message, *args)
        except Exception:
            pass

    def _log_debug(self, message: str, *args: Any) -> None:
        """尽力写入不含消息标识的本地调试日志。

        Args:
            message: 传给 logger 的格式字符串。
            *args: 由 logger 延迟插值的格式参数。
        """

        try:
            self._logger.debug(message, *args)
        except Exception:
            pass
