#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
24h stability observer
---------------------
Periodically samples DAY3 stability metrics and writes JSONL snapshots.
At the end, emits a final report JSON and TXT export.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict


MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2])))
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.runtime_paths import get_metrics_dir

METRICS_DIR = get_metrics_dir()
DAY3_MOD_PATH = MAGI_ROOT / "scripts" / "ops" / "day3_stability_report.py"


def _load_day3_module():
    spec = importlib.util.spec_from_file_location("day3_stability_report", str(DAY3_MOD_PATH))
    if not spec or not spec.loader:
        raise RuntimeError(f"failed to load module: {DAY3_MOD_PATH}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _build_report(mod, start_dt: datetime, now_dt: datetime, hours: int, from_ts: str = "") -> Dict[str, Any]:
    return {
        "generated_at": now_dt.isoformat(timespec="seconds"),
        "window_hours": int(hours),
        "from_ts": str(from_ts or ""),
        "window_start": start_dt.isoformat(timespec="seconds"),
        "window_end": now_dt.isoformat(timespec="seconds"),
        "autopilot": mod.collect_autopilot(start_dt),
        "summary": mod.collect_summary_metrics(start_dt),
        "transcript_queue": mod.collect_captcha_queue(start_dt),
        "transcript_manual_queue": mod.collect_transcript_manual_queue(start_dt),
        "notify_outbox": mod.collect_notify_outbox(start_dt),
        "transcribe": mod.collect_transcribe_metrics(start_dt),
        "connectivity": mod.collect_connectivity(),
    }


def _snapshot_row(report: Dict[str, Any]) -> Dict[str, Any]:
    ap = report.get("autopilot") or {}
    sm = report.get("summary") or {}
    tq = report.get("transcript_queue") or {}
    tm = report.get("transcript_manual_queue") or {}
    no = report.get("notify_outbox") or {}
    conn = report.get("connectivity") or {}
    return {
        "ts": report.get("generated_at"),
        "window_start": report.get("window_start"),
        "window_end": report.get("window_end"),
        "autopilot_success_rate": float(ap.get("success_rate") or 0.0),
        "summary_timeout_rate": float(sm.get("timeout_rate") or 0.0),
        "summary_p95_sec": float(sm.get("p95_sec") or 0.0),
        "transcript_captcha_queue_total": int(tq.get("total") or 0),
        "transcript_manual_pending": int(tm.get("pending") or 0),
        "notify_outbox_pending": int(no.get("pending") or 0),
        "notify_outbox_recovered": int(no.get("recovered") or 0),
        "connectivity_fail": int(conn.get("fail") or 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Observe stability metrics over 24h window")
    ap.add_argument("--hours", type=int, default=24, help="observation duration")
    ap.add_argument("--interval-sec", type=int, default=300, help="sampling interval")
    ap.add_argument("--from-ts", type=str, default="", help="optional fixed ISO lower bound")
    ap.add_argument("--snapshot-path", type=str, default="", help="jsonl snapshot output path")
    ap.add_argument("--final-json-path", type=str, default="", help="final report json output path")
    ap.add_argument("--status-path", type=str, default="", help="runtime status json output path")
    ap.add_argument("--once", action="store_true", help="sample once and exit")
    args = ap.parse_args()

    mod = _load_day3_module()
    now = datetime.now()
    start = now
    from_ts = str(args.from_ts or "").strip()
    if from_ts:
        try:
            start = datetime.fromisoformat(from_ts)
        except Exception:
            start = now
    end_at = start + timedelta(hours=max(1, int(args.hours)))

    stamp = start.strftime("%Y%m%d_%H%M%S")
    snapshot_path = Path(args.snapshot_path or str(METRICS_DIR / f"stability_observe_{stamp}.jsonl"))
    final_json_path = Path(args.final_json_path or str(METRICS_DIR / f"stability_observe_{stamp}_final.json"))
    status_path = Path(args.status_path or str(METRICS_DIR / f"stability_observe_{stamp}_status.json"))
    interval_sec = max(30, int(args.interval_sec))

    while True:
        now = datetime.now()
        report = _build_report(mod, start, now, int(args.hours), from_ts)
        row = _snapshot_row(report)
        _append_jsonl(snapshot_path, row)
        _write_json(status_path, {"running": True, "pid": os.getpid(), "last_snapshot": row, "snapshot_path": str(snapshot_path), "final_json_path": str(final_json_path)})

        if args.once or now >= end_at:
            txt = mod.render_txt(report)
            export_txt = mod._load_export_txt()
            txt_out = export_txt(txt, prefix="qa_24h_stability_report")
            payload = {"success": True, "report": report, "snapshot_path": str(snapshot_path), "txt_export": txt_out}
            _write_json(final_json_path, payload)
            _write_json(status_path, {"running": False, "pid": os.getpid(), "finished_at": datetime.now().isoformat(timespec="seconds"), "snapshot_path": str(snapshot_path), "final_json_path": str(final_json_path), "txt_export": txt_out})
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0

        sleep_for = min(interval_sec, max(1, int((end_at - now).total_seconds())))
        time.sleep(sleep_for)


if __name__ == "__main__":
    raise SystemExit(main())
