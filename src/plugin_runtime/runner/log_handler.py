"""Runner 端 IPC 日志 Handler

将 Runner 进程内所有 stdlib logging 日志通过 IPC 批量发送到 Host，
Host 端将其重放到主进程的 Logger（以 plugin.<name> 为名）中，从而
统一在主进程的结构化日志体系中显示插件日志。

架构：
    Runner 进程
      └── logging.root  ←  RunnerIPCLogHandler（本文件）
            └── emit() 非阻塞入缓冲 → 后台刷新协程批量发送
                  └── rpc_client.send_event("runner.log_batch", ...)
                        └── IPC socket → Host
                              └── RunnerLogBridge.handle_log_batch()
                                    └── logging.getLogger("plugin.<name>").handle(record)

设计原则：
- emit() 必须是非阻塞的，不得在热路径上 await 任何 IPC 调用
- 使用 collections.deque(maxlen=QUEUE_MAX) 作为有界环形缓冲：
  满时最旧条目自动被覆盖（不区分级别，为实现简单接受此折损）
- CPython 的 deque.append / deque.popleft 在 GIL 保护下是线程安全的，
  适合单消费后台协程 + 多生产线程的使用场景
- 后台刷新协程每 FLUSH_INTERVAL_SEC 秒或 FLUSH_BATCH_SIZE 条后批量发送
- IPC 发送失败时静默忽略；stderr fallback 由 supervisor 的 drain task 覆盖
"""

from __future__ import annotations

from typing import TYPE_CHECKING, List, Optional

import asyncio
import collections
import contextlib
import json
import logging

from src.plugin_runtime.protocol.envelope import LogBatchPayload, LogEntry

if TYPE_CHECKING:
    from src.plugin_runtime.runner.rpc_client import RPCClient


