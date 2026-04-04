#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf-namer/action.py
PDF 自動命名技能 (PyMuPDF + RapidOCR)
"""

import argparse
import fitz  # PyMuPDF
import os
import re
import sys
import logging
from collections import Counter, defaultdict
from pathlib import Path
import base64
import json
import io
import time
import subprocess
import threading
import uuid
import tempfile
from typing import Optional, Tuple
from datetime import datetime, timedelta

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.case_path_mapper import default_case_roots, preferred_case_roots
from skills.bridge.shared_utils.case_number_utils import extract_case_number as _extract_case_number, RE_CASE_NUMBER
from skills.bridge.shared_utils.court_utils import extract_court_name as _extract_court_name, RE_COURT_NAME

try:
    from rapidocr_onnxruntime import RapidOCR
    HAS_OCR = True
except ImportError:
    HAS_OCR = False

try:
    from PIL import Image
    HAS_PIL = True
except Exception:
    HAS_PIL = False

try:
    import requests
    HAS_REQUESTS = True
except Exception:
    HAS_REQUESTS = False

try:
    import shutil as _shutil
    HAS_TESSERACT = bool(_shutil.which("tesseract"))
except Exception:
    HAS_TESSERACT = False

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("pdf-namer")

JOB_DIR = Path(__file__).resolve().parent / "_bg_jobs"
try:
    _VISION_CONCURRENCY = int(os.environ.get("MAGI_PDF_NAMER_VISION_MAX_WORKERS", "1") or "1")
except Exception:
    _VISION_CONCURRENCY = 1
_VISION_CONCURRENCY = max(1, min(_VISION_CONCURRENCY, 2))
_VISION_SEMAPHORE = threading.BoundedSemaphore(_VISION_CONCURRENCY)


def _with_vision_slot(fn, *args, **kwargs):
    with _VISION_SEMAPHORE:
        return fn(*args, **kwargs)

# Initialize OCR engine globally if available
ocr_engine = RapidOCR() if HAS_OCR else None

# --- Configuration ---

# Regex patterns (Reuse from pdf-bookmarker)
RE_DATE = re.compile(r"((?:1\d{2}|[89]\d))\s*([年\.\-/])\s*([01]?\d)\s*([月\.\-/])\s*([0-3]?\d)\s*([日]?)")
RE_DEFENDANT = re.compile(
    r"(?:被\s*告|原\s*告|聲\s*請\s*人|相\s*對\s*人|受\s*刑\s*人|"
    r"抗\s*告\s*人|上\s*訴\s*人|受\s*文\s*者|移送被告|當\s*事\s*人|"
    r"即\s*債\s*權\s*人|即\s*債\s*務\s*人|債\s*權\s*人|債\s*務\s*人)"
    r"\s*[:：]?\s*([\u4e00-\u9fffA-Za-z·\-]{2,20})"
)
RE_AD_DATE = re.compile(r"(20\d{2})\s*([年\.\-/])\s*([01]?\d)\s*([月\.\-/])\s*([0-3]?\d)\s*([日]?)")
RE_ANY_ROC_DATE = re.compile(r"(\d{2,3})\s*([年\.\-/])\s*([01]?\d)\s*([月\.\-/])\s*([0-3]?\d)\s*([日]?)")

DOC_TYPES = [
    # 筆錄
    "訊問筆錄", "讯问笔录", "調查筆錄", "调查笔录", "準備程序筆錄", "审判笔录", "勘驗筆錄",
    # 法院通知 (Court Notices)
    "庭通知書", "法院通知", "開庭通知", "期日通知", "傳票", "通知書",
    # 法院裁判 / 命令 — must precede 對造/相對人 to avoid false match
    "支付命令", "判決", "判决", "裁定",
    "起訴書", "起诉书", "不起訴處分書", "不起诉处分书", "聲請簡易判決處刑書", "声请简易判决处刑书",
    "声请书", "聲請書", "陈报状", "陳報狀", "答辯狀", "答辩状", "抗告狀", "上訴狀",
    # 對造書狀 (Opponent) — after specific doc types to avoid false match on 相對人
    "對造書狀", "對造", "相對人", "原告書狀", "被告訴狀",
    # 令狀
    "搜索票", "拘票", "押票", "提票", "通緝書", "通缉书",
    # 證物等
    "扣押物品目錄表", "扣押物品目录表", "扣押物品收據", "贓證物品清單", "赃证物品清单",
    "委任狀", "委任书", "選任辯護人委任書", "选任辩护人委任书",
    "驗傷診斷書", "相驗屍體證明書"
]

DOC_TYPE_MAP = {
    # 筆錄
    "訊問筆錄": "訊問筆錄", "讯问笔录": "訊問筆錄",
    "調查筆錄": "調查筆錄", "调查笔录": "調查筆錄",
    "準備程序筆錄": "準備程序筆錄", "审判笔录": "審判筆錄", "審判筆錄": "審判筆錄",
    "勘驗筆錄": "勘驗筆錄",
    # 對造書狀
    "對造書狀": "對造_書狀", "對造": "對造_書狀", "相對人": "對造_書狀", 
    "原告書狀": "對造_書狀", "被告訴狀": "對造_書狀",
    # 法院通知
    "庭通知書": "法院_通知", "法院通知": "法院_通知", "開庭通知": "法院_通知", "期日通知": "法院_通知", "傳票": "法院_傳票", "通知書": "法院_通知",
    # 書狀
    "起訴書": "起訴書", "起诉书": "起訴書",
    "不起訴處分書": "不起訴處分書", "不起诉处分书": "不起訴處分書",
    "聲請簡易判決處刑書": "聲請簡易判決處刑書", "声请简易判决处刑书": "聲請簡易判決處刑書",
    "判決": "判決", "判决": "判決",
    "支付命令": "支付命令",
    "裁定": "裁定",
    "聲請書": "聲請書", "声请书": "聲請書",
    "陳報狀": "陳報狀", "陈报状": "陳報狀",
    "答辯狀": "答辯狀", "答辩状": "答辯狀",
    "抗告狀": "抗告狀", "上訴狀": "上訴狀",
    # 令狀
    "搜索票": "搜索票", "拘票": "拘票", "押票": "押票",
    "提票": "提票", "通緝書": "通緝書", "通缉书": "通緝書",
    # 證物等
    "扣押物品目錄表": "扣押物品目錄表", "扣押物品目录表": "扣押物品目錄表",
    "扣押物品收據": "扣押物品收據",
    "贓證物品清單": "贓證物品清單", "赃证物品清单": "贓證物品清單",
    "委任狀": "委任狀", "委任书": "委任狀",
    "選任辯護人委任書": "委任狀", "选任辩护人委任书": "委任狀",
    "驗傷診斷書": "驗傷診斷書", "相驗屍體證明書": "相驗屍體證明書"
}

_CASE_ROOTS = preferred_case_roots(include_closed=False)
_FALLBACK_CASE_ROOTS = default_case_roots(include_closed=False)
CASE_ROOT = os.environ.get(
    "MAGI_CASE_ROOT",
    _CASE_ROOTS[0] if _CASE_ROOTS else (_FALLBACK_CASE_ROOTS[0] if _FALLBACK_CASE_ROOTS else str(Path.home() / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件")),
)
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
LEARNED_RULES_PATH = os.path.join(SKILL_DIR, "_learned_filename_rules.json")
CORRECTIONS_PATH = os.path.join(SKILL_DIR, "_corrections.json")

_LEARNED_RULES_CACHE: Optional[dict] = None

_DOC_TYPE_HINTS = [
    ("預付酬金領款單掛號郵件收件回執", "收據"),
    ("預付酬金領款單", "收據"),
    ("領款單回執", "收據"),
    ("掛號郵件收件回執", "收據"),
    ("調解不成立證明書", "法院通知"),
    ("檢察官補充理由書", "書狀_對造"),
    ("檢察官", "書狀_對造"),
    ("消費者債務清理更生聲請狀", "債清_書狀"),
    ("更生聲請狀", "債清_書狀"),
    ("無償委任證明書", "無償委任資料"),
    ("委任證明書", "無償委任資料"),
    ("繳費收據", "閱卷"),
    ("閱卷", "閱卷"),
    ("收據", "收據"),
    ("裁定", "裁定"),
    ("通知", "法院通知"),
]

_TOKEN_STOPWORDS = {
    "存底",
    "已簽名",
    "已用印",
    "正本",
    "影本",
    "掃描",
    "文件",
    "資料",
}

_FAST_DOWNGRADE_RECEIPT_KEYWORDS = (
    "預付酬金領款單掛號郵件收件回執",
    "預付酬金領款單",
    "領款單回執",
    "領款單",
    "掛號郵件收件回執",
)


def _strip_date_prefix(name: str) -> str:
    s = os.path.splitext(os.path.basename(name or ""))[0]
    s = re.sub(r"^\s*20\d{6}[\s_]*", "", s)
    return s.strip()


def _normalize_filename_key(name: str) -> str:
    s = _strip_date_prefix(name)
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    s = re.sub(r"[\s_\-]+", "", s)
    return s.strip()


def _tokenize_filename(name: str) -> list[str]:
    s = _strip_date_prefix(name)
    s = re.sub(r"[（(][^）)]*[）)]", " ", s)
    s = s.replace("_", " ").replace("-", " ")
    tokens: list[str] = []
    for tok in re.findall(r"[\u4e00-\u9fffA-Za-z]{2,}", s):
        t = tok.strip()
        if (not t) or (t in _TOKEN_STOPWORDS):
            continue
        tokens.append(t)
        # For long Chinese token, also add useful sub-keywords
        if re.fullmatch(r"[\u4e00-\u9fff]{6,}", t):
            for kw in _DOC_TYPE_HINTS:
                if kw[0] in t:
                    tokens.append(kw[0])
    return tokens


def _extract_name_from_filename(filename: str) -> Optional[str]:
    base = _strip_date_prefix(filename)
    # Prefer parentheses name first — handle common format （name；description）.
    m = re.search(r"[（(]([^）)]+)[）)]", base)
    if m:
        inner = m.group(1)
        # Take the first segment before ；or ; (the party name part).
        party = re.split(r"[；;]", inner)[0].strip()
        # Clean whitespace and validate as a plausible name (2-24 CJK/alpha chars).
        party = re.sub(r"\s+", "", party)
        if party and re.match(r"^[\u4e00-\u9fffA-Za-z·\-]{2,24}$", party):
            if not re.search(r"(聲請狀|理由書|證明書|收據|通知|裁定|主文)", party):
                return party
    # Fallback to pattern "...書(姓名)" — skip known non-name tokens.
    _NON_NAME_TOKENS = {"花蓮", "臺灣", "台灣", "地方", "法院", "檢察", "最高", "高等"}
    for m2 in re.finditer(r"([\u4e00-\u9fff]{2,5})", base):
        candidate = m2.group(1)
        if candidate not in _NON_NAME_TOKENS and not re.search(r"(法院|檢察|地方|字第|年度)", candidate):
            return candidate
    return None


def _infer_doc_type_from_hints(filename: str) -> Optional[str]:
    base = _strip_date_prefix(filename)
    for kw, dt in _DOC_TYPE_HINTS:
        if kw in base:
            return dt
    return None


def _is_fast_downgrade_receipt(filename: str, pdf_path: str = "") -> bool:
    """
    Fast downgrade for legal-aid receipt-like docs:
    still parse date/name/type, but skip strict stamp verification path.
    """
    mode = os.environ.get("MAGI_PDF_NAMER_FAST_DOWNGRADE_RECEIPT", "1").strip().lower()
    if mode in {"0", "false", "no", "off"}:
        return False
    base = _strip_date_prefix(filename or "")
    p = str(pdf_path or "")
    if any(k in base for k in _FAST_DOWNGRADE_RECEIPT_KEYWORDS):
        return True
    # Some files only show this signal in folder path.
    if ("回執" in p) and ("領款單" in p):
        return True
    return False


def _category_from_subfolder(subfolder: str, filename: str = "") -> str:
    clean = re.sub(r"^\d+_", "", (subfolder or "").strip())
    bn = os.path.basename(filename or "")
    if ("對方歷次書狀" in clean) or ("對造" in clean):
        return "書狀_對造"
    if "我方歷次書狀" in clean:
        if ("消費者債務清理" in bn) or ("更生" in bn) or ("清算" in bn):
            return "債清_書狀"
        return "書狀_我方"
    if "無償委任資料" in clean:
        return "無償委任資料"
    if "閱卷資料" in clean:
        return "閱卷"
    if ("法院通知" in clean) or ("程序裁定" in clean):
        return "裁定" if "裁定" in bn else "法院通知"
    if "法扶資料" in clean:
        return "法扶表單"
    if "回執" in clean:
        return "收據"
    if "判決書" in clean:
        return "判決"
    if "證據資料" in clean:
        return "證據"
    if "筆錄" in clean:
        return "筆錄"
    return ""


def build_filename_learning_rules(
    case_root: str = CASE_ROOT,
    max_samples: int = 5000,
    min_token_count: int = 2,
    min_purity: float = 0.62,
) -> dict:
    """
    Build lightweight token->doc_type rules from already-filed PDFs.
    This is local self-learning from existing Synology folders.
    """
    token_counts: dict[str, Counter] = defaultdict(Counter)
    exact_counts: dict[str, Counter] = defaultdict(Counter)
    label_counts: Counter = Counter()
    sample_count = 0

    if os.path.isdir(case_root):
        for root, _, files in os.walk(case_root):
            if sample_count >= max_samples:
                break
            subfolder = os.path.basename(root)
            for fn in files:
                if sample_count >= max_samples:
                    break
                if (not fn.lower().endswith(".pdf")) or fn.startswith("."):
                    continue
                label = _category_from_subfolder(subfolder, fn)
                if not label:
                    continue
                label_counts[label] += 1
                sample_count += 1
                key = _normalize_filename_key(fn)
                if key:
                    exact_counts[key][label] += 1
                for tok in _tokenize_filename(fn):
                    token_counts[tok][label] += 1

    # Boost with recent manual corrections (higher supervision weight).
    if os.path.exists(CORRECTIONS_PATH):
        try:
            corrections = json.loads(Path(CORRECTIONS_PATH).read_text(encoding="utf-8") or "[]")
            for item in corrections[-500:]:
                fn = os.path.basename(str(item.get("filename") or ""))
                sf = str(item.get("subfolder") or "")
                label = _category_from_subfolder(sf, fn)
                if not (fn and label):
                    continue
                label_counts[label] += 1
                sample_count += 1
                key = _normalize_filename_key(fn)
                if key:
                    exact_counts[key][label] += 3
                for tok in _tokenize_filename(fn):
                    token_counts[tok][label] += 3
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 356, exc_info=True)

    rules: list[dict] = []
    for token, cnt in token_counts.items():
        total = int(sum(cnt.values()))
        if total < int(min_token_count):
            continue
        best_label, best_count = cnt.most_common(1)[0]
        purity = float(best_count) / float(total)
        if purity < float(min_purity):
            continue
        rules.append(
            {
                "token": token,
                "label": best_label,
                "count": int(best_count),
                "total": total,
                "purity": round(purity, 4),
                "weight": round((best_count * purity), 3),
            }
        )

    rules.sort(key=lambda x: (x.get("weight", 0.0), x.get("count", 0)), reverse=True)

    exact_rules: dict[str, dict] = {}
    for key, cnt in exact_counts.items():
        best_label, best_count = cnt.most_common(1)[0]
        total = int(sum(cnt.values()))
        purity = float(best_count) / float(total) if total else 0.0
        if purity < 0.6:
            continue
        exact_rules[key] = {
            "label": best_label,
            "count": int(best_count),
            "total": total,
            "purity": round(purity, 4),
        }

    payload = {
        "generated_at": datetime.now().isoformat(),
        "case_root": case_root,
        "sample_count": int(sample_count),
        "label_counts": dict(label_counts),
        "rules": rules,
        "exact_rules": exact_rules,
    }
    try:
        Path(LEARNED_RULES_PATH).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"寫入學習規則失敗: {e}")
    return payload


def _load_filename_learning_rules() -> dict:
    global _LEARNED_RULES_CACHE
    if _LEARNED_RULES_CACHE is not None:
        return _LEARNED_RULES_CACHE
    if os.path.exists(LEARNED_RULES_PATH):
        try:
            _LEARNED_RULES_CACHE = json.loads(Path(LEARNED_RULES_PATH).read_text(encoding="utf-8") or "{}")
            return _LEARNED_RULES_CACHE or {}
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 418, exc_info=True)
    _LEARNED_RULES_CACHE = build_filename_learning_rules()
    return _LEARNED_RULES_CACHE or {}


def _infer_doc_type_from_learning(filename: str) -> Optional[str]:
    learned = _load_filename_learning_rules()
    if not learned:
        return None
    key = _normalize_filename_key(filename)
    exact = (learned.get("exact_rules") or {}).get(key)
    if isinstance(exact, dict) and exact.get("label"):
        return str(exact.get("label"))

    score: Counter = Counter()
    rule_by_token = {str(r.get("token")): r for r in (learned.get("rules") or []) if r.get("token")}
    for tok in _tokenize_filename(filename):
        rr = rule_by_token.get(tok)
        if not rr:
            continue
        label = str(rr.get("label") or "")
        weight = float(rr.get("weight") or 0.0)
        if label and weight > 0:
            score[label] += weight

    if not score:
        return None
    best_label, best_score = score.most_common(1)[0]
    if best_score < 1.0:
        return None
    return best_label


def _extract_roc_date(text: str):
    """Extract ROC date and return formatted string YYYYMMDD"""
    # Normalize OCR spacing artifacts (e.g. "115. 3. 2 0" → "115.3.20")
    text = _normalize_date_text(text)
    m = RE_DATE.search(text)
    if m:
        # Groups: 1=Year, 2=Sep, 3=Month, 4=Sep, 5=Day, 6=Suffix
        y_roc = int(m.group(1).strip())
        y_ad = y_roc + 1911
        mo = int(m.group(3).strip())
        d = int(m.group(5).strip())
        return f"{y_ad}{mo:02d}{d:02d}"
    return None

def _extract_ad_date(text: str) -> Optional[str]:
    """Extract AD date and return formatted string YYYYMMDD"""
    s = _normalize_date_text(text or "")
    m = RE_AD_DATE.search(s)
    if not m:
        return None
    try:
        y = int(m.group(1).strip())
        mo = int(m.group(3).strip())
        d = int(m.group(5).strip())
        if mo < 1 or mo > 12 or d < 1 or d > 31:
            return None
        return f"{y}{mo:02d}{d:02d}"
    except Exception:
        return None

def _extract_any_date(text: str) -> Optional[str]:
    """
    Best-effort: AD date first, then ROC date.
    """
    s = _normalize_date_text(text or "")
    d = _extract_ad_date(s)
    if d:
        return d
    # ROC date
    m = RE_ANY_ROC_DATE.search(s)
    if not m:
        return None
    try:
        y_roc = int(m.group(1).strip())
        # Heuristic: 2-3 digits looks like ROC year.
        y_ad = y_roc + 1911 if y_roc < 1911 else y_roc
        mo = int(m.group(3).strip())
        d2 = int(m.group(5).strip())
        if mo < 1 or mo > 12 or d2 < 1 or d2 > 31:
            return None
        return f"{y_ad}{mo:02d}{d2:02d}"
    except Exception:
        return None

def _extract_doc_type(text: str):
    """Identify document type keyword from text"""
    header_text = text[:1000] 
    for keyword in DOC_TYPES:
        if keyword in header_text:
            return DOC_TYPE_MAP.get(keyword, keyword)
    return None

def _is_envelope_page(text: str) -> bool:
    """Detect if a page is a court envelope (公文封) rather than actual content.

    An envelope page has NO structured court form fields (案號/案由/當事人/應到).
    A court notice page has those fields even if it also contains some envelope text
    at the top (common with scanned docs).
    """
    t = (text or "")[:2000]
    # If the page has structured court form fields, it's NOT an envelope
    content_markers = ["案號", "案由", "當事人", "應到", "期日", "種類", "庭通知書", "傳票"]
    content_hits = sum(1 for m in content_markers if m in t)
    if content_hits >= 2:
        return False

    envelope_markers = [
        "受送達人住居所",
        "受送達人居住所",
        "受送達人姓名",
        "送達人住居所代收文件處",
        "送達代收人",
        "郵務送達",
        "寄存送達",
        "公文封",
        "送達主旨",
        "訴訟當事人注意事項",
    ]
    hits = sum(1 for m in envelope_markers if m in t)
    # Also detect template text like "被告聲請人或相對人" (instructions, not actual party names)
    if "或被告" in t or "聲請人或相對人" in t or "或相對人" in t:
        hits += 1
    return hits >= 2


def _extract_name(text: str, default_name: str = "Unknown"):
    """Extract party name from court document text."""
    # Skip pure envelope pages
    if _is_envelope_page(text):
        return default_name
    # Use finditer to skip template/instruction matches (e.g. "原告或被告聲請人或相對人")
    _bad_fragments = {"或被告", "或相對人", "聲請人或", "證人", "定人用", "或定人"}
    for m in RE_DEFENDANT.finditer(text[:1500]):
        name = m.group(1).strip()
        name = re.sub(r"[，,。;；].*", "", name)
        if any(k in name for k in _bad_fragments):
            continue
        # Skip single-char or obviously wrong names
        if len(name) < 2:
            continue
        return name
    return default_name

def _ocr_page_rapid(page):
    """Use RapidOCR to extract text from page image"""
    if not ocr_engine:
        return ""
    try:
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes("png")
        result, _ = ocr_engine(img_bytes)
        if not result:
            return ""
        texts = [line[1] for line in result]
        return "\n".join(texts)
    except Exception as e:
        logger.error(f"OCR failed for page {page.number}: {e}")
        return ""

def _ocr_image_bytes(img_bytes: bytes) -> str:
    if not ocr_engine or not img_bytes:
        return ""
    try:
        result, _ = ocr_engine(img_bytes)
        if not result:
            return ""
        texts = [line[1] for line in result if isinstance(line, (list, tuple)) and len(line) >= 2]
        return "\n".join([t for t in texts if t])
    except Exception:
        return ""

def _normalize_date_text(text: str) -> str:
    """
    Normalize OCR text for date extraction.

    RapidOCR often inserts spaces between digits (e.g. "113. 10. 0 7").
    This helper collapses digit-spaces and trims spaces around common separators.
    """
    s = (text or "").strip()
    if not s:
        return ""
    # Collapse spaces between digits: "0 7" -> "07"
    s = re.sub(r"(?<=\d)\s+(?=\d)", "", s)
    # Normalize spaces around separators: "113. 10. 07" -> "113.10.07"
    s = re.sub(r"\s*([./\-])\s*", r"\1", s)
    return s


def _ocr_image_bytes_tesseract(img_bytes: bytes, timeout_sec: int = 6, psm: int = 6) -> str:
    """
    OCR fallback using local Tesseract (chi_tra+eng) for stamp-only regions.
    """
    if (not HAS_TESSERACT) or (not img_bytes):
        return ""
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            tmp_path = f.name
            f.write(img_bytes)
        cmd = [
            "tesseract",
            tmp_path,
            "stdout",
            "-l",
            "chi_tra+eng",
            "--psm",
            str(int(psm or 6)),
            "-c",
            "preserve_interword_spaces=1",
        ]
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=max(2, int(timeout_sec)))
        return (p.stdout or "").strip()
    except Exception:
        return ""
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 640, exc_info=True)


def _extract_receipt_month_day_from_text(text: str) -> Optional[Tuple[int, int]]:
    """
    Extract month/day when OCR drops year, e.g. '年02月13' or '02月13日'.
    """
    s = _normalize_date_text(text or "")
    if not s:
        return None
    patterns = [
        re.compile(r"(?:\d{2,3}\s*[年./-]\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日?"),
        re.compile(r"(?:\d{2,3}\s*[年./-]\s*)?(\d{1,2})\s*[./-]\s*(\d{1,2})"),
    ]
    for rg in patterns:
        m = rg.search(s)
        if not m:
            continue
        try:
            mo = int(m.group(1))
            dd = int(m.group(2))
            if 1 <= mo <= 12 and 1 <= dd <= 31:
                return mo, dd
        except Exception:
            continue
    return None


def _resolve_partial_md_to_ymd(month: int, day: int, ref_dts: list[datetime], window_days: int = 120) -> Optional[str]:
    if (month < 1) or (month > 12) or (day < 1) or (day > 31):
        return None
    if not ref_dts:
        return None
    candidates: list[datetime] = []
    for rd in ref_dts:
        for y in (rd.year - 1, rd.year, rd.year + 1):
            try:
                candidates.append(datetime(y, month, day))
            except Exception:
                continue
    if not candidates:
        return None
    best_dt = None
    best_gap = None
    for c in candidates:
        gap = min(abs((c - rd).days) for rd in ref_dts)
        if (best_gap is None) or (gap < best_gap):
            best_gap = gap
            best_dt = c
    if (best_dt is None) or (best_gap is None) or (best_gap > int(window_days)):
        return None
    return best_dt.strftime("%Y%m%d")

def _crop_png_bytes(png_bytes: bytes, *, x0: float, y0: float, x1: float, y1: float) -> bytes:
    if (not HAS_PIL) or (not png_bytes):
        return b""
    try:
        im = Image.open(io.BytesIO(png_bytes)).convert("RGB")  # type: ignore[name-defined]
        w, h = im.size
        box = (
            int(max(0, min(w, w * float(x0)))),
            int(max(0, min(h, h * float(y0)))),
            int(max(0, min(w, w * float(x1)))),
            int(max(0, min(h, h * float(y1)))),
        )
        crop = im.crop(box)
        out = io.BytesIO()
        crop.save(out, format="PNG")
        return out.getvalue()
    except Exception:
        return b""

def _find_receipt_date_from_text(text: str) -> Optional[str]:
    """
    只要在章戳/收文區塊中看到日期，就視為收文章日期（檔名日期優先）。
    """
    s = _normalize_date_text(text or "")
    if not s:
        return None

    # Prefer labeled receipt dates (more precise than "any date" when multiple dates appear).
    # Common patterns in court/law-firm stamps: 收文, 收件, 收發, 到達.
    receipt_labels = [
        r"收文日期[：:\s]*",
        r"收件日期[：:\s]*",
        r"收發日期[：:\s]*",
        r"收狀日期[：:\s]*",
        r"收受日期[：:\s]*",
        r"到達日期[：:\s]*",
        r"收文章[：:\s]*",
        r"收文[：:\s]*",
        r"收件[：:\s]*",
        r"收發[：:\s]*",
    ]

    def _find_labeled_receipt_date(txt: str) -> Optional[str]:
        t = _normalize_date_text(txt or "")
        if not t:
            return None
        for label_re in receipt_labels:
            # ROC: 114年2月15日 / 114.02.15 / 114/02/15
            m = re.search(label_re + r"(\d{2,3})\s*[年.\-/]\s*(\d{1,2})\s*[月.\-/]\s*(\d{1,2})\s*日?", t)
            if m:
                try:
                    y = int(m.group(1)) + 1911
                    mo = int(m.group(2))
                    d = int(m.group(3))
                    if 1 <= mo <= 12 and 1 <= d <= 31:
                        return f"{y}{mo:02d}{d:02d}"
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 750, exc_info=True)
            # AD: 2025-02-15 / 2025/02/15
            m2 = re.search(label_re + r"(20\d{2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{1,2})", t)
            if m2:
                try:
                    y = int(m2.group(1))
                    mo = int(m2.group(2))
                    d = int(m2.group(3))
                    if 1 <= mo <= 12 and 1 <= d <= 31:
                        return f"{y}{mo:02d}{d:02d}"
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 761, exc_info=True)
        return None

    def _plausible(yyyymmdd: str) -> bool:
        try:
            dt = datetime.strptime(yyyymmdd, "%Y%m%d")
            now = datetime.now()
            # Most office workflows deal with recent documents; keep a generous window.
            if dt < (now - timedelta(days=365 * 10)):
                return False
            if dt > (now + timedelta(days=365)):
                return False
            return True
        except Exception:
            return False

    # If explicit keywords exist, prefer the nearest date.
    kw = ["收文日期", "收件日期", "收發日期", "收狀日期", "收受日期", "到達日期", "收文章", "收文", "收件", "收發"]
    if any(k in s for k in kw):
        d0 = _find_labeled_receipt_date(s)
        if d0 and _plausible(d0):
            return d0
        # Quick parse: any date in this snippet is likely the receipt date.
        d = _extract_any_date(s)
        if d and _plausible(d):
            return d
    # Without keywords, only accept a date when it's within a plausible window (avoid OCR/VLM hallucinations).
    d2 = _extract_any_date(s)
    if d2 and _plausible(d2):
        return d2
    return None

def _llava_extract_receipt_date(png_bytes: bytes, *, timeout_sec: int = 14) -> Optional[str]:
    """
    使用本機視覺模型做「收文章日期」判讀（優先於 OCR）。
    路徑：oMLX/GLM-OCR → Ollama vision chain。
    回覆格式要求：YYYYMMDD 或 NONE
    """
    if (not HAS_REQUESTS) or (not png_bytes):
        return None
    # Feature flag: allow turning off vision for speed/debug.
    if os.environ.get("MAGI_PDF_NAMER_USE_VISION", "1").strip() in {"0", "false", "no", "off"}:
        return None
    try:
        b64 = base64.b64encode(png_bytes).decode("utf-8")
        prompt = (
            "請逐字轉錄這張文件章戳/角落區域所有可見文字與數字，不要推論、不用解釋。\n"
            "若看不到任何文字才回覆 NONE。"
        )

        def _parse_date_result(out: str) -> Optional[str]:
            out = (out or "").strip()
            if not out or "NONE" in out.upper():
                return None
            m = re.search(r"(20\d{6})", out)
            if m:
                return m.group(1)
            return _extract_any_date(out)

        # ── Primary: oMLX (GLM-OCR or TAIDE-12b vision) ──
        try:
            from skills.bridge import melchior_client as _mc
            _chat_omlx = getattr(_mc, "_chat_omlx", None)
            _omlx_avail = getattr(_mc, "_omlx_available", None)
            if callable(_chat_omlx) and callable(_omlx_avail) and _omlx_avail():
                ocr_model = getattr(_mc, "OMLX_OCR_MODEL", "GLM-OCR-bf16")
                r = _chat_omlx(
                    prompt=prompt, model=ocr_model,
                    timeout=max(8, int(timeout_sec)),
                    temperature=0.0, max_tokens=1024, images=[b64],
                )
                if r.get("success") and r.get("response"):
                    d = _parse_date_result(r["response"])
                    if d:
                        return d
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 837, exc_info=True)

        # ── Fallback: Ollama vision chain ──
        chain = (os.environ.get("MAGI_PDF_NAMER_VISION_MODELS") or "").strip()
        if not chain:
            chain = (
                os.environ.get("MAGI_PDF_NAMER_VISION_MODEL", "taide-12b")
                or "taide-12b"
            ).strip()
        models = [m.strip() for m in chain.split(",") if m.strip()]

        def _try(mname: str) -> Optional[str]:
            retries = max(1, int(os.environ.get("MAGI_OLLAMA_BUSY_RETRIES", "2") or "2"))
            retry_sleep = float(os.environ.get("MAGI_OLLAMA_BUSY_RETRY_SEC", "0.8") or "0.8")
            omlx_base = (os.environ.get("MAGI_OMLX_VISION_URL") or os.environ.get("OMLX_URL") or "http://127.0.0.1:8082").rstrip("/")
            for attempt in range(retries):
                r = requests.post(
                    f"{omlx_base}/v1/chat/completions",
                    json={
                        "model": mname,
                        "messages": [
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": prompt},
                                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                                ],
                            }
                        ],
                        "stream": False,
                        "temperature": 0.0,
                    },
                    timeout=max(5, int(timeout_sec)),
                )
                if r.status_code != 200:
                    txt = (r.text or "")
                    if (r.status_code == 503) and ("server busy" in txt.lower() or "maximum pending requests exceeded" in txt.lower()):
                        time.sleep(retry_sleep)
                        continue
                    return None
                choices = (r.json() or {}).get("choices") or []
                content = ((choices[0].get("message") or {}).get("content") or "") if choices else ""
                return _parse_date_result(content)
            return None

        with _VISION_SEMAPHORE:
            for mname in models:
                out_date = _try(mname)
                if out_date:
                    return out_date
        return None
    except Exception:
        return None

def _extract_receipt_date_from_stamp(page, ref_dt: Optional[object] = None) -> Tuple[Optional[str], str]:
    """
    嘗試從第一頁的章戳區塊抓出收文章日期。
    回傳: (YYYYMMDD or None, method)
    """
    try:
        pix = page.get_pixmap(dpi=220)
        full_png = pix.tobytes("png")
    except Exception:
        return None, "render_failed"
    deadline_sec = max(4, int(os.environ.get("MAGI_PDF_NAMER_STAMP_DEADLINE_SEC", "10") or "10"))
    deadline_at = time.time() + deadline_sec

    # Candidate crops: cover common stamp placements (courts vary a lot).
    crops = [
        ("top_right", (0.55, 0.00, 1.00, 0.35)),
        ("right_strip", (0.72, 0.05, 1.00, 0.55)),
        ("top_center", (0.25, 0.00, 0.75, 0.35)),
        ("bottom_right", (0.55, 0.70, 1.00, 1.00)),
        ("bottom_left", (0.00, 0.70, 0.45, 1.00)),
        ("top_left", (0.00, 0.00, 0.45, 0.35)),
    ]

    def _enhance_for_ocr(png: bytes) -> bytes:
        if (not HAS_PIL) or (not png):
            return png
        try:
            from PIL import ImageOps, ImageEnhance  # type: ignore
            im = Image.open(io.BytesIO(png)).convert("L")  # grayscale
            im = ImageOps.autocontrast(im)
            # upscale to help OCR on low-res stamps
            w, h = im.size
            im = im.resize((int(w * 2.0), int(h * 2.0)))
            im = ImageEnhance.Sharpness(im).enhance(1.6)
            im = ImageEnhance.Contrast(im).enhance(1.4)
            out = io.BytesIO()
            im.save(out, format="PNG")
            return out.getvalue()
        except Exception:
            return png
    window_days = int(os.environ.get("MAGI_PDF_NAMER_STAMP_REF_WINDOW_DAYS", "7") or "7")
    ref_dts: list[datetime] = []
    if isinstance(ref_dt, datetime):
        ref_dts = [ref_dt]
    elif isinstance(ref_dt, (list, tuple)):
        for x in ref_dt:
            if isinstance(x, datetime):
                ref_dts.append(x)

    require_ref = os.environ.get("MAGI_PDF_NAMER_REQUIRE_REF_DATE", "1").strip().lower() in {"1", "true", "yes", "on"}

    def _plausible(yyyymmdd: str) -> bool:
        try:
            dt = datetime.strptime(yyyymmdd, "%Y%m%d")
            if ref_dts:
                # Receipt date should be within ~1 week of the file creation time.
                for rd in ref_dts:
                    if abs((dt - rd).days) <= int(window_days):
                        return True
                return False
            if require_ref:
                # Hard safety: when no reliable file timestamp is available, do not accept guessed receipt date.
                return False
            now = datetime.now()
            if dt < (now - timedelta(days=int(window_days))):
                return False
            if dt > (now + timedelta(days=int(window_days))):
                return False
            return True
        except Exception:
            return False

    # Prefer image-OCR first (still "vision"), because some VLMs may hallucinate dates.
    ocr_hits: list[tuple[str, str]] = []  # (date, crop_name)
    ocr_counts: dict[str, int] = {}
    partial_md_hits: list[tuple[int, int, str, str]] = []  # (month, day, method, crop_name)

    if HAS_OCR:
        for name, (x0, y0, x1, y1) in crops:
            if time.time() > deadline_at:
                break
            png = _crop_png_bytes(full_png, x0=x0, y0=y0, x1=x1, y1=y1) if HAS_PIL else full_png
            if not png:
                continue
            png2 = _enhance_for_ocr(png)
            ocr_text = _ocr_image_bytes(png2) or ""
            ocr_date = _find_receipt_date_from_text(ocr_text)
            if ocr_date and _plausible(ocr_date):
                ocr_hits.append((ocr_date, name))
                ocr_counts[ocr_date] = int(ocr_counts.get(ocr_date, 0)) + 1
                if ocr_counts.get(ocr_date, 0) >= 2:
                    break
            else:
                md = _extract_receipt_month_day_from_text(ocr_text)
                if md:
                    partial_md_hits.append((md[0], md[1], "stamp_rapid_partial", name))

            if not ocr_date:
                t_text = _ocr_image_bytes_tesseract(png2, timeout_sec=5, psm=6) or ""
                t_date = _find_receipt_date_from_text(t_text)
                if t_date and _plausible(t_date):
                    ocr_hits.append((t_date, name))
                    ocr_counts[t_date] = int(ocr_counts.get(t_date, 0)) + 1
                    if ocr_counts.get(t_date, 0) >= 2:
                        break
                else:
                    t_md = _extract_receipt_month_day_from_text(t_text)
                    if t_md:
                        partial_md_hits.append((t_md[0], t_md[1], "stamp_tesseract_partial", name))

    def _pick_best(hits: list[tuple[str, str, str]]) -> Optional[tuple[str, str]]:
        if not hits:
            return None
        # Prefer most frequent; tie-break by latest date (收文章日期通常較晚) then earliest crop order.
        from collections import Counter

        dates = [d for d, _, _ in hits]
        c = Counter(dates)
        best_cnt = max(c.values()) if c else 0
        best_dates = sorted([d for d, cnt in c.items() if cnt == best_cnt])
        chosen = best_dates[-1] if best_dates else dates[0]
        # Keep the first crop that produced the chosen date for traceability.
        for d, m, nm in hits:
            if d == chosen:
                return chosen, f"{m}:{nm}"
        d, m, nm = hits[0]
        return d, f"{m}:{nm}"

    if ocr_hits:
        # Convert to the common (date, method, crop_name) shape and pick best.
        best = _pick_best([(d, "stamp_ocr", nm) for d, nm in ocr_hits])
        if best:
            return best[0], best[1]

    if partial_md_hits:
        md_counter: dict[Tuple[int, int], int] = {}
        for mo, dd, _, _ in partial_md_hits:
            md_counter[(mo, dd)] = int(md_counter.get((mo, dd), 0)) + 1
        best_md = sorted(md_counter.items(), key=lambda x: x[1], reverse=True)[0][0]
        ymd = _resolve_partial_md_to_ymd(best_md[0], best_md[1], ref_dts=ref_dts, window_days=max(31, window_days * 2))
        partial_ok = False
        if ymd:
            try:
                dt = datetime.strptime(ymd, "%Y%m%d")
                pwin = max(21, int(os.environ.get("MAGI_PDF_NAMER_STAMP_PARTIAL_WINDOW_DAYS", "45") or "45"))
                if ref_dts:
                    partial_ok = any(abs((dt - rd).days) <= pwin for rd in ref_dts)
                else:
                    partial_ok = _plausible(ymd)
            except Exception:
                partial_ok = False
        if ymd and partial_ok:
            # Keep first matching source for traceability.
            for mo, dd, mth, nm in partial_md_hits:
                if (mo, dd) == best_md:
                    return ymd, f"{mth}:{nm}"
            return ymd, "stamp_partial"

    # Fallback: VLM (skip when running in parallel with main Vision to avoid oMLX contention)
    if os.environ.get("_MAGI_STAMP_SKIP_VLM", "").strip() in {"1", "true"}:
        return None, "not_found"
    vision_timeout = int(os.environ.get("MAGI_PDF_NAMER_STAMP_VISION_TIMEOUT", "6"))
    vlm_hits: list[tuple[str, str]] = []
    vlm_counts: dict[str, int] = {}
    try:
        max_vision_crops = max(1, int(os.environ.get("MAGI_PDF_NAMER_STAMP_MAX_VISION_CROPS", "3") or "3"))
    except Exception:
        max_vision_crops = 3

    def _reconcile_vlm_date(draw: str) -> Optional[str]:
        """
        Vision models sometimes read ROC year incorrectly while month/day is right.
        Reconcile by snapping month/day to the nearest reference date window.
        """
        if not draw:
            return None
        try:
            dt = datetime.strptime(str(draw), "%Y%m%d")
        except Exception:
            return None
        ymd = _resolve_partial_md_to_ymd(dt.month, dt.day, ref_dts=ref_dts, window_days=max(31, window_days * 2))
        if not ymd:
            return None
        try:
            ydt = datetime.strptime(ymd, "%Y%m%d")
            pwin = max(21, int(os.environ.get("MAGI_PDF_NAMER_STAMP_PARTIAL_WINDOW_DAYS", "45") or "45"))
            if ref_dts:
                return ymd if any(abs((ydt - rd).days) <= pwin for rd in ref_dts) else None
            return ymd if _plausible(ymd) else None
        except Exception:
            return None

    for idx, (name, (x0, y0, x1, y1)) in enumerate(crops):
        if time.time() > deadline_at:
            break
        if idx >= max_vision_crops:
            break
        png = _crop_png_bytes(full_png, x0=x0, y0=y0, x1=x1, y1=y1) if HAS_PIL else full_png
        if not png:
            continue
        png2 = _enhance_for_ocr(png)
        try:
            from vision_parser import extract_date_with_vision
            d2 = _with_vision_slot(extract_date_with_vision, png2, timeout_sec=vision_timeout)
        except ImportError:
            d2 = None

        if d2 and (not _plausible(d2)):
            d2 = _reconcile_vlm_date(d2)

        if not d2 or (not _plausible(d2)):
            d2 = _llava_extract_receipt_date(png2, timeout_sec=vision_timeout)

        if d2 and (not _plausible(d2)):
            d2 = _reconcile_vlm_date(d2)

        if not d2 or (not _plausible(d2)):
            continue
        vlm_hits.append((d2, name))
        vlm_counts[d2] = int(vlm_counts.get(d2, 0)) + 1
        if vlm_counts.get(d2, 0) >= 2:
            break
    if vlm_hits:
        best = _pick_best([(d, "stamp_vlm", nm) for d, nm in vlm_hits])
        if best:
            return best[0], best[1]

    return None, "not_found"

def generate_name_proposal(pdf_path: str, case_name: str = None, return_structured: bool = False):
    """Propose a filename following the standard convention:
    {YYYYMMDD} {法院全名}{案號}{文件類型}（{當事人}）.pdf

    Court docs: Page 1-2 = envelope, Page 3+ = actual content.
    Uses oMLX Vision on page 3 as primary, OCR as supplement for date.

    Args:
        return_structured: If True, returns dict with all extracted fields + filename.
                          If False, returns filename string (backward compat).
    """
    empty_result = {"filename": None, "date": None, "court": "", "case_number": "",
                    "doc_type": "", "party": "", "date_method": ""}

    if not os.path.exists(pdf_path):
        return empty_result if return_structured else None

    doc = fitz.open(pdf_path)
    if doc.needs_pass:
        try:
            doc.authenticate("3800")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1142, exc_info=True)

    is_single_page = doc.page_count <= 2

    # ── Step 1: Get content page (page 3, index 2) ──
    content_page = None
    content_text = ""
    content_text_native = ""
    start_idx = min(2, doc.page_count)  # Skip envelope pages (index 0, 1)
    for i in range(start_idx, min(start_idx + 3, doc.page_count)):
        page = doc[i]
        native_text = page.get_text() or ""
        text = native_text
        if len(text.strip()) < 50 and HAS_OCR:
            logger.info(f"Page {i+1}: OCR scanning...")
            text = _ocr_page_rapid(page)
        if len(text.strip()) < 20:
            continue
        content_page = page
        content_text_native = native_text
        content_text = text
        break

    # Fallback for single/short-page docs
    if content_page is None and doc.page_count > 0:
        content_page = doc[0]
        content_text_native = content_page.get_text() or ""
        content_text = content_text_native
        if len(content_text.strip()) < 50 and HAS_OCR:
            content_text = _ocr_page_rapid(content_page)

    if content_page is None:
        return empty_result if return_structured else None

    fast_text = "\n".join(part for part in [content_text_native, content_text] if part)
    fast_result = _maybe_fast_text_name_result(fast_text, case_name=case_name)
    if fast_result:
        logger.info("Fast text path hit for %s", pdf_path)
        return fast_result if return_structured else fast_result["filename"]

    # ── Step 2: Parallel — Vision analysis + stamp extraction ──
    # Run Vision and stamp extraction concurrently to save ~30-60s
    vision_info = {}
    stamp_dates = []
    env_text_cache = {"text": None}  # shared cache for envelope OCR

    def _run_vision():
        nonlocal vision_info
        vision_info = _vision_analyze_for_naming(content_page)

    def _run_stamp_and_envelope():
        nonlocal stamp_dates
        # Skip VLM fallback in stamp extraction — Vision thread handles date
        os.environ["_MAGI_STAMP_SKIP_VLM"] = "1"
        # Stamp pages: envelope first (if multi-page), then content page
        stamp_pages = [content_page]
        if doc.page_count > 2 and content_page != doc[0]:
            stamp_pages.insert(0, doc[0])
            # Cache envelope OCR for later party extraction
            env_text = doc[0].get_text() or ""
            if len(env_text.strip()) < 50 and HAS_OCR:
                env_text = _ocr_page_rapid(doc[0])
            env_text_cache["text"] = env_text
        elif is_single_page:
            # Single-page: stamp is on same page as content
            stamp_pages = [content_page]

        for sp in stamp_pages:
            try:
                stamp_result = _extract_receipt_date_from_stamp(sp)
                if stamp_result and stamp_result[0]:
                    stamp_dates.append(stamp_result[0])
                    logger.info("Receipt stamp date: %s (method: %s)", stamp_result[0], stamp_result[1])
            except Exception as e:
                logger.debug("Stamp date extraction failed: %s", e)

    # Run in parallel threads
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_vision = pool.submit(_run_vision)
        f_stamp = pool.submit(_run_stamp_and_envelope)
        concurrent.futures.wait([f_vision, f_stamp], timeout=120)

    os.environ.pop("_MAGI_STAMP_SKIP_VLM", None)
    logger.info("Vision info: %s", {k: v[:30] if isinstance(v, str) and len(v) > 30 else v
                                     for k, v in vision_info.items()})

    # Cross-verify stamp dates
    stamp_date = None
    date_method = ""
    if stamp_dates:
        if len(stamp_dates) >= 2 and stamp_dates[0] == stamp_dates[1]:
            stamp_date = stamp_dates[0]
            date_method = "stamp_cross_verified"
            logger.info("Stamp date cross-verified: %s", stamp_date)
        else:
            stamp_date = stamp_dates[0]
            date_method = "stamp"

    # ── Step 3: OCR extraction (supplementary fields from content text) ──
    ocr_date = _extract_roc_date(content_text)
    ocr_court = _extract_court_name(content_text)
    ocr_case_no = _extract_case_number(content_text)
    ocr_type = _extract_doc_type(content_text)
    ocr_name = _extract_name(content_text, default_name=None)

    # Use cached envelope text for party extraction (no redundant OCR)
    if not ocr_name and env_text_cache["text"]:
        env_name_m = re.search(r"受送達人\S*[：:\s]+\d?([\u4e00-\u9fffA-Za-z·\-]{2,20})", env_text_cache["text"])
        if env_name_m:
            ocr_name = env_name_m.group(1).strip()
            logger.info("Party from envelope (cached): %s", ocr_name)
    elif not ocr_name and doc.page_count > 2:
        # Envelope not yet OCR'd — do it now
        env_page = doc[0]
        env_text = env_page.get_text() or ""
        if len(env_text.strip()) < 50 and HAS_OCR:
            env_text = _ocr_page_rapid(env_page)
        env_name_m = re.search(r"受送達人\S*[：:\s]+\d?([\u4e00-\u9fffA-Za-z·\-]{2,20})", env_text)
        if env_name_m:
            ocr_name = env_name_m.group(1).strip()
            logger.info("Party from envelope: %s", ocr_name)

    # ── Step 4: Merge — stamp date > OCR date > Vision date ──
    found_date = stamp_date or ocr_date or vision_info.get("date")
    if not date_method:
        date_method = "ocr" if (found_date == ocr_date) else "vision"
    found_court = vision_info.get("court") or ocr_court
    found_case_no = vision_info.get("case_number") or ocr_case_no
    found_type = vision_info.get("doc_type") or ocr_type
    found_party = vision_info.get("party") or ocr_name

    # Normalize simplified → traditional Chinese
    if found_party:
        try:
            import opencc
            found_party = opencc.OpenCC("s2t").convert(found_party)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1270, exc_info=True)

    if case_name:
        found_party = case_name

    if not found_date:
        logger.warning("Could not extract date from %s", pdf_path)
        return empty_result if return_structured else None

    result = _build_name_result(
        found_date=found_date,
        found_court=found_court,
        found_case_no=found_case_no,
        found_type=found_type,
        found_party=found_party,
        date_method=date_method,
    )

    if return_structured:
        return result
    return result["filename"]


def _vision_analyze_for_naming(content_page) -> dict:
    """Use oMLX Vision to analyze a content page for naming metadata.
    Returns dict with keys: date, court, case_number, doc_type, party."""
    if os.environ.get("MAGI_PDF_NAMER_USE_VISION", "1").strip() in {"0", "false", "no", "off"}:
        return {}
    try:
        from skills.bridge import melchior_client as _mc
        _chat_omlx = getattr(_mc, "_chat_omlx", None)
        _omlx_avail = getattr(_mc, "_omlx_available", None)
        if not (callable(_chat_omlx) and callable(_omlx_avail) and _omlx_avail()):
            logger.info("Vision: oMLX not available.")
            return {}
    except Exception:
        return {}

    try:
        pix = content_page.get_pixmap(dpi=150)
        png = pix.tobytes("png")
        b64 = base64.b64encode(png).decode("utf-8")

        prompt = (
            "這是一份臺灣法院文件的掃描頁面。請辨識並以以下格式回覆（每行一項）：\n"
            "日期: YYYYMMDD (民國年轉西元，如115年3月20日→20260320)\n"
            "法院: (完整法院名，如臺灣士林地方法院)\n"
            "案號: (完整案號，如115年度司促字第1781號)\n"
            "文件類型: (支付命令、庭通知書、判決、裁定、傳票等)\n"
            "當事人: (債權人/原告/聲請人的姓名或公司名)\n"
            "無法辨識的項目寫「無」。"
        )

        vision_model = getattr(_mc, "OMLX_VISION_MODEL", "TAIDE-12b-Chat-mlx-4bit")
        vision_timeout = int(os.environ.get("MAGI_PDF_NAMER_VISION_NAMING_TIMEOUT", "90"))
        r = _chat_omlx(
            prompt=prompt, model=vision_model,
            timeout=max(60, vision_timeout),
            temperature=0.0, max_tokens=512, images=[b64],
        )
        if not (r.get("success") and r.get("response")):
            logger.info("Vision: no response.")
            return {}

        logger.info("Vision result: %s", r["response"][:300])
        return _parse_naming_response(r["response"])

    except Exception as e:
        logger.error("Vision naming failed: %s", e)
        return {}


def _parse_naming_response(text: str) -> dict:
    """Parse structured naming response from Vision model.
    Handles both plain text and markdown formats."""
    result = {}

    # Normalize markdown bold markers
    normalized = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    # Remove markdown list markers
    normalized = re.sub(r"^\s*[*·•\-]\s+", "", normalized, flags=re.MULTILINE)

    # Parse date
    dm = re.search(r"日期\s*[:：]\s*(20\d{6})", normalized)
    if dm:
        result["date"] = dm.group(1)
    else:
        dm_roc = re.search(r"日期\s*[:：]\s*(\d{7})", normalized)
        if dm_roc:
            roc_raw = dm_roc.group(1)
            try:
                y_roc = int(roc_raw[:3])
                mo = int(roc_raw[3:5])
                day = int(roc_raw[5:7])
                if 90 <= y_roc <= 200 and 1 <= mo <= 12 and 1 <= day <= 31:
                    result["date"] = f"{y_roc + 1911}{mo:02d}{day:02d}"
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1385, exc_info=True)
        if "date" not in result:
            dm_written = re.search(r"民國\s*(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", normalized)
            if dm_written:
                try:
                    y = int(dm_written.group(1)) + 1911
                    m = int(dm_written.group(2))
                    d = int(dm_written.group(3))
                    if 2000 <= y <= 2100 and 1 <= m <= 12 and 1 <= d <= 31:
                        result["date"] = f"{y}{m:02d}{d:02d}"
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1396, exc_info=True)
            if "date" not in result:
                d = _extract_any_date(normalized)
                if d:
                    result["date"] = d

    # Parse court name
    cm = re.search(r"法院\s*[:：]\s*(.+)", normalized)
    if cm:
        court = cm.group(1).strip().rstrip("。，,")
        if court != "無" and len(court) >= 4:
            court = re.sub(r"[，,。;；\n].*", "", court).strip()
            if "法院" in court:
                result["court"] = court

    # Parse case number
    cn = re.search(r"案號\s*[:：]\s*(.+)", normalized)
    if cn:
        case_no = cn.group(1).strip().rstrip("。，,")
        if case_no != "無" and "年" in case_no and "字" in case_no:
            case_no = re.sub(r"[，,。;；\n].*", "", case_no).strip()
            result["case_number"] = case_no

    # Parse doc type
    tm = re.search(r"文件類型\s*[:：]\s*(.+)", normalized)
    if tm:
        raw_type = tm.group(1).strip().rstrip("。，,")
        raw_type = re.sub(r"[，,。;；\n].*", "", raw_type).strip()
        if raw_type != "無" and raw_type != "公文封" and len(raw_type) >= 2:
            result["doc_type"] = raw_type

    # Parse party name
    pm = re.search(r"當事人\s*[:：]\s*(.+)", normalized)
    if pm:
        name = pm.group(1).strip().rstrip("。，,")
        name = re.sub(r"^[\s*·•\-]+", "", name).strip()
        if name != "無" and len(name) >= 2:
            name = re.sub(r"^(原告|被告|聲請人|債權人|債務人)\s*[:：]?\s*", "", name)
            name = re.sub(r"[，,。;；\n].*", "", name).strip()
            if len(name) >= 2 and len(name) <= 20:
                result["party"] = name

    # Fallback for party
    if "party" not in result:
        for label in ["債權人", "原告", "聲請人"]:
            pm2 = re.search(rf"{label}\s*[:：]\s*([\u4e00-\u9fffA-Za-z·\-]{{2,20}})", normalized)
            if pm2:
                result["party"] = pm2.group(1).strip()
                break

    return result

def rename_file(pdf_path: str, case_name: str = None, dry_run: bool = False):
    proposal = generate_name_proposal(pdf_path, case_name)
    if not proposal:
        logger.warning(f"Could not determine name for {pdf_path}")
        return
    
    dir_name = os.path.dirname(pdf_path)
    new_path = os.path.join(dir_name, proposal)
    
    if new_path == pdf_path:
        logger.info("Name already correct.")
        return

    # Conflict resolution
    base, ext = os.path.splitext(new_path)
    counter = 1
    while os.path.exists(new_path):
        new_path = f"{base}({counter}){ext}"
        counter += 1
    
    logger.info(f"Renaming: {os.path.basename(pdf_path)} -> {os.path.basename(new_path)}")
    
    if not dry_run:
        os.rename(pdf_path, new_path)

def extract_text(pdf_path: str) -> Tuple[str, bool]:
    """Extract text from PDF (first 5 pages), with OCR fallback."""
    if not os.path.exists(pdf_path):
        return "", False
    try:
        doc = fitz.open(pdf_path)
        if doc.needs_pass:
            try:
                doc.authenticate("3800")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1483, exc_info=True)
        text = ""
        try:
            max_pages = int(os.environ.get("MAGI_PDF_NAMER_TEXT_SCAN_PAGES", "5") or "5")
        except Exception:
            max_pages = 3
        try:
            ocr_pages = int(os.environ.get("MAGI_PDF_NAMER_TEXT_OCR_PAGES", "4") or "4")
        except Exception:
            ocr_pages = 2
        max_pages = max(1, max_pages)
        ocr_pages = max(0, ocr_pages)
        depth = min(max_pages, doc.page_count)
        for i in range(depth):
            page = doc[i]
            t = page.get_text()
            if (i < ocr_pages) and len(t.strip()) < 50 and HAS_OCR:
                t = _ocr_page_rapid(page)
            text += t + "\n"
        return text, True
    except Exception as e:
        logger.error(f"Text extraction failed: {e}")
        return "", False

def extract_text_quick(pdf_path: str, max_pages: int = 1) -> Tuple[str, bool]:
    """
    Lightweight text extraction for fast-downgrade docs.
    No OCR fallback; returns quickly.
    """
    if not os.path.exists(pdf_path):
        return "", False
    try:
        doc = fitz.open(pdf_path)
        if doc.needs_pass:
            try:
                doc.authenticate("3800")
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1520, exc_info=True)
        text = ""
        depth = min(max(1, int(max_pages or 1)), doc.page_count)
        for i in range(depth):
            text += (doc[i].get_text() or "") + "\n"
        return text, True
    except Exception:
        return "", False

def task_analyze(pdf_path: str) -> str:
    """Analyze PDF and return JSON string for smart_filer.

    Delegates naming to generate_name_proposal() which follows the standard
    convention: {YYYYMMDD} {法院全名}{案號}{文件類型}（{當事人}）.pdf
    """
    try:
        if not os.path.exists(pdf_path):
            return json.dumps({"error": "File not found"})

        bn = os.path.basename(pdf_path or "")
        fast_receipt_mode = _is_fast_downgrade_receipt(bn, pdf_path)

        # Fast-downgrade lane for receipts: keep simple naming
        if fast_receipt_mode:
            return _task_analyze_fast_receipt(pdf_path, bn)

        # Use generate_name_proposal for standard court documents
        info = generate_name_proposal(pdf_path, return_structured=True)

        if not info.get("filename"):
            return json.dumps({
                "suggested_filename": None,
                "doc_type": info.get("doc_type", ""),
                "parties": [info["party"]] if info.get("party") else [],
                "date": info.get("date"),
                "confidence": 0.0,
            }, ensure_ascii=False)

        stamp_verified = str(info.get("date_method", "")).startswith("stamp")
        res = {
            "suggested_filename": info["filename"],
            "doc_type": info.get("doc_type", ""),
            "parties": [info["party"]] if info.get("party") else [],
            "date": info.get("date"),
            "date_method": info.get("date_method", ""),
            "confidence": 0.85 if stamp_verified else 0.8,
            "stamp_verified": stamp_verified,
            "requires_stamp_verification": False,
            "fast_downgrade_receipt": False,
            "db_template_used": False,
        }
        return json.dumps(res, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)})


def _task_analyze_fast_receipt(pdf_path: str, bn: str) -> str:
    """Fast lane for receipt-like files: simple naming without Vision."""
    m_pre = re.match(r"^(20\d{6})", bn)
    d = None
    date_method = "text"

    if m_pre:
        d = m_pre.group(1)
        date_method = "filename_prefix_fast_receipt"
    else:
        text, _ = extract_text_quick(pdf_path, max_pages=1)
        d2 = _extract_any_date(text) or _extract_roc_date(text)
        if d2:
            d = d2
            date_method = "text_fast_receipt"

    t = _infer_doc_type_from_hints(bn) or _infer_doc_type_from_learning(bn) or "收據"
    n = _extract_name_from_filename(bn) or "Unknown"

    suggested = f"{d} {n}{t}.pdf" if (d and t and n and n != "Unknown") else None
    res = {
        "suggested_filename": suggested,
        "doc_type": t,
        "parties": [n] if (n and n != "Unknown") else [],
        "date": d,
        "date_method": date_method,
        "confidence": 0.8 if suggested else 0.0,
        "stamp_verified": False,
        "requires_stamp_verification": False,
        "fast_downgrade_receipt": True,
        "db_template_used": False,
    }
    return json.dumps(res, ensure_ascii=False)


def _build_name_result(
    *,
    found_date: Optional[str],
    found_court: Optional[str] = "",
    found_case_no: Optional[str] = "",
    found_type: Optional[str] = "",
    found_party: Optional[str] = "",
    date_method: str = "",
) -> dict:
    result = {
        "filename": None,
        "date": found_date,
        "date_method": date_method,
        "court": found_court or "",
        "case_number": found_case_no or "",
        "doc_type": found_type or "",
        "party": found_party or "",
    }
    if not found_date:
        return result

    body = ""
    if found_court:
        body += found_court
    if found_case_no:
        body += found_case_no
    if found_type:
        body += found_type
    if not body:
        body = "文件"

    suffix = ""
    if found_party and found_party != "Unknown":
        suffix = f"（{found_party}）"

    new_name = f"{found_date} {body}{suffix}.pdf"
    new_name = re.sub(r'[/\\:*?"<>|]', "", new_name)
    result["filename"] = new_name
    return result


def _maybe_fast_text_name_result(content_text: str, *, case_name: Optional[str] = None) -> Optional[dict]:
    """
    Fast path for searchable PDFs.

    When the page already contains enough native text, OCR/Vision adds latency and
    can hallucinate unrelated court/case metadata. Prefer deterministic parsing.
    """
    text = (content_text or "").strip()
    if len(text) < 30:
        return None

    found_date = _extract_any_date(text) or _extract_roc_date(text)
    found_court = _extract_court_name(text)
    found_case_no = _extract_case_number(text)
    found_type = _extract_doc_type(text)
    found_party = case_name or _extract_name(text, default_name=None)

    if found_party:
        try:
            import opencc

            found_party = opencc.OpenCC("s2t").convert(found_party)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1664, exc_info=True)

    if not found_date or not (found_court or found_case_no or found_type):
        return None

    return _build_name_result(
        found_date=found_date,
        found_court=found_court,
        found_case_no=found_case_no,
        found_type=found_type,
        found_party=found_party,
        date_method="ocr_fast_path",
    )


def task_self_train(case_root: str = CASE_ROOT) -> str:
    """
    Build/update lightweight filename-learning rules from 01_案件 folder.
    """
    global _LEARNED_RULES_CACHE
    payload = build_filename_learning_rules(case_root=case_root)
    _LEARNED_RULES_CACHE = payload
    res = {
        "ok": True,
        "generated_at": payload.get("generated_at"),
        "sample_count": int(payload.get("sample_count") or 0),
        "labels": payload.get("label_counts") or {},
        "rule_count": len(payload.get("rules") or []),
        "exact_rule_count": len(payload.get("exact_rules") or {}),
        "rules_path": LEARNED_RULES_PATH,
    }
    return json.dumps(res, ensure_ascii=False)


def _truthy(v: str) -> bool:
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def _job_paths(job_id: str) -> Tuple[Path, Path]:
    return JOB_DIR / f"file_{job_id}.json", JOB_DIR / f"file_{job_id}.log"


def _read_job(job_id: str) -> dict:
    status_path, _ = _job_paths(job_id)
    if not status_path.exists():
        return {}
    try:
        return json.loads(status_path.read_text(encoding="utf-8") or "{}")
    except Exception:
        return {}


def _write_job(job_id: str, patch: dict) -> dict:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    status_path, _ = _job_paths(job_id)
    data = _read_job(job_id)
    data.update(patch or {})
    data["job_id"] = job_id
    data["updated_at"] = datetime.now().isoformat()
    tmp_path = status_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(status_path)
    return data


def _latest_job_id() -> str:
    if not JOB_DIR.exists():
        return ""
    files = sorted(JOB_DIR.glob("file_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return ""
    return files[0].stem.replace("file_", "", 1)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _summary_from_report(report: dict) -> dict:
    return {
        "filed": len((report or {}).get("filed") or []),
        "failed": len((report or {}).get("failed") or []),
        "unnamed": len((report or {}).get("unnamed") or []),
        "skipped": len((report or {}).get("skipped") or []),
    }


def _run_file_pipeline(execute: bool, notify: bool, job_id: str = "") -> dict:
    if job_id:
        _write_job(
            job_id,
            {
                "status": "running",
                "running": True,
                "started_at": datetime.now().isoformat(),
                "execute": bool(execute),
                "notify": bool(notify),
            },
        )
    try:
        from smart_filer import process_scan_folder

        report = process_scan_folder(dry_run=not bool(execute), notify=bool(notify))
        summary = _summary_from_report(report)
        out = {"success": True, "report": report, "summary": summary}
        if job_id:
            _write_job(
                job_id,
                {
                    "status": "done",
                    "running": False,
                    "finished_at": datetime.now().isoformat(),
                    "success": True,
                    "summary": summary,
                },
            )
        return out
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if job_id:
            _write_job(
                job_id,
                {
                    "status": "failed",
                    "running": False,
                    "finished_at": datetime.now().isoformat(),
                    "success": False,
                    "error": err,
                },
            )
        return {"success": False, "error": err}


def _spawn_file_background(execute: bool, notify: bool) -> dict:
    JOB_DIR.mkdir(parents=True, exist_ok=True)
    singleton = _truthy(os.environ.get("MAGI_PDF_NAMER_FILE_BG_SINGLETON", "1"))
    if singleton:
        latest = _latest_job_id()
        if latest:
            st = _read_job(latest)
            pid = int(st.get("pid") or 0)
            if st.get("running") and pid > 1 and _pid_alive(pid):
                return {
                    "success": True,
                    "queued": True,
                    "deduped": True,
                    "job_id": latest,
                    "pid": pid,
                    "status": "already_running",
                    "message": f"pdf-namer 背景任務已在執行中（job_id={latest}）",
                }

    job_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    status_path, log_path = _job_paths(job_id)
    _write_job(
        job_id,
        {
            "status": "queued",
            "running": False,
            "queued_at": datetime.now().isoformat(),
            "execute": bool(execute),
            "notify": bool(notify),
            "log_path": str(log_path),
        },
    )
    cmd = [
        sys.executable,
        os.path.abspath(__file__),
        "--task",
        "file_worker",
        "--execute",
        "1" if execute else "0",
        "--notify",
        "1" if notify else "0",
        "--job-id",
        job_id,
    ]
    env = os.environ.copy()
    env["MAGI_PDF_NAMER_FILE_BACKGROUND"] = "0"

    try:
        with open(log_path, "a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,
            )
        threading.Thread(target=proc.wait, daemon=True).start()
        _write_job(
            job_id,
            {
                "status": "running",
                "running": True,
                "pid": int(proc.pid),
                "started_at": datetime.now().isoformat(),
                "status_path": str(status_path),
            },
        )
        return {
            "success": True,
            "queued": True,
            "job_id": job_id,
            "pid": int(proc.pid),
            "status_path": str(status_path),
            "log_path": str(log_path),
            "message": f"pdf-namer 背景任務已啟動（job_id={job_id}）",
        }
    except Exception as e:
        err = f"spawn_failed: {e}"
        _write_job(
            job_id,
            {
                "status": "failed",
                "running": False,
                "success": False,
                "error": err,
                "finished_at": datetime.now().isoformat(),
            },
        )
        return {"success": False, "error": err, "job_id": job_id}


def _get_file_status(job_id: str = "") -> dict:
    jid = (job_id or "").strip()
    if not jid or jid == "latest":
        jid = _latest_job_id()
    if not jid:
        return {"success": False, "error": "no_background_job"}

    st = _read_job(jid)
    if not st:
        return {"success": False, "error": "job_not_found", "job_id": jid}

    pid = int(st.get("pid") or 0)
    if st.get("running") and pid > 1 and (not _pid_alive(pid)):
        status_name = str(st.get("status") or "")
        if status_name not in {"done", "failed"}:
            st = _write_job(jid, {"running": False, "status": "stopped", "finished_at": datetime.now().isoformat()})
        else:
            st = _write_job(jid, {"running": False})
    st["success"] = bool(st.get("status") in {"done", "running", "queued", "stopped"} or st.get("success"))
    return st

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--task",
        required=True,
        choices=["rename_file", "review_name", "file", "file_sync", "file_worker", "file_status", "self_train", "help"],
    )
    parser.add_argument("--path", help="PDF file path")
    parser.add_argument("--case_name", help="Client Name (e.g. 游秀鈴)")
    parser.add_argument("--execute", default="0", help="Execute filing (1=yes)")
    parser.add_argument("--notify", default="1", help="Send notification (1=yes)")
    parser.add_argument("--job-id", default="", help="Background job id")
    args = parser.parse_args()

    if args.task == "help":
        print(json.dumps({"skill": "pdf-namer", "tasks": ["rename_file", "review_name", "file", "file_sync", "file_worker", "file_status", "self_train"], "description": "PDF 智慧命名與歸檔"}, ensure_ascii=False, indent=2))
        return

    if args.task == "review_name":
        if not args.path:
            print("Error: --path required")
            return
        prop = generate_name_proposal(args.path, args.case_name)
        print(f"Proposed Name: {prop}")
    
    elif args.task == "rename_file":
        if not args.path:
            print("Error: --path required")
            return
        rename_file(args.path, args.case_name, dry_run=False)

    elif args.task == "file":
        execute = (args.execute == "1")
        notify = (args.notify == "1")
        bg_default = _truthy(os.environ.get("MAGI_PDF_NAMER_FILE_BACKGROUND", "1"))
        out = _spawn_file_background(execute, notify) if bg_default else _run_file_pipeline(execute, notify)
        print(json.dumps(out, ensure_ascii=False, indent=2))

    elif args.task == "file_sync":
        execute = (args.execute == "1")
        notify = (args.notify == "1")
        out = _run_file_pipeline(execute, notify)
        print(json.dumps(out, ensure_ascii=False, indent=2))

    elif args.task == "file_worker":
        execute = (args.execute == "1")
        notify = (args.notify == "1")
        out = _run_file_pipeline(execute, notify, job_id=(args.job_id or ""))
        print(json.dumps({"success": bool(out.get("success")), "job_id": (args.job_id or "")}, ensure_ascii=False))

    elif args.task == "file_status":
        out = _get_file_status(args.job_id or "latest")
        print(json.dumps(out, ensure_ascii=False, indent=2))

    elif args.task == "self_train":
        print(task_self_train())


if __name__ == "__main__":
    main()
