"""MsgPack 编解码器"""

from abc import ABC, abstractmethod
from datetime import date, datetime
from enum import Enum
from typing import Any, Dict

import msgpack

from .envelope import Envelope

DATETIME_EXT_CODE = 42
DATE_EXT_CODE = 43


class Codec(ABC):
    """消息编解码器基类"""

    @abstractmethod
    def encode_envelope(self, envelope: Envelope) -> bytes: ...

    @abstractmethod
    def decode_envelope(self, data: bytes) -> Envelope: ...

    @abstractmethod
    def encode(self, obj: Dict[str, Any]) -> bytes: ...

    @abstractmethod
    def decode(self, data: bytes) -> Dict[str, Any]: ...


class MsgPackCodec(Codec):
    """MsgPack 编解码器"""

    @staticmethod
    def _encode_ext_type(obj: Any) -> Any:
        """编码 MsgPack 原生不支持的 Python 类型。"""
        if isinstance(obj, datetime):
            return msgpack.ExtType(DATETIME_EXT_CODE, obj.isoformat().encode("utf-8"))
        if isinstance(obj, date):
            return msgpack.ExtType(DATE_EXT_CODE, obj.isoformat().encode("utf-8"))
        if isinstance(obj, Enum):
            return obj.value
        raise TypeError(f"can not serialize {type(obj).__name__!r} object")

    @staticmethod
    def _decode_ext_type(code: int, data: bytes) -> Any:
        if code == DATETIME_EXT_CODE:
            return datetime.fromisoformat(data.decode("utf-8"))
        if code == DATE_EXT_CODE:
            return date.fromisoformat(data.decode("utf-8"))
        return msgpack.ExtType(code, data)

    def encode(self, obj: Dict[str, Any]) -> bytes:
        result = msgpack.packb(obj, default=self._encode_ext_type, use_bin_type=True)
        if result is None:
            raise ValueError("msgpack.packb returned None, expected bytes")
        return result

    def decode(self, data: bytes) -> Dict[str, Any]:
        result = msgpack.unpackb(data, ext_hook=self._decode_ext_type, raw=False)
        if not isinstance(result, dict):
            raise ValueError(f"期望解码为 dict，实际为 {type(result)}")
        return result

    def encode_envelope(self, envelope: Envelope) -> bytes:
        return self.encode(envelope.model_dump())

    def decode_envelope(self, data: bytes) -> Envelope:
        raw = self.decode(data)
        return Envelope.model_validate(raw)
