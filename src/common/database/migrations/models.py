"""数据库迁移基础设施核心数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

from sqlalchemy.engine import Connection

if TYPE_CHECKING:
    from .progress import BaseMigrationProgressReporter


def _utc_now() -> datetime:
    """返回当前 UTC 时间。

    Returns:
        datetime: 当前 UTC 时间。
    """
    return datetime.now(timezone.utc)


class SchemaVersionSource(str, Enum):
    """数据库版本来源。"""

    PRAGMA = "pragma"
    DETECTOR = "detector"
    EMPTY_DATABASE = "empty_database"


@dataclass(frozen=True)
class ColumnSchema:
    """数据库列结构快照。"""

    name: str
    declared_type: str
    default_value: Optional[str]
    is_not_null: bool
    primary_key_position: int


@dataclass(frozen=True)
class TableSchema:
    """数据库表结构快照。"""

    name: str
    columns: Dict[str, ColumnSchema]

    def has_column(self, column_name: str) -> bool:
        """判断表中是否存在指定列。

        Args:
            column_name: 待检查的列名。

        Returns:
            bool: 若列存在则返回 ``True``，否则返回 ``False``。
        """
        return column_name in self.columns

    def get_column(self, column_name: str) -> Optional[ColumnSchema]:
        """获取指定列的结构信息。

        Args:
            column_name: 待获取的列名。

        Returns:
            Optional[ColumnSchema]: 列存在时返回列结构，否则返回 ``None``。
        """
        return self.columns.get(column_name)

    def column_names(self) -> List[str]:
        """返回当前表中全部列名。

        Returns:
            List[str]: 按字母顺序排列的列名列表。
        """
        return sorted(self.columns)


@dataclass(frozen=True)
class DatabaseSchemaSnapshot:
    """数据库结构快照。"""

    tables: Dict[str, TableSchema]

    def is_empty(self) -> bool:
        """判断数据库是否没有任何用户表。

        Returns:
            bool: 若数据库中没有用户表则返回 ``True``。
        """
        return not self.tables

    def has_table(self, table_name: str) -> bool:
        """判断数据库是否存在指定表。

        Args:
            table_name: 待检查的表名。

        Returns:
            bool: 若表存在则返回 ``True``，否则返回 ``False``。
        """
        return table_name in self.tables

    def has_column(self, table_name: str, column_name: str) -> bool:
        """判断数据库指定表中是否存在指定列。

        Args:
            table_name: 待检查的表名。
            column_name: 待检查的列名。

        Returns:
            bool: 若表和列均存在则返回 ``True``。
        """
        table_schema = self.get_table(table_name)
        if table_schema is None:
            return False
        return table_schema.has_column(column_name)

    def get_table(self, table_name: str) -> Optional[TableSchema]:
        """获取指定表的结构信息。

        Args:
            table_name: 待获取的表名。

        Returns:
            Optional[TableSchema]: 表存在时返回对应结构，否则返回 ``None``。
        """
        return self.tables.get(table_name)

    def table_names(self) -> List[str]:
        """返回当前数据库中的全部用户表名。

        Returns:
            List[str]: 按字母顺序排列的表名列表。
        """
        return sorted(self.tables)


@dataclass(frozen=True)
class ResolvedSchemaVersion:
    """解析后的数据库版本信息。"""

    version: int
    source: SchemaVersionSource
    detector_name: Optional[str] = None
    snapshot: Optional[DatabaseSchemaSnapshot] = None


@dataclass(frozen=True)
class MigrationExecutionContext:
    """单个迁移步骤的执行上下文。"""

    connection: Connection
    current_version: int
    target_version: int
    step_index: int
    step_name: str
    total_steps: int
    started_at: datetime = field(default_factory=_utc_now)
    progress_reporter: Optional["BaseMigrationProgressReporter"] = None

    def is_last_step(self) -> bool:
        """判断当前步骤是否为最后一步。

        Returns:
            bool: 若当前步骤已是计划中的最后一步则返回 ``True``。
        """
        return self.step_index >= self.total_steps

    def start_progress(
        self,
        total_tables: int,
        total_records: int,
        description: str = "总迁移进度",
        table_unit_name: str = "表",
        record_unit_name: str = "记录",
    ) -> None:
        """启动当前迁移步骤的进度展示。

        Args:
            total_tables: 当前步骤需要处理的总表数。
            total_records: 当前步骤需要处理的总记录数。
            description: 进度描述文本。
            table_unit_name: 表级进度单位名称。
            record_unit_name: 记录级进度单位名称。
        """
        if self.progress_reporter is None:
            return
        self.progress_reporter.start(
            total_records=total_records,
            total_tables=total_tables,
            description=description,
            table_unit_name=table_unit_name,
            record_unit_name=record_unit_name,
        )

    def advance_progress(
        self,
        records: int = 0,
        completed_tables: int = 0,
        item_name: Optional[str] = None,
    ) -> None:
        """推进当前迁移步骤的进度展示。

        Args:
            records: 本次推进的记录数。
            completed_tables: 本次完成的表数。
            item_name: 当前完成的项目名称。
        """
        if self.progress_reporter is None:
            return
        self.progress_reporter.advance(
            records=records,
            completed_tables=completed_tables,
            item_name=item_name,
        )


MigrationHandler = Callable[[MigrationExecutionContext], None]


@dataclass(frozen=True)
class MigrationStep:
    """单个数据库迁移步骤定义。"""

    version_from: int
    version_to: int
    name: str
    description: str
    handler: MigrationHandler
    transactional: bool = True

    def __post_init__(self) -> None:
        """校验迁移步骤定义是否合法。

        Raises:
            ValueError: 当版本号不合法或迁移方向错误时抛出。
        """
        if self.version_from < 0:
            raise ValueError("迁移起始版本不能小于 0。")
        if self.version_to <= self.version_from:
            raise ValueError("迁移目标版本必须大于起始版本。")

    def run(self, context: MigrationExecutionContext) -> None:
        """执行当前迁移步骤。

        Args:
            context: 当前迁移步骤的执行上下文。
        """
        self.handler(context)


@dataclass(frozen=True)
class MigrationPlan:
    """数据库迁移执行计划。"""

    current_version: int
    target_version: int
    steps: List[MigrationStep]

    def is_empty(self) -> bool:
        """判断迁移计划是否为空。

        Returns:
            bool: 若无需执行任何迁移步骤则返回 ``True``。
        """
        return not self.steps

    def step_count(self) -> int:
        """返回迁移计划中的步骤数量。

        Returns:
            int: 当前计划中的迁移步骤数。
        """
        return len(self.steps)

    def latest_reachable_version(self) -> int:
        """返回该计划执行后的最终版本。

        Returns:
            int: 若计划为空则返回当前版本，否则返回最后一步的目标版本。
        """
        if self.is_empty():
            return self.current_version
        return self.steps[-1].version_to


@dataclass(frozen=True)
class DatabaseMigrationState:
    """数据库迁移状态描述。"""

    resolved_version: ResolvedSchemaVersion
    target_version: int
    plan: MigrationPlan

    def requires_migration(self) -> bool:
        """判断当前状态是否需要执行迁移。

        Returns:
            bool: 若计划中存在待执行迁移步骤则返回 ``True``。
        """
        return not self.plan.is_empty()
