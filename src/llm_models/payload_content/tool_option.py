from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Tuple, TypeAlias, cast


class ToolParamType(str, Enum):
    """工具参数类型。"""

    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    FLOAT = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


LegacyToolParameterTuple = Tuple[str, ToolParamType, str, bool, List[str] | None]
"""旧版工具参数元组格式。"""


def normalize_tool_param_type(raw_value: ToolParamType | str | None) -> ToolParamType:
    """将任意输入值规范化为内部工具参数类型。

    Args:
        raw_value: 原始参数类型值。

    Returns:
        ToolParamType: 规范化后的参数类型。未知值会回退为 `STRING`。
    """
    if isinstance(raw_value, ToolParamType):
        return raw_value

    normalized_value = str(raw_value or "").strip().lower()
    if normalized_value in {"integer", "int"}:
        return ToolParamType.INTEGER
    if normalized_value in {"number", "float"}:
        return ToolParamType.NUMBER
    if normalized_value in {"boolean", "bool"}:
        return ToolParamType.BOOLEAN
    if normalized_value == "array":
        return ToolParamType.ARRAY
    if normalized_value == "object":
        return ToolParamType.OBJECT
    return ToolParamType.STRING


def _is_object_schema(schema: Dict[str, Any]) -> bool:
    """判断输入字典是否已经是对象级 JSON Schema。

    Args:
        schema: 待判断的字典。

    Returns:
        bool: 为对象级 JSON Schema 时返回 `True`。
    """
    return schema.get("type") == "object" or "properties" in schema or "required" in schema


def _build_parameters_schema_from_property_map(property_map: Dict[str, Any]) -> Dict[str, Any]:
    """将属性映射转换为对象级 JSON Schema。

    Args:
        property_map: 仅包含属性定义的映射。

    Returns:
        Dict[str, Any]: 对象级 JSON Schema。
    """
    required_names: List[str] = []
    normalized_properties: Dict[str, Any] = {}
    for property_name, property_schema in property_map.items():
        if not isinstance(property_schema, dict):
            continue

        property_schema_copy = deepcopy(property_schema)
        is_required = bool(property_schema_copy.pop("required", False))
        if is_required:
            required_names.append(str(property_name))
        normalized_properties[str(property_name)] = property_schema_copy

    parameters_schema: Dict[str, Any] = {
        "type": "object",
        "properties": normalized_properties,
    }
    if required_names:
        parameters_schema["required"] = required_names
    return parameters_schema


def _build_empty_object_schema() -> Dict[str, Any]:
    """构建无参工具使用的空对象 Schema。"""

    # 带 required: []，否则 DeepSeek 等严格校验会报 null is not of type "array"
    return {
        "type": "object",
        "properties": {},
        "required": [],
    }


