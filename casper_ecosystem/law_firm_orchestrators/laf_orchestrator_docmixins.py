# -*- coding: utf-8 -*-
"""
LAF Orchestrator Document Mixin
================================
Provides document classification, date extraction, and case folder
scanning utilities used by LAFOrchestrator.

This mixin is mixed into LAFOrchestrator and its methods are also
patched onto the class via setattr at module load time.

Methods:
    - _empty_docs_map: baseline empty docs dict
    - _dedupe_sorted: deduplicate + sort a list of paths
    - _is_consumer_debt_case_folder: check folder name for 消費者債務清理
    - _scan_case_folder_docs: enhanced folder scanner (adds closing/withdrawal keys)
    - _get_withdrawal_pdf_candidates / _non_pdf / _template: withdrawal doc helpers
    - _sort_closing_basis_files: sort judgment/ruling files for closing
    - _infer_closing_metadata_from_docs: extract court info from filenames
    - _query_db: thin DB query wrapper
    - various date/text extraction helpers
"""

import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("laf_orchestrator.docmixins")

# ── Keywords ──────────────────────────────────────────────
_CONSUMER_DEBT_KEYWORDS = (
    "消費者債務清理", "消債", "更生", "清算", "前置調解",
    "前置協商", "債務清理",
)

_CLOSING_BASIS_KEYWORDS = (
    "判決", "裁定", "不起訴處分書", "起訴書", "確定證明書",
    "和解筆錄", "調解筆錄", "調解成立",
    "併辦意旨書", "追加起訴書",
)

_ENFORCEMENT_CLOSING_KEYWORDS = (
    "執行命令", "債權憑證", "執行結果", "終結執行",
)

_OFFICE_RECEIPT_KEYWORDS = (
    "收文章", "法院章", "回執", "收件回執", "送達證書",
    "郵局回執", "掛號回執",
)

_CLOSING_FEE_KEYWORDS = (
    "結案費用", "結案酬金", "酬金收據", "酬金", "律師費收據",
)

_CHANGE_REVIEW_KEYWORDS = (
    "變更審查通知", "變更通知", "審查通知",
)

_WITHDRAWAL_SIGNED_PDF_KEYWORDS = (
    "撤回書", "撤回狀", "撤回聲請",
)

_WITHDRAWAL_TEMPLATE_KEYWORDS = (
    "撤回書範本", "撤回範本", "撤回書母版", "撤回書模板",
    "撤回書_範本", "撤回書(範本)", "撤回書（範本）",
)


