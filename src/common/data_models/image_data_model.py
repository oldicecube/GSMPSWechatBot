from datetime import datetime
from pathlib import Path
from typing import List, Optional

import asyncio
import hashlib
import io
import traceback

from PIL import Image as PILImage
from rich.traceback import install

from src.common.database.database_model import Images, ImageType
from src.common.logger import get_logger
from src.common.utils.image_path import resolve_stored_image_path, serialize_stored_image_path

from . import BaseDatabaseDataModel


install(extra_lines=3)

logger = get_logger("emoji")


class BaseImageDataModel(BaseDatabaseDataModel[Images]):
    def __init__(self, full_path: str | Path, image_bytes: Optional[bytes] = None):
        if not full_path:
            raise ValueError("图片路径不能为空")
        resolved_path = Path(full_path).absolute().resolve()
        if resolved_path.is_dir() or not resolved_path.exists():
            raise FileNotFoundError(f"图片路径无效: {full_path}")

        self.full_path: Path
        self.dir_path: Path
        self.file_name: str
        self._set_full_path(resolved_path)

        self.file_hash: str = None  # type: ignore
        self.image_bytes: Optional[bytes] = image_bytes
        self.image_format: str = ""

    def _set_full_path(self, full_path: Path) -> None:
        """同步刷新路径、目录和文件名等运行时元数据。"""
        resolved_path = full_path.absolute().resolve()
        self.full_path = resolved_path
        self.dir_path = resolved_path.parent.resolve()
        self.file_name = resolved_path.name

    def _restore_image_format_from_path(self) -> None:
        """根据文件扩展名恢复图片格式信息。"""
        self.image_format = self.full_path.suffix.removeprefix(".").lower()

    def _build_non_conflicting_path(self, target_path: Path) -> Path:
        """在目标路径被占用时，生成一个可用的新路径。"""
        candidate_path = target_path
        index = 1
        while candidate_path.exists():
            candidate_path = target_path.with_name(
                f"{target_path.stem}_{self.file_hash[:8]}_{index}{target_path.suffix}"
            )
            index += 1
        return candidate_path

    def _rename_file_to_match_format(self) -> None:
        """修正文件扩展名，并处理目标文件已存在的冲突。"""
        new_file_name = ".".join(self.file_name.split(".")[:-1] + [self.image_format])
        new_full_path = self.dir_path / new_file_name
        if new_full_path == self.full_path:
            return

        if new_full_path.exists():
            existing_file_hash = hashlib.sha256(self.read_image_bytes(new_full_path)).hexdigest()
            if existing_file_hash == self.file_hash:
                logger.info(f"[初始化] {new_full_path.name} 已存在且内容一致，复用已有文件")
                self.full_path.unlink(missing_ok=True)
                self._set_full_path(new_full_path)
                return

            conflict_free_path = self._build_non_conflicting_path(new_full_path)
            logger.warning(
                f"[初始化] {new_full_path.name} 已存在且内容不同，改为保存到 {conflict_free_path.name}"
            )
            self.full_path.rename(conflict_free_path)
            self._set_full_path(conflict_free_path)
            return

        self.full_path.rename(new_full_path)
        self._set_full_path(new_full_path)

    def read_image_bytes(self, path: Path) -> bytes:
        """
        同步读取图片文件的字节内容。

        Args:
            path: 图片文件的完整路径。

        Returns:
            图片文件的字节内容。
        """
        try:
            with open(path, "rb") as file:
                return file.read()
        except FileNotFoundError as exc:
            logger.error(f"[读取图片文件] 文件未找到: {path}")
            raise exc
        except Exception as exc:
            logger.error(f"[读取图片文件] 读取文件时发生错误: {exc}")
            raise exc

    def get_image_format(self, image_bytes: bytes) -> str:
        """
        获取图片的实际格式。

        Args:
            image_bytes: 图片的字节内容。

        Returns:
            小写格式名，例如 `png`、`jpeg`。
        """
        try:
            with PILImage.open(io.BytesIO(image_bytes)) as img:
                if not img.format:
                    raise ValueError("无法识别图片格式")
                return img.format.lower()
        except Exception as exc:
            logger.error(f"[获取图片格式] 读取图片格式时发生错误: {exc}")
            raise exc

    async def calculate_hash_format(self) -> bool:
        """
        计算图片哈希和实际格式，并在需要时修正扩展名。

        Returns:
            成功返回 `True`，失败返回 `False`。
        """
        try:
            logger.debug(f"[初始化] 计算 {self.file_name} 的哈希值...")
            if self.image_bytes is None:
                logger.debug(f"[初始化] 正在读取文件: {self.full_path}")
                image_bytes = await asyncio.to_thread(self.read_image_bytes, self.full_path)
            else:
                image_bytes = self.image_bytes

            self.image_bytes = image_bytes
            self.file_hash = hashlib.sha256(image_bytes).hexdigest()
            logger.debug(f"[初始化] {self.file_name} 计算哈希值成功: {self.file_hash}")

            logger.debug(f"[初始化] 读取 {self.file_name} 的图片格式...")
            self.image_format = await asyncio.to_thread(self.get_image_format, image_bytes)
            logger.debug(f"[初始化] {self.file_name} 读取图片格式成功: {self.image_format}")

            file_ext = self.file_name.split(".")[-1].lower()
            if file_ext != self.image_format:
                log_message = f"[初始化] {self.file_name} 文件扩展名与实际格式不符: ext`{file_ext}`!=`{self.image_format}`"
                if file_ext == "tmp":
                    logger.debug(log_message)
                else:
                    logger.warning(log_message)
                self._rename_file_to_match_format()

            return True
        except Exception as exc:
            logger.error(f"[初始化] 初始化图片时发生错误: {exc}")
            logger.error(traceback.format_exc())
            return False


