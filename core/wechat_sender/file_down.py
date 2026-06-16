#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件下载工具
"""

import os
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
