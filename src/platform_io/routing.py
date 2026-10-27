"""提供 Platform IO 的轻量路由绑定表。"""

from typing import Dict, List, Optional

from .types import RouteBinding, RouteKey


class RouteTable:
    """维护单张路由绑定表。

    该实现不负责裁决“唯一 owner”，只负责保存绑定，并按
    ``RouteKey.resolution_order()`` 解析出候选绑定列表。
    """

    def __init__(self) -> None:
        """初始化空路由绑定表。"""

        self._bindings: Dict[RouteKey, Dict[str, RouteBinding]] = {}

    def bind(self, binding: RouteBinding) -> None:
        """注册或更新一条路由绑定。

        Args:
            binding: 要保存的路由绑定。
        """

        self._bindings.setdefault(binding.route_key, {})[binding.driver_id] = binding

    def unbind(self, route_key: RouteKey, driver_id: Optional[str] = None) -> List[RouteBinding]:
        """移除指定路由键上的绑定。

        Args:
            route_key: 要移除绑定的路由键。
            driver_id: 可选的驱动 ID；为空时移除该路由键下全部绑定。

        Returns:
            List[RouteBinding]: 被移除的绑定列表。
        """

        binding_map = self._bindings.get(route_key)
        if not binding_map:
            return []

        if driver_id is None:
            removed = list(binding_map.values())
            self._bindings.pop(route_key, None)
            return self._sort_bindings(removed)

        removed_binding = binding_map.pop(driver_id, None)
        if not binding_map:
            self._bindings.pop(route_key, None)
        return [removed_binding] if removed_binding is not None else []

    def remove_bindings_by_driver(self, driver_id: str) -> List[RouteBinding]:
        """移除某个驱动在整张表上的全部绑定。

        Args:
            driver_id: 要移除绑定的驱动 ID。

        Returns:
            List[RouteBinding]: 被移除的绑定列表。
        """

        removed_bindings: List[RouteBinding] = []
        empty_route_keys: List[RouteKey] = []
        for route_key, binding_map in self._bindings.items():
            removed_binding = binding_map.pop(driver_id, None)
            if removed_binding is not None:
                removed_bindings.append(removed_binding)
            if not binding_map:
                empty_route_keys.append(route_key)

        for route_key in empty_route_keys:
            self._bindings.pop(route_key, None)

        return self._sort_bindings(removed_bindings)

    def list_bindings(self, route_key: Optional[RouteKey] = None) -> List[RouteBinding]:
        """列出当前路由表中的绑定。

        Args:
            route_key: 可选的路由键过滤条件。

        Returns:
            List[RouteBinding]: 当前绑定列表。
        """

        if route_key is None:
            bindings: List[RouteBinding] = []
            for binding_map in self._bindings.values():
                bindings.extend(binding_map.values())
            return self._sort_bindings(bindings)

        binding_map = self._bindings.get(route_key, {})
        return self._sort_bindings(list(binding_map.values()))

    def resolve_bindings(self, route_key: RouteKey) -> List[RouteBinding]:
        """按从具体到宽泛的顺序解析路由候选绑定。

        Args:
            route_key: 待解析的路由键。

        Returns:
            List[RouteBinding]: 去重后的候选绑定列表。
        """

        resolved_bindings: List[RouteBinding] = []
        seen_driver_ids: set[str] = set()
        for candidate_key in route_key.resolution_order():
            for binding in self.list_bindings(candidate_key):
                if binding.driver_id in seen_driver_ids:
                    continue
                seen_driver_ids.add(binding.driver_id)
                resolved_bindings.append(binding)
        return resolved_bindings

    def has_binding_for_driver(self, route_key: RouteKey, driver_id: str) -> bool:
        """判断指定驱动是否在当前路由键解析结果中。

        Args:
            route_key: 待解析的路由键。
            driver_id: 目标驱动 ID。

        Returns:
            bool: 若驱动存在于解析结果中则返回 ``True``。
        """

        return any(binding.driver_id == driver_id for binding in self.resolve_bindings(route_key))

    @staticmethod
    def _sort_bindings(bindings: List[RouteBinding]) -> List[RouteBinding]:
        """按优先级降序排列绑定列表。

        Args:
            bindings: 待排序的绑定列表。

        Returns:
            List[RouteBinding]: 排序后的绑定列表。
        """

        return sorted(bindings, key=lambda item: item.priority, reverse=True)