class MaiEmoji(BaseImageDataModel):
    """表情包数据模型。"""

    def __init__(self, full_path: str | Path, image_bytes: Optional[bytes] = None):
        self.description: str = ""
        self.emotion: List[str] = []
        self.query_count = 0
        self.register_time: Optional[datetime] = None
        self.last_used_time: Optional[datetime] = None
        super().__init__(full_path, image_bytes)

    @classmethod
    def from_db_instance(cls, db_record: Images):
        """从数据库记录构建 `MaiEmoji` 对象。"""
        if db_record.no_file_flag:
            raise ValueError(f"数据库记录 {db_record.image_hash} 标记为文件不存在，无法创建 MaiEmoji 对象")

        obj = cls(resolve_stored_image_path(db_record.full_path))
        obj.file_hash = db_record.image_hash
        obj._restore_image_format_from_path()

        description = db_record.description or ""
        obj.description = description
        normalized_tags = [
            str(item).strip()
            for item in str(description).replace("，", ",").replace("。", ",").replace("、", ",").split(",")
            if str(item).strip()
        ]
        deduped_tags: List[str] = []
        for item in normalized_tags:
            if item not in deduped_tags:
                deduped_tags.append(item)

        obj.emotion = deduped_tags
        obj.query_count = db_record.query_count
        obj.last_used_time = db_record.last_used_time
        obj.register_time = db_record.register_time
        return obj

    def to_db_instance(self) -> Images:
        return Images(
            image_hash=self.file_hash,
            description=self.description,
            full_path=serialize_stored_image_path(self.full_path),
            image_type=ImageType.EMOJI,
            query_count=self.query_count,
            last_used_time=self.last_used_time,
            register_time=self.register_time,
        )


class MaiImage(BaseImageDataModel):
    """普通图片数据模型。"""

    def __init__(self, full_path: str | Path, image_bytes: Optional[bytes] = None):
        self.description: str = ""
        self.vlm_processed: bool = False
        super().__init__(full_path, image_bytes)

    @classmethod
    def from_db_instance(cls, db_record: Images):
        """从数据库记录构建 `MaiImage` 对象。"""
        if db_record.no_file_flag:
            raise ValueError(f"数据库记录 {db_record.image_hash} 标记为文件不存在，无法创建 MaiImage 对象")

        obj = cls(resolve_stored_image_path(db_record.full_path))
        obj.file_hash = db_record.image_hash
        obj._restore_image_format_from_path()
        obj.description = db_record.description
        obj.vlm_processed = db_record.vlm_processed
        return obj

    def to_db_instance(self) -> Images:
        return Images(
            image_hash=self.file_hash,
            description=self.description,
            full_path=serialize_stored_image_path(self.full_path),
            image_type=ImageType.IMAGE,
            vlm_processed=self.vlm_processed,
        )
