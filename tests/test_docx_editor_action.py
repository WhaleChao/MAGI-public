"""
Tests for skills/docx-editor/action.py (CLI smoke tests)
"""

import importlib.util
import io
import json
import os
import sys
import tempfile


def _import_docx_editor_action():
    """Import skills/docx-editor/action.py without polluting sys.modules with 'action'."""
    action_path = os.path.join(
        os.path.dirname(__file__), "..", "skills", "docx-editor", "action.py"
    )
    spec = importlib.util.spec_from_file_location("docx_editor_action", action_path)
    mod = importlib.util.module_from_spec(spec)
    # Ensure lib/ is importable relative to the skill
    skill_dir = os.path.dirname(os.path.abspath(action_path))
    if skill_dir not in sys.path:
        sys.path.insert(0, skill_dir)
    spec.loader.exec_module(mod)
    return mod


docx_action = _import_docx_editor_action()

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "docx_editor")


def fixture_path(name: str) -> str:
    return os.path.join(FIXTURES_DIR, name)


class TestSelfTest:
    def test_self_test_returns_ok(self):
        """cmd_self_test() 全綠"""
        result = docx_action.cmd_self_test()
        assert result["ok"] is True
        assert result["errors"] == []


class TestCmdApply:
    def test_cmd_apply_basic(self):
        """cmd_apply: 套一個 edit，成功寫出輸出檔"""
        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as tf:
            out_path = tf.name

        try:
            result = docx_action.cmd_apply(
                doc_path=fixture_path("simple.docx"),
                edits=[{
                    "find": "Hello World",
                    "replace": "Hello MAGI",
                    "context_before": "",
                    "context_after": "",
                }],
                output_path=out_path,
                author="TestAuthor",
            )
            assert result["ok"] is True
            assert result["success_count"] == 1
            assert result["error_count"] == 0
            assert os.path.exists(out_path)
            assert os.path.getsize(out_path) > 0
        finally:
            if os.path.exists(out_path):
                os.unlink(out_path)

    def test_cmd_apply_returns_errors_for_bad_find(self):
        """cmd_apply: find 找不到時 ok=False，errors 含訊息"""
        result = docx_action.cmd_apply(
            doc_path=fixture_path("simple.docx"),
            edits=[{
                "find": "不存在的文字",
                "replace": "新文字",
                "context_before": "",
                "context_after": "",
            }],
        )
        assert result["ok"] is False
        assert result["error_count"] == 1
        assert len(result["errors"]) == 1


class TestCmdExtract:
    def test_cmd_extract_returns_text(self):
        """cmd_extract: 回傳正確的文字和段落數"""
        result = docx_action.cmd_extract(doc_path=fixture_path("simple.docx"))
        assert "text" in result
        assert "paragraph_count" in result
        assert "Hello World" in result["text"]
        assert result["paragraph_count"] >= 1

    def test_cmd_extract_multi_paragraph(self):
        """cmd_extract: 多段文件正確計數"""
        result = docx_action.cmd_extract(doc_path=fixture_path("multi_paragraph.docx"))
        assert result["paragraph_count"] >= 3
        assert "First paragraph" in result["text"]
        assert "Second paragraph" in result["text"]


class TestCmdFind:
    def test_cmd_find_finds_text(self):
        """cmd_find: 找到目標文字"""
        result = docx_action.cmd_find(
            doc_path=fixture_path("multi_paragraph.docx"),
            query="defendant",
        )
        assert "matches" in result
        assert result["total"] >= 1
        assert result["matches"][0]["match"] == "defendant"

    def test_cmd_find_not_found(self):
        """cmd_find: 找不到時回傳空 matches"""
        result = docx_action.cmd_find(
            doc_path=fixture_path("simple.docx"),
            query="不存在的文字",
        )
        assert result["total"] == 0
        assert result["matches"] == []

    def test_cmd_find_includes_context(self):
        """cmd_find: matches 包含前後文"""
        result = docx_action.cmd_find(
            doc_path=fixture_path("simple.docx"),
            query="World",
            context_chars=5,
        )
        if result["total"] > 0:
            match = result["matches"][0]
            assert "before" in match
            assert "after" in match
            assert match["match"] == "World"