@dataclass(slots=True)
class ToolParam:
    """工具参数定义。"""

    name: str
    param_type: ToolParamType
    description: str
    required: bool
    enum_values: List[Any] | None = None
    items_schema: Dict[str, Any] | None = None
    properties: Dict[str, Dict[str, Any]] | None = None
    required_properties: List[str] = field(default_factory=list)
    additional_properties: bool | Dict[str, Any] | None = None
    default: Any = None

    def __post_init__(self) -> None:
        """执行参数定义的基础校验。

        Raises:
            ValueError: 当参数名称或复杂类型定义不合法时抛出。
        """
        if not self.name:
            raise ValueError("参数名称不能为空")
        if self.param_type == ToolParamType.ARRAY and self.items_schema is None:
            raise ValueError("数组参数必须提供 items_schema")
        if self.param_type == ToolParamType.OBJECT and self.properties is None:
            self.properties = {}

    @classmethod
    def from_legacy_tuple(cls, parameter: LegacyToolParameterTuple) -> "ToolParam":
        """从旧版五元组参数定义构建工具参数。

        Args:
            parameter: 旧版参数元组。

        Returns:
            ToolParam: 规范化后的工具参数对象。
        """
        return cls(
            name=parameter[0],
            param_type=parameter[1],
            description=parameter[2],
            required=parameter[3],
            enum_values=parameter[4],
        )

    @classmethod
    def from_dict(
        cls,
        name: str,
        parameter_schema: Dict[str, Any],
        *,
        required: bool = False,
    ) -> "ToolParam":
        """从属性级 JSON Schema 或结构化参数字典构建工具参数。

        Args:
            name: 参数名称。
            parameter_schema: 参数对应的 Schema 或结构化定义。
            required: 参数是否必填。

        Returns:
            ToolParam: 规范化后的工具参数对象。
        """
        raw_required_properties = parameter_schema.get("required_properties")
        if raw_required_properties is None and isinstance(parameter_schema.get("required"), list):
            raw_required_properties = parameter_schema.get("required")
        return cls(
            name=name,
            param_type=normalize_tool_param_type(parameter_schema.get("param_type") or parameter_schema.get("type")),
            description=str(parameter_schema.get("description", "") or ""),
            required=required,
            enum_values=deepcopy(parameter_schema.get("enum_values") or parameter_schema.get("enum")),
            items_schema=deepcopy(parameter_schema.get("items_schema") or parameter_schema.get("items")),
            properties=deepcopy(parameter_schema.get("properties")),
            required_properties=list(raw_required_properties or []),
            additional_properties=deepcopy(
                parameter_schema["additional_properties"]
                if "additional_properties" in parameter_schema
                else parameter_schema.get("additionalProperties")
            ),
            default=deepcopy(parameter_schema.get("default")),
        )

    def to_json_schema(self) -> Dict[str, Any]:
        """将参数定义转换为 JSON Schema。

        Returns:
            Dict[str, Any]: 参数对应的 JSON Schema 片段。
        """
        schema: Dict[str, Any] = {
            "type": self.param_type.value,
            "description": self.description,
        }
        if self.enum_values:
            schema["enum"] = list(self.enum_values)
        if self.default is not None:
            schema["default"] = deepcopy(self.default)
        if self.param_type == ToolParamType.ARRAY and self.items_schema is not None:
            schema["items"] = deepcopy(self.items_schema)
        if self.param_type == ToolParamType.OBJECT:
            schema["properties"] = deepcopy(self.properties or {})
            schema["required"] = list(self.required_properties)
            if self.additional_properties is not None:
                schema["additionalProperties"] = deepcopy(self.additional_properties)
        return schema


