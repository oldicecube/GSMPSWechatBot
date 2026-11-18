"""客户端画像：NapCat / SnowLuma 能力差异定义与自动判定。

两类客户端都讲 OneBot v11 风格 WebSocket，但在以下维度存在实现差异，
统一由画像（ClientProfile）声明，编解码与传输层按画像取值：

1. 发送消息动作：NapCat 用 ``send_group_msg`` / ``send_private_msg``，SnowLuma 用通用 ``send_msg``。
2. 出站文件：SnowLuma 需要拆成独立 ``upload_group_file`` / ``upload_private_file`` 动作，
   NapCat 支持内联 ``file`` 段。
3. 合并转发参数键：NapCat 为 ``message``，SnowLuma 为 ``messages``。
4. 语音：SnowLuma 返回 silk 编码，需要 pysilk + ffmpeg 转码；NapCat 可直接要 wav。
5. QZone 系列 API 仅 SnowLuma 实现。

token 鉴权不区分画像：传输层同时附带 ``Authorization: Bearer`` 头与 URL
``access_token`` 参数（OneBot v11 两种标准方式），两类服务端各取所需，
这也是 auto 模式下唯一可行的方式——探测发生在握手之后。

对端类型判定基于标准动作 ``get_version_info`` 返回的 ``app_name``：
该字段由各实现自报家门（SnowLuma 返回 "SnowLuma"，对端返回 "NapCat"），
属于确定性识别而非启发式猜测。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

import asyncio

PROBE_ACTION = "get_version_info"
PROBE_ATTEMPTS = 2
PROBE_RETRY_INTERVAL_SEC = 1.0

MSG_ACTION_STYLE_TARGETED = "targeted"
MSG_ACTION_STYLE_GENERIC = "generic"

FORWARD_PARAM_KEY_MESSAGE = "message"
FORWARD_PARAM_KEY_MESSAGES = "messages"

QZONE_API_ACTIONS: Tuple[Tuple[str, str], ...] = (
    ("get_qzone_msg_list", "获取 QZone 好友动态列表"),
    ("get_qzone_feeds", "获取 QZone 个人档与动态"),
    ("send_qzone_msg", "发表 QZone 说说"),
    ("delete_qzone_msg", "删除 QZone 说说"),
    ("like_qzone", "点赞 QZone 内容"),
    ("unlike_qzone", "取消 QZone 点赞"),
    ("comment_qzone", "评论 QZone 内容"),
)


@dataclass(frozen=True)
class ClientProfile:
    """单个客户端实现的能力画像。"""

    client_type: str
    expected_app_name: str
    msg_action_style: str
    split_file_upload: bool
    forward_param_key: str
    record_out_format: str
    transcode_silk_voice: bool
    supports_qzone: bool
    # 对端是否用严格数值 schema 校验 ID 字段（NapukettoQQ 的 zod schema 只接受 number）。
    # 为 True 时动作层会把纯数字字符串的 ID 字段转成 int，避免 1400 参数校验失败。
    coerce_numeric_ids: bool = False
    # 合并转发能否携带「自造节点（name/uin/content）」。
    # NapCat 接受自造节点；NapukettoQQ 只按 ``data.id`` 反查已有消息，
    # 自造节点会被静默跳过，因此该画像下退化为单条文本消息。
    forward_supports_synthetic_nodes: bool = True


NAPCAT_PROFILE = ClientProfile(
    client_type="napcat",
    expected_app_name="napcat",
    msg_action_style=MSG_ACTION_STYLE_TARGETED,
    split_file_upload=False,
    forward_param_key=FORWARD_PARAM_KEY_MESSAGE,
    record_out_format="wav",
    transcode_silk_voice=False,
    supports_qzone=False,
)

SNOWLUMA_PROFILE = ClientProfile(
    client_type="snowluma",
    expected_app_name="snowluma",
    msg_action_style=MSG_ACTION_STYLE_GENERIC,
    split_file_upload=True,
    forward_param_key=FORWARD_PARAM_KEY_MESSAGES,
    record_out_format="mp3",
    transcode_silk_voice=True,
    supports_qzone=True,
)

# 通用 OneBot 11 实现（LLBot / NapukettoQQ / 其他标准实现）。
# 采用 OneBot 11 标准的分目标动作名，与 NapCat 画像一致；不支持 QZone 与
# SnowLuma 专属的语音转码/分片上传。
# 合并转发参数键按 OneBot 11 规范取 ``messages``：NapukettoQQ 的
# send_group_forward_msg / send_private_forward_msg 只接受 ``messages``，
# 传 ``message`` 会被 schema 校验拒绝（2026-11-18 依据 @napuketto/adapter 实测源码）。
ONEBOT11_PROFILE = ClientProfile(
    client_type="onebot11",
    expected_app_name="",
    msg_action_style=MSG_ACTION_STYLE_TARGETED,
    split_file_upload=False,
    forward_param_key=FORWARD_PARAM_KEY_MESSAGES,
    record_out_format="wav",
    transcode_silk_voice=False,
    supports_qzone=False,
    coerce_numeric_ids=True,
    forward_supports_synthetic_nodes=False,
)

# 需要 ``number`` 类型 ID 的参数键。NapukettoQQ 用 zod 严格校验，
# 传字符串会直接 1400 参数校验失败；NapCat 会自行宽松转换。
NUMERIC_PARAM_KEYS: Tuple[str, ...] = (
    "group_id",
    "user_id",
    "target_id",
    "duration",
    "times",
    "emoji_id",
    "emoji_type",
    "count",
    "file_count",
)


def coerce_numeric_params(params: Mapping[str, Any]) -> Dict[str, Any]:
    """把纯数字字符串的 ID 类参数转成 ``int``。

    仅用于声明了 ``coerce_numeric_ids`` 的画像（当前是通用 OneBot 11），
    其它画像保持原有宽松传参行为，避免影响 NapCat / SnowLuma。

    Args:
        params: 待发送的 OneBot 动作参数。

    Returns:
        Dict[str, Any]: 归一化后的参数副本（不修改入参）。
    """

    normalized: Dict[str, Any] = {}
    for key, value in params.items():
        if key in NUMERIC_PARAM_KEYS and isinstance(value, str):
            text = value.strip()
            if text and (text.isdigit() or (text[0] == "-" and text[1:].isdigit())):
                normalized[key] = int(text)
                continue
        normalized[key] = value
    return normalized


PROFILE_BY_TYPE: Dict[str, ClientProfile] = {
    NAPCAT_PROFILE.client_type: NAPCAT_PROFILE,
    SNOWLUMA_PROFILE.client_type: SNOWLUMA_PROFILE,
    ONEBOT11_PROFILE.client_type: ONEBOT11_PROFILE,
}


class ProfileState:
    """当前连接生效画像的可变容器。

    连接建立前以配置声明（或 auto 的临时占位）初始化；
    ``get_version_info`` 探测完成后由路由器写回判定结果。
    """

    def __init__(self, initial_profile: ClientProfile) -> None:
        """初始化画像容器。

        Args:
            initial_profile: 初始生效画像。
        """
        self._profile = initial_profile

    @property
    def profile(self) -> ClientProfile:
        """返回当前生效画像。"""

        return self._profile

    def update(self, profile: ClientProfile) -> ClientProfile:
        """写入新的生效画像并返回。

        Args:
            profile: 探测判定后的画像。

        Returns:
            ClientProfile: 写入后的当前画像。
        """
        self._profile = profile
        return self._profile


def detect_client_type_from_app_name(app_name: str) -> Optional[str]:
    """从 ``get_version_info`` 的 app_name 判定客户端类型。

    Args:
        app_name: 对端自报的实现名称。

    Returns:
        Optional[str]: 判定出的客户端类型；``app_name`` 为空时返回 ``None``，
        其余无法识别的实现回退到通用 OneBot 11 画像。
    """

    normalized_app_name = str(app_name or "").strip().lower()
    if not normalized_app_name:
        return None
    if "snowluma" in normalized_app_name:
        return SNOWLUMA_PROFILE.client_type
    if "napcat" in normalized_app_name:
        return NAPCAT_PROFILE.client_type
    # 其余 OneBot 11 实现（LLBot / NapukettoQQ / 自建实现等）统一按通用画像处理：
    # 标准动作名与 NapCat 画像一致，避免每出现一个新实现就无法连接。
    return ONEBOT11_PROFILE.client_type


async def probe_client_profile(
    call_action_data: Callable[[str, Optional[Dict[str, Any]]], Awaitable[Dict[str, Any]]],
    logger: Any,
) -> Tuple[str, str, str]:
    """探测对端客户端类型。

    Args:
        call_action_data: 发送 OneBot 动作并返回 ``data`` 字段的回调。
        logger: 插件日志对象。

    Returns:
        Tuple[str, str, str]: ``(client_type, app_name, app_version)``。

    Raises:
        RuntimeError: 探测动作失败或对端类型无法识别时抛出（不静默猜测）。
    """

    last_error: Optional[Exception] = None
    version_info: Any = None
    for attempt in range(1, PROBE_ATTEMPTS + 1):
        try:
            version_info = await call_action_data(PROBE_ACTION, {})
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(f"客户端类型探测动作 {PROBE_ACTION} 失败，第 {attempt}/{PROBE_ATTEMPTS} 次: {exc}")
            if attempt < PROBE_ATTEMPTS:
                await asyncio.sleep(PROBE_RETRY_INTERVAL_SEC)

    if last_error is not None:
        raise RuntimeError(f"无法通过对端 {PROBE_ACTION} 探测客户端类型: {last_error}") from last_error

    if not isinstance(version_info, Mapping):
        raise RuntimeError(f"对端 {PROBE_ACTION} 响应缺少 data 字段，无法判定客户端类型")

    app_name = str(version_info.get("app_name") or version_info.get("app") or "").strip()
    app_version = str(version_info.get("app_version") or version_info.get("version") or "").strip()
    client_type = detect_client_type_from_app_name(app_name)
    if client_type is None:
        logger.warning(
            "对端 get_version_info 未提供 app_name，按通用 OneBot 11 画像连接；"
            "如需特定实现的行为请在 client.client_type 中显式指定"
        )
        client_type = ONEBOT11_PROFILE.client_type
    return client_type, app_name, app_version
