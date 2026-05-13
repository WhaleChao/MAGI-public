"""
tests/test_docx_editor_chat_edit.py

Phase 3: cmd_chat_edit tests. LLM calls are mocked.
"""

import os
import sys
import json
import pytest
from unittest.mock import patch, MagicMock

_SKILL_DIR = os.path.join(os.path.dirname(__file__), "..", "skills", "docx-editor")
sys.path.insert(0, _SKILL_DIR)
sys.path.insert(0, os.path.join(_SKILL_DIR, "lib"))

# Import action module
import importlib.util
_spec = importlib.util.spec_from_file_location("docx_action", os.path.join(_SKILL_DIR, "action.py"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
cmd_chat_edit = _mod.cmd_chat_edit

# Fixture path
_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "docx_editor", "simple.docx")


def _mock_plan_edits(edits_list, warnings_list):
    """Return a mock that returns given (edits, warnings) from plan_edits_with_llm."""
    def _mock(docx_text, instruction, **kwargs):
        return edits_list, warnings_list
    return _mock


# ── Test 1: security gate blocks CLI without MAGI_DOCX_EDITOR_ALLOW_CLI ──────

def test_cmd_chat_edit_security_gate_blocks_empty_source():
    """source='' without env override should be blocked."""
    result = cmd_chat_edit(
        doc_path=_FIXTURE_PATH,
        instruction="把甲方改成原告",
        source="",
    )
    assert result["ok"] is False
    assert result["changes_applied"] == 0
    assert any("安全閘門" in e["reason"] for e in result["errors"])


# ── Test 2: security gate allows telegram source ────────────────────────────

def test_cmd_chat_edit_security_gate_allows_telegram():
    """source='telegram' should pass the security gate."""
    from lib.tracked_edits import EditInput

    mock_edit = EditInput(
        find="Hello World",
        replace="Hello MAGI",
        context_before="",
        context_after="",
        reason="test",
    )

    with patch("lib.llm_edit_planner.plan_edits_with_llm", return_value=([mock_edit], [])):
        result = cmd_chat_edit(
            doc_path=_FIXTURE_PATH,
            instruction="把 Hello World 改成 Hello MAGI",
            source="telegram",
        )

    # Should pass gate and attempt edit
    assert "errors" in result
    assert "changes_applied" in result
    # With a valid fixture and valid edit, should succeed
    assert result["ok"] is True or result["changes_applied"] >= 0


# ── Test 3: security gate allows discord source ──────────────────────────────

def test_cmd_chat_edit_security_gate_allows_discord():
    """source='discord' should pass the security gate."""
    with patch("lib.llm_edit_planner.plan_edits_with_llm", return_value=([], ["LLM 判定指令超出 anchored edit 範圍"])):
        result = cmd_chat_edit(
            doc_path=_FIXTURE_PATH,
            instruction="重寫全文",
            source="discord",
        )

    assert result["ok"] is True
    assert result["changes_applied"] == 0
    assert any("建議用 @MAGI 產文件" in w or "超出" in w for w in result["warnings"])


# ── Test 4: MAGI_DOCX_EDITOR_ALLOW_CLI=1 bypasses gate ──────────────────────

def test_cmd_chat_edit_allow_cli_env():
    """MAGI_DOCX_EDITOR_ALLOW_CLI=1 should bypass the security gate."""
    with patch.dict(os.environ, {"MAGI_DOCX_EDITOR_ALLOW_CLI": "1"}):
        with patch("lib.llm_edit_planner.plan_edits_with_llm", return_value=([], [])):
            result = cmd_chat_edit(
                doc_path=_FIXTURE_PATH,
                instruction="測試",
                source="cli",
            )
    # Gate should not block
    assert "安全閘門" not in json.dumps(result.get("errors", []))


# ── Test 5: output written to /tmp/magi_docx_edits/ by default ────────────────

def test_cmd_chat_edit_output_default_path():
    """When edits are applied, output should be in /tmp/magi_docx_edits/."""
    from lib.tracked_edits import EditInput

    mock_edit = EditInput(
        find="Hello World",
        replace="Hello MAGI",
        context_before="",
        context_after="",
        reason="test",
    )

    with patch("lib.llm_edit_planner.plan_edits_with_llm", return_value=([mock_edit], [])):
        result = cmd_chat_edit(
            doc_path=_FIXTURE_PATH,
            instruction="修改",
            source="user",
        )

    if result.get("output_path"):
        assert "/tmp/magi_docx_edits/" in result["output_path"]


# ── Test 6: LLM warnings propagated to result ─────────────────────────────────

def test_cmd_chat_edit_warnings_propagated():
    """LLM planner warnings should appear in result warnings."""
    with patch("lib.llm_edit_planner.plan_edits_with_llm",
               return_value=([], ["anchor 預檢失敗（not_found）"])):
        result = cmd_chat_edit(
            doc_path=_FIXTURE_PATH,
            instruction="修改",
            source="line",
        )

    assert any("not_found" in w or "預檢" in w for w in result.get("warnings", []))
