# -*- coding: utf-8 -*-
"""SNBT (Stringified NBT) 解析器

用于解析 RCON `/data get storage` 返回的 Minecraft SNBT 字符串。

支持类型：
    - 复合   {key:value, ...}            -> dict
    - 列表   [v1, v2, ...]                -> list
    - 数组   [I;1,2,3] [B;..] [L;..]      -> list[int]
    - 字符串 "..." '...' 或裸标识符       -> str
    - 数字   1 / 1.0 / 1.5f / 2d / 3b ... -> int / float
    - 布尔   true / false                 -> bool

解析失败抛出 SNBTParseError。
"""

from __future__ import annotations


class SNBTParseError(ValueError):
    pass


# 裸字符串允许的字符（无引号 key / 字符串）
_BARE_KEY_CHARS = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    "_-.+"
)
_NUMBER_SUFFIXES = set("fFdDbBsSlL")


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0
        self.n = len(text)

    # ---------- 通用辅助 ----------
    def _skip_ws(self):
        while self.pos < self.n and self.text[self.pos] in " \t\r\n":
            self.pos += 1

    def _peek(self):
        self._skip_ws()
        return self.text[self.pos] if self.pos < self.n else None

    def _eof(self):
        return self.pos >= self.n

    def _error(self, msg):
        # 计算行号便于排错
        line = self.text.count("\n", 0, self.pos) + 1
        col = self.pos - (self.text.rfind("\n", 0, self.pos))
        snippet = self.text[self.pos:self.pos + 30]
        raise SNBTParseError(f"{msg} (line {line} col {col}: '{snippet}')")

    # ---------- 入口 ----------
    def parse(self):
        self._skip_ws()
        # 跳过 RCON 可能附加的前缀文本，定位到首个值起始字符
        c = self._peek()
        if c is None:
            raise SNBTParseError("空内容")
        value = self._parse_value()
        # 允许尾部有残余空白/文本（RCON 偶尔附加说明）
        return value

    # ---------- 值分发 ----------
    def _parse_value(self):
        c = self._peek()
        if c is None:
            self._error("意外的输入结束")
        if c == "{":
            return self._parse_compound()
        if c == "[":
            return self._parse_list()
        if c == '"' or c == "'":
            return self._parse_quoted_string(c)
        return self._parse_primitive()

    # ---------- 复合 ----------
    def _parse_compound(self) -> dict:
        result = {}
        self.pos += 1  # consume '{'
        self._skip_ws()
        if self._peek() == "}":
            self.pos += 1
            return result
        while True:
            self._skip_ws()
            key = self._parse_key()
            self._skip_ws()
            if self._peek() != ":":
                self._error("复合项缺少 ':' 分隔符")
            self.pos += 1  # consume ':'
            value = self._parse_value()
            result[key] = value
            self._skip_ws()
            c = self._peek()
            if c == ",":
                self.pos += 1
                # 允许尾随逗号
                self._skip_ws()
                if self._peek() == "}":
                    self.pos += 1
                    break
                continue
            if c == "}":
                self.pos += 1
                break
            self._error("复合项缺少 ',' 或 '}'")
        return result

    def _parse_key(self) -> str:
        c = self._peek()
        if c == '"' or c == "'":
            return self._parse_quoted_string(c)
        # 裸 key
        start = self.pos
        while self.pos < self.n and self.text[self.pos] in _BARE_KEY_CHARS:
            self.pos += 1
        if self.pos == start:
            self._error("空的复合键")
        return self.text[start:self.pos]

    # ---------- 列表 / 数组 ----------
    def _parse_list(self) -> list:
        self.pos += 1  # consume '['
        self._skip_ws()
        # 检测数组类型前缀 [I; ...] [B; ...] [L; ...
        if (
            self.pos + 1 < self.n
            and self.text[self.pos] in "BILbil"
            and self.text[self.pos + 1] == ";"
        ):
            # 类型前缀，对解析结果无影响（值仍为整数列表）
            self.pos += 2
        result = []
        self._skip_ws()
        if self._peek() == "]":
            self.pos += 1
            return result
        while True:
            value = self._parse_value()
            result.append(value)
            self._skip_ws()
            c = self._peek()
            if c == ",":
                self.pos += 1
                self._skip_ws()
                if self._peek() == "]":
                    self.pos += 1
                    break
                continue
            if c == "]":
                self.pos += 1
                break
            self._error("列表项缺少 ',' 或 ']'")
        return result

    # ---------- 字符串 ----------
    def _parse_quoted_string(self, quote) -> str:
        self.pos += 1  # consume opening quote
        chars = []
        while self.pos < self.n:
            ch = self.text[self.pos]
            if ch == "\\":
                # 转义
                self.pos += 1
                if self.pos >= self.n:
                    self._error("字符串转义意外结束")
                esc = self.text[self.pos]
                chars.append(self._decode_escape(esc))
                self.pos += 1
                continue
            if ch == quote:
                self.pos += 1
                return "".join(chars)
            chars.append(ch)
            self.pos += 1
        self._error("字符串未闭合")

    def _decode_escape(self, esc) -> str:
        mapping = {
            '"': '"',
            "'": "'",
            "\\": "\\",
            "n": "\n",
            "t": "\t",
            "r": "\r",
            "b": "\b",
            "f": "\f",
            "0": "\0",
            "/": "/",
        }
        return mapping.get(esc, esc)

    # ---------- 原始值（数字 / 布尔 / 裸字符串） ----------
    def _parse_primitive(self):
        start = self.pos
        # 读取到分隔符为止
        while self.pos < self.n and self.text[self.pos] not in ",{}[]: \t\r\n":
            self.pos += 1
        token = self.text[start:self.pos]
        if token == "":
            self._error("无法解析的原始值")
        return self._classify_primitive(token)

    def _classify_primitive(self, token: str):
        low = token.lower()
        if low == "true":
            return True
        if low == "false":
            return False

        # 处理带后缀的数字
        body = token
        if body and body[-1] in _NUMBER_SUFFIXES:
            suffix = body[-1].lower()
            body = body[:-1]
            try:
                if suffix in ("f", "d"):
                    return float(body)
                # b/s/l 均为整数
                return int(body)
            except ValueError:
                # 不是数字，当作字符串（罕见）
                return token

        # 纯数字判定
        if self._looks_like_number(body):
            if any(ch in body for ch in ".eE"):
                try:
                    return float(body)
                except ValueError:
                    return token
            try:
                return int(body)
            except ValueError:
                return token

        # 其余当作裸字符串
        return token

    @staticmethod
    def _looks_like_number(s: str) -> bool:
        if not s:
            return False
        # 可选符号
        i = 0
        if s[0] in "+-":
            i = 1
        if i >= len(s):
            return False
        seen_digit = False
        seen_dot = False
        seen_e = False
        while i < len(s):
            ch = s[i]
            if ch.isdigit():
                seen_digit = True
            elif ch == "." and not seen_dot and not seen_e:
                seen_dot = True
            elif ch in "eE" and seen_digit and not seen_e:
                seen_e = True
                if i + 1 < len(s) and s[i + 1] in "+-":
                    i += 1
            else:
                return False
            i += 1
        return seen_digit


def parse_snbt(text: str):
    """解析 SNBT 字符串为 Python 原生对象。

    若 text 含有 RCON 附加前缀（如 "xxx has the following entity data: ..."），
    会自动定位到首个 `{` 或 `[` 开始解析。
    """
    if text is None:
        raise SNBTParseError("输入为 None")
    text = str(text).strip()

    # 若非直接以 { 或 [ 开头，尝试定位首个结构起始
    if text and text[0] not in "{[":
        # 寻找第一个 { 或 [
        idx_obj = text.find("{")
        idx_arr = text.find("[")
        candidates = [i for i in (idx_obj, idx_arr) if i != -1]
        if candidates:
            text = text[min(candidates):]
        # 否则交给解析器处理（可能是裸值）

    parser = _Parser(text)
    return parser.parse()
