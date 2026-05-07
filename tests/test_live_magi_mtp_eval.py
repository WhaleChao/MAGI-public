from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


def _load_eval_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "live_magi_mtp_eval.py"
    spec = importlib.util.spec_from_file_location("live_magi_mtp_eval", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_unsafe_claim_detector_ignores_negated_claims():
    mod = _load_eval_module()

    assert mod._has_unsafe_claim("我無法判斷一定會勝訴。") is False
    assert mod._has_unsafe_claim("沒有足夠證據，不能斷定一定有罪。") is False
    assert mod._has_unsafe_claim("本案一定會勝訴。") is True


def test_eval_json_extractor_accepts_plain_object():
    mod = _load_eval_module()

    parsed = mod._extract_json('{"action":"request_input","params":{"missing":"document_text"}}')

    assert parsed["action"] == "request_input"
    assert parsed["params"]["missing"] == "document_text"


def test_instrumented_tools_cover_full_registry():
    mod = _load_eval_module()
    from skills.engine.tool_registry import TOOLS

    instrumented = mod._instrumented_tools()

    assert set(instrumented) == set(TOOLS)
    assert instrumented["get_schedule"]["fn"](date="2026-05-07").startswith("TOOL_CALLED:get_schedule:")
