"""回归测试：控制台被卡住时，print() 与日志都不能把进程拖死。

2026-11-19 实测：Windows 控制台被选中后写入会无限期阻塞，player-monitor 线程
卡在 print()、maibot-native-core 卡在日志轮转清理里的 print()，整条消息管线停摆。
"""
import logging
import threading
import time

from src.common.logger import NonBlockingStreamHandler, NonBlockingTextStream, TimestampedFileHandler


class _BlockingStream:
    """模拟被卡住的控制台：write 一直不返回。"""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self):
        self.release = threading.Event()
        self.chunks = []

    def write(self, text):
        self.release.wait()
        self.chunks.append(text)
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        return 1


def test_text_stream_write_never_blocks():
    raw = _BlockingStream()
    stream = NonBlockingTextStream(raw)

    started = time.monotonic()
    for index in range(2000):
        stream.write(f"line {index}\n")
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, f"写入被底层流阻塞了 {elapsed:.2f}s"
    assert stream.dropped >= 0


def test_print_through_wrapped_stream_does_not_block():
    raw = _BlockingStream()
    stream = NonBlockingTextStream(raw)

    def emit():
        for index in range(500):
            print(f"noise {index}", file=stream)

    worker = threading.Thread(target=emit)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=5)
    elapsed = time.monotonic() - started

    assert not worker.is_alive(), "print() 被卡住的控制台拖死了"
    assert elapsed < 2.0


def test_console_handler_emit_does_not_block():
    raw = _BlockingStream()
    handler = NonBlockingStreamHandler(raw)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("test.nonblocking.console")
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)
    logger.propagate = False

    started = time.monotonic()
    for index in range(500):
        logger.info("hello %s", index)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0


def test_file_handler_cleanup_does_not_print(tmp_path):
    """日志轮转清理跑在 handler 锁内，绝不能碰控制台。"""

    handler = TimestampedFileHandler(log_dir=tmp_path, max_bytes=1024, backup_count=1)
    try:
        for index in range(5):
            handler._init_current_file()
        handler._cleanup_old_files()
    finally:
        if handler.current_stream:
            handler.current_stream.close()
