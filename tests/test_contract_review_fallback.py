"""Regression tests for contract-review deterministic fallbacks."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_MODULE_PATH = Path(__file__).resolve().parents[1] / "skills" / "contract-review" / "action.py"


def _load_contract_review():
    spec = spec_from_file_location("contract_review_action", _MODULE_PATH)
    module = module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_summarize_fallback_returns_structured_payload():
    module = _load_contract_review()
    module._llm_json = lambda prompt, fallback: dict(fallback)

    result = module.summarize("本合約約定乙方應保密，並於2026年4月3日提供顧問服務。違約金為新台幣十萬元。")

    assert result["task"] == "summarize"
    assert result["fallback_used"] is True
    assert result["fallback_reason"] == "llm_unavailable"
    assert result["doc_type"]
    assert isinstance(result["risk_points"], list)
    assert "error" not in result


def test_review_fallback_returns_recommendations():
    module = _load_contract_review()
    module._llm_json = lambda prompt, fallback: dict(fallback)

    result = module.review("甲方得單方終止本合約，乙方應保密並負全部損害賠償責任。")

    assert result["task"] == "review"
    assert result["fallback_used"] is True
    assert result["recommendations"]
    assert isinstance(result["flagged_clauses"], list)
    assert "error" not in result


def test_vendor_check_fallback_is_non_fatal():
    module = _load_contract_review()
    module._llm_json = lambda prompt, fallback: dict(fallback)

    result = module.vendor_check("供應商應於驗收後請款，契約未載明保固與智財歸屬。")

    assert result["task"] == "vendor_check"
    assert result["fallback_used"] is True
    assert isinstance(result["missing_clauses"], list)
    assert "error" not in result
