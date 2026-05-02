#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build a first OCR training dataset for MAGI.

This script intentionally creates SILVER data, not human gold data. A sample is
accepted only when the already-curated filename yields legal fields and at least
one OCR/text source supports those fields. Hard samples are preserved for later
manual labeling instead of being smuggled into training data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import fitz  # PyMuPDF
except Exception as exc:  # pragma: no cover
    raise SystemExit("PyMuPDF is required: %s" % exc)

from api.case_path_mapper import preferred_case_roots
from skills.bridge.shared_utils.case_number_utils import extract_case_number
from skills.bridge.shared_utils.court_utils import extract_court_name
from skills.engine.ocr.legal_entities import extract_entities
from skills.engine.ocr.quality import compute_quality_score


DATE_PREFIX_RE = re.compile(r"^(20\d{6})(?:[\s_]+)?")
LOOSE_CASE_RE = re.compile(r"(\d{2,3})年度([^\s\d（）()；;，,。]{1,8}?字)第(\d{1,6})號?")
PARTY_RE = re.compile(r"[（(]([^（）()]+)[）)]")
VISION_BIN = Path.home() / "Library/Application Support/MAGI/bin/vision_ocr"
DEFAULT_OUTPUT_ROOT = ROOT / "data" / "ocr_training"
SKIP_DIR_NAMES = {
    ".git",
    ".cache",
    ".claude",
    "venv",
    "__pycache__",
    "node_modules",
    "site-packages",
    ".backup",
    ".duplicates",
}
SKIP_PATH_PARTS = SKIP_DIR_NAMES | {".Trash", ".DS_Store"}
COURT_ALIASES = (
    ("花蓮地院", "臺灣花蓮地方法院"),
    ("花蓮地方法院", "臺灣花蓮地方法院"),
    ("花蓮高分院", "臺灣高等法院花蓮分院"),
    ("高等法院花蓮分院", "臺灣高等法院花蓮分院"),
    ("最高法院", "最高法院"),
    ("高雄高等行政法院", "高雄高等行政法院"),
    ("臺北高等行政法院", "臺北高等行政法院"),
    ("台北高等行政法院", "臺北高等行政法院"),
)


@dataclass
class FilenameFields:
    date: str = ""
    court: str = ""
    case_number: str = ""
    doc_type: str = ""
    party: str = ""

    def usable(self) -> bool:
        return bool(self.date and (self.case_number or self.court) and self.doc_type)


@dataclass
class SourceResult:
    source: str
    text: str = ""
    quality: float = 0.0
    entities: Dict[str, object] = field(default_factory=dict)
    error: str = ""


