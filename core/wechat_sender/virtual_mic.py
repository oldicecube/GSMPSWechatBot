# -*- coding: utf-8 -*-
"""虚拟麦克风音频注入模块

将任意音频文件播放到虚拟麦克风的"输入端"，使微信（或任意程序）把这段音频
当作真实麦克风声音采集。

默认目标设备（VB-CABLE）：
  - 播放设备（注入端）：CABLE Input
  - 采集设备（麦克风端）：CABLE Output
安装 VB-CABLE 后，在微信「设置 -> 通用 -> 语音/通话」里把麦克风/语音输入
设备选为 "CABLE Output"，本模块把音频播到 "CABLE Input" 即可完成注入。

设备名可用环境变量覆盖（便于测试/自定义声卡）：
  WECHAT_VOICE_PLAYBACK_DEVICE  注入端播放设备关键字，默认 "CABLE Input"
  WECHAT_VOICE_CAPTURE_DEVICE   采集端设备关键字，默认 "CABLE Output"
"""
import logging
import os
import threading

import numpy as np
import sounddevice as sd
import soundfile as sf

logger = logging.getLogger(__name__)

DEFAULT_PLAYBACK_DEVICE = "CABLE Input"
DEFAULT_CAPTURE_DEVICE = "CABLE Output"
MAX_VOICE_DURATION = 60.0
MIN_VOICE_DURATION = 1.0


_AUDIO_LOCK = threading.RLock()


def _clamp_duration(duration):
    """将语音时长限制在 [MIN_VOICE_DURATION, MAX_VOICE_DURATION] 秒内。"""
    try:
        value = float(duration)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return max(MIN_VOICE_DURATION, min(value, MAX_VOICE_DURATION))


def _find_device(keyword, want_input, want_output):
    """按名称关键字查找音频设备，返回设备序号或 None。"""
    if not keyword or not str(keyword).strip():
        return None
    keyword = str(keyword).strip().lower()
    try:
        devices = sd.query_devices()
    except Exception as exc:
        logger.warning("查询音频设备失败: %s", exc)
        return None
    for idx, dev in enumerate(devices):
        name = str(dev.get("name", "")).lower()
        if keyword not in name:
            continue
        if want_input and dev.get("max_input_channels", 0) <= 0:
            continue
        if want_output and dev.get("max_output_channels", 0) <= 0:
            continue
        return idx
    return None


