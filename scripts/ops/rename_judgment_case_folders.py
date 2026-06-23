#!/usr/bin/env python3
"""Rename legacy judgment folders inside case trees.

Canonical folder names now spell out that the bucket contains judgments,
terminal rulings, and terminal dispositions.  This repair is intentionally
idempotent and non-destructive: existing canonical folders are merged, duplicate
files are removed only when their content is identical, and conflicting files
are quarantined under the canonical folder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from api.case_path_mapper import preferred_case_roots
from skills.bridge.shared_utils.judgment_folder_names import (
    JUDGMENT_FOLDER_LABEL,
    LEGACY_JUDGMENT_FOLDER_LABEL,
    judgment_folder_name,
    legacy_judgment_folder_name,
)

DEFAULT_REPORT = ROOT / ".runtime" / "judgment_folder_rename_latest.json"
LEGACY_PREFIXES = (3, 4, 8, 9, 10)


def _md5(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.md5()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def canonical_name_for_legacy(name: str) -> str:
    text = str(name or "").strip()
    if text == LEGACY_JUDGMENT_FOLDER_LABEL:
        return JUDGMENT_FOLDER_LABEL
    for prefix in LEGACY_PREFIXES:
        if text == legacy_judgment_folder_name(prefix):
            return judgment_folder_name(prefix)
    return ""


def _same_file(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return _md5(a) == _md5(b)
    except OSError:
        return False


def _unique_conflict_path(conflict_root: Path, relative: Path) -> Path:
    target = conflict_root / relative
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix
    parent = target.parent
    for idx in range(1, 10_000):
        candidate = parent / f"{stem}__conflict_{idx}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot allocate conflict path for {target}")


def _merge_dir(src: Path, dst: Path, *, apply: bool, report: dict[str, Any], base: Path | None = None) -> None:
    base = base or src
    for item in sorted(src.iterdir(), key=lambda p: p.name):
        rel = item.relative_to(base)
        target = dst / rel
        if item.is_dir():
            if not target.exists():
                report["moves"].append({"source": str(item), "target": str(target), "kind": "dir"})
                if apply:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(item), str(target))
                continue
            if target.is_dir():
                _merge_dir(item, dst, apply=apply, report=report, base=base)
                continue
            conflict_root = dst / ".judgment_folder_rename_conflicts" / datetime.now().strftime("%Y%m%d_%H%M%S")
            conflict_target = _unique_conflict_path(conflict_root, rel)
            report["conflicts"].append({"source": str(item), "target": str(target), "quarantine": str(conflict_target)})
            if apply:
                conflict_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(conflict_target))
            continue

        if not target.exists():
            report["moves"].append({"source": str(item), "target": str(target), "kind": "file"})
            if apply:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(target))
            continue
        if target.is_file() and _same_file(item, target):
            report["duplicates_removed"].append({"source": str(item), "target": str(target)})
            if apply:
                item.unlink()
            continue
        conflict_root = dst / ".judgment_folder_rename_conflicts" / datetime.now().strftime("%Y%m%d_%H%M%S")
        conflict_target = _unique_conflict_path(conflict_root, rel)
        report["conflicts"].append({"source": str(item), "target": str(target), "quarantine": str(conflict_target)})
        if apply:
            conflict_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(conflict_target))


def _remove_empty_dirs(path: Path, *, stop_at: Path, apply: bool, report: dict[str, Any]) -> None:
    current = path
    while current != stop_at and current.exists():
        try:
            current.rmdir()
            report["removed_empty_dirs"].append(str(current))
        except OSError:
            break
        current = current.parent


def rename_folder(path: Path, *, apply: bool) -> dict[str, Any]:
    new_name = canonical_name_for_legacy(path.name)
    item_report: dict[str, Any] = {
        "source": str(path),
        "target": str(path.with_name(new_name)) if new_name else "",
        "renamed": False,
        "merged": False,
        "moves": [],
        "duplicates_removed": [],
        "conflicts": [],
        "removed_empty_dirs": [],
        "errors": [],
    }
    if not new_name:
        item_report["errors"].append("not_legacy_judgment_folder")
        return item_report
    target = path.with_name(new_name)
    try:
        if not target.exists():
            item_report["renamed"] = True
            if apply:
                path.rename(target)
            return item_report
        if not target.is_dir():
            item_report["errors"].append("target_exists_not_directory")
            return item_report
        item_report["merged"] = True
        _merge_dir(path, target, apply=apply, report=item_report)
        if apply:
            _remove_empty_dirs(path, stop_at=path.parent, apply=apply, report=item_report)
        return item_report
    except Exception as exc:
        item_report["errors"].append(f"{type(exc).__name__}: {exc}")
        return item_report


def find_legacy_judgment_folders(roots: list[Path]) -> list[Path]:
    legacy_names = {LEGACY_JUDGMENT_FOLDER_LABEL}
    legacy_names.update(legacy_judgment_folder_name(prefix) for prefix in LEGACY_PREFIXES)
    found: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        if shutil.which("find"):
            expr: list[str] = []
            for idx, name in enumerate(sorted(legacy_names)):
                if idx:
                    expr.append("-o")
                expr.extend(["-name", name])
            try:
                proc = subprocess.run(
                    ["find", str(root), "-type", "d", "(", *expr, ")", "-print"],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=900,
                )
                if proc.returncode in {0, 1}:
                    found.extend(Path(line) for line in proc.stdout.splitlines() if line.strip())
                    continue
            except Exception:
                pass
        for dirpath, dirnames, _filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in {"@eaDir", "#recycle"} and not name.startswith(".")]
            for name in list(dirnames):
                if name in legacy_names:
                    found.append(Path(dirpath) / name)
    return sorted(dict.fromkeys(found), key=lambda p: str(p))


def run(roots: list[Path], *, apply: bool) -> dict[str, Any]:
    folders = find_legacy_judgment_folders(roots)
    report = {
        "ok": True,
        "apply": apply,
        "roots": [str(p) for p in roots],
        "legacy_folder_count": len(folders),
        "items": [],
        "errors": [],
    }
    for folder in folders:
        item = rename_folder(folder, apply=apply)
        report["items"].append(item)
        if item["errors"]:
            report["errors"].append({"source": str(folder), "errors": item["errors"]})
    report["ok"] = not report["errors"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", action="append", default=[], help="Case root to scan. Defaults to preferred_case_roots().")
    parser.add_argument("--apply", action="store_true", help="Apply changes. Without this flag, only reports planned moves.")
    parser.add_argument("--json-out", default=str(DEFAULT_REPORT))
    args = parser.parse_args()

    roots = [Path(p).expanduser() for p in args.root] if args.root else [Path(p) for p in preferred_case_roots()]
    report = run(roots, apply=bool(args.apply))
    out = Path(args.json_out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
