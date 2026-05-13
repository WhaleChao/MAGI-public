#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Layer 3 — omlx switch 前置守門。

由 omlx_switch_model.sh 在切換前呼叫；三個子命令：

1. check-paused
     若 .runtime/oomlx_switch_paused_until 存在且 TTL 未過 → 寫入 alert file，
     exit 1（由 bash 攔截，跳過本輪切換）。

2. check-rss-before-switch --max-model-memory-gb N --mode {day,night}
     列出所有 omlx serve；任何一個 RSS > N × 1.3 視為 RSS 異常，append
     aborts.jsonl，exit 3（由 bash 攔截）。

3. register-abort --reason X
     append aborts.jsonl；若同 reason 過去 24h 已 abort ≥ 3 次，touch
     pause 檔並設定 TTL（預設 +6h，可由 OMLX_SWITCH_PAUSE_TTL_SEC 調）。
     TTL 有上限 24h，防止誤設永久 pause。

紅線：
  - pause 檔必須有 TTL（絕不無限 pause）
  - abort throttle 不得阻擋 `status` / `auto`（由 bash 端控制）
  - RSS 檢查只看 omlx serve，不誤判其他 python process
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT", "/Users/ai/Desktop/MAGI_v2")).resolve()
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

# runtime_dir 可能需要 MAGI_USE_RUNTIME_DIR=1 才會在正式位置，但 root() 本身
# 會 fallback 到 default，這裡無論 flag 都能用
from api.platforms import runtime_dir  # noqa: E402

ADMIN_NOTIFY_FILE = Path(os.environ.get(
    "OMLX_SWITCH_ADMIN_NOTIFY",
    "/tmp/omlx_switch_alert.txt",
))
DEFAULT_ABORT_WINDOW_SEC = 24 * 3600
DEFAULT_ABORT_THRESHOLD = 3
DEFAULT_PAUSE_TTL_SEC = 6 * 3600
MAX_PAUSE_TTL_SEC = 24 * 3600
RSS_MULTIPLIER = 1.3


def _aborts_log() -> Path:
    return runtime_dir.root() / "oomlx_switch_aborts.jsonl"


def _pause_file() -> Path:
    return runtime_dir.root() / "oomlx_switch_paused_until"


def _now() -> float:
    return time.time()


