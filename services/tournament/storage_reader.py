# -*- coding: utf-8 -*-
"""RCON Storage 读取器。

通过 RCON 执行 Minecraft 命令读取 Storage 数据：
    /data get storage luckypillar:games list[-1]        # 最近一局
    /data get storage luckypillar:games list[{id:N}]    # 指定比赛

并将返回的 SNBT 解析为 Python 对象。
"""

from __future__ import annotations

from mcrcon import MCRcon

from .snbt import parse_snbt, SNBTParseError


class StorageReader:
    def __init__(self, rcon_config: dict, timeout: int = 10):
        self.host = rcon_config.get("host")
        try:
            self.port = int(rcon_config.get("port") or 25575)
        except (TypeError, ValueError):
            self.port = 25575
        self.password = rcon_config.get("password")
        self.timeout = max(1, int(timeout or 10))

    @property
    def available(self) -> bool:
        return bool(self.host and self.password)

    # ----------------------------------------------------------
    # 底层命令执行
    # ----------------------------------------------------------
    def _command(self, command: str):
        if not self.available:
            print("[TOURNAMENT RCON] 未配置 RCON，跳过")
            return None
        try:
            with MCRcon(self.host, self.password, port=self.port, timeout=self.timeout) as mcr:
                return mcr.command(command)
        except Exception as e:
            print(f"[TOURNAMENT RCON ERROR] 命令执行失败: {command!r} -> {e}")
            return None

    # ----------------------------------------------------------
    # Storage 读取
    # ----------------------------------------------------------
    def get_latest_match(self, storage: str):
        """读取 storage 中 list[-1] 的最近一局，返回解析后的 dict 或 None。"""
        resp = self._command(f"data get storage {storage} list[-1]")
        return self._parse_storage_match(resp)

    def get_all_matches(self, storage: str):
        """读取 storage 的完整 list，返回对局字典列表；空或解析失败返回空列表。"""
        resp = self._command(f"data get storage {storage} list")
        if not resp:
            return []
        text = str(resp).strip()
        if "Found no elements" in text or "couldn't" in text.lower():
            return []
        try:
            parsed = parse_snbt(text)
        except SNBTParseError as e:
            print(f"[TOURNAMENT SNBT ERROR] 完整列表解析失败: {e} | 原文: {text[:200]}")
            return []
        if isinstance(parsed, list):
            return [match for match in parsed if isinstance(match, dict)]
        if isinstance(parsed, dict):
            return [parsed]
        return []

    def get_match_by_id(self, match_id: int, storage: str):
        """读取 storage 中 list[{id:N}] 指定比赛。"""
        resp = self._command(f"data get storage {storage} list[{{id:{int(match_id)}}}]")
        return self._parse_storage_match(resp)

    @staticmethod
    def _parse_storage_match(resp):
        if not resp:
            return None
        text = str(resp).strip()
        # 无数据时的常见回复
        low = text.lower()
        if (
            not text
            or low.startswith("couldn't")
            or low.startswith("no element")
            or "is not a valid" in low
        ):
            return None
        try:
            parsed = parse_snbt(text)
            # `list[{id:N}]` 在部分版本会返回单元素列表；统一给上层单局 dict。
            if isinstance(parsed, list):
                if len(parsed) == 1 and isinstance(parsed[0], dict):
                    return parsed[0]
                return None
            return parsed
        except SNBTParseError as e:
            print(f"[TOURNAMENT SNBT ERROR] 解析失败: {e} | 原文: {text[:200]}")
            return None