class LAFOrchestratorDocumentMixin:
    """Document-related methods mixed into LAFOrchestrator."""

    # ==================================================================
    # Text / Path Utilities
    # ==================================================================

    @staticmethod
    def _text_contains_any(text: str, keywords: tuple | list) -> bool:
        """Return True if *text* contains any keyword."""
        t = str(text or "")
        return any(k in t for k in keywords)

    @staticmethod
    def _find_first_existing(paths: List[str]) -> str:
        """Return first path that exists on disk."""
        for p in (paths or []):
            if p and os.path.exists(p):
                return p
        return ""

    @staticmethod
    def _dedupe_sorted(items: List[str]) -> List[str]:
        """Deduplicate and sort a list of file paths."""
        seen: set = set()
        out: List[str] = []
        for p in (items or []):
            norm = os.path.normpath(str(p))
            if norm not in seen:
                seen.add(norm)
                out.append(str(p))
        return sorted(out)

    # ==================================================================
    # Date Extraction
    # ==================================================================

    @staticmethod
    def _normalize_date_text(raw: str) -> str:
        """Normalize various date formats → YYYY-MM-DD."""
        s = (raw or "").strip()
        if not s:
            return ""
        s = s.replace("年", "-").replace("月", "-").replace("日", "")
        s = s.replace("/", "-").replace(".", "-")
        s = re.sub(r"\s+", "", s)
        # YYYY-M-D
        m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        # ROC 3-digit year
        m = re.search(r"(\d{3})-(\d{1,2})-(\d{1,2})", s)
        if m:
            y = int(m.group(1)) + 1911
            return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        # YYYYMMDD
        m = re.search(r"(\d{4})(\d{2})(\d{2})", s)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        # RRRMMDD
        m = re.search(r"(\d{3})(\d{2})(\d{2})", s)
        if m:
            y = int(m.group(1)) + 1911
            return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return ""

    def _extract_date_from_filename(self, path_value: str) -> str:
        """Extract date from filename via normalization."""
        base = os.path.basename(str(path_value or ""))
        return self._normalize_date_text(base)

    @staticmethod
    def _extract_date_from_office_text(text: str) -> str:
        """Extract date from Office document text content (docx/pdf text)."""
        s = str(text or "").strip()
        if not s:
            return ""
        # Look for ROC date patterns like 115年3月27日
        m = re.search(r"(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", s)
        if m:
            y = int(m.group(1)) + 1911
            return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        # Western date 2026-03-27 or 2026/03/27
        m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return ""

    # ── OCR / Vision Helpers ──

    def _get_doc_hint_ocr_engine(self):
        """Lazily initialize and return the OCR engine (macOS Vision)."""
        if getattr(self, "_doc_hint_ocr_init_attempted", False):
            return getattr(self, "_doc_hint_ocr_engine", None)
        self._doc_hint_ocr_init_attempted = True
        try:
            from laf_vision import LAFVision
            self._doc_hint_ocr_engine = LAFVision()
        except Exception as e:
            logger.debug("OCR engine init failed: %s", e)
            self._doc_hint_ocr_engine = None
        return self._doc_hint_ocr_engine

    def _ocr_text_from_image(self, img_path: str) -> str:
        """Run OCR on an image file, returning extracted text."""
        engine = self._get_doc_hint_ocr_engine()
        if not engine:
            return ""
        try:
            return str(engine.extract_text(img_path) or "").strip()
        except Exception:
            return ""

    @staticmethod
    def _should_sniff_doc_content(path_value: str) -> bool:
        """Whether to attempt reading content for date/hint extraction."""
        ext = Path(path_value or "").suffix.lower()
        return ext in {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}

    def _extract_document_hint_text(self, path_value: str) -> str:
        """Extract hint text from a document (PDF first page, or OCR on images)."""
        p = str(path_value or "").strip()
        if not p or not os.path.isfile(p):
            return ""
        # Check cache
        cache = getattr(self, "_doc_hint_text_cache", {})
        if p in cache:
            return cache[p]
        text = ""
        ext = Path(p).suffix.lower()
        try:
            if ext == ".pdf":
                import fitz
                doc = fitz.open(p)
                if doc.page_count > 0:
                    text = doc.load_page(0).get_text().strip()[:2000]
                doc.close()
            elif ext in {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}:
                text = self._ocr_text_from_image(p)
        except Exception as e:
            logger.debug("Hint text extraction failed for %s: %s", p, e)
        if hasattr(self, "_doc_hint_text_cache"):
            self._doc_hint_text_cache[p] = text
        return text

    def _extract_date_with_vision(self, path_value: str) -> str:
        """Extract date from document using Vision/OCR as fallback."""
        p = str(path_value or "").strip()
        if not p or not os.path.exists(p):
            return ""
        ext = Path(p).suffix.lower()
        img_path = p
        temp_img = ""
        try:
            if ext == ".pdf":
                import fitz
                doc = fitz.open(p)
                page = doc.load_page(0)
                pix = page.get_pixmap()
                temp_img = str(Path(p).with_suffix(".laf_tmp.jpg"))
                pix.save(temp_img)
                img_path = temp_img

            from laf_vision import LAFVision
            vision = LAFVision()
            raw = vision.extract_start_date(img_path) or ""
            return self._normalize_date_text(raw)
        except Exception as e:
            logger.warning("Vision date extraction failed for %s: %s", p, e)
            return ""
        finally:
            if temp_img and os.path.exists(temp_img):
                try:
                    os.remove(temp_img)
                except Exception:
                    pass

    def _extract_best_date_from_doc(self, path_value: str, is_poa: bool = True) -> str:
        """Extract best date from document: filename → Office text → Vision OCR."""
        date_from_name = self._extract_date_from_filename(path_value)
        if date_from_name:
            return date_from_name
        # Try extracting from document text content
        if self._should_sniff_doc_content(path_value):
            hint = self._extract_document_hint_text(path_value)
            if hint:
                date_from_text = self._extract_date_from_office_text(hint)
                if date_from_text:
                    return date_from_text
        return self._extract_date_with_vision(path_value)

    # ==================================================================
    # Consumer Debt Detection
    # ==================================================================

    @staticmethod
    def _is_consumer_debt_case_folder(case_folder: str) -> bool:
        """Check if case folder name indicates 消費者債務清理 case."""
        p = str(case_folder or "").replace("\\", "/")
        return any(kw in p for kw in _CONSUMER_DEBT_KEYWORDS)

    @staticmethod
    def _is_consumer_debt_terminal_doc(filename: str) -> bool:
        """Check if filename is a terminal document for consumer debt case."""
        fn = str(filename or "")
        terminal_keywords = ("免責裁定", "不免責裁定", "認可更生方案", "清算終結",
                             "更生方案認可", "終止更生", "終結清算")
        return any(k in fn for k in terminal_keywords)

    @staticmethod
    def _is_fee_related_receipt_doc(filename: str) -> bool:
        """Check if filename is a fee/receipt related document."""
        fn = str(filename or "")
        fee_keywords = ("收據", "裁判費", "繳費", "粉紅", "pink",
                        "郵資", "掛號費", "影印費", "鑑定費")
        return any(k in fn.lower() for k in fee_keywords)

    @staticmethod
    def _filter_receipt_evidence_files(files: List[str]) -> List[str]:
        """Filter to only receipt/fee evidence files from a list."""
        out = []
        for f in (files or []):
            fn = os.path.basename(str(f or ""))
            if any(k in fn for k in ("收據", "裁判費", "收文章", "粉紅", "pink")):
                out.append(f)
        return out

    # ==================================================================
    # Empty Docs Map
    # ==================================================================

    @staticmethod
    def _empty_docs_map() -> dict:
        """Return a baseline empty document classification dict."""
        return {
            "opening_notice_files": [],
            "poa_files": [],
            "mediation_failure_files": [],
            "mediation_success_files": [],
            "pink_receipt_files": [],
            "receipt_files": [],
            "closing_basis_files": [],
            "office_receipt_files": [],
            "closing_fee_files": [],
            "change_review_notice_files": [],
            "withdrawal_files": [],
        }

    # ==================================================================
    # Enhanced Folder Scanning
    # ==================================================================

    def _scan_case_folder_docs(self, case_folder: str) -> dict:
        """
        Enhanced document scanner for LAF case folders.
        Scans known sub-directories (shallow) to avoid NAS I/O hang,
        and classifies documents into multiple categories including
        closing basis, withdrawal, office receipts, etc.
        """
        root = (case_folder or "").strip()
        out = self._empty_docs_map()
        if not root or not os.path.isdir(root):
            return out

        # Only scan known sub-directories (shallow), NOT os.walk the entire tree.
        # This avoids NAS I/O hang on folders with many files (e.g. 專員來信).
        _SCAN_SUBDIRS = [
            "",                  # root level
            "01_法扶資料",
            "02_開辦資料",
            "03_對造資料",
            "04_我方歷次書狀",
            "05_證據資料",
            "06_法院函文",
            "06_閱卷資料",
            "07_對造書狀",
            "08_法院通知或程序裁定",
            "09_酬金及費用",
            "10_判決書",
            "11_回執",
            "12_結案資料",
        ]
        allowed = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp",
                    ".doc", ".docx"}

        for subdir in _SCAN_SUBDIRS:
            scan_path = os.path.join(root, subdir) if subdir else root
            if not os.path.isdir(scan_path):
                continue
            try:
                entries = os.listdir(scan_path)
            except OSError:
                continue
            for fn in entries:
                if fn.startswith(".") or fn.startswith("~"):
                    continue
                ext = Path(fn).suffix.lower()
                if ext not in allowed:
                    # Check one level deeper for dated sub-folders
                    sub_sub = os.path.join(scan_path, fn)
                    if os.path.isdir(sub_sub):
                        try:
                            for fn2 in os.listdir(sub_sub):
                                if fn2.startswith(".") or fn2.startswith("~"):
                                    continue
                                if Path(fn2).suffix.lower() in allowed:
                                    self._classify_doc_file_enhanced(
                                        fn2, os.path.join(sub_sub, fn2), out, subdir
                                    )
                        except OSError:
                            pass
                    continue
                full = os.path.join(scan_path, fn)
                self._classify_doc_file_enhanced(fn, full, out, subdir)

        for k in out:
            out[k] = sorted(set(out[k]))
        return out

    @staticmethod
    def _classify_doc_file_enhanced(fn: str, full_path: str, out: dict,
                                      subdir: str = "") -> None:
        """Enhanced classification covering opening, closing, withdrawal, receipts."""
        if fn.startswith(".") or fn.startswith("~"):
            return
        # ── Opening documents ──
        # 主要關鍵字：開辦通知書 / 接案通知書 / 准予扶助證明書
        # 補充：在 02_開辦資料 路徑下檔名含「開辦資料」/「開辦」也視為 opening notice
        # （案件資料夾結構慣例 — 律師可能用較簡短檔名儲存簽署版開辦文件）
        is_opening_notice = any(k in fn for k in ("開辦通知書", "接案通知書", "准予扶助證明書"))
        # check 路徑（subdir 或 full_path）是否在 02_開辦資料 下
        in_open_dir = ("02_開辦資料" in str(subdir or "")) or ("02_開辦資料" in str(full_path or ""))
        if not is_opening_notice and in_open_dir and any(k in fn for k in ("開辦資料", "開辦")):
            # 排除「委任狀」「附條件...」等其他類別
            if not any(k in fn for k in ("委任狀", "附條件", "酬金", "結案", "撤回")):
                is_opening_notice = True
        if is_opening_notice:
            out["opening_notice_files"].append(full_path)
        if "委任狀" in fn:
            out["poa_files"].append(full_path)

        # ── Mediation documents ──
        if any(k in fn for k in ("調解不成立證明書", "調解不成立")):
            out["mediation_failure_files"].append(full_path)
        if any(k in fn for k in ("調解筆錄", "調解成立", "和解筆錄", "和解成立", "調解書")):
            if "不成立" not in fn:
                out["mediation_success_files"].append(full_path)

        # ── Receipts / Pink receipts ──
        low = fn.lower()
        if ("收據" in fn) or ("裁判費" in fn) or ("粉紅" in fn) or ("pink" in low):
            out["pink_receipt_files"].append(full_path)
        if "回執" in fn or "收件回執" in fn:
            out.setdefault("receipt_files", []).append(full_path)

        # ── Closing basis files (判決/裁定/不起訴處分書 etc.) ──
        is_enforcement_basis = LAFOrchestratorDocumentMixin._is_enforcement_closing_basis(fn, full_path, subdir)
        if any(k in fn for k in _CLOSING_BASIS_KEYWORDS) or is_enforcement_basis:
            # Exclude templates/drafts
            if "範本" not in fn and "模板" not in fn and "草稿" not in fn:
                out.setdefault("closing_basis_files", []).append(full_path)

        # ── Office receipt / court stamps ──
        if any(k in fn for k in _OFFICE_RECEIPT_KEYWORDS):
            out.setdefault("office_receipt_files", []).append(full_path)

        # ── Closing fee files ──
        if any(k in fn for k in _CLOSING_FEE_KEYWORDS):
            out.setdefault("closing_fee_files", []).append(full_path)

        # ── Change review notice ──
        if any(k in fn for k in _CHANGE_REVIEW_KEYWORDS):
            out.setdefault("change_review_notice_files", []).append(full_path)

        # ── Withdrawal files ──
        if any(k in fn for k in _WITHDRAWAL_SIGNED_PDF_KEYWORDS):
            out.setdefault("withdrawal_files", []).append(full_path)

    # ==================================================================
    # Withdrawal Doc Helpers
    # ==================================================================

    @staticmethod
    def _get_withdrawal_pdf_candidates(docs: dict) -> List[str]:
        """Get signed withdrawal PDF candidates from scanned docs."""
        candidates = []
        for f in (docs.get("withdrawal_files") or []):
            fn = os.path.basename(str(f or ""))
            ext = Path(fn).suffix.lower()
            if ext != ".pdf":
                continue
            # Exclude templates/unsigned
            if any(k in fn for k in _WITHDRAWAL_TEMPLATE_KEYWORDS):
                continue
            if "未簽" in fn or "unsigned" in fn.lower():
                continue
            candidates.append(f)
        return sorted(candidates)

    @staticmethod
    def _get_withdrawal_non_pdf_candidates(docs: dict) -> List[str]:
        """Get non-PDF withdrawal items (Word, images of unsigned docs)."""
        candidates = []
        for f in (docs.get("withdrawal_files") or []):
            fn = os.path.basename(str(f or ""))
            ext = Path(fn).suffix.lower()
            if ext == ".pdf":
                continue
            if any(k in fn for k in _WITHDRAWAL_TEMPLATE_KEYWORDS):
                continue
            candidates.append(f)
        return sorted(candidates)

    @staticmethod
    def _get_withdrawal_template_candidates(docs: dict) -> List[str]:
        """Get withdrawal template files (mother copies / 範本)."""
        candidates = []
        for f in (docs.get("withdrawal_files") or []):
            fn = os.path.basename(str(f or ""))
            if any(k in fn for k in _WITHDRAWAL_TEMPLATE_KEYWORDS):
                candidates.append(f)
        return sorted(candidates)

    # ==================================================================
    # Closing Basis Helpers
    # ==================================================================

    @staticmethod
    def _is_enforcement_closing_basis(fn: str, full_path: str = "", subdir: str = "") -> bool:
        text = f"{fn} {full_path} {subdir}"
        in_judgment_folder = "10_判決書" in text or "/判決書/" in text.replace("\\", "/")
        is_enforcement_case = any(k in text for k in ("強制執行", "司執", "執行"))
        has_enforcement_doc = any(k in fn for k in _ENFORCEMENT_CLOSING_KEYWORDS)
        return bool(in_judgment_folder and is_enforcement_case and has_enforcement_doc)

    @staticmethod
    def _closing_basis_sort_key(path: str) -> tuple:
        """Sort key for closing basis files: prioritize 判決 > 裁定 > 不起訴處分書."""
        path_text = str(path or "")
        fn = os.path.basename(str(path or ""))
        # Priority: lower = earlier
        if "判決" in fn:
            priority = 0
        elif "裁定" in fn:
            priority = 1
        elif "確定證明書" in fn:
            priority = 2
        elif "不起訴處分書" in fn:
            priority = 3
        elif "追加起訴書" in fn:
            priority = 4
        elif "起訴書" in fn:
            priority = 4
        elif "併辦意旨書" in fn:
            priority = 5
        elif "和解" in fn or "調解" in fn:
            priority = 6
        elif LAFOrchestratorDocumentMixin._is_enforcement_closing_basis(fn, str(path or "")):
            priority = 7
        else:
            priority = 9
        folder_priority = 0 if "10_判決書" in path_text else 1
        return (priority, folder_priority, fn)

    def _sort_closing_basis_files(self, files: List[str]) -> List[str]:
        """Sort closing basis files by document type priority."""
        return sorted(files or [], key=self._closing_basis_sort_key)

    def _infer_closing_metadata_from_docs(
        self,
        basis_files: List[str],
        client_name: str = "",
        folder_path: str = "",
    ) -> dict:
        """
        Infer closing metadata (court info, case outcome) from
        judgment/ruling filenames and content.

        Returns dict with keys like court_kind, court_name,
        court_case_year, court_case_code, court_case_no,
        closing_result, closing_result_doc, closing_doc_type, etc.
        """
        meta: Dict[str, str] = {}
        if not basis_files:
            return meta

        # Use best (first) basis file
        best = basis_files[0]
        fn = os.path.basename(str(best or ""))

        # Determine doc type
        if "判決" in fn:
            meta["closing_doc_type"] = "判決"
        elif "裁定" in fn:
            meta["closing_doc_type"] = "裁定"
        elif "不起訴處分書" in fn:
            meta["closing_doc_type"] = "不起訴處分書"
        elif "追加起訴書" in fn:
            meta["closing_doc_type"] = "追加起訴書"
        elif "起訴書" in fn:
            meta["closing_doc_type"] = "起訴書"
        elif "併辦意旨書" in fn:
            meta["closing_doc_type"] = "併辦意旨書"
        elif "和解" in fn:
            meta["closing_doc_type"] = "和解筆錄"
        elif "調解" in fn:
            meta["closing_doc_type"] = "調解筆錄"
        elif self._is_enforcement_closing_basis(fn, str(best or ""), folder_path):
            meta["closing_doc_type"] = "執行命令"
        else:
            meta["closing_doc_type"] = ""

        meta["closing_result_doc"] = str(best)

        # Try to extract court info from filename
        # Pattern: 臺灣花蓮地方法院114年度原訴字第000024號判決
        court_pattern = re.compile(
            r"(臺灣.*?(?:地方|高等|最高)(?:法院|行政法院))"
        )
        m = court_pattern.search(fn)
        if m:
            meta["court_name"] = m.group(1)

        # Case number pattern: 114年度原訴字第000024號
        case_pattern = re.compile(
            r"(\d{2,3})\s*年度?\s*([^\d\s第]+?)\s*字?\s*第?\s*(\d+)\s*號"
        )
        m = case_pattern.search(fn)
        if m:
            meta["court_case_year"] = m.group(1)
            meta["court_case_code"] = m.group(2)
            meta["court_case_no"] = m.group(3)

        # Infer court kind from folder path
        fp = str(folder_path or "").replace("\\", "/")
        if "/刑事/" in fp:
            meta["court_kind"] = "刑事"
        elif "/民事/" in fp:
            meta["court_kind"] = "民事"
        elif "/家事/" in fp:
            meta["court_kind"] = "家事"
        elif "/行政/" in fp:
            meta["court_kind"] = "行政"

        # Infer closing result from doc type
        if meta.get("closing_doc_type") == "判決":
            meta.setdefault("closing_result", "判決")
        elif meta.get("closing_doc_type") == "和解筆錄":
            meta.setdefault("closing_result", "和解")
        elif meta.get("closing_doc_type") == "調解筆錄":
            meta.setdefault("closing_result", "調解")
        elif meta.get("closing_doc_type") == "不起訴處分書":
            meta.setdefault("closing_result", "不起訴")

        return meta

    # ==================================================================
    # DB Query Wrapper
    # ==================================================================

    def _query_db(self, sql: str, params: tuple = (), as_dict: bool = True) -> List[dict]:
        """Thin wrapper around self.db.fetch_all with safe fallback."""
        db = getattr(self, "db", None)
        if not db:
            return []
        try:
            result = db.fetch_all(sql, params, as_dict=as_dict)
            return list(result or [])
        except Exception as e:
            logger.warning("_query_db failed: %s", e)
            return []
