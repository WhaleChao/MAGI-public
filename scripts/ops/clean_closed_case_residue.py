#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Find and optionally merge active OSC case folders that already exist in the archive.

This is intentionally conservative:
- dry-run by default;
- only scans expected case-folder depths;
- only merges when an active folder has an exact archived folder name, or a unique
  OSC case-number match such as ``2025-0051``;
- verifies copied files by sha256 before removing the active residue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_ACTIVE_ROOTS = (
    Path("~/Library/CloudStorage/SynologyDrive-homes/01_案件").expanduser(),
)
DEFAULT_ARCHIVE_ROOTS = (
    Path("/Volumes/lumi/lumi/03_工作資料/10_結案"),
)
CASE_ID_RE = re.compile(r"^(\d{4}-\d{4})(?:-|$)")
SKIP_NAMES = {".DS_Store", ".gitkeep"}


@dataclass(frozen=True)
class CaseDir:
    path: Path
    root: Path
    rel: Path
    case_id: str


def _is_skip_file(path: Path) -> bool:
    name = path.name
    return name in SKIP_NAMES or name.startswith("._")


def _case_id(name: str) -> str:
    match = CASE_ID_RE.match(name)
    return match.group(1) if match else ""


def _iter_dirs(path: Path) -> Iterable[Path]:
    try:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        yield Path(entry.path)
                except OSError:
                    continue
    except OSError:
        return


def _iter_case_dirs(root: Path) -> Iterable[CaseDir]:
    if not root.exists():
        return
    # Expected layout: root / case_category / case_type / case_folder.
    for category in _iter_dirs(root):
        for case_type in _iter_dirs(category):
            for case_dir in _iter_dirs(case_type):
                cid = _case_id(case_dir.name)
                if cid:
                    yield CaseDir(path=case_dir, root=root, rel=case_dir.relative_to(root), case_id=cid)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _conflict_path(path: Path, stamp: str) -> Path:
    suffix = path.suffix
    stem = path.name[: -len(suffix)] if suffix else path.name
    candidate = path.with_name(f"{stem}__active_residue_{stamp}{suffix}")
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{stem}__active_residue_{stamp}_{index}{suffix}")
        index += 1
    return candidate


def _copy_file_verified(source_file: Path, target_file: Path, *, strict_hash: bool = False, bwlimit_mbps: float = 0.0) -> None:
    target_file.parent.mkdir(parents=True, exist_ok=True)
    if bwlimit_mbps > 0:
        chunk_size = 1024 * 1024
        bytes_per_sec = max(1.0, bwlimit_mbps * 1024 * 1024)
        with source_file.open("rb") as src_fh, target_file.open("wb") as dst_fh:
            while True:
                started = time.monotonic()
                chunk = src_fh.read(chunk_size)
                if not chunk:
                    break
                dst_fh.write(chunk)
                elapsed = time.monotonic() - started
                target_elapsed = len(chunk) / bytes_per_sec
                if target_elapsed > elapsed:
                    time.sleep(target_elapsed - elapsed)
    else:
        shutil.copyfile(source_file, target_file)
    try:
        st = source_file.stat()
        os.utime(target_file, (st.st_atime, st.st_mtime))
    except OSError:
        pass
    same_size = source_file.stat().st_size == target_file.stat().st_size
    same_hash = (not strict_hash) or _sha256(source_file) == _sha256(target_file)
    if not same_size or not same_hash:
        raise RuntimeError(f"copy verification failed: {source_file} -> {target_file}")


