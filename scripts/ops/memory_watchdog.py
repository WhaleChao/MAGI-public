#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Layer 2 — 常駐 memory watchdog。

每 30 秒檢查系統記憶體壓力；連續 3 次（共 ~90s）偵測到：
  - swap_used > 8GB    或
  - free + inactive < 2GB
就觸發 action：找出 RSS 最高的 MAGI subprocess（不含 daemon.py 本身、
cloudflared、launchd、系統進程）；

Mode:
  - shadow（預設 MAGI_WATCHDOG_KILL_MODE=shadow）：寫
    .runtime/metrics/memory_watchdog_decisions.jsonl，不真殺
  - enforce：SIGTERM 該 PID，讓 daemon 自動補回（只殺 daemon supervision
    下可恢復的 subprocess；不殺 daemon 本身）

設計紅線：
  - 不殺 daemon.py（MAGI 主管家）
  - 不殺 launchd / cloudflared / omlx serve / Chrome
  - 只殺可自動重啟的 MAGI subprocess（api/server.py, api/discord_bot.py,
    api/tools_api.py, rpc_worker.py 等由 daemon 監看的）
  - 連續 N 次才觸發（避免瞬時波動誤判）
  - 額外回收 MAGI 啟動後逾時未關的 Playwright driver / headless browser；
    這類進程常是 portal 自動化 teardown hang，不受記憶體壓力門檻限制。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT", "/Users/ai/Desktop/MAGI_v2")).resolve()
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.platforms import runtime_dir  # noqa: E402

# ---- 設定 --------------------------------------------------------------

CHECK_INTERVAL_SEC = int(os.environ.get("MAGI_WATCHDOG_INTERVAL_SEC", "30"))
TRIGGER_CONSECUTIVE = int(os.environ.get("MAGI_WATCHDOG_TRIGGER_CONSECUTIVE", "3"))
SWAP_THRESHOLD_GB = float(os.environ.get("MAGI_WATCHDOG_SWAP_GB", "8"))
FREE_INACTIVE_MIN_GB = float(os.environ.get("MAGI_WATCHDOG_FREE_MIN_GB", "2"))
ACTION_COOLDOWN_SEC = int(os.environ.get("MAGI_WATCHDOG_COOLDOWN_SEC", "600"))  # 10 min between kills
STALE_PLAYWRIGHT_ENABLED = os.environ.get(
    "MAGI_WATCHDOG_REAP_STALE_PLAYWRIGHT", "1"
).strip().lower() not in {"0", "false", "no", "off"}
STALE_PLAYWRIGHT_MAX_AGE_SEC = int(os.environ.get(
    "MAGI_WATCHDOG_STALE_PLAYWRIGHT_SEC", str(45 * 60),
))
STALE_PLAYWRIGHT_COOLDOWN_SEC = int(os.environ.get(
    "MAGI_WATCHDOG_STALE_PLAYWRIGHT_COOLDOWN_SEC", "300",
))
STALE_PLAYWRIGHT_MODE = os.environ.get(
    "MAGI_WATCHDOG_STALE_PLAYWRIGHT_MODE", "enforce",
).strip().lower()
if STALE_PLAYWRIGHT_MODE not in {"shadow", "enforce"}:
    STALE_PLAYWRIGHT_MODE = "shadow"

# 絕不殺清單（basename 或 substring 比對）
_NEVER_KILL = frozenset({
    "daemon.py",           # MAGI 主 daemon（殺掉整個系統死）
    "cloudflared",         # Tailscale / webhook tunnel
    "launchd",
    "kernel_task",
    "WindowServer",
    "loginwindow",
    "Finder",
    "SystemUIServer",
    "mdworker",
    "mds_stores",
    "memory_watchdog.py",  # self
    "omlx_heartbeat_reaper.py",
    "omlx",                # omlx serve 歸 launchd 管，watchdog 不碰
})

