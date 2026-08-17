#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件下载工具
"""

import os
import time
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path


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


def download_voice_file(url: str, prefix: str = "voice") -> str:
    """下载音频文件到统一语音临时目录，返回本地路径。

    Args:
        url: 音频文件的 HTTP/HTTPS 地址
        prefix: 文件名前缀（用于区分来源，例如歌曲 ID）
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

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Referer': 'https://music.163.com/',
    }
    request = urllib.request.Request(url, headers=headers)

    with urllib.request.urlopen(request, timeout=60) as response:
        with open(temp_path, 'wb') as f:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    print(f"语音文件已下载到: {temp_path}")
    return temp_path



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
