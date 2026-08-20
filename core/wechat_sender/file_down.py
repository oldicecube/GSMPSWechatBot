#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件下载工具
"""

import os
import logging
import time
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path

import soundfile as sf

logger = logging.getLogger(__name__)


def download_file(url: str) -> str:
    """
    从HTTP/HTTPS地址下载文件到临时目录

    Args:
        url: 文件的HTTP/HTTPS地址

    Returns:
        下载后的本地临时文件路径
    """
    try:
        # 解析URL获取文件名
        parsed_url = urllib.parse.urlparse(url)
        filename = os.path.basename(parsed_url.path)
        if not filename:
            filename = 'downloaded_file'

        # 临时文件路径
        temp_dir = tempfile.gettempdir() + "\\WechatRobot"
        # 创建临时目录（如果不存在）
        Path(temp_dir).mkdir(parents=True, exist_ok=True)
        temp_path = os.path.join(temp_dir, filename)

        # 下载文件
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.0'
        }
        request = urllib.request.Request(url, headers=headers)

        with urllib.request.urlopen(request, timeout=30) as response:
            with open(temp_path, 'wb') as f:
                f.write(response.read())
        print(f"文件已下载到: {temp_path}")
        return temp_path
    except Exception as e:
        raise Exception(f"下载文件失败: {e}")


# 统一语音临时目录：所有准备发送为语音的音频文件都放到这里，
# 发送完成后由发送组件（wechat_hook_server）删除。
VOICE_TEMP_DIR = os.path.join(tempfile.gettempdir(), "WechatRobot", "voice")


def get_voice_temp_dir() -> str:
    """返回统一语音临时目录（不存在则创建）。"""
    Path(VOICE_TEMP_DIR).mkdir(parents=True, exist_ok=True)
    return VOICE_TEMP_DIR


def is_in_voice_temp_dir(path) -> bool:
    """判断路径是否位于统一语音临时目录内（Windows 路径不区分大小写）。"""
    if not path:
        return False
    try:
        real = os.path.normcase(os.path.abspath(os.path.normpath(str(path))))
        base = os.path.normcase(os.path.abspath(os.path.normpath(VOICE_TEMP_DIR)))
    except Exception:
        return False
    return real == base or real.startswith(base + os.sep)


def validate_audio_file(path):
    """验证音频文件能被虚拟麦克风使用的解码器读取。"""
    try:
        info = sf.info(str(path))
    except Exception as exc:
        raise ValueError(f"音频无法解码：{exc}") from exc

    frames = getattr(info, "frames", 0)
    sample_rate = getattr(info, "samplerate", 0)
    if frames <= 0 or sample_rate <= 0:
        raise ValueError(
            f"音频没有有效采样数据：frames={frames}, samplerate={sample_rate}"
        )
    return info


def _safe_url_for_log(url):
    try:
        parsed = urllib.parse.urlsplit(str(url))
        query_keys = sorted(
            {
                key
                for key, _ in urllib.parse.parse_qsl(
                    parsed.query,
                    keep_blank_values=True,
                )
                if key
            }
        )
        query_suffix = f"?keys={','.join(query_keys)}" if query_keys else ""
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}{query_suffix}"
    except Exception:
        return "<invalid-url>"


def _response_header(response, name):
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        return headers.get(name)
    except Exception:
        return None


def _info_value(info, name, default=None):
    if isinstance(info, Mapping):
        return info.get(name, default)
    return getattr(info, name, default)


def download_voice_file(
    url: str,
    prefix: str = "voice",
    headers=None,
    timeout: float = 60,
    source_id: str = "unknown",
) -> str:
    """下载音频文件到统一语音临时目录，返回本地路径。

    Args:
        url: 音频文件的 HTTP/HTTPS 地址
        prefix: 文件名前缀（用于区分来源，例如歌曲 ID）
        headers: 音源要求的额外 HTTP 请求头
        timeout: 下载超时秒数
        source_id: 日志中的音源 ID
    """
    get_voice_temp_dir()
    ext = ".mp3"
    try:
        parsed_url = urllib.parse.urlparse(url)
        path_ext = os.path.splitext(os.path.basename(parsed_url.path))[1].lower()
        if path_ext in (".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".amr"):
            ext = path_ext
    except Exception:
        pass

    filename = "%s_%d%s" % (prefix, int(time.time() * 1000), ext)
    temp_path = os.path.join(VOICE_TEMP_DIR, filename)
    safe_url = _safe_url_for_log(url)
    source_id = str(source_id or "unknown")
    started_at = time.monotonic()
    total_bytes = 0
    logger.info(
        "[VOICE DOWNLOAD] start source=%s url=%s timeout=%.1fs headers=%s",
        source_id,
        safe_url,
        float(timeout),
        sorted(str(key) for key in (headers or {}).keys()),
    )

    request_headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    if isinstance(headers, dict):
        request_headers.update(
            {
                str(key): str(value)
                for key, value in headers.items()
                if value is not None
            }
        )
    request = urllib.request.Request(url, headers=request_headers)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", None)
            if not isinstance(status, int):
                try:
                    status = response.getcode()
                except Exception:
                    status = None
            content_type = str(_response_header(response, "Content-Type") or "")
            content_length = _response_header(response, "Content-Length")
            final_url = _safe_url_for_log(
                response.geturl() if hasattr(response, "geturl") else url
            )
            logger.info(
                "[VOICE DOWNLOAD] response source=%s status=%s content_type=%s "
                "content_length=%s final_url=%s",
                source_id,
                status if status is not None else "unknown",
                content_type or "unknown",
                content_length or "unknown",
                final_url,
            )

            if status is not None and not 200 <= status < 300:
                raise RuntimeError(f"HTTP 状态码异常：{status}")

            normalized_content_type = content_type.split(";", 1)[0].strip().lower()
            if normalized_content_type in {
                "application/json",
                "text/html",
                "text/plain",
            }:
                raise ValueError(f"响应不是音频类型：{content_type}")

            with open(temp_path, 'wb') as f:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    f.write(chunk)

        if total_bytes <= 0:
            raise ValueError("下载结果为空")

        audio_info = validate_audio_file(temp_path)
        elapsed = time.monotonic() - started_at
        logger.info(
            "[VOICE DOWNLOAD] validated source=%s path=%s bytes=%d "
            "frames=%s samplerate=%s channels=%s duration=%.3fs elapsed=%.3fs",
            source_id,
            temp_path,
            total_bytes,
            _info_value(audio_info, "frames", "unknown"),
            _info_value(audio_info, "samplerate", "unknown"),
            _info_value(audio_info, "channels", "unknown"),
            float(_info_value(audio_info, "duration", 0.0) or 0.0),
            elapsed,
        )
        print(f"语音文件已下载到: {temp_path}")
        return temp_path
    except Exception as exc:
        elapsed = time.monotonic() - started_at
        logger.error(
            "[VOICE DOWNLOAD] failed source=%s url=%s path=%s bytes=%d "
            "elapsed=%.3fs error=%s",
            source_id,
            safe_url,
            temp_path,
            total_bytes,
            elapsed,
            exc,
        )
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
                logger.info(
                    "[VOICE DOWNLOAD] removed invalid temporary file source=%s path=%s",
                    source_id,
                    temp_path,
                )
        except OSError as cleanup_error:
            logger.warning(
                "[VOICE DOWNLOAD] cleanup failed source=%s path=%s error=%s",
                source_id,
                temp_path,
                cleanup_error,
            )
        raise



def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("用法: python file_down.py <文件路径或URL>")
        print("示例:")
        print("  python file_down.py C:\\Users\\User\\Documents\\file.txt")
        print("  python file_down.py https://example.com/file.pdf")
        sys.exit(1)

    source = sys.argv[1]

    try:
        download_file(source)
    except Exception as e:
        print(f"错误: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