# 可殺的 MAGI subprocess markers（daemon 會自動補回）
_RECOVERABLE_MARKERS = (
    "api/server.py",
    "api/discord_bot.py",
    "api/tools_api.py",
    "api/rpc_worker.py",
    "rpc_worker.py",
    "admin_server.py",
    "magi_menubar.py",
)

DECISION_LOG = runtime_dir.metrics("memory_watchdog_decisions")


# ---- 記憶體感測 ---------------------------------------------------------

@dataclass
class MemoryReading:
    swap_used_gb: float
    free_gb: float
    inactive_gb: float
    page_size: int = 16384

    @property
    def free_plus_inactive_gb(self) -> float:
        return self.free_gb + self.inactive_gb


def read_memory() -> MemoryReading:
    """用 vm_stat + sysctl 讀系統記憶體；free/inactive 單位 GB，swap 用 sysctl。"""
    vm_stat = subprocess.run(
        ["vm_stat"], capture_output=True, text=True, timeout=10, check=False,
    )
    page_size = 16384
    free_pages = 0
    inactive_pages = 0
    for line in vm_stat.stdout.splitlines():
        if "page size of" in line:
            # "Mach Virtual Memory Statistics: (page size of 16384 bytes)"
            try:
                page_size = int(line.split("page size of")[1].split("bytes")[0].strip())
            except (IndexError, ValueError):
                pass
        elif line.startswith("Pages free:"):
            free_pages = int(line.split(":")[1].strip().rstrip("."))
        elif line.startswith("Pages inactive:"):
            inactive_pages = int(line.split(":")[1].strip().rstrip("."))
    free_gb = free_pages * page_size / 1024 / 1024 / 1024
    inactive_gb = inactive_pages * page_size / 1024 / 1024 / 1024

    # swap (use absolute paths so launchd PATH limitations don't break us)
    _sysctl_bin = "/usr/sbin/sysctl"
    if not os.path.exists(_sysctl_bin):
        _sysctl_bin = "sysctl"  # fallback to PATH lookup
    swap = subprocess.run(
        [_sysctl_bin, "vm.swapusage"],
        capture_output=True, text=True, timeout=10, check=False,
    )
    swap_used_gb = 0.0
    # "vm.swapusage: total = 12288.00M  used = 5432.16M  free = ..."
    for token in swap.stdout.replace("=", " ").split():
        if token.endswith("M") and "swap_used_marker" not in token:
            pass
    # 更穩定的解析：split by "used"
    if "used" in swap.stdout:
        try:
            after = swap.stdout.split("used", 1)[1]
            # 下一個 M 之前的數字
            num = ""
            for ch in after:
                if ch.isdigit() or ch == ".":
                    num += ch
                elif num:
                    break
            swap_used_gb = float(num) / 1024  # MB → GB
        except (ValueError, IndexError):
            pass
    return MemoryReading(
        swap_used_gb=swap_used_gb,
        free_gb=free_gb,
        inactive_gb=inactive_gb,
        page_size=page_size,
    )


def is_memory_pressure(r: MemoryReading) -> bool:
    return r.swap_used_gb > SWAP_THRESHOLD_GB or r.free_plus_inactive_gb < FREE_INACTIVE_MIN_GB


# ---- 找 MAGI subprocess -------------------------------------------------

@dataclass
class Proc:
    pid: int
    rss_bytes: int
    cmdline: str


@dataclass
class ProcessRow:
    pid: int
    ppid: int
    elapsed_sec: int
    cmdline: str


def list_magi_procs() -> List[Proc]:
    """ps -eo pid,rss,command；過濾非 MAGI 可回收目標。"""
    res = subprocess.run(
        ["ps", "-eo", "pid=,rss=,command="],
        capture_output=True, text=True, timeout=10, check=False,
    )
    out: List[Proc] = []
    for line in res.stdout.splitlines():
        line = line.rstrip()
        if not line:
            continue
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            rss_kb = int(parts[1])
        except ValueError:
            continue
        cmd = parts[2]
        # 不在名單內
        if any(nk in cmd for nk in _NEVER_KILL):
            continue
        # 必須是可回收的 MAGI subprocess
        if not any(m in cmd for m in _RECOVERABLE_MARKERS):
            continue
        out.append(Proc(pid=pid, rss_bytes=rss_kb * 1024, cmdline=cmd))
    # RSS 由大到小
    out.sort(key=lambda p: p.rss_bytes, reverse=True)
    return out


