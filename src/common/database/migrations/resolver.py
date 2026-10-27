"""数据库版本解析器。"""

from abc import ABC, abstractmethod
from typing import List, Optional

from sqlalchemy.engine import Connection

from .exceptions import DatabaseMigrationVersionError, UnrecognizedDatabaseSchemaError
from .models import DatabaseSchemaSnapshot, ResolvedSchemaVersion, SchemaVersionSource
from .schema import SQLiteSchemaInspector
from .version_store import SQLiteUserVersionStore


class BaseSchemaVersionDetector(ABC):
    """未标记版本数据库的结构探测器基类。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """返回当前探测器名称。

        Returns:
            str: 当前探测器名称。
        """

    @abstractmethod
    def detect_version(self, snapshot: DatabaseSchemaSnapshot) -> Optional[int]:
        """根据数据库结构快照推断版本号。

        Args:
            snapshot: 当前数据库结构快照。

        Returns:
            Optional[int]: 若识别成功则返回版本号，否则返回 ``None``。
        """


class SchemaVersionResolver:
    """数据库版本解析器。"""

    def __init__(
        self,
        version_store: Optional[SQLiteUserVersionStore] = None,
        schema_inspector: Optional[SQLiteSchemaInspector] = None,
        detectors: Optional[List[BaseSchemaVersionDetector]] = None,
    ) -> None:
        """初始化数据库版本解析器。

        Args:
            version_store: 版本存储器；未提供时将使用默认实现。
            schema_inspector: 结构探测器；未提供时将使用默认实现。
            detectors: 未标记版本数据库的探测器列表。
        """
        self.version_store = version_store or SQLiteUserVersionStore()
        self.schema_inspector = schema_inspector or SQLiteSchemaInspector()
        self.detectors: List[BaseSchemaVersionDetector] = list(detectors or [])

    def add_detector(self, detector: BaseSchemaVersionDetector) -> None:
        """注册一个未标记版本数据库探测器。

        Args:
            detector: 待注册的探测器实例。
        """
        self.detectors.append(detector)

    def list_detectors(self) -> List[BaseSchemaVersionDetector]:
        """返回当前已注册的全部探测器。

        Returns:
            List[BaseSchemaVersionDetector]: 已注册探测器列表副本。
        """
        return list(self.detectors)

    def resolve(self, connection: Connection) -> ResolvedSchemaVersion:
        """解析当前数据库的 schema 版本信息。

        解析顺序如下：
            1. 优先读取 ``PRAGMA user_version``。
            2. 若其值为 0，则对数据库结构做快照。
            3. 若数据库为空，则返回空库版本。
            4. 若数据库非空，则交给探测器链进行识别。

        Args:
            connection: 当前数据库连接。

        Returns:
            ResolvedSchemaVersion: 解析后的数据库版本信息。

        Raises:
            DatabaseMigrationVersionError: 当探测器返回非法版本号时抛出。
            UnrecognizedDatabaseSchemaError: 当数据库非空但无法识别版本时抛出。
        """
        recorded_version = self.version_store.read_version(connection)
        if recorded_version > 0:
            return ResolvedSchemaVersion(version=recorded_version, source=SchemaVersionSource.PRAGMA)

        snapshot = self.schema_inspector.inspect(connection)
        if snapshot.is_empty():
            return ResolvedSchemaVersion(
                version=0,
                source=SchemaVersionSource.EMPTY_DATABASE,
                snapshot=snapshot,
            )

        return self._detect_unversioned_database(snapshot)

    def _detect_unversioned_database(self, snapshot: DatabaseSchemaSnapshot) -> ResolvedSchemaVersion:
        """识别未标记版本的历史数据库。

        Args:
            snapshot: 当前数据库结构快照。

        Returns:
            ResolvedSchemaVersion: 探测器识别出的版本信息。

        Raises:
            DatabaseMigrationVersionError: 当探测器返回非法版本号时抛出。
            UnrecognizedDatabaseSchemaError: 当全部探测器都无法识别结构时抛出。
        """
        for detector in self.detectors:
            detected_version = detector.detect_version(snapshot)
            if detected_version is None:
                continue
            if detected_version < 0:
                raise DatabaseMigrationVersionError(
                    f"探测器 {detector.name!r} 返回了非法版本号: {detected_version}"
                )
            return ResolvedSchemaVersion(
                version=detected_version,
                source=SchemaVersionSource.DETECTOR,
                detector_name=detector.name,
                snapshot=snapshot,
            )

        raise UnrecognizedDatabaseSchemaError("当前数据库未记录版本号，且现有探测器无法识别其结构。")