@dataclass(slots=True)
class ToolOption:
    """工具定义。"""

    name: str
    description: str
    params: List[ToolParam] | None = None
    parameters_schema_override: Dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """执行工具定义的基础校验。

        Raises:
            ValueError: 当工具名称、描述或参数 Schema 不合法时抛出。
        """
        if not self.name:
            raise ValueError("工具名称不能为空")
        if not self.description:
            raise ValueError("工具描述不能为空")
        if self.parameters_schema_override is not None:
            schema_type = self.parameters_schema_override.get("type")
            if schema_type != "object":
                raise ValueError("工具参数 Schema 必须是 object 类型")

    @classmethod
    def from_definition(cls, definition: Dict[str, Any]) -> "ToolOption":
        """从任意支持的工具定义字典构建内部工具对象。

        支持以下输入形状：
        - `{"name", "description", "parameters_schema"}`
        - `{"name", "description", "parameters"}`
        - OpenAI function tool：`{"type": "function", "function": {...}}`
        - 仅属性映射的对象参数定义：`{"query": {"type": "string"}}`

        Args:
            definition: 原始工具定义字典。

        Returns:
            ToolOption: 规范化后的工具定义对象。

        Raises:
            ValueError: 当工具定义缺少必要字段时抛出。
        """
        if definition.get("type") == "function" and isinstance(definition.get("function"), dict):
            function_definition = cast(Dict[str, Any], definition["function"])
            return cls.from_definition(
                {
                    "name": function_definition.get("name", ""),
                    "description": function_definition.get("description", ""),
                    "parameters_schema": function_definition.get("parameters"),
                }
            )

        name = str(definition.get("name", "") or "").strip()
        description = str(definition.get("description", "") or "").strip()
        if not name:
            raise ValueError("工具定义缺少 name")
        if not description:
            description = f"工具 {name}"

        parameters_schema = definition.get("parameters_schema")
        if isinstance(parameters_schema, dict):
            normalized_schema = deepcopy(parameters_schema)
            if not _is_object_schema(normalized_schema):
                normalized_schema = _build_parameters_schema_from_property_map(normalized_schema)
            return cls(
                name=name,
                description=description,
                params=None,
                parameters_schema_override=normalized_schema,
            )

        raw_parameters = definition.get("parameters")
        if isinstance(raw_parameters, dict):
            normalized_schema = deepcopy(raw_parameters)
            if not _is_object_schema(normalized_schema):
                normalized_schema = _build_parameters_schema_from_property_map(normalized_schema)
            return cls(
                name=name,
                description=description,
                params=None,
                parameters_schema_override=normalized_schema,
            )

        if isinstance(raw_parameters, list):
            params: List[ToolParam] = []
            for raw_parameter in raw_parameters:
                if isinstance(raw_parameter, tuple) and len(raw_parameter) == 5:
                    params.append(ToolParam.from_legacy_tuple(raw_parameter))
                    continue
                if isinstance(raw_parameter, dict):
                    parameter_name = str(raw_parameter.get("name", "") or "").strip()
                    if not parameter_name:
                        continue
                    params.append(
                        ToolParam.from_dict(
                            parameter_name,
                            raw_parameter,
                            required=bool(raw_parameter.get("required", False)),
                        )
                    )
            return cls(
                name=name,
                description=description,
                params=params or None,
                parameters_schema_override=None,
            )

        return cls(name=name, description=description, params=None, parameters_schema_override=None)

    @property
    def parameters_schema(self) -> Dict[str, Any] | None:
        """获取工具参数的对象级 JSON Schema。

        Returns:
            Dict[str, Any] | None: 工具参数 Schema。无参数工具时返回 `None`。
        """
        if self.parameters_schema_override is not None:
            schema = deepcopy(self.parameters_schema_override)
            # override 工具的 required 可能缺失或非数组，统一规整为列表以满足严格校验
            if schema.get("type") == "object" and not isinstance(schema.get("required"), list):
                schema["required"] = []
            return schema
        if not self.params:
            return None
        return {
            "type": "object",
            "properties": {param.name: param.to_json_schema() for param in self.params},
            "required": [param.name for param in self.params if param.required],
        }

    def to_openai_function_schema(self) -> Dict[str, Any]:
        """转换为 OpenAI function calling 结构。

        Returns:
            Dict[str, Any]: OpenAI 兼容的工具定义。
        """
        function_schema: Dict[str, Any] = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters_schema or _build_empty_object_schema(),
        }
        return {
            "type": "function",
            "function": function_schema,
        }


