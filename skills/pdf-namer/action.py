#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf-namer/action.py
PDF 自動命名技能 (PyMuPDF + RapidOCR)
"""

import argparse
import fitz  # PyMuPDF
import math
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
from typing import Dict, List, Optional, Tuple
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
    # 法院通知 (Court Notices) — 庭函 before 通知 to avoid false match
    "庭通知書", "庭函", "開庭通知", "期日通知", "傳票", "法院通知", "通知書",
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
    "驗傷診斷書", "相驗屍體證明書",
    # 回執
    "預酬回執", "委任狀回執", "回執", "掛號郵件收件回執",
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
    "庭通知書": "法院_通知", "庭函": "函文", "法院通知": "法院_通知", "開庭通知": "法院_通知", "期日通知": "法院_通知", "傳票": "法院_傳票", "通知書": "法院_通知",
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
    "驗傷診斷書": "驗傷診斷書", "相驗屍體證明書": "相驗屍體證明書",
    # 回執
    "預酬回執": "預酬回執", "委任狀回執": "委任狀回執",
    "回執": "回執", "掛號郵件收件回執": "回執",
}

_CASE_ROOTS = preferred_case_roots(include_closed=False)
_FALLBACK_CASE_ROOTS = default_case_roots(include_closed=False)
CASE_ROOT = os.environ.get(
    "MAGI_CASE_ROOT",
    _CASE_ROOTS[0] if _CASE_ROOTS else (_FALLBACK_CASE_ROOTS[0] if _FALLBACK_CASE_ROOTS else str(Path.home() / "Library" / "CloudStorage" / "SynologyDrive-homes" / "01_案件")),
)

# ── Synology Drive local path candidates (for SMB fallback) ──
_HOME = Path.home()
_SYNOLOGY_LOCAL_SHARE_CANDIDATES = [
    str(_HOME / "Library/CloudStorage/SynologyDrive-homes"),
    str(_HOME / "SynologyDrive/homes"),
    str(_HOME / "SynologyDrive"),
]
# NAS root prefixes that map to the user's home share
_NAS_HOME_SHARE_PREFIXES = [
    str(_HOME / ".magi_mounts/homes/lumi63181107"),
    str(_HOME / ".magi_mounts/homes"),
]
for _vol_base in ("homes", "homes-1", "homes-2", "homes-3"):
    _NAS_HOME_SHARE_PREFIXES.append(f"/Volumes/{_vol_base}/lumi63181107")
    _NAS_HOME_SHARE_PREFIXES.append(f"/Volumes/{_vol_base}")


def _resolve_pdf_with_synology_fallback(pdf_path: str) -> str:
    """If pdf_path is inaccessible, try the equivalent Synology Drive local path.

    Maps SMB/NAS prefixes (e.g. /Volumes/homes-1/lumi63181107/...) to local
    Synology Drive paths (~/Library/CloudStorage/SynologyDrive-homes/...).
    Returns the best accessible path, or the original path if none found.
    """
    if os.path.exists(pdf_path):
        return pdf_path
    # Find which NAS prefix matches this path
    rel = None
    for prefix in _NAS_HOME_SHARE_PREFIXES:
        norm_prefix = prefix.rstrip("/") + "/"
        norm_path = pdf_path if pdf_path.endswith("/") else pdf_path
        if norm_path.startswith(norm_prefix):
            rel = norm_path[len(norm_prefix):]
            break
        # also try without trailing slash
        if norm_path == prefix:
            rel = ""
            break
    if rel is None:
        return pdf_path
    # Try each local Synology Drive candidate
    for local_root in _SYNOLOGY_LOCAL_SHARE_CANDIDATES:
        candidate = os.path.join(local_root, rel) if rel else local_root
        if os.path.exists(candidate):
            logger.info("SMB 路徑不可達，改用 Synology Drive 本機路徑: %s → %s", pdf_path, candidate)
            return candidate
    return pdf_path


SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
LEARNED_RULES_PATH = os.path.join(SKILL_DIR, "_learned_filename_rules.json")
CORRECTIONS_PATH = os.path.join(SKILL_DIR, "_corrections.json")

_LEARNED_RULES_CACHE: Optional[dict] = None
_BATCH_ANALYSIS_CACHE = {}  # type: dict[str, dict] — Pre-computed by batch_analyze_texts

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


def _infer_party_from_case_folder_path(pdf_path: str) -> Optional[str]:
    """Infer party from a MAGI case folder path: YYYY-NNNN-Party-Stage-Reason."""
    try:
        parts = Path(pdf_path).parts
    except Exception:
        return None
    for part in reversed(parts):
        m = re.match(r"^\d{4}-\d{4}-(.+)$", part)
        if not m:
            continue
        tokens = m.group(1).split("-")
        if not tokens:
            continue
        party = tokens[0].strip()
        if (
            party
            and 2 <= len(party) <= 30
            and not party.startswith("[")
            and not re.search(r"(當事人|Unknown|不詳)", party)
        ):
            return party
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
    # Check for 函 pattern first: "主旨" + no other specific doc type = 函文
    has_zhi = "主旨" in header_text or "主　旨" in header_text
    for keyword in DOC_TYPES:
        if keyword in header_text:
            return DOC_TYPE_MAP.get(keyword, keyword)
    # Fallback: 主旨 without other type markers = 函文
    if has_zhi:
        return "函文"
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
    _bad_fragments = {"或被告", "或相對人", "聲請人或", "證人", "定人用", "或定人",
                       "經本院", "本院於", "謄本", "乙份", "正本", "影本",
                       "附件", "清單", "資料", "保險", "全戶"}
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
    路徑：macOS Vision → oMLX Gemma → Ollama vision chain。
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

        # ── Primary: oMLX (Gemma vision) ──
        try:
            from skills.bridge import melchior_client as _mc
            _chat_omlx = getattr(_mc, "_chat_omlx", None)
            _omlx_avail = getattr(_mc, "_omlx_available", None)
            if callable(_chat_omlx) and callable(_omlx_avail) and _omlx_avail():
                ocr_model = getattr(_mc, "OMLX_OCR_MODEL", os.environ.get("MAGI_OMLX_OCR_MODEL", ""))
                from skills.bridge.melchior_client import OMLX_VISION_BASE
                r = _chat_omlx(
                    prompt=prompt, model=ocr_model,
                    base_url=OMLX_VISION_BASE,
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
                os.environ.get("MAGI_PDF_NAMER_VISION_MODEL", os.environ.get("MAGI_MAIN_MODEL", ""))
                or os.environ.get("MAGI_MAIN_MODEL", "")
            ).strip()
        models = [m.strip() for m in chain.split(",") if m.strip()]

        def _try(mname: str) -> Optional[str]:
            retries = max(1, int(os.environ.get("MAGI_OLLAMA_BUSY_RETRIES", "2") or "2"))
            retry_sleep = float(os.environ.get("MAGI_OLLAMA_BUSY_RETRY_SEC", "0.8") or "0.8")
            omlx_base = (os.environ.get("MAGI_OMLX_VISION_URL") or os.environ.get("OMLX_URL") or "http://127.0.0.1:8080").rstrip("/")
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

def _try_stamp_crop_vision(doc, pages_info: dict = None) -> Tuple[Optional[str], str]:
    """Try to find receipt stamp date by cropping page corners and OCR'ing them."""
    _stamp_crop_regions = [
        ("top_right", (0.55, 0.00, 1.00, 0.35)),
        ("right_strip", (0.72, 0.00, 1.00, 0.55)),
        ("top_left", (0.00, 0.00, 0.45, 0.35)),
        ("bottom_right", (0.55, 0.70, 1.00, 1.00)),
    ]
    scan_page_idxs = [0]
    if pages_info:
        ci = pages_info.get("content_idx", 0)
        if ci > 0 and ci not in scan_page_idxs:
            scan_page_idxs.append(ci)

    try:
        from skills.bridge import melchior_client as _mc_s
        _chat_s = getattr(_mc_s, "_chat_omlx", None)
        _avail_s = getattr(_mc_s, "_omlx_available", None)
        if not (callable(_chat_s) and callable(_avail_s) and _avail_s()):
            return None, ""
        from skills.bridge.melchior_client import (
            OMLX_VISION_BASE as _VBS, OMLX_VISION_MODEL as _VMS,
            _OMLX_VISION_CIRCUIT as _VCS, _OMLX_VISION_LOCK as _VLS,
        )
        stamp_prompt = (
            "這張圖片是文件角落的裁切區域。請找出收文章（藍色圓形章）上的日期。"
            "收文章格式通常為民國年.月.日（如115.4.02代表民國115年4月2日=西元2026年4月2日）。"
            "只回覆日期數字（民國年.月.日格式），看不到收文章就回覆NONE。"
        )
        for sp_idx in scan_page_idxs:
            if sp_idx < 0 or sp_idx >= doc.page_count:
                continue
            try:
                sp_pix = doc[sp_idx].get_pixmap(dpi=220)
                sp_png = sp_pix.tobytes("png")
            except Exception:
                continue
            for crop_name, (cx0, cy0, cx1, cy1) in _stamp_crop_regions:
                crop_png = _crop_png_bytes(sp_png, x0=cx0, y0=cy0, x1=cx1, y1=cy1)
                if not crop_png:
                    continue
                if HAS_PIL:
                    try:
                        from PIL import ImageOps, ImageEnhance
                        im = Image.open(io.BytesIO(crop_png)).convert("L")
                        im = ImageOps.autocontrast(im)
                        w, h = im.size
                        im = im.resize((int(w * 2.0), int(h * 2.0)))
                        im = ImageEnhance.Sharpness(im).enhance(1.6)
                        im = ImageEnhance.Contrast(im).enhance(1.4)
                        buf = io.BytesIO()
                        im.save(buf, format="PNG")
                        crop_png = buf.getvalue()
                    except Exception:
                        pass
                b64_crop = base64.b64encode(crop_png).decode("utf-8")
                try:
                    r_stamp = _chat_s(
                        prompt=stamp_prompt, model=_VMS,
                        base_url=_VBS, timeout=45,
                        temperature=0.0, max_tokens=64, images=[b64_crop],
                        circuit=_VCS, lock=_VLS,
                    )
                    if r_stamp.get("success") and r_stamp.get("response"):
                        raw = r_stamp["response"].strip()
                        if "NONE" not in raw.upper():
                            from vision_parser import _parse_date_from_text
                            sd = _parse_date_from_text(raw)
                            if sd:
                                try:
                                    yr = int(sd[:4])
                                    if 2000 <= yr <= 2030:
                                        logger.info("Stamp date from crop %s: %s", crop_name, sd)
                                        return sd, f"vision_stamp_crop:{crop_name}"
                                except ValueError:
                                    pass
                except Exception:
                    pass
    except Exception:
        pass
    return None, ""


def _ai_generate_structured_name(ocr_text: str, already_extracted: dict) -> Optional[dict]:
    """Use Gemma 4 + SYSTEM_PROMPT to generate a structured filename proposal.

    Called when doc_type is still uncertain (「其他」/「文件」/empty) after Step 5.
    Returns parsed JSON dict with keys: doc_type, suggested_filename, confidence, date, party,
    case_number, reasoning — or None on failure.
    """
    if not ocr_text or len(ocr_text.strip()) < 30:
        return None

    ai_timeout_sec = int(os.environ.get("MAGI_PDF_AI_NAME_TIMEOUT", "45"))

    try:
        from naming_rules import SYSTEM_PROMPT
        import json as _json
        import requests as _req

        base = (os.environ.get("MAGI_OMLX_CHAT_URL") or "http://127.0.0.1:8080").rstrip("/")
        from skills.bridge.melchior_client import TEXT_PRIMARY_MODEL as _tpm

        # Provide already-extracted context as hint
        hint_parts = []
        if already_extracted.get("date"):
            hint_parts.append(f"已知日期：{already_extracted['date']}")
        if already_extracted.get("court"):
            hint_parts.append(f"已知法院：{already_extracted['court']}")
        if already_extracted.get("case_no"):
            hint_parts.append(f"已知案號：{already_extracted['case_no']}")
        if already_extracted.get("party"):
            hint_parts.append(f"已知當事人：{already_extracted['party']}")
        hint = ("【已提取欄位】\n" + "\n".join(hint_parts) + "\n\n") if hint_parts else ""

        user_msg = f"{hint}【OCR 文字】\n{ocr_text[:3000]}"

        resp = _req.post(
            f"{base}/v1/chat/completions",
            json={
                "model": _tpm,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "temperature": 0.0,
                "max_tokens": 300,
                "stream": False,
            },
            timeout=ai_timeout_sec,
        )
        if resp.status_code != 200:
            return None

        raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
        # Extract JSON from response
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start < 0 or end <= start:
            return None
        parsed = _json.loads(raw[start:end])
        if not isinstance(parsed, dict) or not parsed.get("doc_type"):
            return None
        return parsed
    except Exception as _e:
        logger.debug("[ai_name] structured naming failed: %s", _e)
        return None


def _pick_best_source(field_name, sources):
    # type: (str, list) -> tuple
    """Phase 2D: confidence-weighted field merge.
    sources = [(value, confidence, source_name), ...]
    Returns (best_value, best_confidence, best_source_name).
    Empty/blank strings are excluded — a non-empty 0.60 beats an empty 0.90.
    Tie-break on confidence: vision > ocr > learn.
    """
    _SOURCE_PRIORITY = {"vision": 1, "ocr": 2, "learn": 3}
    filtered = [(v, c, src) for (v, c, src) in sources if v and str(v).strip()]
    if not filtered:
        return ("", 0.0, "none")
    return max(filtered, key=lambda x: (x[1], -_SOURCE_PRIORITY.get(x[2], 99)))


