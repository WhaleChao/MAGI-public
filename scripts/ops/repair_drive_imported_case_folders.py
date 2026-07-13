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
import re
from pathlib import Path, PurePosixPath
from typing import Any

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.osc.drive_case_sync import (
    drive_to_nas_relative_path,
    looks_like_drive_case_folder_segment,
    split_relative_parts,
    strip_embedded_drive_case_folder,
)
from api.osc.case_folder_schema import case_subfolders as osc_case_subfolders
from skills.bridge.shared_utils.judgment_folder_names import judgment_folder_candidates, judgment_folder_name

DEFAULT_REPORT = MAGI_ROOT / ".runtime" / "drive_imported_folder_repair_latest.json"
HISTORY_PATH = MAGI_ROOT / ".runtime" / "drive_imported_folder_repair_history.jsonl"
IGNORE_NAMES = {".DS_Store", ".gitkeep", "@eaDir", "#recycle", ".TemporaryItems", ".Trashes", ".duplicates"}
CANONICAL_FIRST_SEGMENT_PREFIXES = tuple(f"{i:02d}_" for i in range(1, 40))
CASE_FOLDER_RE = re.compile(r"^20\d{2}-\d{4}-")
CASE_CATEGORY_SEGMENTS = {
    "一般案件": "一般案件",
    "法扶案件": "法扶案件",
    "法律扶助案件": "法扶案件",
    "指定辯護案件": "指定辯護案件",
    "無償案件": "無償案件",
}
CASE_CATEGORY_SCHEMA_SEGMENTS = {
    "一般案件": "一般案件",
    "法扶案件": "法律扶助案件",
    "法律扶助案件": "法律扶助案件",
    "指定辯護案件": "指定辯護案件",
    "無償案件": "無償案件",
}


