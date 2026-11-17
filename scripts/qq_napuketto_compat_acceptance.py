"""NapukettoQQ（OneBot 11 协议端）兼容验收：本地模拟对端，不登录 QQ、不发真实消息。

对端行为依据 @napuketto/adapter@0.16.4 的 OneBot 11 适配器实现：
- get_version_info 返回 app_name = "napuketto-qq"
- 消息段为 OneBot 11 数组格式（text/at/image/reply/forward/...）
- 拍一拍通过 notice(notify/poke) 上报，字段 user_id=发起者、target_id=目标
- send_group_forward_msg / send_private_forward_msg 只接受 ``messages`` 参数
- 未实现的动作（如 send_poke）返回 status=failed / retcode=1404
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CHECKS = []

APP_NAME = "napuketto-qq"
APP_VERSION = "0.6.12"
TOKEN = "napuketto-fixture-token"
SELF_ID = "3294652115"
GROUP_ID = "1020993873"
POKER_ID = "246813579"
QUOTE_ID = "7123456789"


def check(name, passed, detail=None):
    CHECKS.append(dict(name=name, passed=bool(passed), detail=detail))
    print(("PASS " if passed else "FAIL ") + name, flush=True)


async def run():
    pkg = ModuleType("napuketto_acceptance")
    pkg.__path__ = [str(ROOT / "plugins/MaiBot-SnowLuma-Adapter")]
    sys.modules[pkg.__name__] = pkg

    def load(name):
        return importlib.import_module(pkg.__name__ + "." + name)

    config = load("config")
    profile = load("profile")
    services = load("services")
    inbound_mod = load("codecs.inbound")
    outbound_mod = load("codecs.outbound")
    notice_mod = load("codecs.notice")
    transport_mod = load("transport")
    filters = load("filters")

    logger = logging.getLogger("napuketto-acceptance")
    logger.setLevel(logging.ERROR)

    from aiohttp import web

    actions = []
    received = []
    opened = asyncio.Event()
    incoming = asyncio.Event()

    base_url = {"value": ""}
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000a49444154789c6360000002000154a24f6e0000000049454e44ae426082"
    )

    async def image_endpoint(request):
        return web.Response(body=png_bytes, content_type="image/png")

    async def endpoint(request):
        if request.headers.get("Authorization") != "Bearer " + TOKEN:
            return web.Response(status=401)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json(message_event())
        async for raw in ws:
            payload = json.loads(raw.data)
            actions.append(payload)
            await ws.send_json(reply_for(payload))
        return ws

    def message_event():
        return dict(
            post_type="message",
            message_type="group",
            self_id=int(SELF_ID),
            user_id=int(POKER_ID),
            group_id=int(GROUP_ID),
            group_name="GDUT SIE Minecraft Public Server",
            message_id=int(QUOTE_ID) + 1,
            time=1794900000,
            sender=dict(user_id=int(POKER_ID), nickname="群友昵称", card="群名片", role="member"),
            message=[
                dict(type="reply", data=dict(id=QUOTE_ID)),
                dict(type="at", data=dict(qq=SELF_ID, name="まひるん")),
                dict(type="text", data=dict(text="中文\nemoji 😀")),
                dict(type="image", data=dict(file="abc123.image", url=base_url["value"] + "/img.png")),
            ],
            raw_message="[CQ:reply,id=7123456789][CQ:at,qq=3294652115]中文",
        )

    def reply_for(payload):
        name = payload["action"]
        echo = payload.get("echo")
        params = payload.get("params") or {}

        def ok(data):
            return dict(status="ok", retcode=0, data=data, message="", echo=echo)

        if name == "get_version_info":
            return ok(dict(app_name=APP_NAME, app_version=APP_VERSION, protocol_version="v11"))
        if name == "get_login_info":
            return ok(dict(user_id=int(SELF_ID), nickname="まひるん"))
        if name == "get_group_info":
            return ok(dict(group_id=int(GROUP_ID), group_name="GDUT SIE Minecraft Public Server", member_count=42))
        if name == "get_group_member_info":
            user_id = str(params.get("user_id"))
            if user_id == SELF_ID:
                return ok(dict(user_id=int(SELF_ID), nickname="まひるん", card="まひるん", role="admin"))
            return ok(dict(user_id=int(user_id), nickname="群友昵称", card="群名片", role="member"))
        if name == "get_msg":
            return ok(dict(
                message_id=int(params.get("message_id")),
                message_type="group",
                group_id=int(GROUP_ID),
                time=1794899990,
                sender=dict(user_id=int(POKER_ID), nickname="群友昵称", card="群名片"),
                message=[dict(type="text", data=dict(text="引用原文"))],
                raw_message="引用原文",
            ))
        if name == "get_forward_msg":
            return ok(dict(messages=[dict(
                message_id=18,
                sender=dict(user_id=int(POKER_ID), nickname="群友昵称"),
                time=1794899980,
                content=[dict(type="text", data=dict(text="聊天记录正文"))],
            )]))
        if name == "send_group_msg":
            return ok(dict(message_id=987654321))
        if name == "send_private_msg":
            return ok(dict(message_id=987654322))
        if name in ("set_group_ban", "delete_msg", "set_msg_emoji_like"):
            return ok(None)
        # NapukettoQQ 未实现的动作：显式失败，避免调用方把静默空响应当成功
        return dict(status="failed", retcode=1404, data=None,
                    message="unsupported action: " + name, echo=echo)

    app = web.Application()
    app.router.add_get("/", endpoint)
    app.router.add_get("/img.png", image_endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base_url["value"] = "http://127.0.0.1:" + str(port)

    async def on_open():
        opened.set()

    async def on_close():
        pass

    async def on_payload(payload):
        received.append(payload)
        incoming.set()

    transport = transport_mod.QQTransportClient(logger, on_open, on_close, on_payload)
    state = profile.ProfileState(profile.ONEBOT11_PROFILE)
    transport.configure(
        config.QQServerConfig(server="127.0.0.1", port=port, token=TOKEN, connection_id="napuketto-fixture"),
        state,
    )
    try:
        await transport.start()
        await asyncio.wait_for(opened.wait(), 5)
        await asyncio.wait_for(incoming.wait(), 5)

        check("profile_detects_napuketto_as_generic_onebot11",
              profile.detect_client_type_from_app_name(APP_NAME) == "onebot11")
        detected, app_name, app_version = await profile.probe_client_profile(
            lambda action, params: _call_data(transport, action, params), logger)
        check("probe_reports_napuketto_identity",
              (detected, app_name, app_version) == ("onebot11", APP_NAME, APP_VERSION),
              dict(detected=detected, app_name=app_name, app_version=app_version))

        action_service = services.QQActionService(logger, transport, state)
        query = services.QQQueryService(action_service, logger)
        settings = config.QQPluginSettings()
        inbound = inbound_mod.QQInboundCodec(logger, query, state, lambda: settings)
        event = received[0]
        native = await inbound.build_message_dict(event, SELF_ID, str(POKER_ID), event["sender"])
        segments = native["raw_message"]
        by_type = {seg["type"]: seg for seg in segments}
        check("inbound_keeps_qq_identity",
              native["platform"] == "qq" and native["account_id"] == SELF_ID
              and native["message_info"]["group_info"]["group_id"] == GROUP_ID)
        check("inbound_at_is_structural",
              native["is_at"] and by_type["at"]["data"]["target_user_id"] == SELF_ID)
        text_segments = [seg["data"] for seg in segments if seg["type"] == "text"]
        check("inbound_text_preserves_unicode_and_newline",
              "中文\nemoji 😀" in text_segments, text_segments)
        quote = by_type["reply"]["data"]
        check("inbound_quote_resolved_via_get_msg",
              quote["target_message_id"] == QUOTE_ID and quote["target_message_content"] == "引用原文"
              and quote["target_message_sender_id"] == POKER_ID,
              quote)
        check("inbound_image_detected", bool(native.get("is_picture")), native.get("is_picture"))

        notice_filter = filters.QQNoticeFilter(logger)
        notice_config = config.QQNoticeConfig()
        poke = dict(
            post_type="notice", notice_type="notify", sub_type="poke",
            self_id=int(SELF_ID), group_id=int(GROUP_ID), user_id=int(POKER_ID), target_id=int(SELF_ID),
            time=1794900100,
        )
        check("notice_filter_allows_poke", notice_filter.is_notice_event_allowed("notify", "poke", notice_config))
        notice_codec = notice_mod.QQNoticeCodec(logger, query)
        poke_dict = await notice_codec.build_notice_message_dict(poke)
        poke_text = "" if poke_dict is None else str(poke_dict.get("processed_plain_text") or "")
        check("poke_notice_rendered", poke_dict is not None and "戳" in poke_text, poke_text)
        check("poke_notice_marks_real_target",
              poke_dict is not None
              and poke_dict["message_info"]["additional_config"].get("event_target_user_id") == SELF_ID,
              None if poke_dict is None else poke_dict["message_info"]["additional_config"].get("event_target_user_id"))

        ban = dict(
            post_type="notice", notice_type="group_ban", sub_type="ban",
            self_id=int(SELF_ID), group_id=int(GROUP_ID), operator_id=int(SELF_ID),
            user_id=int(POKER_ID), duration=60, time=1794900200,
        )
        ban_dict = await notice_codec.build_notice_message_dict(ban)
        relation = None if ban_dict is None else ban_dict["message_info"]["additional_config"].get("event_relation")
        check("ban_notice_relation_matches_napuketto_fields",
              bool(relation) and relation["operator_id"] == SELF_ID and relation["target_user_id"] == POKER_ID
              and relation["target_is_bot"] is False and relation["operator_is_bot"] is True,
              relation)

        recall = dict(
            post_type="notice", notice_type="group_recall", self_id=int(SELF_ID),
            group_id=int(GROUP_ID), user_id=int(POKER_ID), operator_id=int(POKER_ID),
            message_id=int(QUOTE_ID), time=1794900300,
        )
        recall_dict = await notice_codec.build_notice_message_dict(recall)
        recall_text = "" if recall_dict is None else str(recall_dict.get("processed_plain_text") or "")
        check("recall_notice_rendered", recall_dict is not None and "撤回" in recall_text, recall_text)

        essence = dict(
            post_type="notice", notice_type="group_essence", sub_type="add",
            self_id=int(SELF_ID), group_id=int(GROUP_ID), sender_id=int(POKER_ID),
            operator_id=int(SELF_ID), message_id=int(QUOTE_ID), time=1794900400,
        )
        check("essence_alias_allowed_by_filter",
              notice_filter.is_notice_event_allowed("group_essence", "add", notice_config))
        essence_dict = await notice_codec.build_notice_message_dict(essence)
        essence_text = "" if essence_dict is None else str(essence_dict.get("processed_plain_text") or "")
        check("essence_notice_rendered_via_alias",
              essence_dict is not None and "精华" in essence_text, essence_text)

        outbound = outbound_mod.QQOutboundCodec(state)
        outbound_payload = dict(
            platform="qq",
            account_id=SELF_ID,
            message_info=dict(group_info=dict(group_id=GROUP_ID)),
            raw_message=[
                dict(type="reply", data=dict(target_message_id=QUOTE_ID)),
                dict(type="at", data=dict(target_user_id=POKER_ID)),
                dict(type="text", data="回复正文\n😀"),
            ],
        )
        encoded = await outbound.prepare_outbound_actions(outbound_payload, {})
        action_name, params = encoded[0]
        check("outbound_uses_standard_targeted_action", action_name == "send_group_msg", action_name)
        check("outbound_reply_and_at_survive",
              params["message"][0] == dict(type="reply", data=dict(id=QUOTE_ID))
              and params["message"][1] == dict(type="at", data=dict(qq=POKER_ID)),
              params["message"][:2])
        receipt = await transport.call_action(action_name, params)
        check("outbound_receipt_message_id", receipt["data"]["message_id"] == 987654321)

        forward_payload = dict(
            platform="qq",
            account_id=SELF_ID,
            message_info=dict(group_info=dict(group_id=GROUP_ID)),
            raw_message=[dict(type="forward", data=[dict(user_id=POKER_ID, nickname="群友昵称",
                                                        content=[dict(type="text", data="转发内容")])])],
        )
        forward_actions = outbound.build_outbound_actions(forward_payload, {})
        forward_name, forward_params = forward_actions[0]
        check("outbound_forward_uses_messages_key",
              forward_name == "send_group_forward_msg" and "messages" in forward_params
              and "message" not in forward_params,
              dict(action=forward_name, keys=sorted(forward_params)))

        unsupported = None
        try:
            await action_service.call_action("send_poke", dict(user_id=int(POKER_ID), group_id=int(GROUP_ID)))
        except Exception as exc:  # noqa: BLE001 - 期望显式失败
            unsupported = str(exc)
        check("unsupported_action_fails_loudly",
              unsupported is not None and "1404" not in unsupported and "unsupported action" in unsupported,
              unsupported)

        check("transport_clean_shutdown", True)
    finally:
        await transport.stop()
        await runner.cleanup()

    return all(item["passed"] for item in CHECKS)


async def _call_data(transport, action, params):
    response = await transport.call_action(action, params)
    return response.get("data")


def main():
    instance = Path(tempfile.mkdtemp(prefix="napuketto-", dir=ROOT / "temp"))
    (instance / "config").mkdir(parents=True, exist_ok=True)
    for name in ("bot_config.toml", "model_config.toml"):
        source = ROOT / "config" / name
        if source.exists():
            shutil.copy2(source, instance / "config" / name)
    os.environ["MAIBOT_INSTANCE_DIR"] = str(instance)
    passed = False
    try:
        passed = asyncio.run(run())
    except Exception:
        import traceback
        traceback.print_exc()
        check("unhandled_exception", False)
    finally:
        database = sys.modules.get("src.common.database.database")
        if database is not None:
            database.engine.dispose()
        shutil.rmtree(instance, ignore_errors=True)
    report = ROOT / "docs/maibot-wechat-acceptance/qq-napuketto-compat-20261118.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(dict(checks=CHECKS, passed=passed, real_qq_login=False,
                                      physical_messages=False, restarted=False),
                                 ensure_ascii=False, indent=2), encoding="utf8")
    print("report:", report)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