def _parse_etime(raw: str) -> int:
    """Parse ps etime ([[DD-]HH:]MM:SS) into seconds."""
    text = (raw or "").strip()
    if not text:
        return 0
    days = 0
    if "-" in text:
        day_s, text = text.split("-", 1)
        try:
            days = int(day_s)
        except ValueError:
            days = 0
    parts = text.split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(nums) == 3:
        hours, minutes, seconds = nums
    elif len(nums) == 2:
        hours = 0
        minutes, seconds = nums
    else:
        return 0
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def list_process_rows() -> List[ProcessRow]:
    res = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,etime=,command="],
        capture_output=True, text=True, timeout=10, check=False,
    )
    rows: List[ProcessRow] = []
    for line in res.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            rows.append(ProcessRow(
                pid=int(parts[0]),
                ppid=int(parts[1]),
                elapsed_sec=_parse_etime(parts[2]),
                cmdline=parts[3],
            ))
        except ValueError:
            continue
    return rows


def _is_stale_magi_playwright(row: ProcessRow, by_pid: Dict[int, ProcessRow]) -> bool:
    if "playwright/driver/node" not in row.cmdline or "run-driver" not in row.cmdline:
        return False
    if row.elapsed_sec < STALE_PLAYWRIGHT_MAX_AGE_SEC:
        return False
    parent = by_pid.get(row.ppid)
    parent_cmd = parent.cmdline if parent else ""
    if str(MAGI_ROOT) not in row.cmdline and str(MAGI_ROOT) not in parent_cmd:
        return False
    return any(marker in parent_cmd for marker in ("api/server.py", "daemon.py", "laf_orchestrator.py"))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def reap_stale_playwright(state: "WatchdogState") -> Optional[Dict]:
    """Reap MAGI-owned Playwright drivers that outlive the automation request."""
    if not STALE_PLAYWRIGHT_ENABLED:
        return None
    if time.time() - state.last_stale_playwright_at < STALE_PLAYWRIGHT_COOLDOWN_SEC:
        return None
    rows = list_process_rows()
    by_pid = {p.pid: p for p in rows}
    stale = [p for p in rows if _is_stale_magi_playwright(p, by_pid)]
    if not stale:
        return None
    stale.sort(key=lambda p: p.elapsed_sec, reverse=True)
    target = stale[0]
    parent = by_pid.get(target.ppid)
    record: Dict = {
        "mode": STALE_PLAYWRIGHT_MODE,
        "action": "stale_playwright_would_reap" if STALE_PLAYWRIGHT_MODE == "shadow" else "stale_playwright_reaped",
        "target_pid": target.pid,
        "target_ppid": target.ppid,
        "target_elapsed_sec": target.elapsed_sec,
        "target_cmd": target.cmdline,
        "parent_cmd": parent.cmdline if parent else "",
    }
    if STALE_PLAYWRIGHT_MODE == "enforce":
        try:
            os.kill(target.pid, signal.SIGTERM)
            time.sleep(2)
            if _pid_alive(target.pid):
                os.kill(target.pid, signal.SIGKILL)
                record["sigkill_sent"] = True
            else:
                record["sigterm_sent"] = True
        except ProcessLookupError:
            record["action"] = "stale_playwright_target_gone"
        except PermissionError as e:
            record["action"] = "stale_playwright_permission_denied"
            record["error"] = str(e)
        except Exception as e:
            record["action"] = "stale_playwright_error"
            record["error"] = str(e)
    _write_decision(record)
    state.last_stale_playwright_at = time.time()
    print(
        f"[memory-watchdog] {record['action']}: pid={target.pid} "
        f"age={target.elapsed_sec}s cmd={target.cmdline[:80]}"
    )
    return record


