"""Host 侧插件 API 动态注册表。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from src.common.logger import get_logger

from .component_timeout import normalize_component_timeout_ms

logger = get_logger("plugin_runtime.host.api_registry")


@dataclass(slots=True)
class APIEntry:
    """API 组件条目。"""

    name: str
    plugin_id: str
    description: str = ""
    version: str = "1"
    public: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    handler_name: str = ""
    dynamic: bool = False
    offline_reason: str = ""
    disabled_session: Set[str] = field(default_factory=set)
    timeout_ms: int = field(init=False)
    full_name: str = field(init=False)
    registry_key: str = field(init=False)

    def __post_init__(self) -> None:
        """规范化 API 条目字段。"""

        self.name = str(self.name or "").strip()
        self.plugin_id = str(self.plugin_id or "").strip()
        self.description = str(self.description or "").strip()
        self.version = str(self.version or "1").strip() or "1"
        self.handler_name = str(self.handler_name or self.name).strip() or self.name
        self.offline_reason = str(self.offline_reason or "").strip()
        self.timeout_ms = normalize_component_timeout_ms(self.metadata.get("timeout_ms", 0))
        self.full_name = f"{self.plugin_id}.{self.name}"
        self.registry_key = APIRegistry.build_registry_key(self.plugin_id, self.name, self.version)

    @classmethod
    def from_metadata(cls, name: str, plugin_id: str, metadata: Dict[str, Any]) -> "APIEntry":
        """根据 Runner 上报的元数据构造 API 条目。"""

        safe_metadata = dict(metadata)
        return cls(
            name=name,
            plugin_id=plugin_id,
            description=str(safe_metadata.get("description", "") or ""),
            version=str(safe_metadata.get("version", "1") or "1"),
            public=bool(safe_metadata.get("public", False)),
            metadata=safe_metadata,
            enabled=bool(safe_metadata.get("enabled", True)),
            handler_name=str(safe_metadata.get("handler_name", name) or name),
            dynamic=bool(safe_metadata.get("dynamic", False)),
            offline_reason=str(safe_metadata.get("offline_reason", "") or ""),
        )


class APIRegistry:
    """Host 侧插件 API 动态注册表。

    该注册表不直接面向 Runner，而是复用插件组件注册/卸载事件，
    维护面向 API 调用场景的专用索引。
    """

    def __init__(self) -> None:
        """初始化 API 注册表。"""

        self._apis: Dict[str, APIEntry] = {}
        self._by_full_name: Dict[str, List[APIEntry]] = {}
        self._by_plugin: Dict[str, List[APIEntry]] = {}
        self._by_name: Dict[str, List[APIEntry]] = {}

    def clear(self) -> None:
        """清空全部 API 注册状态。"""

        self._apis.clear()
        self._by_full_name.clear()
        self._by_plugin.clear()
        self._by_name.clear()

    @staticmethod
    def _is_api_component(component_type: Any) -> bool:
        """判断组件声明是否属于 API。"""

        return str(component_type or "").strip().upper() == "API"

    @staticmethod
    def _normalize_query_version(version: Any) -> str:
        """规范化查询使用的版本字符串。"""

        return str(version or "").strip()

    @classmethod
    def _split_reference(cls, reference: str, version: Any = "") -> Tuple[str, str]:
        """解析可能带 ``@version`` 后缀的 API 引用。"""

        normalized_reference = str(reference or "").strip()
        normalized_version = cls._normalize_query_version(version)
        if normalized_reference and not normalized_version and "@" in normalized_reference:
            candidate_reference, candidate_version = normalized_reference.rsplit("@", 1)
            candidate_reference = candidate_reference.strip()
            candidate_version = candidate_version.strip()
            if candidate_reference and candidate_version:
                normalized_reference = candidate_reference
                normalized_version = candidate_version
        return normalized_reference, normalized_version

    @staticmethod
    def build_registry_key(plugin_id: str, name: str, version: str) -> str:
        """构造 API 注册表唯一键。"""

        normalized_full_name = f"{str(plugin_id or '').strip()}.{str(name or '').strip()}"
        normalized_version = str(version or "1").strip() or "1"
        return f"{normalized_full_name}@{normalized_version}"

    @staticmethod
    def check_api_enabled(entry: APIEntry, session_id: Optional[str] = None) -> bool:
        """判断 API 条目当前是否处于启用状态。"""

        if session_id and session_id in entry.disabled_session:
            return False
        return entry.enabled

    def register_api(self, name: str, plugin_id: str, metadata: Dict[str, Any]) -> bool:
        """注册单个 API 条目。"""

        normalized_name = str(name or "").strip()
        if not normalized_name:
            logger.warning(f"插件 {plugin_id} 存在空 API 名称声明，已忽略")
            return False

        entry = APIEntry.from_metadata(name=normalized_name, plugin_id=plugin_id, metadata=metadata)
        existing_entry = self._apis.get(entry.registry_key)
        if existing_entry is not None:
            logger.warning(f"API {entry.registry_key} 已存在，覆盖旧条目")
            self._remove_entry(existing_entry)

        self._apis[entry.registry_key] = entry
        self._by_full_name.setdefault(entry.full_name, []).append(entry)
        self._by_plugin.setdefault(plugin_id, []).append(entry)
        self._by_name.setdefault(entry.name, []).append(entry)
        return True

    def register_plugin_apis(self, plugin_id: str, components: List[Dict[str, Any]]) -> int:
        """批量注册某个插件声明的全部 API。"""

        count = 0
        for component in components:
            if not self._is_api_component(component.get("component_type")):
                continue
            if self.register_api(
                name=str(component.get("name", "") or ""),
                plugin_id=plugin_id,
                metadata=component.get("metadata", {}) if isinstance(component.get("metadata"), dict) else {},
            ):
                count += 1
        return count

    def replace_plugin_dynamic_apis(
        self,
        plugin_id: str,
        components: List[Dict[str, Any]],
        *,
        offline_reason: str = "动态 API 已下线",
    ) -> Tuple[int, int]:
        """替换指定插件当前声明的动态 API 集合。"""

        normalized_offline_reason = str(offline_reason or "").strip() or "动态 API 已下线"
        desired_registry_keys: Set[str] = set()
        registered_count = 0

        for component in components:
            if not self._is_api_component(component.get("component_type")):
                continue
            metadata = component.get("metadata", {}) if isinstance(component.get("metadata"), dict) else {}
            dynamic_metadata = dict(metadata)
            dynamic_metadata["dynamic"] = True
            dynamic_metadata.pop("offline_reason", None)

            entry = APIEntry.from_metadata(
                name=str(component.get("name", "") or ""),
                plugin_id=plugin_id,
                metadata=dynamic_metadata,
            )
            desired_registry_keys.add(entry.registry_key)
            if self.register_api(entry.name, plugin_id, dynamic_metadata):
                registered_count += 1

        offlined_count = 0
        for entry in list(self._by_plugin.get(plugin_id, [])):
            if not entry.dynamic or entry.registry_key in desired_registry_keys:
                continue
            entry.enabled = False
            entry.offline_reason = normalized_offline_reason
            entry.metadata["offline_reason"] = normalized_offline_reason
            offlined_count += 1

        return registered_count, offlined_count

    def _remove_entry(self, entry: APIEntry) -> None:
        """从全部索引中移除单个 API 条目。"""

        self._apis.pop(entry.registry_key, None)

        full_name_entries = self._by_full_name.get(entry.full_name)
        if full_name_entries is not None:
            self._by_full_name[entry.full_name] = [
                candidate for candidate in full_name_entries if candidate is not entry
            ]
            if not self._by_full_name[entry.full_name]:
                self._by_full_name.pop(entry.full_name, None)

        plugin_entries = self._by_plugin.get(entry.plugin_id)
        if plugin_entries is not None:
            self._by_plugin[entry.plugin_id] = [candidate for candidate in plugin_entries if candidate is not entry]
            if not self._by_plugin[entry.plugin_id]:
                self._by_plugin.pop(entry.plugin_id, None)

        name_entries = self._by_name.get(entry.name)
        if name_entries is not None:
            self._by_name[entry.name] = [candidate for candidate in name_entries if candidate is not entry]
            if not self._by_name[entry.name]:
                self._by_name.pop(entry.name, None)

    def remove_apis_by_plugin(self, plugin_id: str) -> int:
        """移除某个插件的全部 API。"""

        entries = list(self._by_plugin.get(plugin_id, []))
        for entry in entries:
            self._remove_entry(entry)
        return len(entries)

    def get_api_by_full_name(
        self,
        full_name: str,
        *,
        version: str = "",
        enabled_only: bool = True,
        session_id: Optional[str] = None,
    ) -> Optional[APIEntry]:
        """按完整名查询单个 API。"""

        normalized_full_name, normalized_version = self._split_reference(full_name, version)
        if not normalized_full_name:
            return None

        if normalized_version:
            entry = self._apis.get(f"{normalized_full_name}@{normalized_version}")
            if entry is None:
                return None
            if enabled_only and not self.check_api_enabled(entry, session_id):
                return None
            return entry

        candidates = list(self._by_full_name.get(normalized_full_name, []))
        filtered_entries = [
            entry
            for entry in candidates
            if not enabled_only or self.check_api_enabled(entry, session_id)
        ]
        if len(filtered_entries) != 1:
            return None
        return filtered_entries[0]

    def get_api(
        self,
        plugin_id: str,
        name: str,
        *,
        version: str = "",
        enabled_only: bool = True,
        session_id: Optional[str] = None,
    ) -> Optional[APIEntry]:
        """按插件 ID、短名与版本查询单个 API。"""

        return self.get_api_by_full_name(
            f"{plugin_id}.{name}",
            version=version,
            enabled_only=enabled_only,
            session_id=session_id,
        )

    def get_apis(
        self,
        *,
        plugin_id: Optional[str] = None,
        name: str = "",
        version: str = "",
        enabled_only: bool = True,
        session_id: Optional[str] = None,
    ) -> List[APIEntry]:
        """查询 API 列表。"""

        normalized_name = str(name or "").strip()
        normalized_version = self._normalize_query_version(version)

        if plugin_id:
            candidates = list(self._by_plugin.get(plugin_id, []))
        elif normalized_name:
            candidates = list(self._by_name.get(normalized_name, []))
        else:
            candidates = list(self._apis.values())

        filtered_entries: List[APIEntry] = []
        for entry in candidates:
            if plugin_id and entry.plugin_id != plugin_id:
                continue
            if normalized_name and entry.name != normalized_name:
                continue
            if normalized_version and entry.version != normalized_version:
                continue
            if enabled_only and not self.check_api_enabled(entry, session_id):
                continue
            filtered_entries.append(entry)

        filtered_entries.sort(key=lambda entry: (entry.plugin_id, entry.name, entry.version))
        return filtered_entries

    def toggle_api_status(
        self,
        full_name: str,
        enabled: bool,
        *,
        version: str = "",
        session_id: Optional[str] = None,
    ) -> bool:
        """设置指定 API 的启用状态。"""

        entry = self.get_api_by_full_name(
            full_name,
            version=version,
            enabled_only=False,
            session_id=session_id,
        )
        if entry is None:
            return False
        if session_id:
            if enabled:
                entry.disabled_session.discard(session_id)
            else:
                entry.disabled_session.add(session_id)
        else:
            entry.enabled = enabled
            if enabled:
                entry.offline_reason = ""
                entry.metadata.pop("offline_reason", None)
        return True
