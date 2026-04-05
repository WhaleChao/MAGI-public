"""
Process Guardian Skill (程序守護者)
==================================
Monitors key system processes to ensure they are healthy and not duplicated.
Reports on process status — actual killing is delegated to daemon.py unified reaper.
"""

import psutil
import os
import signal
import logging
import time

logger = logging.getLogger("ProcessGuardian")

def _write_autopilot_kill_reason(pid: int, reason: str) -> None:
    """寫入 kill reason — 同時寫入統一日誌及 per-PID 檔（供 autopilot 讀取後刪除）。"""
    try:
        magi_root = os.environ.get("MAGI_ROOT_DIR", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        # Per-PID file for autopilot signal handler to read
        reason_path = os.path.join(magi_root, f"_autopilot_kill_reason_{pid}")
        with open(reason_path, "w", encoding="utf-8") as f:
            f.write(reason)
        # Consolidated log (append-only)
        import datetime as _dt
        import json as _json
        log_path = os.path.join(magi_root, "_autopilot_kill_log.jsonl")
        entry = _json.dumps({"ts": _dt.datetime.now().isoformat(), "pid": pid, "reason": reason}, ensure_ascii=False)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 23, exc_info=True)

def get_running_processes(script_name):
    """
    Finds all running python processes executing the given script.
    Also matches module-style invocations (e.g. "python -m api.server"
    when searching for "api/server.py").
    Returns a list of dicts: {'pid': int, 'create_time': float, 'cmdline': list}
    """
    matches = []
    # Build alternative patterns for module-style invocation
    # "api/server.py" → also match "api.server" (python -m style)
    alt_names = [script_name]
    if "/" in script_name:
        module_name = script_name.replace("/", ".").removesuffix(".py")
        alt_names.append(module_name)
    try:
        current_pid = os.getpid()
        for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
            try:
                cmdline = proc.info.get('cmdline')
                if cmdline and len(cmdline) > 1:
                    # Check if python and script name matches
                    if 'python' in proc.info['name'].lower() or 'python' in cmdline[0]:
                        # Match both "python3 api/server.py" and "python -m api.server"
                        if any(alt in arg for arg in cmdline for alt in alt_names):
                            matches.append({
                                'pid': proc.info['pid'],
                                'create_time': proc.info['create_time'],
                                'cmdline': cmdline,
                                'process_obj': proc
                            })
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                continue
    except Exception as e:
        logger.error(f"Error iterating processes: {e}")

    return matches

def check_and_clean_duplicates(script_name="api/discord_bot.py"):
    """
    Checks for duplicates of a script (report-only, no killing).
    Actual duplicate cleanup is handled by daemon.py unified reaper.
    """
    procs = get_running_processes(script_name)
    count = len(procs)

    report = f"🛡️ **Process Guardian Report: {script_name}**\n"
    report += f"Found {count} running instance(s).\n"

    if count <= 1:
        report += "✅ Status: Healthy (Single or No Instance)."
        return report

    # Sort by creation time (descending: newest first)
    procs.sort(key=lambda x: x['create_time'], reverse=True)

    keeper = procs[0]
    duplicates = procs[1:]

    report += f"⚠️ **Duplicate Alert!** Newest is PID {keeper['pid']}.\n"
    report += f"🔍 **Detected {len(duplicates)} older duplicate(s):**\n"
    for dup in duplicates:
        age_sec = int(time.time() - dup['create_time'])
        report += f"   - PID {dup['pid']} (age: {age_sec}s)\n"
    report += "ℹ️ Daemon unified reaper 會自動處理重複進程。"

    return report

def force_kill_all(script_name="api/discord_bot.py"):
    """
    Kills ALL instances of a script.
    (Kept for daemon startup cleanup — direct kill is intentional here.)
    """
    procs = get_running_processes(script_name)
    if not procs:
        return f"No instances of {script_name} found."

    report = f"🔪 **Force Killing All: {script_name}**\n"
    for p in procs:
        try:
            _write_autopilot_kill_reason(p['pid'], f"process_guardian force_kill_all({script_name})")
            os.kill(p['pid'], signal.SIGTERM)
            report += f"   - Killed PID {p['pid']}\n"
        except Exception as e:
            report += f"   - Error killing {p['pid']}: {e}\n"

    return report

def is_daemon_running():
    """Checks if the Daemon Supervisor is active."""
    return len(get_running_processes("daemon.py")) > 0


# ════════════════════════════════════════════════════════════════
# Stale Code Auto-Reload (過期程式碼自動重載)
# ════════════════════════════════════════════════════════════════

from dotenv import load_dotenv
load_dotenv()
_DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MAGI_ROOT = str(os.environ.get("MAGI_ROOT", _DEFAULT_ROOT))

# Map: process script name → (source files to watch, restart command)
_RELOAD_TARGETS = {
    "api/discord_bot.py": {
        "watch": [
            "api/discord_bot.py",
            "api/orchestrator.py",
            "api/tw_output_guard.py",
        ],
        "cmd": ["python3", "api/discord_bot.py"],
        "log": "/tmp/magi_discord.log",
        "label": "Discord Bot",
    },
    "api/server.py": {
        "watch": [
            "api/server.py",
            "api/orchestrator.py",
            "api/tw_output_guard.py",
        ],
        "cmd": ["python3", "api/server.py"],
        "log": "/tmp/magi_server.log",
        "label": "MAGI Server",
    },
}


