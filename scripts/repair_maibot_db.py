"""Repair the MaiBot chat database (data/MaiBot.db) without losing history.

Dry-run by default: only inspects the database and prints the plan. Pass
``--apply`` (with the bot stopped) to rebuild the file from SQLite's
``.recover`` output, validate it, back up the damaged original and swap it in.

The bot writes chat history on every inbound message, so a malformed file
aborts shell commands ("插件执行异常") long before the plugin itself runs. A
nearly-full disk is the usual root cause of that corruption, so free space is
checked before anything is rebuilt.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "MaiBot.db"

# Tables the shell and the MaiBot core require before the bot can boot cleanly.
REQUIRED_TABLES = {
    "binary_data", "bot_platform_accounts", "chat_sessions", "expressions",
    "high_frequency_terms", "images", "jargons", "llm_usage", "lost_and_found",
    "mai_messages", "maisaka_monitor_events", "model_task_usage",
    "one_time_maintenance_tasks", "online_time", "person_info",
    "statistics_aggregation_cursors", "statistics_message_hourly",
    "statistics_model_hourly", "statistics_tool_hourly", "tool_records",
}

BOT_ENTRYPOINTS = {"main.py", "webui.py"}
HEADROOM_BYTES = 250 * 1024 * 1024


def find_sqlite_cli(explicit: str | None) -> Path:
    candidates = [explicit, os.environ.get("MAIBOT_SQLITE3"), shutil.which("sqlite3")]
    candidates += [str(p) for p in sorted((ROOT / "temp").glob("db-*/tools/sqlite3.exe"))]
    candidates += [str(p) for p in sorted((ROOT / "temp").glob("*/sqlite3.exe"))]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate).resolve()
    raise SystemExit(
        "找不到 sqlite3 命令行工具。请从 https://www.sqlite.org/download.html 下载 "
        "sqlite-tools-win-x64-*.zip 解压后，用 --sqlite <path> 指定 sqlite3.exe，"
        "或设置环境变量 MAIBOT_SQLITE3。")


def running_bot_processes() -> list[dict]:
    try:
        import psutil
    except ImportError:
        return []
    found = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            cmdline = process.info.get("cmdline") or []
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if process.info["pid"] == os.getpid():
            continue
        joined = " ".join(cmdline).lower()
        if "python" not in (process.info.get("name") or "").lower() and "python" not in joined:
            continue
        # Compare the script basename so helpers such as napcat_lan_webui.py
        # are not mistaken for the bot entrypoint.
        scripts = {os.path.basename(str(token).strip(chr(34) + chr(39))).lower()
                   for token in cmdline if str(token).strip().lower().endswith(".py")}
        if not (scripts & BOT_ENTRYPOINTS):
            continue
        found.append({"pid": process.info["pid"], "cmdline": " ".join(cmdline)[:240]})
    return found


def db_holders(path: Path) -> list[int]:
    try:
        import psutil
    except ImportError:
        return []
    target = str(path.resolve()).lower()
    holders = []
    for process in psutil.process_iter(["pid"]):
        try:
            for opened in process.open_files():
                if str(opened.path).lower() == target:
                    holders.append(process.info["pid"])
                    break
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return holders


def integrity(path: Path) -> tuple[bool, list[str]]:
    """Read-only integrity probe; never mutates the file under test."""
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error as exc:
        return False, [f"无法打开数据库: {exc}"]
    try:
        rows = [row[0] for row in connection.execute("PRAGMA integrity_check").fetchall()]
    except sqlite3.Error as exc:
        return False, [f"integrity_check 失败: {exc}"]
    finally:
        connection.close()
    return rows == ["ok"], [str(row) for row in rows[:20]]


def table_names(path: Path) -> set[str]:
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error:
        return set()
    try:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    except sqlite3.Error:
        return set()
    finally:
        connection.close()


def message_stats(path: Path) -> dict:
    stats: dict = {}
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as exc:
        return {"error": str(exc)}
    try:
        for table, column in (("mai_messages", "timestamp"), ("person_info", None),
                              ("chat_sessions", None), ("expressions", None)):
            try:
                if column:
                    row = connection.execute(
                        f"SELECT COUNT(*), MIN({column}), MAX({column}) FROM {table}").fetchone()
                    stats[table] = {"rows": row[0], "min": row[1], "max": row[2]}
                else:
                    stats[table] = {"rows": connection.execute(
                        f"SELECT COUNT(*) FROM {table}").fetchone()[0]}
            except sqlite3.Error as exc:
                stats[table] = {"error": str(exc)}
    finally:
        connection.close()
    return stats


def run_sqlite(cli: Path, db: Path, statement: str, *, timeout: int = 1800) -> str:
    completed = subprocess.run([str(cli), str(db), statement], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if completed.returncode != 0:
        raise RuntimeError(f"sqlite3 执行失败 ({completed.returncode}): {statement[:60]} :: "
                           f"{(completed.stderr or completed.stdout).strip()[:400]}")
    return completed.stdout


def checkpoint_wal(cli: Path, path: Path) -> str:
    """Fold WAL frames into the working copy so .recover can still see them.

    A damaged database often refuses the checkpoint; the caller only logs this.
    """
    try:
        return run_sqlite(cli, path, "PRAGMA wal_checkpoint(TRUNCATE);", timeout=600).strip()
    except Exception as exc:  # noqa: BLE001 - best effort only
        return f"checkpoint failed: {exc}"


def recover_into(cli: Path, source: Path, workspace: Path, *, keep_sql: bool = False) -> Path:
    dump = workspace / "recovered.sql"
    rebuilt = workspace / "recovered.db"
    for stale in (rebuilt, Path(str(rebuilt) + "-wal"), Path(str(rebuilt) + "-shm")):
        if stale.exists():
            stale.unlink()
    with dump.open("w", encoding="utf-8", newline="\n") as handle:
        completed = subprocess.run([str(cli), str(source), ".recover"], stdout=handle,
                                   stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   errors="replace", timeout=3600)
    if completed.returncode != 0:
        raise RuntimeError(f".recover 失败: {(completed.stderr or '').strip()[:400]}")
    if dump.stat().st_size == 0:
        raise RuntimeError(".recover 未产出任何 SQL")
    with dump.open("r", encoding="utf-8", errors="replace") as handle:
        completed = subprocess.run([str(cli), str(rebuilt)], stdin=handle,
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=3600)
    if completed.returncode != 0:
        raise RuntimeError(f"导入 recovered.sql 失败: "
                           f"{(completed.stderr or completed.stdout).strip()[:400]}")
    if not keep_sql:
        dump.unlink()
    return rebuilt


def prepare(db: Path, backup_dir: Path) -> dict:
    """Copy the damaged database (with WAL/SHM) so recovery never touches live files."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    copies = {}
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(db) + suffix)
        if not source.exists():
            continue
        destination = backup_dir / f"MaiBot.db{suffix}"
        shutil.copy2(source, destination)
        copies[suffix or "main"] = str(destination)
    return copies


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(str(path.parent if path.suffix else path)).free


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="修复 MaiBot 聊天数据库")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--sqlite", default=None)
    parser.add_argument("--apply", action="store_true", help="真正执行修复并替换生产库")
    parser.add_argument("--force", action="store_true", help="即使 integrity_check 通过也重建")
    parser.add_argument("--backup-dir", default=None)
    parser.add_argument("--report", default=None)
    parser.add_argument("--keep-sql", action="store_true", help="保留中间 recovered.sql 便于排查")
    parser.add_argument("--assume-stopped", action="store_true",
                        help="跳过进程占用检查（仅在修复离线副本时使用）")
    args = parser.parse_args(argv)

    db = Path(args.db).resolve()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = Path(args.backup_dir) if args.backup_dir else ROOT / "temp" / f"db-repair-{stamp}"
    report: dict = {"database": str(db), "apply": bool(args.apply), "steps": []}

    def note(step: str, **payload):
        report["steps"].append({"step": step, **payload})
        rendered = " ".join(f"{k}={str(v)[:180]}" for k, v in payload.items())
        print(f"[{step}] {rendered}")

    if not db.exists():
        note("missing", path=str(db))
        print("数据库文件不存在，无需修复。")
        return 0

    healthy, detail = integrity(db)
    note("integrity", healthy=healthy, detail=detail[:5])
    report["healthy_before"] = healthy
    if healthy and not args.force:
        print("数据库完整性检查通过，无需修复。使用 --force 可强制重建。")
        report["result"] = "healthy"
        write_report(args.report, report)
        return 0

    if args.assume_stopped:
        holders, running = [], []
        print("警告: 已按 --assume-stopped 跳过进程占用检查。")
    else:
        holders = db_holders(db)
        running = running_bot_processes()
    note("locks", file_holders=holders, bot_processes=[p["pid"] for p in running])
    report["bot_running"] = bool(running or holders)
    if args.apply and (running or holders):
        print("检测到 bot 仍在运行（或仍持有数据库文件）。请先手动停止 bot 再执行 --apply。")
        report["result"] = "blocked-bot-running"
        write_report(args.report, report)
        return 2

    cli = find_sqlite_cli(args.sqlite)
    note("sqlite_cli", path=str(cli))
    report["sqlite_cli"] = str(cli)

    before_tables = table_names(db)
    before_stats = message_stats(db)
    note("before", tables=len(before_tables), messages=before_stats.get("mai_messages", {}))
    report["before"] = {"tables": sorted(before_tables), "stats": before_stats}

    size = db.stat().st_size
    available = free_bytes(db)
    note("space", database_mb=round(size / 1048576, 1), free_mb=round(available / 1048576, 1))
    report["space"] = {"database": size, "free": available}

    if not args.apply:
        print("\n=== DRY RUN ===")
        print("损坏摘要: " + " | ".join(str(line)[:120] for line in str(detail[0]).splitlines()[:3]))
        print(f"可读取表数量: {len(before_tables)}")
        print(f"计划: 备份到 {backup_dir} -> checkpoint WAL -> .recover -> 校验 -> 原子替换 {db}")
        if available < size + HEADROOM_BYTES:
            print(f"警告: 剩余空间不足，至少需要 {(size + HEADROOM_BYTES) / 1048576:.0f}MB。")
        print("确认 bot 已停止后，重新执行并追加 --apply。")
        report["result"] = "dry-run"
        write_report(args.report, report)
        return 0

    if available < size + HEADROOM_BYTES:
        print(f"磁盘空间不足: 剩余 {available / 1048576:.0f}MB，至少需要 "
              f"{(size + HEADROOM_BYTES) / 1048576:.0f}MB。请先清理 D 盘再重试。")
        report["result"] = "no-space"
        write_report(args.report, report)
        return 4

    workspace = backup_dir / "work"
    workspace.mkdir(parents=True, exist_ok=True)
    copies = prepare(db, backup_dir / "damaged")
    note("backup", files=list(copies))
    report["backup"] = copies
    working = backup_dir / "damaged" / "MaiBot.db"
    note("checkpoint", result=checkpoint_wal(cli, working))

    try:
        rebuilt = recover_into(cli, working, workspace, keep_sql=args.keep_sql)
        note("recover", path=str(rebuilt), size=rebuilt.stat().st_size)
    except Exception as exc:  # noqa: BLE001 - report and keep the original in place
        note("recover_failed", error=str(exc))
        report["result"] = "recover-failed"
        write_report(args.report, report)
        return 3

    ok, rebuilt_detail = integrity(rebuilt)
    rebuilt_tables = table_names(rebuilt)
    rebuilt_stats = message_stats(rebuilt)
    missing = sorted(REQUIRED_TABLES - rebuilt_tables)
    note("validate", integrity_ok=ok, detail=rebuilt_detail[:5], tables=len(rebuilt_tables),
         missing=missing, messages=rebuilt_stats.get("mai_messages", {}))
    report["after_validation"] = {"integrity_ok": ok, "detail": rebuilt_detail,
                                  "tables": sorted(rebuilt_tables), "missing": missing,
                                  "stats": rebuilt_stats}
    if not ok or missing:
        print("重建库未通过校验，生产库保持不变。")
        report["result"] = "validation-failed"
        write_report(args.report, report)
        return 3

    before_max = (before_stats.get("mai_messages") or {}).get("max")
    after_max = (rebuilt_stats.get("mai_messages") or {}).get("max")
    report["message_window"] = {"before_max": before_max, "after_max": after_max}
    if before_max and after_max and str(after_max) < str(before_max):
        print(f"警告: 重建库最新消息 {after_max} 早于原库 {before_max}；"
              "请确认 bot 已停止后重跑，否则会丢掉最新记录。")
        report["result"] = "stale-recovery"
        write_report(args.report, report)
        return 3

    try:
        run_sqlite(cli, rebuilt, "PRAGMA journal_mode=WAL;")
    except Exception as exc:  # noqa: BLE001 - the app re-applies WAL on connect
        note("wal_warning", error=str(exc))

    try:
        os.replace(rebuilt, db)
    except OSError as exc:
        note("swap_failed", error=str(exc))
        report["result"] = "swap-failed"
        write_report(args.report, report)
        return 3
    for suffix in ("-wal", "-shm"):
        leftover = Path(str(db) + suffix)
        if leftover.exists():
            leftover.unlink()
    note("swapped", path=str(db))

    final_ok, final_detail = integrity(db)
    final_stats = message_stats(db)
    note("verify", integrity_ok=final_ok, detail=final_detail[:5],
         messages=final_stats.get("mai_messages", {}))
    report["final"] = {"integrity_ok": final_ok, "detail": final_detail, "stats": final_stats}
    if not final_ok:
        if working.exists():
            shutil.copy2(working, db)
            note("rollback", source=str(working))
        report["result"] = "rolled-back"
        write_report(args.report, report)
        return 3

    report["result"] = "repaired"
    write_report(args.report, report)
    print(f"\n修复完成。原始损坏库备份在 {backup_dir / 'damaged'}，请手动重启 bot。")
    return 0


def write_report(path: str | None, report: dict) -> None:
    if not path:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())

