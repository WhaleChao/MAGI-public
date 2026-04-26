# -*- coding: utf-8 -*-
"""
pdf-namer / naming_validator.py
================================
Format guard for generated PDF filenames.
Called by generate_name_proposal() before returning; adds warnings but does NOT block.
"""
import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

# Document types that require bracket supplemental info
_TYPES_REQUIRING_BRACKETS = frozenset([
    "判決", "裁定", "函文", "函", "庭通知書", "起訴書",
    "不起訴處分書", "聲請書", "再議聲請書",
])

_DATE_RE = re.compile(r"^\d{8}")
_BRACKET_RE = re.compile(r"[（(].+[）)]")
_PARTY_RE = re.compile(r"[（(]([^（）()]+)[）)]")

_UNKNOWN_TOKENS = (
    "找不到",
    "未知",
    "不明",
    "無法辨識",
    "無法識別",
    "n/a",
    "N/A",
)
_UNKNOWN_REPEAT_RE = re.compile(
    r"(?P<t>" + "|".join(re.escape(t) for t in _UNKNOWN_TOKENS) + r")(?:\s*(?P=t))+",
    re.IGNORECASE,
)
_NAME_BLACKLIST_TOKENS = frozenset(
    (
        "法院",
        "地方法院",
        "高等法院",
        "分院",
        "檢察署",
        "判決",
        "裁定",
        "起訴書",
        "書狀",
        "收文",
        "主文",
        "年度",
        "民國",
        "通知書",
        "函文",
        "刑事",
        "民事",
        "行政",
    )
)
_PARTY_NOISE_MARKERS = (
    "民國",
    "年度",
    "字第",
    "號",
    "法院",
    "分院",
    "檢察署",
    "判決",
    "裁定",
    "起訴書",
    "書狀",
    "收文",
    "主文",
    "年",
    "月",
    "日",
)
_SOURCE_NAME_RE = re.compile(r"(?<![\u4e00-\u9fff])([\u4e00-\u9fff]{2,6})(?![\u4e00-\u9fff])")


@lru_cache(maxsize=1)
def _opencc_s2t():
    try:
        import opencc  # type: ignore

        return opencc.OpenCC("s2t")
    except Exception:
        return None


def _strip_ext(name: str) -> str:
    return name[:-4] if str(name or "").lower().endswith(".pdf") else str(name or "")


def _normalize_name(name: str) -> str:
    s = str(name or "").strip()
    return re.sub(r"\s+", "", s)


def _to_traditional(text: str) -> str:
    conv = _opencc_s2t()
    if not conv:
        return str(text or "")
    try:
        return conv.convert(str(text or ""))
    except Exception:
        return str(text or "")


def _extract_party_segment_with_span(stem: str) -> Tuple[str, Optional[Tuple[int, int]]]:
    m = _PARTY_RE.search(stem or "")
    if not m:
        return "", None
    raw = m.group(1).strip()
    if not raw:
        return "", None
    party = raw.split("；", 1)[0].split(";", 1)[0].strip()
    rel = raw.find(party) if party else -1
    if rel < 0:
        return party, None
    return party, (m.start(1) + rel, m.start(1) + rel + len(party))


def extract_source_name_candidates(source_hint: str) -> List[str]:
    """Extract likely Chinese person names from source file/path hints."""
    text = str(source_hint or "")
    if not text:
        return []
    candidates = []
    seen = set()
    for match in _SOURCE_NAME_RE.finditer(text):
        token = match.group(1).strip()
        if len(token) < 2 or len(token) > 5:
            continue
        if token in _NAME_BLACKLIST_TOKENS:
            continue
        if any(t in token for t in _PARTY_NOISE_MARKERS):
            continue
        if token not in seen:
            seen.add(token)
            candidates.append(token)
    return candidates


def _detect_repeated_unknown_tokens(stem: str) -> List[str]:
    issues = []
    for m in _UNKNOWN_REPEAT_RE.finditer(stem or ""):
        token = m.group("t") or ""
        if token:
            issues.append(token)
    return issues


def _party_noise_markers(party: str) -> List[str]:
    found = [marker for marker in _PARTY_NOISE_MARKERS if marker and marker in (party or "")]
    if len(party or "") >= 3 and str(party).endswith(("女", "男")):
        found.append("trailing_gender_marker")
    return sorted(set(found))


def _is_variant_drift(proposed_party: str, source_candidates: List[str]) -> Optional[str]:
    """Detect drift like 余秋菊 -> 餘秋菊 while traditional-normalized strings are equal."""
    p_norm = _normalize_name(proposed_party)
    if not p_norm:
        return None
    exact_set = {_normalize_name(c) for c in source_candidates}
    if p_norm in exact_set:
        return None
    p_t = _normalize_name(_to_traditional(proposed_party))
    for src in source_candidates:
        src_norm = _normalize_name(src)
        if not src_norm:
            continue
        if src_norm == p_norm:
            return None
        if _normalize_name(_to_traditional(src)) == p_t:
            return src
    return None


