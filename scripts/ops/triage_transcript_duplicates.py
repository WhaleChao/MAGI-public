#!/usr/bin/env python3
"""
Triage transcript `_N` filename duplicates safely.

For each `*_N.pdf` (e.g. `20240618 言詞辯論筆錄_2.pdf`):
  1. Compute MD5; find sibling original (same name without `_N` suffix)
  2. Three buckets:
     - SAME_MD5 → move to `<case>/.duplicates/<timestamp>/` (true duplicate)
     - DIFFERENT_MD5 → write to `.runtime/transcript_review_required.jsonl` (lawyer review)
     - ORPHAN (no original) → write to `.runtime/transcript_orphan.jsonl`
     - CLOUD_ONLY (cannot read) → write to `.runtime/transcript_cloud_only.jsonl` (retry next run)

Default is dry-run; `--apply` actually moves files.

Safety red lines:
  - Never `rm` any file
  - Different MD5 = potentially different content; lawyer must review
  - Cloud-only files retried next run, not skipped permanently
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = ROOT / ".runtime"
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CASE_ROOTS = [
    Path("/Users/ai/Library/CloudStorage/SynologyDrive-homes/01_案件"),
    Path("/Volumes/lumi/lumi/01_案件"),
]

REVIEW_LOG = RUNTIME_DIR / "transcript_review_required.jsonl"
ORPHAN_LOG = RUNTIME_DIR / "transcript_orphan.jsonl"
CLOUD_ONLY_LOG = RUNTIME_DIR / "transcript_cloud_only.jsonl"
SUMMARY_OUT = RUNTIME_DIR / "transcript_triage_summary.json"

import re

NUMERIC_SUFFIX_RE = re.compile(r"^(?P<base>.+?)_(?P<n>\d+)(?P<ext>\.pdf)$", re.IGNORECASE)


def _md5(path: Path, *, timeout_chunks: int = 1024) -> str | None:
    """Compute MD5; return None if file unreadable (cloud-only or permission)."""
    try:
        h = hashlib.md5()
        with open(path, "rb") as f:
            for _ in range(timeout_chunks):
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except (OSError, IOError):
        return None


def _content_hash(path: Path) -> str | None:
    """
    Compute hash of PDF text content (excluding metadata like CreationDate).

    Discovery 2026-04-25: 198/200 _N transcripts have identical file size but
    different MD5 — root cause is PDF metadata (CreationDate / ModDate) differs
    each print, but the actual transcript text is identical.

    Returns None on read failure (cloud-only / permission / corrupt).
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return None
    try:
        doc = fitz.open(str(path))
        texts = []
        for page in doc:
            t = page.get_text()
            if t:
                texts.append(t)
        doc.close()
        combined = "".join(texts)
        if not combined.strip():
            return None  # empty extract — fall back to MD5
        return hashlib.sha256(combined.encode("utf-8", errors="replace")).hexdigest()
    except Exception:
        return None


def _find_n_suffix_files(roots: list[Path], max_files: int = 10000, *,
                          max_dirs: int = 3000, max_depth: int = 5) -> list[Path]:
    """
    Walk case roots looking for *_N.pdf files in 筆錄 folders.

    NAS I/O 節流（per CLAUDE.md 守則）：
    - max_dirs 上限避免無限制 walk
    - max_depth 限制（案件結構 3-5 層）
    - 每 50 個目錄 sleep 0.05 避免衝爆 NAS
    - 跳過 .duplicates / .快取 / non-筆錄 子樹
    """
    found: list[Path] = []
    dir_count = 0
    for root in roots:
        if not root.exists():
            continue
        root_depth = len(root.parts)
        for dirpath, dirs, files in os.walk(root):
            dir_count += 1
            if dir_count > max_dirs:
                return found
            if dir_count % 50 == 0:
                time.sleep(0.05)

            depth = len(Path(dirpath).parts) - root_depth
            if depth > max_depth:
                dirs[:] = []  # don't recurse deeper
                continue

            # Skip .duplicates and other hidden / cache
            if "/.duplicates/" in dirpath or "/.快取/" in dirpath or "/.cache/" in dirpath:
                dirs[:] = []
                continue

            # Don't recurse into folders unlikely to have 筆錄
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "node_modules"]

            # Only collect files from 筆錄 folders
            if "筆錄" not in dirpath and "transcript" not in dirpath.lower():
                continue
            for fn in files:
                if NUMERIC_SUFFIX_RE.match(fn):
                    found.append(Path(dirpath) / fn)
                    if len(found) >= max_files:
                        return found
    return found


def _strip_suffix(name: str) -> str:
    m = NUMERIC_SUFFIX_RE.match(name)
    if not m:
        return name
    return f"{m.group('base')}{m.group('ext')}"


