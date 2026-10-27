from copy import deepcopy
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Type, cast

from pydantic import BaseModel
from typing_extensions import Required, TypedDict


class RespFormatType(Enum):
    """响应格式类型。"""

    TEXT = "text"
    JSON_OBJ = "json_object"
    JSON_SCHEMA = "json_schema"


class JsonSchema(TypedDict, total=False):
    """内部使用的 JSON Schema 包装结构。"""

    name: Required[str]
    description: Optional[str]
    schema: Dict[str, Any]
    strict: Optional[bool]


def _json_schema_type_check(instance: Mapping[str, Any]) -> str | None:
    """检查 JSON Schema 包装结构是否合法。

    Args:
        instance: 待检查的 JSON Schema 包装字典。

    Returns:
        str | None: 不合法时返回错误信息，合法时返回 `None`。
    """
    if "name" not in instance:
        return "schema必须包含'name'字段"
    if not isinstance(instance["name"], str) or instance["name"].strip() == "":
        return "schema的'name'字段必须是非空字符串"
    if "description" in instance and (
        not isinstance(instance["description"], str) or instance["description"].strip() == ""
    ):
        return "schema的'description'字段只能填入非空字符串"
    if "schema" not in instance:
        return "schema必须包含'schema'字段"
    if not isinstance(instance["schema"], dict):
        return "schema的'schema'字段必须是字典，详见https://json-schema.org/"
    if "strict" in instance and not isinstance(instance["strict"], bool):
        return "schema的'strict'字段只能填入布尔值"
    return None


def _remove_title(schema: Dict[str, Any] | List[Any]) -> Dict[str, Any] | List[Any]:
    """递归移除 JSON Schema 中的 `title` 字段。

    Args:
        schema: 待处理的 Schema 树。

    Returns:
        Dict[str, Any] | List[Any]: 处理后的 Schema 树。
    """
    if isinstance(schema, list):
        for index, item in enumerate(schema):
            if isinstance(item, (dict, list)):
                schema[index] = _remove_title(item)
        return schema

    if "title" in schema:
        del schema["title"]
    for key, value in schema.items():
        if isinstance(value, (dict, list)):
            schema[key] = _remove_title(value)
    return schema


def _link_definitions(schema: Dict[str, Any]) -> Dict[str, Any]:
    """展开 Schema 中的本地 `$defs`/`$ref` 引用。

    Args:
        schema: 待处理的根 Schema。

    Returns:
        Dict[str, Any]: 展开后的 Schema。
    """

    def link_definitions_recursive(
        path: str,
        sub_schema: Dict[str, Any] | List[Any],
        definitions: Dict[str, Any],
    ) -> Dict[str, Any] | List[Any]:
        """递归展开局部定义。

        Args:
            path: 当前递归路径。
            sub_schema: 当前子 Schema。
            definitions: 已收集的定义字典。

        Returns:
            Dict[str, Any] | List[Any]: 展开后的子 Schema。
        """
        if isinstance(sub_schema, list):
            for index, item in enumerate(sub_schema):
                if isinstance(item, (dict, list)):
                    sub_schema[index] = link_definitions_recursive(f"{path}/{index}", item, definitions)
            return sub_schema

        if "$defs" in sub_schema:
            key_prefix = f"{path}/$defs/"
            defs_payload = cast(Dict[str, Any], sub_schema["$defs"])
            for key, value in defs_payload.items():
                definition_key = key_prefix + key
                if definition_key not in definitions:
                    definitions[definition_key] = value
            del sub_schema["$defs"]

        if "$ref" in sub_schema:
            definition_key = cast(str, sub_schema["$ref"])
            if definition_key in definitions:
                return definitions[definition_key]
            raise ValueError(f"Schema中引用的定义'{definition_key}'不存在")

        for key, value in sub_schema.items():
            if isinstance(value, (dict, list)):
                sub_schema[key] = link_definitions_recursive(f"{path}/{key}", value, definitions)
        return sub_schema

    return cast(Dict[str, Any], link_definitions_recursive("#", schema, {}))


def _remove_defs(schema: Dict[str, Any] | List[Any]) -> Dict[str, Any] | List[Any]:
    """递归移除 JSON Schema 中的 `$defs` 字段。

    Args:
        schema: 待处理的 Schema 树。

    Returns:
        Dict[str, Any] | List[Any]: 处理后的 Schema 树。
    """
    if isinstance(schema, list):
        for index, item in enumerate(schema):
            if isinstance(item, (dict, list)):
                schema[index] = _remove_defs(item)
        return schema

    if "$defs" in schema:
        del schema["$defs"]
    for key, value in schema.items():
        if isinstance(value, (dict, list)):
            schema[key] = _remove_defs(value)
    return schema


class RespFormat:
    """统一响应格式定义。"""

    @staticmethod
    def _generate_schema_from_model(schema_model: Type[BaseModel]) -> JsonSchema:
        """从 Pydantic 模型生成内部 JSON Schema 包装结构。

        Args:
            schema_model: Pydantic 模型类。

        Returns:
            JsonSchema: 内部统一 JSON Schema 包装结构。
        """
        schema_tree = deepcopy(schema_model.model_json_schema())
        json_schema: JsonSchema = {
            "name": schema_model.__name__,
            "schema": cast(
                Dict[str, Any],
                _remove_defs(_link_definitions(cast(Dict[str, Any], _remove_title(schema_tree)))),
            ),
            "strict": False,
        }
        if schema_model.__doc__:
            json_schema["description"] = schema_model.__doc__
        return json_schema

    def __init__(
        self,
        format_type: RespFormatType = RespFormatType.TEXT,
        schema: Type[BaseModel] | JsonSchema | None = None,
    ) -> None:
        """初始化响应格式对象。

        Args:
            format_type: 响应格式类型。
            schema: 模型类或 JSON Schema 包装结构，仅 `JSON_SCHEMA` 模式使用。
        """
        self.format_type: RespFormatType = format_type
        self.schema_source: Type[BaseModel] | JsonSchema | None = schema
        self.schema: JsonSchema | None = None

        if format_type != RespFormatType.JSON_SCHEMA:
            return
        if schema is None:
            raise ValueError("当format_type为'JSON_SCHEMA'时，schema不能为空")
        if isinstance(schema, dict):
            if check_msg := _json_schema_type_check(schema):
                raise ValueError(f"schema格式不正确，{check_msg}")
            self.schema = cast(JsonSchema, deepcopy(schema))
            return
        if isinstance(schema, type) and issubclass(schema, BaseModel):
            try:
                self.schema = self._generate_schema_from_model(schema)
            except Exception as exc:
                raise ValueError(
                    f"自动生成JSON Schema时发生异常，请检查模型类{schema.__name__}的定义，详细信息：\n"
                    f"{schema.__name__}:\n"
                ) from exc
            return
        raise ValueError("schema必须是BaseModel的子类或JsonSchema")

    def get_schema_object(self) -> Dict[str, Any] | None:
        """获取内部包装中的对象级 JSON Schema。

        Returns:
            Dict[str, Any] | None: 对象级 JSON Schema；不存在时返回 `None`。
        """
        if self.schema is None:
            return None
        schema_payload = self.schema.get("schema")
        if isinstance(schema_payload, dict):
            return cast(Dict[str, Any], deepcopy(schema_payload))
        return None

    def to_dict(self) -> Dict[str, Any]:
        """将响应格式转换为字典。

        Returns:
            Dict[str, Any]: 序列化后的响应格式字典。
        """
        if self.schema:
            return {
                "format_type": self.format_type.value,
                "schema": self.schema,
            }
        return {
            "format_type": self.format_type.value,
        }
