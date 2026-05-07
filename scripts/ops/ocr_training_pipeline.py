#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Incrementally collect OCR silver data and append OCR-specific distill pairs.

This is intentionally separate from the legal-summary Gemma distill corpus.
OCR field extraction is a strict-JSON task; mixing it into the general legal
analysis corpus would teach the base assistant conflicting output shapes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.case_path_mapper import preferred_case_roots  # noqa: E402
from scripts.ops import build_ocr_training_dataset as builder  # noqa: E402

OUTPUT_ROOT = ROOT / "data" / "ocr_training" / "auto"
DEFAULT_DISTILL_DIR = Path.home() / ".omlx" / "training" / "ocr-field-distill"

DATE_PREFIX_RE = re.compile(r"^20\d{6}")
COURT_DOC_HINT_RE = re.compile(
    r"(法院|地院|高分院|最高法院|年度.+字第.+號|判決|裁定|通知書|執行處函|民事庭函|刑事庭通知|規費繳款單)"
)
SKIP_DIR_NAMES = {
    ".git",
    ".cache",
    ".claude",
    ".duplicates",
    ".Trash",
    "__pycache__",
    "node_modules",
    "site-packages",
    "venv",
}
SKIP_PATH_HINTS = (
    "/04_證據資料/",
    "/07_證據資料/",
    "/05_筆錄/",
    "/09_信件往返/",
    "/00_委任狀/",
)
SKIP_NAME_HINTS = (
    "無償委任",
    "已簽名",
    "信件",
    "議程",
    "附件",
    "存底",
    "理賠審核通知書",
)


@dataclass
class PipelineStats:
    candidates: int = 0
    scanned: int = 0
    silver: int = 0
    needs_labeling: int = 0
    rejected: int = 0
    errors: int = 0
    distill_appended: int = 0
    distill_duplicates: int = 0


def _path_allowed(path: Path, *, cutoff_ts: float | None = None) -> bool:
    if path.suffix.lower() != ".pdf" or path.name.startswith("."):
        return False
    if not DATE_PREFIX_RE.match(path.name):
        return False
    if not COURT_DOC_HINT_RE.search(path.name):
        return False
    path_text = str(path)
    if any(hint in path_text for hint in SKIP_PATH_HINTS):
        return False
    if any(hint in path.name for hint in SKIP_NAME_HINTS):
        return False
    if any(part in SKIP_DIR_NAMES or part.startswith(".") for part in path.parts):
        return False
    if cutoff_ts is not None:
        try:
            if path.stat().st_mtime < cutoff_ts:
                return False
        except OSError:
            return False
    return True


def collect_candidates(
    roots: Iterable[Path],
    *,
    limit: int,
    max_dirs: int,
    max_depth: int,
    recent_days: int,
) -> list[Path]:
    cutoff_ts = time.time() - recent_days * 86400 if recent_days > 0 else None
    out: list[Path] = []
    visited_dirs = 0
    seen: set[str] = set()

    for root in roots:
        root = Path(root).expanduser()
        if not root.exists():
            continue
        root_depth = len(root.parts)
        for cur, dirs, files in os.walk(root):
            visited_dirs += 1
            if visited_dirs > max_dirs or len(out) >= limit:
                return out

            cur_path = Path(cur)
            depth = max(0, len(cur_path.parts) - root_depth)
            if depth >= max_depth:
                dirs[:] = []
            else:
                dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".")]

            for name in sorted(files):
                if len(out) >= limit:
                    return out
                path = cur_path / name
                if not _path_allowed(path, cutoff_ts=cutoff_ts):
                    continue
                key = str(path)
                if key in seen:
                    continue
                seen.add(key)
                out.append(path)

            if visited_dirs % 50 == 0:
                time.sleep(0.05)

    return out


