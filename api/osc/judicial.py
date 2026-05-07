"""Public-release placeholders for removed legal-research data sources."""

from __future__ import annotations


def _osc_collect_insights():
    return []


def _osc_fetch_fulltext_from_judicial(*args, **kwargs):
    return {"ok": False, "error": "public_release_feature_removed"}


def _osc_summarize_legal_insight(*args, **kwargs):
    return ""


def _osc_doc_kind_match(*args, **kwargs):
    return False


def _osc_doc_kind_label(*args, **kwargs):
    return ""
