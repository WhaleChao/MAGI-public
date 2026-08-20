#!/usr/bin/env python3
"""Low-pressure incremental reconciliation worker.

This is a short-lived cron task, not another resident service.  It drains a
small number of case evidence events when the Mac has headroom and performs
one bounded full scan per day as a safety net.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


RELEASE_ROOT = Path(__file__).resolve().parents[2]
if str(RELEASE_ROOT) not in sys.path:
    sys.path.insert(0, str(RELEASE_ROOT))

from magi_v3.business_events import BusinessEventLedger  # noqa: E402


def _bounded_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def resource_snapshot() -> dict[str, Any]:
    """Return a cheap, privacy-safe hardware admission snapshot."""
    load_1m = float(os.getloadavg()[0])
    cpu_count = max(1, int(os.cpu_count() or 1))
    normalized_load = load_1m / cpu_count
    free_percent: int | None = None
    try:
        probe = subprocess.run(
            ["/usr/bin/memory_pressure", "-Q"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        match = re.search(r"free percentage:\s*(\d+)%", probe.stdout, re.I)
        if probe.returncode == 0 and match:
            free_percent = int(match.group(1))
    except (OSError, subprocess.SubprocessError):
        free_percent = None
    min_free = int(_bounded_float("MAGI_AUTONOMY_MIN_FREE_PERCENT", 25, 15, 60))
    max_load = _bounded_float("MAGI_AUTONOMY_MAX_NORMALIZED_LOAD", 0.80, 0.25, 1.5)
    min_disk_gb = _bounded_float("MAGI_AUTONOMY_MIN_DISK_FREE_GB", 30, 10, 100)
    disk_root = Path(os.environ.get("MAGI_RUNTIME_DIR", "").strip() or Path.home())
    try:
        disk_free_gb = shutil.disk_usage(disk_root).free / (1024**3)
    except OSError:
        disk_free_gb = None
    reasons: list[str] = []
    if free_percent is None:
        reasons.append("memory_probe_unavailable")
    elif free_percent < min_free:
        reasons.append("memory_headroom_low")
    if normalized_load > max_load:
        reasons.append("cpu_pressure_high")
    if disk_free_gb is None:
        reasons.append("disk_probe_unavailable")
    elif disk_free_gb < min_disk_gb:
        reasons.append("disk_headroom_low")
    return {
        "safe": not reasons,
        "free_percent": free_percent,
        "min_free_percent": min_free,
        "load_1m": round(load_1m, 2),
        "normalized_load": round(normalized_load, 3),
        "max_normalized_load": max_load,
        "disk_free_gb": round(disk_free_gb, 1) if disk_free_gb is not None else None,
        "min_disk_free_gb": min_disk_gb,
        "reasons": reasons,
    }


def _load_indexer() -> Callable[[dict[str, Any]], dict[str, Any]]:
    action_path = RELEASE_ROOT / "skills" / "osc-orchestrator" / "action.py"
    spec = importlib.util.spec_from_file_location("magi_osc_incremental_indexer", action_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("case indexer could not be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.task_index_cases


def _marker_path() -> Path:
    shared = os.environ.get("MAGI_SHARED_STATE_DIR", "").strip()
    root = Path(shared).expanduser() if shared else Path.home() / "Library" / "Application Support" / "MAGI"
    return root / "runtime" / "autonomy_full_scan.json"


def _full_scan_due(now: datetime, marker: Path) -> bool:
    # Use quiet shoulders rather than competing with either interactive hours
    # or the 02:00-06:00 heavy maintenance window.
    if now.hour not in {6, 7, 8, 20, 21, 22, 23}:
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return True
    return str(payload.get("completed_date") or "") != now.date().isoformat()


def _write_marker(marker: Path, result: dict[str, Any], now: datetime) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    temp = marker.with_suffix(f".tmp-{os.getpid()}")
    payload = {
        "schema_version": 1,
        "completed_date": now.date().isoformat(),
        "completed_at": now.isoformat(),
        "scanned": int(result.get("scanned") or 0),
        "updated": int(result.get("updated") or 0),
    }
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, marker)


def run_once(
    *,
    ledger: BusinessEventLedger | None = None,
    indexer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    snapshot: dict[str, Any] | None = None,
    max_events: int = 8,
    allow_full_scan: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    resources = snapshot or resource_snapshot()
    if not bool(resources.get("safe")):
        return {
            "ok": False,
            "success": False,
            "status": "deferred",
            "deferred": True,
            "partial": False,
            "retryable": False,
            "reason": "resource_guard_skipped",
            "resources": resources,
            "processed": 0,
        }

    ledger = ledger or BusinessEventLedger()
    events = ledger.claim(limit=max_events, lease_seconds=600)
    cases = sorted({str(event.get("case_number") or "") for event in events if event.get("case_number")})
    indexer = indexer or _load_indexer()
    processed = 0
    incremental_result: dict[str, Any] | None = None
    if cases:
        try:
            incremental_result = indexer(
                {
                    "_autonomy_worker_internal": True,
                    "only_case_numbers": cases,
                    "max_cases": len(cases),
                    "max_files_per_case": 120,
                    "time_budget_sec": 180,
                }
            )
            if not bool(incremental_result.get("ok")):
                raise RuntimeError("incremental reconciliation did not complete")
        except Exception:
            for event in events:
                attempts = int(event.get("attempts") or 1)
                if attempts >= 8:
                    ledger.fail(str(event["event_id"]), reason_code="reconcile_retry_exhausted")
                else:
                    ledger.defer(
                        str(event["event_id"]),
                        reason_code="reconcile_unavailable",
                        delay_seconds=min(21600, 300 * (2 ** min(5, attempts - 1))),
                    )
            return {
                "ok": False,
                "success": False,
                "status": "deferred",
                "deferred": True,
                "partial": False,
                "retryable": True,
                "reason": "incremental_reconcile_unavailable",
                "resources": resources,
                "processed": 0,
                "deferred_count": len(events),
            }
        for event in events:
            ledger.complete(
                str(event["event_id"]),
                {"incremental": True, "scanned": int(incremental_result.get("scanned") or 0)},
            )
            processed += 1

    current = now or datetime.now()
    marker = _marker_path()
    full_result: dict[str, Any] | None = None
    # Event traffic has priority.  The full safety-net scan runs only when the
    # queue was empty, avoiding an incremental and full NAS walk in one turn.
    if allow_full_scan and not events and _full_scan_due(current, marker):
        second_snapshot = resource_snapshot() if snapshot is None else resources
        if bool(second_snapshot.get("safe")):
            full_result = indexer(
                {
                    "_autonomy_worker_internal": True,
                    "max_cases": 220,
                    "max_files_per_case": 120,
                    "time_budget_sec": 600,
                }
            )
            if bool(full_result.get("ok")):
                _write_marker(marker, full_result, current)

    if full_result is not None and not bool(full_result.get("ok")):
        ledger.prune()
        return {
            "ok": False,
            "success": False,
            "status": "deferred",
            "deferred": True,
            "partial": False,
            "retryable": True,
            "reason": str(full_result.get("reason") or "full_scan_unavailable"),
            "resources": resources,
            "processed": processed,
            "case_count": len(cases),
            "incremental": incremental_result,
            "full_scan": full_result,
            "queue_health": ledger.health(),
        }

    ledger.prune()
    return {
        "ok": True,
        "success": True,
        "status": "success",
        "resources": resources,
        "processed": processed,
        "case_count": len(cases),
        "incremental": incremental_result,
        "full_scan": full_result,
        "queue_health": ledger.health(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-events", type=int, default=8)
    parser.add_argument("--no-full-scan", action="store_true")
    args = parser.parse_args()
    try:
        os.nice(10)
    except OSError:
        pass
    result = run_once(max_events=max(1, min(16, args.max_events)), allow_full_scan=not args.no_full_scan)
    print(json.dumps(result, ensure_ascii=False, default=str))
    # Resource pressure is an expected, self-recovering deferral.
    return 0 if result.get("ok") or result.get("status") == "deferred" else 1


if __name__ == "__main__":
    raise SystemExit(main())