class RunnerIPCLogHandler(logging.Handler):
    """将 Runner 进程内所有日志通过 IPC 批量转发到 Host 主进程。

    典型用法::

        handler = RunnerIPCLogHandler()
        handler.start(rpc_client, asyncio.get_running_loop())
        logging.root.addHandler(handler)
        # ... 进程运行 ...
        logging.root.removeHandler(handler)
        await handler.stop()
    """

    #: 日志缓冲最大条数；超出后最旧的条目将被静默丢弃（deque(maxlen) 行为）
    QUEUE_MAX: int = 200

    #: 后台刷新循环的休眠间隔（秒）
    FLUSH_INTERVAL_SEC: float = 0.1

    #: 每次 send_event 携带的最大日志条数
    FLUSH_BATCH_SIZE: int = 20

    #: 仅转发 logger name 以这些前缀开头的日志，第三方库日志将被忽略
    #: 包含 "_maibot_plugin_" 前缀以覆盖插件模块中 logging.getLogger(__name__) 的场景
    ALLOWED_LOGGER_PREFIXES: tuple[str, ...] = ("plugin.", "plugin_runtime.", "_maibot_plugin_")

    def __init__(self) -> None:
        """初始化 Runner 端日志转发处理器。

        创建有界日志缓冲区，并准备与 RPC 客户端绑定的后台刷新任务。
        此时不会启动任何异步任务；真正开始转发要等到 :meth:`start`
        被调用后才会发生。
        """
        super().__init__()
        # deque(maxlen=N): append/popleft 在 CPython GIL 保护下线程安全
        self._buffer: collections.deque[LogEntry] = collections.deque(maxlen=self.QUEUE_MAX)
        self._rpc_client: Optional[RPCClient] = None
        self._flush_task: Optional[asyncio.Task[None]] = None

    # ─── 公开 API ──────────────────────────────────────────────────

    def start(self, rpc_client: RPCClient, loop: asyncio.AbstractEventLoop) -> None:
        """握手完成后、在事件循环内调用，启动后台刷新任务。

        Args:
            rpc_client: 已完成握手的 RPCClient 实例。
            loop:       当前运行的 asyncio 事件循环。
        """
        self._rpc_client = rpc_client
        self._flush_task = loop.create_task(
            self._flush_loop(),
            name="RunnerIPCLogHandler._flush_loop",
        )

    async def stop(self) -> None:
        """停止刷新任务并将缓冲中剩余日志全部发送出去。

        应在 ``logging.root.removeHandler(handler)`` 之后调用，
        确保 emit() 不会在 stop() 执行期间向已消耗的缓冲写入新条目。
        """
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None

        # 关闭前全量刷新，分多批次直到缓冲清空
        await self._flush_remaining()

    # ─── logging.Handler 接口 ──────────────────────────────────────

    def emit(self, record: logging.LogRecord) -> None:
        """将一条 LogRecord 序列化后放入缓冲（同步，永不阻塞）。

        仅转发 logger name 匹配 ``ALLOWED_LOGGER_PREFIXES`` 的日志，
        第三方库日志被静默忽略，避免噪声淹没插件日志。
        缓冲已满时，deque 自动从左侧丢弃最旧条目（FIFO 溢出）。
        异常通过 ``self.handleError(record)`` 写到 stderr，不引发。
        """
        try:
            # 过滤：仅允许插件相关的 logger，跳过第三方库日志
            if not any(record.name.startswith(p) for p in self.ALLOWED_LOGGER_PREFIXES):
                return

            # structlog 透传到 stdlib logging 时，record.msg 往往是 event_dict。
            # 这里先提取可读的 event 文本，避免 Host 侧收到一整段 dict 字符串。
            msg = self._serialize_message(record)
            entry = LogEntry(
                timestamp_ms=int(record.created * 1000),
                level=record.levelno,
                logger_name=record.name,
                message=msg,
                exception_text=record.exc_text or "",
            )
            self._buffer.append(entry)
        except Exception:
            self.handleError(record)

    def _serialize_message(self, record: logging.LogRecord) -> str:
        """将 LogRecord 序列化为适合 Host 重放的纯文本消息。"""
        if isinstance(record.msg, dict):
            event_dict = record.msg
            event_text = self._stringify_value(event_dict.get("event", ""))
            extras = []
            ignored_keys = {
                "event",
                "logger",
                "logger_name",
                "level",
                "timestamp",
                "module",
                "lineno",
                "pathname",
                "_from_structlog",
                "_record",
            }
            for key, value in event_dict.items():
                if key in ignored_keys:
                    continue
                extras.append(f"{key}={self._stringify_value(value)}")

            if extras:
                return f"{event_text} {' '.join(extras)}".strip()
            return event_text

        # format() 会处理占位参数替换和 exc_info 文本拼接。
        return self.format(record)

    @staticmethod
    def _stringify_value(value: object) -> str:
        """将结构化字段转换为紧凑字符串。"""
        if isinstance(value, (dict, list)):
            try:
                return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                return str(value)
        return str(value)

    # ─── 内部方法 ──────────────────────────────────────────────────

    async def _flush_loop(self) -> None:
        """后台批量刷新循环——每 FLUSH_INTERVAL_SEC 秒醒来一次。"""
        while True:
            try:
                await asyncio.sleep(self.FLUSH_INTERVAL_SEC)
                await self._flush_batch(self.FLUSH_BATCH_SIZE)
            except asyncio.CancelledError:
                break
            except Exception:
                # 任何发送侧错误都静默忽略，避免向 logging 写入导致嵌套循环
                pass

    async def _flush_batch(self, max_count: int) -> None:
        """从缓冲中取出最多 max_count 条日志并通过 IPC 发送一个批次。

        Args:
            max_count: 本次最多发送的条目数。
        """
        if not self._buffer or self._rpc_client is None:
            return

        entries: List[LogEntry] = []
        while self._buffer and len(entries) < max_count:
            entries.append(self._buffer.popleft())

        if not entries:
            return

        # IPC 连接断开时回退到 stderr，避免日志静默丢失
        if not self._rpc_client.is_connected:
            import sys

            for entry in entries:
                print(
                    f"[LOG-FALLBACK] [{entry.logger_name}] {entry.message}",
                    file=sys.stderr,
                )
            return

        # IPC 发送失败时回退到 stderr
        try:
            await self._rpc_client.send_event(
                "runner.log_batch",
                payload=LogBatchPayload(entries=entries).model_dump(),
            )
        except Exception:
            import sys

            for entry in entries:
                print(
                    f"[LOG-FALLBACK] [{entry.logger_name}] {entry.message}",
                    file=sys.stderr,
                )

    async def _flush_remaining(self) -> None:
        """将缓冲中剩余的所有条目分批全部发送。"""
        while self._buffer:
            await self._flush_batch(self.FLUSH_BATCH_SIZE)