class ToolOptionBuilder:
    """工具定义构建器。"""

    def __init__(self) -> None:
        """初始化构建器。"""
        self.__name: str = ""
        self.__description: str = ""
        self.__params: List[ToolParam] = []
        self.__parameters_schema_override: Dict[str, Any] | None = None

    def set_name(self, name: str) -> "ToolOptionBuilder":
        """设置工具名称。

        Args:
            name: 工具名称。

        Returns:
            ToolOptionBuilder: 当前构建器实例。

        Raises:
            ValueError: 当名称为空时抛出。
        """
        if not name:
            raise ValueError("工具名称不能为空")
        self.__name = name
        return self

    def set_description(self, description: str) -> "ToolOptionBuilder":
        """设置工具描述。

        Args:
            description: 工具描述。

        Returns:
            ToolOptionBuilder: 当前构建器实例。

        Raises:
            ValueError: 当描述为空时抛出。
        """
        if not description:
            raise ValueError("工具描述不能为空")
        self.__description = description
        return self

    def set_parameters_schema(self, schema: Dict[str, Any]) -> "ToolOptionBuilder":
        """直接设置完整的参数对象 Schema。

        Args:
            schema: 完整的对象级 JSON Schema。

        Returns:
            ToolOptionBuilder: 当前构建器实例。

        Raises:
            ValueError: 当 schema 不是 object 类型时抛出。
        """
        if schema.get("type") != "object":
            raise ValueError("工具参数 Schema 必须是 object 类型")
        self.__parameters_schema_override = deepcopy(schema)
        self.__params.clear()
        return self

    def add_param(
        self,
        name: str,
        param_type: ToolParamType,
        description: str,
        required: bool = False,
        enum_values: List[Any] | None = None,
        *,
        items_schema: Dict[str, Any] | None = None,
        properties: Dict[str, Dict[str, Any]] | None = None,
        required_properties: List[str] | None = None,
        additional_properties: bool | Dict[str, Any] | None = None,
        default: Any = None,
    ) -> "ToolOptionBuilder":
        """添加一个参数定义。

        Args:
            name: 参数名称。
            param_type: 参数类型。
            description: 参数描述。
            required: 参数是否必填。
            enum_values: 可选的枚举值列表。
            items_schema: 数组参数的元素 Schema。
            properties: 对象参数的属性定义。
            required_properties: 对象参数内部的必填字段。
            additional_properties: 对象参数是否允许额外字段。
            default: 参数默认值。

        Returns:
            ToolOptionBuilder: 当前构建器实例。

        Raises:
            ValueError: 当构建器已经设置完整 Schema 时抛出。
        """
        if self.__parameters_schema_override is not None:
            raise ValueError("已设置完整参数 Schema，不能再逐项添加参数")
        self.__params.append(
            ToolParam(
                name=name,
                param_type=param_type,
                description=description,
                required=required,
                enum_values=enum_values,
                items_schema=deepcopy(items_schema),
                properties=deepcopy(properties),
                required_properties=list(required_properties or []),
                additional_properties=deepcopy(additional_properties),
                default=deepcopy(default),
            )
        )
        return self

    def build(self) -> ToolOption:
        """构建工具定义。

        Returns:
            ToolOption: 构建完成的工具定义。

        Raises:
            ValueError: 当工具名称或描述缺失时抛出。
        """
        if not self.__name or not self.__description:
            raise ValueError("工具名称和描述不能为空")
        return ToolOption(
            name=self.__name,
            description=self.__description,
            params=None if not self.__params else list(self.__params),
            parameters_schema_override=deepcopy(self.__parameters_schema_override),
        )


ToolDefinitionInput: TypeAlias = ToolOption | Dict[str, Any]
"""统一的工具定义输入类型。"""

TOOL_CALL_SOURCE_EXTRA_KEY = "tool_call_source"
TOOL_CALL_SOURCE_REASONING = "reasoning"
TOOL_CALL_SOURCE_RESPONSE = "response"


def normalize_tool_option(tool_definition: ToolDefinitionInput) -> ToolOption:
    """将任意支持的工具输入规范化为内部 `ToolOption`。

    Args:
        tool_definition: 原始工具定义输入。

    Returns:
        ToolOption: 规范化后的工具定义对象。
    """
    if isinstance(tool_definition, ToolOption):
        return tool_definition
    return ToolOption.from_definition(tool_definition)


def normalize_tool_options(
    tool_definitions: List[ToolDefinitionInput] | None,
) -> List[ToolOption] | None:
    """批量规范化工具定义列表。

    Args:
        tool_definitions: 原始工具定义列表。

    Returns:
        List[ToolOption] | None: 规范化后的工具列表；输入为空时返回 `None`。
    """
    if not tool_definitions:
        return None
    return [normalize_tool_option(tool_definition) for tool_definition in tool_definitions]


@dataclass(slots=True)
class ToolCall:
    """来自模型输出的工具调用。"""

    call_id: str
    func_name: str
    args: Dict[str, Any] | None = None
    extra_content: Dict[str, Any] | None = None

    def __post_init__(self) -> None:
        """执行工具调用的基础校验。

        Raises:
            ValueError: 当工具调用标识或函数名缺失时抛出。
        """
        if not self.call_id:
            raise ValueError("工具调用 ID 不能为空")
        if not self.func_name:
            raise ValueError("工具函数名称不能为空")
