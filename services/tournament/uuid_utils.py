# -*- coding: utf-8 -*-
"""Minecraft UUID 转换工具

Minecraft 实体 UUID 在 NBT 中以 4 个 32 位有符号整数数组存储：
    uuid:[I;a,b,c,d]

本模块将其转换为标准 UUID 字符串格式：
    xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

转换逻辑与 Minecraft 原版 (NbtUtils.loadUUID / UUID.toString) 完全一致：
    mostSigBits  = (long)a << 32 | (b & 0xFFFFFFFF)
    leastSigBits = (long)c << 32 | (d & 0xFFFFFFFF)

    字符串 = hex(a,8) "-" hex(b_hi,4) "-" hex(b_lo,4) "-"
             hex(c_lo,4) "-" hex(d,12)
"""

from __future__ import annotations


def _to_unsigned(x: int) -> int:
    """将有符号 32 位整数转为无符号 32 位整数。"""
    return x & 0xFFFFFFFF


def ints_to_uuid_str(ints) -> str:
    """[I;a,b,c,d] -> 标准 UUID 字符串。

    参数:
        ints: 长度为 4 的整数列表（允许负数，视为有符号 32 位）

    返回:
        标准 UUID 字符串，如 "0123abcd-12ab-34cd-56ef-7890abcdef12"
    """
    if not isinstance(ints, (list, tuple)):
        raise ValueError(f"UUID ints 必须为列表/元组，收到 {type(ints).__name__}")
    if len(ints) != 4:
        raise ValueError(f"UUID 需要恰好 4 个整数，收到 {len(ints)} 个")

    a = _to_unsigned(int(ints[0]))
    b = _to_unsigned(int(ints[1]))
    c = _to_unsigned(int(ints[2]))
    d = _to_unsigned(int(ints[3]))

    most = (a << 32) | b
    least = (c << 32) | d

    return "%08x-%04x-%04x-%04x-%012x" % (
        (most >> 32) & 0xFFFFFFFF,
        (most >> 16) & 0xFFFF,
        most & 0xFFFF,
        (least >> 48) & 0xFFFF,
        least & 0xFFFFFFFFFFFF,
    )


def uuid_str_to_ints(uuid_str: str):
    """标准 UUID 字符串 -> [I;a,b,c,d]（逆向转换，便于调试/RCON 查询）。"""
    clean = uuid_str.replace("-", "").strip()
    if len(clean) != 32:
        raise ValueError(f"无效的 UUID 字符串: {uuid_str}")

    a = int(clean[0:8], 16)
    b = int(clean[8:16], 16)
    c = int(clean[16:24], 16)
    d = int(clean[24:32], 16)

    def to_signed(v: int) -> int:
        return v - 0x100000000 if v >= 0x80000000 else v

    return [to_signed(a), to_signed(b), to_signed(c), to_signed(d)]