def _get_latest_mtime(file_list: list[str]) -> tuple[float, str]:
    """
    Return the latest modification time and the filename among a list of files.
    Paths are relative to _MAGI_ROOT.
    """
    latest = 0.0
    latest_file = ""
    for f in file_list:
        full = os.path.join(_MAGI_ROOT, f)
        try:
            mt = os.path.getmtime(full)
            if mt > latest:
                latest = mt
                latest_file = f
        except Exception:
            continue
    return latest, latest_file


def reload_stale_services(dry_run: bool = False) -> str:
    """
    Check if any MAGI service processes are running stale code.
    If a watched source file has been modified AFTER the process started,
    kill the process and restart it.

    Returns a formatted report.
    """
    import subprocess
    import threading
    import time as _time

    reloaded = []
    up_to_date = []
    errors = []

    for script_name, cfg in _RELOAD_TARGETS.items():
        procs = get_running_processes(script_name)
        if not procs:
            continue

        # Use the newest process instance
        procs.sort(key=lambda x: x['create_time'], reverse=True)
        proc = procs[0]
        proc_start = proc['create_time']
        pid = proc['pid']

        # Check if any watched file is newer than the process
        latest_mtime, changed_file = _get_latest_mtime(cfg["watch"])

        if latest_mtime <= 0:
            continue

        if latest_mtime > proc_start:
            # Source file changed after process started → stale!
            age_sec = int(_time.time() - proc_start)
            change_ago = int(_time.time() - latest_mtime)

            if dry_run:
                reloaded.append(
                    f"⚠️ **{cfg['label']}** (PID {pid}) 已過期\n"
                    f"   程式碼 `{changed_file}` 在 {change_ago}s 前更新，"
                    f"但程序啟動於 {age_sec}s 前"
                )
                continue

            # Kill and restart
            try:
                os.kill(pid, signal.SIGTERM)
                _time.sleep(2)

                # Kill any remaining instances
                for p in procs[1:]:
                    try:
                        os.kill(p['pid'], signal.SIGTERM)
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 225, exc_info=True)
                _time.sleep(1)

                # Restart
                _log_path = cfg.get("log")
                if _log_path:
                    _log_fh = open(_log_path, "a")
                    _p = subprocess.Popen(
                        cfg["cmd"],
                        stdout=_log_fh,
                        stderr=subprocess.STDOUT,
                        cwd=_MAGI_ROOT,
                        start_new_session=True,
                    )
                    threading.Thread(target=_p.wait, daemon=True).start()
                else:
                    _p = subprocess.Popen(
                        cfg["cmd"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        cwd=_MAGI_ROOT,
                        start_new_session=True,
                    )
                    threading.Thread(target=_p.wait, daemon=True).start()
                _time.sleep(2)

                # Verify restart
                new_procs = get_running_processes(script_name)
                if new_procs:
                    new_pid = new_procs[0]['pid']
                    reloaded.append(
                        f"🔄 **{cfg['label']}** 已自動重啟\n"
                        f"   原因：`{changed_file}` 已更新\n"
                        f"   舊 PID {pid} → 新 PID {new_pid}"
                    )
                else:
                    errors.append(
                        f"❌ **{cfg['label']}** 重啟失敗（已殺舊進程 PID {pid}，但新進程未啟動）"
                    )
            except Exception as e:
                errors.append(f"❌ {cfg['label']} 重載失敗: {e}")
        else:
            up_to_date.append(f"✅ {cfg['label']} (PID {pid}) — 程式碼最新")

    # Build report
    if not reloaded and not errors:
        if up_to_date:
            return "✅ **熱重載巡檢完成** — 所有服務程式碼都是最新版本。\n" + "\n".join(up_to_date)
        return "✅ **熱重載巡檢完成** — 沒有執行中的 MAGI 服務需要檢查。"

    lines = ["🔄 **熱重載巡檢報告** (Hot Reload Patrol)"]
    if dry_run:
        lines.append("⚠️ *模擬模式 — 未實際重啟*\n")

    for r in reloaded:
        lines.append(r)
    for e in errors:
        lines.append(e)
    for u in up_to_date:
        lines.append(u)

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════════
# Backward-compatible patrol_zombies — delegates to daemon reaper
# ════════════════════════════════════════════════════════════════

def patrol_zombies(
    max_age_minutes: int = 30,
    dry_run: bool = False,
    report_even_clean: bool = True,
) -> str:
    """
    Delegated to daemon.py unified reaper.
    Kept for backward compatibility (orchestrator imports).
    """
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(_MAGI_ROOT)) if _MAGI_ROOT else ".")
        from daemon import reap_orphan_workers, get_reap_report
        reap_orphan_workers(force=True, dry_run=dry_run)
        report = get_reap_report()
        if not report and report_even_clean:
            return "✅ **殭屍巡邏完成** — 系統乾淨，沒有發現殭屍程序。"
        return report or ""
    except Exception as e:
        logger.error(f"patrol_zombies delegation failed: {e}")
        return f"❌ 殭屍巡邏失敗（delegation error）: {e}"


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        if sys.argv[1] == "patrol":
            print(patrol_zombies(dry_run="--dry" in sys.argv))
        elif sys.argv[1] == "reload":
            print(reload_stale_services(dry_run="--dry" in sys.argv))
        else:
            target = sys.argv[1]
            print(f"Running Process Guardian on: {target}")
            print(check_and_clean_duplicates(target))
    else:
        # Default test
        print("Usage: python3 skills/ops/process_guardian.py <script_name>")
        print("       python3 skills/ops/process_guardian.py patrol [--dry]")
        print("       python3 skills/ops/process_guardian.py reload [--dry]")
        print("Checking default: api/discord_bot.py")
        print(check_and_clean_duplicates("api/discord_bot.py"))
