"""命名 Hook 规格注册中心。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


@dataclass(slots=True)
class HookSpec:
    """命名 Hook 的静态规格定义。

    Attributes:
        name: Hook 的唯一名称。
        description: Hook 描述。
        parameters_schema: Hook 参数模型，使用对象级 JSON Schema 表示。
        default_timeout_ms: 默认超时毫秒数；为 ``0`` 时退回系统默认值。
        allow_blocking: 是否允许注册阻塞处理器。
        allow_observe: 是否允许注册观察处理器。
        allow_abort: 是否允许处理器中止当前 Hook 调用。
        allow_kwargs_mutation: 是否允许阻塞处理器修改 ``kwargs``。
    """

    name: str
    description: str = ""
    parameters_schema: Dict[str, Any] = field(default_factory=dict)
    default_timeout_ms: int = 0
    allow_blocking: bool = True
    allow_observe: bool = True
    allow_abort: bool = True
    allow_kwargs_mutation: bool = True


class HookSpecRegistry:
    """命名 Hook 规格注册中心。"""

    def __init__(self) -> None:
        """初始化 Hook 规格注册中心。"""

        self._hook_specs: Dict[str, HookSpec] = {}

    @staticmethod
    def _normalize_hook_name(hook_name: str) -> str:
        """规范化 Hook 名称。

        Args:
            hook_name: 原始 Hook 名称。

        Returns:
            str: 规范化后的 Hook 名称。

        Raises:
            ValueError: Hook 名称为空时抛出。
        """

        normalized_name = str(hook_name or "").strip()
        if not normalized_name:
            raise ValueError("Hook 名称不能为空")
        return normalized_name

    @staticmethod
    def _normalize_parameters_schema(raw_schema: Any) -> Dict[str, Any]:
        """规范化 Hook 参数模型。

        Args:
            raw_schema: 原始参数模型。

        Returns:
            Dict[str, Any]: 规范化后的对象级 JSON Schema。

        Raises:
            ValueError: 参数模型不是合法对象级 Schema 时抛出。
        """

        if raw_schema is None:
            return {}
        if not isinstance(raw_schema, dict):
            raise ValueError("Hook 参数模型必须是字典")
        if not raw_schema:
            return {}

        normalized_schema = deepcopy(raw_schema)
        schema_type = normalized_schema.get("type")
        properties = normalized_schema.get("properties")
        if schema_type not in {"", None, "object"} and properties is None:
            raise ValueError("Hook 参数模型必须是 object 类型或属性映射")
        if schema_type in {"", None} and properties is None:
            normalized_schema = {
                "type": "object",
                "properties": normalized_schema,
            }
        elif schema_type in {"", None}:
            normalized_schema["type"] = "object"

        if normalized_schema.get("type") != "object":
            raise ValueError("Hook 参数模型必须是 object 类型")
        return normalized_schema

    @classmethod
    def _normalize_spec(cls, spec: HookSpec) -> HookSpec:
        """规范化 Hook 规格对象。

        Args:
            spec: 原始 Hook 规格。

        Returns:
            HookSpec: 规范化后的 Hook 规格副本。
        """

        return HookSpec(
            name=cls._normalize_hook_name(spec.name),
            description=str(spec.description or "").strip(),
            parameters_schema=cls._normalize_parameters_schema(spec.parameters_schema),
            default_timeout_ms=max(int(spec.default_timeout_ms), 0),
            allow_blocking=bool(spec.allow_blocking),
            allow_observe=bool(spec.allow_observe),
            allow_abort=bool(spec.allow_abort),
            allow_kwargs_mutation=bool(spec.allow_kwargs_mutation),
        )

    def clear(self) -> None:
        """清空全部 Hook 规格。"""

        self._hook_specs.clear()

    def register_hook_spec(self, spec: HookSpec) -> HookSpec:
        """注册单个 Hook 规格。

        Args:
            spec: 需要注册的 Hook 规格。

        Returns:
            HookSpec: 规范化后实际注册的 Hook 规格。
        """

        normalized_spec = self._normalize_spec(spec)
        self._hook_specs[normalized_spec.name] = normalized_spec
        return normalized_spec

    def register_hook_specs(self, specs: Sequence[HookSpec]) -> List[HookSpec]:
        """批量注册 Hook 规格。

        Args:
            specs: 需要注册的 Hook 规格列表。

        Returns:
            List[HookSpec]: 规范化后实际注册的 Hook 规格列表。
        """

        return [self.register_hook_spec(spec) for spec in specs]

    def unregister_hook_spec(self, hook_name: str) -> bool:
        """注销指定 Hook 规格。

        Args:
            hook_name: 目标 Hook 名称。

        Returns:
            bool: 是否成功删除。
        """

        normalized_name = self._normalize_hook_name(hook_name)
        return self._hook_specs.pop(normalized_name, None) is not None

    def get_hook_spec(self, hook_name: str) -> Optional[HookSpec]:
        """获取指定 Hook 的显式规格。

        Args:
            hook_name: 目标 Hook 名称。

        Returns:
            Optional[HookSpec]: 已注册时返回规格副本，否则返回 ``None``。
        """

        normalized_name = self._normalize_hook_name(hook_name)
        spec = self._hook_specs.get(normalized_name)
        return None if spec is None else self._normalize_spec(spec)

    def list_hook_specs(self) -> List[HookSpec]:
        """返回当前全部 Hook 规格。

        Returns:
            List[HookSpec]: 按 Hook 名称升序排列的规格副本列表。
        """

        return [
            self._normalize_spec(spec)
            for _, spec in sorted(self._hook_specs.items(), key=lambda item: item[0])
        ]
