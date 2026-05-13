"""
Helper utilities for T3 未結案件進度回報.

- PDF/DOCX selection: 存底 > 清稿 > v-number > date
- For 'doc' kind: files ending with「狀」are preferred; 回執/掛號收據 are excluded
- ROC date conversion
- Remark builder
"""
import re
import os
from typing import List, Optional, Tuple
from pathlib import Path


_COURT_SUBFOLDERS = [
    # numbered variants (standard LAF folder structure)
    "09_法院通知或程序裁定",
    "08_法院通知或程序裁定",
    "04_法院通知",
    "03_法院通知",
    # unnumbered fallbacks
    "法院通知或程序裁定",
    "法院通知",
    "程序裁定",
]
_DOC_SUBFOLDERS = [
    # numbered variants (standard LAF folder structure)
    "04_我方歷次書狀",
    "05_我方歷次書狀",
    "03_我方歷次書狀",
    # unnumbered fallbacks
    "我方歷次書狀",
    "書狀",
    "我方書狀",
]

# Priority scoring constants (higher = picked first)
_SCORE_BACKUP = 100
_SCORE_CLEAN = 50
_SCORE_ZHUAN = 30   # filename stem ends with「狀」— proper 書狀 document
_VNUM_RE = re.compile(r'[vV](\d+)')
_DATE_RE = re.compile(r'^(\d{4})(\d{2})(\d{2})')

# Keywords in 'doc' filenames that indicate a postal receipt — must be excluded
_DOC_EXCLUDE_KEYWORDS = (
    "回執",
    "收件回執",
    "郵件回執",
    "掛號收據",
    "掛號回執",
    "郵件收件",
)

# Valid doc file extensions (PDF + Word)
_DOC_EXTENSIONS = {".pdf", ".docx", ".doc"}


def _is_doc_excluded(name: str) -> bool:
    """Return True if this 'doc' candidate should be skipped (it's a receipt, not a 書狀)."""
    return any(kw in name for kw in _DOC_EXCLUDE_KEYWORDS)


def _stem_ends_with_zhuan(name: str) -> bool:
    """Return True if the stem ends with「狀」(e.g. 陳報狀, 聲請狀, 準備狀).

    Strips trailing parenthetical suffixes like （[當事人D]）or (client) before checking,
    so '20250623_更生陳報狀（[當事人D]）' is correctly recognised as a 書狀.
    """
    # Strip trailing （…）or (…) groups and whitespace
    cleaned = re.sub(r'[（(][^（(）)]*[）)]?\s*$', '', name).rstrip()
    return cleaned.endswith("狀")


def _score_pdf(path: Path, kind: str = "court") -> Tuple[int, int, str]:
    """Return (priority, vnum, datestr) for sorting — higher priority first.

    For 'doc' kind:
    - Files ending with「狀」get _SCORE_ZHUAN base priority
    - Receipt/掛號 files should be filtered before calling this
    """
    name = path.stem
    if "存底" in name:
        return (_SCORE_BACKUP, 0, name)
    if "清稿" in name:
        return (_SCORE_CLEAN, 0, name)
    vm = _VNUM_RE.search(name)
    if vm:
        return (10, int(vm.group(1)), name)
    dm = _DATE_RE.match(name)
    if kind == "doc" and _stem_ends_with_zhuan(name):
        # 書狀 files get a boosted base score; use date as tiebreaker (newer wins)
        date_str = dm.group(0) if dm else ""
        return (_SCORE_ZHUAN, 0, date_str)
    # Non-書狀 files with date prefix rank lower
    if dm:
        return (5, 0, dm.group(0))
    return (0, 0, name)


def pick_latest_pdf(case_folder: Path, kind: str) -> Optional[Path]:
    """
    kind: 'court' (法院通知/程序裁定) or 'doc' (我方歷次書狀)

    For 'doc':
    - Scans PDF, DOCX, DOC files
    - Excludes filenames containing 回執 / 掛號收據 / 郵件回執 etc.
    - Files whose stem ends with「狀」get priority boost

    Priority order: 存底 > 清稿 > 書狀結尾(newest) > largest v-number > newest date > others
    Returns Path or None.
    """
    subfolders = _COURT_SUBFOLDERS if kind == "court" else _DOC_SUBFOLDERS
    candidates: List[Path] = []

    case_folder = Path(case_folder)
    if not case_folder.is_dir():
        return None

    for sf in subfolders:
        target = case_folder / sf
        if not target.is_dir():
            continue
        for root, _dirs, files in os.walk(str(target)):
            depth = len(Path(root).relative_to(case_folder).parts)
            if depth > 4:
                break
            for fname in files:
                fpath = Path(root) / fname
                ext = fpath.suffix.lower()
                if kind == "court":
                    if ext != ".pdf":
                        continue
                else:  # doc
                    if ext not in _DOC_EXTENSIONS:
                        continue
                    if _is_doc_excluded(fpath.stem):
                        continue
                candidates.append(fpath)

    if not candidates:
        return None

    def _sort_key(p: Path) -> Tuple[int, int, str, int]:
        sc = _score_pdf(p, kind)
        # Prefer PDF over DOCX/DOC when scores are equal (portal handles PDF best)
        ext_rank = 1 if p.suffix.lower() == ".pdf" else 0
        return (sc[0], sc[1], sc[2], ext_rank)

    # Sort: higher score first, then larger vnum, then larger datestr (newer), then PDF > DOCX
    return sorted(candidates, key=_sort_key, reverse=True)[0]


def extract_date_from_pdf_name(path: Path) -> Optional[str]:
    """
    Filename prefix YYYYMMDD → ROC format: e.g. 20260415 → 115 年 4 月 15 日
    Returns None if no date found.
    """
    dm = _DATE_RE.match(Path(path).stem)
    if not dm:
        return None
    try:
        year = int(dm.group(1)) - 1911  # Gregorian to ROC
        month = int(dm.group(2))
        day = int(dm.group(3))
        return f"{year} 年 {month} 月 {day} 日"
    except (ValueError, OverflowError):
        return None


def build_progress_remark(
    court_pdf: Optional[Path],
    doc_pdf: Optional[Path],
) -> str:
    """
    Build remark string:
    - Both: '{ROC date} 收受最後一份裁定，{ROC date} 提出書狀'
    - Court only: '{ROC date} 收受最後一份裁定'
    - Doc only: '{ROC date} 提出書狀'
    - Neither: raises ValueError
    """
    if court_pdf is None and doc_pdf is None:
        raise ValueError("court_pdf and doc_pdf are both None; cannot build remark")

    parts = []
    if court_pdf is not None:
        date_str = extract_date_from_pdf_name(court_pdf) or "（日期不明）"
        parts.append(f"{date_str} 收受最後一份裁定")
    if doc_pdf is not None:
        date_str = extract_date_from_pdf_name(doc_pdf) or "（日期不明）"
        parts.append(f"{date_str} 提出書狀")
    return "，".join(parts)


# ── Progress email classification ──
_PRIORITY_TYPES = {
    "dispatch", "opening", "closing", "review", "inquiry",
    "fee", "withdrawal", "condition", "派案通知", "審核結果通知",
    "審查結果通知", "審查通知", "第1次通知", "派案",
}


def classify_progress_email(subject: str, snippet: str) -> bool:
    """
    Return True if subject+snippet contains both '案件' and '進度',
    AND the notification_type is not already a priority type.
    Caller is responsible for checking priority types.
    """
    combined = (subject or "") + " " + (snippet or "")
    return ("案件" in combined) and ("進度" in combined)