def _tree_file_count(path: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for filename in filenames:
            if not _is_skip_file(Path(dirpath) / filename):
                total += 1
    return total


def _plan_merge(src: Path, dst: Path, *, verify_existing: bool = False) -> dict:
    result = {
        "source": str(src),
        "target": str(dst),
        "copied": [],
        "duplicates": [],
        "conflicts": [],
        "skipped": [],
        "source_files": 0,
    }
    for dirpath, _, filenames in os.walk(src):
        rel_dir = Path(dirpath).relative_to(src)
        for filename in filenames:
            source_file = Path(dirpath) / filename
            if _is_skip_file(source_file):
                result["skipped"].append(str(source_file.relative_to(src)))
                continue
            result["source_files"] += 1
            rel_file = rel_dir / filename
            target_file = dst / rel_file
            if not target_file.exists():
                result["copied"].append(str(rel_file))
                continue
            try:
                same_size = source_file.stat().st_size == target_file.stat().st_size
                same = same_size and (not verify_existing or _sha256(source_file) == _sha256(target_file))
            except OSError:
                same = False
            if same:
                result["duplicates"].append(str(rel_file))
            else:
                result["conflicts"].append(str(rel_file))
    return result


def _apply_merge(src: Path, dst: Path, *, strict_hash: bool = False, bwlimit_mbps: float = 0.0) -> dict:
    result = _plan_merge(src, dst, verify_existing=strict_hash)
    stamp = time.strftime("%Y%m%d%H%M%S")
    for rel_name in result["copied"]:
        source_file = src / rel_name
        target_file = dst / rel_name
        _copy_file_verified(source_file, target_file, strict_hash=strict_hash, bwlimit_mbps=bwlimit_mbps)
    for rel_name in result["conflicts"]:
        source_file = src / rel_name
        target_file = _conflict_path(dst / rel_name, stamp)
        _copy_file_verified(source_file, target_file, strict_hash=strict_hash, bwlimit_mbps=bwlimit_mbps)
    shutil.rmtree(src)
    result["removed_source"] = True
    return result


def _build_archive_maps(roots: list[Path]) -> tuple[dict[str, CaseDir], dict[str, list[CaseDir]]]:
    by_name: dict[str, CaseDir] = {}
    by_case_id: dict[str, list[CaseDir]] = {}
    for root in roots:
        for case_dir in _iter_case_dirs(root):
            by_name.setdefault(case_dir.path.name, case_dir)
            by_case_id.setdefault(case_dir.case_id, []).append(case_dir)
    return by_name, by_case_id


def _find_matches(active_roots: list[Path], archive_roots: list[Path], case_id_filter: str = "") -> list[tuple[CaseDir, CaseDir, str]]:
    by_name, by_case_id = _build_archive_maps(archive_roots)
    matches: list[tuple[CaseDir, CaseDir, str]] = []
    for root in active_roots:
        for active in _iter_case_dirs(root):
            if case_id_filter and active.case_id != case_id_filter:
                continue
            archived = by_name.get(active.path.name)
            reason = "exact_name"
            if archived is None:
                candidates = by_case_id.get(active.case_id, [])
                if len(candidates) == 1:
                    archived = candidates[0]
                    reason = "unique_case_id"
            if archived is None:
                continue
            try:
                active.path.relative_to(archived.root)
                continue
            except ValueError:
                pass
            matches.append((active, archived, reason))
    return matches


def _parse_roots(values: list[str] | None, defaults: tuple[Path, ...]) -> list[Path]:
    if values:
        return [Path(v).expanduser() for v in values]
    seen: set[str] = set()
    roots: list[Path] = []
    for root in defaults:
        key = str(root)
        if key not in seen:
            roots.append(root)
            seen.add(key)
    return roots


def main() -> int:
    parser = argparse.ArgumentParser(description="Clean active OSC case folders already merged into the closed archive.")
    parser.add_argument("--active-root", action="append", help="Active 01_案件 root; repeatable.")
    parser.add_argument("--archive-root", action="append", help="Closed archive root; repeatable.")
    parser.add_argument("--case-id", help="Limit to an OSC case id like 2025-0051.")
    parser.add_argument("--list-only", action="store_true", help="List matched source/target folders without recursively scanning files.")
    parser.add_argument("--apply", action="store_true", help="Actually copy missing files and remove the active residue.")
    parser.add_argument("--strict-hash", action="store_true", help="Hash source and target files before removing the source. Slower on NAS.")
    parser.add_argument("--bwlimit-mbps", type=float, default=0.0, help="Throttle writes while applying, in MiB/s.")
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    args = parser.parse_args()

    active_roots = _parse_roots(args.active_root, DEFAULT_ACTIVE_ROOTS)
    archive_roots = _parse_roots(args.archive_root, DEFAULT_ARCHIVE_ROOTS)
    matches = _find_matches(active_roots, archive_roots, args.case_id or "")
    report = {
        "mode": "apply" if args.apply else "dry_run",
        "active_roots": [str(p) for p in active_roots if p.exists()],
        "archive_roots": [str(p) for p in archive_roots if p.exists()],
        "matches": [],
    }
    for active, archived, reason in matches:
        if args.list_only:
            plan = {
                "source": str(active.path),
                "target": str(archived.path),
                "copied": [],
                "duplicates": [],
                "conflicts": [],
                "skipped": [],
                "source_files": None,
            }
        else:
            plan = (
                _apply_merge(active.path, archived.path, strict_hash=args.strict_hash, bwlimit_mbps=max(0.0, args.bwlimit_mbps))
                if args.apply
                else _plan_merge(active.path, archived.path)
            )
        plan.update(
            {
                "case_id": active.case_id,
                "match_reason": reason,
                "active_file_count_before": (
                    None
                    if args.list_only
                    else (_tree_file_count(active.path) if active.path.exists() else plan.get("source_files", 0))
                ),
            }
        )
        report["matches"].append(plan)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"mode={report['mode']} matches={len(report['matches'])}")
        for item in report["matches"]:
            print(
                f"- {item['case_id']} {item['match_reason']} "
                f"copy={len(item['copied'])} dup={len(item['duplicates'])} "
                f"conflict={len(item['conflicts'])} skipped={len(item['skipped'])}"
            )
            print(f"  src={item['source']}")
            print(f"  dst={item['target']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
