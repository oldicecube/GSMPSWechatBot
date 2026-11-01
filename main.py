import json
import os
import sys
import signal
import threading
import subprocess
import time

# [HOT-RELOAD DISABLED] from core.project_reloader import start as start_project_reloader
# 鐑噸杞藉凡鎸夌敤鎴疯姹傛殏鏃剁鐢紙2026-09-08锛夛紱濡傞渶鎭㈠锛屽彇娑堜笂闈?import 涓庝笅鏂硅皟鐢ㄥ鐨勬敞閲婂嵆鍙?
from core.sender import configure as configure_sender
from core.weflow_media import configure as configure_weflow_media
from core.windows_awake import start as start_windows_awake
from core.queue import task_queue
from core.weflow_client import WeFlowClient
from core.router import Router
from core.dispatcher import Dispatcher
from core.worker import Worker
from core.weflow_headless import WeFlowCore
from core.wechat_sender import start_server as start_sender_server, stop_server
from utils.dedup import Dedup
from utils.sqlite_store import migrate_legacy_storage


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)

# 鍏ㄥ眬寮曠敤锛屼緵淇″彿澶勭悊鍣ㄤ娇鐢?
_weflow: WeFlowCore | None = None
_dispatcher: Dispatcher | None = None
_worker: Worker | None = None


def _request_process_restart():
    """Schedule an in-process restart requested by the authenticated WebUI."""
    def restart():
        time.sleep(0.75)
        try:
            if _worker is not None:
                _worker.shutdown()
        except Exception:
            pass
        try:
            if _dispatcher is not None:
                _dispatcher.shutdown()
        except Exception:
            pass
        try:
            stop_server()
        except Exception:
            pass
        try:
            if _weflow is not None:
                _weflow.stop()
        except Exception:
            pass
        os.execv(sys.executable, [sys.executable, *sys.argv])

    threading.Thread(target=restart, name="webui-requested-restart", daemon=True).start()
    return {"scheduled": True}


def graceful_shutdown(signum, frame):
    """Stop accepting work, drain owned tasks, then stop child services."""
    global _dispatcher, _worker, _weflow
    print("\n鏀跺埌缁堟淇″彿锛屾鍦ㄤ紭闆呴€€鍑?..", flush=True)
    if _worker is not None:
        try:
            _worker.shutdown()
        except Exception:
            pass
    if _dispatcher is not None:
        try:
            _dispatcher.shutdown()
        except Exception:
            pass
    try:
        stop_server()
    except Exception:
        pass
    if _weflow is not None:
        try:
            _weflow.stop()
        except Exception:
            pass
    sys.exit(0)


signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


def _launch_weflow_setup_wizard():
    """Explain standalone setup; the local-only WebUI is not required."""
    print("[INFO] 请先完成 config.json 中的 WeFlow 配置。", flush=True)
    print("[HINT] 微信登录后，在交互终端运行 setup_wizard.bat 获取密钥。", flush=True)
    print("[HINT] 也可从 WeFlow GUI 获取配置，手动填写 dbPath、decryptKey、myWxid。", flush=True)
    print("[HINT] 随后配置 target_group 与 LLM API，再运行 start.bat。", flush=True)
    print("[SAFE] 此处不会启动服务、操作微信或修改系统时间。", flush=True)

