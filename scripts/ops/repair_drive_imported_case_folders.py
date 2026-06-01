#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Repair Google-Drive-style folders accidentally downloaded into NAS cases.

Drive and NAS intentionally use different case folder vocabulary.  Downloaded
Drive files must be filed into NAS/OSC canonical subfolders, while uploaded NAS
files should follow the existing Google Drive folder vocabulary.  This script
repairs already-mixed local case folders without overwriting different files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.osc.drive_case_sync import drive_to_nas_relative_path, split_relative_parts

DEFAULT_REPORT = MAGI_ROOT / ".runtime" / "drive_imported_folder_repair_latest.json"
HISTORY_PATH = MAGI_ROOT / ".runtime" / "drive_imported_folder_repair_history.jsonl"
IGNORE_NAMES = {".DS_Store", "@eaDir", "#recycle", ".TemporaryItems", ".Trashes", ".duplicates"}
CANONICAL_FIRST_SEGMENT_PREFIXES = tuple(f"{i:02d}_" for i in range(1, 40))


def file_md5(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def is_noncanonical_drive_folder(name: str) -> bool:
    if not name or name in IGNORE_NAMES or name.startswith("."):
        return False
    if name.startswith(CANONICAL_FIRST_SEGMENT_PREFIXES):
        return False
    mapped = drive_to_nas_relative_path(f"{name}/__probe__")
    mapped_parts = split_relative_parts(mapped)
    return bool(mapped_parts and mapped_parts[0] != name)


def mapped_file_relative_path(alias_folder: str, inner_relative: str) -> str:
    rel = PurePosixPath(alias_folder, inner_relative).as_posix()
    return drive_to_nas_relative_path(rel)


def _iter_files(folder: Path, *, max_files: int, max_seconds: int) -> tuple[list[Path], dict[str, Any]]:
    started = time.time()
    files: list[Path] = []
    stopped_reason = "completed"
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [
            name for name in dirnames
            if name not in IGNORE_NAMES and not name.startswith(".")
        ]
        for filename in sorted(filenames):
            if filename in IGNORE_NAMES or filename.startswith("._") or filename.startswith("~$"):
                continue
            files.append(Path(dirpath) / filename)
            if max_files and len(files) >= max_files:
                stopped_reason = "max_files"
                return files, {"stopped_reason": stopped_reason, "elapsed_sec": round(time.time() - started, 3)}
            if max_seconds and time.time() - started >= max_seconds:
                stopped_reason = "max_seconds"
                return files, {"stopped_reason": stopped_reason, "elapsed_sec": round(time.time() - started, 3)}
    return files, {"stopped_reason": stopped_reason, "elapsed_sec": round(time.time() - started, 3)}


def _remove_empty_dirs(root: Path, *, stop_at: Path) -> int:
    removed = 0
    for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
        path = Path(dirpath)
        if path == stop_at:
            continue
        try:
            path.rmdir()
            removed += 1
        except OSError:
            pass
    return removed


def repair_case_folder(
    case_folder: Path,
    *,
    apply: bool = False,
    delete_duplicate: bool = False,
    max_files: int = 500,
    max_seconds: int = 300,
) -> dict[str, Any]:
    case_folder = case_folder.expanduser()
    report: dict[str, Any] = {
        "case_folder": str(case_folder),
        "mode": "apply" if apply else "dry_run",
        "delete_duplicate": bool(delete_duplicate),
        "alias_folders": [],
        "planned_moves": [],
        "duplicates": [],
        "conflicts": [],
        "errors": [],
        "removed_empty_dirs": 0,
    }
    if not case_folder.is_dir():
        report["errors"].append({"path": str(case_folder), "error": "case_folder_not_found"})
        return report

    alias_dirs = [
        p for p in sorted(case_folder.iterdir(), key=lambda p: p.name)
        if p.is_dir() and is_noncanonical_drive_folder(p.name)
    ]
    report["alias_folders"] = [p.name for p in alias_dirs]

    for alias_dir in alias_dirs:
        files, meta = _iter_files(alias_dir, max_files=max_files, max_seconds=max_seconds)
        alias_item = {"name": alias_dir.name, "file_count": len(files), **meta}
        for src in files:
            try:
                inner = src.relative_to(alias_dir).as_posix()
                target_rel = mapped_file_relative_path(alias_dir.name, inner)
                target = case_folder / target_rel
                if target == src:
                    continue
                if target.exists():
                    same_size = target.stat().st_size == src.stat().st_size
                    same_hash = False
                    if same_size:
                        same_hash = file_md5(target) == file_md5(src)
                    if same_hash:
                        item = {
                            "source": str(src),
                            "target": str(target),
                            "target_relative_path": target_rel,
                            "action": "delete_duplicate" if delete_duplicate else "skip_duplicate",
                        }
                        report["duplicates"].append(item)
                        if apply and delete_duplicate:
                            src.unlink()
                        continue
                    report["conflicts"].append({
                        "source": str(src),
                        "target": str(target),
                        "target_relative_path": target_rel,
                        "reason": "target_exists_different_content",
                    })
                    continue
                item = {
                    "source": str(src),
                    "target": str(target),
                    "target_relative_path": target_rel,
                }
                report["planned_moves"].append(item)
                if apply:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(target))
            except Exception as exc:
                report["errors"].append({"source": str(src), "error": f"{type(exc).__name__}: {exc}"})
        report.setdefault("alias_folder_details", []).append(alias_item)
        if apply:
            report["removed_empty_dirs"] += _remove_empty_dirs(alias_dir, stop_at=case_folder)

    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair Drive-imported alias folders in a local case folder.")
    parser.add_argument("case_folder")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-duplicate", action="store_true", help="Delete exact duplicate files from alias folders after hash match.")
    parser.add_argument("--max-files", type=int, default=500)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--json-out", default=str(DEFAULT_REPORT))
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args(argv)

    started = time.time()
    report = repair_case_folder(
        Path(args.case_folder),
        apply=args.apply,
        delete_duplicate=args.delete_duplicate,
        max_files=args.max_files,
        max_seconds=args.max_seconds,
    )
    report["ok"] = not report["errors"]
    report["elapsed_sec"] = round(time.time() - started, 3)
    _write_report(Path(args.json_out), report)
    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            "drive-imported-folder-repair: "
            f"alias_folders={len(report.get('alias_folders') or [])} "
            f"moves={len(report.get('planned_moves') or [])} "
            f"duplicates={len(report.get('duplicates') or [])} "
            f"conflicts={len(report.get('conflicts') or [])} "
            f"errors={len(report.get('errors') or [])}"
        )
        print(f"report={args.json_out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