class VirtualMicInjector:
    """虚拟麦克风注入器。

    核心能力：
      - play(): 把音频文件播放到注入端（CABLE Input），由虚拟声卡转发到麦克风端
      - inject_and_capture(): 播放的同时从采集端录音，用于回路自检（验证注入成功）
    """

    def __init__(self, playback_device=None, capture_device=None):
        self.playback_keyword = (
            playback_device
            or os.environ.get("WECHAT_VOICE_PLAYBACK_DEVICE")
            or DEFAULT_PLAYBACK_DEVICE
        )
        self.capture_keyword = (
            capture_device
            or os.environ.get("WECHAT_VOICE_CAPTURE_DEVICE")
            or DEFAULT_CAPTURE_DEVICE
        )
        self._playback_index = None
        self._capture_index = None
        self._scan()

    def _scan(self):
        self._playback_index = _find_device(self.playback_keyword, want_input=False, want_output=True)
        self._capture_index = _find_device(self.capture_keyword, want_input=True, want_output=False)

    def playback_index(self):
        return self._playback_index

    def capture_index(self):
        return self._capture_index

    def is_available(self):
        """注入端（播放设备）是否可用。"""
        return self._playback_index is not None

    def capture_available(self):
        """采集端（麦克风设备）是否可用。"""
        return self._capture_index is not None

    def describe(self):
        """返回诊断信息。"""
        def _name(idx):
            try:
                return sd.query_devices(idx)["name"] if idx is not None else None
            except Exception:
                return None

        return {
            "playback_keyword": self.playback_keyword,
            "capture_keyword": self.capture_keyword,
            "playback_index": self._playback_index,
            "playback_name": _name(self._playback_index),
            "capture_index": self._capture_index,
            "capture_name": _name(self._capture_index),
            "available": self.is_available(),
            "capture_available": self.capture_available(),
            "max_duration_seconds": MAX_VOICE_DURATION,
        }

    @staticmethod
    def _load_audio(audio_path):
        """读取音频文件为 float32 二维数组 + 采样率。"""
        data, sample_rate = sf.read(str(audio_path), dtype="float32", always_2d=True)
        if data.size == 0:
            raise ValueError("音频文件为空: %s" % audio_path)
        return data, int(sample_rate)

    @staticmethod
    def _fit_duration(data, sample_rate, duration, start=None):
        """按起始偏移 + 目标时长裁剪/静音补齐音频，返回 (data, actual_seconds)。

        - start（秒，支持小数）：从音频第 start 秒开始注入；默认 0。起始偏移超出
          音频长度时抛 ValueError。
        - duration=None（默认）：按住时长 = 裁剪后剩余音频的实际长度；超过
          MAX_VOICE_DURATION 秒直接截断，不足不补静音（注入完即松开 Shift）。
        - duration 显式给定：按指定时长裁剪；文件比目标时长短时尾部补静音（旧语义）。
        """
        try:
            start_sec = max(0.0, float(start or 0.0))
        except (TypeError, ValueError):
            start_sec = 0.0
        start_frames = int(round(start_sec * sample_rate))
        if start_frames > 0:
            data = data[start_frames:]
        if len(data) == 0:
            raise ValueError("起始秒 %.2f 超出音频长度" % start_sec)
        file_seconds = len(data) / float(sample_rate)
        if duration is None:
            # 默认：实际注入时长 = min(剩余文件时长, 60s)，不补静音
            target = min(file_seconds, MAX_VOICE_DURATION)
            target_frames = int(round(target * sample_rate))
            return data[:target_frames], target
        target = _clamp_duration(duration)
        if target is None:
            target = min(file_seconds, MAX_VOICE_DURATION)
            target_frames = int(round(target * sample_rate))
            return data[:target_frames], target
        target_frames = int(round(target * sample_rate))
        if len(data) >= target_frames:
            return data[:target_frames], target
        # 显式时长且文件比目标时长短：尾部补静音（微信会采集到完整时长）
        pad_frames = target_frames - len(data)
        pad = np.zeros((pad_frames, data.shape[1]), dtype=np.float32)
        return np.concatenate([data, pad], axis=0), target

    @staticmethod
    def _match_output_channels(data, device_index):
        """确保声道数不超过播放设备支持的最大输出声道。"""
        try:
            max_out = int(sd.query_devices(device_index).get("max_output_channels", 0) or 0)
        except Exception:
            max_out = 2
        if data.shape[1] > max_out:
            data = data.mean(axis=1, keepdims=True).astype(np.float32)
        return data

    def play(self, audio_path, duration=None, start=None):
        """将音频注入虚拟麦克风输入端。

        Args:
            audio_path: 音频文件路径（wav/mp3/flac/ogg 等 soundfile 支持的格式）
            duration: 语音时长（秒），最大 60；None 表示按音频实际长度注入
                （超过 60 秒截断为 60 秒，不补静音）
            start: 起始秒（从音频第 N 秒开始注入，支持小数），默认 0

        Returns:
            float: 实际注入时长（秒）
        """
        if not self.is_available():
            raise RuntimeError(
                "未找到虚拟麦克风播放设备（关键字: %r）。请安装 VB-CABLE "
                "（https://vb-audio.com/Cable/），或在微信设置中把麦克风设为 "
                "'CABLE Output'，并用 WECHAT_VOICE_PLAYBACK_DEVICE 覆盖设备名。" % self.playback_keyword
            )
        data, sample_rate = self._load_audio(audio_path)
        data, actual_seconds = self._fit_duration(data, sample_rate, duration, start)
        data = self._match_output_channels(data, self._playback_index)

        with _AUDIO_LOCK:
            logger.info(
                "开始注入音频: file=%s, frames=%d, sr=%d, channels=%d, device=%d(%s), duration=%.2fs",
                audio_path, len(data), sample_rate, data.shape[1],
                self._playback_index, self.playback_keyword, actual_seconds,
            )
            sd.play(data, samplerate=sample_rate, device=self._playback_index)
            sd.wait()
        logger.info("音频注入完成: %.2fs", actual_seconds)
        return actual_seconds

    def inject_and_capture(self, audio_path, duration=None, start=None):
        """播放到注入端并同时从采集端录音，验证虚拟麦克风回路。

        Returns:
            dict: {ok, played_seconds, source_rms, captured_rms, peak, frames, input_channels}
        """
        if not self.is_available():
            raise RuntimeError("未找到虚拟麦克风播放设备: %r" % self.playback_keyword)
        if not self.capture_available():
            raise RuntimeError("未找到虚拟麦克风采集设备: %r" % self.capture_keyword)

        data, sample_rate = self._load_audio(audio_path)
        data, actual_seconds = self._fit_duration(data, sample_rate, duration, start)
        data = self._match_output_channels(data, self._playback_index)

        try:
            in_channels = int(sd.query_devices(self._capture_index).get("max_input_channels", 0) or 1)
        except Exception:
            in_channels = 1
        out_channels = data.shape[1]

        # 注入端与采集端必须属于同一音频宿主 API，否则 PortAudio 无法同时打开
        try:
            in_hostapi = int(sd.query_devices(self._capture_index).get("hostapi", -1))
            out_hostapi = int(sd.query_devices(self._playback_index).get("hostapi", -1))
        except Exception:
            in_hostapi = out_hostapi = -1
        if in_hostapi >= 0 and out_hostapi >= 0 and in_hostapi != out_hostapi:
            raise RuntimeError(
                "注入端(%s, hostapi=%d)与采集端(%s, hostapi=%d)属于不同音频宿主 API，"
                "无法做全双工回路自检。请改用同一宿主 API 的设备（VB-CABLE 的 Input/Output 通常同在 MME）。"
                % (self.playback_keyword, out_hostapi, self.capture_keyword, in_hostapi)
            )

        with _AUDIO_LOCK:
            logger.info(
                "回路自检: 播放到 %s(%d) / 从 %s(%d) 采集",
                self.playback_keyword, self._playback_index,
                self.capture_keyword, self._capture_index,
            )
            recorded = sd.playrec(
                data,
                samplerate=sample_rate,
                channels=in_channels,
                device=(self._capture_index, self._playback_index),
            )
            sd.wait()

        source_rms = float(np.sqrt(np.mean(data ** 2)))
        if recorded.ndim == 1:
            recorded = recorded.reshape(-1, 1)
        captured = recorded[:, :min(in_channels, recorded.shape[1])]
        captured_rms = float(np.sqrt(np.mean(captured ** 2)))
        peak = float(np.max(np.abs(captured))) if captured.size else 0.0
        # 虚拟声卡近乎 1:1 透传；只要采集到明显能量即视为注入成功
        threshold = max(0.005, source_rms * 0.05)
        ok = bool(captured_rms > threshold and peak > 0.01)
        return {
            "ok": ok,
            "played_seconds": actual_seconds,
            "source_rms": round(source_rms, 5),
            "captured_rms": round(captured_rms, 5),
            "peak": round(peak, 5),
            "threshold": round(threshold, 5),
            "frames": int(captured.shape[0]),
            "input_channels": in_channels,
        }