def file_md5(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def is_noncanonical_drive_folder(name: str, *, case_context_name: str = "") -> bool:
    if not name or name in IGNORE_NAMES or name.startswith("."):
        return False
    if name.startswith(CANONICAL_FIRST_SEGMENT_PREFIXES):
        return False
    if looks_like_drive_case_folder_segment(name, case_context_name=case_context_name):
        return True
    mapped = drive_to_nas_relative_path(
        f"{name}/__probe__",
        case_context_name=case_context_name,
    )
    mapped_parts = split_relative_parts(mapped)
    return bool(mapped_parts and mapped_parts[0] != name)


def mapped_file_relative_path(
    alias_folder: str,
    inner_relative: str,
    *,
    case_category: str = "",
    case_context_name: str = "",
    existing_nas_first_segments: set[str] | None = None,
) -> str:
    rel = PurePosixPath(alias_folder, inner_relative).as_posix()
    stripped = strip_embedded_drive_case_folder(rel, case_context_name=case_context_name)
    return drive_to_nas_relative_path(
        stripped or rel,
        case_category=case_category,
        case_context_name=case_context_name,
        existing_nas_first_segments=existing_nas_first_segments,
    )


def _case_category_from_path(case_folder: Path) -> str:
    for part in case_folder.parts:
        if part in CASE_CATEGORY_SEGMENTS:
            return CASE_CATEGORY_SEGMENTS[part]
    return ""


def _canonical_case_first_segments(case_category: str) -> set[str]:
    schema_category = CASE_CATEGORY_SCHEMA_SEGMENTS.get(str(case_category or "").strip(), "")
    if not schema_category:
        return set()
    return set(osc_case_subfolders(schema_category))


def _existing_case_first_segments(case_folder: Path, *, case_category: str = "") -> set[str]:
    canonical_segments = _canonical_case_first_segments(case_category)
    try:
        numbered_segments = {
            p.name
            for p in case_folder.iterdir()
            if p.is_dir() and p.name.startswith(CANONICAL_FIRST_SEGMENT_PREFIXES)
        }
        if canonical_segments:
            return {name for name in numbered_segments if name in canonical_segments}
        return numbered_segments
    except OSError:
        return set()


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


def _unique_conflict_target(target: Path, label: str = "Drive匯入差異") -> Path:
    candidate = target.with_name(f"{target.stem}（{label}）{target.suffix}")
    if not candidate.exists():
        return candidate
    for idx in range(2, 1000):
        candidate = target.with_name(f"{target.stem}（{label}-{idx}）{target.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"unable_to_allocate_conflict_target:{target}")


def _record_file_move(
    report: dict[str, Any],
    src: Path,
    target: Path,
    target_rel: str,
    *,
    apply: bool,
    delete_duplicate: bool,
    move_conflicts_with_suffix: bool,
    hash_existing: bool = True,
    move_bucket: str = "planned_moves",
    conflict_label: str = "Drive匯入差異",
    reason: str = "",
) -> None:
    if target == src:
        return
    if target.exists():
        same_size = target.stat().st_size == src.stat().st_size
        same_hash = False
        if hash_existing and same_size:
            same_hash = file_md5(target) == file_md5(src)
        if same_hash:
            item = {
                "source": str(src),
                "target": str(target),
                "target_relative_path": target_rel,
                "action": "delete_duplicate" if delete_duplicate else "skip_duplicate",
            }
            if reason:
                item["reason"] = reason
            report["duplicates"].append(item)
            if apply and delete_duplicate:
                src.unlink()
            return
        if move_conflicts_with_suffix:
            conflict_target = _unique_conflict_target(target, label=conflict_label)
            item = {
                "source": str(src),
                "target": str(conflict_target),
                "target_relative_path": conflict_target.relative_to(Path(report["case_folder"])).as_posix(),
                "reason": reason or "target_exists_different_content",
            }
            report["conflict_moves"].append(item)
            if apply:
                conflict_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(conflict_target))
            return
        report["conflicts"].append({
            "source": str(src),
            "target": str(target),
            "target_relative_path": target_rel,
            "reason": reason or "target_exists_different_content",
        })
        return

    item = {
        "source": str(src),
        "target": str(target),
        "target_relative_path": target_rel,
    }
    if reason:
        item["reason"] = reason
    report.setdefault(move_bucket, []).append(item)
    if apply:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(target))


def _target_for_misfiled_judgment_file(case_folder: Path, src: Path) -> str:
    """Return canonical NAS relative target for files misplaced in judgment folders."""
    rel = ""
    for judgment_dir in judgment_folder_candidates(case_folder, 10):
        try:
            rel = src.relative_to(judgment_dir).as_posix()
            break
        except ValueError:
            continue
    if not rel:
        return ""
    text = rel.replace("\\", "/")
    name = PurePosixPath(text).name
    if not name:
        return ""

    if "筆錄" in text and "不成立證明" not in text:
        return PurePosixPath("08_筆錄", *PurePosixPath(rel).parts).as_posix()

    mapped = drive_to_nas_relative_path(PurePosixPath("法院裁判", rel).as_posix())
    mapped_parts = split_relative_parts(mapped)
    if mapped_parts and mapped_parts[0] in {"08_筆錄", "09_法院通知或程序裁定"}:
        return mapped
    return ""


def _iter_case_folders(root: Path, *, max_cases: int, max_seconds: int) -> tuple[list[Path], dict[str, Any]]:
    started = time.time()
    cases: list[Path] = []
    stopped_reason = "completed"
    for dirpath, dirnames, _filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames
            if name not in IGNORE_NAMES and not name.startswith(".")
        ]
        path = Path(dirpath)
        if CASE_FOLDER_RE.match(path.name):
            cases.append(path)
            dirnames[:] = []
            if max_cases and len(cases) >= max_cases:
                stopped_reason = "max_cases"
                break
        if max_seconds and time.time() - started >= max_seconds:
            stopped_reason = "max_seconds"
            break
    return cases, {"stopped_reason": stopped_reason, "elapsed_sec": round(time.time() - started, 3)}


