#!/usr/bin/env python3
"""
process-hygiene — MAGI 程序衛生檢查與清理
==========================================
偵測殭屍程序、重複 daemon、孤兒子程序、port 佔用、長時間卡死，
並可自動修復。

Usage (CLI):
    python action.py --task scan       # 只掃描報告
    python action.py --task clean      # 掃描 + 自動修復
    python action.py --task zombies    # 只處理殭屍
    python action.py --task dedup      # 只處理重複 daemon
"""
import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [process-hygiene] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("process-hygiene")

# ---------------------------------------------------------------------------
# MAGI 關鍵程序定義
# ---------------------------------------------------------------------------
MAGI_ROOT = os.environ.get("MAGI_ROOT_DIR", os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

# 預期只應有 1 個實例的程序
SINGLETON_SCRIPTS = [
    "daemon.py",
    "api/server.py",
    "api/discord_bot.py",
    "skills/ops/file_review_auto_worker.py",
]

# MAGI 使用的 port
MAGI_PORTS = {
    5001: "Flask server",
    8080: "oMLX chat",
    8081: "oMLX embed",
}

# 子程序最大允許執行時間（秒）
STUCK_THRESHOLDS = {
    "action.py": 3600,       # skill action 最多 1 小時
    "autopilot": 7200,       # autopilot tick 最多 2 小時
    "worker": 3600,          # worker 最多 1 小時
}

DEFAULT_STUCK_SEC = 3600

# 這些服務預期可由 launchd 或 MAGI CLI 以 PPID=1 常駐執行；
# 不應被當作孤兒或卡死程序，否則週報會反覆誤報。
MANAGED_LONG_RUNNING_SCRIPTS = [
    "gui/magi_menubar.py",
    "scripts/ops/memory_watchdog.py",
    "scripts/share_gateway.py",
    "scripts/share_tunnel_supervisor.py",
    "scripts/serve_mlx_mtp.py",
]


# ---------------------------------------------------------------------------
# 工具函式
# ---------------------------------------------------------------------------
def _ps_all() -> List[Dict[str, Any]]:
    """取得所有程序列表（使用 ps，不依賴 psutil）。"""
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,stat=,etime=,command="],
            capture_output=True, text=True, timeout=10,
        )
        lines = (out.stdout or "").strip().splitlines()
    except Exception as e:
        logger.error("ps 執行失敗: %s", e)
        return []

    procs = []
    for line in lines:
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid_s, ppid_s, stat, etime, cmd = parts
        try:
            pid = int(pid_s)
            ppid = int(ppid_s)
        except ValueError:
            continue
        procs.append({
            "pid": pid,
            "ppid": ppid,
            "stat": stat,
            "etime": etime.strip(),
            "command": cmd.strip(),
        })
    return procs


def _etime_to_seconds(etime: str) -> int:
    """將 ps etime 格式 [[DD-]HH:]MM:SS 轉為秒數。"""
    try:
        days = 0
        if "-" in etime:
            d, etime = etime.split("-", 1)
            days = int(d)
        parts = etime.split(":")
        parts = [int(p) for p in parts]
        if len(parts) == 3:
            h, m, s = parts
        elif len(parts) == 2:
            h, m, s = 0, parts[0], parts[1]
        else:
            return 0
        return days * 86400 + h * 3600 + m * 60 + s
    except Exception:
        return 0


def _is_magi_process(cmd: str) -> bool:
    """判斷是否為 MAGI 相關程序。"""
    magi_markers = [
        "MAGI", "magi", "daemon.py", "server.py", "discord_bot.py",
        "action.py", "worker", "autopilot", "skills/", "api/",
        "casper_ecosystem",
    ]
    return any(m in cmd for m in magi_markers)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _is_managed_long_running(cmd: str) -> bool:
    return any(marker in (cmd or "") for marker in MANAGED_LONG_RUNNING_SCRIPTS)


