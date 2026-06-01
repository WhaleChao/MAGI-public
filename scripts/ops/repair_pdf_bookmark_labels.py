#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Audit and repair polluted PDF bookmark labels.

The regular bookmark batch skips PDFs that already have enough bookmarks.  That
is correct for throughput, but it also means old false-positive bookmarks can
survive forever.  This tool only targets those existing bookmarks whose page no
longer proves the label under the current boundary/title rules.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

DEFAULT_REPORT = MAGI_ROOT / ".runtime" / "pdf_bookmark_label_repair_latest.json"
HISTORY_PATH = MAGI_ROOT / ".runtime" / "pdf_bookmark_label_repair_history.jsonl"

TARGET_DIR_HINTS = (
    "閱卷資料",
    "法院通知",
    "程序裁定",
    "判決書",
    "筆錄",
    "證據資料",
    "對方歷次書狀",
    "對造歷次書狀",
)
SKIP_DIR_NAMES = {
    ".DS_Store",
    ".git",
    ".Trash",
    "@eaDir",
    "#recycle",
    "node_modules",
    "__pycache__",
}
POLLUTION_PRONE_FALLBACK_LABELS = {
    "鑑定報告",
    "精神鑑定報告",
    "法醫報告",
    "驗傷診斷書",
    "相驗屍體證明書",
    "勘查報告",
    "照片/截圖",
    "扣押物品目錄表",
    "搜索扣押筆錄",
    "起訴書",
    "追加起訴書",
    "不起訴處分書",
    "緩起訴處分書",
    "聲請簡易判決處刑書",
    "判決",
    "裁定",
    "答辯狀",
    "陳報狀",
    "聲請狀",
    "上訴/抗告狀",
    "補充理由狀",
    "準備程序筆錄",
    "審判筆錄",
    "訊問筆錄",
    "調查筆錄",
    "言詞辯論筆錄",
}
LEGACY_IMAGE_LABEL_RE = re.compile(r"^image\d{4,}$", re.IGNORECASE)
_DOC_REFERENCE_TERMS_RE = (
    r"判決(?:書)?|裁定(?:書)?|起訴書|追加起訴書|不起訴處分書|緩起訴處分書|"
    r"聲請簡易判決處刑書|答辯(?:狀|書)|陳報(?:狀|書)|聲請(?:狀|書)|"
    r"上訴(?:狀|書|理由)|抗告(?:狀|書|理由)|補充(?:理由|上訴|告訴)(?:狀|書)|"
    r"(?:審判|準備程序|言詞辯論|訊問|調查|勘驗).{0,3}筆錄|"
    r"鑑定(?:報告|書|意見)|法醫(?:報告|鑑定)|解剖(?:報告|鑑定)|"
    r"診斷(?:證明|書)|相驗屍體證明書|扣押物品(?:目錄表|清單)|"
    r"搜索扣押(?:筆錄|紀錄)|勘(?:查|察|驗)(?:報告|紀錄)|前案紀錄表"
)
_BODY_DOC_REFERENCE_RE = re.compile(
    rf"(?:證據|附件|附表|目錄|清單|卷附|卷內|提出|檢附|引用|參酌|調查|提示|"
    rf"所附|所載|記載|主張|抗辯|證明|待證|詳如|如附件|如附表|前開|上開|"
    rf"起訴意旨|上訴意旨|原審|本院|檢察官|辯護人|法官問|被告答)"
    rf".{{0,90}}(?:{_DOC_REFERENCE_TERMS_RE})|"
    rf"(?:{_DOC_REFERENCE_TERMS_RE}).{{0,90}}"
    rf"(?:證據|附件|附表|目錄|清單|卷附|卷內|提出|檢附|引用|參酌|調查|提示|"
    rf"所附|所載|記載|主張|抗辯|證明|待證|詳如|如附件|如附表|前開|上開|"
    rf"起訴意旨|上訴意旨|原審|本院|檢察官|辯護人|法官問|被告答)"
)
_TRANSCRIPT_DIALOGUE_RE = re.compile(
    r"(?:法官問|檢察官問|檢察官答|被告答|辯護人答|通譯答).{0,180}"
    rf"(?:{_DOC_REFERENCE_TERMS_RE}|所附下列證據|逐一提示|告以要旨|有何意見)"
)
_ATTACHMENT_OR_EVIDENCE_LIST_RE = re.compile(
    rf"(?:檢證|辯證|證物|證據|附件|附表|附錄|目錄|清單)\s*(?:編號|名稱|項次|待證事實).{{0,160}}"
    rf"(?:{_DOC_REFERENCE_TERMS_RE})|"
    rf"(?:{_DOC_REFERENCE_TERMS_RE}).{{0,120}}(?:待證事實|證據能力|調查方式|附件|附表|附錄|目錄|清單)"
)
_CONTINUATION_CONTEXT_RE = re.compile(r"^\s*[\(（【\[]?\s*(?:續|接|承|讀)\s*(?:上|前|上頁|前頁)")
_DOCUMENT_EQUIVALENT_GROUPS = [
    {"鑑定報告", "精神鑑定報告", "法醫報告"},
    {"調解/和解", "調解/和解筆錄"},
]


