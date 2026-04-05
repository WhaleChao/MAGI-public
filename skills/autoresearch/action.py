#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
autoresearch — MAGI 自主 ML 研究技能
====================================

基於 Karpathy 的 autoresearch 框架，讓 MAGI 自主進行模型訓練實驗。

支援模式：
  1. remote — SSH 到 GPU 主機執行（推薦）
  2. local  — 本機有 NVIDIA GPU 時直接執行

用法：
  python action.py setup <host>            # 在 GPU 主機準備環境
  python action.py run <host> [--tag TAG]  # 啟動實驗循環
  python action.py status [host]           # 查看進度
  python action.py results [host]          # 取得 results.tsv
  python action.py stop <host>             # 停止實驗
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

_SKILL_DIR = Path(__file__).resolve().parent
_MAGI_ROOT = _SKILL_DIR.parents[1]
_RESULTS_DIR = _SKILL_DIR / "runs"
_RESULTS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def _ssh(host: str, cmd: str, *, timeout: int = 600, check: bool = True) -> subprocess.CompletedProcess:
    """Run command on remote host via SSH."""
    return subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=no", host, cmd],
        capture_output=True, text=True, timeout=timeout, check=check,
    )


def _scp_to(host: str, local: str, remote: str, *, timeout: int = 120) -> None:
    subprocess.run(
        ["scp", "-o", "ConnectTimeout=10", local, f"{host}:{remote}"],
        capture_output=True, text=True, timeout=timeout, check=True,
    )


def _scp_from(host: str, remote: str, local: str, *, timeout: int = 120) -> None:
    subprocess.run(
        ["scp", "-o", "ConnectTimeout=10", f"{host}:{remote}", local],
        capture_output=True, text=True, timeout=timeout, check=True,
    )


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

REMOTE_DIR = "~/autoresearch"


def cmd_setup(host: str) -> dict:
    """Setup autoresearch environment on remote GPU host."""
    steps = []

    # 1. Check connectivity
    try:
        r = _ssh(host, "echo ok && nvidia-smi --query-gpu=name --format=csv,noheader | head -1", timeout=30)
        gpu_name = r.stdout.strip().split("\n")[-1]
        steps.append(f"GPU detected: {gpu_name}")
    except Exception as e:
        return {"success": False, "error": f"SSH connection or GPU check failed: {e}"}

    # 2. Clone or update repo
    try:
        _ssh(host, f"""
            if [ -d {REMOTE_DIR}/.git ]; then
                cd {REMOTE_DIR} && git pull --ff-only 2>/dev/null || true
            else
                git clone https://github.com/karpathy/autoresearch.git {REMOTE_DIR}
            fi
        """, timeout=120)
        steps.append("Repository cloned/updated")
    except Exception as e:
        return {"success": False, "error": f"Git clone failed: {e}", "steps": steps}

    # 3. Install uv if needed
    try:
        _ssh(host, "command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh", timeout=120)
        steps.append("uv package manager ready")
    except Exception as e:
        steps.append(f"uv install warning: {e}")

    # 4. Prepare data (one-time)
    try:
        r = _ssh(host, f"ls ~/.cache/autoresearch/tokenizer/ 2>/dev/null | head -1", timeout=15)
        if r.stdout.strip():
            steps.append("Data already prepared (cache exists)")
        else:
            steps.append("Data not prepared yet — run: ssh {host} 'cd ~/autoresearch && uv run prepare.py'")
    except Exception:
        steps.append("Could not check data cache")

    return {"success": True, "host": host, "gpu": gpu_name, "steps": steps}


def cmd_run(host: str, tag: str | None = None) -> dict:
    """Start autonomous experiment loop on remote host."""
    if not tag:
        tag = datetime.now().strftime("%b%d").lower()

    # Create run record locally
    run_id = f"{tag}_{int(time.time())}"
    run_meta = {
        "run_id": run_id,
        "host": host,
        "tag": tag,
        "started_at": datetime.now().isoformat(),
        "status": "running",
    }
    meta_path = _RESULTS_DIR / f"{run_id}.json"
    meta_path.write_text(json.dumps(run_meta, indent=2, ensure_ascii=False), encoding="utf-8")

    # Create branch + results.tsv + start experiment on remote
    startup_script = f"""
cd {REMOTE_DIR} || exit 1

# Create fresh branch
git checkout main 2>/dev/null || git checkout master 2>/dev/null
git pull --ff-only 2>/dev/null || true
git checkout -b autoresearch/{tag} 2>/dev/null || git checkout autoresearch/{tag}

# Initialize results.tsv
if [ ! -f results.tsv ]; then
    printf 'commit\\tval_bpb\\tmemory_gb\\tstatus\\tdescription\\n' > results.tsv
fi

# Run baseline first
echo "Running baseline..."
uv run train.py > run.log 2>&1
BASELINE_BPB=$(grep "^val_bpb:" run.log | awk '{{print $2}}')
BASELINE_MEM=$(grep "^peak_vram_mb:" run.log | awk '{{print $2}}')
BASELINE_MEM_GB=$(python3 -c "print(f'{{float(${{BASELINE_MEM:-0}})/1024:.1f}}')" 2>/dev/null || echo "0.0")
COMMIT=$(git rev-parse --short HEAD)
printf '%s\\t%s\\t%s\\tkeep\\tbaseline\\n' "$COMMIT" "$BASELINE_BPB" "$BASELINE_MEM_GB" >> results.tsv
echo "Baseline: val_bpb=$BASELINE_BPB mem=$BASELINE_MEM_GB GB"
"""

    try:
        # Start in background via nohup
        _ssh(host,
             f"nohup bash -c '{startup_script}' > {REMOTE_DIR}/autoresearch_daemon.log 2>&1 &",
             timeout=30, check=False)

        run_meta["status"] = "started"
        meta_path.write_text(json.dumps(run_meta, indent=2, ensure_ascii=False), encoding="utf-8")

        return {
            "success": True,
            "run_id": run_id,
            "host": host,
            "tag": tag,
            "message": f"Experiment loop started on {host} (branch: autoresearch/{tag}). "
                       f"Baseline training in progress (~5 min).",
        }
    except Exception as e:
        run_meta["status"] = "failed"
        run_meta["error"] = str(e)
        meta_path.write_text(json.dumps(run_meta, indent=2, ensure_ascii=False), encoding="utf-8")
        return {"success": False, "error": str(e)}


