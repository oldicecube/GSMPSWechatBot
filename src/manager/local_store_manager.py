from pathlib import Path
from typing import TypeAlias

import json
import os
import tempfile

from src.common.logger import get_logger

LOCAL_STORE_FILE_PATH = "data/local_store.json"
LocalStoreValue: TypeAlias = str | list | dict | int | float | bool

logger = get_logger("local_storage")


class LocalStoreManager:
    """管理本地 JSON 存储文件的加载、保存和损坏恢复。"""

    file_path: str
    """本地存储路径"""

    store: dict[str, LocalStoreValue]
    """本地存储数据"""

    def __init__(self, local_store_path: str | None = None):
        """初始化本地存储路径并加载已有数据。"""
        self.file_path = local_store_path or LOCAL_STORE_FILE_PATH
        self.store = {}
        self.load_local_store()

    def __getitem__(self, item: str) -> LocalStoreValue | None:
        """获取本地存储数据"""
        return self.store.get(item)

    def __setitem__(self, key: str, value: LocalStoreValue):
        """设置本地存储数据"""
        self.store[key] = value
        self.save_local_store()

    def __delitem__(self, key: str):
        """删除本地存储数据"""
        if key in self.store:
            del self.store[key]
            self.save_local_store()
        else:
            logger.warning(f"尝试删除不存在的键: {key}")

    def __contains__(self, item: str) -> bool:
        """检查本地存储数据是否存在"""
        return item in self.store

    def load_local_store(self) -> None:
        """加载本地存储数据"""
        file_path = Path(self.file_path)
        if file_path.exists():
            # 存在本地存储文件，加载数据
            logger.info("正在阅读记事本......我在看，我真的在看！")
            logger.debug(f"加载本地存储数据: {self.file_path}")
            try:
                with file_path.open("r", encoding="utf-8") as f:
                    loaded_store = json.load(f)
                if not isinstance(loaded_store, dict):
                    raise ValueError("本地存储根节点必须是 JSON 对象")
                self.store = loaded_store
                logger.info("全都记起来了！")
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("啊咧？记事本被弄脏了，正在重建记事本......")
                logger.debug(f"本地存储文件无法读取: {exc}")
                self._backup_broken_store(file_path)
                self.store = {}
                self._write_store_atomically(file_path, self.store)
                logger.info("记事本重建成功！")
        else:
            # 不存在本地存储文件，创建新的目录和文件
            logger.warning("啊咧？记事本不存在，正在创建新的记事本......")
            self._write_store_atomically(file_path, self.store)
            logger.info("记事本创建成功！")

    def save_local_store(self):
        """保存本地存储数据"""
        logger.debug(f"保存本地存储数据: {self.file_path}")
        self._write_store_atomically(Path(self.file_path), self.store)

    def _write_store_atomically(self, file_path: Path, store: dict[str, LocalStoreValue]) -> None:
        """将本地存储写入临时文件后原子替换目标文件。"""
        file_path.parent.mkdir(parents=True, exist_ok=True)
        temp_file_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=file_path.parent,
                prefix=f".{file_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_file_path = temp_file.name
                json.dump(store, temp_file, ensure_ascii=False, indent=4)
                temp_file.write("\n")
            os.replace(temp_file_path, file_path)
        finally:
            if temp_file_path and os.path.exists(temp_file_path):
                os.unlink(temp_file_path)

    def _backup_broken_store(self, file_path: Path) -> None:
        """备份无法读取的本地存储文件，避免重建时覆盖现场。"""
        backup_path = self._next_backup_path(file_path)
        file_path.replace(backup_path)
        logger.warning(f"已将损坏的本地存储备份到: {backup_path}")

    @staticmethod
    def _next_backup_path(file_path: Path) -> Path:
        """生成不会覆盖已有文件的损坏备份路径。"""
        backup_path = file_path.with_name(f"{file_path.name}.corrupt")
        if not backup_path.exists():
            return backup_path

        index = 1
        while True:
            indexed_backup_path = file_path.with_name(f"{file_path.name}.corrupt.{index}")
            if not indexed_backup_path.exists():
                return indexed_backup_path
            index += 1


local_storage = LocalStoreManager("data/local_store.json")  # 全局单例化
