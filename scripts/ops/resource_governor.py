#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Resource governance for single-node MAGI.

This script is deliberately conservative. It never touches case folders,
databases, or user documents. Its job is to turn raw disk/swap/memory numbers
into an operational mode, emit a readable report, and perform only safe
pre-switch cleanup such as reaping stale MAGI-owned Playwright drivers.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2]))).resolve()
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.platforms import runtime_dir  # noqa: E402


DEFAULT_WARN_DISK_GB = float(os.environ.get("MAGI_RESOURCE_WARN_DISK_GB", "50"))
DEFAULT_CORE_ONLY_DISK_GB = float(os.environ.get("MAGI_RESOURCE_CORE_ONLY_DISK_GB", "30"))
DEFAULT_CRITICAL_DISK_GB = float(os.environ.get("MAGI_RESOURCE_CRITICAL_DISK_GB", "15"))
DEFAULT_SWAP_WARN_GB = float(os.environ.get("MAGI_RESOURCE_SWAP_WARN_GB", "16"))
DEFAULT_SWAP_CORE_ONLY_GB = float(os.environ.get("MAGI_RESOURCE_SWAP_CORE_ONLY_GB", "24"))
DEFAULT_SWAP_CRITICAL_GB = float(os.environ.get("MAGI_RESOURCE_SWAP_CRITICAL_GB", "28"))
DEFAULT_FREE_WARN_GB = float(os.environ.get("MAGI_RESOURCE_FREE_WARN_GB", "6"))
DEFAULT_FREE_CORE_ONLY_GB = float(os.environ.get("MAGI_RESOURCE_FREE_CORE_ONLY_GB", "4"))
DEFAULT_FREE_CRITICAL_GB = float(os.environ.get("MAGI_RESOURCE_FREE_CRITICAL_GB", "2"))


def _memory_watchdog():
    from scripts.ops import memory_watchdog  # noqa: WPS433

    return memory_watchdog


@dataclass
class ResourceSnapshot:
    disk_free_gb: float
    disk_total_gb: float
    swap_used_gb: float
    free_gb: float
    inactive_gb: float
    free_plus_inactive_gb: float
    memory_free_percent: float = -1.0
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))


@dataclass
class ResourceDecision:
    ok: bool
    level: str
    reasons: list[str]
    actions: list[str]
    snapshot: ResourceSnapshot


def collect_snapshot(path: Path = MAGI_ROOT) -> ResourceSnapshot:
    usage = shutil.disk_usage(path)
    memory_watchdog = _memory_watchdog()
    mem = memory_watchdog.read_memory()
    return ResourceSnapshot(
        disk_free_gb=round(usage.free / (1024 ** 3), 2),
        disk_total_gb=round(usage.total / (1024 ** 3), 2),
        swap_used_gb=round(mem.swap_used_gb, 2),
        free_gb=round(mem.free_gb, 2),
        inactive_gb=round(mem.inactive_gb, 2),
        free_plus_inactive_gb=round(mem.free_plus_inactive_gb, 2),
        memory_free_percent=_read_memory_free_percent(),
    )