def _iter_case_files(case_folder: Path, *, include_duplicates: bool) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(case_folder):
        if include_duplicates:
            dirnames[:] = [
                name for name in dirnames
                if name == ".duplicates" or (name not in IGNORE_NAMES and not name.startswith("."))
            ]
        else:
            dirnames[:] = [name for name in dirnames if name not in IGNORE_NAMES and not name.startswith(".")]
        for filename in filenames:
            if filename in IGNORE_NAMES or filename.startswith("._") or filename.startswith("~$"):
                continue
            files.append(Path(dirpath) / filename)
    return files


def _is_under_duplicate_quarantine(path: Path, case_folder: Path) -> bool:
    try:
        return ".duplicates" in path.relative_to(case_folder).parts
    except ValueError:
        return False


def cleanup_duplicate_quarantine(case_folder: Path, *, apply: bool = False, max_files: int = 3000) -> dict[str, Any]:
    report: dict[str, Any] = {
        "case_folder": str(case_folder),
        "mode": "apply" if apply else "dry_run",
        "quarantine_files": 0,
        "safe_duplicates": [],
        "kept_unique": [],
        "errors": [],
        "removed_empty_dirs": 0,
    }
    if not (case_folder / ".duplicates").exists() and not list(case_folder.glob("*/.duplicates")):
        return report

    try:
        all_files = _iter_case_files(case_folder, include_duplicates=True)
        normal_files = [p for p in all_files if not _is_under_duplicate_quarantine(p, case_folder)]
        quarantine_files = [p for p in all_files if _is_under_duplicate_quarantine(p, case_folder)]
        if max_files:
            normal_files = normal_files[:max_files]
            quarantine_files = quarantine_files[:max_files]
        report["quarantine_files"] = len(quarantine_files)

        hash_index: dict[tuple[int, str], list[Path]] = {}
        for path in normal_files:
            try:
                key = (path.stat().st_size, file_md5(path))
                hash_index.setdefault(key, []).append(path)
            except Exception as exc:
                report["errors"].append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})

        for path in quarantine_files:
            try:
                key = (path.stat().st_size, file_md5(path))
                originals = hash_index.get(key) or []
                if originals:
                    item = {
                        "duplicate": str(path),
                        "original": str(originals[0]),
                        "action": "delete" if apply else "would_delete",
                    }
                    report["safe_duplicates"].append(item)
                    if apply:
                        path.unlink()
                    continue
                report["kept_unique"].append(str(path))
            except Exception as exc:
                report["errors"].append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})

        if apply:
            for dup_dir in [case_folder / ".duplicates", *case_folder.glob("*/.duplicates")]:
                if dup_dir.exists():
                    report["removed_empty_dirs"] += _remove_empty_dirs(dup_dir, stop_at=case_folder)
                    try:
                        dup_dir.rmdir()
                        report["removed_empty_dirs"] += 1
                    except OSError:
                        pass
    except Exception as exc:
        report["errors"].append({"path": str(case_folder), "error": f"{type(exc).__name__}: {exc}"})
    return report


