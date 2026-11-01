"""配置热更新期间的最近有效快照保护。"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

import tomllib


class LastKnownGoodConfig:
    """保存最近一次有效配置，并识别编辑过程中的临时 TOML 语法错误。"""

    def __init__(self, config_path: Path) -> None:
        """创建绑定到指定配置文件的快照保护器。

        Args:
            config_path: Runner 读取的 ``config.toml`` 路径。只有该文件存在且
                当前无法按 TOML 解析时，SDK 默认回退配置才会触发快照恢复。
        """

        self._config_path = config_path
        self._snapshot: dict[str, Any] | None = None

    def remember(self, config_data: Mapping[str, Any]) -> None:
        """深拷贝并保存一份已经由 SDK 校验通过的配置。

        Args:
            config_data: 已完成默认值合并和类型校验的完整配置映射。
        """

        self._snapshot = deepcopy(dict(config_data))

    def recover_for_invalid_file(
        self,
        config_data: Mapping[str, Any] | None,
        default_config: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """在 Runner 因临时 TOML 错误回退默认配置时恢复最近有效快照。

        Runner 会先把 TOML 解析失败产生的空字典重建为插件默认配置，再调
        用插件归一化方法。因此只有输入恰好等于默认回退值、磁盘文件存在且
        当前不可解析时才恢复。配置文件不存在或内容是合法 TOML 时不恢复，
        使 WebUI 的“重置配置”和真实默认配置仍可按 SDK 语义处理。读取期
        间的 I/O 错误按临时不可用处理，以避免一次保存竞态导致错误卸载。

        Args:
            config_data: Runner 完成配置版本预处理后传给插件的配置。
            default_config: 当前插件配置模型生成的完整默认配置。

        Returns:
            需要继续使用旧配置时返回其深拷贝；其他情况返回 ``None``。
        """

        incoming_config = dict(config_data) if isinstance(config_data, Mapping) else {}
        if incoming_config != dict(default_config) or self._snapshot is None or not self._config_path.exists():
            return None
        try:
            with self._config_path.open("rb") as config_file:
                tomllib.load(config_file)
        except (OSError, tomllib.TOMLDecodeError):
            return deepcopy(self._snapshot)
        return None