def _read_memory_free_percent() -> float:
    """Return macOS memory_pressure free percentage, or -1 when unavailable."""
    try:
        proc = subprocess.run(
            ["memory_pressure", "-Q"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return -1.0
    for line in (proc.stdout or "").splitlines():
        if "System-wide memory free percentage:" not in line:
            continue
        try:
            return float(line.rsplit(":", 1)[1].strip().rstrip("%"))
        except Exception:
            return -1.0
    return -1.0


def classify(snapshot: ResourceSnapshot) -> ResourceDecision:
    level_rank = {"normal": 0, "throttle": 1, "core_only": 2, "critical": 3}
    level = "normal"
    reasons: list[str] = []
    actions: list[str] = []

    def raise_to(new_level: str, reason: str) -> None:
        nonlocal level
        if level_rank[new_level] > level_rank[level]:
            level = new_level
        reasons.append(reason)

    if snapshot.disk_free_gb < DEFAULT_CRITICAL_DISK_GB:
        raise_to("critical", f"disk_free<{DEFAULT_CRITICAL_DISK_GB:g}GB")
    elif snapshot.disk_free_gb < DEFAULT_CORE_ONLY_DISK_GB:
        raise_to("core_only", f"disk_free<{DEFAULT_CORE_ONLY_DISK_GB:g}GB")
    elif snapshot.disk_free_gb < DEFAULT_WARN_DISK_GB:
        raise_to("throttle", f"disk_free<{DEFAULT_WARN_DISK_GB:g}GB")

    memory_pressure_healthy = snapshot.memory_free_percent >= 25.0
    swap_is_probably_stale = memory_pressure_healthy and snapshot.free_plus_inactive_gb >= DEFAULT_FREE_CRITICAL_GB

    if snapshot.swap_used_gb > DEFAULT_SWAP_CRITICAL_GB and not swap_is_probably_stale:
        raise_to("critical", f"swap_used>{DEFAULT_SWAP_CRITICAL_GB:g}GB")
    elif snapshot.swap_used_gb > DEFAULT_SWAP_CORE_ONLY_GB and not swap_is_probably_stale:
        raise_to("core_only", f"swap_used>{DEFAULT_SWAP_CORE_ONLY_GB:g}GB")
    elif snapshot.swap_used_gb > DEFAULT_SWAP_WARN_GB and not swap_is_probably_stale:
        raise_to("throttle", f"swap_used>{DEFAULT_SWAP_WARN_GB:g}GB")

    if snapshot.free_plus_inactive_gb < DEFAULT_FREE_CRITICAL_GB:
        raise_to("critical", f"free_plus_inactive<{DEFAULT_FREE_CRITICAL_GB:g}GB")
    elif snapshot.free_plus_inactive_gb < DEFAULT_FREE_CORE_ONLY_GB and not memory_pressure_healthy:
        raise_to("core_only", f"free_plus_inactive<{DEFAULT_FREE_CORE_ONLY_GB:g}GB")
    elif snapshot.free_plus_inactive_gb < DEFAULT_FREE_WARN_GB and not memory_pressure_healthy:
        raise_to("throttle", f"free_plus_inactive<{DEFAULT_FREE_WARN_GB:g}GB")

    if level in {"throttle", "core_only", "critical"}:
        actions.extend([
            "pause_heavy_backlog_jobs",
            "prefer_e4b_for_non_critical_work",
            "skip_training_or_distill_deploy",
        ])
    if level in {"core_only", "critical"}:
        actions.extend([
            "business_core_only",
            "defer_bulk_ocr_and_judgment_summarization",
            "require_manual_confirmation_for_26b",
        ])
    if level == "critical":
        actions.extend([
            "do_not_start_26b",
            "notify_operator",
        ])

    return ResourceDecision(
        ok=level != "critical",
        level=level,
        reasons=reasons,
        actions=actions,
        snapshot=snapshot,
    )


def append_metric(decision: ResourceDecision) -> Path:
    path = runtime_dir.metrics("resource_governor")
    runtime_dir.atomic_append_jsonl(path, asdict(decision), rotate_at=500, keep_tail=300)
    return path


def safe_cleanup(*, enforce: bool) -> list[dict[str, Any]]:
    """Run safe cleanup only. No user data, DB, NAS, or model files are removed."""
    actions: list[dict[str, Any]] = []
    memory_watchdog = _memory_watchdog()
    old_mode = os.environ.get("MAGI_WATCHDOG_STALE_PLAYWRIGHT_MODE")
    try:
        os.environ["MAGI_WATCHDOG_STALE_PLAYWRIGHT_MODE"] = "enforce" if enforce else "shadow"
        state = memory_watchdog.WatchdogState()
        rec = memory_watchdog.reap_stale_playwright(state)
        if rec:
            actions.append({"name": "reap_stale_playwright", "result": rec})
    finally:
        if old_mode is None:
            os.environ.pop("MAGI_WATCHDOG_STALE_PLAYWRIGHT_MODE", None)
        else:
            os.environ["MAGI_WATCHDOG_STALE_PLAYWRIGHT_MODE"] = old_mode
    if not actions:
        actions.append({"name": "safe_cleanup", "result": "nothing_to_clean"})
    return actions


def prepare_switch(mode: str, required_free_gb: float, *, enforce: bool) -> dict[str, Any]:
    before = collect_snapshot()
    cleanup_actions = safe_cleanup(enforce=enforce)
    # Give SIGTERM/SIGKILL fallout a small moment to show up in vm_stat.
    time.sleep(float(os.environ.get("MAGI_RESOURCE_GOVERNOR_SETTLE_SEC", "2")))
    after = collect_snapshot()
    decision = classify(after)
    ok = after.free_plus_inactive_gb >= required_free_gb and decision.ok
    payload = {
        "ok": ok,
        "mode": mode,
        "required_free_gb": required_free_gb,
        "before": asdict(before),
        "after": asdict(after),
        "decision": asdict(decision),
        "cleanup_actions": cleanup_actions,
    }
    runtime_dir.atomic_append_jsonl(
        runtime_dir.root() / "resource_governor_switch.jsonl",
        payload,
        rotate_at=300,
        keep_tail=200,
    )
    return payload


def _print_human(decision: ResourceDecision) -> None:
    s = decision.snapshot
    print(f"MAGI Resource Governor: {decision.level.upper()} ok={decision.ok}")
    print(
        f"disk={s.disk_free_gb:.2f}/{s.disk_total_gb:.2f}GB free, "
        f"swap={s.swap_used_gb:.2f}GB, free+inactive={s.free_plus_inactive_gb:.2f}GB, "
        f"memory_free={s.memory_free_percent:.0f}%"
    )
    if decision.reasons:
        print("reasons: " + ", ".join(decision.reasons))
    if decision.actions:
        print("actions: " + ", ".join(decision.actions))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI resource governance and safe cleanup.")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    status = sub.add_parser("status")
    status.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--enforce", action="store_true")
    cleanup.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    prep = sub.add_parser("prepare-switch")
    prep.add_argument("--mode", required=True, choices=["DAY", "NIGHT", "day", "night"])
    prep.add_argument("--required-free-gb", type=float, required=True)
    prep.add_argument("--enforce", action="store_true")
    prep.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.cmd == "status":
        decision = classify(collect_snapshot())
        append_metric(decision)
        if args.json:
            print(json.dumps(asdict(decision), ensure_ascii=False, indent=2))
        else:
            _print_human(decision)
        return 0 if decision.ok else 2

    if args.cmd == "cleanup":
        payload = {"ok": True, "actions": safe_cleanup(enforce=bool(args.enforce))}
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else payload)
        return 0

    if args.cmd == "prepare-switch":
        payload = prepare_switch(args.mode.upper(), args.required_free_gb, enforce=bool(args.enforce))
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else payload)
        return 0 if payload.get("ok") else 2

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
