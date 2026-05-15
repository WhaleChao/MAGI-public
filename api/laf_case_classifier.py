# -*- coding: utf-8 -*-
"""Shared LAF case classification helpers.

The LAF portal sometimes labels public-law social insurance matters with a
generic civil procedure label.  OSC folder roots and reporting logic need the
substantive case type, so this module keeps those overrides in one place.
"""

from __future__ import annotations

from typing import Tuple


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
