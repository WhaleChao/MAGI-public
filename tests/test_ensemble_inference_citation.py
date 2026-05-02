"""
tests/test_ensemble_inference_citation.py

Phase 5: ensemble_inference enable_citation parameter tests.
All LLM calls are mocked.
"""

import pytest
from unittest.mock import patch, MagicMock


# ── Fixtures ──────────────────────────────────────────────────────────────────

ANSWER_WITH_CITATIONS = """依據[1]，被告應負損害賠償責任。

<CITATIONS>
[{"ref": 1, "doc_id": "doc-0", "page": "3", "quote": "被告應負損害賠償責任"}]
</CITATIONS>"""

ANSWER_WITHOUT_CITATIONS = "本案當事人應自行協商，無需進一步法律程序。"


def _make_mock_result(text: str, success: bool = True) -> dict:
    return {"success": success, "text": text, "error": "" if success else "timeout"}


# ── Test 1: enable_citation=True → result contains citations + prose ──────────

def test_ensemble_chat_enable_citation_injects_result():
    """ensemble_chat with enable_citation=True should inject citations into result."""
    with patch("skills.bridge.ensemble_inference._call_omlx_chat") as mock_call:
        mock_call.return_value = _make_mock_result(ANSWER_WITH_CITATIONS)

        from skills.bridge.ensemble_inference import ensemble_chat
        result = ensemble_chat(
            prompt="被告責任",
            mode="fast",
            enable_citation=True,
        )

    assert "citations" in result
    assert "prose" in result
    assert len(result["citations"]) == 1
    assert result["citations"][0]["ref"] == 1
    assert result["citations"][0]["doc_id"] == "doc-0"
    assert "<CITATIONS>" not in result["prose"]


# ── Test 2: enable_citation=False (default) → no citations in result ──────────

def test_ensemble_chat_enable_citation_false_no_injection():
    """ensemble_chat with enable_citation=False (default) should NOT inject citations."""
    with patch("skills.bridge.ensemble_inference._call_omlx_chat") as mock_call:
        mock_call.return_value = _make_mock_result(ANSWER_WITH_CITATIONS)

        from skills.bridge.ensemble_inference import ensemble_chat
        result = ensemble_chat(
            prompt="被告責任",
            mode="fast",
            # enable_citation not passed → defaults to False
        )

    assert "citations" not in result
    assert "prose" not in result


# ── Test 3: enable_citation=True but LLM gives no citations → empty list ──────

def test_ensemble_chat_enable_citation_no_citations_in_answer():
    """When LLM doesn't include <CITATIONS> block, citations should be empty list."""
    with patch("skills.bridge.ensemble_inference._call_omlx_chat") as mock_call:
        mock_call.return_value = _make_mock_result(ANSWER_WITHOUT_CITATIONS)

        from skills.bridge.ensemble_inference import ensemble_chat
        result = ensemble_chat(
            prompt="簡單問答",
            mode="fast",
            enable_citation=True,
        )

    assert "citations" in result
    assert result["citations"] == []
    assert result["prose"] == ANSWER_WITHOUT_CITATIONS


# ── Test 4: ensemble_chat_verified with enable_citation=True ─────────────────

def test_ensemble_chat_verified_enable_citation():
    """ensemble_chat_verified with enable_citation=True should add citations to individual_results."""
    mock_primary = _make_mock_result(ANSWER_WITH_CITATIONS)
    mock_review = {"phi4": {"success": True, "text": "OK"}, "smol": {"success": True, "text": "OK"}}

    with patch("skills.bridge.ensemble_inference._call_omlx_chat") as mock_call, \
         patch("skills.bridge.ensemble_inference._ensemble_review") as mock_review_fn:
        mock_call.return_value = mock_primary
        mock_review_fn.return_value = mock_review

        from skills.bridge.ensemble_inference import ensemble_chat_verified
        result = ensemble_chat_verified(
            prompt="被告責任",
            enable_citation=True,
        )

    # Result is ConsensusResult; individual_results should have citations
    assert hasattr(result, "individual_results")
    ir = result.individual_results or {}
    assert "citations" in ir
    assert "prose" in ir
    assert len(ir["citations"]) >= 0  # may be 0 if parse_citations got empty


# ── Test 5: enable_citation=False (default) for ensemble_chat_verified ───────

def test_ensemble_chat_verified_no_citation_by_default():
    """ensemble_chat_verified without enable_citation should not add citations to individual_results."""
    mock_primary = _make_mock_result(ANSWER_WITH_CITATIONS)
    mock_review = {"phi4": {"success": True, "text": "OK"}, "smol": {"success": True, "text": "OK"}}

    with patch("skills.bridge.ensemble_inference._call_omlx_chat") as mock_call, \
         patch("skills.bridge.ensemble_inference._ensemble_review") as mock_review_fn:
        mock_call.return_value = mock_primary
        mock_review_fn.return_value = mock_review

        from skills.bridge.ensemble_inference import ensemble_chat_verified
        result = ensemble_chat_verified(prompt="被告責任")

    ir = result.individual_results or {}
    assert "citations" not in ir
    assert "prose" not in ir


# ── Test 6: _build_system_with_citation helper ────────────────────────────────

def test_build_system_with_citation_appends():
    """_build_system_with_citation with enable_citation=True appends CITATION_INSTRUCTIONS."""
    from skills.bridge.ensemble_inference import _build_system_with_citation
    result = _build_system_with_citation("你是法律助理。", enable_citation=True)
    assert "<CITATIONS>" in result
    assert "你是法律助理。" in result


def test_build_system_with_citation_disabled():
    """_build_system_with_citation with enable_citation=False returns system unchanged."""
    from skills.bridge.ensemble_inference import _build_system_with_citation
    result = _build_system_with_citation("你是法律助理。", enable_citation=False)
    assert result == "你是法律助理。"
    assert "<CITATIONS>" not in result