def write_candidate_list(paths: list[Path], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = out_dir / "candidates.txt"
    candidate_path.write_text("\n".join(str(p) for p in paths) + ("\n" if paths else ""), encoding="utf-8")
    return candidate_path


def _record_hash(record: dict) -> str:
    payload = {
        "sha256_head": record.get("sha256_head"),
        "filename_fields": record.get("filename_fields"),
        "messages": record.get("training_messages"),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"seen_hashes": [], "total_collected": 0, "last_collected_at": None}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = list(dict.fromkeys(state.get("seen_hashes", [])))
    if len(seen) > 50000:
        seen = seen[-45000:]
    state["seen_hashes"] = seen
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def append_silver_to_ocr_distill(silver_path: Path, distill_dir: Path, *, source_manifest: Path | None = None) -> dict:
    distill_dir.mkdir(parents=True, exist_ok=True)
    raw_path = distill_dir / "raw_pairs.jsonl"
    state_path = distill_dir / "collector_state.json"
    state = _load_state(state_path)
    seen = set(state.get("seen_hashes", []))
    appended = 0
    duplicates = 0

    with Path(silver_path).open("r", encoding="utf-8") as src, raw_path.open("a", encoding="utf-8") as dst:
        for line in src:
            if not line.strip():
                continue
            record = json.loads(line)
            messages = record.get("training_messages") or []
            if len(messages) < 3:
                continue
            h = _record_hash(record)
            if h in seen:
                duplicates += 1
                continue
            seen.add(h)
            out = {
                "messages": messages,
                "metadata": {
                    "source": "magi_ocr_field_silver",
                    "filename": record.get("filename"),
                    "pdf_path": record.get("pdf_path"),
                    "sha256_head": record.get("sha256_head"),
                    "support_score": record.get("support_score"),
                    "best_quality": record.get("best_quality"),
                    "dataset_manifest": str(source_manifest) if source_manifest else "",
                    "content_hash": f"sha256:{h}",
                    "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                },
            }
            dst.write(json.dumps(out, ensure_ascii=False) + "\n")
            appended += 1

    state["seen_hashes"] = list(seen)
    state["total_collected"] = int(state.get("total_collected", 0)) + appended
    if appended:
        state["last_collected_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    _save_state(state_path, state)
    return {
        "raw_path": str(raw_path),
        "state_path": str(state_path),
        "appended": appended,
        "duplicates": duplicates,
        "total_collected": state["total_collected"],
    }


def run_pipeline(args: argparse.Namespace) -> dict:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir or (OUTPUT_ROOT / ts)).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)

    roots = [Path(p).expanduser() for p in args.root] if args.root else [Path(p) for p in preferred_case_roots(include_closed=False)]
    candidates = collect_candidates(
        roots,
        limit=args.candidate_limit,
        max_dirs=args.max_dirs,
        max_depth=args.max_depth,
        recent_days=args.recent_days,
    )
    candidate_list = write_candidate_list(candidates, out_dir)

    if args.dry_run or not candidates:
        manifest = {
            "ok": bool(candidates),
            "dry_run": bool(args.dry_run),
            "candidates": len(candidates),
            "candidate_list": str(candidate_list),
            "output_dir": str(out_dir),
        }
        (out_dir / "pipeline_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest

    build_args = argparse.Namespace(
        roots=[],
        candidate_list=str(candidate_list),
        output_dir=str(out_dir),
        max_candidates=args.max_candidates,
        max_silver=args.max_silver,
        max_labeling=args.max_labeling,
        pages=args.pages,
        text_limit=args.text_limit,
        vision=not args.no_vision,
        vision_timeout=args.vision_timeout,
        per_file_timeout=args.per_file_timeout,
        min_support_score=args.min_support_score,
        min_quality=args.min_quality,
    )
    build_stats = builder.build_dataset(build_args)
    manifest_path = out_dir / "manifest.json"
    silver_path = out_dir / "silver_ocr_field_training.jsonl"
    distill_stats = append_silver_to_ocr_distill(silver_path, Path(args.distill_dir).expanduser(), source_manifest=manifest_path)

    manifest = {
        "ok": True,
        "candidate_list": str(candidate_list),
        "candidate_count": len(candidates),
        "build": build_stats,
        "distill": distill_stats,
        "output_dir": str(out_dir),
        "finished_at": datetime.now().isoformat(),
    }
    (out_dir / "pipeline_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    latest = OUTPUT_ROOT / "latest_pipeline_manifest.json"
    latest.parent.mkdir(parents=True, exist_ok=True)
    latest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect MAGI OCR silver data and append OCR-specific distill pairs.")
    parser.add_argument("--root", action="append", default=[], help="Case root to scan; repeatable. Defaults to preferred open case roots.")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--distill-dir", default=str(DEFAULT_DISTILL_DIR))
    parser.add_argument("--candidate-limit", type=int, default=40)
    parser.add_argument("--max-candidates", type=int, default=40)
    parser.add_argument("--max-silver", type=int, default=24)
    parser.add_argument("--max-labeling", type=int, default=16)
    parser.add_argument("--recent-days", type=int, default=45)
    parser.add_argument("--max-dirs", type=int, default=1500)
    parser.add_argument("--max-depth", type=int, default=6)
    parser.add_argument("--pages", default="0")
    parser.add_argument("--text-limit", type=int, default=2400)
    parser.add_argument("--no-vision", action="store_true")
    parser.add_argument("--vision-timeout", type=float, default=15.0)
    parser.add_argument("--per-file-timeout", type=float, default=25.0)
    parser.add_argument("--min-support-score", type=float, default=0.58)
    parser.add_argument("--min-quality", type=float, default=0.22)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    stats = run_pipeline(args)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
