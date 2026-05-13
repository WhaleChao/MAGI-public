# -*- coding: utf-8 -*-
"""Metric denominator tests for scripts/ops/benchmark_pdf_bookmarker.py."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "ops" / "benchmark_pdf_bookmarker.py"


def _load_module():
    name = "benchmark_pdf_bookmarker_for_test"
    spec = importlib.util.spec_from_file_location(name, str(SCRIPT_PATH))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_compute_recall_metrics_excludes_legitimate_single_doc_from_failures():
    mod = _load_module()
    outcomes = [
        {"toc_count": 0, "classification": "legitimate_single_doc"},
        {"toc_count": 0, "classification": "empty_failure"},
        {"toc_count": 3, "classification": "bookmarkable"},
    ]
    metrics = mod._compute_recall_metrics(outcomes)

    assert metrics["bookmarkable_total"] == 2
    assert metrics["legitimate_single_doc"] == 1
    assert metrics["empty_failures"] == 1
    assert metrics["bookmark_recall"] == 0.5
    assert metrics["empty_failure_rate"] == 0.5


def test_compute_recall_metrics_counts_needs_manual_review_as_unresolved():
    mod = _load_module()
    outcomes = [
        {"toc_count": 0, "classification": "needs_manual_review"},
        {"toc_count": 2, "classification": "bookmarkable"},
    ]
    metrics = mod._compute_recall_metrics(outcomes)

    assert metrics["bookmarkable_total"] == 2
    assert metrics["needs_manual_review"] == 1
    assert metrics["empty_failures"] == 0
    assert metrics["bookmark_recall"] == 0.5


def test_legacy_image_labels_are_included_in_cleanup_candidates():
    mod = _load_module()
    observed_toc = [[1, "image00001", 1], [1, "image00002", 2]]
    generated_toc = [[1, "收發文", 1]]
    candidate = mod._collect_legacy_cleanup_candidate(
        "/tmp/sample.pdf",
        observed_toc=observed_toc,
        generated_toc=generated_toc,
        classification="bookmarkable",
    )

    assert candidate is not None
    assert candidate["legacy_labels"] == ["image00001", "image00002"]
    assert candidate["recommended_action"] == "replace_with_generated_toc"
    assert candidate["proposed_generated_toc"][0]["label"] == "收發文"
