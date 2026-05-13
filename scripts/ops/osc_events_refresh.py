#!/usr/bin/env python3
"""Refresh OSC-created todos and calendar-imported events on a bounded cadence.

This is intentionally conservative for NAS safety:
- scans only a bounded number of case folders per run;
- imports Google Calendar incrementally when credentials are available;
- treats missing OAuth as a non-fatal partial result so fresh installs do not
  create noisy cron failures before the user connects Google.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
LATEST_PATH = ROOT / ".runtime" / "osc_events_refresh_latest.json"


def _load_osc_action_module():
    path = ROOT / "skills" / "osc-orchestrator" / "action.py"
    spec = importlib.util.spec_from_file_location("_magi_osc_orchestrator_action", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(mod)
    return mod


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    return value


def _write_latest(data: dict[str, Any], out_path: Path = LATEST_PATH) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(_json_safe(data), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out_path)


def run_refresh(args: argparse.Namespace) -> dict[str, Any]:
    os.environ.setdefault("MAGI_GCAL_DEDUP_ENABLED", "1")
    os.environ.setdefault("MAGI_GCAL_INCREMENTAL_IMPORT", "1")

    mod = _load_osc_action_module()
    started = time.monotonic()
    result: dict[str, Any] = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "interval_hours": 6,
        "scan": {},
        "calendar_import": {},
        "warnings": [],
    }

    if not args.calendar_only:
        try:
            result["scan"] = mod.task_scan_cases(
                {
                    "max_cases": args.max_cases,
                    "max_files_per_case": args.max_files_per_case,
                    "time_budget_sec": args.scan_time_budget_sec,
                    "dry_run": False,
                    "force_rebuild": bool(args.force_rebuild),
                }
            )
        except Exception as exc:
            result["ok"] = False
            result["scan"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

    if not args.scan_only:
        try:
            calendar_payload = {
                "lookback_days": args.lookback_days,
                "lookahead_days": args.lookahead_days,
                "limit": args.calendar_limit,
                "incremental": True,
            }
            cal = mod.task_gcal_import(calendar_payload)
            result["calendar_import"] = cal
            if not cal.get("ok") and cal.get("need_interactive_oauth"):
                result["warnings"].append("google_calendar_oauth_required")
            elif not cal.get("ok"):
                result["ok"] = False
        except Exception as exc:
            result["ok"] = False
            result["calendar_import"] = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:240]}"}

    result["elapsed_sec"] = round(time.monotonic() - started, 3)
    _write_latest(result, Path(args.json_out) if args.json_out else LATEST_PATH)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refresh OSC todos and calendar-imported events.")
    parser.add_argument("--max-cases", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_MAX_CASES", "30")))
    parser.add_argument("--max-files-per-case", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_MAX_FILES_PER_CASE", "40")))
    parser.add_argument("--scan-time-budget-sec", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_SCAN_BUDGET_SEC", "900")))
    parser.add_argument("--calendar-limit", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_CALENDAR_LIMIT", "250")))
    parser.add_argument("--lookback-days", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_LOOKBACK_DAYS", "30")))
    parser.add_argument("--lookahead-days", type=int, default=int(os.environ.get("OSC_EVENTS_REFRESH_LOOKAHEAD_DAYS", "180")))
    parser.add_argument("--json-out", default="")
    parser.add_argument("--scan-only", action="store_true")
    parser.add_argument("--calendar-only", action="store_true")
    parser.add_argument("--force-rebuild", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_refresh(args)
    print(json.dumps(_json_safe(result), ensure_ascii=False, indent=2, sort_keys=True))
    if result.get("ok"):
        return 0
    if "google_calendar_oauth_required" in (result.get("warnings") or []) and (result.get("scan") or {}).get("ok"):
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
