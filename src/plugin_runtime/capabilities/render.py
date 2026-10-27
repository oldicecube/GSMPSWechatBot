"""插件运行时的浏览器渲染能力。"""

from typing import TYPE_CHECKING, Any, Dict

from src.common.logger import get_logger

if TYPE_CHECKING:
    from src.services.html_render_service import HtmlRenderRequest

logger = get_logger("plugin_runtime.integration")


class RuntimeRenderCapabilityMixin:
    """插件运行时的浏览器渲染能力混入。"""

    @staticmethod
    def _coerce_int(value: Any, default: int) -> int:
        """将任意值尽量转换为整数。

        Args:
            value: 原始输入值。
            default: 转换失败时返回的默认值。

        Returns:
            int: 规范化后的整数结果。
        """

        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_float(value: Any, default: float) -> float:
        """将任意值尽量转换为浮点数。

        Args:
            value: 原始输入值。
            default: 转换失败时返回的默认值。

        Returns:
            float: 规范化后的浮点结果。
        """

        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _coerce_bool(value: Any, default: bool = False) -> bool:
        """将任意值转换为布尔值。

        Args:
            value: 原始输入值。
            default: 输入为空时返回的默认值。

        Returns:
            bool: 规范化后的布尔结果。
        """

        if value is None:
            return default
        if isinstance(value, str):
            normalized_value = value.strip().lower()
            if normalized_value in {"0", "false", "no", "off"}:
                return False
            if normalized_value in {"1", "true", "yes", "on"}:
                return True
        return bool(value)

    def _build_html_render_request(self, args: Dict[str, Any]) -> "HtmlRenderRequest":
        """根据 capability 调用参数构造渲染请求。

        Args:
            args: capability 调用参数。

        Returns:
            HtmlRenderRequest: 结构化后的渲染请求。
        """

        from src.services.html_render_service import HtmlRenderRequest

        viewport = args.get("viewport", {})
        viewport_width = 900
        viewport_height = 500
        if isinstance(viewport, dict):
            viewport_width = self._coerce_int(viewport.get("width"), viewport_width)
            viewport_height = self._coerce_int(viewport.get("height"), viewport_height)

        render_timeout_ms = args.get("render_timeout_ms")
        if render_timeout_ms is None:
            render_timeout_ms = args.get("timeout_ms")

        return HtmlRenderRequest(
            html=str(args.get("html", "") or ""),
            selector=str(args.get("selector", "body") or "body"),
            viewport_width=viewport_width,
            viewport_height=viewport_height,
            device_scale_factor=self._coerce_float(args.get("device_scale_factor"), 2.0),
            full_page=self._coerce_bool(args.get("full_page"), False),
            omit_background=self._coerce_bool(args.get("omit_background"), False),
            wait_until=str(args.get("wait_until", "load") or "load"),
            wait_for_selector=str(args.get("wait_for_selector", "") or ""),
            wait_for_timeout_ms=self._coerce_int(args.get("wait_for_timeout_ms"), 0),
            timeout_ms=self._coerce_int(render_timeout_ms, 0),
            allow_network=self._coerce_bool(args.get("allow_network"), False),
        )

    async def _cap_render_html2png(self, plugin_id: str, capability: str, args: Dict[str, Any]) -> Any:
        """将 HTML 内容渲染为 PNG 图片。

        Args:
            plugin_id: 调用该能力的插件 ID。
            capability: 当前能力名称。
            args: 能力调用参数。

        Returns:
            Any: 标准化后的能力返回结构。
        """

        del plugin_id, capability
        try:
            from src.services.html_render_service import get_html_render_service

            request = self._build_html_render_request(args)
            result = await get_html_render_service().render_html_to_png(request)
            return {"success": True, "result": result.to_payload()}
        except Exception as exc:
            logger.error(f"[cap.render.html2png] 执行失败: {exc}", exc_info=True)
            return {"success": False, "error": str(exc)}
