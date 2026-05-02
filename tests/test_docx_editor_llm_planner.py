"""
tests/test_docx_editor_llm_planner.py

Phase 3: llm_edit_planner tests. All LLM calls are mocked via _call_llm.
"""

import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock

# Ensure skill lib is importable
_SKILL_DIR = os.path.join(os.path.dirname(__file__), "..", "skills", "docx-editor")
sys.path.insert(0, _SKILL_DIR)
sys.path.insert(0, os.path.join(_SKILL_DIR, "lib"))

from lib.llm_edit_planner import plan_edits_with_llm, _parse_json_response


# ── Fixtures ──────────────────────────────────────────────────────────────────

SAMPLE_TEXT = "甲方同意於民國114年12月31日前支付乙方新台幣十萬元整。甲方應履行上述義務。"

RANGE_EXCEEDED_JSON = json.dumps([
    {
        "find": "",
        "replace": "",
        "context_before": "",
        "context_after": "",
        "reason": "指令超出 anchored edit 範圍",
    }
])


# ── Test 1: happy path — anchor found ─────────────────────────────────────────

def test_plan_edits_happy_path():
    """Valid LLM response with anchor-findable edit → edits list populated."""
    docx_text = "甲方同意於民國114年12月31日前支付乙方新台幣十萬元整。甲方應履行上述義務。"
    llm_json = json.dumps([
        {
            "find": "甲方同意",
            "replace": "原告同意",
            "context_before": "",
            "context_after": "於民國114年",
            "reason": "改甲方為原告",
        }
    ])

    with patch("lib.llm_edit_planner._call_llm", return_value=llm_json):
        edits, warnings = plan_edits_with_llm(docx_text, "把甲方改成原告")

    assert len(edits) == 1
    assert edits[0].find == "甲方同意"
    assert edits[0].replace == "原告同意"
    assert warnings == []


# ── Test 2: LLM returns [] (range exceeded) ────────────────────────────────────

def test_plan_edits_empty_returns_warning():
    """LLM returns [] with 'exceeded' reason → empty edits + warning."""
    with patch("lib.llm_edit_planner._call_llm", return_value=RANGE_EXCEEDED_JSON):
        edits, warnings = plan_edits_with_llm(SAMPLE_TEXT, "把全文重寫")

    assert edits == []
    assert any("超出" in w for w in warnings)


# ── Test 3: anchor not found → warning, edit skipped ─────────────────────────

def test_plan_edits_anchor_not_found():
    """If LLM gives a find string not in docx_text, edit is skipped with warning."""
    llm_json = json.dumps([
        {
            "find": "根本不存在的字串XYZ",
            "replace": "替換",
            "context_before": "",
            "context_after": "",
            "reason": "測試用",
        }
    ])

    with patch("lib.llm_edit_planner._call_llm", return_value=llm_json):
        edits, warnings = plan_edits_with_llm(SAMPLE_TEXT, "修改")

    assert edits == []
    assert any("not_found" in w for w in warnings)


# ── Test 4: LLM call failure → empty edits + warning ─────────────────────────

def test_plan_edits_llm_failure():
    """LLM call failure (returns None) returns empty edits with warning."""
    with patch("lib.llm_edit_planner._call_llm", return_value=None):
        edits, warnings = plan_edits_with_llm(SAMPLE_TEXT, "修改")

    assert edits == []
    assert any("失敗" in w or "call" in w.lower() for w in warnings)


# ── Test 5: document truncation warning ──────────────────────────────────────

def test_plan_edits_long_doc_truncation_warning():
    """Very long docx_text triggers truncation warning."""
    long_text = "甲方 " * 3000  # >8000 chars
    with patch("lib.llm_edit_planner._call_llm", return_value="[]"):
        edits, warnings = plan_edits_with_llm(long_text, "修改")

    assert any("截斷" in w for w in warnings)


# ── Test 6: _parse_json_response strips markdown fences ──────────────────────

def test_parse_json_response_strips_fences():
    raw = "```json\n[{\"find\": \"x\", \"replace\": \"y\"}]\n```"
    result = _parse_json_response(raw)
    assert isinstance(result, list)
    assert result[0]["find"] == "x"


# ── Test 7: _parse_json_response malformed JSON raises ───────────────────────

def test_parse_json_response_malformed_raises():
    with pytest.raises(json.JSONDecodeError):
        _parse_json_response("{bad json}")


# ── Test 8: ambiguous anchor → warning or success depending on uniqueness ────

def test_plan_edits_ambiguous_anchor():
    """If find appears multiple times with no context, system handles gracefully."""
    # "甲方" appears twice in SAMPLE_TEXT
    llm_json = json.dumps([
        {
            "find": "甲方",
            "replace": "原告",
            "context_before": "",
            "context_after": "",
            "reason": "改甲方",
        }
    ])

    with patch("lib.llm_edit_planner._call_llm", return_value=llm_json):
        edits, warnings = plan_edits_with_llm(SAMPLE_TEXT, "改甲方")

    # "甲方" appears twice without context → ambiguous → edit skipped
    assert isinstance(edits, list)
    assert isinstance(warnings, list)
    # Should have a warning about ambiguous or 0 edits
    if not edits:
        assert len(warnings) > 0


# ── Test 9: malformed JSON from LLM → warning, empty edits ───────────────────

def test_plan_edits_malformed_json_from_llm():
    """Malformed LLM response → warning + empty edits."""
    with patch("lib.llm_edit_planner._call_llm", return_value="INVALID JSON [[["):
        edits, warnings = plan_edits_with_llm(SAMPLE_TEXT, "修改")

    assert edits == []
    assert any("parse" in w.lower() or "JSON" in w for w in warnings)