def _clean_party_noise(party: str) -> str:
    value = str(party or "").strip()
    if not value:
        return value
    first_hit = None
    for marker in _PARTY_NOISE_MARKERS:
        idx = value.find(marker)
        if idx > 0:
            if first_hit is None or idx < first_hit:
                first_hit = idx
    if first_hit is not None:
        value = value[:first_hit].strip()
    if len(value) >= 3 and value.endswith(("女", "男")):
        value = value[:-1].strip()
    return value


def sanitize_filename(name: str, source_hint: str = "") -> Tuple[str, List[str]]:
    """Apply conservative sanitization for obvious OCR pollution."""
    current = str(name or "")
    fixes = []
    if not current:
        return current, fixes

    # 1) Collapse repeated unknown tokens (e.g., 找不到找不到 -> 找不到)
    collapsed = _UNKNOWN_REPEAT_RE.sub(lambda m: m.group("t"), current)
    if collapsed != current:
        fixes.append("collapse_repeated_unknown_token")
        current = collapsed

    # 2) Remove legal-context noise accidentally appended to party field
    stem = _strip_ext(current)
    party, span = _extract_party_segment_with_span(stem)
    if party and span:
        cleaned = _clean_party_noise(party)
        if cleaned and cleaned != party and len(cleaned) >= 2:
            stem = stem[: span[0]] + cleaned + stem[span[1] :]
            current = stem + (".pdf" if str(name).lower().endswith(".pdf") else "")
            fixes.append("trim_party_noise")

    # 3) If we know source names, prefer exact source spelling over converted variant
    stem = _strip_ext(current)
    party, span = _extract_party_segment_with_span(stem)
    source_candidates = extract_source_name_candidates(source_hint)
    if party and span and source_candidates:
        drift_from = _is_variant_drift(party, source_candidates)
        if drift_from:
            stem = stem[: span[0]] + drift_from + stem[span[1] :]
            current = stem + (".pdf" if str(name).lower().endswith(".pdf") else "")
            fixes.append("restore_source_name_variant")

    return current, fixes


def validate_filename_quality(name: str, source_hint: str = "") -> Tuple[bool, List[str], Dict[str, List[str]]]:
    """Semantic quality checks beyond format-only validation."""
    issues: List[str] = []
    details: Dict[str, List[str]] = {}
    stem = _strip_ext(name)

    repeated_unknown = _detect_repeated_unknown_tokens(stem)
    if repeated_unknown:
        issues.append("檔名包含重複未知詞（如 找不到找不到 / 未知未知）")
        details["repeated_unknown_tokens"] = repeated_unknown

    party, _ = _extract_party_segment_with_span(stem)
    if party:
        noise = _party_noise_markers(party)
        if noise:
            issues.append("姓名欄疑似混入法律上下文殘片（OCR 汙染）")
            details["party_noise_markers"] = noise

        source_candidates = extract_source_name_candidates(source_hint)
        if source_candidates:
            drift_from = _is_variant_drift(party, source_candidates)
            if drift_from:
                issues.append("姓名字形與來源不一致（疑似任意繁簡/異體轉換）")
                details["name_variant_drift"] = [drift_from, party]

    return len(issues) == 0, issues, details


def validate_filename(name: str) -> Tuple[bool, List[str]]:
    """Validate a proposed PDF filename against naming rules.

    Rules:
      1. Must not be empty
      2. Must start with 8-digit YYYYMMDD
      3. Character after date must be a single space (not underscore/dash)
      4. Extension must be .pdf (case-insensitive)
      5. Judgment/ruling/letter types must have bracket supplemental info

    Returns:
        (is_valid, warnings) — warnings is empty when is_valid is True.
        Non-blocking: caller logs warnings but still returns the filename.
    """
    warnings: List[str] = []

    if not name or not name.strip():
        return False, ["檔名不得為空字串"]

    stem = name
    if name.lower().endswith(".pdf"):
        stem = name[:-4]
    else:
        warnings.append("副檔名不是 .pdf")

    if not _DATE_RE.match(stem):
        warnings.append("檔名未以 8 位西元日期 (YYYYMMDD) 開頭")
    else:
        date_part = stem[:8]
        try:
            y, m, d = int(date_part[:4]), int(date_part[4:6]), int(date_part[6:8])
            if not (2000 <= y <= 2099 and 1 <= m <= 12 and 1 <= d <= 31):
                warnings.append(f"日期 {date_part} 超出合法範圍")
        except ValueError:
            warnings.append(f"日期 {date_part} 無法解析")

        if len(stem) > 8 and stem[8] != " ":
            sep = repr(stem[8])
            warnings.append(f"日期後分隔符應為空格，實際為 {sep}")

    # Check for required bracket info in judgment/ruling/letter types
    for doc_type in _TYPES_REQUIRING_BRACKETS:
        if doc_type in stem:
            if not _BRACKET_RE.search(stem):
                warnings.append(
                    f"文件類型「{doc_type}」應包含括號補充資訊，例如（當事人；主文摘要）"
                )
            break

    is_valid = len(warnings) == 0
    return is_valid, warnings