class PerFileTimeout(RuntimeError):
    pass


class per_file_timeout:
    def __init__(self, seconds: int, label: str):
        self.seconds = int(seconds or 0)
        self.label = label
        self._previous_handler = None

    def __enter__(self):
        if self.seconds <= 0 or not hasattr(signal, "SIGALRM"):
            return self

        def _raise_timeout(_signum, _frame):
            raise PerFileTimeout(f"{self.label} exceeded {self.seconds}s")

        self._previous_handler = signal.getsignal(signal.SIGALRM)
        signal.signal(signal.SIGALRM, _raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, self.seconds)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.seconds > 0 and hasattr(signal, "SIGALRM"):
            signal.setitimer(signal.ITIMER_REAL, 0)
            if self._previous_handler is not None:
                signal.signal(signal.SIGALRM, self._previous_handler)
        return False


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if not spec or not spec.loader:
        raise RuntimeError(f"cannot load module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_bookmarker():
    return _load_module("pdf_bookmarker_action_repair", MAGI_ROOT / "skills" / "pdf-bookmarker" / "action.py")


def _default_roots(include_closed: bool = False) -> list[str]:
    try:
        from api.case_path_mapper import preferred_case_roots

        return [p for p in preferred_case_roots(include_closed=include_closed) if p]
    except Exception:
        fallback = Path.home() / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件"
        return [str(fallback)]


def _safe_rel(path: Path) -> str:
    try:
        return str(path.relative_to(MAGI_ROOT))
    except Exception:
        return str(path)


def _target_hint_match(path: str, hints: tuple[str, ...] = TARGET_DIR_HINTS) -> bool:
    normalized = str(path).replace("\\", "/")
    return any(hint in normalized for hint in hints)


def _prune_dirs(dirnames: list[str]) -> None:
    keep = []
    for name in dirnames:
        if name.startswith(".") or name in SKIP_DIR_NAMES:
            continue
        if name.endswith(".tmp") or name.endswith(".backup"):
            continue
        keep.append(name)
    dirnames[:] = keep


def discover_case_roots(
    roots: list[str],
    *,
    case_number: str,
    max_dirs: int = 3000,
    max_seconds: int = 600,
) -> tuple[list[Path], dict[str, Any]]:
    started = time.time()
    found: list[Path] = []
    visited_dirs = 0
    stopped_reason = ""
    case_number = (case_number or "").strip()
    if not case_number:
        return [], {"visited_dirs": 0, "stopped_reason": "no_case_number", "elapsed_sec": 0}

    for root in roots:
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            continue
        if case_number in str(root_path):
            found.append(root_path)
            continue

        for dirpath, dirnames, _filenames in os.walk(root_path):
            visited_dirs += 1
            if visited_dirs >= max_dirs:
                stopped_reason = "max_dirs"
                dirnames[:] = []
                break
            if time.time() - started >= max_seconds:
                stopped_reason = "max_seconds"
                dirnames[:] = []
                break

            _prune_dirs(dirnames)
            dirnames.sort()
            current = Path(dirpath)
            rel_depth = max(0, len(current.relative_to(root_path).parts)) if current != root_path else 0
            if case_number in current.name:
                found.append(current)
                dirnames[:] = []
                continue
            # Case folders live near the top of each case-root tree.  Do not
            # descend into every evidence/document subfolder while only looking
            # for a case folder name.
            if rel_depth >= 5:
                dirnames[:] = []

        if stopped_reason in {"max_dirs", "max_seconds"}:
            break

    return found, {
        "visited_dirs": visited_dirs,
        "stopped_reason": stopped_reason or "completed",
        "elapsed_sec": round(time.time() - started, 3),
        "case_root_count": len(found),
    }


def iter_pdf_candidates(
    roots: list[str],
    *,
    case_number: str = "",
    target_hints: tuple[str, ...] = TARGET_DIR_HINTS,
    max_files: int = 200,
    max_dirs: int = 3000,
    max_seconds: int = 600,
    max_file_mb: int = 200,
) -> tuple[list[Path], dict[str, Any]]:
    started = time.time()
    pdfs: list[Path] = []
    visited_dirs = 0
    skipped_large_files = 0
    stopped_reason = ""
    case_number = (case_number or "").strip()

    if case_number:
        case_roots, discovery_meta = discover_case_roots(
            roots,
            case_number=case_number,
            max_dirs=max_dirs,
            max_seconds=max_seconds,
        )
        if not case_roots:
            return [], {
                "visited_dirs": discovery_meta.get("visited_dirs", 0),
                "stopped_reason": "case_not_found",
                "elapsed_sec": round(time.time() - started, 3),
                "case_discovery": discovery_meta,
            }
        remaining_seconds = max(1, int(max_seconds - (time.time() - started))) if max_seconds else max_seconds
        pdfs, scan_meta = iter_pdf_candidates(
            [str(path) for path in case_roots],
            case_number="",
            target_hints=target_hints,
            max_files=max_files,
            max_dirs=max_dirs,
            max_seconds=remaining_seconds,
            max_file_mb=max_file_mb,
        )
        scan_meta["case_discovery"] = discovery_meta
        scan_meta["visited_dirs"] = int(scan_meta.get("visited_dirs", 0)) + int(discovery_meta.get("visited_dirs", 0))
        scan_meta["elapsed_sec"] = round(time.time() - started, 3)
        return pdfs, scan_meta

    for root in roots:
        root_path = Path(root).expanduser()
        if not root_path.is_dir():
            continue

        for dirpath, dirnames, filenames in os.walk(root_path):
            visited_dirs += 1
            if visited_dirs >= max_dirs:
                stopped_reason = "max_dirs"
                dirnames[:] = []
                break
            if time.time() - started >= max_seconds:
                stopped_reason = "max_seconds"
                dirnames[:] = []
                break

            _prune_dirs(dirnames)
            current = Path(dirpath)
            rel_depth = max(0, len(current.relative_to(root_path).parts)) if current != root_path else 0
            if rel_depth > 9:
                dirnames[:] = []
                continue

            current_text = str(current)
            if case_number and case_number not in current_text:
                # Keep descending near the top because the case number is usually
                # embedded in the case folder name, not in category folders.
                if rel_depth >= 4:
                    dirnames[:] = []
                continue

            in_target = _target_hint_match(current_text, target_hints)
            for name in sorted(filenames):
                if len(pdfs) >= max_files:
                    stopped_reason = "max_files"
                    dirnames[:] = []
                    break
                if not name.lower().endswith(".pdf") or name.startswith("."):
                    continue
                if name.endswith(".tmp.pdf") or name.endswith(".part.pdf"):
                    continue
                if in_target:
                    pdf_path = current / name
                    if max_file_mb > 0:
                        try:
                            if pdf_path.stat().st_size > max_file_mb * 1024 * 1024:
                                skipped_large_files += 1
                                continue
                        except OSError:
                            pass
                    pdfs.append(pdf_path)

            if stopped_reason:
                break

        if stopped_reason in {"max_files", "max_dirs", "max_seconds"}:
            break

    return pdfs, {
        "visited_dirs": visited_dirs,
        "stopped_reason": stopped_reason or "completed",
        "elapsed_sec": round(time.time() - started, 3),
        "skipped_large_files": skipped_large_files,
    }


def _known_labels(bookmarker) -> list[str]:
    labels = set(getattr(bookmarker, "KNOWN_DOC_LABELS", set()) or set())
    labels.update(POLLUTION_PRONE_FALLBACK_LABELS)
    return sorted(labels, key=len, reverse=True)


def _label_in_title(title: str, labels: list[str]) -> str:
    text = re.sub(r"\s+", "", str(title or ""))
    for label in labels:
        compact = re.sub(r"\s+", "", label)
        if compact and compact in text:
            return label
    if LEGACY_IMAGE_LABEL_RE.match(text):
        return text
    return ""


def _same_label(a: str | None, b: str | None) -> bool:
    aa = re.sub(r"\s+", "", str(a or ""))
    bb = re.sub(r"\s+", "", str(b or ""))
    if not aa or not bb:
        return False
    if aa == bb:
        return True
    for group in _DOCUMENT_EQUIVALENT_GROUPS:
        compact_group = {re.sub(r"\s+", "", item) for item in group}
        if aa in compact_group and bb in compact_group:
            return True
    return bool(aa in bb or bb in aa)


def _compact(text: str | None) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _page_title_proves_label(bookmarker, text: str, label: str | None) -> bool:
    """Return True only when the page header/title area itself supports label."""
    normalized = _compact(label)
    if not normalized:
        return False
    boundary = getattr(bookmarker, "_boundary_region", lambda t, limit=650: str(t or "")[:limit])(text, limit=650)
    lines = [line.strip() for line in str(boundary or "").splitlines() if line.strip()]
    early_region = "\n".join(lines[:4]) or str(boundary or "")[:260]
    try:
        if getattr(bookmarker, "_is_reference_only_page")(text):
            return False
    except Exception:
        pass
    if _ATTACHMENT_OR_EVIDENCE_LIST_RE.search(early_region):
        return False
    for idx, line in enumerate(lines[:4]):
        line_compact = _compact(line)
        if normalized not in line_compact:
            continue
        if _BODY_DOC_REFERENCE_RE.search(line) or _TRANSCRIPT_DIALOGUE_RE.search(line):
            continue
        title_like = (
            idx == 0
            or len(line_compact) <= 36
            or bool(re.search(r"(?:法院|檢察署|地檢署|法務部|研究所|醫院|鑑定中心)", line))
        )
        if title_like:
            return True
    detected, _level = getattr(bookmarker, "_detect_doc_type")(text, in_prior_record=False, allow_vision=False)
    return _same_label(detected, normalized)


def _pollution_context_evidence(bookmarker, text: str, label: str | None) -> list[str]:
    """Collect broad evidence that a label is merely cited, not a page boundary."""
    if not label:
        return []
    evidence: list[str] = []
    boundary = getattr(bookmarker, "_boundary_region", lambda t, limit=900: str(t or "")[:limit])(text, limit=900)
    first_line = next((line.strip() for line in str(boundary or "").splitlines() if line.strip()), "")
    title_proven = _page_title_proves_label(bookmarker, text, label)
    try:
        if getattr(bookmarker, "_is_reference_only_page")(text):
            evidence.append("reference_only_page")
    except Exception:
        pass
    if _CONTINUATION_CONTEXT_RE.search(first_line):
        evidence.append("continuation_page")
    if _TRANSCRIPT_DIALOGUE_RE.search(boundary):
        evidence.append("transcript_dialogue_reference")
    if _ATTACHMENT_OR_EVIDENCE_LIST_RE.search(boundary):
        evidence.append("attachment_or_evidence_list")
    if _BODY_DOC_REFERENCE_RE.search(boundary) and not title_proven:
        evidence.append("body_reference_context")
    return sorted(set(evidence))


def _page_text(doc, page_number: int) -> str:
    try:
        if page_number < 1 or page_number > doc.page_count:
            return ""
        return doc[page_number - 1].get_text() or ""
    except Exception:
        return ""


def _audit_existing_toc(
    *,
    pdf_path: str,
    existing_toc: list[list[Any]],
    text_by_page: dict[int, str],
    bookmarker,
) -> list[dict[str, Any]]:
    labels = _known_labels(bookmarker)
    issues: list[dict[str, Any]] = []
    is_transcript_pdf = bool(getattr(bookmarker, "_is_standalone_transcript_pdf", lambda _p: False)(pdf_path))

    if is_transcript_pdf:
        transcript_like = [
            entry for entry in existing_toc
            if len(entry) >= 3 and "筆錄" in str(entry[1] or "")
        ]
        if len(existing_toc) != 1 or not transcript_like:
            issues.append(
                {
                    "kind": "standalone_transcript_multi_bookmark",
                    "page": 1,
                    "title": existing_toc[0][1] if existing_toc else "",
                    "detail": "筆錄單檔應只保留第一頁筆錄書籤，內文提到的鑑定/書狀不得成為書籤",
                }
            )
        return issues

    for entry in existing_toc:
        if len(entry) < 3:
            continue
        _level, title, page = entry[:3]
        try:
            page_num = int(page)
        except Exception:
            continue

        title_text = str(title or "").strip()
        embedded_label = _label_in_title(title_text, labels)
        if not embedded_label:
            continue

        if LEGACY_IMAGE_LABEL_RE.match(embedded_label):
            issues.append(
                {
                    "kind": "legacy_image_label",
                    "page": page_num,
                    "title": title_text,
                    "detail": "舊版 image0000x 書籤需要用現在規則重建",
                }
            )
            continue

        text = text_by_page.get(page_num, "")
        detected, _level = getattr(bookmarker, "_detect_doc_type")(text, in_prior_record=False, allow_vision=False)
        normalized_title = getattr(bookmarker, "_normalize_doc_type", lambda v, _c="": v)(embedded_label, text)
        title_proven = _page_title_proves_label(bookmarker, text, normalized_title or embedded_label)
        context_evidence = _pollution_context_evidence(bookmarker, text, normalized_title or embedded_label)

        if context_evidence and not title_proven and not _same_label(detected, normalized_title):
            issues.append(
                {
                    "kind": "context_reference_label",
                    "page": page_num,
                    "title": title_text,
                    "detected": detected,
                    "expected": normalized_title,
                    "context_evidence": context_evidence,
                    "detail": "該頁看起來是在正文、附件表、證據清單或筆錄問答中引用文件名稱，不是該文件首頁",
                }
            )
            continue

        if (
            detected is None
            and normalized_title
            and not title_proven
            and (context_evidence or embedded_label in POLLUTION_PRONE_FALLBACK_LABELS)
        ):
            issues.append(
                {
                    "kind": "unproven_document_label",
                    "page": page_num,
                    "title": title_text,
                    "expected": normalized_title,
                    "context_evidence": context_evidence,
                    "detail": "既有書籤標籤未能由頁面標題區證明，需用現行邊界規則重建",
                }
            )
            continue

        if detected and normalized_title and not _same_label(detected, normalized_title):
            issues.append(
                {
                    "kind": "label_page_mismatch",
                    "page": page_num,
                    "title": title_text,
                    "detected": detected,
                    "expected": normalized_title,
                    "detail": "既有書籤類型與目前頁首辨識結果不同",
                }
            )

    return issues


def audit_pdf(pdf_path: Path, bookmarker, *, verify_rebuild: bool = False) -> dict[str, Any]:
    item: dict[str, Any] = {
        "pdf": str(pdf_path),
        "relative_pdf": _safe_rel(pdf_path),
        "needs_repair": False,
        "repairable": False,
        "issues": [],
        "existing_toc_count": 0,
        "generated_toc_count": 0,
    }
    doc = None
    try:
        doc = bookmarker.fitz.open(str(pdf_path))
        existing_toc = doc.get_toc() or []
        item["page_count"] = doc.page_count
        item["existing_toc_count"] = len(existing_toc)
        if not existing_toc:
            item["classification"] = "no_existing_toc"
            return item
        pages = sorted({int(entry[2]) for entry in existing_toc if len(entry) >= 3 and str(entry[2]).isdigit()})
        text_by_page = {page: _page_text(doc, page) for page in pages}
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass

    issues = _audit_existing_toc(
        pdf_path=str(pdf_path),
        existing_toc=existing_toc,
        text_by_page=text_by_page,
        bookmarker=bookmarker,
    )
    item["issues"] = issues
    item["needs_repair"] = bool(issues)
    if not issues:
        item["classification"] = "clean_existing_toc"
        return item

    if not verify_rebuild:
        item["repairable"] = True
        item["classification"] = "needs_repair_by_rule"
        item["repair_message"] = "rule audit only; rebuild will be attempted only in apply mode"
        return item

    result = bookmarker.scan_and_bookmark(str(pdf_path), dry_run=True, rebuild_existing=True)
    generated = result.get("generated_toc") or result.get("toc") or []
    item["generated_toc_count"] = len(generated)
    item["generated_sample"] = [
        {"level": row[0], "title": row[1], "page": row[2]}
        for row in generated[:12]
        if len(row) >= 3
    ]
    item["repairable"] = bool(result.get("success") and generated)
    item["repair_message"] = result.get("message", "")
    item["classification"] = result.get("classification", "")
    item["classification_reason"] = result.get("classification_reason", "")
    return item


def repair_pdf(pdf_path: Path, bookmarker) -> dict[str, Any]:
    started = time.time()
    result = bookmarker.scan_and_bookmark(str(pdf_path), dry_run=False, rebuild_existing=True)
    return {
        "pdf": str(pdf_path),
        "success": bool(result.get("success")),
        "bookmarks": int(result.get("bookmarks") or 0),
        "message": result.get("message", ""),
        "elapsed_sec": round(time.time() - started, 3),
    }


def audit_pdf_in_subprocess(pdf_path: Path, *, timeout_sec: int, verify_rebuild: bool = False) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--audit-one",
        str(pdf_path),
    ]
    if verify_rebuild:
        cmd.append("--verify-rebuild")
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1, int(timeout_sec or 0)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PerFileTimeout(f"audit {pdf_path.name} exceeded {timeout_sec}s") from exc
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"audit failed: {pdf_path}").strip())
    return json.loads(proc.stdout)


