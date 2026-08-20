"""Narrow outbound-calendar policies shared by OSC and legacy sync paths."""

from __future__ import annotations

import re
from typing import Any, Mapping


OVERDUE_GOVERNANCE_MARKER = "【MAGI逾期治理：原待辦#"
LEGACY_OVERDUE_CONFIRMATION_TYPE = "逾期確認"
OSC_ONLY_MANUAL_REVIEW_TYPES = frozenset({"確認", "案號確認"})


def is_osc_only_overdue_confirmation(todo: Mapping[str, Any] | None) -> bool:
    """Return True only for MAGI overdue-governance review rows.

    These rows remain actionable in OSC, but are deliberately not calendar
    obligations.  The marker also covers newer rows whose ``todo_type`` was
    normalized to the original action (for example, ``抗告``).
    """

    row = todo or {}
    todo_type = str(row.get("todo_type") or "").strip()
    description = str(row.get("description") or "")
    return (
        todo_type == LEGACY_OVERDUE_CONFIRMATION_TYPE
        or OVERDUE_GOVERNANCE_MARKER in description
    )


def osc_only_overdue_confirmation_sql(alias: str = "ct") -> str:
    """SQL predicate matching exactly the OSC-only overdue-review class."""

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("unsafe SQL alias")
    marker = OVERDUE_GOVERNANCE_MARKER.replace("'", "''")
    legacy = LEGACY_OVERDUE_CONFIRMATION_TYPE.replace("'", "''")
    return (
        f"(COALESCE({alias}.todo_type,'')='{legacy}' "
        f"OR COALESCE({alias}.description,'') LIKE '%{marker}%')"
    )


def is_osc_only_calendar_review(todo: Mapping[str, Any] | None) -> bool:
    """Return True for human review rows that must remain in OSC only.

    A generic ``確認`` or ``案號確認`` is a review queue item, not a
    court occurrence or an actionable filing deadline.  Sending it to Google
    Calendar creates noisy all-day events and can incorrectly demand a hearing
    time merely because its source text mentions a hearing.  Real obligations
    (for example ``補正``/``上訴``) and real timed occurrences remain eligible.
    """

    row = todo or {}
    todo_type = str(row.get("todo_type") or "").strip()
    return (
        is_osc_only_overdue_confirmation(row)
        or todo_type in OSC_ONLY_MANUAL_REVIEW_TYPES
    )


def osc_only_calendar_review_sql(alias: str = "ct") -> str:
    """SQL predicate equivalent to :func:`is_osc_only_calendar_review`."""

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias):
        raise ValueError("unsafe SQL alias")
    manual_types = ",".join(
        f"'{value.replace(chr(39), chr(39) * 2)}'"
        for value in sorted(OSC_ONLY_MANUAL_REVIEW_TYPES)
    )
    return (
        f"({osc_only_overdue_confirmation_sql(alias)} "
        f"OR COALESCE({alias}.todo_type,'') IN ({manual_types}))"
    )