def _safe_kill(pid: int, sig: int = signal.SIGTERM) -> bool:
    """安全發送訊號，返回是否成功。"""
    try:
        os.kill(pid, sig)
        return True
    except (ProcessLookupError, PermissionError) as e:
        logger.warning("  無法發送訊號到 PID %d: %s", pid, e)
        return False


# ---------------------------------------------------------------------------
# 檢查模組
# ---------------------------------------------------------------------------
def scan_zombies(procs: List[Dict]) -> List[Dict]:
    """偵測殭屍程序 (stat 含 Z)。"""
    zombies = []
    for p in procs:
        if "Z" in p["stat"]:
            # 找父程序資訊
            parent_cmd = ""
            for pp in procs:
                if pp["pid"] == p["ppid"]:
                    parent_cmd = pp["command"][:120]
                    break
            zombies.append({
                "pid": p["pid"],
                "ppid": p["ppid"],
                "etime": p["etime"],
                "parent_command": parent_cmd,
                "is_magi": _is_magi_process(parent_cmd),
            })
    return zombies


def clean_zombies(zombies: List[Dict]) -> List[Dict]:
    """
    嘗試清理殭屍程序。
    殭屍程序本身無法被 kill（已經死了），必須讓父程序 wait() 回收。
    策略：
    1. 對父程序發送 SIGCHLD（提示它回收子程序）
    2. 如果父程序是 MAGI 且已無用，可考慮終止父程序
    3. 如果父程序是 PID 1（init/launchd），無法處理，只能報告
    """
    results = []
    notified_parents = set()

    for z in zombies:
        ppid = z["ppid"]
        action = "reported"

        if ppid <= 1:
            # 父程序是 init/launchd，無法處理
            action = "orphan_zombie_ignored"
            logger.info("  殭屍 PID %d: 父程序為 launchd(1)，無法回收，等待系統自動清理", z["pid"])
        elif ppid not in notified_parents and _pid_alive(ppid):
            # 嘗試發送 SIGCHLD 讓父程序回收
            logger.info("  殭屍 PID %d: 向父程序 PID %d 發送 SIGCHLD", z["pid"], ppid)
            _safe_kill(ppid, signal.SIGCHLD)
            notified_parents.add(ppid)
            time.sleep(0.5)

            # 檢查是否已回收
            if not _pid_alive(z["pid"]):
                action = "reaped_by_sigchld"
                logger.info("    ✓ 已回收")
            else:
                action = "sigchld_sent_pending"
                logger.info("    ⏳ SIGCHLD 已發送，等待父程序回收")
        elif not _pid_alive(ppid):
            # 父程序已死，殭屍會被 launchd 接管並回收
            action = "parent_dead_will_auto_reap"
            logger.info("  殭屍 PID %d: 父程序 %d 已不存在，系統將自動回收", z["pid"], ppid)

        results.append({**z, "action": action})

    return results


def scan_duplicates(procs: List[Dict]) -> List[Dict]:
    """偵測 MAGI 關鍵程序的重複實例。"""
    issues = []
    for script in SINGLETON_SCRIPTS:
        matches = []
        for p in procs:
            if "Z" in p["stat"]:
                continue
            if script in p["command"] and "python" in p["command"].lower():
                matches.append(p)
        if len(matches) > 1:
            # 按啟動時間排序，最新的保留
            matches.sort(key=lambda x: _etime_to_seconds(x["etime"]))
            issues.append({
                "script": script,
                "count": len(matches),
                "pids": [m["pid"] for m in matches],
                "keep_pid": matches[0]["pid"],  # etime 最短 = 最新啟動
                "kill_pids": [m["pid"] for m in matches[1:]],
            })
    return issues


