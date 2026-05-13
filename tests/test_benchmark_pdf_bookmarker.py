# -*- coding: utf-8 -*-
"""Live benchmark bookkeeping tests for scripts/ops/benchmark_pdf_bookmarker.py."""

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


def test_common_laf_forms_do_not_count_as_empty_bookmark_failures():
    mod = _load_module()
    metrics = mod._compute_recall_metrics(
        [
            {
                "pdf": "/case/01_法扶資料/資力詢問表_1150225-E-007.pdf",
                "toc_count": 0,
                "classification": "empty_failure",
            },
            {
                "pdf": "/case/01_法扶資料/1150225-E-007 張裕和 2A.pdf",
                "toc_count": 0,
                "classification": "empty_failure",
            },
        ]
    )

    assert metrics["bookmarkable_total"] == 0
    assert metrics["legitimate_single_doc"] == 2
    assert metrics["empty_failures"] == 0
