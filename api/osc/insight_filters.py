"""Public-release placeholders for removed legal-research filters."""

from __future__ import annotations


def is_non_extractable_legal_insight(*args, **kwargs) -> bool:
    return True


def non_extractable_legal_insight_sql_where() -> tuple[str, tuple]:
    return "1=1", ()


def displayable_insight_item(item) -> bool:
    return False
