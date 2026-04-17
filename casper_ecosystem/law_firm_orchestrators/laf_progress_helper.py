"""
Helper utilities for T3 未結案件進度回報.

- PDF selection: 存底 > 清稿 > v-number > date
- ROC date conversion
- Remark builder
"""
import re
import os
from typing import List, Optional, Tuple
from pathlib import Path


_COURT_SUBFOLDERS = [
    "法院通知", "程序裁定", "03_法院通知", "04_法院通知", "法院通知或程序裁定",
]
_DOC_SUBFOLDERS = [
    "我方歷次書狀", "書狀", "03_我方歷次書狀", "04_我方歷次書狀", "我方書狀",
]

# Priority scoring constants (higher = picked first)
_SCORE_BACKUP = 100
_SCORE_CLEAN = 50
_VNUM_RE = re.compile(r'[vV](\d+)')
_DATE_RE = re.compile(r'^(\d{4})(\d{2})(\d{2})')


def _score_pdf(path: Path) -> Tuple[int, int, str]:
    """Return (priority, vnum, datestr) for sorting — higher priority first."""
    name = path.stem
    if "存底" in name:
        return (_SCORE_BACKUP, 0, name)
    if "清稿" in name:
        return (_SCORE_CLEAN, 0, name)
    vm = _VNUM_RE.search(name)
    if vm:
        return (10, int(vm.group(1)), name)
    dm = _DATE_RE.match(name)
    if dm:
        return (5, 0, dm.group(0))
    return (0, 0, name)


def pick_latest_pdf(case_folder: Path, kind: str) -> Optional[Path]:
    """
    kind: 'court' (法院通知/程序裁定) or 'doc' (我方歷次書狀)
    Priority: 存底 > 清稿 > largest v-number > newest date
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
                if fname.lower().endswith(".pdf"):
                    candidates.append(Path(root) / fname)

    if not candidates:
        return None

    # Sort: higher score first, then larger vnum, then larger datestr (newer)
    return sorted(
        candidates,
        key=lambda p: (_score_pdf(p)[0], _score_pdf(p)[1], _score_pdf(p)[2]),
        reverse=True,
    )[0]


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
