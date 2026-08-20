#!/usr/bin/env python3
"""Audit and repair NAS-style folders that leaked into Google Drive cases.

The repair is deliberately one-way at the boundary:

* numbered OSC/NAS folders are merged into the Drive-native category;
* merge conflicts are renamed and retained;
* the emptied/duplicate source folder is moved to Drive trash (recoverable);
* internal folders are moved to one fixed MAGI review area, never copied into
  another case folder;
* dry-run is the default and every write requires ``--apply``.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from api.domains.case_file_operation_lock import (
    acquire_case_file_operation_lock,
    release_case_file_operation_lock,
)
from api.osc.drive_case_sync import (
    DRIVE_EXISTING_ALIAS_PRIORITY,
    GOOGLE_FOLDER_MIME,
    SEMANTIC_TO_DRIVE_DEFAULT_PARTS,
    SYNC_IGNORE_NAMES,
    _drive_first_segment_semantic,
    _drive_list_children,
    _merge_drive_folder_children,
    _prefer_existing_drive_prefix,
    _semantic_first_for_nas_segment,
    build_drive_service,
    drive_file_entries,
    ensure_drive_folder_path,
    find_drive_root,
    move_drive_item,
    split_relative_parts,
    trash_drive_item,
)


DEFAULT_REVIEW_PATH = "MAGI待整理/跨端錯置資料夾"
INTERNAL_FOLDERS = {".duplicates", ".trash", ".runtime", ".magi"}


def _native_target_for_numbered_folder(name: str, sibling_names: set[str]) -> str:
    semantic = _semantic_first_for_nas_segment(name)
    if not semantic:
        semantic = _drive_first_segment_semantic(re.sub(r"^\d{2}_", "", str(name or "")))
    if not semantic:
        return ""
    valid_siblings = {
        value
        for value in sibling_names
        if value != name and not re.match(r"^\d{2}_", value) and not value.startswith(".")
    }
    # Repair only into an explicit Drive-native alias.  The regular sync mapper
    # may infer that an arbitrary dated folder ending in `狀` is pleading-like,
    # but collapsing an entire leaked NAS category into that one dated filing
    # would be a destructive semantic misclassification.
    for alias in DRIVE_EXISTING_ALIAS_PRIORITY.get(semantic, ()):
        if alias in valid_siblings:
            return alias
    prefix = _prefer_existing_drive_prefix(semantic, valid_siblings)
    if prefix:
        return PurePosixPath(*prefix).as_posix()
    default = SEMANTIC_TO_DRIVE_DEFAULT_PARTS.get(semantic) or ()
    return PurePosixPath(*default).as_posix() if default else ""


def _case_review_bucket(
    service: Any,
    drive_root_id: str,
    case_drive_id: str,
    *,
    review_path: str,
) -> str:
    root = ensure_drive_folder_path(service, drive_root_id, review_path)
    bucket = ensure_drive_folder_path(
        service,
        str(root.get("drive_id") or ""),
        case_drive_id[:16],
    )
    return str(bucket.get("drive_id") or "")


def repair_case_layout(
    service: Any,
    *,
    drive_root_id: str,
    case_drive_id: str,
    case_path: str = "",
    apply: bool = False,
    quarantine_unknown_numbered: bool = False,
    review_path: str = DEFAULT_REVIEW_PATH,
    max_depth: int = 12,
    max_items: int = 2000,
) -> dict[str, Any]:
    children = _drive_list_children(service, case_drive_id)
    folders = [item for item in children if item.get("mimeType") == GOOGLE_FOLDER_MIME]
    sibling_names = {str(item.get("name") or "") for item in folders}
    record: dict[str, Any] = {
        "case_drive_id": case_drive_id,
        "case_path": case_path,
        "mode": "apply" if apply else "dry_run",
        "numbered_folders": [],
        "internal_folders": [],
        "unknown_numbered_folders": [],
        "quarantined_unknown_folders": [],
        "errors": [],
    }
    for item in folders:
        name = str(item.get("name") or "")
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        if name in INTERNAL_FOLDERS:
            action = {
                "source_id": item_id,
                "source_name": name,
                "status": "planned_review_move",
            }
            if apply:
                try:
                    review_bucket = _case_review_bucket(
                        service,
                        drive_root_id,
                        case_drive_id,
                        review_path=review_path,
                    )
                    moved = move_drive_item(
                        service,
                        item_id,
                        add_parent_id=review_bucket,
                        remove_parent_ids=[case_drive_id],
                        new_name=f"{name}-{item_id[:8]}",
                    )
                    action["status"] = "moved_to_review"
                    action["result_id"] = str(moved.get("id") or "")
                except Exception as exc:  # pragma: no cover - live API edge
                    action["status"] = "failed"
                    action["error"] = f"{type(exc).__name__}: {exc}"
                    record["errors"].append(action["error"])
            record["internal_folders"].append(action)
            continue
        if not re.match(r"^\d{2}_", name):
            continue
        target_path = _native_target_for_numbered_folder(name, sibling_names)
        action = {
            "source_id": item_id,
            "source_name": name,
            "target_path": target_path,
            "status": "planned_merge" if target_path else "needs_review",
        }
        if not target_path:
            if apply and quarantine_unknown_numbered:
                try:
                    review_bucket = _case_review_bucket(
                        service,
                        drive_root_id,
                        case_drive_id,
                        review_path=review_path,
                    )
                    moved = move_drive_item(
                        service,
                        item_id,
                        add_parent_id=review_bucket,
                        remove_parent_ids=[case_drive_id],
                        new_name=f"{name}-{item_id[:8]}",
                    )
                    action["status"] = "moved_unknown_to_review"
                    action["result_id"] = str(moved.get("id") or "")
                    record["quarantined_unknown_folders"].append(action)
                    continue
                except Exception as exc:  # pragma: no cover - live API edge
                    action["status"] = "failed"
                    action["error"] = f"{type(exc).__name__}: {exc}"
                    record["errors"].append(action["error"])
            record["unknown_numbered_folders"].append(action)
            continue
        if apply:
            try:
                target = ensure_drive_folder_path(service, case_drive_id, target_path)
                target_id = str(target.get("drive_id") or "")
                merge = _merge_drive_folder_children(
                    service,
                    source_folder_id=item_id,
                    target_folder_id=target_id,
                    execute=True,
                    max_depth=max_depth,
                    max_items=max_items,
                    counter={"items": 0},
                )
                action["merge"] = merge
                if merge.get("skipped_limit"):
                    action["status"] = "partial_kept_source"
                else:
                    trashed = trash_drive_item(service, item_id)
                    action["status"] = "merged_source_trashed"
                    action["trashed_source_id"] = str(trashed.get("id") or "")
            except Exception as exc:  # pragma: no cover - live API edge
                action["status"] = "failed"
                action["error"] = f"{type(exc).__name__}: {exc}"
                record["errors"].append(action["error"])
        record["numbered_folders"].append(action)
    record["incomplete"] = any(
        action.get("status") == "partial_kept_source"
        for action in record["numbered_folders"]
    )
    record["ok"] = (
        not record["errors"]
        and not record["unknown_numbered_folders"]
        and not record["incomplete"]
    )
    record["violations"] = (
        len(record["numbered_folders"])
        + len(record["internal_folders"])
        + len(record["unknown_numbered_folders"])
        + len(record["quarantined_unknown_folders"])
    )
    return record


def _run_unlocked(
    *,
    apply: bool = False,
    max_cases: int = 0,
    case_ids: list[str] | None = None,
    root_id: str = "",
    root_name: str = "案件辦理",
    review_path: str = DEFAULT_REVIEW_PATH,
    quarantine_unknown_numbered: bool = False,
) -> dict[str, Any]:
    service = build_drive_service(interactive=False, write=apply)
    root = find_drive_root(service, root_id=root_id, root_name=root_name)
    root_drive_id = str(root.get("id") or "")
    selected: list[tuple[str, str]] = []
    if case_ids:
        selected = [(value, "") for value in dict.fromkeys(case_ids) if value]
    else:
        _entries, cases = drive_file_entries(service, root_drive_id, max_depth=8, max_items=10000)
        selected = [(case.drive_id, case.relative_path) for case in cases if case.drive_id]
    if max_cases:
        selected = selected[: max(0, max_cases)]

    records: list[dict[str, Any]] = []
    summary = {
        "cases_scanned": 0,
        "cases_with_violations": 0,
        "numbered_folders": 0,
        "internal_folders": 0,
        "unknown_numbered_folders": 0,
        "quarantined_unknown_folders": 0,
        "incomplete_cases": 0,
        "errors": 0,
        "apply": bool(apply),
    }
    for case_id, case_path in selected:
        summary["cases_scanned"] += 1
        try:
            record = repair_case_layout(
                service,
                drive_root_id=root_drive_id,
                case_drive_id=case_id,
                case_path=case_path,
                apply=apply,
                review_path=review_path,
                quarantine_unknown_numbered=quarantine_unknown_numbered,
            )
        except Exception as exc:  # pragma: no cover - live API edge
            record = {
                "case_drive_id": case_id,
                "case_path": case_path,
                "ok": False,
                "violations": 0,
                "errors": [f"{type(exc).__name__}: {exc}"],
                "numbered_folders": [],
                "internal_folders": [],
                "unknown_numbered_folders": [],
                "quarantined_unknown_folders": [],
                "incomplete": False,
            }
        if record.get("violations") or record.get("errors"):
            records.append(record)
        if record.get("violations"):
            summary["cases_with_violations"] += 1
        summary["numbered_folders"] += len(record.get("numbered_folders") or [])
        summary["internal_folders"] += len(record.get("internal_folders") or [])
        summary["unknown_numbered_folders"] += len(record.get("unknown_numbered_folders") or [])
        summary["quarantined_unknown_folders"] += len(record.get("quarantined_unknown_folders") or [])
        summary["incomplete_cases"] += int(bool(record.get("incomplete")))
        summary["errors"] += len(record.get("errors") or [])

    return {
        "ok": (
            summary["errors"] == 0
            and summary["unknown_numbered_folders"] == 0
            and summary["incomplete_cases"] == 0
        ),
        "generated_at": datetime.now().astimezone().isoformat(),
        "mode": "apply" if apply else "dry_run",
        "safety": "merge_no_overwrite_conflicts_renamed_source_trash_recoverable_internal_to_fixed_review_area",
        "drive_root_id": root_drive_id,
        "review_path": review_path,
        "summary": summary,
        "records": records,
    }


def run(
    *,
    apply: bool = False,
    max_cases: int = 0,
    case_ids: list[str] | None = None,
    root_id: str = "",
    root_name: str = "案件辦理",
    review_path: str = DEFAULT_REVIEW_PATH,
    quarantine_unknown_numbered: bool = False,
) -> dict[str, Any]:
    """Run the repair under the shared case-file mutation lock when applying."""
    lock: dict[str, Any] = {"acquired": True, "disabled": True}
    if apply:
        lock = acquire_case_file_operation_lock(owner="repair_drive_native_case_layout")
        if not lock.get("acquired"):
            return {
                "ok": False,
                "generated_at": datetime.now().astimezone().isoformat(),
                "mode": "apply",
                "safety": "write_blocked_by_case_file_operation_lock",
                "drive_root_id": "",
                "review_path": review_path,
                "summary": {
                    "cases_scanned": 0,
                    "cases_with_violations": 0,
                    "numbered_folders": 0,
                    "internal_folders": 0,
                    "unknown_numbered_folders": 0,
                    "quarantined_unknown_folders": 0,
                    "incomplete_cases": 0,
                    "errors": 1,
                    "apply": True,
                },
                "records": [{"status": "blocked", "reason": "case_file_operation_lock_busy"}],
                "lock": lock,
            }
    try:
        report = _run_unlocked(
            apply=apply,
            max_cases=max_cases,
            case_ids=case_ids,
            root_id=root_id,
            root_name=root_name,
            review_path=review_path,
            quarantine_unknown_numbered=quarantine_unknown_numbered,
        )
        report["lock"] = lock
        return report
    finally:
        if apply and lock.get("acquired"):
            release_case_file_operation_lock()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair NAS-style folders inside Google Drive cases")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--max-cases", type=int, default=0)
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--root-id", default="")
    parser.add_argument("--root-name", default="案件辦理")
    parser.add_argument("--review-path", default=DEFAULT_REVIEW_PATH)
    parser.add_argument("--quarantine-unknown-numbered", action="store_true")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args(argv)
    report = run(
        apply=args.apply,
        max_cases=max(0, args.max_cases),
        case_ids=args.case_id,
        root_id=args.root_id,
        root_name=args.root_name,
        review_path=args.review_path,
        quarantine_unknown_numbered=args.quarantine_unknown_numbered,
    )
    if args.json_out:
        path = Path(args.json_out).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "mode": report["mode"], "summary": report["summary"]}, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
