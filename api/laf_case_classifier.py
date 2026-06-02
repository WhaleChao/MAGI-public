# -*- coding: utf-8 -*-
"""Shared LAF case classification helpers.

The LAF portal sometimes labels public-law social insurance matters with a
generic civil procedure label.  OSC folder roots and reporting logic need the
substantive case type, so this module keeps those overrides in one place.
"""

from __future__ import annotations

import re

from typing import Dict, Tuple


ADMINISTRATIVE_REASON_KEYWORDS = (
    "勞工保險爭議",
    "勞工保險",
    "勞保",
    "就業保險",
    "職業災害保險",
    "職災保險",
    "勞工退休金",
    "國民年金",
    "全民健康保險",
    "健保",
    "勞保局",
    "勞動部勞工保險局",
    "保險給付爭議",
    "行政處分",
    "行政訴訟",
    "訴願",
)


def is_administrative_laf_reason(reason: str, laf_case_type: str = "") -> bool:
    """Return True when LAF matter text should be filed as 行政."""
    text = f"{laf_case_type or ''} {reason or ''}".strip()
    if not text:
        return False
    return any(keyword in text for keyword in ADMINISTRATIVE_REASON_KEYWORDS)


def normalize_laf_case_type(
    case_type: str,
    case_stage: str = "",
    case_reason: str = "",
    laf_case_type: str = "",
) -> Tuple[str, str]:
    """Apply substantive LAF case-type overrides.

    This intentionally does not rewrite ``case_reason``.  Callers that have
    special reason normalization, such as consumer debt defaulting to 更生,
    should continue to do that locally.
    """
    current_type = (case_type or "").strip()
    current_stage = (case_stage or "").strip()

    if is_administrative_laf_reason(case_reason, laf_case_type):
        return "行政", current_stage or "一審"

    return current_type, current_stage


_PENDING_REASONS = {"", "待確認", "未確認"}


def clean_laf_case_reason(value: str) -> str:
    """Return a compact case-reason string parsed from noisy LAF email text."""
    text = re.sub(r"\s+", "", str(value or "").strip())
    text = text.strip("：:，,。；;、（）()[]【】")
    text = re.sub(r"(之)?案件資料$", "", text)
    text = re.sub(r"(之)?資料$", "", text)
    text = text.strip("：:，,。；;、（）()[]【】")
    return text


def is_pending_laf_reason(value: str) -> bool:
    return clean_laf_case_reason(value) in _PENDING_REASONS


def normalize_laf_case_fields(
    case_type: str,
    case_stage: str = "",
    case_reason: str = "",
    laf_case_type: str = "",
) -> Tuple[str, str, str]:
    """Normalize the three OSC-facing LAF fields in one place."""
    current_type = (case_type or "").strip()
    current_stage = (case_stage or "").strip()
    current_reason = clean_laf_case_reason(case_reason)

    if any(token in current_reason for token in ("消費者債務清理", "更生", "清算")):
        return "消費者債務清理", "其他", "清算" if "清算" in current_reason else "更生"

    normalized_type, normalized_stage = normalize_laf_case_type(
        current_type,
        current_stage,
        current_reason,
        laf_case_type,
    )
    return normalized_type, normalized_stage, current_reason


def extract_laf_staff_case_hint(
    text: str,
    *,
    laf_case_number: str = "",
    client_name: str = "",
) -> Dict[str, str]:
    """Extract case type/reason hints from generic LAF staff emails."""
    body = re.sub(r"\s+", " ", str(text or "")).strip()
    if not body:
        return {}

    laf_no_pattern = re.escape(laf_case_number) if laf_case_number else r"\d{6,8}-[A-Za-z]-\d{3}"
    patterns = [
        rf"(?:檢陳|檢送|寄送)\s*(?P<laf>{laf_no_pattern})\s*(?P<trailing>[^。\n\r]{{0,120}}?)(?:之)?\s*案件資料",
        rf"(?P<laf>{laf_no_pattern})\s*(?P<trailing>[^。\n\r]{{0,120}}?)(?:之)?\s*案件資料",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, body):
            trailing = str(match.group("trailing") or "")
            candidate = _strip_known_case_tokens(trailing, client_name=client_name)
            if candidate and candidate not in {"案件資料", "資料", "案件"}:
                case_type, case_stage, case_reason = normalize_laf_case_fields("", "", candidate, "")
                return {
                    "case_type": case_type,
                    "case_stage": case_stage,
                    "case_reason": case_reason,
                    "raw_reason": candidate,
                }

    return {}


def _strip_known_case_tokens(value: str, *, client_name: str = "") -> str:
    text = clean_laf_case_reason(value)
    if client_name:
        text = re.sub(re.escape(client_name), "", text)
    text = re.sub(r"^[、，,\-－:：]+", "", text)
    text = re.sub(r"^(先生|小姐|女士|君)", "", text)
    text = clean_laf_case_reason(text)
    return text
