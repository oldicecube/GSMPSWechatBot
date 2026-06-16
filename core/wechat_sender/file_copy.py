#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件复制工具
"""

import sys
import os
import ctypes
from ctypes import wintypes

# Windows API 常量
CF_HDROP = 15
GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040


class DROPFILES(ctypes.Structure):
    _fields_ = [
        ("pFiles", wintypes.DWORD),
        ("pt", wintypes.POINT),
        ("fNC", wintypes.BOOL),
        ("fWide", wintypes.BOOL),
    ]


# 配置 Windows API 函数的参数和返回类型（解决64位指针截断问题）
kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32

# GlobalAlloc
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL

# GlobalLock
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = wintypes.LPVOID

# GlobalUnlock
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL

# GlobalFree
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL

# OpenClipboard
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL

# CloseClipboard
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL

# EmptyClipboard
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL

# SetClipboardData
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE


def copy_file_to_clipboard(file_path: str) -> None:
    """
    将文件复制到 Windows 剪贴板（CF_HDROP 格式）

    Args:
        file_path: 本地文件的绝对路径或相对路径
    """
    # 确保文件路径是绝对路径
    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"文件不存在: {abs_path}")

    # 准备文件路径（以双 null 结尾的宽字符串）
    file_path_w = abs_path + '\0\0'
    file_path_bytes = file_path_w.encode('utf-16-le')

    # 计算总大小
    dropfiles_size = ctypes.sizeof(DROPFILES)
    total_size = dropfiles_size + len(file_path_bytes)

    # 分配全局内存
    h_global = kernel32.GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, total_size)
    if not h_global:
        raise RuntimeError(f"无法分配全局内存，大小: {total_size}")

    # 锁定内存
    p_global = kernel32.GlobalLock(h_global)
    if not p_global:
        kernel32.GlobalFree(h_global)
        raise RuntimeError("无法锁定全局内存")

    try:
        # 创建 DROPFILES 结构
        dropfiles = DROPFILES()
        dropfiles.pFiles = dropfiles_size
        dropfiles.pt.x = 0
        dropfiles.pt.y = 0
        dropfiles.fNC = False
        dropfiles.fWide = True

        # 写入 DROPFILES 结构
        ctypes.memmove(p_global, ctypes.byref(dropfiles), dropfiles_size)

        # 写入文件路径
        ctypes.memmove(p_global + dropfiles_size, file_path_bytes, len(file_path_bytes))
    finally:
        kernel32.GlobalUnlock(h_global)

    # 打开剪贴板并设置数据
    if not user32.OpenClipboard(None):
        kernel32.GlobalFree(h_global)
        raise RuntimeError("无法打开剪贴板")

    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_HDROP, h_global):
            raise RuntimeError("无法设置剪贴板数据")
        # 成功后，内存所有权转移给系统，不要释放
        h_global = None
    finally:
        user32.CloseClipboard()
        if h_global:
            kernel32.GlobalFree(h_global)
    print(f"✅ 文件已成功复制到剪贴板: {os.path.abspath(file_path)}")


def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("用法: uv run file_copy.py <文件路径>")
        print("示例: uv run file_copy.py C:\\Users\\User\\Documents\\file.txt")
        sys.exit(1)
    
    file_path = sys.argv[1]
    
    try:
        copy_file_to_clipboard(file_path)
    except Exception as e:
        print(f"❌ 错误: {e}")
        sys.exit(1)



if __name__ == "__main__":
    main()
