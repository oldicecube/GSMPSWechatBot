"""SQLite 数据库版本存储实现。"""

from sqlalchemy.engine import Connection

from .exceptions import DatabaseMigrationVersionError


class SQLiteUserVersionStore:
    """基于 ``PRAGMA user_version`` 的 SQLite 版本存储器。"""

    def read_version(self, connection: Connection) -> int:
        """读取当前数据库的 schema 版本号。

        Args:
            connection: 当前数据库连接。

        Returns:
            int: 数据库记录的 schema 版本号。

        Raises:
            DatabaseMigrationVersionError: 当读取结果异常或版本号非法时抛出。
        """
        row = connection.exec_driver_sql("PRAGMA user_version").first()
        if row is None or len(row) == 0:
            raise DatabaseMigrationVersionError("读取 SQLite user_version 失败，返回结果为空。")

        version = row[0]
        if not isinstance(version, int):
            raise DatabaseMigrationVersionError(f"读取到的 SQLite user_version 不是整数: {version!r}")
        if version < 0:
            raise DatabaseMigrationVersionError(f"读取到的 SQLite user_version 不能为负数: {version}")
        return version

    def write_version(self, connection: Connection, version: int) -> None:
        """写入新的 schema 版本号。

        Args:
            connection: 当前数据库连接。
            version: 待写入的 schema 版本号。

        Raises:
            DatabaseMigrationVersionError: 当版本号非法时抛出。
        """
        self._validate_version(version)
        connection.exec_driver_sql(f"PRAGMA user_version = {version}")

    def _validate_version(self, version: int) -> None:
        """校验版本号是否合法。

        Args:
            version: 待校验的版本号。

        Raises:
            DatabaseMigrationVersionError: 当版本号非法时抛出。
        """
        if version < 0:
            raise DatabaseMigrationVersionError(f"SQLite user_version 不能小于 0: {version}")
