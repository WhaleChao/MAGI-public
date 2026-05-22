#!/usr/bin/env python3
"""Bounded Google Drive/NAS bidirectional case sync worker.

The worker intentionally processes a small rotating slice of matched cases per
run. New NAS-only case folders are mirrored to Google Drive using Drive's native
folder layout; file sync remains missing-only and never overwrites or deletes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.osc.drive_case_sync import DEFAULT_DRIVE_ROOT_NAME, run_inventory, runtime_dir


def state_path() -> Path:
    return runtime_dir() / "worker_state.json"


def load_state() -> dict:
    path = state_path()
    if not path.exists():
        return {"matched_case_offset": 0}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"matched_case_offset": 0}
    if not isinstance(data, dict):
        return {"matched_case_offset": 0}
    return data


def save_state(state: dict) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MAGI bounded Drive/NAS bidirectional sync worker")
    parser.add_argument("--root-id", default="")
    parser.add_argument("--root-name", default="")
    parser.add_argument("--max-depth", type=int, default=7)
    parser.add_argument("--max-items", type=int, default=5000)
    parser.add_argument("--matched-case-limit", type=int, default=4)
    parser.add_argument("--download-limit", type=int, default=20)
    parser.add_argument("--upload-limit", type=int, default=20)
    parser.add_argument("--max-download-bytes", type=int, default=300_000_000)
    parser.add_argument("--max-upload-bytes", type=int, default=300_000_000)
    parser.add_argument("--max-case-depth", type=int, default=4)
    parser.add_argument("--max-case-items", type=int, default=150)
    parser.add_argument("--create-drive-folder-limit", type=int, default=10)
    parser.add_argument("--create-drive-folder-max-age-hours", type=int, default=168)
    parser.add_argument("--drive-owner-bucket", default="")
    parser.add_argument("--no-downloads", action="store_true")
    parser.add_argument("--no-uploads", action="store_true")
    parser.add_argument("--no-create-drive-folders", action="store_true")
    parser.add_argument("--no-context-resolve", action="store_true")
    args = parser.parse_args(argv)

    state = load_state()
    offset = max(0, int(state.get("matched_case_offset") or 0))
    report = run_inventory(
        root_id=args.root_id,
        root_name=args.root_name or os.environ.get("MAGI_DRIVE_SYNC_ROOT_FOLDER_NAME", DEFAULT_DRIVE_ROOT_NAME),
        max_depth=args.max_depth,
        max_items=args.max_items,
        resolve_context=not args.no_context_resolve,
        file_diff=not (args.no_downloads and args.no_uploads),
        execute_downloads=not args.no_downloads,
        execute_uploads=not args.no_uploads,
        download_limit=args.download_limit,
        max_download_bytes=args.max_download_bytes,
        upload_limit=args.upload_limit,
        max_upload_bytes=args.max_upload_bytes,
        max_case_depth=args.max_case_depth,
        max_case_items=args.max_case_items,
        matched_case_limit=args.matched_case_limit,
        matched_case_offset=offset,
        ensure_drive_case_folders=not args.no_create_drive_folders,
        create_drive_folder_limit=args.create_drive_folder_limit,
        create_drive_folder_max_age_hours=args.create_drive_folder_max_age_hours,
        drive_owner_bucket_name=args.drive_owner_bucket,
    )

    matched_total = int((report.get("summary") or {}).get("matched_case_folders") or 0)
    scanned = int(((report.get("file_sync_plan") or {}).get("summary") or {}).get("matched_cases_scanned") or 0)
    if matched_total > 0:
        state["matched_case_offset"] = (offset + max(scanned, 1)) % matched_total
    else:
        state["matched_case_offset"] = 0
    state["last_output_paths"] = report.get("output_paths") or {}
    state["last_summary"] = report.get("summary") or {}
    state["last_file_sync_summary"] = (report.get("file_sync_plan") or {}).get("summary") or {}
    state["last_execution_summary"] = (report.get("execution_result") or {}).get("summary") or {}
    state["last_drive_folder_summary"] = (report.get("drive_folder_result") or {}).get("summary") or {}
    save_state(state)

    print(json.dumps({
        "ok": True,
        "matched_case_offset_before": offset,
        "matched_case_offset_after": state["matched_case_offset"],
        "summary": report.get("summary") or {},
        "file_sync_summary": state["last_file_sync_summary"],
        "execution_summary": state["last_execution_summary"],
        "drive_folder_summary": state["last_drive_folder_summary"],
        "output_paths": report.get("output_paths") or {},
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