def _compute_dynamic_confidence(value, base_conf, field_name, source):
    # type: (str, float, str, str) -> float
    """Apply quality discounts to base confidence based on field-specific rules.

    Discounts:
      court:    must contain 地方法院/高等法院/最高法院/檢察署, else × 0.5
      case_no:  must match legal case number pattern, else × 0.3
      date:     must parse as valid YYYYMMDD, else × 0.2
      party:    must not contain obvious garbled chars, else × 0.5
    """
    import re as _re
    if not value or not str(value).strip():
        return 0.0
    v = str(value).strip()
    if field_name == "court":
        if not _re.search(r"(地方法院|高等法院|最高法院|檢察署|地院|高院)", v):
            base_conf *= 0.5
    elif field_name in ("case_no", "case_number"):
        if not _re.search(r"\d+年?.?\w+字第?\d+號?", v):
            base_conf *= 0.3
    elif field_name == "date":
        try:
            y, m, d = int(v[:4]), int(v[4:6]), int(v[6:8])
            if not (2000 <= y <= 2099 and 1 <= m <= 12 and 1 <= d <= 31):
                base_conf *= 0.2
        except (ValueError, IndexError):
            base_conf *= 0.2
    elif field_name == "party":
        garbled = sum(1 for c in v if ord(c) > 0xFFFF or (0xD800 <= ord(c) <= 0xDFFF))
        if garbled > 2:
            base_conf *= 0.5
    return base_conf


def _build_source_hint(pdf_path: str, case_name: str = "", snippets: Optional[List[str]] = None) -> str:
    hints: List[str] = []
    bn = os.path.basename(pdf_path or "")
    if bn:
        hints.append(bn)
    if pdf_path:
        hints.append(pdf_path)
    if case_name:
        hints.append(case_name)
    for part in snippets or []:
        text = str(part or "").strip()
        if not text:
            continue
        hints.append(text[:1500])
    return "\n".join(hints)


def _apply_naming_guards(result: dict, source_hint: str = "") -> dict:
    """Apply filename sanitization + format/quality checks (non-blocking)."""
    if not isinstance(result, dict):
        return result
    filename = result.get("filename")
    if not filename:
        return result

    try:
        from skills.pdf_namer.naming_validator import (
            sanitize_filename as _sanitize_filename,
            validate_filename as _validate_filename,
            validate_filename_quality as _validate_filename_quality,
        )
    except ImportError:
        try:
            from naming_validator import (  # type: ignore
                sanitize_filename as _sanitize_filename,
                validate_filename as _validate_filename,
                validate_filename_quality as _validate_filename_quality,
            )
        except ImportError:
            return result

    sanitized, fixes = _sanitize_filename(filename, source_hint=source_hint)
    if sanitized and sanitized != filename:
        logger.warning("pdf-namer sanitizer: %s -> %s", filename, sanitized)
        result["filename"] = sanitized
        result["sanitizer_fixes"] = fixes
    elif fixes:
        result["sanitizer_fixes"] = fixes

    valid, warns = _validate_filename(result.get("filename", ""))
    if warns:
        logger.warning("pdf-namer format guard: %s -> %s", result.get("filename", ""), warns)
        result["warnings"] = warns
    result["format_ok"] = bool(valid)

    quality_ok, quality_issues, quality_details = _validate_filename_quality(
        result.get("filename", ""),
        source_hint=source_hint,
    )
    result["quality_ok"] = bool(quality_ok)
    if quality_issues:
        result["quality_issues"] = quality_issues
    if quality_details:
        result["quality_issue_details"] = quality_details

    return result