def repair_case_folder(
    case_folder: Path,
    *,
    apply: bool = False,
    delete_duplicate: bool = False,
    move_conflicts_with_suffix: bool = False,
    renumber_folders: bool = True,
    hash_existing: bool = True,
    max_files: int = 500,
    max_seconds: int = 300,
) -> dict[str, Any]:
    case_folder = case_folder.expanduser()
    report: dict[str, Any] = {
        "case_folder": str(case_folder),
        "mode": "apply" if apply else "dry_run",
        "delete_duplicate": bool(delete_duplicate),
        "renumber_folders": bool(renumber_folders),
        "hash_existing": bool(hash_existing),
        "alias_folders": [],
        "planned_moves": [],
        "canonical_folder_moves": [],
        "canonical_misfile_moves": [],
        "duplicates": [],
        "conflict_moves": [],
        "conflicts": [],
        "errors": [],
        "removed_empty_dirs": 0,
    }
    if not case_folder.is_dir():
        report["errors"].append({"path": str(case_folder), "error": "case_folder_not_found"})
        return report

    case_category = _case_category_from_path(case_folder)
    canonical_first_segments = _canonical_case_first_segments(case_category)
    existing_first_segments = _existing_case_first_segments(case_folder, case_category=case_category)
    alias_dirs = [
        p for p in sorted(case_folder.iterdir(), key=lambda p: p.name)
        if p.is_dir() and is_noncanonical_drive_folder(p.name, case_context_name=case_folder.name)
    ]
    report["alias_folders"] = [p.name for p in alias_dirs]

    for alias_dir in alias_dirs:
        files, meta = _iter_files(alias_dir, max_files=max_files, max_seconds=max_seconds)
        alias_item = {"name": alias_dir.name, "file_count": len(files), **meta}
        for src in files:
            try:
                inner = src.relative_to(alias_dir).as_posix()
                target_rel = mapped_file_relative_path(
                    alias_dir.name,
                    inner,
                    case_category=case_category,
                    case_context_name=case_folder.name,
                    existing_nas_first_segments=existing_first_segments,
                )
                target = case_folder / target_rel
                _record_file_move(
                    report,
                    src,
                    target,
                    target_rel,
                    apply=apply,
                    delete_duplicate=delete_duplicate,
                    move_conflicts_with_suffix=move_conflicts_with_suffix,
                    hash_existing=hash_existing,
                )
            except Exception as exc:
                report["errors"].append({"source": str(src), "error": f"{type(exc).__name__}: {exc}"})
        report.setdefault("alias_folder_details", []).append(alias_item)
        if apply:
            report["removed_empty_dirs"] += _remove_empty_dirs(alias_dir, stop_at=case_folder)

    if renumber_folders and canonical_first_segments:
        numbered_dirs = [
            p for p in sorted(case_folder.iterdir(), key=lambda p: p.name)
            if (
                p.is_dir()
                and p.name.startswith(CANONICAL_FIRST_SEGMENT_PREFIXES)
                and p.name not in canonical_first_segments
            )
        ]
        report["noncanonical_numbered_folders"] = [p.name for p in numbered_dirs]
        for source_dir in numbered_dirs:
            files, meta = _iter_files(source_dir, max_files=max_files, max_seconds=max_seconds)
            folder_item = {"name": source_dir.name, "file_count": len(files), **meta}
            for src in files:
                try:
                    inner = src.relative_to(source_dir).as_posix()
                    source_rel = PurePosixPath(source_dir.name, inner).as_posix()
                    target_rel = drive_to_nas_relative_path(
                        source_rel,
                        case_category=case_category,
                        case_context_name=case_folder.name,
                        existing_nas_first_segments=existing_first_segments,
                    )
                    target_parts = split_relative_parts(target_rel)
                    if not target_parts or target_parts[0] == source_dir.name:
                        continue
                    if target_parts[0] not in canonical_first_segments:
                        continue
                    target = case_folder / target_rel
                    _record_file_move(
                        report,
                        src,
                        target,
                        target_rel,
                        apply=apply,
                        delete_duplicate=delete_duplicate,
                        move_conflicts_with_suffix=move_conflicts_with_suffix,
                        hash_existing=hash_existing,
                        move_bucket="canonical_folder_moves",
                        conflict_label="資料夾重編差異",
                        reason=f"renumbered_from_{source_dir.name}",
                    )
                except Exception as exc:
                    report["errors"].append({"source": str(src), "error": f"{type(exc).__name__}: {exc}"})
            report.setdefault("noncanonical_numbered_folder_details", []).append(folder_item)
            if apply:
                report["removed_empty_dirs"] += _remove_empty_dirs(source_dir, stop_at=case_folder)

    for judgment_dir in judgment_folder_candidates(case_folder, 10):
        if not judgment_dir.is_dir():
            continue
        files, meta = _iter_files(judgment_dir, max_files=max_files, max_seconds=max_seconds)
        report.setdefault("canonical_misfile_scan", []).append(
            {"folder": judgment_dir.name, "file_count": len(files), **meta}
        )
        for src in files:
            try:
                target_rel = _target_for_misfiled_judgment_file(case_folder, src)
                if not target_rel:
                    continue
                target = case_folder / target_rel
                _record_file_move(
                    report,
                    src,
                    target,
                    target_rel,
                    apply=apply,
                    delete_duplicate=delete_duplicate,
                    move_conflicts_with_suffix=move_conflicts_with_suffix,
                    hash_existing=hash_existing,
                    move_bucket="canonical_misfile_moves",
                    conflict_label="歸檔差異",
                    reason=f"misfiled_in_{judgment_folder_name(10)}",
                )
            except Exception as exc:
                report["errors"].append({"source": str(src), "error": f"{type(exc).__name__}: {exc}"})

    return report