def make_test_wav(path, seconds=3.0, sample_rate=44100, channels=2):
    """生成一段双音测试音频（440Hz + 880Hz），用于验证注入链路。"""
    seconds = _clamp_duration(seconds) or 3.0
    t = np.linspace(0, seconds, int(seconds * sample_rate), endpoint=False, dtype=np.float32)
    tone = (0.35 * np.sin(2 * np.pi * 440 * t) + 0.25 * np.sin(2 * np.pi * 880 * t)).astype(np.float32)
    data = np.column_stack([tone] * channels)
    sf.write(str(path), data, sample_rate)
    return str(path)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        prog="virtual_mic",
        description="虚拟麦克风音频注入工具（VB-CABLE / 自定义设备）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_devices = sub.add_parser("devices", help="列出所有音频设备")
    p_status = sub.add_parser("status", help="显示注入器设备探测结果")
    p_gen = sub.add_parser("make-test-wav", help="生成测试音频")
    p_gen.add_argument("path")
    p_gen.add_argument("--seconds", type=float, default=3.0)
    p_play = sub.add_parser("play", help="播放音频到虚拟麦克风输入端")
    p_play.add_argument("file")
    p_play.add_argument("--duration", type=float, default=None)
    p_play.add_argument("--start", type=float, default=None, help="起始秒（从音频第 N 秒开始）")
    p_selftest = sub.add_parser("selftest", help="注入并同时采集，验证虚拟麦克风回路")
    p_selftest.add_argument("file")
    p_selftest.add_argument("--duration", type=float, default=None)
    p_selftest.add_argument("--start", type=float, default=None, help="起始秒（从音频第 N 秒开始）")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.cmd == "devices":
        for idx, dev in enumerate(sd.query_devices()):
            print("%2d | %-55s | in=%d out=%d" % (
                idx, dev["name"], dev["max_input_channels"], dev["max_output_channels"]))
        return 0
    if args.cmd == "status":
        injector = VirtualMicInjector()
        info = injector.describe()
        print("注入端关键字 : %s" % info["playback_keyword"])
        print("采集端关键字 : %s" % info["capture_keyword"])
        print("注入端设备   : %s" % (info["playback_name"] or "未找到！请安装 VB-CABLE"))
        print("采集端设备   : %s" % (info["capture_name"] or "未找到！请安装 VB-CABLE"))
        return 0 if info["available"] else 1
    if args.cmd == "make-test-wav":
        print("生成测试音频: %s (%.1fs)" % (args.path, args.seconds))
        make_test_wav(args.path, seconds=args.seconds)
        return 0
    if args.cmd == "play":
        injector = VirtualMicInjector()
        played = injector.play(args.file, duration=args.duration, start=args.start)
        print("注入完成: %.2fs" % played)
        return 0
    if args.cmd == "selftest":
        injector = VirtualMicInjector()
        result = injector.inject_and_capture(args.file, duration=args.duration, start=args.start)
        print(result)
        return 0 if result["ok"] else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