def clean_duplicates(duplicates: List[Dict]) -> List[Dict]:
    """清理重複的 MAGI 程序，保留最新的。"""
    results = []
    for dup in duplicates:
        killed = []
        for pid in dup["kill_pids"]:
            logger.info("  終止重複程序 %s PID %d (保留 PID %d)",
                        dup["script"], pid, dup["keep_pid"])
            if _safe_kill(pid, signal.SIGTERM):
                killed.append(pid)
                # 等待 graceful shutdown
                time.sleep(2)
                if _pid_alive(pid):
                    logger.warning("    SIGTERM 無效，強制 SIGKILL PID %d", pid)
                    _safe_kill(pid, signal.SIGKILL)
        results.append({
            "script": dup["script"],
            "kept": dup["keep_pid"],
            "killed": killed,
        })
    return results


def scan_orphans(procs: List[Dict]) -> List[Dict]:
    """偵測 MAGI 孤兒子程序（父程序已不存在或為 launchd）。"""
    orphans = []
    all_pids = {p["pid"] for p in procs}
    for p in procs:
        if "Z" in p["stat"]:
            continue
        if not _is_magi_process(p["command"]):
            continue
        if "python" not in p["command"].lower():
            continue
        if _is_managed_long_running(p["command"]):
            continue
        # 父程序不存在或為 launchd
        if p["ppid"] == 1 or p["ppid"] not in all_pids:
            # 排除 daemon 本身（它的父程序就是 launchd）
            if "daemon.py" in p["command"]:
                continue
            # 排除 jedi language server 等 IDE 程序
            if "jedi" in p["command"] or "language-server" in p["command"]:
                continue
            orphans.append({
                "pid": p["pid"],
                "ppid": p["ppid"],
                "etime": p["etime"],
                "elapsed_sec": _etime_to_seconds(p["etime"]),
                "command": p["command"][:150],
            })
    return orphans


def scan_stuck(procs: List[Dict]) -> List[Dict]:
    """偵測執行時間異常長的 MAGI 子程序。"""
    stuck = []
    for p in procs:
        if "Z" in p["stat"]:
            continue
        if not _is_magi_process(p["command"]):
            continue
        if "python" not in p["command"].lower():
            continue
        if _is_managed_long_running(p["command"]):
            continue
        # 排除常駐程序
        if any(s in p["command"] for s in ["daemon.py", "server.py", "discord_bot.py",
                                            "file_review_auto_worker.py", "tools_api.py",
                                            "openclaw_cron_runner.py", "heartbeat.py",
                                            "jedi", "language-server"]):
            continue

        elapsed = _etime_to_seconds(p["etime"])
        threshold = DEFAULT_STUCK_SEC
        for key, val in STUCK_THRESHOLDS.items():
            if key in p["command"]:
                threshold = val
                break

        if elapsed > threshold:
            stuck.append({
                "pid": p["pid"],
                "ppid": p["ppid"],
                "etime": p["etime"],
                "elapsed_sec": elapsed,
                "threshold_sec": threshold,
                "command": p["command"][:150],
            })
    return stuck