def cmd_status(host: str | None = None) -> dict:
    """Check experiment status."""
    # List local run records
    runs = []
    for p in sorted(_RESULTS_DIR.glob("*.json"), reverse=True):
        try:
            runs.append(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            continue

    if not runs:
        return {"success": True, "message": "No experiment runs recorded.", "runs": []}

    # If host specified, check remote
    if host:
        try:
            r = _ssh(host, f"cat {REMOTE_DIR}/results.tsv 2>/dev/null | wc -l", timeout=15)
            experiment_count = max(0, int(r.stdout.strip() or "0") - 1)  # minus header

            r2 = _ssh(host, f"tail -1 {REMOTE_DIR}/results.tsv 2>/dev/null", timeout=15)
            last_line = r2.stdout.strip()

            r3 = _ssh(host, f"cat {REMOTE_DIR}/autoresearch_daemon.log 2>/dev/null | tail -5", timeout=15)
            recent_log = r3.stdout.strip()

            return {
                "success": True,
                "host": host,
                "experiments_completed": experiment_count,
                "last_result": last_line,
                "recent_log": recent_log,
                "local_runs": runs[:5],
            }
        except Exception as e:
            return {"success": False, "error": f"Remote check failed: {e}", "local_runs": runs[:5]}

    return {"success": True, "runs": runs[:10]}


def cmd_results(host: str) -> dict:
    """Fetch results.tsv from remote host."""
    local_path = str(_RESULTS_DIR / f"results_{host.replace('@', '_').replace('.', '_')}_{int(time.time())}.tsv")
    try:
        _scp_from(host, f"{REMOTE_DIR}/results.tsv", local_path, timeout=30)
        content = Path(local_path).read_text(encoding="utf-8")
        lines = content.strip().split("\n")
        experiments = len(lines) - 1  # minus header

        # Parse best result
        best_bpb = float("inf")
        best_desc = ""
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) >= 4 and parts[3] == "keep":
                try:
                    bpb = float(parts[1])
                    if bpb < best_bpb:
                        best_bpb = bpb
                        best_desc = parts[4] if len(parts) > 4 else ""
                except ValueError:
                    continue

        return {
            "success": True,
            "host": host,
            "total_experiments": experiments,
            "best_val_bpb": best_bpb if best_bpb < float("inf") else None,
            "best_description": best_desc,
            "results_file": local_path,
            "raw": content,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def cmd_stop(host: str) -> dict:
    """Stop running experiments on remote host."""
    try:
        # Kill any running train.py
        _ssh(host, f"pkill -f 'uv run train.py' 2>/dev/null; pkill -f 'autoresearch_daemon' 2>/dev/null",
             timeout=15, check=False)
        return {"success": True, "message": f"Experiment stopped on {host}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# MAGI skill entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="autoresearch — MAGI autonomous ML research")
    subparsers = parser.add_subparsers(dest="command")

    p_setup = subparsers.add_parser("setup", help="Setup GPU host")
    p_setup.add_argument("host", help="SSH host (e.g. user@gpu-server)")

    p_run = subparsers.add_parser("run", help="Start experiment loop")
    p_run.add_argument("host", help="SSH host")
    p_run.add_argument("--tag", help="Run tag (default: date-based)")

    p_status = subparsers.add_parser("status", help="Check status")
    p_status.add_argument("host", nargs="?", help="SSH host (optional)")

    p_results = subparsers.add_parser("results", help="Fetch results")
    p_results.add_argument("host", help="SSH host")

    p_stop = subparsers.add_parser("stop", help="Stop experiments")
    p_stop.add_argument("host", help="SSH host")

    # MAGI JSON-cmd mode
    if "--json-cmd" in sys.argv:
        raw = sys.stdin.read().strip()
        if raw:
            data = json.loads(raw)
            task = str(data.get("task", "")).strip()
        else:
            task = ""
        parts = task.split()
        cmd = parts[0] if parts else "status"
        host = parts[1] if len(parts) > 1 else None

        if cmd == "setup" and host:
            result = cmd_setup(host)
        elif cmd == "run" and host:
            tag = parts[2] if len(parts) > 2 else None
            result = cmd_run(host, tag)
        elif cmd == "status":
            result = cmd_status(host)
        elif cmd == "results" and host:
            result = cmd_results(host)
        elif cmd == "stop" and host:
            result = cmd_stop(host)
        else:
            result = {"success": False, "error": f"Unknown command: {task}",
                      "usage": "setup|run|status|results|stop <host> [--tag TAG]"}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    if args.command == "setup":
        result = cmd_setup(args.host)
    elif args.command == "run":
        result = cmd_run(args.host, args.tag)
    elif args.command == "status":
        result = cmd_status(getattr(args, "host", None))
    elif args.command == "results":
        result = cmd_results(args.host)
    elif args.command == "stop":
        result = cmd_stop(args.host)
    else:
        parser.print_help()
        return

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
