import json
import os
import sys
import signal
import threading
import subprocess

from core.project_reloader import start as start_project_reloader
from core.sender import configure as configure_sender
from core.weflow_media import configure as configure_weflow_media
from core.windows_awake import start as start_windows_awake
from core.queue import task_queue
from core.weflow_client import WeFlowClient
from core.router import Router
from core.dispatcher import Dispatcher
from core.worker import Worker
from core.weflow_headless import WeFlowCore
from core.wechat_sender import start_server as start_sender_server
from utils.dedup import Dedup
from utils.sqlite_store import migrate_legacy_storage


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)


# 全局引用，供信号处理器使用
_weflow: WeFlowCore | None = None


def graceful_shutdown(signum, frame):
    """Ctrl+C → 先停 weflow-core → 再退出"""
    print("\n⚠️  收到终止信号，正在优雅退出...", flush=True)
    if _weflow:
        _weflow.stop()
    sys.exit(0)


signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


def _launch_weflow_setup_wizard():
    """启动 weflow-core 交互式配置向导（独立终端窗口）

    当 config.json 配置不完整时，弹出一个可见的终端窗口，
    让用户通过 weflow-core 的交互式引导完成配置。
    向导完成后自动退出，由 start.bat 重新启动。
    """
    weflow_dir = os.path.join(BASE_DIR, "weflow-core")
    dist_js = os.path.join(weflow_dir, "dist", "index.js")
    config_path = os.path.join(BASE_DIR, "config.json")

    if not os.path.isfile(dist_js):
        print("[ERROR] weflow-core 未构建，请先运行 start.bat 完成首次构建", flush=True)
        sys.exit(1)

    if sys.platform == "win32":
        # Windows: 使用 start 打开新终端窗口，/wait 等待用户完成
        cmd = (
            f'start "WeFlow 配置向导" /wait cmd /c '
            f'"cd /d {weflow_dir} && node dist/index.js {config_path} && echo. && echo 配置完成，按任意键继续... && pause >nul"'
        )
    else:
        # macOS / Linux: 尝试打开新终端
        term = os.environ.get("TERM_PROGRAM", "")
        if term == "Apple_Terminal" or sys.platform == "darwin":
            cmd = (
                f"osascript -e 'tell app \"Terminal\" to do script "
                f"\"cd {weflow_dir} && node dist/index.js {config_path}; echo; echo 配置完成，按回车继续...; read\"'"
            )
        else:
            cmd = (
                f"x-terminal-emulator -e "
                f"bash -c 'cd {weflow_dir} && node dist/index.js {config_path}; echo; echo 配置完成，按回车继续...; read'"
            )

    try:
        subprocess.run(cmd, shell=True)
    except Exception as e:
        print(f"[ERROR] 无法启动配置向导: {e}", flush=True)
        print("[HINT] 请手动运行: cd weflow-core && node dist/index.js ../config.json", flush=True)
        sys.exit(1)


def main():
    global _weflow

    try:
        config = json.load(open("config.json", encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"\n[ERROR] 无法加载 config.json: {e}", flush=True)
        print("[HINT] 请复制 sample_config.json 为 config.json 并填入真实值", flush=True)
        sys.exit(1)

    # ── 自动生成 API Token（内部鉴权用，无需用户手动填写）──
    _token = config.get("token", "").strip()
    if not _token or _token == "your-token-here":
        import secrets
        _token = secrets.token_hex(32)
        config["token"] = _token
        try:
            json.dump(config, open("config.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"[INFO] 已自动生成 API Token: {_token[:8]}...", flush=True)
        except Exception as e:
            print(f"[WARN] 无法保存自动生成的 Token: {e}", flush=True)

    # ── 启动前配置预检 ──
    _weflow_cfg = config.get("weflow", {})

    # weflow 相关缺失项（可通过 weflow-core 向导自动配置）
    _weflow_errors = []
    if not _weflow_cfg.get("dbPath", "").strip():
        _weflow_errors.append("weflow.dbPath 未填写（微信文件目录）")
    if not _weflow_cfg.get("decryptKey", "").strip() or len(_weflow_cfg.get("decryptKey", "")) != 64:
        _weflow_errors.append("weflow.decryptKey 未填写（64位十六进制解密密钥）")
    if not _weflow_cfg.get("myWxid", "").strip():
        _weflow_errors.append("weflow.myWxid 未填写（微信账号 wxid）")

    # 仅 target_group 缺失（weflow-core 向导无法处理，直接报错退出）
    _tg_missing = not config.get("target_group") or config["target_group"] == ["Your Target Group Name"]

    if _weflow_errors:
        print("\n[INFO] 检测到 weflow 配置不完整，正在启动 weflow-core 交互式配置向导...", flush=True)
        for err in _weflow_errors:
            print(f"  - {err}", flush=True)
        if _tg_missing:
            print("  - target_group 未填写（向导完成后请在 config.json 中手动设置）", flush=True)
        _launch_weflow_setup_wizard()
        # 向导完成后退出，由 start.bat 自动重启加载新配置
        print("\n[INFO] 配置向导已关闭，正在重启...", flush=True)
        sys.exit(0)

    if _tg_missing:
        print("\n[ERROR] target_group 未填写（请设置为你的目标群名）", flush=True)
        print("[HINT] 编辑 config.json，将 target_group 改为你的微信群名称", flush=True)
        sys.exit(1)

    migrate_legacy_storage()
    start_windows_awake()
    configure_sender(config)
    configure_weflow_media(config)
    start_project_reloader()

    # ═══════════════════════════════════════
    # � 启动微信发送服务（localhost:9999）
    # ═══════════════════════════════════════
    start_sender_server()

    # ═══════════════════════════════════════
    # �🚀 启动 weflow-core（Node.js 无头服务）
    # ═══════════════════════════════════════
    print("[bot] 正在启动 weflow-core...", flush=True)
    _weflow = WeFlowCore(BASE_DIR)
    if not _weflow.start():
        print("[bot] ❌ weflow-core 启动失败，退出", flush=True)
        sys.exit(1)

    dedup = Dedup()

    router = Router(
        prefix=config.get("prefix", "@服务器状态@我"),
        target_group=config.get("target_group", []),
        time_slots=config.get("time_slots", []),
        rate_limit_cfg=config.get("rate_limit", {}),
        prefix_mode=config.get("prefix_mode", "only")
    )

    dispatcher = Dispatcher()
    dispatcher.load_plugins()
    dispatcher.init_plugins(config)   # ⭐如果你需要 config

    worker = Worker(task_queue, router, dispatcher, config=config)
    worker.start(config["worker_num"])

    client = WeFlowClient(
        token=config["token"],
        queue=task_queue,
        dedup=dedup
    )

    threading.Thread(target=client.start, daemon=False).start()


if __name__ == "__main__":
    main()