def _sha256_file(path: Path, max_bytes: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        remaining = max_bytes
        while remaining > 0:
            chunk = fh.read(min(65536, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest()


def _clean_text(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + " ..."


def parse_filename_fields(filename: str) -> FilenameFields:
    stem = Path(filename).stem
    m = DATE_PREFIX_RE.match(stem)
    date = m.group(1) if m else ""
    rest = stem[m.end():].strip() if m else stem.strip()

    party = ""
    pm = PARTY_RE.search(rest)
    if pm:
        party = pm.group(1).split("；", 1)[0].split(";", 1)[0].strip()
        rest_no_party = (rest[: pm.start()] + rest[pm.end():]).strip()
    else:
        rest_no_party = rest

    court_alias = ""
    court = extract_court_name(rest_no_party) or ""
    court_aliases_in_name = []
    compact = re.sub(r"\s+", "", rest_no_party)
    for needle, _normalized in COURT_ALIASES:
        if needle in compact:
            court_aliases_in_name.append(needle)
    if not court:
        for needle, normalized in COURT_ALIASES:
            if needle in compact:
                court_alias = needle
                court = normalized
                break

    if not party or "聲請閱卷" in party or "線上聲請" in party:
        case_party = re.search(r"《([^》]+?)案》", rest_no_party)
        if case_party:
            party = case_party.group(1).strip()

    case_number = extract_case_number(rest_no_party) or ""
    loose_case_text = ""
    if not case_number:
        cm = LOOSE_CASE_RE.search(rest_no_party)
        if cm:
            loose_case_text = cm.group(0)
            case_number = "%s年度%s第%s號" % (cm.group(1), cm.group(2), cm.group(3))

    doc_type = rest_no_party
    for token in (court, court_alias, loose_case_text, case_number) + tuple(court_aliases_in_name):
        if token:
            doc_type = doc_type.replace(token, "")
    doc_type = re.sub(r"《[^》]+?案》", "", doc_type)
    doc_type = re.sub(r"^[\s_、，,.-]+|[\s_、，,.-]+$", "", doc_type)
    doc_type = re.sub(r"\s+", "", doc_type)

    # Keep the learnable target compact; summaries in filenames are metadata, not
    # OCR truth for a text-correction model.
    if "；" in doc_type:
        doc_type = doc_type.split("；", 1)[0]
    if ";" in doc_type:
        doc_type = doc_type.split(";", 1)[0]
    doc_type = doc_type[:40]

    return FilenameFields(date=date, court=court, case_number=case_number, doc_type=doc_type, party=party)


def _candidate_allowed(path: Path) -> bool:
    if path.name.startswith(".") or path.suffix.lower() != ".pdf":
        return False
    return not any(part in SKIP_PATH_PARTS or part.startswith(".") for part in path.parts)


def _native_text(doc: "fitz.Document", page_nums: List[int], limit: int) -> SourceResult:
    chunks = []
    try:
        for page_num in page_nums:
            if page_num >= doc.page_count:
                continue
            chunks.append(doc[page_num].get_text() or "")
        text = _clean_text("\n".join(chunks), limit)
        ents = extract_entities(text)
        return SourceResult("native_text", text, compute_quality_score(text), asdict(ents))
    except Exception as exc:
        return SourceResult("native_text", error="%s: %s" % (type(exc).__name__, exc))


def _vision_text(pdf_path: Path, page_nums: List[int], limit: int, timeout_sec: float) -> SourceResult:
    if not VISION_BIN.exists():
        return SourceResult("macos_vision", error="vision_ocr binary missing")
    chunks = []
    errors = []
    for page_num in page_nums:
        try:
            completed = subprocess.run(
                [str(VISION_BIN), str(pdf_path), str(page_num)],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            if completed.returncode == 0 and completed.stdout.strip():
                chunks.append(completed.stdout.strip())
            elif completed.stderr.strip():
                errors.append(completed.stderr.strip()[-240:])
        except subprocess.TimeoutExpired:
            errors.append("page %d timeout" % page_num)
        except Exception as exc:
            errors.append("%s: %s" % (type(exc).__name__, exc))
    text = _clean_text("\n".join(chunks), limit)
    ents = extract_entities(text)
    return SourceResult("macos_vision", text, compute_quality_score(text), asdict(ents), "; ".join(errors[:3]))


def _process_pdf_direct(
    pdf_path: Path,
    page_nums: List[int],
    text_limit: int,
    run_vision: bool,
    vision_timeout: float,
) -> Dict[str, object]:
    sha256_head = _sha256_file(pdf_path)
    doc = fitz.open(str(pdf_path))
    try:
        if doc.needs_pass:
            try:
                doc.authenticate("3800")
            except Exception:
                pass
        page_count = doc.page_count
        pages = [p for p in page_nums if p < page_count]
        sources = [_native_text(doc, pages, text_limit)]
        if run_vision:
            sources.append(_vision_text(pdf_path, pages, text_limit, vision_timeout))
        return {
            "ok": True,
            "sha256_head": sha256_head,
            "page_count": page_count,
            "sources": [asdict(s) for s in sources],
        }
    finally:
        doc.close()


def _process_pdf_with_timeout(
    pdf_path: Path,
    page_nums: List[int],
    text_limit: int,
    run_vision: bool,
    vision_timeout: float,
    per_file_timeout: float,
) -> Dict[str, object]:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--process-one",
        str(pdf_path),
        "--pages",
        ",".join(str(p) for p in page_nums),
        "--text-limit",
        str(text_limit),
        "--vision-timeout",
        str(vision_timeout),
    ]
    if run_vision:
        cmd.append("--vision")
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, timeout=per_file_timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "TimeoutExpired: per-file timeout %.1fs" % per_file_timeout}
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-400:]
        return {"ok": False, "error": "WorkerExit: exitcode %s %s" % (completed.returncode, detail)}
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": "JSONDecodeError: %s" % exc}


def _support_score(fields: FilenameFields, sources: List[SourceResult]) -> Tuple[float, List[str]]:
    support = []
    joined = "\n".join(s.text for s in sources if s.text)
    joined_compact = re.sub(r"\s+", "", joined)

    if fields.case_number and fields.case_number in joined_compact:
        support.append("case_number_exact")
    elif fields.case_number:
        normalized_case = re.sub(r"\D", "", fields.case_number)
        if normalized_case and normalized_case in re.sub(r"\D", "", joined_compact):
            support.append("case_number_numeric")

    if fields.court and fields.court in joined_compact:
        support.append("court_exact")
    elif fields.court:
        short_court = fields.court.replace("臺灣", "").replace("台灣", "")
        if short_court and short_court in joined_compact:
            support.append("court_partial")

    if fields.party and fields.party in joined_compact:
        support.append("party_exact")

    if fields.doc_type and fields.doc_type[:4] in joined_compact:
        support.append("doc_type_hint")

    quality = max((s.quality for s in sources), default=0.0)
    base = min(quality, 1.0) * 0.35
    evidence = 0.0
    for item in support:
        if item.startswith("case_number"):
            evidence += 0.30
        elif item.startswith("court"):
            evidence += 0.20
        elif item.startswith("party"):
            evidence += 0.10
        elif item.startswith("doc_type"):
            evidence += 0.05
    return min(1.0, round(base + evidence, 4)), support


def _training_messages(fields: FilenameFields, sources: List[SourceResult], filename: str) -> List[Dict[str, str]]:
    source_blocks = []
    for source in sources:
        if not source.text:
            continue
        source_blocks.append("[%s quality=%.3f]\n%s" % (source.source, source.quality, source.text))
    user = (
        "以下是同一份台灣法律 PDF 的多個 OCR 來源。請只做 OCR 後校正與欄位抽取，"
        "不得摘要、不得法律分析、不得補不存在的內容。\n\n"
        "原始檔名：%s\n\n%s"
    ) % (filename, "\n\n".join(source_blocks))
    assistant = json.dumps(asdict(fields), ensure_ascii=False, sort_keys=True)
    return [
        {
            "role": "system",
            "content": "你是繁體中文法律文件 OCR 校正器。只輸出嚴格 JSON，欄位為 date,court,case_number,doc_type,party。",
        },
        {"role": "user", "content": user},
        {"role": "assistant", "content": assistant},
    ]


def _iter_listed_pdfs(candidate_list: Path, max_candidates: int) -> Iterable[Path]:
    yielded = 0
    with candidate_list.open("r", encoding="utf-8") as fh:
        for raw in fh:
            if yielded >= max_candidates:
                return
            path = Path(raw.strip()).expanduser()
            if not _candidate_allowed(path) or not path.exists():
                continue
            yielded += 1
            yield path


def _iter_pdfs(roots: Iterable[Path], max_candidates: int) -> Iterable[Path]:
    yielded = 0
    for root in roots:
        if not root.exists():
            continue
        for cur, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES and not d.startswith(".")]
            for name in sorted(files):
                if not name.lower().endswith(".pdf") or name.startswith("."):
                    continue
                path = Path(cur) / name
                if not _candidate_allowed(path):
                    continue
                yielded += 1
                yield path
                if yielded >= max_candidates:
                    return


def build_dataset(args: argparse.Namespace) -> Dict[str, object]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir or (DEFAULT_OUTPUT_ROOT / ts))
    out_dir.mkdir(parents=True, exist_ok=True)

    silver_path = out_dir / "silver_ocr_field_training.jsonl"
    label_path = out_dir / "needs_labeling.jsonl"
    reject_path = out_dir / "rejected.jsonl"
    manifest_path = out_dir / "manifest.json"

    candidate_list = Path(args.candidate_list).expanduser() if args.candidate_list else None
    if args.roots:
        roots = [Path(p).expanduser() for p in args.roots]
    else:
        roots = [Path(p) for p in preferred_case_roots(include_closed=True)]
        roots.extend([ROOT / "閱卷下載", ROOT / "筆錄下載"])

    page_nums = [int(p) for p in str(args.pages).split(",") if str(p).strip().isdigit()]
    page_nums = page_nums or [0]

    stats = {
        "scanned": 0,
        "silver": 0,
        "needs_labeling": 0,
        "rejected": 0,
        "errors": 0,
        "started_at": ts,
        "roots": [str(p) for p in roots],
        "candidate_list": str(candidate_list) if candidate_list else "",
        "page_nums": page_nums,
        "min_support_score": args.min_support_score,
        "min_quality": args.min_quality,
    }

    with silver_path.open("w", encoding="utf-8") as silver_fh, \
            label_path.open("w", encoding="utf-8") as label_fh, \
            reject_path.open("w", encoding="utf-8") as reject_fh:
        pdf_iter = _iter_listed_pdfs(candidate_list, args.max_candidates) if candidate_list else _iter_pdfs(roots, args.max_candidates)
        for pdf_path in pdf_iter:
            if stats["silver"] >= args.max_silver and stats["needs_labeling"] >= args.max_labeling:
                break
            stats["scanned"] += 1
            fields = parse_filename_fields(pdf_path.name)
            record_base = {
                "pdf_path": str(pdf_path),
                "filename": pdf_path.name,
                "sha256_head": "",
                "filename_fields": asdict(fields),
            }
            result = _process_pdf_with_timeout(
                pdf_path,
                page_nums,
                args.text_limit,
                args.vision,
                args.vision_timeout,
                args.per_file_timeout,
            )
            if not result.get("ok"):
                stats["errors"] += 1
                item = dict(record_base)
                item["error"] = str(result.get("error", "unknown error"))
                reject_fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                if stats["scanned"] % 25 == 0:
                    print(json.dumps({k: stats[k] for k in ("scanned", "silver", "needs_labeling", "rejected", "errors")}, ensure_ascii=False), flush=True)
                continue
            record_base["sha256_head"] = str(result.get("sha256_head", ""))
            page_count = int(result.get("page_count", 0))
            sources = [SourceResult(**s) for s in result.get("sources", [])]

            support_score, support = _support_score(fields, sources)
            best_quality = max((s.quality for s in sources), default=0.0)
            item = dict(record_base)
            item.update({
                "sources": [asdict(s) for s in sources],
                "support_score": support_score,
                "support": support,
                "best_quality": best_quality,
                "page_count": page_count,
            })

            can_train = (
                fields.usable()
                and support_score >= args.min_support_score
                and best_quality >= args.min_quality
            )
            if can_train and stats["silver"] < args.max_silver:
                item["dataset_tier"] = "silver"
                item["training_messages"] = _training_messages(fields, sources, pdf_path.name)
                silver_fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                stats["silver"] += 1
            elif stats["needs_labeling"] < args.max_labeling:
                item["dataset_tier"] = "needs_labeling"
                reasons = []
                if not fields.usable():
                    reasons.append("filename_fields_not_usable")
                if support_score < args.min_support_score:
                    reasons.append("low_support_score")
                if best_quality < args.min_quality:
                    reasons.append("low_ocr_quality")
                item["labeling_reasons"] = reasons
                label_fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                stats["needs_labeling"] += 1
            else:
                item["dataset_tier"] = "rejected"
                reject_fh.write(json.dumps(item, ensure_ascii=False) + "\n")
                stats["rejected"] += 1

            if stats["scanned"] % 25 == 0:
                print(json.dumps({k: stats[k] for k in ("scanned", "silver", "needs_labeling", "rejected", "errors")}, ensure_ascii=False), flush=True)

    stats["finished_at"] = datetime.now().strftime("%Y%m%d_%H%M%S")
    stats["output_dir"] = str(out_dir)
    stats["files"] = {
        "silver": str(silver_path),
        "needs_labeling": str(label_path),
        "rejected": str(reject_path),
        "manifest": str(manifest_path),
    }
    manifest_path.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build MAGI OCR silver training data from curated PDFs.")
    parser.add_argument("--process-one", default="", help=argparse.SUPPRESS)
    parser.add_argument("--root", dest="roots", action="append", help="Root directory to scan. Can be passed multiple times.")
    parser.add_argument("--candidate-list", default="", help="Newline-delimited PDF path list. Prefer this for large NAS/Synology roots.")
    parser.add_argument("--output-dir", default="", help="Output directory. Defaults to data/ocr_training/<timestamp>.")
    parser.add_argument("--max-candidates", type=int, default=500)
    parser.add_argument("--max-silver", type=int, default=120)
    parser.add_argument("--max-labeling", type=int, default=80)
    parser.add_argument("--pages", default="0", help="Comma-separated zero-based pages to sample, default: 0.")
    parser.add_argument("--text-limit", type=int, default=2400)
    parser.add_argument("--vision", action="store_true", help="Run macOS Vision OCR in addition to native text.")
    parser.add_argument("--vision-timeout", type=float, default=25.0)
    parser.add_argument("--per-file-timeout", type=float, default=45.0, help="Hard timeout per PDF to avoid one cloud placeholder blocking the batch.")
    parser.add_argument("--min-support-score", type=float, default=0.58)
    parser.add_argument("--min-quality", type=float, default=0.22)
    args = parser.parse_args(argv)

    if args.process_one:
        page_nums = [int(p) for p in str(args.pages).split(",") if str(p).strip().isdigit()] or [0]
        try:
            result = _process_pdf_direct(
                Path(args.process_one).expanduser(),
                page_nums,
                args.text_limit,
                args.vision,
                args.vision_timeout,
            )
        except Exception as exc:
            result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 1

    start = time.time()
    stats = build_dataset(args)
    stats["duration_sec"] = round(time.time() - start, 2)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
