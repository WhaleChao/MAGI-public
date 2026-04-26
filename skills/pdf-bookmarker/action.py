#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pdf-bookmarker/action.py  v2.0
================================
自動掃描法院卷宗 PDF，根據文件首頁特徵建立書籤目錄。

設計原則：
- 日期 OR 文件類型，有一個就標（不再要求兩者同時存在）
- 涵蓋民事、刑事、家事、消債常見文件類型
- 直接寫入原檔（不產出 _bookmarked 分身）
- 掃描頁 OCR fallback（RapidOCR）
- 支援合併卷宗（多份文件合在同一 PDF）的文件邊界偵測
"""

import argparse
import fitz  # PyMuPDF
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Optional

MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

try:
    from rapidocr_onnxruntime import RapidOCR
    HAS_OCR = True
except ImportError:
    HAS_OCR = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("pdf-bookmarker")

ocr_engine = RapidOCR() if HAS_OCR else None

try:
    from bookmark_validator import validate_bookmark
except Exception:
    validate_bookmark = None

# ═══════════════════════════════════════════════════════════════════════════════
# 文件類型定義 — 依辨識優先順序排列
# ═══════════════════════════════════════════════════════════════════════════════

# (regex_pattern, display_label, level)
# level 1 = 主要文件, level 2 = 次要/附件
DOC_PATTERNS: list[tuple[re.Pattern, str, int]] = []

def _p(pattern: str, label: str, level: int = 1):
    """Helper to build pattern list."""
    DOC_PATTERNS.append((re.compile(pattern), label, level))

# ── 卷宗封面 ──
_p(r"(?:刑事|民事|家事|少年|消債).*卷宗", "卷宗封面", 1)

# ── 筆錄類（最常翻找） ──
_p(r"審判(?:筆錄|程序筆錄)", "審判筆錄", 1)
_p(r"準備程序筆錄", "準備程序筆錄", 1)
_p(r"(?:訊問|讯问)筆錄", "訊問筆錄", 1)
_p(r"(?:調查|调查)筆錄", "調查筆錄", 1)
_p(r"(?:勘驗|勘验)筆錄", "勘驗筆錄", 1)
_p(r"(?:調解|和解)(?:程序)?筆錄", "調解/和解筆錄", 1)
_p(r"言詞辯論筆錄", "言詞辯論筆錄", 1)

# ── 裁判類 ──
_p(r"(?:刑事|民事)?判決(?:書)?", "判決", 1)
_p(r"(?:刑事|民事)?裁定(?:書)?", "裁定", 1)

# ── 書狀類 — 檢察官/法院 ──
_p(r"起訴書", "起訴書", 1)
_p(r"追加起訴書", "追加起訴書", 1)
_p(r"不起訴處分書", "不起訴處分書", 1)
_p(r"緩起訴處分書", "緩起訴處分書", 1)
_p(r"聲請簡易判決處刑書", "聲請簡易判決處刑書", 1)
_p(r"公訴檢察官[論論]告書", "論告書", 1)
_p(r"(?:公訴檢察官)?論告(?:要旨|書|意旨)", "論告書", 1)

# ── 書狀類 — 當事人 ──
_p(r"(?:刑事|民事|家事)?答辯(?:狀|書|意旨)", "答辯狀", 1)
_p(r"(?:刑事|民事|家事)?(?:上訴|抗告)(?:狀|書|理由)", "上訴/抗告狀", 1)
_p(r"(?:刑事|民事|家事)?陳報(?:狀|書)", "陳報狀", 1)
_p(r"(?:刑事|民事|家事)?聲請(?:狀|書)", "聲請狀", 1)
_p(r"(?:刑事|民事|家事)?補充(?:理由|上訴|告訴)(?:狀|書)", "補充理由狀", 1)
_p(r"量刑(?:辯論|鑑定)?意旨(?:狀|書)?", "量刑辯論意旨狀", 1)
_p(r"辯護(?:意旨|要旨)(?:狀|書)?", "辯護意旨狀", 1)

# ── 委任/選任 ──
_p(r"(?:選任辯護人)?委任(?:狀|書)", "委任狀", 2)

# ── 函文/公文 ──
_p(r"(?:臺灣|最高|智慧財產).*(?:法院|檢察署|地檢署)\s*函", "法院函", 1)
_p(r"(?:警察局|分局|派出所|調查[處局站])\s*函", "警察機關函", 2)
_p(r"(?:移送書|移送函)", "移送書", 1)

# ── 送達/傳喚/提訊 ──
_p(r"送達證書", "送達證書", 2)
_p(r"(?:合議審理|審理)?傳票", "傳票", 2)
_p(r"提票", "提票", 2)
_p(r"拘票", "拘票", 1)
_p(r"押票", "押票", 1)
_p(r"搜索票", "搜索票", 1)
_p(r"通緝書", "通緝書", 1)

# ── 證據/鑑定 ──
_p(r"鑑定(?:報告|書|意見)", "鑑定報告", 1)
_p(r"(?:精神|心理)鑑定", "精神鑑定報告", 1)
_p(r"(?:法醫|解剖|相驗).*(?:報告|鑑定|證明)", "法醫報告", 1)
_p(r"驗傷診斷(?:書|證明)", "驗傷診斷書", 1)
_p(r"(?:死亡|相驗).*(?:證明書|屍體)", "相驗屍體證明書", 1)
_p(r"診斷(?:證明|書)", "診斷證明書", 2)
_p(r"(?:扣押物品|贓證物品)(?:目錄表|清單|收據)", "扣押物品目錄表", 1)
_p(r"調取扣押物條", "調取扣押物條", 2)
_p(r"(?:搜索|扣押)(?:筆錄|紀錄)", "搜索扣押筆錄", 1)
_p(r"勘(?:查|察|驗)(?:報告|紀錄)", "勘查報告", 1)

# ── 前科/在監 ──
_p(r"(?:前案紀錄表|前科(?:紀錄|資料))", "前案紀錄表", 1)
_p(r"(?:在監在押|矯正機關)", "在監在押資料", 2)
_p(r"(?:全國刑案|刑案資料)", "刑案資料", 2)

# ── 審理單/報到單 ──
_p(r"案件審理單", "審理單", 2)
_p(r"報到單", "報到單", 2)

# ── 消債/民事特殊 ──
_p(r"(?:財產|所得|稅務).*(?:清冊|資料|歸戶)", "財產所得資料", 1)
_p(r"(?:債權人|清冊|債權)(?:表|清冊)", "債權人清冊", 1)
_p(r"(?:更生|清算)(?:方案|計畫)", "更生/清算方案", 1)
_p(r"(?:調解|和解)(?:方案|條件|筆錄|書)", "調解/和解", 1)

# ── 其他常見 ──
_p(r"(?:戶籍|戶口).*(?:謄本|資料)", "戶籍謄本", 2)
_p(r"(?:土地|建物).*(?:登記|謄本)", "土地/建物謄本", 2)
_p(r"(?:存摺|帳戶|金融).*(?:交易|明細)", "金融交易明細", 2)
_p(r"(?:照片|相片|截圖|翻拍)(?:.*張)?", "照片/截圖", 2)
_p(r"(?:本票|支票|借據|契約)", "票據/契約", 2)
_p(r"(?:收據|收文|發文)", "收發文", 2)
_p(r"(?:通訊監察|監聽)(?:書|譯文)", "通訊監察", 1)
_p(r"(?:監視器|錄影).*(?:畫面|截圖|翻拍)", "監視器畫面", 2)

KNOWN_DOC_LABELS = {label for _, label, _ in DOC_PATTERNS}
SINGLE_DOC_FILENAME_HINT_RE = re.compile(
    r"(?:同意書|證明書|委任|上訴理由狀|聲明上訴|答辯狀|陳報狀|聲請狀|信件|回執|收據)"
)
_STRONG_DOC_SIGNAL_PATTERNS = [
    ("判決", re.compile(r"(?:刑事|民事|家事)?判決(?:書)?|主文")),
    ("裁定", re.compile(r"(?:刑事|民事|家事)?裁定(?:書)?")),
    ("起訴書", re.compile(r"起訴書|公訴檢察官|追加起訴書")),
    ("筆錄", re.compile(r"(?:審判|準備程序|言詞辯論|訊問|調查|勘驗).{0,3}筆錄")),
    ("聲請狀", re.compile(r"(?:刑事|民事|家事)?聲請(?:狀|書)")),
    ("答辯狀", re.compile(r"(?:刑事|民事|家事)?答辯(?:狀|書|意旨)")),
    ("前案紀錄表", re.compile(r"(?:前案紀錄表|前科(?:紀錄|資料))")),
    ("報到單", re.compile(r"報到單")),
    ("收發文", re.compile(r"(?:收發文|收文章|發文字號|收文日期|發文日期)")),
    ("照片/截圖", re.compile(r"(?:照片|相片|截圖|翻拍)")),
]
_AGENCY_SIGNAL_PATTERNS = [
    ("法院", re.compile(r"(?:臺灣|高等|地方法院|最高法院)")),
    ("檢察", re.compile(r"(?:檢察署|地檢署|地方檢察)")),
    ("警察", re.compile(r"(?:警察局|分局|派出所|刑事警察)")),
]
_PAGE_BREAK_SIGNAL_RE = re.compile(r"(?:第\s*\d+\s*頁|共\s*\d+\s*頁|收文章|發文字號|案號)")

# ═══════════════════════════════════════════════════════════════════════════════
# 日期偵測
# ═══════════════════════════════════════════════════════════════════════════════

# 民國年 — RRR年MM月DD日 / RRR.MM.DD / RRR/MM/DD
RE_ROC_DATE = re.compile(
    r"((?:1[01]\d|[89]\d))\s*[年\.\-/]\s*([01]?\d)\s*[月\.\-/]\s*([0-3]?\d)\s*[日]?"
)

# 西元年 — 202X/MM/DD or 202X年MM月DD日
RE_AD_DATE = re.compile(
    r"(20[12]\d)\s*[年\.\-/]\s*([01]?\d)\s*[月\.\-/]\s*([0-3]?\d)\s*[日]?"
)


def _extract_roc_date(text: str) -> Optional[str]:
    """Extract first ROC date, return 'RRR.MM.DD' or None."""
    m = RE_ROC_DATE.search(text)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        return f"{y}.{mo.zfill(2)}.{d.zfill(2)}"
    # Try AD date → convert to ROC
    m2 = RE_AD_DATE.search(text)
    if m2:
        y = int(m2.group(1)) - 1911
        if 80 <= y <= 200:
            mo, d = m2.group(2), m2.group(3)
            return f"{y}.{mo.zfill(2)}.{d.zfill(2)}"
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# 當事人偵測
# ═══════════════════════════════════════════════════════════════════════════════

_ROLE_PATTERNS = [
    re.compile(r"被\s*告\s+([^\s，,。；;（(]{2,5})"),
    re.compile(r"原\s*告\s+([^\s，,。；;（(]{2,5})"),
    re.compile(r"聲\s*請\s*人\s+([^\s，,。；;（(]{2,5})"),
    re.compile(r"相\s*對\s*人\s+([^\s，,。；;（(]{2,5})"),
    re.compile(r"證\s*人\s+([^\s，,。；;（(]{2,5})"),
    re.compile(r"(?:債務人|債權人)\s+([^\s，,。；;（(]{2,5})"),
]


def _extract_party(text: str, default: str = "") -> str:
    """Extract first party name from text header."""
    header = text[:1500]
    for pat in _ROLE_PATTERNS:
        m = pat.search(header)
        if m:
            name = m.group(1).strip()
            # Filter out obvious non-names
            if re.match(r"^[\u4e00-\u9fff]{2,4}$", name):
                return name
    return default


# ═══════════════════════════════════════════════════════════════════════════════
# OLA 系統頁偵測（只有浮水印，無實際內容）
# ═══════════════════════════════════════════════════════════════════════════════

RE_OLA_WATERMARK = re.compile(r"司法院線上閱卷系統作業平台")


def _is_ola_separator(text: str) -> bool:
    """Detect OLA system watermark-only pages (≤80 meaningful chars)."""
    clean = text.replace("\n", "").replace(" ", "").strip()
    if len(clean) < 80 and RE_OLA_WATERMARK.search(text):
        return True
    return False


def _meaningful_char_count(text: str) -> int:
    clean = re.sub(r"\s+", "", text or "")
    clean = re.sub(r"[^\w\u4e00-\u9fff]", "", clean)
    return len(clean)


def _compute_ola_threshold(counts: list[int]) -> int:
    valid = sorted(c for c in counts if c >= 0)
    if not valid:
        return 80
    idx = max(0, min(len(valid) - 1, int(len(valid) * 0.10)))
    return max(80, valid[idx])


# ═══════════════════════════════════════════════════════════════════════════════
# 文件類型辨識
# ═══════════════════════════════════════════════════════════════════════════════

def _is_prior_record_page(text: str) -> bool:
    """Detect if this page is part of a 前案紀錄表 (multi-page continuous doc)."""
    header = text[:2000]
    indicators = [
        "臺灣高等法院被告前案紀錄表",
        "前案紀錄表",
        "查詢條件：姓名",
        "列印條件：依",
        "印表單位：",
        "前案不含少年",
        "報表編號",       # continuation pages have this
        "HHD4D01",        # standard report ID
        "H1ID4D01",       # variant report ID
    ]
    count = sum(1 for ind in indicators if ind in header)
    return count >= 2


def _normalize_doc_type(raw_label: Optional[str], context_text: str = "") -> Optional[str]:
    """Normalize doc type aliases from regex / vision into canonical bookmark labels."""
    if not raw_label:
        return None

    label = re.sub(r"\s+", "", str(raw_label))
    context = re.sub(r"\s+", "", context_text or "")
    probe = f"{label}{context}"

    exact_alias = {
        "上訴抗告狀": "上訴/抗告狀",
        "調解筆錄": "調解/和解筆錄",
        "調解/和解方案": "調解/和解",
    }
    if label in exact_alias:
        return exact_alias[label]
    if label in KNOWN_DOC_LABELS:
        return label

    if "前案" in probe and ("紀錄" in probe or "前科" in probe):
        return "前案紀錄表"
    if any(k in probe for k in ("報到單", "報到")):
        return "報到單"
    if any(k in probe for k in ("收發文", "收文", "發文", "掛號", "回執")):
        return "收發文"
    if any(k in probe for k in ("照片", "相片", "截圖", "翻拍")):
        return "照片/截圖"

    if "起訴" in probe:
        if "追加起訴" in probe:
            return "追加起訴書"
        if "不起訴" in probe:
            return "不起訴處分書"
        if "緩起訴" in probe:
            return "緩起訴處分書"
        return "起訴書"

    if "裁定" in probe:
        return "裁定"
    if "判決" in probe:
        return "判決"

    if "筆錄" in probe:
        if "言詞辯論" in probe:
            return "言詞辯論筆錄"
        if "準備程序" in probe:
            return "準備程序筆錄"
        if "訊問" in probe or "讯问" in probe:
            return "訊問筆錄"
        if "調查" in probe or "调查" in probe:
            return "調查筆錄"
        if "勘驗" in probe or "勘验" in probe:
            return "勘驗筆錄"
        if "調解" in probe or "和解" in probe:
            return "調解/和解筆錄"
        if "搜索" in probe or "扣押" in probe:
            return "搜索扣押筆錄"
        return "審判筆錄"

    return None


def _classify_no_boundary_case(
    pdf_path: str,
    page_count: int,
    meaningful_counts: list[int],
    detected_doc_types: set[str],
    page_texts: list[str] | None = None,
) -> tuple[str, str]:
    """Classify no-boundary outcomes to avoid penalizing legitimate single docs."""
    stem = Path(pdf_path).stem
    max_meaningful = max(meaningful_counts) if meaningful_counts else 0
    filename_doc_type, _ = _detect_doc_type(stem, in_prior_record=False, allow_vision=False)
    audit = _audit_no_boundary_multidoc_signals(page_texts or [], detected_doc_types)

    if audit["has_multi_doc_signal"]:
        return "needs_manual_review", f"multi_doc_signal:{','.join(audit['evidence'][:3])}"

    if detected_doc_types and len(detected_doc_types) == 1 and page_count <= 20:
        only_type = sorted(detected_doc_types)[0]
        return "legitimate_single_doc", f"single_doc_detected_type:{only_type}"

    if filename_doc_type and page_count <= 20:
        return "legitimate_single_doc", f"filename_doc_type:{filename_doc_type}"

    if page_count <= 2 and max_meaningful < 45:
        return "legitimate_single_doc", "short_low_text_pdf"

    if page_count <= 12 and SINGLE_DOC_FILENAME_HINT_RE.search(stem):
        return "legitimate_single_doc", "filename_single_doc_hint"

    return "empty_failure", "no_boundary_without_single_doc_signal"


def _audit_no_boundary_multidoc_signals(
    page_texts: list[str],
    detected_doc_types: set[str],
) -> dict:
    """
    Audit no-boundary documents for multi-document boundary signals.

    Returns a dict with:
      - has_multi_doc_signal: bool
      - evidence: list[str]
      - distinct_doc_types: list[str]
      - transition_hits: int
    """
    evidence: list[str] = []
    distinct_doc_types = set(detected_doc_types or set())
    distinct_agencies = set()
    page_multi_type_hits = 0
    transition_hits = 0
    page_break_hits = 0
    prev_signature: tuple[tuple[str, ...], tuple[str, ...]] | None = None

    for raw in page_texts:
        text = str(raw or "")[:3000]
        if not text.strip():
            continue
        page_types = {name for name, pat in _STRONG_DOC_SIGNAL_PATTERNS if pat.search(text)}
        page_agencies = {name for name, pat in _AGENCY_SIGNAL_PATTERNS if pat.search(text)}
        if _PAGE_BREAK_SIGNAL_RE.search(text):
            page_break_hits += 1

        if len(page_types) >= 2:
            page_multi_type_hits += 1
        distinct_doc_types.update(page_types)
        distinct_agencies.update(page_agencies)

        signature = (tuple(sorted(page_types)), tuple(sorted(page_agencies)))
        if (signature[0] or signature[1]) and prev_signature and signature != prev_signature:
            transition_hits += 1
        if signature[0] or signature[1]:
            prev_signature = signature

    if len(distinct_doc_types) >= 2:
        evidence.append("distinct_doc_types>=2")
    if page_multi_type_hits >= 1:
        evidence.append("page_multi_type_hits>=1")
    if len(distinct_agencies) >= 2 and transition_hits >= 1:
        evidence.append("cross_agency_transition")
    if transition_hits >= 2 and len(distinct_doc_types) >= 1:
        evidence.append("signature_transition>=2")
    if page_break_hits >= 2 and len(distinct_doc_types) >= 2:
        evidence.append("page_break_with_multi_doc_type")

    return {
        "has_multi_doc_signal": bool(evidence),
        "evidence": evidence,
        "distinct_doc_types": sorted(distinct_doc_types),
        "distinct_agencies": sorted(distinct_agencies),
        "transition_hits": transition_hits,
        "page_break_hits": page_break_hits,
    }


def _detect_doc_type(
    text: str,
    in_prior_record: bool = False,
    allow_vision: bool = True,
) -> tuple[Optional[str], int]:
    """
    Detect document type from page text.
    Returns (label, level) or (None, 0).

    If in_prior_record=True, only returns 前案紀錄表-related types.
    Falls back to shared doc_type_detector Vision path when regex fails
    (MAGI_BOOKMARKER_VISION_FALLBACK=1 by default).
    """
    header = text[:2000]

    # Inside 前案紀錄表 section — don't match standalone doc types
    if in_prior_record:
        if _is_prior_record_page(header):
            return None, 0  # continuation of same 前案紀錄表, skip
        # If it's no longer a prior record page, we've exited — fall through

    for pattern, label, level in DOC_PATTERNS:
        if pattern.search(header):
            normalized = _normalize_doc_type(label, header)
            return normalized or label, level

    # Vision fallback (controlled by MAGI_BOOKMARKER_VISION_FALLBACK)
    import os as _os
    if allow_vision and _os.environ.get("MAGI_BOOKMARKER_VISION_FALLBACK", "1").strip() in ("1", "true", "yes"):
        try:
            from skills.engine.doc_type_detector import detect_doc_type as _dtd
            r = _dtd(header)
            if r.source == "vision" and r.confidence >= 0.60 and r.doc_type != "其他":
                normalized = _normalize_doc_type(r.doc_type, header)
                if normalized:
                    return normalized, 1
        except Exception:
            pass

    return None, 0


# ═══════════════════════════════════════════════════════════════════════════════
# OCR
# ═══════════════════════════════════════════════════════════════════════════════

def _ocr_page(page) -> str:
    """OCR a page using RapidOCR. Returns text or empty string."""
    if not ocr_engine:
        return ""
    try:
        pix = page.get_pixmap(dpi=200)
        img_bytes = pix.tobytes("png")
        result, _ = ocr_engine(img_bytes)
        if not result:
            return ""
        return "\n".join(line[1] for line in result)
    except Exception as e:
        logger.debug(f"OCR failed page {page.number}: {e}")
        return ""


# ═══════════════════════════════════════════════════════════════════════════════
# 核心：掃描 + 建立書籤
# ═══════════════════════════════════════════════════════════════════════════════

def scan_and_bookmark(
    pdf_path: str,
    output_path: Optional[str] = None,
    dry_run: bool = False,
    default_name: str = "",
    min_text_len: int = 30,
) -> dict:
    """
    Scan PDF and generate bookmarks.

    Returns dict with keys: success, bookmarks (count), toc (list), message.
    """
    if not os.path.exists(pdf_path):
        return {"success": False, "bookmarks": 0, "toc": [], "message": f"找不到檔案: {pdf_path}"}

    doc = fitz.open(pdf_path)

    # Try common passwords for encrypted court PDFs
    if doc.needs_pass:
        for pw in ["3800", "1234", ""]:
            try:
                if doc.authenticate(pw):
                    break
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 304, exc_info=True)

    existing_toc = doc.get_toc() or []
    toc: list[list] = []
    page_count = doc.page_count

    # Track state for dedup and section awareness
    last_label = ""
    last_doc_type = ""
    in_prior_record = False       # True while inside a 前案紀錄表 section
    consecutive_same_count = 0    # Count consecutive same-type (e.g., 送達證書)
    consecutive_same_type = ""
    consecutive_same_start_pg = 0
    detected_doc_types: set[str] = set()

    # Types that are individually less important — group consecutive ones
    _GROUPABLE_TYPES = {"送達證書", "傳票", "提票", "報到單", "收發文"}

    logger.info(f"掃描 {Path(pdf_path).name}（{page_count} 頁，現有書籤 {len(existing_toc)} 個）...")

    page_texts = []
    meaningful_counts = []
    for page_num in range(page_count):
        page = doc[page_num]
        text = page.get_text()
        if len(text.strip()) < min_text_len and HAS_OCR:
            text = _ocr_page(page)
        page_texts.append(text)
        meaningful_counts.append(_meaningful_char_count(text))

    ola_threshold = _compute_ola_threshold(meaningful_counts)
    stats_path = Path(__file__).resolve().parents[2] / ".runtime" / "bookmarker_ola_stats.jsonl"
    try:
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        with stats_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "pdf": str(pdf_path),
                "threshold": ola_threshold,
                "pages": page_count,
                "sample_counts": meaningful_counts[:20],
            }, ensure_ascii=False) + "\n")
        try:
            from api.events.sinks import rotate_jsonl
            rotate_jsonl(str(stats_path))
        except Exception:
            pass
    except Exception:
        logger.debug("Failed to write OLA stats", exc_info=True)

    def _flush_group():
        """Flush accumulated consecutive same-type bookmarks into one grouped entry."""
        nonlocal consecutive_same_count, consecutive_same_type, consecutive_same_start_pg
        if consecutive_same_count > 0 and consecutive_same_type:
            if consecutive_same_count == 1:
                suffix = ""
            else:
                suffix = f"（共 {consecutive_same_count} 份）"
            label = f"{consecutive_same_type}{suffix}"
            if callable(validate_bookmark):
                ok, warns = validate_bookmark(label)
                if not ok:
                    logger.warning("bookmark format guard: %s → %s", label, warns)
            toc.append([2, label, consecutive_same_start_pg])
            logger.info(f"  [P{consecutive_same_start_pg}] {label}")
        consecutive_same_count = 0
        consecutive_same_type = ""
        consecutive_same_start_pg = 0

    for page_num in range(page_count):
        page = doc[page_num]
        text = page_texts[page_num]

        # Skip OLA watermark-only separator pages
        if _is_ola_separator(text) and _meaningful_char_count(text) <= ola_threshold:
            continue

        # Skip nearly empty pages
        if len(text.strip()) < min_text_len:
            continue

        # ── Check if we're inside/entering 前案紀錄表 section ──
        is_prior = _is_prior_record_page(text)
        if is_prior and not in_prior_record:
            # Entering 前案紀錄表 — add one bookmark for the section start
            _flush_group()
            in_prior_record = True
            roc_date = _extract_roc_date(text)
            name = _extract_party(text, default_name)
            parts = []
            if roc_date:
                parts.append(roc_date)
            parts.append("前案紀錄表")
            if name:
                parts.append(name)
            label = " ".join(parts)
            if callable(validate_bookmark):
                ok, warns = validate_bookmark(label)
                if not ok:
                    logger.warning("bookmark format guard: %s → %s", label, warns)
            if label != last_label:
                logger.info(f"  [P{page_num+1}] {label}")
                toc.append([1, label, page_num + 1])
                last_label = label
                last_doc_type = "前案紀錄表"
            continue
        elif is_prior and in_prior_record:
            # Still inside 前案紀錄表 — skip
            continue
        elif not is_prior and in_prior_record:
            # Exited 前案紀錄表
            in_prior_record = False

        # ── Detect document type (not in prior record context) ──
        doc_type, level = _detect_doc_type(text, in_prior_record=False)

        # ── Detect date ──
        roc_date = _extract_roc_date(text)

        # ── Decide whether to add bookmark ──
        if not doc_type:
            # No doc_type detected — skip (date alone is too noisy)
            continue
        detected_doc_types.add(doc_type)

        # ── Handle groupable types (送達證書, 傳票, etc.) ──
        if doc_type in _GROUPABLE_TYPES:
            if doc_type == consecutive_same_type:
                consecutive_same_count += 1
                continue
            else:
                _flush_group()
                consecutive_same_type = doc_type
                consecutive_same_count = 1
                consecutive_same_start_pg = page_num + 1
                continue

        # Not a groupable type — flush any pending group first
        if consecutive_same_count > 0:
            _flush_group()

        # Build label
        parts = []
        if roc_date:
            parts.append(roc_date)
        parts.append(doc_type)

        # Try to add party name for key document types
        _NAME_WORTHY = {
            "訊問筆錄", "調查筆錄", "準備程序筆錄", "審判筆錄",
            "言詞辯論筆錄", "鑑定報告", "精神鑑定報告", "驗傷診斷書",
            "委任狀", "答辯狀", "上訴/抗告狀", "陳報狀", "聲請狀",
        }
        if doc_type in _NAME_WORTHY:
            name = _extract_party(text, default_name)
            if name:
                parts.append(name)

        label = " ".join(parts)
        if callable(validate_bookmark):
            ok, warns = validate_bookmark(label)
            if not ok:
                logger.warning("bookmark format guard: %s → %s", label, warns)

        # Dedup: skip if identical to last bookmark
        if label == last_label:
            continue

        logger.info(f"  [P{page_num+1}] {label}")
        toc.append([level, label, page_num + 1])
        last_label = label
        last_doc_type = doc_type

    # Flush any remaining group
    _flush_group()

    generated_toc = list(toc)

    # ── Write bookmarks ──
    if not toc:
        classification, reason = _classify_no_boundary_case(
            pdf_path=pdf_path,
            page_count=page_count,
            meaningful_counts=meaningful_counts,
            detected_doc_types=detected_doc_types,
            page_texts=page_texts,
        )
        doc.close()
        msg = f"未偵測到文件邊界，無法產生書籤（{Path(pdf_path).name}）"
        if classification == "legitimate_single_doc":
            msg = f"{msg}；判定為單一文件（{reason}）"
        logger.warning(msg)
        return {
            "success": False,
            "bookmarks": 0,
            "toc": [],
            "generated_toc": [],
            "classification": classification,
            "classification_reason": reason,
            "message": msg,
        }

    # Merge with existing TOC if any (keep existing, append new non-overlapping)
    if existing_toc:
        existing_pages = {entry[2] for entry in existing_toc}
        new_entries = [e for e in toc if e[2] not in existing_pages]
        merged = existing_toc + new_entries
        merged.sort(key=lambda x: x[2])
        toc = merged

    if not dry_run:
        doc.set_toc(toc)
        out = output_path or pdf_path
        if out == pdf_path:
            temp = pdf_path + ".tmp.pdf"
            doc.save(temp, garbage=4, deflate=True)
            doc.close()
            os.replace(temp, pdf_path)
        else:
            doc.save(out, garbage=4, deflate=True)
            doc.close()
        logger.info(f"完成：{len(toc)} 個書籤 → {Path(out).name}")
    else:
        doc.close()
        logger.info(f"Dry run：{len(toc)} 個書籤")

    return {
        "success": True,
        "bookmarks": len(toc),
        "toc": toc,
        "generated_toc": generated_toc,
        "classification": "bookmarkable",
        "classification_reason": "detected_boundary",
        "message": f"成功建立 {len(toc)} 個書籤",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# 批次處理
# ═══════════════════════════════════════════════════════════════════════════════

def batch_process(folder: str, recursive: bool = True, dry_run: bool = False) -> str:
    """Process all PDFs in a folder."""
    folder_path = Path(folder)
    if not folder_path.is_dir():
        return f"資料夾不存在: {folder}"

    pattern = "**/*.pdf" if recursive else "*.pdf"
    pdfs = sorted(folder_path.glob(pattern))
    pdfs = [p for p in pdfs if not p.name.startswith(".")]

    if not pdfs:
        return f"在 {folder} 中找不到 PDF 檔案"

    total = 0
    total_bookmarks = 0
    skipped = 0
    errors = []

    for pdf in pdfs:
        try:
            # Skip if already has reasonable bookmarks
            doc = fitz.open(str(pdf))
            existing = doc.get_toc() or []
            page_count = doc.page_count
            doc.close()

            if len(existing) >= max(3, page_count // 15):
                skipped += 1
                continue

            result = scan_and_bookmark(str(pdf), dry_run=dry_run)
            if result["success"]:
                total += 1
                total_bookmarks += result["bookmarks"]
            else:
                skipped += 1
        except Exception as e:
            errors.append(f"{pdf.name}: {e}")

    lines = [
        f"批次處理完成 — {folder_path.name}",
        f"  處理：{total} 份 / {total_bookmarks} 個書籤",
        f"  跳過：{skipped} 份",
    ]
    if errors:
        lines.append(f"  錯誤：{len(errors)} 筆")
        for e in errors[:5]:
            lines.append(f"    {e}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# 檢視書籤
# ═══════════════════════════════════════════════════════════════════════════════

def show_toc(pdf_path: str) -> str:
    """Display existing bookmarks in a PDF."""
    if not os.path.exists(pdf_path):
        return f"找不到檔案: {pdf_path}"

    doc = fitz.open(pdf_path)
    toc = doc.get_toc() or []
    page_count = doc.page_count
    doc.close()

    if not toc:
        return f"{Path(pdf_path).name}（{page_count} 頁）：無書籤"

    lines = [f"{Path(pdf_path).name}（{page_count} 頁，{len(toc)} 個書籤）："]
    for level, title, pg in toc:
        indent = "  " * (level - 1)
        lines.append(f"  {indent}P{pg:>4d}  {title}")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def task_self_test() -> dict:
    """驗證 pdf-bookmarker 的關鍵依賴與基本功能，無副作用。"""
    import tempfile
    errors = []
    warnings = []
    checks = {}

    # 1. PyMuPDF
    try:
        import fitz as _fitz  # noqa: F401
        checks["fitz"] = True
    except ImportError as e:
        errors.append("PyMuPDF (fitz) missing: " + str(e)[:80])
        checks["fitz"] = False

    # 2. RapidOCR
    checks["rapidocr"] = HAS_OCR
    if not HAS_OCR:
        warnings.append("rapidocr_onnxruntime not installed; OCR fallback disabled")

    # 3. bookmark_validator importable
    try:
        from skills.pdf_bookmarker.bookmark_validator import validate_bookmark as _vb  # noqa: F401
        checks["bookmark_validator"] = True
    except ImportError:
        _bv_path = Path(__file__).parent / "bookmark_validator.py"
        checks["bookmark_validator"] = _bv_path.exists()
        if not checks["bookmark_validator"]:
            errors.append("bookmark_validator.py missing from pdf-bookmarker skill directory")

    # 4. doc_type_detector importable
    try:
        from skills.engine.doc_type_detector import detect_doc_type as _dtd  # noqa: F401
        checks["doc_type_detector"] = True
    except ImportError:
        warnings.append("doc_type_detector not importable; Vision fallback may not work")
        checks["doc_type_detector"] = False

    # 5. Smoke: scan_and_bookmark on a synthetic PDF (dry_run)
    try:
        _tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        _tmp_path = _tmp.name
        _tmp.close()
        _doc = fitz.open()
        _page = _doc.new_page()
        _page.insert_text(
            (50, 100),
            "臺灣花蓮地方法院刑事判決\n"
            "中華民國115年1月1日\n"
            "被告王小明犯詐欺取財罪，處有期徒刑。",
            fontsize=12,
            fontname="china-t",
        )
        _doc.save(_tmp_path)
        _doc.close()
        result = scan_and_bookmark(_tmp_path, dry_run=True)
        os.unlink(_tmp_path)
        checks["scan_smoke"] = result.get("success", False)
        if not checks["scan_smoke"]:
            warnings.append("scan smoke returned success=False: " + result.get("message", "")[:80])
    except Exception as e:
        warnings.append("scan smoke exception: " + str(e)[:120])
        checks["scan_smoke"] = False

    ok = len(errors) == 0
    return {"success": ok, "checks": checks,
            "errors": errors if errors else None,
            "warnings": warnings if warnings else None}


def main():
    parser = argparse.ArgumentParser(description="MAGI PDF 自動書籤 v2.0")
    parser.add_argument("--task", required=True,
                        choices=["scan_file", "batch", "show", "test", "self_test"],
                        help="scan_file=單檔, batch=整個資料夾, show=顯示書籤, test=測試, self_test=健康檢查")
    parser.add_argument("--path", help="PDF 檔案或資料夾路徑")
    parser.add_argument("--output", help="輸出路徑（預設覆寫原檔）")
    parser.add_argument("--case-name", default="", help="當事人姓名（輔助辨識）")
    parser.add_argument("--dry-run", action="store_true", help="只顯示不寫入")
    parser.add_argument("--no-recursive", action="store_true", help="batch 時不遞迴")
    args = parser.parse_args()

    if args.task == "scan_file":
        if not args.path:
            print("ERROR: --path is required")
            return 1
        result = scan_and_bookmark(
            args.path,
            output_path=args.output,
            dry_run=args.dry_run,
            default_name=args.case_name,
        )
        print(result["message"])
        if args.dry_run and result["toc"]:
            for level, title, pg in result["toc"]:
                indent = "  " * (level - 1)
                print(f"  {indent}P{pg:>4d}  {title}")
        return 0 if result["success"] else 1

    elif args.task == "batch":
        if not args.path:
            print("ERROR: --path is required")
            return 1
        print(batch_process(args.path, recursive=not args.no_recursive, dry_run=args.dry_run))
        return 0

    elif args.task == "show":
        if not args.path:
            print("ERROR: --path is required")
            return 1
        print(show_toc(args.path))
        return 0

    elif args.task == "test":
        # Run on a sample file
        if args.path:
            result = scan_and_bookmark(args.path, dry_run=True)
            print(result["message"])
            if result["toc"]:
                for level, title, pg in result["toc"]:
                    indent = "  " * (level - 1)
                    print(f"  {indent}P{pg:>4d}  {title}")
            return 0 if result["success"] else 1
        else:
            print("ERROR: --path is required for test mode")
            return 1

    elif args.task == "self_test":
        result = task_self_test()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["success"] else 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
