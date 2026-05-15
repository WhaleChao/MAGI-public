#!/usr/bin/env python3
"""Fetch and materialize the complete Supreme Court interpreter corpus.

The source of truth is the 812-item list exported from Judicial Yuan search.
Existing clean TXT files are reused through the renumbering map; missing items
are fetched from the public judgment HTML page first, falling back to the same
fetch stack used by MAGI's judicial-web-search skill.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any


MAGI_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEXT_DIR = Path("/Users/ai/Desktop/最高法院_通譯_TXT")
DEFAULT_LIST_PATH = DEFAULT_TEXT_DIR / "最高法院_通譯_812清單.json"
DEFAULT_OUTPUT_DIR = DEFAULT_TEXT_DIR / "完整812"


def safe_title(title: str) -> str:
    value = re.sub(r'[\\/:*?"<>|]+', "_", (title or "").strip())
    value = re.sub(r"\s+", "_", value)
    return value[:150].strip("_") or "judgment"


def clean_judgment_text(text: str) -> str:
    """Normalize line breaks without flattening legal paragraphs."""
    raw_lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    lines: list[str] = []
    for raw in raw_lines:
        line = raw.replace("\u3000", " ").strip()
        line = re.sub(r"[ \t]+", " ", line)
        if not line:
            if lines and lines[-1]:
                lines.append("")
            continue
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines).strip() + "\n"


def load_source_items(list_path: Path) -> list[dict[str, Any]]:
    data = json.loads(list_path.read_text(encoding="utf-8"))
    items = data.get("results") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise RuntimeError(f"source list is not a list: {list_path}")
    return [it for it in items if isinstance(it, dict)]


def _read_mapping_json(text_dir: Path) -> dict[int, Path]:
    path = text_dir / "重新編號對照表.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[int, Path] = {}
    for row in data.get("mapping") or []:
        try:
            auth_idx = int(row.get("old_authoritative_index") or 0)
        except Exception:
            continue
        filename = str(row.get("new_filename") or "").strip()
        candidate = text_dir / filename
        if auth_idx and candidate.exists():
            out[auth_idx] = candidate
    return out


def _read_mapping_csv(text_dir: Path) -> dict[int, Path]:
    path = text_dir / "重新編號對照表.csv"
    if not path.exists():
        return {}
    out: dict[int, Path] = {}
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    auth_idx = int(row.get("old_authoritative_index") or 0)
                except Exception:
                    continue
                current = str(row.get("new_index") or "").zfill(4)
                matches = sorted(text_dir.glob(f"{current}_*.txt"))
                if auth_idx and matches:
                    out[auth_idx] = matches[0]
    except Exception:
        return {}
    return out


def existing_text_by_authoritative_index(text_dir: Path) -> dict[int, Path]:
    out = _read_mapping_json(text_dir) or _read_mapping_csv(text_dir)
    if out:
        return out
    # Fallback: if the folder is already authoritative, use the leading index.
    fallback: dict[int, Path] = {}
    for path in sorted(text_dir.glob("*.txt")):
        match = re.match(r"^(\d{4})_", path.name)
        if match:
            fallback[int(match.group(1))] = path
    return fallback


def load_judicial_web_search_module():
    action_path = MAGI_ROOT / "skills" / "judicial-web-search" / "action.py"
    spec = importlib.util.spec_from_file_location("magi_judicial_web_search_complete_interpreter", action_path)
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot import {action_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def materialize_complete_corpus(
    *,
    source_dir: Path,
    list_path: Path,
    output_dir: Path,
    max_fetch: int,
    force: bool,
    delay_sec: float,
    timeout_sec: int,
) -> dict[str, Any]:
    items = load_source_items(list_path)
    existing = existing_text_by_authoritative_index(source_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    txt_dir = output_dir / "TXT"
    txt_dir.mkdir(parents=True, exist_ok=True)
    jws = load_judicial_web_search_module()

    fetched = 0
    reused = 0
    skipped_existing_output = 0
    written = 0
    failed: list[dict[str, Any]] = []
    mapping: list[dict[str, Any]] = []

    for idx, item in enumerate(items, start=1):
        title = str(item.get("title") or f"最高法院通譯裁判{idx}")
        url = str(item.get("url") or "")
        out_path = txt_dir / f"{idx:04d}_{safe_title(title)}.txt"
        source = ""
        text = ""
        if out_path.exists() and out_path.stat().st_size > 80 and not force:
            skipped_existing_output += 1
            mapping.append({
                "authoritative_index": idx,
                "title": title,
                "url": url,
                "source": "existing_output",
                "txt_file": str(out_path),
            })
            continue

        existing_path = existing.get(idx)
        if existing_path and existing_path.exists() and existing_path.stat().st_size > 80:
            text = existing_path.read_text(encoding="utf-8", errors="replace")
            source = f"reused:{existing_path.name}"
            reused += 1
        else:
            if max_fetch >= 0 and fetched >= max_fetch:
                failed.append({"idx": idx, "title": title, "url": url, "error": "max_fetch_reached"})
                continue
            fetched += 1
            result = jws._fetch_text_impl(url, headless=True, timeout_sec=timeout_sec, max_chars=500000)
            if not result.get("success"):
                failed.append({
                    "idx": idx,
                    "title": title,
                    "url": url,
                    "error": result.get("error"),
                    "engine": result.get("engine"),
                    "recoverable": bool(result.get("recoverable")),
                    "fallback": result.get("fallback"),
                })
                continue
            text_path = Path(str(result.get("text_path") or ""))
            text = text_path.read_text(encoding="utf-8", errors="replace") if text_path.exists() else str(result.get("text_preview") or "")
            source = f"fetched:{result.get('engine') or 'browser'}"
            time.sleep(max(0.0, float(delay_sec)))

        out_path.write_text(clean_judgment_text(text), encoding="utf-8")
        written += 1
        mapping.append({
            "authoritative_index": idx,
            "title": title,
            "url": url,
            "source": source,
            "txt_file": str(out_path),
        })

    report = {
        "success": len(failed) == 0 and len(list(txt_dir.glob("*.txt"))) == len(items),
        "source_list": str(list_path),
        "source_dir": str(source_dir),
        "output_dir": str(output_dir),
        "txt_dir": str(txt_dir),
        "expected": len(items),
        "txt_count": len(list(txt_dir.glob("*.txt"))),
        "reused": reused,
        "fetched": fetched,
        "written": written,
        "skipped_existing_output": skipped_existing_output,
        "failed_count": len(failed),
        "failed": failed,
        "mapping": mapping,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    (output_dir / "完整812抓取報告.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "完整812對照表.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["authoritative_index", "title", "url", "source", "txt_file"])
        writer.writeheader()
        writer.writerows(mapping)
    return report


def sync_summary_outputs(output_dir: Path) -> None:
    """Copy the finished table files to the parent folder for easy discovery."""
    parent = output_dir.parent
    for name in ("最高法院_通譯_分類表.xlsx", "最高法院_通譯_分類表.csv", "最高法院_通譯_分類表.md"):
        src = output_dir / name
        if src.exists():
            shutil.copy2(src, parent / name)


def main() -> int:
    parser = argparse.ArgumentParser(description="補齊最高法院通譯裁判 TXT 並產生完整812資料夾")
    parser.add_argument("--source-dir", default=str(DEFAULT_TEXT_DIR))
    parser.add_argument("--list-path", default=str(DEFAULT_LIST_PATH))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--max-fetch", type=int, default=-1, help="-1 means unlimited")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--delay-sec", type=float, default=0.15)
    parser.add_argument("--timeout-sec", type=int, default=45)
    args = parser.parse_args()

    report = materialize_complete_corpus(
        source_dir=Path(args.source_dir),
        list_path=Path(args.list_path),
        output_dir=Path(args.output_dir),
        max_fetch=int(args.max_fetch),
        force=bool(args.force),
        delay_sec=float(args.delay_sec),
        timeout_sec=int(args.timeout_sec),
    )
    print(json.dumps({k: v for k, v in report.items() if k not in {"mapping", "failed"}}, ensure_ascii=False, indent=2))
    if report["failed_count"]:
        print(json.dumps(report["failed"][:10], ensure_ascii=False, indent=2))
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
