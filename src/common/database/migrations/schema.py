"""SQLite 数据库结构探测工具。"""

from typing import Dict, List

from sqlalchemy import text
from sqlalchemy.engine import Connection

from .models import ColumnSchema, DatabaseSchemaSnapshot, TableSchema


class SQLiteSchemaInspector:
    """SQLite 数据库结构探测器。"""

    def inspect(self, connection: Connection) -> DatabaseSchemaSnapshot:
        """提取数据库中的全部用户表结构快照。

        Args:
            connection: 当前数据库连接。

        Returns:
            DatabaseSchemaSnapshot: 当前数据库结构快照。
        """
        tables: Dict[str, TableSchema] = {}
        for table_name in self.list_user_tables(connection):
            table_schema = self.get_table_schema(connection, table_name)
            tables[table_name] = table_schema
        return DatabaseSchemaSnapshot(tables=tables)

    def list_user_tables(self, connection: Connection) -> List[str]:
        """列出数据库中的全部用户表。

        Args:
            connection: 当前数据库连接。

        Returns:
            List[str]: 按字母顺序排列的用户表名列表。
        """
        statement = text(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        )
        rows = connection.execute(statement).fetchall()
        return [str(row[0]) for row in rows]

    def get_table_schema(self, connection: Connection, table_name: str) -> TableSchema:
        """获取指定表的结构信息。

        Args:
            connection: 当前数据库连接。
            table_name: 待读取结构的表名。

        Returns:
            TableSchema: 指定表的结构快照。
        """
        quoted_table_name = self._quote_identifier(table_name)
        rows = connection.exec_driver_sql(f"PRAGMA table_info({quoted_table_name})").mappings().all()

        columns: Dict[str, ColumnSchema] = {}
        for row in rows:
            column_schema = ColumnSchema(
                name=str(row["name"]),
                declared_type=str(row["type"] or ""),
                default_value=None if row["dflt_value"] is None else str(row["dflt_value"]),
                is_not_null=bool(row["notnull"]),
                primary_key_position=int(row["pk"]),
            )
            columns[column_schema.name] = column_schema

        return TableSchema(name=table_name, columns=columns)

    def table_exists(self, connection: Connection, table_name: str) -> bool:
        """判断数据库中是否存在指定表。

        Args:
            connection: 当前数据库连接。
            table_name: 待检查的表名。

        Returns:
            bool: 若表存在则返回 ``True``。
        """
        return table_name in self.list_user_tables(connection)

    def _quote_identifier(self, identifier: str) -> str:
        """为 SQLite 标识符添加安全引号。

        Args:
            identifier: 待引用的 SQLite 标识符。

        Returns:
            str: 可直接拼接到 PRAGMA 语句中的安全标识符。
        """
        escaped_identifier = identifier.replace('"', '""')
        return f'"{escaped_identifier}"'
