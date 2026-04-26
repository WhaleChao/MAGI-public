# -*- coding: utf-8 -*-
"""Threshold logic tests for scripts/ops/benchmark_pdf_namer.py."""

import importlib.util
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "ops" / "benchmark_pdf_namer.py"


def _load_module():
    name = "benchmark_pdf_namer_for_test"
    spec = importlib.util.spec_from_file_location(name, str(SCRIPT_PATH))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_thresholds_fail_when_quality_is_bad_even_if_format_is_good():
    mod = _load_module()
    failed = mod._collect_threshold_failures(
        format_valid_rate=1.0,
        quality_pass_rate=0.8,
        overall_pass_rate=0.8,
        empty_rate=0.0,
    )
    assert any("quality_pass_rate" in item for item in failed)


def test_thresholds_pass_when_all_metrics_are_good():
    mod = _load_module()
    failed = mod._collect_threshold_failures(
        format_valid_rate=1.0,
        quality_pass_rate=1.0,
        overall_pass_rate=1.0,
        empty_rate=0.0,
    )
    assert failed == []