def repair_case_tree(
    root: Path,
    *,
    apply: bool = False,
    delete_duplicate: bool = False,
    move_conflicts_with_suffix: bool = False,
    clean_duplicate_quarantine: bool = False,
    renumber_folders: bool = True,
    hash_existing: bool = True,
    max_cases: int = 500,
    max_files: int = 500,
    max_seconds: int = 600,
) -> dict[str, Any]:
    root = root.expanduser()
    report: dict[str, Any] = {
        "root": str(root),
        "mode": "apply" if apply else "dry_run",
        "case_count": 0,
        "cases": [],
        "summary": {
            "alias_folders": 0,
            "planned_moves": 0,
            "canonical_folder_moves": 0,
            "canonical_misfile_moves": 0,
            "duplicates": 0,
            "conflict_moves": 0,
            "conflicts": 0,
            "safe_quarantine_duplicates": 0,
            "kept_unique_quarantine": 0,
            "errors": 0,
        },
    }
    if not root.is_dir():
        report["summary"]["errors"] += 1
        report["cases"].append({"case_folder": str(root), "errors": [{"error": "root_not_found"}]})
        return report

    case_folders, meta = _iter_case_folders(root, max_cases=max_cases, max_seconds=max_seconds)
    report["case_count"] = len(case_folders)
    report["scan"] = meta
    for case_folder in case_folders:
        item = repair_case_folder(
            case_folder,
            apply=apply,
            delete_duplicate=delete_duplicate,
            move_conflicts_with_suffix=move_conflicts_with_suffix,
            renumber_folders=renumber_folders,
            hash_existing=hash_existing,
            max_files=max_files,
            max_seconds=max_seconds,
        )
        if clean_duplicate_quarantine:
            item["duplicate_quarantine"] = cleanup_duplicate_quarantine(
                case_folder,
                apply=apply and delete_duplicate,
                max_files=max_files,
            )
        if (
            item.get("alias_folders")
            or item.get("planned_moves")
            or item.get("canonical_folder_moves")
            or item.get("canonical_misfile_moves")
            or item.get("duplicates")
            or item.get("conflict_moves")
            or item.get("conflicts")
            or item.get("errors")
            or (item.get("duplicate_quarantine") or {}).get("safe_duplicates")
            or (item.get("duplicate_quarantine") or {}).get("kept_unique")
        ):
            report["cases"].append(item)
        report["summary"]["alias_folders"] += len(item.get("alias_folders") or [])
        report["summary"]["planned_moves"] += len(item.get("planned_moves") or [])
        report["summary"]["canonical_folder_moves"] += len(item.get("canonical_folder_moves") or [])
        report["summary"]["canonical_misfile_moves"] += len(item.get("canonical_misfile_moves") or [])
        report["summary"]["duplicates"] += len(item.get("duplicates") or [])
        report["summary"]["conflict_moves"] += len(item.get("conflict_moves") or [])
        report["summary"]["conflicts"] += len(item.get("conflicts") or [])
        report["summary"]["errors"] += len(item.get("errors") or [])
        dq = item.get("duplicate_quarantine") or {}
        report["summary"]["safe_quarantine_duplicates"] += len(dq.get("safe_duplicates") or [])
        report["summary"]["kept_unique_quarantine"] += len(dq.get("kept_unique") or [])
        report["summary"]["errors"] += len(dq.get("errors") or [])
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
    parser.add_argument("--tree", action="store_true", help="Treat case_folder as a root and scan OSC case folders below it.")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--delete-duplicate", action="store_true", help="Delete exact duplicate files from alias folders after hash match.")
    parser.add_argument("--move-conflicts-with-suffix", action="store_true", help="Move same-name/different-content alias files into canonical folder with a safe suffix instead of leaving alias shell.")
    parser.add_argument("--clean-duplicate-quarantine", action="store_true", help="Clean .duplicates files only when identical content exists outside quarantine.")
    parser.add_argument("--no-renumber-folders", action="store_true", help="Skip category-specific renumbering of existing numbered NAS folders.")
    parser.add_argument("--no-hash-duplicates", action="store_true", help="Do not hash existing target files; report or suffix same-name targets without duplicate detection.")
    parser.add_argument("--max-cases", type=int, default=500)
    parser.add_argument("--max-files", type=int, default=500)
    parser.add_argument("--max-seconds", type=int, default=300)
    parser.add_argument("--json-out", default=str(DEFAULT_REPORT))
    parser.add_argument("--print-json", action="store_true")
    args = parser.parse_args(argv)

    started = time.time()
    if args.tree:
        report = repair_case_tree(
            Path(args.case_folder),
            apply=args.apply,
            delete_duplicate=args.delete_duplicate,
            move_conflicts_with_suffix=args.move_conflicts_with_suffix,
            clean_duplicate_quarantine=args.clean_duplicate_quarantine,
            renumber_folders=not args.no_renumber_folders,
            hash_existing=not args.no_hash_duplicates,
            max_cases=args.max_cases,
            max_files=args.max_files,
            max_seconds=args.max_seconds,
        )
    else:
        report = repair_case_folder(
            Path(args.case_folder),
            apply=args.apply,
            delete_duplicate=args.delete_duplicate,
            move_conflicts_with_suffix=args.move_conflicts_with_suffix,
            renumber_folders=not args.no_renumber_folders,
            hash_existing=not args.no_hash_duplicates,
            max_files=args.max_files,
            max_seconds=args.max_seconds,
        )
    report["ok"] = not (report.get("errors") or []) and not int((report.get("summary") or {}).get("errors") or 0)
    report["elapsed_sec"] = round(time.time() - started, 3)
    _write_report(Path(args.json_out), report)
    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        summary = report.get("summary") or {}
        if summary:
            print(
                "drive-imported-folder-repair: "
                f"alias_folders={summary.get('alias_folders', 0)} "
                f"moves={summary.get('planned_moves', 0)} "
                f"renumbered={summary.get('canonical_folder_moves', 0)} "
                f"canonical_misfiles={summary.get('canonical_misfile_moves', 0)} "
                f"duplicates={summary.get('duplicates', 0)} "
                f"conflict_moves={summary.get('conflict_moves', 0)} "
                f"conflicts={summary.get('conflicts', 0)} "
                f"errors={summary.get('errors', 0)}"
            )
        else:
            print(
                "drive-imported-folder-repair: "
                f"alias_folders={len(report.get('alias_folders') or [])} "
                f"moves={len(report.get('planned_moves') or [])} "
                f"renumbered={len(report.get('canonical_folder_moves') or [])} "
                f"duplicates={len(report.get('duplicates') or [])} "
                f"conflicts={len(report.get('conflicts') or [])} "
                f"errors={len(report.get('errors') or [])}"
            )
        print(f"report={args.json_out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
