from typing import Optional

import hashlib

from rich.traceback import install
from sqlmodel import col, select

from src.common.data_models.person_info_data_model import MaiPersonInfo
from src.common.database.database import get_db_session
from src.common.database.database_model import PersonInfo
from src.common.logger import get_logger

install(extra_lines=3)

logger = get_logger("person_utils")


class PersonUtils:
    @staticmethod
    def get_person_info_by_id(person_id: str) -> Optional[MaiPersonInfo]:
        """根据person_id获取用户信息"""
        try:
            with get_db_session() as session:
                statement = select(PersonInfo).filter_by(person_id=person_id).limit(1)
                if result := session.exec(statement).first():
                    return MaiPersonInfo.from_db_instance(result)
        except Exception as e:
            logger.error(f"查询用户信息失败: {str(e)}")
        return None

    @staticmethod
    def calculate_person_id(platform: str, user_id: str) -> str:
        """根据平台和用户ID计算person_id"""

        normalized_platform = platform.split("-", 1)[1] if "-" in platform else platform
        return hashlib.md5(f"{normalized_platform}_{user_id}".encode("utf-8")).hexdigest()

    @staticmethod
    def get_person_info_by_user_id_and_platform(user_id: str, platform: str) -> Optional[MaiPersonInfo]:
        """根据user_id和platform获取用户信息"""

        normalized_platform = platform.split("-", 1)[1] if "-" in platform else platform
        try:
            with get_db_session() as session:
                statement = (
                    select(PersonInfo)
                    .where(col(PersonInfo.platform) == normalized_platform)
                    .where(col(PersonInfo.user_id) == user_id)
                    .limit(1)
                )
                if result := session.exec(statement).first():
                    return MaiPersonInfo.from_db_instance(result)
        except Exception as e:
            logger.error(f"根据 user_id 和 platform 查询用户信息失败: {str(e)}")
        return None
