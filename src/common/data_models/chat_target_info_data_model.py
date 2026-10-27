from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from . import BaseDataModel

if TYPE_CHECKING:
    from src.common.data_models.person_info_data_model import MaiPersonInfo


@dataclass
class ChatTargetInfo(BaseDataModel):
    """当前聊天目标的轻量摘要信息。

    该模型只服务于 Planner、Replyer 和 HFC 等聊天编排层，
    用于描述“这一轮对话里我面对的是谁”。
    它不是人物档案模型，不承载记忆点、关系历史或统计信息。

    Attributes:
        platform: 目标所在的平台标识。
        user_id: 目标在平台上的原始用户 ID。
        session_nickname: 当前会话里观测到的昵称，优先使用消息现场值。
        person_id: 主程序内部稳定人物 ID；未建档时为空。
        person_name: 主程序内部维护的人物名称；未建档时为空。
        is_known: 该目标是否已经建立人物档案。
    """

    platform: str = field(default_factory=str)
    user_id: str = field(default_factory=str)
    session_nickname: str = field(default_factory=str)
    person_id: Optional[str] = None
    person_name: Optional[str] = None
    is_known: bool = False

    @property
    def display_name(self) -> str:
        """返回用于 Prompt、日志和界面展示的目标名称。"""
        return self.person_name or self.session_nickname or self.user_id

    @classmethod
    def from_person_info(
        cls,
        platform: str,
        user_id: str,
        session_nickname: str = "",
        person_info: Optional["MaiPersonInfo"] = None,
    ) -> "ChatTargetInfo":
        """根据当前会话信息和人物档案生成聊天目标摘要。

        Args:
            platform: 当前聊天平台。
            user_id: 当前聊天目标的原始用户 ID。
            session_nickname: 当前会话里观察到的昵称。
            person_info: 可选的人物档案对象。

        Returns:
            ChatTargetInfo: 生成后的轻量聊天目标信息。
        """
        if person_info is None:
            return cls(
                platform=platform,
                user_id=user_id,
                session_nickname=session_nickname,
                is_known=False,
            )

        return cls(
            platform=platform,
            user_id=user_id,
            session_nickname=session_nickname or person_info.user_nickname,
            person_id=person_info.person_id,
            person_name=person_info.person_name,
            is_known=person_info.is_known,
        )
