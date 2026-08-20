"""Shared case lifecycle rules for every MAGI case category.

The lifecycle is intentionally independent from legal-aid workflow details.
All cases can be open, closing, or closed, and both closing and closed cases
belong under the closed-case storage roots.  Keeping this decision in one
module prevents OSC, NAS jobs, Drive sync, and native V3 from drifting.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping


class CaseLifecyclePhase(StrEnum):
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


_FINAL_EXACT = frozenset(
    {
        "已結案",
        "結案",
        "已報結",
        "報結完成",
        "已完成結案",
        "已轉入",
        "待轉入",
        "closed",
        "close",
        "done",
    }
)
_CLOSING_TOKENS = (
    "結案中",
    "待結案",
    "待報結",
    "待送出",
    "已結案，待報結",
    "已結案，待送出",
)
_OPEN_TOKENS = ("未結案", "不可報結", "結案誤判", "撤銷結案")


def phase_for_status(value: Any) -> CaseLifecyclePhase:
    text = str(value or "").strip()
    lower = text.lower()
    if not text or any(token in text for token in _OPEN_TOKENS):
        return CaseLifecyclePhase.OPEN
    if any(token in text for token in _CLOSING_TOKENS):
        return CaseLifecyclePhase.CLOSING
    if lower in _FINAL_EXACT or "已結案" in text or "已報結" in text or "報結完成" in text:
        return CaseLifecyclePhase.CLOSED
    return CaseLifecyclePhase.OPEN


def case_lifecycle_phase(row: Mapping[str, Any] | None) -> CaseLifecyclePhase:
    row = row or {}
    status_phase = phase_for_status(row.get("status"))
    legal_aid_phase = phase_for_status(row.get("legal_aid_status"))
    # An explicit legal-aid workflow state is more precise than the generic
    # case state.  Do not promote ``已結案，待送出`` to final merely
    # because an older integration already wrote generic ``已結案``.
    if legal_aid_phase is not CaseLifecyclePhase.OPEN:
        return legal_aid_phase
    return status_phase


def requires_closed_storage(row: Mapping[str, Any] | None) -> bool:
    """True once work has entered either a closing or final-closed state."""

    return case_lifecycle_phase(row) is not CaseLifecyclePhase.OPEN


def canonical_case_status(row: Mapping[str, Any] | None) -> str:
    phase = case_lifecycle_phase(row)
    if phase is CaseLifecyclePhase.CLOSED:
        return "已結案"
    if phase is CaseLifecyclePhase.CLOSING:
        return "結案中"
    return "進行中"


__all__ = [
    "CaseLifecyclePhase",
    "canonical_case_status",
    "case_lifecycle_phase",
    "phase_for_status",
    "requires_closed_storage",
]
