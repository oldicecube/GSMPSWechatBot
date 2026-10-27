from datetime import datetime
from typing import Dict, Optional

import json

from src.common.database.database_model import ToolRecord

from . import BaseDatabaseDataModel


class MaiToolRecord(BaseDatabaseDataModel[ToolRecord]):
    """工具调用记录数据模型。"""

    def __init__(
        self,
        tool_id: str,
        timestamp: datetime,
        session_id: str,
        tool_name: str,
        tool_reasoning: Optional[str] = None,
        tool_data: Optional[Dict] = None,
    ):
        self.tool_id = tool_id
        self.timestamp = timestamp
        self.session_id = session_id
        self.tool_name = tool_name
        self.tool_reasoning = tool_reasoning
        self.tool_data = tool_data or {}

    @classmethod
    def from_db_instance(cls, db_record: ToolRecord):
        """从数据库实例创建数据模型对象。"""
        return cls(
            tool_id=db_record.tool_id,
            timestamp=db_record.timestamp,
            session_id=db_record.session_id,
            tool_name=db_record.tool_name,
            tool_reasoning=db_record.tool_reasoning,
            tool_data=json.loads(db_record.tool_data) if db_record.tool_data else None,
        )

    def to_db_instance(self):
        """将数据模型对象转换为数据库实例。"""
        return ToolRecord(
            tool_id=self.tool_id,
            timestamp=self.timestamp,
            session_id=self.session_id,
            tool_name=self.tool_name,
            tool_reasoning=self.tool_reasoning,
            tool_data=json.dumps(self.tool_data) if self.tool_data else None,
        )