def _notify_admin(msg: str) -> None:
    try:
        ADMIN_NOTIFY_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(ADMIN_NOTIFY_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [omlx_switch alert] {msg}\n")
    except OSError:
        pass
    print(f"[gatekeeper] ALERT: {msg}", file=sys.stderr)


def _read_pause_until() -> Optional[float]:
    p = _pause_file()
    if not p.exists():
        return None
    try:
        ts_str = p.read_text(encoding="utf-8").strip()
        return float(ts_str)
    except (OSError, ValueError):
        return None


def _write_pause_until(ts: float) -> None:
    p = _pause_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{int(ts)}\n", encoding="utf-8")


def _append_abort(record: Dict[str, Any]) -> None:
    log = _aborts_log()
    record["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    record["ts_epoch"] = int(_now())
    runtime_dir.atomic_append_jsonl(log, record, rotate_at=500, keep_tail=300)


def _read_recent_aborts(reason: str, window_sec: int) -> List[Dict[str, Any]]:
    log = _aborts_log()
    if not log.exists():
        return []
    cutoff = _now() - window_sec
    out: List[Dict[str, Any]] = []
    try:
        for line in log.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("reason") != reason:
                continue
            if rec.get("ts_epoch", 0) < cutoff:
                continue
            out.append(rec)
    except OSError:
        return []
    return out


# ---------- check-paused ------------------------------------------------

def cmd_check_paused(_args: argparse.Namespace) -> int:
    until = _read_pause_until()
    if until is None:
        return 0
    now = _now()
    if until <= now:
        # TTL 已過 → 清除 pause file，允許切換
        try:
            _pause_file().unlink()
        except OSError:
            pass
        return 0
    remaining = int(until - now)
    print(f"[gatekeeper] omlx switch paused for another {remaining}s "
          f"(until ts={int(until)})")
    return 1


# ---------- check-rss-before-switch ------------------------------------

_MODEL_DIR_RE = re.compile(r"--model-dir\s+(\S+)")


def _list_omlx_rss() -> List[Dict[str, Any]]:
    try:
        res = subprocess.run(
            ["ps", "-eo", "pid=,rss=,command="],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    out: List[Dict[str, Any]] = []
    for line in res.stdout.splitlines():
        if "omlx serve" not in line:
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
        if "omlx serve" not in cmd:
            continue
        m = _MODEL_DIR_RE.search(cmd)
        mdir = m.group(1) if m else None
        out.append({
            "pid": pid,
            "rss_bytes": rss_kb * 1024,
            "rss_gb": rss_kb / 1024 / 1024,
            "model_dir": mdir,
            "cmdline": cmd,
        })
    return out


def cmd_check_rss(args: argparse.Namespace) -> int:
    max_gb = float(args.max_model_memory_gb)
    threshold_gb = max_gb * RSS_MULTIPLIER
    procs = _list_omlx_rss()
    offenders = [p for p in procs if p["rss_gb"] > threshold_gb]
    if not offenders:
        print(f"[gatekeeper] RSS check OK: {len(procs)} omlx process(es), "
              f"all ≤ {threshold_gb:.1f}GB")
        return 0
    snapshot = [
        {"pid": p["pid"], "rss_gb": round(p["rss_gb"], 2),
         "model_dir": p["model_dir"]}
        for p in procs
    ]
    _append_abort({
        "reason": "omlx_rss_exceeded",
        "mode": getattr(args, "mode", None),
        "max_model_memory_gb": max_gb,
        "threshold_gb": threshold_gb,
        "rss_snapshot": snapshot,
        "offenders_pids": [p["pid"] for p in offenders],
    })
    for p in offenders:
        print(f"[gatekeeper] RSS EXCEEDED: pid={p['pid']} "
              f"rss={p['rss_gb']:.2f}GB > {threshold_gb:.1f}GB "
              f"model_dir={p['model_dir']}")
    _notify_admin(
        f"{args.mode or '?'} 切換前 oMLX RSS 超標（{len(offenders)}/{len(procs)} process）— 已中止"
    )
    # 順便做 throttle 判斷（register 同一 reason）
    _maybe_pause("omlx_rss_exceeded")
    return 3


# ---------- register-abort ---------------------------------------------

def cmd_register_abort(args: argparse.Namespace) -> int:
    reason = args.reason
    _append_abort({
        "reason": reason,
        "mode": args.mode,
        "extra": args.extra,
    })
    _maybe_pause(reason)
    return 0


def _maybe_pause(reason: str) -> None:
    window = int(os.environ.get("OMLX_SWITCH_ABORT_WINDOW_SEC", str(DEFAULT_ABORT_WINDOW_SEC)))
    threshold = int(os.environ.get("OMLX_SWITCH_ABORT_THRESHOLD", str(DEFAULT_ABORT_THRESHOLD)))
    recent = _read_recent_aborts(reason, window)
    if len(recent) < threshold:
        return
    ttl_raw = int(os.environ.get("OMLX_SWITCH_PAUSE_TTL_SEC", str(DEFAULT_PAUSE_TTL_SEC)))
    ttl = max(60, min(ttl_raw, MAX_PAUSE_TTL_SEC))
    until = _now() + ttl
    _write_pause_until(until)
    _notify_admin(
        f"OOMLX 切換異常需人工介入（reason={reason} 已 {len(recent)} 次 / {window // 3600}h），"
        f"pause 至 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(until))}"
    )


# ---------- entrypoint -------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="omlx switch gatekeeper (Layer 3)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("check-paused", help="exit 1 if pause file active")

    rss_parser = sub.add_parser("check-rss-before-switch", help="exit 3 if any omlx serve RSS > max × 1.3")
    rss_parser.add_argument("--max-model-memory-gb", type=float, required=True)
    rss_parser.add_argument("--mode", default="unknown")

    ab = sub.add_parser("register-abort", help="append abort and maybe pause")
    ab.add_argument("--reason", required=True)
    ab.add_argument("--mode", default="unknown")
    ab.add_argument("--extra", default="")

    args = parser.parse_args(argv)
    handlers = {
        "check-paused": cmd_check_paused,
        "check-rss-before-switch": cmd_check_rss,
        "register-abort": cmd_register_abort,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