def generate_name_proposal(pdf_path: str, case_name: str = None, return_structured: bool = False):
    """Propose a filename following the standard convention:
    {YYYYMMDD} {法院全名}{案號}{文件類型}（{當事人}）.pdf

    Court docs: Page 1-2 = envelope, Page 3+ = actual content.
    Uses oMLX Vision on page 3 as primary, OCR as supplement for date.
    Falls back to Synology Drive local path when SMB/NAS path is inaccessible.

    Args:
        return_structured: If True, returns dict with all extracted fields + filename.
                          If False, returns filename string (backward compat).
    """
    pdf_path = _resolve_pdf_with_synology_fallback(pdf_path)
    empty_result = {"filename": None, "date": None, "court": "", "case_number": "",
                    "doc_type": "", "party": "", "date_method": ""}

    source_hint_parts = [case_name or ""]
    # ── Check batch cache (pre-computed by batch_ocr_pages + batch_analyze_texts) ──
    cached = _BATCH_ANALYSIS_CACHE.get(pdf_path)
    if cached and cached.get("merged"):
        logger.info("[batch-cache] Using pre-computed analysis for %s", os.path.basename(pdf_path))
        vision_info = dict(cached["merged"])
        source_hint_parts.extend([cached.get("envelope_ocr", ""), cached.get("content_ocr", "")])
        # Still need stamp date — do stamp extraction only
        doc = fitz.open(pdf_path)
        if doc.needs_pass:
            try:
                doc.authenticate("3800")
            except Exception:
                pass

        stamp_date = None
        date_method = ""

        # Try extracting stamp date directly from OCR text (收文章 pattern)
        # Combine all OCR texts and look for stamp markers + nearby dates
        _all_ocr = "\n".join(filter(None, [cached.get("envelope_ocr", ""), cached.get("content_ocr", "")]))
        if _all_ocr and ("收文" in _all_ocr or "收件" in _all_ocr or "法警" in _all_ocr):
            from vision_parser import _parse_date_from_text
            # Try every short line that contains digits
            for line in _all_ocr.split("\n"):
                line = line.strip()
                if not line or len(line) > 30 or not any(c.isdigit() for c in line):
                    continue
                d = _parse_date_from_text(line)
                if d:
                    try:
                        yr = int(d[:4])
                        if 2020 <= yr <= 2030:
                            stamp_date = d
                            date_method = "ocr_stamp_text"
                            logger.info("[batch-cache] Stamp date from OCR: %s (line: %s)", d, line[:30])
                            break
                    except ValueError:
                        pass

        try:
            _file_mtime = datetime.fromtimestamp(os.path.getmtime(pdf_path))
        except Exception:
            _file_mtime = datetime.now()

        # Fallback: stamp extraction from page images
        pages_info = cached.get("pages") or {}
        for sp_idx in [0, pages_info.get("content_idx", 0)]:
            if sp_idx < 0 or sp_idx >= doc.page_count:
                continue
            try:
                sr = _extract_receipt_date_from_stamp(doc[sp_idx], ref_dt=_file_mtime)
                if sr and sr[0]:
                    stamp_date = sr[0]
                    date_method = sr[1]
                    break
            except Exception:
                pass

        # If no stamp found, try crop-based vision stamp
        if not stamp_date:
            stamp_date, date_method = _try_stamp_crop_vision(doc, pages_info)

        found_date = stamp_date or vision_info.get("date")
        if stamp_date:
            date_method = date_method or "stamp"
        elif vision_info.get("date"):
            date_method = "vision"

        found_court = vision_info.get("court", "")
        found_case_no = vision_info.get("case_number", "")
        found_type = vision_info.get("doc_type", "")
        found_party = vision_info.get("party", "")
        found_doc_subtype = vision_info.get("doc_subtype", "")
        found_summary = vision_info.get("summary", "")
        found_case_type = vision_info.get("case_type", "")

        if case_name:
            found_party = case_name

        # Extract summary from all pages' native text as fallback
        if not found_summary and found_type:
            all_text = ""
            for pi in range(min(doc.page_count, 6)):
                all_text += (doc[pi].get_text() or "") + "\n"
            if all_text.strip():
                found_summary = _extract_summary_from_ocr(all_text, found_type)

        if not found_date:
            found_date, date_method = _fallback_date_from_filename_or_mtime(pdf_path)
            logger.warning("Could not extract date from %s; fallback=%s", pdf_path, found_date)
        if not found_date:
            return empty_result if return_structured else None

        # Refine with learned rules
        if not found_type or found_type in ("其他", "文件"):
            lt = _infer_doc_type_from_learning(found_doc_subtype or found_type or "")
            if lt:
                found_type = lt

        result = _build_name_result(
            found_date=found_date, found_court=found_court,
            found_case_no=found_case_no, found_type=found_type,
            found_party=found_party, date_method=date_method,
            doc_subtype=found_doc_subtype, summary=found_summary,
            case_type_hint=found_case_type,
        )
        result = _apply_naming_guards(
            result,
            source_hint=_build_source_hint(pdf_path, case_name=case_name or "", snippets=source_hint_parts),
        )
        if return_structured:
            return result
        return result["filename"]

    if not os.path.exists(pdf_path):
        return empty_result if return_structured else None

    doc = fitz.open(pdf_path)
    if doc.needs_pass:
        try:
            doc.authenticate("3800")
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1142, exc_info=True)

    is_single_page = doc.page_count <= 2

    # ── Step 1: Identify envelope page and content page ──
    # Convention: page 0-1 = envelope, page 2+ = actual content.
    # Exception: some docs have no envelope (content starts at page 0).
    envelope_page = None   # page 0 if it looks like an envelope
    content_page = None    # first actual content page (page 2+ or page 0)
    content_text = ""
    content_text_native = ""

    def _get_page_text(page_idx):
        """Get text from a page, with RapidOCR fallback (or consensus when enabled)."""
        page = doc[page_idx]
        native = page.get_text() or ""
        text = native
        if len(text.strip()) < 50:
            if _PDF_OCR_CONSENSUS:
                # Phase 2B: multi-engine consensus
                text = _ocr_consensus(page, pdf_path=pdf_path, page_idx=page_idx)
            elif HAS_OCR:
                text = _ocr_page_rapid(page)
        return page, native, text

    # Check if page 0 is an envelope (公文封)
    # Only positively detect envelope from text markers; garbled/empty text alone
    # is NOT sufficient (image-only docs may have no envelope at all).
    if doc.page_count > 2:
        p0 = doc[0]
        p0_text = p0.get_text() or ""
        if len(p0_text.strip()) < 50 and HAS_OCR:
            p0_text = _ocr_page_rapid(p0)
        if _is_envelope_page(p0_text):
            envelope_page = p0
            logger.info("Page 1: detected as envelope (text markers)")
        elif _is_garbled_text(p0_text) and doc.page_count <= 8:
            # Garbled text: check if it has envelope-like keywords even in garbled form
            _env_kw = ("公文封", "公丈封", "公支封", "受送達", "受送:ミ", "受送逹",
                        "郵務送達", "寄存送達", "送達人住居所", "送達人居住所")
            if any(k in p0_text for k in _env_kw):
                envelope_page = p0
                logger.info("Page 1: detected as envelope (garbled text with envelope keywords)")
            # Else: garbled text but no envelope markers → NOT an envelope
        # Else: large doc or clean text on page 0 → no envelope, content starts at p0

    # Find content page — skip envelope pages
    # Convention: page 0 = envelope front (公文封), page 1 = may be envelope back
    # (訴訟當事人注意事項 / instructions), page 2+ = actual content
    _ENVELOPE_BACK_MARKERS = ("注意事項", "訴訟當事人", "訴訟權益", "行賄", "信任法院", "送達方式")
    start_idx = 0
    if envelope_page:
        start_idx = 1
        # Check if page 1 is also envelope (back side / instructions)
        if doc.page_count > 2:
            p1_text = doc[1].get_text() or ""
            if any(m in p1_text for m in _ENVELOPE_BACK_MARKERS) or _is_garbled_text(p1_text):
                start_idx = 2
                logger.info("Page 2: also envelope back (instructions), content starts at page 3")
    # For no-envelope docs, page 0 is always the primary content page for vision,
    # even if its embedded text is garbled (vision reads the image, not the text).
    _vision_primary_page = doc[0] if (not envelope_page and doc.page_count > 0) else None

    for i in range(start_idx, min(start_idx + 3, doc.page_count)):
        page, native, text = _get_page_text(i)
        if len(text.strip()) < 20:
            continue
        if _is_garbled_text(text):
            logger.info(f"Page {i+1}: garbled embedded text, will use vision only")
            if content_page is None:
                content_page = page
                content_text = ""
                content_text_native = ""
            continue
        content_page = page
        content_text_native = native
        content_text = text
        break

    # For no-envelope docs: page 0 is always scanned as "title page" in
    # _run_envelope_vision regardless of content_page selection.
    # This ensures doc title/case_no/party come from page 0 header.

    # If no content page found yet and we have an envelope, page 0 IS the content
    if content_page is None and envelope_page is None and doc.page_count > 0:
        content_page = doc[0]
        content_text_native = doc[0].get_text() or ""
        content_text = content_text_native
        if len(content_text.strip()) < 50 and HAS_OCR:
            content_text = _ocr_page_rapid(content_page)
    elif content_page is None and doc.page_count > 0:
        # Envelope exists but no content page — use page 2 for vision if available
        if doc.page_count > 2:
            content_page = doc[2]
        else:
            content_page = doc[min(1, doc.page_count - 1)]
        content_text = ""
        content_text_native = ""

    if content_page is None:
        return empty_result if return_structured else None

    # ── Step 1b: Fast text path (only for clean text) ──
    fast_text = "\n".join(part for part in [content_text_native, content_text] if part)
    if fast_text.strip() and not _is_garbled_text(fast_text):
        fast_result = _maybe_fast_text_name_result(fast_text, case_name=case_name, pdf_path=pdf_path)
        if fast_result:
            logger.info("Fast text path hit for %s", pdf_path)
            fast_result = _apply_naming_guards(
                fast_result,
                source_hint=_build_source_hint(pdf_path, case_name=case_name or "", snippets=[fast_text]),
            )
            return fast_result if return_structured else fast_result["filename"]

    # ── Step 2: Dual-page Vision OCR ──
    # Scan BOTH envelope (stamp/court/case) and content (doc_type/party/summary)
    envelope_vision = {}
    content_vision = {}
    stamp_dates = []

    def _run_envelope_vision():
        nonlocal envelope_vision
        page_to_scan = envelope_page
        # For no-envelope multi-page docs, scan page 0 as "title page"
        # to get case_no/party/doc_type from the document header
        if page_to_scan is None and _vision_primary_page is not None and content_page != _vision_primary_page:
            page_to_scan = _vision_primary_page
            logger.info("No envelope: scanning page 0 as title page for metadata")
        if page_to_scan is None:
            return
        envelope_vision = _vision_analyze_for_naming(page_to_scan)
        logger.info("Envelope vision: %s", {k: v[:30] if isinstance(v, str) and len(v) > 30 else v
                                              for k, v in envelope_vision.items()})

    def _run_content_vision():
        nonlocal content_vision
        content_vision = _vision_analyze_for_naming(content_page)
        logger.info("Content vision: %s", {k: v[:30] if isinstance(v, str) and len(v) > 30 else v
                                             for k, v in content_vision.items()})

    def _run_stamp():
        nonlocal stamp_dates
        os.environ["_MAGI_STAMP_SKIP_VLM"] = "1"
        try:
            _file_mtime = datetime.fromtimestamp(os.path.getmtime(pdf_path))
        except Exception:
            _file_mtime = datetime.now()
        # Stamp on envelope first, then content page
        stamp_pages = []
        if envelope_page is not None:
            stamp_pages.append(envelope_page)
        stamp_pages.append(content_page)
        for sp in stamp_pages:
            try:
                stamp_result = _extract_receipt_date_from_stamp(sp, ref_dt=_file_mtime)
                if stamp_result and stamp_result[0]:
                    stamp_dates.append(stamp_result[0])
                    logger.info("Receipt stamp date: %s (method: %s)", stamp_result[0], stamp_result[1])
            except Exception as e:
                logger.debug("Stamp date extraction failed: %s", e)

    # Run envelope+content vision sequentially (single GPU), stamp in parallel
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        def _run_all_vision():
            _run_envelope_vision()
            _run_content_vision()
        f_vision = pool.submit(_run_all_vision)
        f_stamp = pool.submit(_run_stamp)
        concurrent.futures.wait([f_vision, f_stamp], timeout=240)

    os.environ.pop("_MAGI_STAMP_SKIP_VLM", None)

    # ── Merge: envelope provides court/case/date, content provides type/party ──
    vision_info = {}
    if envelope_page is not None:
        # True envelope doc: envelope has court/case/date, content has type/party
        for key in ("date", "court", "case_number"):
            vision_info[key] = envelope_vision.get(key) or content_vision.get(key) or ""
        for key in ("doc_type", "party", "doc_subtype", "summary", "case_type"):
            vision_info[key] = content_vision.get(key) or envelope_vision.get(key) or ""
    else:
        # No envelope: "envelope vision" = page 0 title page (most authoritative)
        # Page 0 has doc title, case_no, party; later pages have body/details
        for key in ("case_number", "party", "doc_subtype"):
            vision_info[key] = envelope_vision.get(key) or content_vision.get(key) or ""
        for key in ("date", "court", "doc_type", "summary", "case_type"):
            vision_info[key] = content_vision.get(key) or envelope_vision.get(key) or ""
    # Remove empty values
    vision_info = {k: v for k, v in vision_info.items() if v}
    logger.info("Merged vision: %s", {k: v[:30] if isinstance(v, str) and len(v) > 30 else v
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

    # Try envelope text for party extraction
    if not ocr_name and envelope_page is not None:
        env_text = envelope_page.get_text() or ""
        if len(env_text.strip()) < 50 and HAS_OCR:
            env_text = _ocr_page_rapid(envelope_page)
        env_name_m = re.search(r"受送達人\S*[：:\s]+\d?([\u4e00-\u9fffA-Za-z·\-]{2,20})", env_text)
        if env_name_m:
            ocr_name = env_name_m.group(1).strip()
            logger.info("Party from envelope: %s", ocr_name)

    # ── Step 4: Merge — stamp date > OCR date > envelope vision date > content vision date ──
    # Content vision date is least reliable (may contain birthdates or irrelevant dates)
    _env_vision_date = envelope_vision.get("date", "")
    _content_vision_date = content_vision.get("date", "")
    # Reject content vision date if it's too old (likely a birthdate from 被告 bio)
    if _content_vision_date:
        try:
            _cv_year = int(_content_vision_date[:4])
            if _cv_year < 2020:
                logger.info("Rejecting content vision date %s (likely birthdate/old ref)", _content_vision_date)
                _content_vision_date = ""
        except (ValueError, IndexError):
            pass
    _vision_date = _env_vision_date or _content_vision_date
    found_date = stamp_date or ocr_date or _vision_date
    if not date_method:
        if found_date == stamp_date:
            date_method = "stamp"
        elif found_date == ocr_date:
            date_method = "ocr"
        else:
            date_method = "vision"
    # Phase 2D: confidence-weighted merge with dynamic quality discounts
    _v_court = vision_info.get("court", "") or ""
    _v_case_no = vision_info.get("case_number", "") or ""
    _v_type = vision_info.get("doc_type", "") or ""
    _v_party = vision_info.get("party", "") or ""
    found_court, _, _ = _pick_best_source("court", [
        (_v_court, _compute_dynamic_confidence(_v_court, 0.90, "court", "vision"), "vision"),
        (ocr_court, _compute_dynamic_confidence(ocr_court, 0.70, "court", "ocr"), "ocr"),
    ])
    found_case_no, _, _ = _pick_best_source("case_number", [
        (_v_case_no, _compute_dynamic_confidence(_v_case_no, 0.90, "case_no", "vision"), "vision"),
        (ocr_case_no, _compute_dynamic_confidence(ocr_case_no, 0.70, "case_no", "ocr"), "ocr"),
    ])
    found_type, _, _ = _pick_best_source("doc_type", [
        (_v_type, 0.90, "vision"),
        (ocr_type, 0.70, "ocr"),
    ])
    found_party, _, _ = _pick_best_source("party", [
        (_v_party, _compute_dynamic_confidence(_v_party, 0.90, "party", "vision"), "vision"),
        (ocr_name, _compute_dynamic_confidence(ocr_name, 0.70, "party", "ocr"), "ocr"),
    ])
    found_doc_subtype = vision_info.get("doc_subtype", "") or ""
    _vision_summary = vision_info.get("summary", "") or ""
    found_case_type = vision_info.get("case_type", "") or ""  # 刑事/民事/行政

    # ── Step 3b: Extract summary from ALL pages' native text + confidence merge ──
    # OCR summary (0.60) used as fallback; vision summary (0.90) wins if non-empty.
    _ocr_summary = ""
    if found_type:
        all_pages_text = ""
        for pi in range(min(doc.page_count, 6)):
            all_pages_text += (doc[pi].get_text() or "") + "\n"
        if all_pages_text.strip():
            _ocr_summary = _extract_summary_from_ocr(all_pages_text, found_type) or ""
    found_summary, _, _sum_src = _pick_best_source("summary", [
        (_vision_summary, 0.90, "vision"),
        (_ocr_summary, 0.60, "ocr"),
    ])
    if found_summary:
        logger.info("Summary source=%s: %s", _sum_src, found_summary[:60])

    # ── Step 3c: Extract legal action fields (holding/correction_order/deadline) ──
    _ocr_legal = _extract_legal_fields_from_ocr(
        "\n".join(doc[pi].get_text() or "" for pi in range(min(doc.page_count, 6))),
        found_type,
    )
    found_holding = vision_info.get("holding", "") or _ocr_legal.get("holding", "")
    found_correction_order = vision_info.get("correction_order", "") or _ocr_legal.get("correction_order", "")
    found_deadline = vision_info.get("deadline") or _ocr_legal.get("deadline")
    found_deadline_type = vision_info.get("deadline_type", "") or _ocr_legal.get("deadline_type", "")

    # ── Step 4a: Targeted stamp-area vision OCR ──
    # When full-page vision missed the stamp, crop stamp regions and OCR them.
    if not found_date:
        _stamp_crop_regions = [
            ("top_right", (0.55, 0.00, 1.00, 0.35)),
            ("right_strip", (0.72, 0.00, 1.00, 0.55)),
            ("top_left", (0.00, 0.00, 0.45, 0.35)),
            ("bottom_right", (0.55, 0.70, 1.00, 1.00)),
        ]
        # Try stamp on envelope page first, then content page
        _stamp_scan_pages = []
        if doc.page_count > 2:
            _stamp_scan_pages.append(doc[0])
        if content_page is not None and content_page not in _stamp_scan_pages:
            _stamp_scan_pages.append(content_page)

        try:
            from skills.bridge import melchior_client as _mc_s
            _chat_s = getattr(_mc_s, "_chat_omlx", None)
            _avail_s = getattr(_mc_s, "_omlx_available", None)
            if callable(_chat_s) and callable(_avail_s) and _avail_s():
                from skills.bridge.melchior_client import (
                    OMLX_VISION_BASE as _VBS,
                    OMLX_VISION_MODEL as _VMS,
                    _OMLX_VISION_CIRCUIT as _VCS,
                    _OMLX_VISION_LOCK as _VLS,
                )
                stamp_prompt = (
                    "這張圖片是文件角落的裁切區域。請找出收文章（藍色圓形章）上的日期。"
                    "收文章格式通常為民國年.月.日（如115.4.02代表民國115年4月2日=西元2026年4月2日）。"
                    "只回覆日期數字（民國年.月.日格式），看不到收文章就回覆NONE。"
                )
                for sp in _stamp_scan_pages:
                    if found_date:
                        break
                    try:
                        sp_pix = sp.get_pixmap(dpi=220)
                        sp_png = sp_pix.tobytes("png")
                    except Exception:
                        continue
                    for crop_name, (cx0, cy0, cx1, cy1) in _stamp_crop_regions:
                        if found_date:
                            break
                        crop_png = _crop_png_bytes(sp_png, x0=cx0, y0=cy0, x1=cx1, y1=cy1)
                        if not crop_png:
                            continue
                        # Enhance for small stamp text
                        if HAS_PIL:
                            try:
                                from PIL import ImageOps, ImageEnhance
                                im = Image.open(io.BytesIO(crop_png)).convert("L")
                                im = ImageOps.autocontrast(im)
                                w, h = im.size
                                im = im.resize((int(w * 2.0), int(h * 2.0)))
                                im = ImageEnhance.Sharpness(im).enhance(1.6)
                                im = ImageEnhance.Contrast(im).enhance(1.4)
                                buf = io.BytesIO()
                                im.save(buf, format="PNG")
                                crop_png = buf.getvalue()
                            except Exception:
                                pass
                        b64_crop = base64.b64encode(crop_png).decode("utf-8")
                        try:
                            r_stamp = _chat_s(
                                prompt=stamp_prompt, model=_VMS,
                                base_url=_VBS, timeout=45,
                                temperature=0.0, max_tokens=64, images=[b64_crop],
                                circuit=_VCS, lock=_VLS,
                            )
                            if r_stamp.get("success") and r_stamp.get("response"):
                                raw = r_stamp["response"].strip()
                                if "NONE" not in raw.upper():
                                    from vision_parser import _parse_date_from_text
                                    sd = _parse_date_from_text(raw)
                                    if sd:
                                        try:
                                            yr = int(sd[:4])
                                            if 2000 <= yr <= 2030:
                                                found_date = sd
                                                date_method = f"vision_stamp_crop:{crop_name}"
                                                logger.info("Stamp date from crop %s: %s (raw: %s)", crop_name, sd, raw[:40])
                                        except ValueError:
                                            pass
                        except Exception as e:
                            logger.debug("Stamp crop vision failed (%s): %s", crop_name, e)
        except Exception as e:
            logger.debug("Stamp crop vision setup failed: %s", e)

    # Normalize simplified → traditional Chinese
    if found_party:
        try:
            import opencc
            found_party = opencc.OpenCC("s2t").convert(found_party)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1270, exc_info=True)

    if case_name:
        found_party = case_name
    elif not found_party:
        found_party = _infer_party_from_case_folder_path(pdf_path) or ""

    # ── Step 4b: Date fallback — scan last page for 具狀人 date, then filing date ──
    if not found_date and doc.page_count > 0:
        last_page = doc[doc.page_count - 1]
        last_text = last_page.get_text() or ""
        if _is_garbled_text(last_text) or len(last_text.strip()) < 20:
            # Try vision OCR on last page for the filing date
            try:
                from skills.bridge import melchior_client as _mc2
                _chat2 = getattr(_mc2, "_chat_omlx", None)
                _avail2 = getattr(_mc2, "_omlx_available", None)
                if callable(_chat2) and callable(_avail2) and _avail2():
                    pix_last = last_page.get_pixmap(dpi=150)
                    png_last = pix_last.tobytes("png")
                    b64_last = base64.b64encode(png_last).decode("utf-8")
                    from skills.bridge.melchior_client import (
                        OMLX_VISION_BASE as _VB2,
                        OMLX_VISION_MODEL as _VM2,
                        _OMLX_VISION_CIRCUIT as _VC2,
                        _OMLX_VISION_LOCK as _VL2,
                    )
                    r_last = _chat2(
                        prompt="這是文件的最後一頁。請找出文件的簽署日期或具狀日期。只回覆民國年.月.日或西元YYYYMMDD格式的日期，看不到就回覆NONE。",
                        model=_VM2, base_url=_VB2, timeout=60,
                        temperature=0.0, max_tokens=128, images=[b64_last],
                        circuit=_VC2, lock=_VL2,
                    )
                    if r_last.get("success") and r_last.get("response"):
                        from vision_parser import _parse_date_from_text
                        last_date_raw = r_last["response"].strip()
                        if "NONE" not in last_date_raw.upper():
                            last_date = _parse_date_from_text(last_date_raw)
                            if last_date:
                                found_date = last_date
                                date_method = "vision_last_page"
                                logger.info("Date from last page vision: %s", found_date)
            except Exception as e:
                logger.debug("Last page date fallback failed: %s", e)
        else:
            # Try extracting date from last page text
            last_date = _extract_any_date(last_text) or _extract_roc_date(last_text)
            if last_date:
                found_date = last_date
                date_method = "ocr_last_page"
                logger.info("Date from last page text: %s", found_date)

    if not found_date:
        found_date, date_method = _fallback_date_from_filename_or_mtime(pdf_path)
        logger.warning("Could not extract date from %s; fallback=%s", pdf_path, found_date)
    if not found_date:
        return empty_result if return_structured else None

    # ── Step 5: Refine with learned rules + DB templates ──
    # The nightly training system has 2,075 samples and 226 rules — use them!
    _suggested_name_for_learning = found_doc_subtype or found_type or ""
    if _suggested_name_for_learning:
        # 5a: Refine doc_type from learned rules (token-based matching)
        if not found_type or found_type in ("其他", "文件"):
            learned_type = _infer_doc_type_from_learning(_suggested_name_for_learning)
            if learned_type:
                logger.info("Learned rules refined doc_type: %s → %s", found_type, learned_type)
                found_type = learned_type
        # Also try with full filename context (party + subtype)
        if not found_type or found_type in ("其他", "文件"):
            _ctx = f"{found_doc_subtype or ''} {found_party or ''}"
            learned_type = _infer_doc_type_from_learning(_ctx)
            if learned_type:
                logger.info("Learned rules (context) refined doc_type: %s", learned_type)
                found_type = learned_type

    # 5b: Consult DB doc_rules for archive destination + template
    _db_archive_dest = ""
    try:
        from training_loader import get_template_for_doc_type
        db_rule = get_template_for_doc_type(found_type or found_doc_subtype or "")
        if db_rule:
            if db_rule.get("archive_destination_type"):
                _db_archive_dest = db_rule["archive_destination_type"]
                logger.info("DB rule archive dest: %s (for %s)", _db_archive_dest, found_type)
    except Exception:
        pass

    # 5c: AI-assisted structured naming (Gemma 4 + SYSTEM_PROMPT)
    # Only trigger when doc_type is still uncertain to avoid unnecessary GPU calls
    _ai_enabled = os.environ.get("MAGI_PDF_AI_NAME_ENABLED", "1").strip() not in {"0", "false", "no"}
    _type_uncertain = not found_type or found_type in ("其他", "文件", "")
    _ocr_for_ai = content_text or content_text_native or ""
    if _ai_enabled and _type_uncertain and _ocr_for_ai:
        _ai_result = _ai_generate_structured_name(
            ocr_text=_ocr_for_ai,
            already_extracted={
                "date": found_date,
                "court": found_court,
                "case_no": found_case_no,
                "party": found_party,
            },
        )
        if _ai_result and float(_ai_result.get("confidence", 0)) >= 0.70:
            ai_doc_type = _ai_result.get("doc_type", "")
            if ai_doc_type and ai_doc_type not in ("其他", "文件"):
                logger.info("[ai_name] refined doc_type: %s → %s (conf=%.2f)",
                            found_type, ai_doc_type, _ai_result.get("confidence", 0))
                found_type = ai_doc_type
                # Also accept AI-suggested fields if our extraction missed them
                if not found_date and _ai_result.get("date"):
                    found_date = _ai_result["date"]
                    date_method = "ai_structured"
                if not found_party and _ai_result.get("party"):
                    found_party = _ai_result["party"]
                if not found_case_no and _ai_result.get("case_number"):
                    found_case_no = _ai_result["case_number"]

    result = _build_name_result(
        found_date=found_date,
        found_court=found_court,
        found_case_no=found_case_no,
        found_type=found_type,
        found_party=found_party,
        date_method=date_method,
        doc_subtype=found_doc_subtype,
        summary=found_summary,
        case_type_hint=found_case_type,
        deadline=found_deadline,
        deadline_type=found_deadline_type,
    )
    # Attach extracted legal action fields to result for downstream use
    if found_holding:
        result["holding"] = found_holding
    if found_correction_order:
        result["correction_order"] = found_correction_order
    if found_deadline is not None:
        result["deadline"] = found_deadline
    if found_deadline_type:
        result["deadline_type"] = found_deadline_type

    result = _apply_naming_guards(
        result,
        source_hint=_build_source_hint(
            pdf_path,
            case_name=case_name or "",
            snippets=[content_text_native, content_text, vision_info.get("summary", "")],
        ),
    )

    if return_structured:
        return result
    return result["filename"]


def _extract_summary_from_ocr(ocr_text: str, doc_type: str = "") -> str:
    """Extract summary/主文 from OCR text for OSC calendar integration.

    For different doc types:
    - 裁定: 主文 section (e.g. 延長羈押2月、准予停止羈押、應於N日內補正)
    - 判決: 主文 section (e.g. 有期徒刑7月、上訴駁回)
    - 庭通知書: hearing schedule (訂X月X日X時 開庭)
    - 函文: 主旨 section or action items (應於N日內...)
    """
    text = (ocr_text or "")
    dt = (doc_type or "")

    # ── 庭通知書: extract hearing date/time ──
    if "庭通知" in dt or "傳票" in dt:
        m = re.search(
            r"(?:定|訂)\s*(?:於\s*)?(?:民國\s*)?"
            r"(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*"
            r"([上下]午)\s*(\d{1,2})\s*時\s*(\d{0,2})\s*分?"
            r".*?(?:行|進行|為)?\s*(審[理判]|準備|調[解查]|[宣言]詞辯論|開庭)?",
            text,
        )
        if m:
            y = int(m.group(1)) + 1911 if int(m.group(1)) < 1911 else int(m.group(1))
            mo, d = m.group(2), m.group(3)
            ampm, hr, mi = m.group(4), m.group(5), m.group(6) or "0"
            proc = m.group(7) or "開庭"
            return f"訂{y}年{mo}月{d}日{ampm}{hr}時{mi}分{proc}程序"
        # Simpler pattern
        m2 = re.search(r"(?:定|訂)\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*([上下]午)\s*(\d{1,2})\s*時\s*(\d{0,2})\s*分?", text)
        if m2:
            return f"訂{m2.group(1)}月{m2.group(2)}日{m2.group(3)}{m2.group(4)}時{(m2.group(5) or '整')}開庭"

    # ── 裁定/判決: extract 主文 ──
    if "裁定" in dt or "判決" in dt:
        # Key ruling phrases — check these first for clean, structured summaries
        # Build composite summary from multiple matching phrases
        parts = []
        for pat, label in [
            (r"(?:自|自民國)\s*(?:民國\s*)?(\d{2,3})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日起\s*[，,]?\s*(?:延長羈押|延長覊押)\s*(\d+)\s*月",
             "自民國{0}年{1}月{2}日起，延長羈押{3}月"),
            (r"(?:延長羈押|延長覊押)\s*(?:貳|參|壹|肆|伍)?\s*月", None),  # handled by above
            (r"(?:延長羈押|延長覊押|延長轟押|延長義押)\s*(?:貳|參|壹|肆|伍|式)?\s*月", None),  # handled by pattern above
            (r"(?:延長羈押|延長覊押|延長轟押|延長義押)\s*(\d+)\s*月", "延長羈押{0}月"),
            (r"提出\s*(?:新[臺台]幣|新豊幣)?\s*([\d,萬參拾]+)\s*(?:萬\s*)?(?:え{0,2}\s*)?元?\s*(?:之?\s*)?保證金.*?(?:准[予子]停止[羈覊轟義]押)", "於提出{0}元保證金後，准予停止羈押"),
            (r"准[予子]停止[羈覊轟義]押", "准予停止羈押"),
            (r"禁止接見\s*(?:、\s*)?通信\s*(?:及\s*收受物件)?", "並禁止接見通信及收受物件"),
            (r"禁[止上]\s*接\s*見", "並禁止接見通信及收受物件"),
            (r"上訴駁回", "上訴駁回"),
            (r"(\d+)\s*日\s*內\s*補正", "{0}日內補正"),
        ]:
            if label is None:
                continue
            pm = re.search(pat, text)
            if pm:
                groups = pm.groups()
                try:
                    s = label.format(*groups) if groups else label
                except (IndexError, KeyError):
                    s = label
                if s and s not in " ".join(parts):
                    parts.append(s)
        if parts:
            return "；".join(parts)[:80]

        # Fallback: 主文 section (between 主文 and 理由)
        m = re.search(r"主\s*文[：:\s]*\n?(.*?)(?:\n\s*(?:理\s*由|事\s*實|犯罪事實)|$)", text, re.DOTALL)
        if m:
            raw = re.sub(r"\s+", "", m.group(1).strip())[:60]
            if raw and len(raw) > 4:
                return raw

    # ── 函文: extract 主旨 or action items ──
    if "函" in dt or "通知" in dt:
        m = re.search(r"主\s*旨[：:\s]*(.+?)(?:\n|。|$)", text)
        if m:
            raw = re.sub(r"\s+", "", m.group(1).strip())[:60]
            if raw:
                return raw
        # Action item patterns
        m2 = re.search(r"(?:應於|文到)\s*(\d+)\s*日\s*內\s*([\u4e00-\u9fff]+)", text)
        if m2:
            return f"應於文到{m2.group(1)}日內{m2.group(2)}"

    # ── 起訴書: brief description ──
    if "起訴" in dt:
        m = re.search(r"犯\s*([\u4e00-\u9fff]+(?:罪|法))", text)
        if m:
            return m.group(1)

    return ""


def _extract_legal_fields_from_ocr(ocr_text: str, doc_type: str = "") -> dict:
    """Extract structured legal fields from OCR text via regex.

    Returns dict with keys: holding, correction_order, deadline, deadline_type.
    All fields default to "" or None if not found.
    """
    text = ocr_text or ""
    # Opus 驗收補丁 D-3: OCR 引擎常把「內」(U+5167) 讀成「内」(U+5185)；
    # 兩者在繁中法律文書是同一字，統一正規化，避免所有 deadline regex 漏抓真實 PDF。
    text = text.replace("\u5185", "\u5167")
    dt = doc_type or ""
    fields = {"holding": "", "correction_order": "", "deadline": None, "deadline_type": ""}

    # Extract holding (判決主文 first line)
    if "判決" in dt or "裁定" in dt:
        m = re.search(r"主\s*文[：:\s]*\n?(.*?)(?:\n|。|$)", text, re.DOTALL)
        if m:
            raw = re.sub(r"\s+", "", m.group(1).strip())[:30]
            if raw and len(raw) > 3:
                fields["holding"] = raw

    # Extract correction_order + deadline
    _DEADLINE_PATTERNS = [
        (r"應於本裁定送達後\s*(\d+)\s*日內\s*(補正)", "補正"),
        (r"(?:應於|限於)\s*文到\s*(\d+)\s*日內\s*(補正)", "補正"),
        (r"命.+?於\s*(\d+)\s*日內\s*(補正)", "補正"),
        (r"應於\s*(\d+)\s*日內\s*(補正)", "補正"),
        (r"上訴期間.*?送達.*?(\d+)\s*日內", None),
        (r"如不服本判決.+?(\d+)\s*日內.+?(上訴)", "上訴"),
        (r"應於判決送達後\s*(\d+)\s*日內提起\s*(上訴)", "上訴"),
        (r"應於文到\s*(\d+)\s*日內\s*(陳述意見)", "陳述意見"),
        (r"限於\s*(\d+)\s*日內.+?(陳述意見)", "陳述意見"),
        # Opus D-3: 真實函文常用「陳報」而非「陳述意見」（如「文到10日內陳報如說明」）
        (r"(?:應於|限於|於)?\s*文到\s*(\d+)\s*日內\s*(陳報)", "陳報"),
        (r"應於\s*(\d+)\s*日內\s*(陳報)", "陳報"),
        (r"應於文到\s*(\d+)\s*日內繳納.+(規費|裁判費)", "繳費"),
        (r"限\s*(\d+)\s*日內.+?繳納.+?(裁判費|規費)", "繳費"),
        (r"應於\s*(\d+)\s*日內.+(閱卷)", "閱卷期限"),
        (r"閱卷期限.+?(\d+)\s*日", None),
    ]
    for pat, dtype in _DEADLINE_PATTERNS:
        pm = re.search(pat, text)
        if pm:
            groups = pm.groups()
            days_str = groups[0] if groups else None
            if days_str and days_str.isdigit():
                fields["deadline"] = int(days_str)
            if dtype:
                fields["deadline_type"] = dtype
                # Build correction_order from matched text
                snippet = pm.group(0)[:20]
                fields["correction_order"] = snippet
            break

    # Extract correction_order from 函文 主旨 if not yet set
    if not fields["correction_order"] and ("函" in dt or "通知" in dt):
        m2 = re.search(r"(?:應於|文到)\s*(\d+)\s*日\s*內\s*([\u4e00-\u9fff]+)", text)
        if m2:
            fields["correction_order"] = f"應於文到{m2.group(1)}日內{m2.group(2)[:10]}"
            if not fields["deadline"]:
                fields["deadline"] = int(m2.group(1))
            if not fields["deadline_type"]:
                act = m2.group(2)
                if "補正" in act:
                    fields["deadline_type"] = "補正"
                elif "陳述意見" in act:
                    fields["deadline_type"] = "陳述意見"
                else:
                    fields["deadline_type"] = act[:4]

    return fields


_VISION_OCR_BIN = os.path.expanduser("~/Library/Application Support/MAGI/bin/vision_ocr")


def _macos_vision_ocr_page(pdf_path: str, page_num: int = 0) -> str:
    """Use macOS Vision framework for OCR — high quality, free, no GPU needed.
    This is the primary OCR engine. Falls back to oMLX Gemma vision if unavailable."""
    if not os.path.exists(_VISION_OCR_BIN):
        return ""
    try:
        r = subprocess.run(
            [_VISION_OCR_BIN, pdf_path, str(page_num)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0 and r.stdout.strip():
            text = r.stdout.strip()
            logger.info("[macOS-Vision] page %d: %d chars", page_num, len(text))
            return text
    except Exception as e:
        logger.debug("[macOS-Vision] failed: %s", e)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2B: Multi-OCR Consensus Mechanism
# Enable with env: MAGI_PDF_OCR_CONSENSUS=1
# Engines: RapidOCR (300 DPI) + Tesseract (PSM4 + PSM6) + macOS Vision
# ─────────────────────────────────────────────────────────────────────────────

_PDF_OCR_CONSENSUS = os.environ.get("MAGI_PDF_OCR_CONSENSUS", "0").strip() in ("1", "true", "yes")
try:
    _CHANDRA_OCR_MIN_SCORE = float(os.environ.get("MAGI_CHANDRA_OCR_MIN_SCORE", "0.45") or "0.45")
except (TypeError, ValueError):
    _CHANDRA_OCR_MIN_SCORE = 0.45


def _score_ocr_text(text: str) -> float:
    """Score OCR result quality.  Higher = better.
    Factors: Chinese character density, total length, non-garbage ratio.
    """
    if not text or not text.strip():
        return 0.0
    s = text.strip()
    total = len(s)
    if total == 0:
        return 0.0
    # Count CJK characters (繁體常見字範圍)
    cjk = sum(1 for c in s if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf")
    # Count obvious garbage: replacement chars, lone punctuation runs
    garbage = s.count("\ufffd") + len(re.findall(r"[□■▪▫]{2,}", s))
    cjk_ratio = cjk / total
    garbage_penalty = min(garbage / max(total, 1), 1.0)
    # Reward longer texts (log scale), penalise garbage
    length_bonus = min(math.log1p(total) / 8.0, 1.0)
    score = (cjk_ratio * 0.6 + length_bonus * 0.4) * (1.0 - garbage_penalty)
    return round(score, 4)


def _chandra_ocr_page(pdf_path: str, page_idx: int = 0) -> str:
    """Optional Chandra OCR fallback for hard scanned PDFs.

    This path is off by default and returns an empty string on any unavailable
    backend/license/server condition, so it cannot destabilize the existing
    Vision/RapidOCR/Gemma path.
    """
    try:
        from skills.engine.ocr import chandra_provider
        if not chandra_provider.enabled():
            return ""
        result = chandra_provider.run_pdf_page(pdf_path, page_num=page_idx)
        if result.success and result.text.strip():
            logger.info("[Chandra-OCR] page %d: %d chars in %.2fs", page_idx, len(result.text), result.duration_sec)
            return result.text.strip()
        if result.error:
            logger.info("[Chandra-OCR] unavailable for page %d: %s", page_idx, result.error)
    except Exception as exc:
        logger.debug("[Chandra-OCR] failed: %s", exc)
    return ""


def _prefer_chandra_if_better(current_text: str, pdf_path: str, page_idx: int = 0) -> str:
    if not pdf_path:
        return current_text
    current_score = _score_ocr_text(current_text)
    if current_score >= _CHANDRA_OCR_MIN_SCORE:
        return current_text
    chandra_text = _chandra_ocr_page(pdf_path, page_idx)
    if not chandra_text:
        return current_text
    chandra_score = _score_ocr_text(chandra_text)
    if chandra_score > current_score:
        logger.info(
            "[Chandra-OCR] selected page %d score %.3f > %.3f",
            page_idx,
            chandra_score,
            current_score,
        )
        return chandra_text
    return current_text


def _preprocess_receipt_image(img_bytes: bytes, scale: int = 4) -> bytes:
    """Preprocess postal receipt images for better OCR:
    4x magnification + high contrast + binarization.
    Falls back to original bytes if PIL/Pillow not available.
    """
    try:
        from PIL import Image, ImageEnhance, ImageFilter
        import io
        img = Image.open(io.BytesIO(img_bytes)).convert("L")
        new_w = img.width * scale
        new_h = img.height * scale
        img = img.resize((new_w, new_h), Image.LANCZOS)
        # Enhance contrast significantly for stamp/small text
        img = ImageEnhance.Contrast(img).enhance(2.5)
        img = ImageEnhance.Sharpness(img).enhance(2.0)
        # Binarize with threshold
        img = img.point(lambda p: 255 if p > 128 else 0, "1").convert("L")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return img_bytes


def _ocr_page_rapid_dpi(page, dpi: int = 300) -> str:
    """RapidOCR at configurable DPI (Phase 2B: use 300 DPI for better accuracy)."""
    if not ocr_engine:
        return ""
    try:
        pix = page.get_pixmap(dpi=dpi)
        img_bytes = pix.tobytes("png")
        result, _ = ocr_engine(img_bytes)
        if not result:
            return ""
        return "\n".join(line[1] for line in result if isinstance(line, (list, tuple)) and len(line) >= 2)
    except Exception as e:
        logger.debug("RapidOCR %ddpi failed: %s", dpi, e)
        return ""


def _ocr_image_bytes_tesseract_dual(img_bytes: bytes, timeout_sec: int = 8) -> str:
    """Run Tesseract in both PSM-4 (single column) and PSM-6 (block) modes.
    Returns the higher-scoring result.
    """
    if not HAS_TESSERACT or not img_bytes:
        return ""
    t4 = _ocr_image_bytes_tesseract(img_bytes, timeout_sec=timeout_sec, psm=4)
    t6 = _ocr_image_bytes_tesseract(img_bytes, timeout_sec=timeout_sec, psm=6)
    s4, s6 = _score_ocr_text(t4), _score_ocr_text(t6)
    return t4 if s4 >= s6 else t6


def _extract_date_consensus(texts: List[str]) -> Optional[str]:
    """Majority vote on date extraction from multiple OCR texts.
    Returns the date string that appears most frequently, or the best single hit.
    """
    date_votes: Dict[str, int] = {}
    for text in texts:
        if not text:
            continue
        d = _extract_any_date(text)
        if d:
            date_votes[d] = date_votes.get(d, 0) + 1
    if not date_votes:
        return None
    # Return the date with the most votes; tie-break by more recent date
    best = max(date_votes, key=lambda k: (date_votes[k], k))
    return best


def _ocr_consensus(page, pdf_path: str = "", page_idx: int = 0) -> str:
    """Run multiple OCR engines on a page and return the best-quality result.

    Engines (run in parallel where possible):
      1. macOS Vision (via binary) — highest quality for Traditional Chinese
      2. RapidOCR at 300 DPI
      3. Tesseract dual PSM (4 + 6)

    Scoring: `_score_ocr_text()` selects the winner.
    Date consensus: `_extract_date_consensus()` used for date fields.

    Returns: str (text of the winning engine)
    """
    import concurrent.futures as _cf

    results: Dict[str, str] = {}

    def _run_macos():
        if pdf_path and os.path.exists(_VISION_OCR_BIN):
            return _macos_vision_ocr_page(pdf_path, page_idx)
        return ""

    def _run_rapid():
        if ocr_engine and page is not None:
            return _ocr_page_rapid_dpi(page, dpi=300)
        return ""

    def _run_tess():
        if HAS_TESSERACT and page is not None:
            try:
                pix = page.get_pixmap(dpi=200)
                img_bytes = pix.tobytes("png")
                return _ocr_image_bytes_tesseract_dual(img_bytes)
            except Exception:
                return ""
        return ""

    with _cf.ThreadPoolExecutor(max_workers=3) as pool:
        futs = {
            "macos": pool.submit(_run_macos),
            "rapid": pool.submit(_run_rapid),
            "tess": pool.submit(_run_tess),
        }
        for name, fut in futs.items():
            try:
                results[name] = fut.result(timeout=30) or ""
            except Exception as exc:
                logger.warning("[ocr-consensus] %s failed: %s", name, exc)
                results[name] = ""

    scores = {name: _score_ocr_text(text) for name, text in results.items()}
    logger.info("[ocr-consensus] page %d scores: %s", page_idx,
                {k: f"{v:.3f}(len={len(results[k])})" for k, v in scores.items()})

    # Pick winner
    best_engine = max(scores, key=lambda k: scores[k])
    best_text = results[best_engine]
    if pdf_path:
        best_text = _prefer_chandra_if_better(best_text, pdf_path, page_idx)

    if not best_text.strip():
        # All engines failed: fall back to basic RapidOCR
        if page is not None:
            rapid_text = _ocr_page_rapid(page)
            return _prefer_chandra_if_better(rapid_text, pdf_path, page_idx)
        return ""

    engines_used = [k for k, v in results.items() if v]
    logger.info("ocr_consensus engines=%s winner=%s score=%.3f", engines_used, best_engine, scores[best_engine])
    return best_text


def _glm_ocr_page(page, dpi: int = 200) -> str:
    """Stage 1 fallback: Use oMLX vision model to transcribe a page image to text.
    Used when macOS Vision OCR is unavailable. Function name kept for backwards compat;
    actual model is now MAGI_OMLX_VISION_MODEL (Gemma E4B by default)."""
    try:
        from skills.bridge import melchior_client as _mc
        _chat_omlx = getattr(_mc, "_chat_omlx", None)
        _omlx_avail = getattr(_mc, "_omlx_available", None)
        if not (callable(_chat_omlx) and callable(_omlx_avail) and _omlx_avail()):
            return ""
    except Exception:
        return ""

    try:
        pix = page.get_pixmap(dpi=dpi)
        png = pix.tobytes("png")
        b64 = base64.b64encode(png).decode("utf-8")

        prompt = (
            "請逐字轉錄這張文件圖片中所有可見的文字與數字。"
            "包括章戳內的文字、信封上的地址、案號、法院名稱、當事人姓名等。"
            "民國年日期格式如 115.3.20 或 115年3月20日 請原樣轉錄。"
            "只轉錄看到的文字，不要推論、不要解釋、不要添加格式。"
            "若完全��不到任何文字回覆 NONE。"
        )
        vision_model = getattr(_mc, "OMLX_VISION_MODEL", os.environ.get("MAGI_TEXT_PRIMARY_MODEL", ""))
        from skills.bridge.melchior_client import (
            OMLX_VISION_BASE, _OMLX_VISION_CIRCUIT, _OMLX_VISION_LOCK,
        )
        # Retry once on failure (model loading delay causes first request to fail)
        for _attempt in range(2):
            r = _chat_omlx(
                prompt=prompt, model=vision_model,
                base_url=OMLX_VISION_BASE,
                timeout=90, temperature=0.0, max_tokens=2048, images=[b64],
                circuit=_OMLX_VISION_CIRCUIT, lock=_OMLX_VISION_LOCK,
            )
            if r.get("success") and r.get("response"):
                break
            if _attempt == 0:
                import time
                logger.info("oMLX vision OCR: retrying after model load delay...")
                time.sleep(8)  # Wait for LRU model swap
        if not (r.get("success") and r.get("response")):
            return ""
        ocr_text = (r.get("response") or "").strip()
        if "NONE" in ocr_text.upper():
            return ""
        # Clean hallucination
        if re.search(r"(.)\1{19,}", ocr_text):
            ocr_text = re.sub(r"(.)\1{9,}", r"\1\1\1", ocr_text)
        if len(ocr_text) > 200:
            for i in range(0, min(len(ocr_text), 100), 10):
                chunk = ocr_text[i:i + 10]
                if len(chunk) >= 8 and ocr_text.count(chunk) >= 5:
                    ocr_text = ocr_text[:300]
                    break
        return ocr_text
    except Exception as e:
        logger.debug("oMLX vision OCR failed: %s", e)
        return ""


def _is_omlx_port_up(port: int = 8080, timeout: float = 0.5) -> bool:
    """Fast socket probe — returns True if oMLX is listening on the given port."""
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def _analyze_ocr_text(ocr_text: str) -> dict:
    """Stage 2 only: Use Gemma E4B (oMLX port 8080) to analyze OCR text, regex fallback.
    Probes port availability first — skips LLM immediately if oMLX is offline."""
    if not ocr_text or len(ocr_text.strip()) < 10:
        return {}

    # Probe port before attempting LLM call to avoid 60s timeout when oMLX is offline
    result = {}
    if _is_omlx_port_up(8080):
        try:
            from skills.bridge import melchior_client as _mc
            _chat_fn = getattr(_mc, "_chat_omlx", None)
            result = _gemma4_analyze_ocr_text(ocr_text, _chat_fn)
        except Exception:
            result = {}
    else:
        logger.info("oMLX port 8080 offline — skipping Gemma E4B, using regex fallback")

    # Regex fallback for missing fields
    if not result.get("court") and not result.get("case_number"):
        v_date = _extract_any_date(ocr_text) or _extract_roc_date(ocr_text)
        if v_date:
            result.setdefault("date", v_date)
        v_court = _extract_court_name(ocr_text)
        if v_court:
            result.setdefault("court", v_court)
        v_case_no = _extract_case_number(ocr_text)
        if v_case_no:
            result.setdefault("case_number", v_case_no)
        v_type = _extract_doc_type(ocr_text)
        if v_type:
            result.setdefault("doc_type", v_type)
        v_name = _extract_name(ocr_text, default_name=None)
        if v_name:
            result.setdefault("party", v_name)

    # Extract doc_subtype from first line
    if not result.get("doc_subtype"):
        _TITLE_RE = re.compile(
            r"^((?:刑事|民事|行政|消費者債務清理)?(?:[\u4e00-\u9fff]*)"
            r"(?:狀|書|筆錄|裁定|判決|契約|收據|委任|方案|清冊|聲請|通知書|起訴書))"
        )
        for line in ocr_text.split("\n"):
            line = line.strip()
            if len(line) < 4 or len(line) > 30:
                continue
            if any(k in line for k in ("法院文件", "無法依", "規定投遞", "送達通知", "郵局")):
                continue
            tm = _TITLE_RE.search(line)
            if tm:
                result["doc_subtype"] = re.sub(r"\s+", "", tm.group(1))
                break

    # Extract summary
    if not result.get("summary"):
        result["summary"] = _extract_summary_from_ocr(ocr_text, result.get("doc_type", ""))

    return result


def _vision_analyze_for_naming(content_page) -> dict:
    """Analyze a page using two-stage pipeline: OCR → Gemma E4B (oMLX).

    Stage 1 OCR priority (no port 8080 needed):
      1. macOS Vision (vision_ocr binary) — best quality for printed Traditional Chinese
      2. RapidOCR                          — fast local fallback
      3. oMLX port 8080 (Gemma vision)    — last resort when local engines unavailable

    Stage 2: Gemma E4B text LLM (oMLX port 8080) with regex fallback when offline.
    Garbled OCR text (simplified Chinese artifacts, kana noise) is rejected early.
    """
    if os.environ.get("MAGI_PDF_NAMER_USE_VISION", "1").strip() in {"0", "false", "no", "off"}:
        return {}

    try:
        # Stage 1: OCR — prefer local engines, avoid port 8080 where possible
        ocr_text = ""

        # 1a. macOS Vision (best for printed Traditional Chinese)
        _pdf_path = getattr(getattr(content_page, "parent", None), "name", "") or ""
        _page_num = getattr(content_page, "number", 0)
        if _pdf_path and os.path.exists(_VISION_OCR_BIN):
            ocr_text = _macos_vision_ocr_page(_pdf_path, _page_num)

        # 1b. RapidOCR fallback
        if not ocr_text and HAS_OCR:
            ocr_text = _ocr_page_rapid(content_page) or ""

        # 1c. oMLX port 8080 (legacy path, last resort — requires oMLX running)
        if not ocr_text and _is_omlx_port_up(8080):
            ocr_text = _glm_ocr_page(content_page, dpi=200)

        # 1d. Chandra layout OCR (explicit opt-in) for empty/low-quality pages.
        if _pdf_path:
            ocr_text = _prefer_chandra_if_better(ocr_text, _pdf_path, _page_num)

        if not ocr_text:
            logger.info("Vision OCR: no text from page.")
            return {}

        # Reject garbled text (simplified Chinese OCR artifacts, kana noise, etc.)
        # — prevents garbage doc_subtype/party from polluting the merged result
        if _is_garbled_text(ocr_text):
            logger.info("Vision OCR: garbled text detected (%d chars), skipping page.", len(ocr_text))
            return {}

        logger.info("Vision OCR transcription (%d chars): %s", len(ocr_text), ocr_text[:200])

        # Stage 2: Analysis (Gemma E4B + regex fallback)
        return _analyze_ocr_text(ocr_text)

    except Exception as e:
        logger.error("Vision naming failed: %s", e)
        return {}


def _gemma4_analyze_ocr_text(ocr_text: str, _chat_fn=None) -> dict:
    """Use Gemma 4 (text LLM on port 8080) to extract structured fields from OCR text.

    This is a text-only call — no image. Gemma 4 excels at understanding
    Chinese legal documents when given clean(ish) OCR text.
    """
    if not ocr_text or len(ocr_text.strip()) < 20:
        return {}

    prompt = (
        "你是法律事務所文件管理助手。根據以下法院文件的OCR文字提取命名欄位。\n\n"
        f"文件內容：\n{ocr_text[:1500]}\n\n"
        "回覆JSON（不要markdown符號、不要其他文字）：\n"
        '{"court":"法院或檢察署全名(null=找不到)",'
        '"case_no":"完整案號如115年度原侵重訴字第1號(null=找不到)",'
        '"case_type":"刑事或民事或行政(null=無法判斷)",'
        '"doc_type":"裁定/判決/庭通知書/函/起訴書/不起訴處分書/聲請書/陳報狀/答辯狀/委任狀/筆錄/其他",'
        '"doc_subtype":"完整文件標題如臺灣花蓮地方法院刑事裁定(null=找不到)",'
        '"party":"被告或原告或聲請人姓名(null=找不到)",'
        '"summary":"主文或主旨摘要60字以內(null=找不到)",'
        '"holding":"判決主文第一條摘要(僅限判決/裁定類,null=無)",'
        '"correction_order":"補正事項核心(若含應於...補正/陳述意見,摘20字,null=無)",'
        '"deadline":"期限天數如30(若有文到N日內,null=無)",'
        '"deadline_type":"期限類型:補正/上訴/陳述意見/繳費/閱卷(null=無)"}'
    )

    try:
        from skills.bridge.melchior_client import OMLX_CHAT_BASE
        if _chat_fn and callable(_chat_fn):
            r = _chat_fn(
                prompt=prompt,
                model="",  # use default model on chat port
                base_url=OMLX_CHAT_BASE,
                timeout=60,
                temperature=0.0,
                max_tokens=512,
            )
        else:
            import requests as _req
            base = (os.environ.get("MAGI_OMLX_CHAT_URL") or "http://127.0.0.1:8080").rstrip("/")
            resp = _req.post(f"{base}/v1/chat/completions", json={
                "model": "",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.0,
                "max_tokens": 512,
                "stream": False,
            }, timeout=60)
            if resp.status_code != 200:
                return {}
            r = {"success": True, "response": resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")}

        if not r.get("success") or not r.get("response"):
            return {}

        raw = r["response"].strip()
        # Strip markdown code fences
        raw = re.sub(r"```json\s*", "", raw)
        raw = re.sub(r"```\s*$", "", raw)
        raw = raw.strip()

        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\{[\s\S]*\}", raw)
            if m:
                try:
                    obj = json.loads(m.group(0))
                except json.JSONDecodeError:
                    return {}
            else:
                return {}

        if not isinstance(obj, dict):
            return {}

        # Map to our internal field names
        result = {}
        if obj.get("court") and obj["court"] != "null":
            result["court"] = str(obj["court"])
        if obj.get("case_no") and obj["case_no"] != "null":
            result["case_number"] = str(obj["case_no"])
        if obj.get("case_type") and obj["case_type"] != "null":
            result["case_type"] = str(obj["case_type"])
        if obj.get("doc_type") and obj["doc_type"] != "null":
            result["doc_type"] = str(obj["doc_type"])
        if obj.get("doc_subtype") and obj["doc_subtype"] != "null":
            result["doc_subtype"] = str(obj["doc_subtype"])
        if obj.get("party") and obj["party"] != "null":
            result["party"] = str(obj["party"])
        if obj.get("summary") and obj["summary"] != "null":
            result["summary"] = str(obj["summary"])[:80]

        logger.info("Gemma4 analysis: %s", {k: v[:30] if isinstance(v, str) and len(v) > 30 else v
                                              for k, v in result.items()})
        return result

    except Exception as e:
        logger.debug("Gemma4 analysis failed: %s", e)
        return {}


_PARROTED_PATTERNS = re.compile(
    r"[\(（].*[\)）]"  # Values wrapped in parens like "(完整法院名"
    r"|完整法院名|完整案號|債權人/原告|聲請人的姓名|YYYYMMDD"
    r"|如臺灣|如115年|法院名，如",
    re.IGNORECASE,
)


def _is_parroted(value: str) -> bool:
    """Detect if a value is parroted from the prompt template."""
    v = (value or "").strip()
    if not v:
        return True
    return bool(_PARROTED_PATTERNS.search(v))


def _parse_naming_response(text: str) -> dict:
    """Parse structured naming response from Vision model.
    Handles both plain text and markdown formats."""
    result = {}

    # Normalize markdown bold markers
    normalized = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    # Remove markdown list markers
    normalized = re.sub(r"^\s*[*·•\-]\s+", "", normalized, flags=re.MULTILINE)

    # Detect wholesale parroting: if the response contains format placeholders
    if "(完整法院名" in normalized or "(完整案號" in normalized or "YYYYMMDD" in normalized:
        logger.warning("Vision response is parroting the prompt — discarding")
        return {}

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
        if court != "無" and len(court) >= 4 and not _is_parroted(court):
            court = re.sub(r"[，,。;；\n].*", "", court).strip()
            if "法院" in court:
                result["court"] = court

    # Parse case number
    cn = re.search(r"案號\s*[:：]\s*(.+)", normalized)
    if cn:
        case_no = cn.group(1).strip().rstrip("。，,")
        if case_no != "無" and "年" in case_no and "字" in case_no and not _is_parroted(case_no):
            case_no = re.sub(r"[，,。;；\n].*", "", case_no).strip()
            result["case_number"] = case_no

    # Parse doc type — must be a single type, not a comma-separated list
    tm = re.search(r"文件類型\s*[:：]\s*(.+)", normalized)
    if tm:
        raw_type = tm.group(1).strip().rstrip("。，,")
        raw_type = re.sub(r"[，,。;；\n].*", "", raw_type).strip()
        # Reject multi-value lists (sign of parroting from prompt)
        if "、" in raw_type and raw_type.count("、") >= 2:
            logger.debug("Rejecting multi-value doc_type (likely parroted): %s", raw_type)
            raw_type = ""
        if raw_type and raw_type != "無" and raw_type != "公文封" and len(raw_type) >= 2 and not _is_parroted(raw_type):
            result["doc_type"] = raw_type

    # Parse party name
    pm = re.search(r"當事人\s*[:：]\s*(.+)", normalized)
    if pm:
        name = pm.group(1).strip().rstrip("。，,")
        name = re.sub(r"^[\s*·•\-]+", "", name).strip()
        if name != "無" and len(name) >= 2 and not _is_parroted(name):
            name = re.sub(r"^(原告|被告|聲請人|債權人|債務人)\s*[:：]?\s*", "", name)
            name = re.sub(r"[，,。;；\n].*", "", name).strip()
            if len(name) >= 2 and len(name) <= 20:
                result["party"] = name

    # Fallback for party
    if "party" not in result:
        for label in ["債權人", "原告", "聲請人"]:
            pm2 = re.search(rf"{label}\s*[:：]\s*([\u4e00-\u9fffA-Za-z·\-]{{2,20}})", normalized)
            if pm2:
                candidate = pm2.group(1).strip()
                if not _is_parroted(candidate):
                    result["party"] = candidate
                    break

    return result

def _trigger_osc_sync_if_applicable(new_path: str, result: dict) -> None:
    """快速 regex 預檢檔名 bracket 是否含期限，命中才呼 OSC sync。

    避免每次 rename 都 spawn subprocess（45s timeout × 兩次）。
    """
    if os.environ.get("PDF_NAMER_OSC_TODO_SYNC", "1") != "1":
        return

    name = os.path.basename(new_path)
    if not re.search(r"\d+日內(補正|上訴|陳述意見|繳納|繳費|閱卷)", name):
        return

    try:
        import importlib.util as _ilu
        _sf_spec = _ilu.spec_from_file_location(
            "smart_filer",
            os.path.join(os.path.dirname(__file__), "smart_filer.py"),
        )
        _sf_mod = _ilu.module_from_spec(_sf_spec)
        _sf_spec.loader.exec_module(_sf_mod)
        sync_result = _sf_mod.sync_osc_todos_for_path(new_path)
    except Exception as _e:
        logger.debug("OSC sync import error: %s", _e, exc_info=True)
        return

    logger.info("OSC sync result for %s: %s", name, sync_result)


def rename_file(pdf_path: str, case_name: str = None, dry_run: bool = False):
    pdf_path = _resolve_pdf_with_synology_fallback(pdf_path)
    structured = generate_name_proposal(pdf_path, case_name, return_structured=True)
    if not structured or not (isinstance(structured, dict) and structured.get("filename")):
        logger.warning(f"Could not determine name for {pdf_path}")
        return

    proposal = structured["filename"]
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
        # 觸發 OSC todo sync（best-effort，失敗不影響 rename 結果）
        try:
            _trigger_osc_sync_if_applicable(new_path, structured)
        except Exception:
            logger.debug("OSC sync trigger raised", exc_info=True)

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

def _select_pages_scored(doc) -> dict:
    """Content-based page selection using scoring.
    Returns {"envelope": page_or_None, "content": page, "envelope_idx": int, "content_idx": int}"""
    if doc.page_count == 0:
        return {"envelope": None, "content": None, "envelope_idx": -1, "content_idx": -1}

    scores = []
    _ENV_MARKERS = ["受送達人", "公文封", "公丈封", "公支封", "郵務送達", "寄存送達",
                     "送達人住居所", "送達人居住所", "訴訟當事人注意事項", "訴訟權益"]
    _CONTENT_MARKERS = ["案號", "被告", "原告", "主文", "主旨", "聲請人", "犯罪事實",
                         "理由", "當事人", "上訴人", "抗告人", "裁定", "判決"]

    # Also detect garbled versions of content markers
    _GARBLED_CONTENT_MARKERS = [
        "案", "琥", "股", "聾", "彗", "債務", "清理", "陳報", "答辯",
        "聲請", "起訴", "裁定", "判決", "通知", "函",
    ]

    for i in range(min(5, doc.page_count)):
        text = doc[i].get_text() or ""
        score = 0
        for m in _ENV_MARKERS:
            if m in text:
                score -= 10
        for m in _CONTENT_MARKERS:
            if m in text:
                score += 2
        # Garbled markers (partial matches from bad OCR layers)
        garbled_hits = sum(1 for m in _GARBLED_CONTENT_MARKERS if m in text)
        if garbled_hits >= 3:
            score += 1  # Moderate boost for garbled title pages
        # Phase 2C: garbled text penalty
        if _is_garbled_text(text):
            score -= 5
        # Page 0 bonus: first page is most likely the title/header
        # (court docs: title page; our docs: 聲請狀/陳報狀 first page)
        if i == 0:
            score += 2
        # Text density bonus
        score += min(len(text.strip()) / 200, 3)
        scores.append((score, i))

    scores.sort(key=lambda x: x[0])
    # Lowest score = most likely envelope, highest = most likely content
    envelope_idx = scores[0][1] if scores[0][0] < -5 else -1
    content_idx = scores[-1][1]

    # If envelope == content, pick next best for content
    if envelope_idx == content_idx and len(scores) > 1:
        content_idx = scores[-2][1]

    envelope = doc[envelope_idx] if envelope_idx >= 0 else None
    content = doc[content_idx] if content_idx >= 0 else doc[0]

    return {"envelope": envelope, "content": content,
            "envelope_idx": envelope_idx, "content_idx": content_idx}


def batch_ocr_pages(pdf_paths: list) -> dict:
    """Phase 1: Batch OCR all pages using oMLX vision model (stays loaded throughout).

    Returns {pdf_path: {"envelope_ocr": str, "content_ocr": str, "pages": dict}}
    """
    # Pre-warm vision model: force model load before batch starts
    # This ensures all OCR requests hit a loaded model (no swap delays)
    try:
        import requests as _req
        from skills.bridge.melchior_client import OMLX_VISION_BASE, OMLX_VISION_MODEL
        _req.post(
            f"{OMLX_VISION_BASE}/v1/chat/completions",
            json={"model": OMLX_VISION_MODEL or "gemma-4-e4b-it-4bit",
                  "messages": [{"role": "user", "content": "test"}],
                  "max_tokens": 1, "stream": False},
            timeout=30,
        )
        logger.info("[batch-ocr] vision model pre-warmed")
    except Exception as e:
        logger.debug("[batch-ocr] Pre-warm failed (OK if model already loaded): %s", e)

    results = {}
    for pdf_path in pdf_paths:
        try:
            doc = fitz.open(pdf_path)
            if doc.needs_pass:
                try:
                    doc.authenticate("3800")
                except Exception:
                    pass

            pages = _select_pages_scored(doc)
            envelope_ocr = ""
            content_ocr = ""

            # Primary OCR: macOS Vision framework (fast, high quality, no GPU)
            # Fallback: oMLX Gemma vision (slower but handles edge cases)
            def _ocr_page(page_idx: int) -> str:
                # Phase 2B: multi-engine consensus when MAGI_PDF_OCR_CONSENSUS=1
                if _PDF_OCR_CONSENSUS and page_idx < doc.page_count:
                    return _ocr_consensus(doc[page_idx], pdf_path=pdf_path, page_idx=page_idx)
                # Default: macOS Vision primary → oMLX Gemma vision fallback
                text = _macos_vision_ocr_page(pdf_path, page_idx)
                if not text and page_idx < doc.page_count:
                    text = _glm_ocr_page(doc[page_idx], dpi=200)
                text = _prefer_chandra_if_better(text, pdf_path, page_idx)
                return text

            if pages["envelope"]:
                envelope_ocr = _ocr_page(pages["envelope_idx"])
                if envelope_ocr:
                    logger.info("[batch-ocr] %s env(%d): %d chars",
                                os.path.basename(pdf_path), pages["envelope_idx"], len(envelope_ocr))

            if pages["content"]:
                content_ocr = _ocr_page(pages["content_idx"])
                if content_ocr:
                    logger.info("[batch-ocr] %s content(%d): %d chars",
                                os.path.basename(pdf_path), pages["content_idx"], len(content_ocr))

            # For no-envelope docs: also OCR page 0 if it wasn't the content page
            if not pages["envelope"] and pages["content_idx"] != 0 and doc.page_count > 1:
                p0_ocr = _ocr_page(0)
                if p0_ocr:
                    logger.info("[batch-ocr] %s page0-title: %d chars",
                                os.path.basename(pdf_path), len(p0_ocr))
                    envelope_ocr = p0_ocr

            results[pdf_path] = {
                "envelope_ocr": envelope_ocr,
                "content_ocr": content_ocr,
                "pages": pages,
                "doc": doc,
            }
        except Exception as e:
            logger.error("[batch-ocr] %s failed: %s", os.path.basename(pdf_path), e)
            results[pdf_path] = {"envelope_ocr": "", "content_ocr": "", "pages": {}, "doc": None}

    # Retry pass: re-OCR pages that returned empty (model swap may have caused timeout)
    retry_needed = [(p, r) for p, r in results.items()
                    if not r.get("envelope_ocr") and not r.get("content_ocr") and r.get("pages")]
    if retry_needed:
        logger.info("[batch-ocr] Retrying %d PDFs with empty OCR results...", len(retry_needed))
        import time
        time.sleep(5)  # Allow model to stabilize
        for pdf_path, r in retry_needed:
            pages = r["pages"]
            if pages.get("content"):
                content_ocr = _glm_ocr_page(pages["content"], dpi=200)
                if content_ocr:
                    r["content_ocr"] = content_ocr
                    logger.info("[batch-ocr-retry] %s content: %d chars", os.path.basename(pdf_path), len(content_ocr))
            if pages.get("envelope"):
                envelope_ocr = _glm_ocr_page(pages["envelope"], dpi=200)
                if envelope_ocr:
                    r["envelope_ocr"] = envelope_ocr

    logger.info("[batch-ocr] Phase 1 complete: %d PDFs OCR'd", len(results))
    return results


def batch_analyze_texts(ocr_results: dict) -> dict:
    """Phase 2: Batch analyze all OCR texts using Gemma 4 (stays loaded throughout).

    Returns {pdf_path: {"envelope_info": dict, "content_info": dict, "merged": dict}}
    """
    # Pre-warm Gemma 4: force model swap from vision model
    try:
        import requests as _req
        from skills.bridge.melchior_client import OMLX_CHAT_BASE, TEXT_PRIMARY_MODEL as _TEXT_MODEL
        _req.post(
            f"{OMLX_CHAT_BASE}/v1/chat/completions",
            json={"model": _TEXT_MODEL,
                  "messages": [{"role": "user", "content": "test"}],
                  "max_tokens": 1, "stream": False},
            timeout=60,
        )
        logger.info("[batch-analyze] Gemma 4 pre-warmed")
    except Exception as e:
        logger.debug("[batch-analyze] Pre-warm note: %s", e)
    results = {}
    for pdf_path, ocr in ocr_results.items():
        envelope_info = {}
        content_info = {}

        if ocr.get("envelope_ocr"):
            envelope_info = _analyze_ocr_text(ocr["envelope_ocr"])
            logger.info("[batch-analyze] %s envelope: %s",
                        os.path.basename(pdf_path),
                        {k: str(v)[:25] for k, v in envelope_info.items() if v})

        if ocr.get("content_ocr"):
            content_info = _analyze_ocr_text(ocr["content_ocr"])
            logger.info("[batch-analyze] %s content: %s",
                        os.path.basename(pdf_path),
                        {k: str(v)[:25] for k, v in content_info.items() if v})

        # Merge: envelope provides date/court/case_no; content provides type/party/summary
        merged = {}
        # Distinguish true envelope (公文封) from title page (page 0 of no-envelope doc)
        pages_info = ocr.get("pages") or {}
        has_real_envelope = pages_info.get("envelope_idx", -1) >= 0
        if has_real_envelope:
            for key in ("date", "court", "case_number"):
                merged[key] = envelope_info.get(key) or content_info.get(key) or ""
            for key in ("doc_type", "party", "doc_subtype", "summary", "case_type"):
                merged[key] = content_info.get(key) or envelope_info.get(key) or ""
        else:
            # No envelope: "envelope_info" = title page (page 0), most authoritative
            # Trust title page for case_number, party, doc_type, doc_subtype
            for key in ("case_number", "party", "doc_subtype", "doc_type"):
                merged[key] = envelope_info.get(key) or content_info.get(key) or ""
            for key in ("date", "court", "summary", "case_type"):
                merged[key] = content_info.get(key) or envelope_info.get(key) or ""

        merged = {k: v for k, v in merged.items() if v}
        results[pdf_path] = {
            "envelope_info": envelope_info,
            "content_info": content_info,
            "merged": merged,
        }

    # Cache results + original OCR text so generate_name_proposal() can use both
    global _BATCH_ANALYSIS_CACHE
    for pdf_path, info in results.items():
        # Include original OCR text from ocr_results for stamp date extraction
        ocr_data = ocr_results.get(pdf_path, {})
        info["envelope_ocr"] = ocr_data.get("envelope_ocr", "")
        info["content_ocr"] = ocr_data.get("content_ocr", "")
        info["pages"] = ocr_data.get("pages", {})
        _BATCH_ANALYSIS_CACHE[pdf_path] = info

    # ── Phase 2b: Infer receipt context from batch neighbors ──
    # Postal receipts (掛號回執) often can't be OCR'd. Infer their content from
    # neighboring PDFs in the same batch (scanned together = same mailing).
    sorted_paths = sorted(results.keys())
    for i, pdf_path in enumerate(sorted_paths):
        info = results[pdf_path]
        merged = info.get("merged", {})
        # Detect unreadable receipt: no doc_type, no party, very little OCR
        total_ocr = len(info.get("envelope_ocr", "")) + len(info.get("content_ocr", ""))
        if total_ocr < 50 and not merged.get("doc_type"):
            # Check page count — receipts are usually 2 pages (front + back)
            doc = ocr_results.get(pdf_path, {}).get("doc")
            page_count = doc.page_count if doc else 0
            if page_count <= 2:
                # This is likely a postal receipt. Find the nearest document
                # with content to infer what was mailed.
                for offset in [-1, -2, 1, 2]:
                    neighbor_idx = i + offset
                    if 0 <= neighbor_idx < len(sorted_paths):
                        neighbor = results[sorted_paths[neighbor_idx]].get("merged", {})
                        if neighbor.get("doc_type") and neighbor.get("party"):
                            # Build receipt name from neighbor context
                            n_sub = neighbor.get("doc_subtype", neighbor.get("doc_type", ""))
                            n_party = neighbor.get("party", "")
                            merged["doc_type"] = "回執"
                            merged["doc_subtype"] = n_sub
                            merged["party"] = n_party
                            # Use neighbor's date or file modification date
                            if not merged.get("date"):
                                merged["date"] = neighbor.get("date", "")
                            info["merged"] = merged
                            logger.info("[batch-receipt] %s inferred as receipt for '%s(%s)' from neighbor %s",
                                        os.path.basename(pdf_path), n_sub, n_party,
                                        os.path.basename(sorted_paths[neighbor_idx]))
                            break

    logger.info("[batch-analyze] Phase 2 complete: %d PDFs analyzed and cached", len(results))
    return results


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


def _shorten_court(court: str) -> str:
    """Keep court name as-is (users expect full name including 臺灣 prefix)."""
    return (court or "").strip()


def _fallback_date_from_filename_or_mtime(pdf_path: str) -> Tuple[Optional[str], str]:
    bn = os.path.basename(pdf_path or "")
    m = re.match(r"^(20\d{6})", bn)
    if m:
        return m.group(1), "filename_prefix_fallback"
    try:
        return datetime.fromtimestamp(os.path.getmtime(pdf_path)).strftime("%Y%m%d"), "file_mtime_fallback"
    except Exception:
        return None, ""


def _resolve_doc_category(doc_type: str) -> Optional[str]:
    """Map extracted doc_type to a DOC_CATEGORIES key from naming_rules."""
    dt = (doc_type or "").strip()
    if not dt:
        return None
    # Direct match on internal type names (from DOC_TYPE_MAP values)
    _TYPE_TO_CATEGORY = {
        "判決": "判決",
        "裁定": "裁定",
        "法院_通知": "法院通知", "法院_傳票": "法院通知",
        "庭通知書": "庭通知書", "法院通知": "法院通知",
        "函文": "函文",
        "起訴書": "起訴書", "不起訴處分書": "書狀_對造",
        "聲請簡易判決處刑書": "書狀_對造",
        "預酬回執": "收據", "委任狀回執": "收據", "回執": "收據",
        "對造_書狀": "書狀_對造",
        "書狀_我方": "書狀_我方", "書狀_對造": "書狀_對造",
        "陳報狀": "書狀_我方", "答辯狀": "書狀_我方",
        "聲請書": "書狀_我方", "抗告狀": "書狀_我方", "上訴狀": "書狀_我方",
        "委任狀": "委任相關",
        "訊問筆錄": "筆錄", "調查筆錄": "筆錄", "準備程序筆錄": "筆錄",
        "審判筆錄": "筆錄", "勘驗筆錄": "筆錄",
        "收據": "收據",
        "債清_書狀": "債清_書狀",
    }
    if dt in _TYPE_TO_CATEGORY:
        return _TYPE_TO_CATEGORY[dt]
    # Fallback: keyword scan
    for kw, cat in [("判決", "判決"), ("裁定", "裁定"), ("庭通知", "庭通知書"),
                    ("起訴書", "起訴書"), ("回執", "收據"),
                    ("函", "函文"), ("筆錄", "筆錄"), ("書狀", "書狀_我方"),
                    ("陳報", "書狀_我方"), ("答辯", "書狀_我方"),
                    ("聲請", "書狀_我方"), ("上訴", "書狀_我方"),
                    ("債清", "債清_書狀"), ("委任", "委任相關"),
                    ("通知", "法院通知"), ("傳票", "法院通知")]:
        if kw in dt:
            return cat
    return None


# OSC todos.py 的 bracket regex 使用「繳納」「閱卷」，需把 Vision/OCR 抽出的詞彙正規化
# Opus D-3: 「陳報」是函文最常見動作之一（如「文到10日內陳報如說明」），
# OSC regex 只認「陳述意見」，故把陳報正規化為陳述意見以觸發 todo_sync。
_OSC_KEYWORDS = {
    "繳費": "繳納",
    "閱卷期限": "閱卷",
    "陳報": "陳述意見",
}

# 5 個類別在括號內注入 deadline（對齊 OSC regex）
_DEADLINE_INJECT_CATEGORIES = {"判決", "裁定", "庭通知書", "函文", "法院通知"}


def _build_name_result(
    *,
    found_date: Optional[str],
    found_court: Optional[str] = "",
    found_case_no: Optional[str] = "",
    found_type: Optional[str] = "",
    found_party: Optional[str] = "",
    date_method: str = "",
    doc_subtype: Optional[str] = "",
    summary: Optional[str] = "",
    suffix: Optional[str] = "",
    case_type_hint: Optional[str] = "",
    deadline: Optional[str] = None,
    deadline_type: Optional[str] = "",
) -> dict:
    """Build filename following naming_rules.DOC_CATEGORIES templates.

    Template patterns:
      判決/裁定:    {date} {court}{case_no}判決（{party}；{summary}）
      庭通知書:     {date} {court}{case_no}庭通知書（{party}；{summary}）
      函文:         {date} {court}{case_no}函（{party}；主旨：{summary}）
      書狀_我方:    {date} {doc_subtype}({party}){suffix}
      法院通知:     {date} {court}{case_no}{doc_subtype}
      其他:         {date} {doc_subtype}
    """
    # Validate date range
    if found_date and len(found_date) == 8:
        try:
            year = int(found_date[:4])
            if year < 2000 or year > 2030:
                logger.warning("Rejecting implausible date %s (year %d)", found_date, year)
                found_date = None
        except ValueError:
            found_date = None

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

    court = _shorten_court(found_court)
    case_no = found_case_no or ""
    party = found_party or ""
    dt = found_type or ""
    # For pure 回執 (postal receipt), subtype from delivery record page often
    # contains form text ("口附上原掛號收據") rather than the receipt label —
    # use doc_type directly so the name is just "回執".
    if dt == "回執":
        sub = dt
    else:
        sub = doc_subtype or dt or ""
    if sub:
        sub = re.sub(r"(留底|留存)$", "存底", sub)
    sfx = suffix or ""

    category = _resolve_doc_category(dt)

    # ── Naming templates (derived from actual user-named files) ──
    # All parens are full-width （）
    # Court documents: {date} {court}{case_no}{case_type}{doc_type}（{party}；{summary}）
    # Our statements:  {date} {doc_subtype}存底（{party}）
    # Receipts:        {date} {回執type}（{party}）
    # Opponent docs:   {date} {court}{case_no}{case_type}{doc_type}繕本（{party}；{summary}）

    # Clean up summary — remove "被告XXX" prefix, convert 大寫數字 to 阿拉伯數字
    smry = (summary or "").strip()
    if smry and party:
        # Remove "被告XXX" / "聲請人XXX" prefix from summary
        smry = re.sub(r"^(?:被告|原告|聲請人|上訴人|抗告人)\s*" + re.escape(party) + r"\s*", "", smry)
    # Convert 壹貳參肆伍陸柒捌玖拾 → 1-10
    _NUM_MAP = {"壹": "1", "貳": "2", "參": "3", "肆": "4", "伍": "5",
                "陸": "6", "柒": "7", "捌": "8", "玖": "9", "拾": "10",
                "佰": "百", "仟": "千"}
    for k, v in _NUM_MAP.items():
        smry = smry.replace(k, v)
    # Truncate to 80 chars
    smry = smry[:80].rstrip("。，、；")

    case_type = (case_type_hint or "").strip()  # 刑事/民事/行政 prefix
    if not case_type and sub:
        for ct in ("刑事", "民事", "行政"):
            if ct in sub:
                case_type = ct
                break

    if category == "判決":
        body = f"{court}{case_no}{case_type}判決"
        if party:
            body += f"（{party}；{smry}）" if smry else f"（{party}）"
        elif smry:
            body += f"（{smry}）"
        else:
            body += "（待補摘要）"
    elif category == "裁定":
        body = f"{court}{case_no}{case_type}裁定"
        if party:
            body += f"（{party}；{smry}）" if smry else f"（{party}）"
        elif smry:
            body += f"（{smry}）"
        else:
            body += "（待補摘要）"
    elif category == "庭通知書":
        body = f"{court}{case_no}{case_type}庭通知書"
        if party:
            body += f"（{party}；{smry}）" if smry else f"（{party}）"
        elif smry:
            body += f"（{smry}）"
        else:
            body += "（待補摘要）"
    elif category == "函文":
        body = f"{court}{case_no}{case_type}函" if case_type else f"{court}{case_no}函"
        if "庭" in (sub or ""):
            body = f"{court}{case_no}{case_type}庭函"
        if party:
            body += f"（{party}；{smry}）" if smry else f"（{party}）"
        elif smry:
            body += f"（{smry}）"
        else:
            body += "（待補摘要）"
    elif category == "起訴書":
        # {date} {court/署}{case_no}起訴書（{party}）
        body = f"{court}{case_no}起訴書"
        if party:
            body += f"（{party}）"
    elif category in ("書狀_我方", "債清_書狀"):
        # {date} {doc_subtype}存底（{party}）
        body = sub or "書狀"
        if sfx:
            body += sfx
        elif not re.search(r"(存底|副本|繕本)$", body):
            body += "存底"
        if party:
            body += f"（{party}）"
    elif category == "書狀_對造":
        # Opponent docs: {court}{case_no}{doc_subtype}繕本（{party}；{summary}）
        body = ""
        if court:
            body += court
        if case_no:
            body += case_no
        body += (sub or "書狀") + "繕本"
        if party:
            body += f"（{party}；{smry}）" if smry else f"（{party}）"
    elif category == "法院通知":
        body = f"{court}{case_no}"
        if "傳票" in dt or "傳票" in sub:
            body += "傳票"
        elif "庭函" in sub or ("函" in sub and "庭" in sub):
            body += f"{case_type}庭函" if case_type else "函"
        elif "函" in dt or "函" in sub:
            body += f"{case_type}函" if case_type else "函"
        else:
            body += "通知"
        if party:
            body += f"（{party}；{smry}）" if smry else f"（{party}）"
    elif category == "委任相關":
        body = sub or "委任狀"
        if sfx:
            body += sfx
        if party:
            body += f"（{party}）"
    elif category == "收據":
        # 回執命名格式: {寄出文件名}({當事人})掛號郵件收件回執
        # 或: 預酬回執（{當事人}）  / 委任狀回執（{當事人}）
        if "預付酬金" in sub or "預酬" in sub or "領款單" in sub:
            body = "預酬回執"
        elif "委任" in sub:
            body = "委任狀回執"
        elif sub and "掛號" not in sub and "回執" not in sub:
            # The sub is the mailed document name — append 掛號郵件收件回執
            body = f"{sub}掛號郵件收件回執"
        else:
            body = sub or "掛號郵件收件回執"
        if party:
            body += f"（{party}）"
    elif category == "筆錄":
        body = sub or dt or "筆錄"
        if summary:
            body += f"（{smry}）"
    else:
        # Fallback: generic
        body = ""
        if court:
            body += court
        if case_no:
            body += case_no
        if sub:
            body += sub
        elif dt:
            body += dt
        if not body:
            body = "文件"
        if party and party != "Unknown":
            body += f"（{party}）"

    # 注入 deadline 到括號（僅 5 個白名單類別）
    # Opus 驗收補丁 D-1: Vision prompt 回純數字 30，OCR 回 int(days)；
    # 原 `"日" in str(deadline)` 對 int/純數字永遠 fail → 先正規化為 "N日內" 字串
    if category in _DEADLINE_INJECT_CATEGORIES and deadline:
        _dl_raw = str(deadline).strip()
        # 純數字 → "{N}日內"
        if _dl_raw.isdigit():
            _dl_raw = f"{_dl_raw}日內"
        if "日" in _dl_raw:
            normalized_type = _OSC_KEYWORDS.get(deadline_type or "", deadline_type or "")
            deadline_part = f"{_dl_raw}{normalized_type}"
            if body.endswith("）"):
                body = body[:-1] + f"；{deadline_part}）"
            else:
                body += f"（{deadline_part}）"

    new_name = f"{found_date} {body}.pdf"
    new_name = re.sub(r'[/\\:*?"<>|]', "", new_name)
    result["filename"] = new_name
    return result


def _is_garbled_text(text: str) -> bool:
    """Detect garbled OCR text (e.g. embedded bad OCR layers producing
    mixed Japanese katakana/symbols with Chinese characters).
    Real Chinese legal documents contain zero Japanese kana."""
    if not text or len(text) < 30:
        return True
    # Count Japanese-specific characters (katakana + hiragana)
    jp_chars = len(re.findall(r'[\u30A0-\u30FF\u3040-\u309F\uFF65-\uFF9F]', text))
    # Any meaningful presence of Japanese kana in a Chinese legal doc = garbled
    if jp_chars >= 5:
        return True
    # Count fullwidth latin that shouldn't appear in Chinese legal docs
    fw_junk = len(re.findall(r'[\uFF10-\uFF5A]', text))
    total_cjk = len(re.findall(r'[\u4e00-\u9fff]', text))
    if total_cjk == 0:
        return True
    if fw_junk > 0 and (fw_junk / total_cjk) > 0.05:
        return True
    return False


def _maybe_fast_text_name_result(
    content_text: str,
    *,
    case_name: Optional[str] = None,
    pdf_path: str = "",
) -> Optional[dict]:
    """
    Fast path for searchable PDFs.

    When the page already contains enough native text, OCR/Vision adds latency and
    can hallucinate unrelated court/case metadata. Prefer deterministic parsing.
    """
    text = (content_text or "").strip()
    if len(text) < 30:
        return None
    # Reject garbled OCR text (bad embedded OCR layers from scanners)
    if _is_garbled_text(text):
        logger.debug("Fast text path: garbled text detected, skipping")
        return None

    found_date = _extract_any_date(text) or _extract_roc_date(text)
    found_court = _extract_court_name(text)
    found_case_no = _extract_case_number(text)
    found_type = _extract_doc_type(text)
    found_party = case_name or _extract_name(text, default_name=None)
    if not found_party and pdf_path:
        found_party = _infer_party_from_case_folder_path(pdf_path)

    if found_party:
        try:
            import opencc

            found_party = opencc.OpenCC("s2t").convert(found_party)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1664, exc_info=True)

    if not found_date or not (found_court or found_case_no or found_type):
        return None

    # Opus 驗收補丁 D-2: fast text path 也要抽 deadline/deadline_type，否則
    # 含 text layer 的掃描 PDF 會完全跳過 Task A 的 deadline 注入
    _legal = _extract_legal_fields_from_ocr(text, found_type or "")
    _fast_deadline = _legal.get("deadline")
    _fast_deadline_type = _legal.get("deadline_type", "")

    return _build_name_result(
        found_date=found_date,
        found_court=found_court,
        found_case_no=found_case_no,
        found_type=found_type,
        found_party=found_party,
        date_method="ocr_fast_path",
        deadline=_fast_deadline,
        deadline_type=_fast_deadline_type,
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
        choices=["rename_file", "review_name", "file", "file_sync", "file_worker", "file_status", "self_train", "self_test", "help"],
    )
    parser.add_argument("--path", help="PDF file path")
    parser.add_argument("--case_name", help="Client Name (e.g. 游秀鈴)")
    parser.add_argument("--execute", default="0", help="Execute filing (1=yes)")
    parser.add_argument("--notify", default="1", help="Send notification (1=yes)")
    parser.add_argument("--job-id", default="", help="Background job id")
    args = parser.parse_args()

    if args.task == "help":
        print(json.dumps({"skill": "pdf-namer", "tasks": ["rename_file", "review_name", "file", "file_sync", "file_worker", "file_status", "self_train", "self_test"], "description": "PDF 智慧命名與歸檔"}, ensure_ascii=False, indent=2))
        return

    if args.task == "self_test":
        errors = []
        warnings = []
        checks = {}

        # 1. Core imports
        try:
            import fitz as _fitz  # noqa: F401
            checks["fitz"] = True
        except ImportError as e:
            errors.append("PyMuPDF (fitz) missing: " + str(e)[:80])
            checks["fitz"] = False

        try:
            from rapidocr_onnxruntime import RapidOCR as _RapidOCR  # noqa: F401
            checks["rapidocr"] = True
        except ImportError:
            warnings.append("rapidocr_onnxruntime not installed; OCR path will use fallback")
            checks["rapidocr"] = False

        # 2. macOS Vision availability
        try:
            import Vision  # noqa: F401
            checks["vision"] = True
        except ImportError:
            try:
                import objc  # noqa: F401
                checks["vision"] = True
            except ImportError:
                warnings.append("macOS Vision (pyobjc) not importable; will use RapidOCR only")
                checks["vision"] = False

        # 3. naming_validator importable
        try:
            from skills.pdf_namer.naming_validator import validate_filename as _vf  # noqa: F401
            checks["naming_validator"] = True
        except ImportError:
            try:
                _nv_path = Path(__file__).parent / "naming_validator.py"
                if _nv_path.exists():
                    checks["naming_validator"] = True
                else:
                    errors.append("naming_validator.py missing from pdf-namer skill directory")
                    checks["naming_validator"] = False
            except Exception:
                checks["naming_validator"] = False

        # 4. Smoke: generate_name_proposal on a tiny synthetic PDF
        try:
            import tempfile as _tf
            _tmp = _tf.NamedTemporaryFile(suffix=".pdf", delete=False)
            _tmp_path = _tmp.name
            _tmp.close()
            _doc = fitz.open()
            _page = _doc.new_page()
            _page.insert_text((50, 100), "臺灣花蓮地方法院 113年度原訴字第024號 判決", fontsize=12)
            _doc.save(_tmp_path)
            _doc.close()
            result = generate_name_proposal(_tmp_path, case_name="測試", return_structured=True)
            os.unlink(_tmp_path)
            checks["name_proposal_smoke"] = bool(result)
        except Exception as e:
            warnings.append("name_proposal smoke failed: " + str(e)[:120])
            checks["name_proposal_smoke"] = False

        ok = len(errors) == 0
        out = {"success": ok, "checks": checks}
        if errors:
            out["errors"] = errors
        if warnings:
            out["warnings"] = warnings
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    if args.task == "review_name":
        if not args.path:
            print("Error: --path required")
            return
        result_r = generate_name_proposal(args.path, args.case_name, return_structured=True)
        if result_r and isinstance(result_r, dict):
            print(f"Proposed Name: {result_r.get('filename', '')}")
            dl = result_r.get("deadline", "")
            dl_type = result_r.get("deadline_type", "")
            if dl and dl_type:
                print(f"Deadline: {dl}{dl_type}")
        else:
            print(f"Proposed Name: {result_r}")
    
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

    elif args.task == "self_test":
        errors = []
        try:
            import fitz as _fitz  # noqa: F401
        except Exception as e:
            errors.append(f"fitz import failed: {e}")
        try:
            from skills.pdf_namer.naming_validator import validate_filename as _vf  # type: ignore
        except Exception:
            try:
                import importlib.util as _ilu
                _spec = _ilu.spec_from_file_location(
                    "naming_validator",
                    os.path.join(os.path.dirname(__file__), "naming_validator.py"),
                )
                _nm = _ilu.module_from_spec(_spec)
                _spec.loader.exec_module(_nm)
                _vf = _nm.validate_filename
            except Exception as e:
                errors.append(f"naming_validator import failed: {e}")
                _vf = None
        if _vf:
            _sample = "20260101 臺灣花蓮地方法院判決（王小明）.pdf"
            _ok_flag, _issues = _vf(_sample)
            if not _ok_flag:
                errors.append(f"naming_validator rejected valid sample: {_issues}")
        result = {"success": len(errors) == 0, "errors": errors or None}
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
