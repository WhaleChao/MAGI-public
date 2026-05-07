#!/usr/bin/env python3
"""Repair transcript filenames that were left as 00000000 and quarantine duplicates."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "casper_ecosystem" / "law_firm_orchestrators" / "judicial_automation_v2.py"
DEFAULT_CASE_ROOTS = [
    Path("/Users/ai/Library/CloudStorage/SynologyDrive-homes/01_案件"),
    Path("/Volumes/lumi/lumi/01_案件"),
]


class HashOnlyDownloader:
    def _calculate_file_md5(self, path: str) -> str | None:
        try:
            h = hashlib.md5()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    def _parse_record_pdf(self, path: str) -> dict[str, Any]:
        raise RuntimeError("--no-pdf-parse cannot repair 00000000 filenames")


def _load_downloader():
    spec = importlib.util.spec_from_file_location("magi_judicial_automation_v2_repair", MODULE)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load {MODULE}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CourtRecordDownloader(
        username="",
        password="",
        download_folder=str(ROOT / "筆錄下載"),
        headless=True,
        log_callback=lambda _msg: None,
    )


def _safe_name(name: str) -> str:
    return re.sub(r'[/:*?"<>|]', "_", name)


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    i = 2
    while True:
        candidate = path.with_name(f"{stem}_{i}{suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def _standard_name(name: str) -> bool:
    return bool(re.match(r"^(?!00000000)\d{8}\s+.+\.pdf$", name))


def _strip_collision_suffix(name: str) -> str:
    return re.sub(r"_\d+(\.pdf)$", r"\1", name)


def _collision_rank(path: Path) -> tuple[int, int, str]:
    match = re.search(r"_(\d+)\.pdf$", path.name)
    if not match:
        return (0, 0, path.name)
    return (1, int(match.group(1)), path.name)


def _folder_matches(files: list[str], *, name_contains: str, only_00000000: bool) -> bool:
    pdfs = [f for f in files if f.lower().endswith(".pdf")]
    if not pdfs:
        return False
    if name_contains and not any(name_contains in f for f in pdfs):
        return False
    if only_00000000 and not any(f.startswith("00000000 ") for f in pdfs):
        return False
    return True


def _iter_transcript_folders(
    root: Path,
    limit: int,
    *,
    name_contains: str = "",
    only_00000000: bool = False,
) -> list[Path]:
    folders: list[Path] = []
    if root.is_file():
        return [root.parent]
    if root.is_dir():
        direct_files = [p.name for p in root.iterdir() if p.is_file()]
        if _folder_matches(direct_files, name_contains=name_contains, only_00000000=only_00000000):
            return [root]
    for dirpath, dirnames, files in os.walk(root):
        depth = Path(dirpath).relative_to(root).parts
        if len(depth) > 6:
            dirnames.clear()
            continue
        if "筆錄" in Path(dirpath).name and _folder_matches(
            files,
            name_contains=name_contains,
            only_00000000=only_00000000,
        ):
            folders.append(Path(dirpath))
            if len(folders) >= limit:
                break
    return folders


def _canonical_name(downloader: Any, path: Path) -> str | None:
    parsed = downloader._parse_record_pdf(str(path))
    if not parsed.get("date") or not parsed.get("type"):
        return None
    return _safe_name(downloader._generate_record_filename(parsed, path.name))


def repair_folder(
    folder: Path,
    downloader: Any,
    apply: bool,
    *,
    name_contains: str = "",
    only_00000000: bool = False,
) -> dict[str, Any]:
    pdfs = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
    by_md5: dict[str, list[Path]] = defaultdict(list)
    for p in pdfs:
        md5 = downloader._calculate_file_md5(str(p))
        if md5:
            by_md5[md5].append(p)

    actions: list[dict[str, str]] = []
    quarantine_dir = folder / ".duplicates" / time.strftime("%Y%m%d_%H%M%S")

    for md5, paths in by_md5.items():
        if name_contains and not any(name_contains in p.name for p in paths):
            continue
        if only_00000000 and not any(p.name.startswith("00000000 ") for p in paths):
            continue
        needs_repair = any(p.name.startswith("00000000 ") for p in paths) or len(paths) > 1
        if not needs_repair:
            continue

        has_unknown_date = any(p.name.startswith("00000000 ") for p in paths)
        canonical = None
        if has_unknown_date:
            for p in paths:
                canonical = _canonical_name(downloader, p)
                if canonical:
                    break
        if canonical is None:
            standard_paths = [p for p in paths if _standard_name(p.name)]
            if standard_paths:
                canonical = sorted(standard_paths, key=_collision_rank)[0].name
            else:
                collision_paths = [p for p in paths if _standard_name(_strip_collision_suffix(p.name))]
                if collision_paths:
                    first = sorted(collision_paths, key=_collision_rank)[0]
                    stripped = _strip_collision_suffix(first.name)
                    if not (folder / stripped).exists() or (folder / stripped) in paths:
                        canonical = stripped
                    else:
                        canonical = first.name
        if canonical is None:
            actions.append({"action": "unparsed", "md5": md5, "paths": " | ".join(str(p) for p in paths)})
            continue

        keeper = next((p for p in paths if p.name == canonical), None)
        if keeper is None:
            keeper = paths[0]
            dest = _unique_path(folder / canonical)
            actions.append({"action": "rename", "from": str(keeper), "to": str(dest)})
            if apply:
                keeper.rename(dest)
            keeper = dest

        for p in paths:
            current = folder / p.name
            if current == keeper or not current.exists():
                continue
            dest = _unique_path(quarantine_dir / current.name)
            actions.append({"action": "quarantine_duplicate", "from": str(current), "to": str(dest)})
            if apply:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(current), str(dest))

    return {"folder": str(folder), "actions": actions}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit-folders", type=int, default=200)
    parser.add_argument("--name-contains", default="")
    parser.add_argument("--only-00000000", action="store_true")
    parser.add_argument("--no-pdf-parse", action="store_true")
    parser.add_argument("--json-out", default=str(ROOT / ".runtime" / "repair_transcript_filenames_latest.json"))
    args = parser.parse_args()

    roots = [Path(p).expanduser() for p in args.root] if args.root else [p for p in DEFAULT_CASE_ROOTS if p.exists()]
    downloader = HashOnlyDownloader() if args.no_pdf_parse else _load_downloader()
    report = {"apply": bool(args.apply), "roots": [str(p) for p in roots], "folders": []}
    try:
        for root in roots:
            for folder in _iter_transcript_folders(
                root,
                args.limit_folders,
                name_contains=args.name_contains,
                only_00000000=args.only_00000000,
            ):
                result = repair_folder(
                    folder,
                    downloader,
                    args.apply,
                    name_contains=args.name_contains,
                    only_00000000=args.only_00000000,
                )
                if result["actions"]:
                    report["folders"].append(result)
    finally:
        try:
            close = getattr(downloader, "close", None)
            if close:
                close()
        except Exception:
            pass

    total = sum(len(f["actions"]) for f in report["folders"])
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"apply": bool(args.apply), "folders": len(report["folders"]), "actions": total, "json_out": str(out)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