def main():
    global _weflow, _dispatcher, _worker

    # NapCat is independent of the bot lifetime. Reuse it if running; otherwise
    # attempt saved-account login and expose its local QR page when needed.
    try:
        from core.napcat_boot import ensure_napcat
        ensure_napcat(BASE_DIR)
    except Exception as exc:
        print('[QQ STARTUP]', type(exc).__name__, 'see depends-data/napcat/config/bootstrap.log', flush=True)

    try:
        config = json.load(open("config.json", encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"\n[ERROR] 鏃犳硶鍔犺浇 config.json: {e}", flush=True)
        print("[HINT] 请复制 sample_config.json 为 config.json 并填入真实配置。", flush=True)
        sys.exit(1)

    # 鈹€鈹€ 鑷姩鐢熸垚 API Token锛堝唴閮ㄩ壌鏉冪敤锛屾棤闇€鐢ㄦ埛鎵嬪姩濉啓锛夆攢鈹€
    _token = config.get("token", "").strip()
    if not _token or _token in {"your-token-here", "your-weflow-token"}:
        import secrets
        _token = secrets.token_hex(32)
        config["token"] = _token
        try:
            json.dump(config, open("config.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"[INFO] 宸茶嚜鍔ㄧ敓鎴?API Token: {_token[:8]}...", flush=True)
        except Exception as e:
            print(f"[WARN] 鏃犳硶淇濆瓨鑷姩鐢熸垚鐨?Token: {e}", flush=True)

    # 鈹€鈹€ 鍚姩鍓嶉厤缃妫€ 鈹€鈹€
    _weflow_cfg = config.get("weflow", {})

    # weflow 鐩稿叧缂哄け椤癸紙鍙€氳繃 weflow-core 鍚戝鑷姩閰嶇疆锛?
    _weflow_errors = []
    if not _weflow_cfg.get("dbPath", "").strip():
        _weflow_errors.append("weflow.dbPath is required (WeChat data directory)")
    if not _weflow_cfg.get("decryptKey", "").strip() or len(_weflow_cfg.get("decryptKey", "")) != 64:
        _weflow_errors.append("weflow.decryptKey is required (64-character hex key)")
    if not _weflow_cfg.get("myWxid", "").strip():
        _weflow_errors.append("weflow.myWxid is required (WeChat wxid)")

    # 浠?target_group 缂哄け锛坵eflow-core 鍚戝鏃犳硶澶勭悊锛岀洿鎺ユ姤閿欓€€鍑猴級
    _tg_missing = not config.get("target_group") or config["target_group"] == ["Your Target Group Name"]

    if _weflow_errors:
        print("\n[INFO] 妫€娴嬪埌 weflow 閰嶇疆涓嶅畬鏁达紝姝ｅ湪鍚姩 weflow-core 浜や簰寮忛厤缃悜瀵?..", flush=True)
        for err in _weflow_errors:
            print(f"  - {err}", flush=True)
        if _tg_missing:
            print("  - target_group is missing; set it in config.json after setup", flush=True)
        _launch_weflow_setup_wizard()
        # 鍚戝瀹屾垚鍚庨€€鍑猴紝鐢?start.bat 鑷姩閲嶅惎鍔犺浇鏂伴厤缃?
        print("\n[INFO] 閰嶇疆鍚戝宸插叧闂紝姝ｅ湪閲嶅惎...", flush=True)
        sys.exit(0)

    if _tg_missing:
        print("[ERROR] target_group is missing; configure your target group first", flush=True)
        print("[HINT] Edit config.json; see README.MD and config_guide.md.", flush=True)
        sys.exit(1)

    migrate_legacy_storage()
    start_windows_awake()
    configure_sender(config)
    configure_weflow_media(config)
    # [HOT-RELOAD DISABLED]     start_project_reloader()  # 鏆傛椂绂佺敤鐑噸杞?

    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
    # 锟?鍚姩寰俊鍙戦€佹湇鍔★紙localhost:9999锛?
    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
    if not start_sender_server(config=config):
        print("[bot] persistent WeChat send HTTP service failed to start; bot startup aborted", flush=True)
        sys.exit(1)

    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
    # 锟金煔€ 鍚姩 weflow-core锛圢ode.js 鏃犲ご鏈嶅姟锛?
    # 鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺愨晲鈺?
    print("[bot] starting weflow-core...", flush=True)
    _weflow = WeFlowCore(BASE_DIR)
    if not _weflow.start():
        print("[bot] weflow-core failed to start; exiting", flush=True)
        sys.exit(1)

    dedup = Dedup()

    llm_config = config.get("llm", {}) or {}
    configured_prefix_mode = str(config.get("prefix_mode", "strict") or "strict").strip().lower()
    if configured_prefix_mode == "only":
        configured_prefix_mode = "strict"
    if configured_prefix_mode not in {"strict", "mixed"}:
        configured_prefix_mode = "strict"
    auto_reply_config = llm_config.get("auto_reply")
    if not isinstance(auto_reply_config, dict):
        auto_reply_config = {}

    # Mixed mode enables the LLM state machine by default. Strict mode releases
    # only explicit prefix/@/reply-to-Bot messages to the same model-led reply chain.
    proactive_enabled = bool(
        llm_config.get("enabled")
        and configured_prefix_mode == "mixed"
        and auto_reply_config.get("enabled", True)
    )

    router = Router(
        prefix=config.get("prefix", ["@bot"]),
        target_group=config.get("target_group", []),
        time_slots=config.get("time_slots", []),
        rate_limit_cfg=config.get("rate_limit", {}),
        prefix_mode=configured_prefix_mode,
        proactive_enabled=proactive_enabled
    )

    dispatcher = Dispatcher()
    _dispatcher = dispatcher
    dispatcher.load_plugins()
    dispatcher.init_plugins(config)   # 猸愬鏋滀綘闇€瑕?config

    worker = Worker(task_queue, router, dispatcher, config=config)
    _worker = worker
    worker.start(config["worker_num"])

    client = WeFlowClient(
        token=config["token"],
        queue=task_queue,
        dedup=dedup
    )

    # Optional WebUI attachment. Disabled unless config.webui.enabled=true;
    # when enabled, the authenticated WebUI is attached to live BotControl.
    webui_config = config.get("webui") if isinstance(config.get("webui"), dict) else {}
    env_webui_enabled = str(os.environ.get("WECHATBOT_WEBUI_ENABLED", "")).strip().lower() in {"1", "true", "yes", "on"}
    if llm_config.get('model_config_source') != 'maibot' and (bool(webui_config.get("enabled", False)) or env_webui_enabled):
        try:
            webui_config = dict(webui_config)
            webui_config["enabled"] = True
            webui_config["host"] = str(os.environ.get("WECHATBOT_WEBUI_HOST") or webui_config.get("host") or "127.0.0.1")
            webui_config["port"] = int(os.environ.get("WECHATBOT_WEBUI_PORT") or webui_config.get("port", 8765) or 8765)
            allowed_networks = os.environ.get("WECHATBOT_WEBUI_ALLOWED_NETWORKS", "").strip()
            if allowed_networks:
                webui_config["allowed_networks"] = [item.strip() for item in allowed_networks.split(",") if item.strip()]
            config["webui"] = webui_config
            from webui.control import BotControl
            from webui.runtime import RuntimeSnapshot
            from webui.server import ensure_webui_jwt, start_embedded
            ensure_webui_jwt(config, persist=True)
            runtime_snapshot = RuntimeSnapshot(
                config=config,
                dispatcher=dispatcher,
                worker=worker,
                weflow=_weflow,
            )
            runtime_control = BotControl(
                config=config,
                dispatcher=dispatcher,
                worker=worker,
                router=router,
                weflow=_weflow,
                config_path=os.path.join(BASE_DIR, "config.json"),
                restart_callback=_request_process_restart,
                dry_run=True,
            )
            print(f"[webui] JWT bootstrap available locally at http://127.0.0.1:{webui_config['port']}/api/v1/auth/bootstrap", flush=True)
            start_embedded(
                host=str(webui_config.get("host") or "127.0.0.1"),
                port=int(webui_config.get("port", 8765) or 8765),
                provider=runtime_snapshot,
                control=runtime_control,
                dry_run=True,
            )
        except Exception as exc:
            print(f"[webui] optional attachment failed: {type(exc).__name__}: {exc}", flush=True)

    # SSE receiving is the main service loop. Keeping it in the main thread
    # keeps process lifecycle and signal handling in one well-defined place.
    print("[bot] SSE client running in main thread", flush=True)
    client.start()


if __name__ == "__main__":
    main()
