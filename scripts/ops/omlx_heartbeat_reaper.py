#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Layer 1 — heartbeat 主動 kill 多餘 MLX instance。

由 omlx_switch_model.sh 在切換完成後呼叫；以 --model-dir 指紋比對找出
重複 process（同一個 model-dir 出現 >1 個 PID），保留最舊的 PID（launchd
真正啟動的那個），將較新的標為重複。

Mode:
  - shadow（預設）：只寫 /tmp/omlx_heartbeat_kill_decisions.jsonl，不殺
  - enforce：SIGTERM → 3s grace → SIGKILL

紅線：
  - 只處理 'omlx serve' 進程（pgrep -f 過濾）
  - 絕不 kill 最舊 PID
  - 絕不 kill 非 omlx serve 的 process
  - 絕不 kill self / parent
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

DECISION_PATH = Path(os.environ.get(
    "OMLX_HEARTBEAT_DECISION_LOG",
    "/tmp/omlx_heartbeat_kill_decisions.jsonl",
))
SIGTERM_GRACE_SEC = float(os.environ.get("OMLX_HEARTBEAT_GRACE_SEC", "3"))


@dataclass
class OmlxProc:
    pid: int
    start_epoch: float
    cmdline: str
    model_dir: Optional[str] = None
    port: Optional[int] = None


_MODEL_DIR_RE = re.compile(r"--model-dir\s+(\S+)")
_PORT_RE = re.compile(r"--port\s+(\d+)")