def scan_ports() -> List[Dict]:
    """檢查 MAGI port 是否被非預期程序佔用。"""
    issues = []
    for port, desc in MAGI_PORTS.items():
        try:
            # Only LISTEN sockets own a MAGI service port. Plain `lsof -ti :port`
            # also returns short-lived CLOSED client sockets and can falsely
            # fail commercial readiness while the managed service is healthy.
            out = subprocess.run(
                ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            pids = [int(p.strip()) for p in (out.stdout or "").strip().split("\n")
                    if p.strip().isdigit()]
        except Exception:
            continue

        if not pids:
            continue

        for pid in pids:
            # 查 command
            try:
                cmd_out = subprocess.run(
                    ["ps", "-o", "command=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=5,
                )
                cmd = (cmd_out.stdout or "").strip()
            except Exception:
                cmd = ""

            if not _is_magi_process(cmd) and "omlx" not in cmd.lower() and "openclaw" not in cmd.lower():
                issues.append({
                    "port": port,
                    "expected": desc,
                    "pid": pid,
                    "command": cmd[:150],
                })
    return issues


# ---------------------------------------------------------------------------
# 主指令
# ---------------------------------------------------------------------------
def cmd_scan() -> dict:
    """完整掃描，只報告不修復。"""
    procs = _ps_all()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    zombies = scan_zombies(procs)
    duplicates = scan_duplicates(procs)
    orphans = scan_orphans(procs)
    stuck = scan_stuck(procs)
    port_issues = scan_ports()

    total_issues = len(zombies) + len(duplicates) + len(orphans) + len(stuck) + len(port_issues)
    healthy = total_issues == 0

    report = {
        "success": True,
        "scan_time": now,
        "healthy": healthy,
        "total_issues": total_issues,
        "zombies": {"count": len(zombies), "items": zombies},
        "duplicates": {"count": len(duplicates), "items": duplicates},
        "orphans": {"count": len(orphans), "items": orphans},
        "stuck": {"count": len(stuck), "items": stuck},
        "port_conflicts": {"count": len(port_issues), "items": port_issues},
    }

    # 產生文字摘要
    lines = [f"🔍 程序衛生掃描 ({now})"]
    if healthy:
        lines.append("✅ 系統程序狀態正常，無問題。")
    else:
        if zombies:
            lines.append(f"💀 殭屍程序: {len(zombies)} 個")
            for z in zombies:
                lines.append(f"   PID {z['pid']} ← 父程序 PID {z['ppid']} ({z.get('parent_command', '')[:60]})")
        if duplicates:
            lines.append(f"👯 重複程序: {len(duplicates)} 組")
            for d in duplicates:
                lines.append(f"   {d['script']}: {d['count']} 個實例 (PIDs: {d['pids']})")
        if orphans:
            lines.append(f"👻 孤兒程序: {len(orphans)} 個")
            for o in orphans:
                lines.append(f"   PID {o['pid']} 執行 {o['etime']} ({o['command'][:60]})")
        if stuck:
            lines.append(f"🐌 卡死程序: {len(stuck)} 個")
            for s in stuck:
                lines.append(f"   PID {s['pid']} 執行 {s['etime']} > 閾值 {s['threshold_sec']}s ({s['command'][:60]})")
        if port_issues:
            lines.append(f"🔌 Port 衝突: {len(port_issues)} 個")
            for pi in port_issues:
                lines.append(f"   Port {pi['port']} ({pi['expected']}) 被 PID {pi['pid']} 佔用")

    report["message"] = "\n".join(lines)
    return report


def cmd_clean() -> dict:
    """掃描 + 自動修復所有問題。"""
    procs = _ps_all()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    actions = []

    # 1. 殭屍程序
    zombies = scan_zombies(procs)
    if zombies:
        logger.info("處理 %d 個殭屍程序...", len(zombies))
        z_results = clean_zombies(zombies)
        actions.append({"type": "zombies", "results": z_results})

    # 2. 重複程序
    duplicates = scan_duplicates(procs)
    if duplicates:
        logger.info("處理 %d 組重複程序...", len(duplicates))
        d_results = clean_duplicates(duplicates)
        actions.append({"type": "duplicates", "results": d_results})

    # 3. 卡死程序
    stuck = scan_stuck(procs)
    if stuck:
        logger.info("處理 %d 個卡死程序...", len(stuck))
        killed = []
        for s in stuck:
            logger.info("  終止卡死程序 PID %d (執行 %s)", s["pid"], s["etime"])
            if _safe_kill(s["pid"], signal.SIGTERM):
                killed.append(s["pid"])
                time.sleep(2)
                if _pid_alive(s["pid"]):
                    _safe_kill(s["pid"], signal.SIGKILL)
        actions.append({"type": "stuck", "killed": killed})

    # 4. 報告孤兒和 port 衝突（不自動清理，只報告）
    orphans = scan_orphans(procs)
    port_issues = scan_ports()

    total_fixed = sum(len(a.get("results", a.get("killed", []))) for a in actions)
    report = {
        "success": True,
        "scan_time": now,
        "actions_taken": actions,
        "fixed_count": total_fixed,
        "remaining_orphans": len(orphans),
        "remaining_port_conflicts": len(port_issues),
    }

    lines = [f"🧹 程序衛生清理 ({now})"]
    if not actions and not orphans and not port_issues:
        lines.append("✅ 系統程序狀態正常，無需清理。")
    else:
        for a in actions:
            if a["type"] == "zombies":
                reaped = sum(1 for r in a["results"] if r["action"] == "reaped_by_sigchld")
                lines.append(f"💀 殭屍: 處理 {len(a['results'])} 個（成功回收 {reaped} 個）")
            elif a["type"] == "duplicates":
                for r in a["results"]:
                    lines.append(f"👯 {r['script']}: 終止 {len(r['killed'])} 個重複（保留 PID {r['kept']}）")
            elif a["type"] == "stuck":
                lines.append(f"🐌 終止 {len(a['killed'])} 個卡死程序")
        if orphans:
            lines.append(f"👻 孤兒程序: {len(orphans)} 個（僅報告，未處理）")
        if port_issues:
            lines.append(f"🔌 Port 衝突: {len(port_issues)} 個（僅報告，未處理）")

    report["message"] = "\n".join(lines)
    return report


def cmd_zombies() -> dict:
    """只處理殭屍程序。"""
    procs = _ps_all()
    zombies = scan_zombies(procs)
    if not zombies:
        return {"success": True, "message": "✅ 無殭屍程序。", "count": 0}
    results = clean_zombies(zombies)
    reaped = sum(1 for r in results if r["action"] == "reaped_by_sigchld")
    return {
        "success": True,
        "count": len(zombies),
        "reaped": reaped,
        "results": results,
        "message": f"💀 發現 {len(zombies)} 個殭屍程序，成功回收 {reaped} 個。",
    }


def cmd_dedup() -> dict:
    """只處理重複 daemon/server。"""
    procs = _ps_all()
    duplicates = scan_duplicates(procs)
    if not duplicates:
        return {"success": True, "message": "✅ 無重複程序。", "count": 0}
    results = clean_duplicates(duplicates)
    lines = ["👯 重複程序清理:"]
    for r in results:
        lines.append(f"  {r['script']}: 終止 {len(r['killed'])} 個（保留 PID {r['kept']}）")
    return {
        "success": True,
        "count": len(duplicates),
        "results": results,
        "message": "\n".join(lines),
    }


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------
COMMANDS = {
    "scan": cmd_scan,
    "clean": cmd_clean,
    "zombies": cmd_zombies,
    "dedup": cmd_dedup,
}


def main():
    parser = argparse.ArgumentParser(description="MAGI Process Hygiene")
    parser.add_argument("--task", required=True, help="Task: scan | clean | zombies | dedup")
    args = parser.parse_args()

    task_str = (args.task or "").strip()
    # 支援 "task_name {json_params}" 格式
    cmd_name = task_str.split()[0] if task_str else ""

    if cmd_name == "help":
        print(json.dumps({
            "success": True,
            "commands": list(COMMANDS.keys()),
            "message": "可用指令: scan（掃描）、clean（清理）、zombies（殭屍）、dedup（去重）",
        }, ensure_ascii=False, indent=2))
        return

    handler = COMMANDS.get(cmd_name)
    if not handler:
        print(json.dumps({
            "success": False,
            "error": f"unknown command: {cmd_name}",
            "available": list(COMMANDS.keys()),
        }, ensure_ascii=False, indent=2))
        sys.exit(1)

    try:
        result = handler()
    except Exception as e:
        result = {"success": False, "error": f"{type(e).__name__}: {e}"}

    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
