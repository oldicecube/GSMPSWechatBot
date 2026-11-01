"""麦麦贴吧搬石 —— protobuf 最小编解码（自研，不依赖 protobuf 库）。

贴吧手机客户端的接口（tiebac.baidu.com/c/f/pb/page 等）收发的是 protobuf，而插件
只带 httpx 一个依赖，也不想为了读几个字段就把整个 google.protobuf 拖进来。

这里只实现 protobuf wire format 的两个最基础的部分：

  * varint（field 1 / wire type 0）：整数，每字节 7 位、最高位表示「还有下一字节」
  * length-delimited（wire type 2）：字符串、字节串、嵌套消息，前面带 varint 长度

不做 schemas、不做代码生成、不做 wire type 1/5（fixed64/fixed32）—— 贴吧真正要读的
字段一个都不落在这两种里，留着反而要多写没测试的代码。**不认识的 wire type 直接
让解析停下来**，而不是猜一个长度继续走：猜错了会把后面的字节错位解析成垃圾数据，
那种 bug 比直接报错难查得多。

对外只暴露四个东西：

  * :func:`encode_varint` / :func:`encode_bytes` / :func:`encode_message` —— 打包请求
  * :func:`parse` —— 把一段 protobuf 拆成 ``{field_number: [(wire_type, value), ...]}``
  * :class:`Message` —— 上面那个 dict 的薄包装，用 ``msg.uint(4)`` / ``msg.text(3)``
    这种读法取值，比到处 ``gv(d, 4)`` 好读

字段号本身不在这里定义，各接口自己的字段含义由调用方（st_tieba）按协议填写。
"""

from __future__ import annotations

from typing import Any, Iterator

# wire type
VARINT = 0
FIXED64 = 1
BYTES = 2
FIXED32 = 5

_MAX_SHIFT = 63


class ProtobufError(ValueError):
    """这段字节不是能认的 protobuf。读数失败一律抛它，不返回半截数据。"""


# --- 打包 ---------------------------------------------------------------------


def encode_varint(value: int) -> bytes:
    """把一个非负整数编成 varint。"""
    if value < 0:
        # protobuf 的负数用 10 字节补码表示；本插件要发的字段全是正数，
        # 真遇到负数是调用方算错了，直接报出来比默默发一串 0xFF 好。
        raise ProtobufError("varint 不接受负数：%r" % (value,))
    if value >= 1 << 64:
        # 超出 uint64 编出来是 11 字节，对端按 64 位读会读到一半就断。
        raise ProtobufError("varint 超出 64 位：%r" % (value,))
    out = bytearray()
    while True:
        chunk = value & 0x7F
        value >>= 7
        if value:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _key(field: int, wire_type: int) -> bytes:
    if field <= 0:
        raise ProtobufError("字段号必须为正：%r" % (field,))
    return encode_varint((field << 3) | wire_type)


def encode_uint(field: int, value: int) -> bytes:
    """``field = value``（整数）。value 为 0 时要发出去就显式发，不替调用方省略。"""
    return _key(field, VARINT) + encode_varint(int(value))


def encode_bytes(field: int, data: "bytes | str") -> bytes:
    """``field = data``（字符串 / 字节串）。"""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return _key(field, BYTES) + encode_varint(len(data)) + data


def encode_message(field: int, payload: bytes) -> bytes:
    """``field = <嵌套消息>``。嵌套消息就是一段完整的 protobuf 字节。"""
    return _key(field, BYTES) + encode_varint(len(payload)) + payload


def encode_packed_uint(field: int, values: "list[int]") -> bytes:
    """``field = [v1, v2, ...]``（packed repeated）。空列表返回空串，调用方自己决定发不发。"""
    if not values:
        return b""
    body = b"".join(encode_varint(int(v)) for v in values)
    return _key(field, BYTES) + encode_varint(len(body)) + body


# --- 拆包 ---------------------------------------------------------------------


