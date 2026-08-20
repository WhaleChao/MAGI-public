from __future__ import annotations

import json
import os

import pytest

from scripts.v3_validation.ime_candidate_probe import EVIDENCE_PREFIX, run_probe


@pytest.mark.skipif(
    os.environ.get("MAGI_V3_OFFLINE_CERTIFICATION") != "1",
    reason="native IME UI is reserved for the bounded offline campaign",
)
def test_real_candidate_window_survives_bounded_memory_pressure():
    evidence = run_probe(cycles=3, pressure_mb=256, timeout_sec=2.0)
    print(EVIDENCE_PREFIX + json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    assert evidence["status"] == "passed"
