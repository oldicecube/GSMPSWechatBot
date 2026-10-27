"""数据库迁移基础设施异常定义。"""


class DatabaseMigrationError(Exception):
    """数据库迁移基础异常。"""


class DatabaseMigrationConfigurationError(DatabaseMigrationError):
    """数据库迁移配置不合法。"""


class DatabaseMigrationPlanningError(DatabaseMigrationError):
    """数据库迁移计划生成失败。"""


class DatabaseMigrationExecutionError(DatabaseMigrationError):
    """数据库迁移执行失败。"""


class DatabaseMigrationVersionError(DatabaseMigrationError):
    """数据库版本读写或校验失败。"""


class MissingMigrationStepError(DatabaseMigrationPlanningError):
    """缺少某个版本区间所需的迁移步骤。"""


class UnsupportedMigrationDirectionError(DatabaseMigrationPlanningError):
    """当前迁移方向不被支持。"""


class UnrecognizedDatabaseSchemaError(DatabaseMigrationVersionError):
    """无法识别未标记版本数据库的结构。"""
