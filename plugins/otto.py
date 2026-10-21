# -*- coding: utf-8 -*-
"""/otto voice-generation plugin."""

from __future__ import annotations

import logging
import os

from core.sender import send
from core.wechat_sender.file_down import get_voice_temp_dir, is_in_voice_temp_dir
from services.otto_generator import OttoAssetError, generate_otto_audio

COMMAND = "/otto"

_LOGGER = logging.getLogger(__name__)


def _target(context):
    context = context or {}
    return context.get("group") or context.get("user") or "文件传输助手"


def _parse(content):
    lines = str(content or "").splitlines()
    text = lines[0].strip() if lines else ""
    reverse = any(line.strip() == "倒放" for line in lines[1:])
    return text, reverse


def _cleanup_temp_file(path):
    """Delete only generated files inside the shared voice temp directory."""
    if not is_in_voice_temp_dir(path):
        try:
            # Windows may spell the same TEMP path with an 8.3 username.
            # samefile() resolves that alias while the generated file exists.
            if not os.path.samefile(os.path.dirname(path), get_voice_temp_dir()):
                return
        except (OSError, TypeError):
            return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        _LOGGER.warning("failed to clean Otto temporary file %s: %s", path, exc)


def handle(content, context):
    text, reverse = _parse(content)
    if not text:
        return "用法：/otto <待生成文本>\n可选换行参数：倒放"

    audio_path = None
    try:
        audio_path, duration = generate_otto_audio(
            text,
            reverse=reverse,
            output_dir=get_voice_temp_dir(),
            max_seconds=60.0,
        )
        try:
            ok, error = send(
                target=_target(context),
                file_path=audio_path,
                mode="wechat_voice",
                duration=duration,
            )
        finally:
            # send() waits for the local voice hook response.  The hook also
            # performs guarded cleanup; this pass covers dry-runs, mocks and
            # alternate senders while remaining restricted to the voice dir.
            _cleanup_temp_file(audio_path)
        if not ok:
            return f"Otto语音发送失败：{error}"
        return None
    except (OttoAssetError, ValueError) as exc:
        _cleanup_temp_file(audio_path)
        return f"Otto语音生成失败：{exc}"
    except Exception as exc:
        _cleanup_temp_file(audio_path)
        _LOGGER.exception("/otto failed")
        return f"Otto语音处理失败：{exc}"