def _list_omlx_serves() -> List[OmlxProc]:
    """回傳所有 `omlx serve` process；用 ps 讀 pid、start epoch、cmdline。"""
    try:
        # -o 保留欄位寬度；lstart= 沒有欄位標題但 bsd ps 的 lstart 是固定 24 chars
        # 改用 etime 較穩：start = now - etime
        res = subprocess.run(
            ["ps", "-eo", "pid=,etime=,command="],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    now = time.time()
    out: List[OmlxProc] = []
    for line in res.stdout.splitlines():
        line = line.rstrip()
        if not line or "omlx serve" not in line:
            continue
        # 跳過 grep 自己（雖然 pgrep 不會，但 ps 可能抓到其他文字）
        if " grep " in line or line.endswith("grep"):
            continue
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        etime_str = parts[1]
        cmd = parts[2]
        # 再次 sanity check: cmdline 真的含 'omlx serve'（排除 scripts/ops 自身）
        if "omlx serve" not in cmd:
            continue
        # etime: [[dd-]hh:]mm:ss
        elapsed = _parse_etime(etime_str)
        start_epoch = now - elapsed if elapsed is not None else now
        mdir = None
        port: Optional[int] = None
        m = _MODEL_DIR_RE.search(cmd)
        if m:
            mdir = m.group(1)
        pm = _PORT_RE.search(cmd)
        if pm:
            try:
                port = int(pm.group(1))
            except ValueError:
                port = None
        out.append(OmlxProc(pid=pid, start_epoch=start_epoch, cmdline=cmd, model_dir=mdir, port=port))
    return out


def _parse_etime(s: str) -> Optional[float]:
    """Parse ps etime: [[dd-]hh:]mm:ss → seconds."""
    try:
        days = 0
        if "-" in s:
            d, s = s.split("-", 1)
            days = int(d)
        parts = s.split(":")
        if len(parts) == 2:
            mm, ss = parts
            return days * 86400 + int(mm) * 60 + int(ss)
        if len(parts) == 3:
            hh, mm, ss = parts
            return days * 86400 + int(hh) * 3600 + int(mm) * 60 + int(ss)
    except (ValueError, TypeError):
        return None
    return None


def find_duplicates(procs: List[OmlxProc]) -> List[OmlxProc]:
    """
    回傳「可殺」的 process list：同一 model-dir 內，保留最舊 PID（launchd 啟的），
    其餘視為重複。no model-dir 的不處理（避免誤殺）。
    """
    groups: Dict[str, List[OmlxProc]] = {}
    for p in procs:
        if not p.model_dir:
            continue
        groups.setdefault(p.model_dir, []).append(p)
    duplicates: List[OmlxProc] = []
    for mdir, lst in groups.items():
        if len(lst) <= 1:
            continue
        # 最舊（start_epoch 最小）優先；若 start_epoch 相同則取 pid 較小
        lst.sort(key=lambda x: (x.start_epoch, x.pid))
        oldest = lst[0]
        for p in lst[1:]:
            if p.pid == oldest.pid:
                continue
            # 防呆：不殺自己或父
            if p.pid in (os.getpid(), os.getppid()):
                continue
            duplicates.append(p)
    return duplicates


def _write_decision(record: Dict) -> None:
    record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        DECISION_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DECISION_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as e:
        print(f"[heartbeat-reaper] decision log write failed: {e}", file=sys.stderr)


def _kill_one(proc: OmlxProc, grace_sec: float = SIGTERM_GRACE_SEC) -> Dict:
    """SIGTERM → grace → SIGKILL；回傳 outcome dict。"""
    outcome = {
        "pid": proc.pid,
        "model_dir": proc.model_dir,
        "started_at_epoch": proc.start_epoch,
        "sigterm_sent": False,
        "sigkill_sent": False,
        "died": False,
        "error": None,
    }
    try:
        os.kill(proc.pid, signal.SIGTERM)
        outcome["sigterm_sent"] = True
    except ProcessLookupError:
        outcome["died"] = True
        return outcome
    except PermissionError as e:
        outcome["error"] = f"permission denied: {e}"
        return outcome
    except Exception as e:
        outcome["error"] = str(e)
        return outcome
    # wait
    deadline = time.time() + grace_sec
    while time.time() < deadline:
        try:
            os.kill(proc.pid, 0)
        except ProcessLookupError:
            outcome["died"] = True
            return outcome
        time.sleep(0.2)
    # still alive → SIGKILL
    try:
        os.kill(proc.pid, signal.SIGKILL)
        outcome["sigkill_sent"] = True
    except ProcessLookupError:
        outcome["died"] = True
        return outcome
    except Exception as e:
        outcome["error"] = str(e)
        return outcome
    # re-check
    time.sleep(0.5)
    try:
        os.kill(proc.pid, 0)
    except ProcessLookupError:
        outcome["died"] = True
    return outcome


def run(mode: str, expected_ports: int, mode_name: str) -> int:
    procs = _list_omlx_serves()
    actual = len(procs)
    upper = expected_ports * 2 + 1
    duplicates = find_duplicates(procs) if actual > upper else []
    decision = {
        "mode": mode,
        "mode_name": mode_name,
        "expected_ports": expected_ports,
        "upper_limit": upper,
        "actual_count": actual,
        "duplicates": [
            {
                "pid": d.pid,
                "model_dir": d.model_dir,
                "port": d.port,
                "start_epoch": d.start_epoch,
            }
            for d in duplicates
        ],
    }
    if not duplicates:
        decision["action"] = "no_op"
        _write_decision(decision)
        print(f"[heartbeat-reaper] {mode_name}: count={actual} upper={upper} — no duplicates")
        return 0
    if mode == "shadow":
        decision["action"] = "would_kill"
        _write_decision(decision)
        print(
            f"[heartbeat-reaper] {mode_name} SHADOW: would kill "
            f"{[d.pid for d in duplicates]} (model-dir duplicates)"
        )
        return 0
    # enforce
    outcomes = [_kill_one(d) for d in duplicates]
    decision["action"] = "killed"
    decision["outcomes"] = outcomes
    _write_decision(decision)
    print(
        f"[heartbeat-reaper] {mode_name} ENFORCE: killed "
        f"{[o['pid'] for o in outcomes if o['died']]} / "
        f"{len(outcomes)} targets"
    )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-ports", type=int, required=True,
                        help="1=NIGHT, 3=DAY")
    parser.add_argument("--mode-name", default="UNKNOWN",
                        help="NIGHT / DAY; for logging only")
    args = parser.parse_args(argv)
    mode = os.environ.get("OMLX_HEARTBEAT_KILL_MODE", "shadow").strip().lower()
    if mode not in {"shadow", "enforce"}:
        print(f"[heartbeat-reaper] invalid OMLX_HEARTBEAT_KILL_MODE={mode!r}, defaulting to shadow", file=sys.stderr)
        mode = "shadow"
    return run(mode, args.expected_ports, args.mode_name)


if __name__ == "__main__":
    sys.exit(main())