# ---- 決策與執行 ---------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _write_decision(record: Dict) -> None:
    record["ts"] = _now_iso()
    try:
        runtime_dir.atomic_append_jsonl(DECISION_LOG, record, rotate_at=500, keep_tail=300)
    except Exception as e:
        print(f"[memory-watchdog] decision log write failed: {e}", file=sys.stderr)


def _kill_mode() -> str:
    m = os.environ.get("MAGI_WATCHDOG_KILL_MODE", "shadow").strip().lower()
    return m if m in {"shadow", "enforce"} else "shadow"


class WatchdogState:
    def __init__(self) -> None:
        self.consecutive_pressure = 0
        self.last_action_at: float = 0.0
        self.last_stale_playwright_at: float = 0.0

    def record_reading(self, r: MemoryReading) -> Optional[Proc]:
        """回傳 None = 無動作；回傳 Proc = 應採取 action（shadow or enforce）。"""
        if is_memory_pressure(r):
            self.consecutive_pressure += 1
        else:
            self.consecutive_pressure = 0
            return None
        if self.consecutive_pressure < TRIGGER_CONSECUTIVE:
            return None
        # cooldown
        if time.time() - self.last_action_at < ACTION_COOLDOWN_SEC:
            return None
        procs = list_magi_procs()
        if not procs:
            return None
        return procs[0]


def _do_action(proc: Proc, r: MemoryReading, mode: str) -> Dict:
    record = {
        "swap_used_gb": r.swap_used_gb,
        "free_gb": r.free_gb,
        "inactive_gb": r.inactive_gb,
        "free_plus_inactive_gb": r.free_plus_inactive_gb,
        "target_pid": proc.pid,
        "target_cmd": proc.cmdline,
        "target_rss_gb": proc.rss_bytes / 1024 / 1024 / 1024,
        "mode": mode,
    }
    if mode == "shadow":
        record["action"] = "would_kill"
        print(
            f"[memory-watchdog] SHADOW: would kill pid={proc.pid} "
            f"rss={record['target_rss_gb']:.2f}GB cmd={proc.cmdline[:80]}"
        )
    else:
        try:
            os.kill(proc.pid, signal.SIGTERM)
            record["action"] = "killed"
            record["sigterm_sent"] = True
            print(
                f"[memory-watchdog] ENFORCE: SIGTERM pid={proc.pid} "
                f"rss={record['target_rss_gb']:.2f}GB"
            )
        except ProcessLookupError:
            record["action"] = "target_gone"
        except PermissionError as e:
            record["action"] = "permission_denied"
            record["error"] = str(e)
        except Exception as e:
            record["action"] = "error"
            record["error"] = str(e)
    _write_decision(record)
    return record


# ---- main loop ----------------------------------------------------------

def run_once(state: WatchdogState) -> Dict:
    """單次檢查；測試用。"""
    stale_rec = reap_stale_playwright(state)
    if stale_rec is not None:
        return stale_rec
    r = read_memory()
    target = state.record_reading(r)
    if target is None:
        return {"pressure": is_memory_pressure(r), "consecutive": state.consecutive_pressure}
    mode = _kill_mode()
    rec = _do_action(target, r, mode)
    state.last_action_at = time.time()
    # reset counter so we don't double-fire
    state.consecutive_pressure = 0
    return rec


def main_loop() -> int:
    print(f"[memory-watchdog] start: interval={CHECK_INTERVAL_SEC}s "
          f"trigger={TRIGGER_CONSECUTIVE}x swap>{SWAP_THRESHOLD_GB}GB "
          f"or free+inactive<{FREE_INACTIVE_MIN_GB}GB, mode={_kill_mode()}, "
          f"stale_playwright={STALE_PLAYWRIGHT_MODE if STALE_PLAYWRIGHT_ENABLED else 'off'}")
    state = WatchdogState()
    while True:
        try:
            run_once(state)
        except Exception as e:
            print(f"[memory-watchdog] loop error: {e}", file=sys.stderr)
        time.sleep(CHECK_INTERVAL_SEC)
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main_loop())