def repair_pdf_in_subprocess(pdf_path: Path, *, timeout_sec: int) -> dict[str, Any]:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--repair-one",
        str(pdf_path),
    ]
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1, int(timeout_sec or 0)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise PerFileTimeout(f"repair {pdf_path.name} exceeded {timeout_sec}s") from exc
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or f"repair failed: {pdf_path}").strip())
    return json.loads(proc.stdout)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(report, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit and repair polluted PDF bookmarks.")
    parser.add_argument("--root", action="append", default=[], help="Case root or case folder to scan. Can be repeated.")
    parser.add_argument("--include-closed", action="store_true", help="Also use closed-case roots when no --root is supplied.")
    parser.add_argument("--case-number", default="", help="Limit scan to a single OSC case number, e.g. 2026-0028.")
    parser.add_argument("--target-hint", action="append", default=[], help="Folder-name hint to include; repeats override defaults.")
    parser.add_argument("--max-files", type=int, default=200)
    parser.add_argument("--max-dirs", type=int, default=3000)
    parser.add_argument("--max-seconds", type=int, default=600)
    parser.add_argument("--max-file-mb", type=int, default=200, help="Skip PDFs larger than this size; 0 disables.")
    parser.add_argument("--apply", action="store_true", help="Rewrite repairable polluted PDFs.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum repairs to apply in one run.")
    parser.add_argument("--per-file-timeout", type=int, default=90, help="Hard timeout for one PDF audit/repair.")
    parser.add_argument("--verify-rebuild", action="store_true", help="Dry-run rebuild during audit. Slower; normally not needed.")
    parser.add_argument("--json-out", default=str(DEFAULT_REPORT))
    parser.add_argument("--print-json", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--audit-one", default="", help=argparse.SUPPRESS)
    parser.add_argument("--repair-one", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    roots = [str(Path(p).expanduser()) for p in args.root] or _default_roots(include_closed=args.include_closed)
    target_hints = tuple(args.target_hint) if args.target_hint else TARGET_DIR_HINTS
    bookmarker = _load_bookmarker()
    if not args.verbose:
        logging.getLogger("pdf-bookmarker").setLevel(logging.WARNING)

    if args.audit_one:
        item = audit_pdf(Path(args.audit_one), bookmarker, verify_rebuild=args.verify_rebuild)
        print(json.dumps(item, ensure_ascii=False))
        return 0
    if args.repair_one:
        item = repair_pdf(Path(args.repair_one), bookmarker)
        print(json.dumps(item, ensure_ascii=False))
        return 0

    started = time.time()

    pdfs, scan_meta = iter_pdf_candidates(
        roots,
        case_number=args.case_number,
        target_hints=target_hints,
        max_files=args.max_files,
        max_dirs=args.max_dirs,
        max_seconds=args.max_seconds,
        max_file_mb=args.max_file_mb,
    )

    audited: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    repaired: list[dict[str, Any]] = []

    audit_stopped_reason = ""
    for pdf in pdfs:
        if args.max_seconds and time.time() - started >= args.max_seconds:
            audit_stopped_reason = "max_seconds"
            break
        try:
            if args.per_file_timeout > 0:
                item = audit_pdf_in_subprocess(
                    pdf,
                    timeout_sec=args.per_file_timeout,
                    verify_rebuild=args.verify_rebuild,
                )
            else:
                item = audit_pdf(pdf, bookmarker, verify_rebuild=args.verify_rebuild)
            audited.append(item)
        except PerFileTimeout as exc:
            errors.append(
                {
                    "pdf": str(pdf),
                    "error": str(exc),
                    "kind": "per_file_timeout",
                }
            )
        except Exception as exc:
            errors.append(
                {
                    "pdf": str(pdf),
                    "error": str(exc),
                    "traceback": traceback.format_exc(limit=5),
                }
            )

    repairable = sorted(
        [item for item in audited if item.get("needs_repair") and item.get("repairable")],
        key=lambda item: (int(item.get("page_count") or 10**9), str(item.get("pdf") or "")),
    )
    if args.apply:
        for item in repairable[: max(0, args.limit)]:
            try:
                if args.per_file_timeout > 0:
                    repaired.append(repair_pdf_in_subprocess(Path(item["pdf"]), timeout_sec=args.per_file_timeout))
                else:
                    repaired.append(repair_pdf(Path(item["pdf"]), bookmarker))
            except PerFileTimeout as exc:
                repaired.append({"pdf": item["pdf"], "success": False, "message": str(exc), "kind": "per_file_timeout"})
            except Exception as exc:
                repaired.append({"pdf": item["pdf"], "success": False, "message": str(exc)})

    report = {
        "ok": not errors,
        "mode": "apply" if args.apply else "dry_run",
        "roots": roots,
        "case_number": args.case_number,
        "target_hints": list(target_hints),
        "scanned_pdf_count": len(pdfs),
        "audited_pdf_count": len(audited),
        "needs_repair_count": sum(1 for item in audited if item.get("needs_repair")),
        "repairable_count": len(repairable),
        "repaired_count": sum(1 for item in repaired if item.get("success")),
        "errors_count": len(errors),
        "elapsed_sec": round(time.time() - started, 3),
        "scan": scan_meta,
        "audit_stopped_reason": audit_stopped_reason or "completed",
        "candidates": [
            item for item in audited
            if item.get("needs_repair")
        ][:100],
        "repaired": repaired,
        "errors": errors[:20],
    }
    _write_report(Path(args.json_out), report)

    if args.print_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            "pdf-bookmark-label-repair: "
            f"scanned={report['scanned_pdf_count']} "
            f"needs_repair={report['needs_repair_count']} "
            f"repairable={report['repairable_count']} "
            f"repaired={report['repaired_count']} "
            f"errors={report['errors_count']}"
        )
        print(f"report={args.json_out}")

    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