def _bucket(n_path: Path) -> tuple[str, dict]:
    """
    Return (bucket, details). Buckets:
      - same_content : PDF text content identical (safe to dedupe; metadata may differ)
      - same_md5     : Byte-identical (rare for re-printed PDFs)
      - different_content : Real different transcripts — needs lawyer review
      - orphan       : No matching base file
      - cloud_only   : Cannot read (cloud sync or permission)
    """
    base_name = _strip_suffix(n_path.name)
    base_path = n_path.with_name(base_name)

    n_md5 = _md5(n_path)
    if n_md5 is None:
        return "cloud_only", {
            "path": str(n_path),
            "reason": "cannot_read_n_file",
        }

    if not base_path.exists():
        return "orphan", {
            "path": str(n_path),
            "expected_base": str(base_path),
            "n_md5": n_md5,
        }

    base_md5 = _md5(base_path)
    if base_md5 is None:
        return "cloud_only", {
            "path": str(n_path),
            "reason": "cannot_read_base_file",
            "base_path": str(base_path),
        }

    if n_md5 == base_md5:
        return "same_md5", {
            "n_path": str(n_path),
            "base_path": str(base_path),
            "md5": n_md5,
        }

    # Same size but different MD5 → likely PDF metadata diff. Compare text content.
    n_size = n_path.stat().st_size
    base_size = base_path.stat().st_size
    if n_size == base_size:
        n_content = _content_hash(n_path)
        base_content = _content_hash(base_path)
        if n_content is not None and base_content is not None and n_content == base_content:
            return "same_content", {
                "n_path": str(n_path),
                "base_path": str(base_path),
                "n_md5": n_md5,
                "base_md5": base_md5,
                "content_hash": n_content,
                "size": n_size,
                "note": "PDF metadata differs but text content identical",
            }
        return "different_content", {
            "n_path": str(n_path),
            "base_path": str(base_path),
            "n_md5": n_md5,
            "base_md5": base_md5,
            "n_content": n_content,
            "base_content": base_content,
            "n_size": n_size,
            "base_size": base_size,
            "size_match": True,
            "content_match": False,
        }

    # Different size = different content for sure
    return "different_content", {
        "n_path": str(n_path),
        "base_path": str(base_path),
        "n_md5": n_md5,
        "base_md5": base_md5,
        "n_size": n_size,
        "base_size": base_size,
        "size_match": False,
    }


def _move_to_duplicates(n_path: Path, ts: int, dry_run: bool) -> str:
    """Move duplicate to <case>/.duplicates/<ts>/<name>."""
    # Find case root: walk up until we find a folder matching YYYY-NNNN-...
    case_root = None
    for parent in n_path.parents:
        if re.match(r"^\d{4}-\d{4}-", parent.name):
            case_root = parent
            break
    if case_root is None:
        case_root = n_path.parent  # fallback to immediate parent

    dup_dir = case_root / ".duplicates" / str(ts)
    dest = dup_dir / n_path.name
    if dry_run:
        return f"WOULD_MOVE: {n_path} → {dest}"
    dup_dir.mkdir(parents=True, exist_ok=True)
    shutil.move(str(n_path), str(dest))
    return f"MOVED: {dest}"


def _append_jsonl(path: Path, record: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually move duplicates (default dry-run)")
    ap.add_argument("--max-files", type=int, default=10000)
    ap.add_argument("--root", action="append", help="case root override (repeatable)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    roots = [Path(r) for r in args.root] if args.root else DEFAULT_CASE_ROOTS

    if not args.quiet:
        print(f"[triage] scanning roots: {[str(r) for r in roots]}", file=sys.stderr)

    files = _find_n_suffix_files(roots, max_files=args.max_files)
    if not args.quiet:
        print(f"[triage] found {len(files)} _N files", file=sys.stderr)

    counts: Counter = Counter()
    moves: list[str] = []
    ts = int(time.time())
    review_count = orphan_count = cloud_count = 0

    for p in files:
        bucket, details = _bucket(p)
        counts[bucket] += 1
        details["bucket"] = bucket
        details["scanned_at"] = ts

        if bucket in ("same_md5", "same_content"):
            moves.append(_move_to_duplicates(p, ts, dry_run=not args.apply))
        elif bucket == "different_content":
            _append_jsonl(REVIEW_LOG, details)
            review_count += 1
        elif bucket == "orphan":
            _append_jsonl(ORPHAN_LOG, details)
            orphan_count += 1
        elif bucket == "cloud_only":
            _append_jsonl(CLOUD_ONLY_LOG, details)
            cloud_count += 1

    summary = {
        "ts": ts,
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "dry_run": not args.apply,
        "total_n_files": len(files),
        "buckets": dict(counts),
        "review_required_path": str(REVIEW_LOG),
        "orphan_path": str(ORPHAN_LOG),
        "cloud_only_path": str(CLOUD_ONLY_LOG),
        "moves_sample": moves[:5],
    }
    SUMMARY_OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.quiet:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