def _read_varint(buf: bytes, pos: int) -> "tuple[int, int]":
    """从 pos 读一个 varint，返回 (值, 新位置)。越界或畸形就抛 :class:`ProtobufError`。"""
    value = 0
    shift = 0
    size = len(buf)
    while True:
        if pos >= size:
            raise ProtobufError("varint 读到一半就没了（偏移 %d，共 %d 字节）" % (pos, size))
        byte = buf[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, pos
        shift += 7
        # 检查要在 shift 自增之后：先判再增的话第 10 个字节会被算成合法的，
        # 而 64 位 varint 最多 10 字节（shift 到 63 为止）。
        if shift > _MAX_SHIFT:
            raise ProtobufError("varint 超过 64 位（偏移 %d）" % (pos,))


def parse(buf: bytes) -> "dict[int, list[tuple[int, Any]]]":
    """把一段 protobuf 拆成 ``{字段号: [(wire_type, 值), ...]}``。

    同一个字段号可能出现多次（repeated 字段就是这样），所以值是**列表**，顺序与
    字节里的顺序一致。

    wire type 1 / 5（fixed64 / fixed32）按原样跳过并保留原始字节：贴吧的响应里
    确实出现过它们，直接报错会让整条消息读不出来；但保留成字节而不是数字，
    免得调用方以为那是能直接用的整数。
    """
    if not buf:
        return {}
    out: "dict[int, list[tuple[int, Any]]]" = {}
    pos = 0
    size = len(buf)
    while pos < size:
        key, pos = _read_varint(buf, pos)
        field = key >> 3
        wire_type = key & 0x07
        if field == 0:
            raise ProtobufError("字段号为 0（偏移 %d），这不是合法的 protobuf" % (pos,))
        if wire_type == VARINT:
            value, pos = _read_varint(buf, pos)
        elif wire_type == BYTES:
            length, pos = _read_varint(buf, pos)
            end = pos + length
            if end > size:
                raise ProtobufError("字段 %d 声明 %d 字节，但只剩 %d 字节" % (field, length, size - pos))
            value = buf[pos:end]
            pos = end
        elif wire_type == FIXED64:
            if pos + 8 > size:
                raise ProtobufError("字段 %d 的 8 字节读不全" % (field,))
            value = buf[pos:pos + 8]
            pos += 8
        elif wire_type == FIXED32:
            if pos + 4 > size:
                raise ProtobufError("字段 %d 的 4 字节读不全" % (field,))
            value = buf[pos:pos + 4]
            pos += 4
        else:
            # wire type 3/4 是 proto2 的 group，贴吧不用；6/7 是非法的。
            # 这两种都会让长度无从判断，继续猜必然错位，只能停。
            raise ProtobufError("字段 %d 的 wire type %d 不认识（偏移 %d）" % (field, wire_type, pos))
        out.setdefault(field, []).append((wire_type, value))
    return out


class Message:
    """一段已解析的 protobuf，按字段号取值。

    取值方法都带默认值：贴吧会**省略默认值字段**（proto3 的规矩，值为 0 / 空串时
    根本不发出来），所以「字段不存在」和「字段是 0」必须当成同一件事处理，
    否则每次读到缺字段都要调用方写一遍 try。
    """

    __slots__ = ("fields",)

    def __init__(self, fields: "dict[int, list[tuple[int, Any]]] | None" = None) -> None:
        self.fields = fields or {}

    @classmethod
    def load(cls, buf: bytes) -> "Message":
        """直接解析一段字节。空字节给空消息，不抛。"""
        if not buf:
            return cls({})
        return cls(parse(buf))

    def __bool__(self) -> bool:
        return bool(self.fields)

    def has(self, field: int) -> bool:
        return bool(self.fields.get(field))

    def uint(self, field: int, default: int = 0) -> int:
        """读一个整数。同字段出现多次时取第一个。"""
        for wire_type, value in self.fields.get(field, ()):
            if wire_type == VARINT:
                return int(value)
        return default

    def raw(self, field: int) -> bytes:
        """读一个 length-delimited 字段的原始字节。没有就给空字节。"""
        for wire_type, value in self.fields.get(field, ()):
            if wire_type == BYTES:
                return value
        return b""

    def text(self, field: int, default: str = "") -> str:
        """读一个字符串。解码失败不抛，用 replace 兜住（贴吧的昵称里有各种字符）。"""
        data = self.raw(field)
        if not data:
            return default
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("utf-8", "replace")

    def message(self, field: int) -> "Message":
        """读一个嵌套消息。没有就给空消息。"""
        return Message.load(self.raw(field))

    def messages(self, field: int) -> "list[Message]":
        """读一个重复的嵌套消息列表。"""
        out = []
        for wire_type, value in self.fields.get(field, ()):
            if wire_type == BYTES:
                out.append(Message.load(value))
        return out

    def uint_list(self, field: int) -> "list[int]":
        """读一个重复整数。packed（一个 length-delimited 里塞多个 varint）和
        非 packed（每个值一个字段）两种写法都要认 —— 同一个字段在不同接口里
        可能用不同写法发。"""
        out: "list[int]" = []
        for wire_type, value in self.fields.get(field, ()):
            if wire_type == VARINT:
                out.append(int(value))
            elif wire_type == BYTES:
                pos = 0
                while pos < len(value):
                    item, pos = _read_varint(value, pos)
                    out.append(item)
        return out

    def __iter__(self) -> Iterator[int]:
        return iter(sorted(self.fields))

    def __repr__(self) -> str:
        return "Message(%s)" % (sorted(self.fields),)
