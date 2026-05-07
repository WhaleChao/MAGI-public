"""Public-release placeholder for removed legal-research integrations."""

from __future__ import annotations


_PUBLIC_DISABLED = (
    "This public MAGI release does not include legal-research collection, "
    "case-law lookup, or opinion-library integrations."
)


def extract_judgment_collect_payload(message: str) -> tuple[dict | None, str]:
    return None, _PUBLIC_DISABLED


def format_judgment_collect_result(payload: dict) -> str:
    return _PUBLIC_DISABLED


def run_judgment_collector_command(orch, message: str, notify: bool = False) -> str:
    return _PUBLIC_DISABLED


def run_judgment_trend_command(orch, message: str) -> str:
    return _PUBLIC_DISABLED
