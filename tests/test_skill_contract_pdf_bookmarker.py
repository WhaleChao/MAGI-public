"""
Skill contract tests for pdf-bookmarker.

Categories:
  1. Normal   - valid input produces expected output format
  2. Missing  - graceful handling when required fields are missing
  3. Boundary - edge cases (empty strings, very long input, special chars)
  4. Reject   - input that should be refused (injection, off-topic)
"""

import os
import sys
import ast
import re
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "pdf-bookmarker"
ACTION_PY = SKILL_DIR / "action.py"

# ---------------------------------------------------------------------------
# pdf-bookmarker imports fitz at module top level.  We mock fitz for all
# tests so no real PDF processing happens.
# ---------------------------------------------------------------------------


def _make_mock_fitz():
    m = MagicMock()
    m.open = MagicMock()
    return m


def _make_mock_ocr():
    m = MagicMock()
    m.RapidOCR = MagicMock
    return m


@pytest.fixture(autouse=True)
def _patch_heavy_deps():
    """Patch fitz and rapidocr so the module can be imported."""
    with patch.dict("sys.modules", {
        "fitz": _make_mock_fitz(),
        "rapidocr_onnxruntime": _make_mock_ocr(),
    }):
        yield


def _load_module():
    """Force-load the bookmarker action module with mocked deps."""
    import importlib.util
    mod_name = "pdf_bookmarker_action"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    spec = importlib.util.spec_from_file_location(mod_name, str(ACTION_PY))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===================================================================
# 1. Normal
# ===================================================================


class TestNormal:
    def test_action_py_exists(self):
        assert ACTION_PY.exists()

    def test_action_py_parseable(self):
        source = ACTION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(ACTION_PY))
        assert tree is not None

    def test_has_scan_and_bookmark_function(self):
        source = ACTION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert "scan_and_bookmark" in names

    def test_has_batch_process_function(self):
        source = ACTION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert "batch_process" in names

    def test_has_show_toc_function(self):
        source = ACTION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert "show_toc" in names

    def test_scan_and_bookmark_missing_file(self):
        """scan_and_bookmark returns failure dict for non-existent file."""
        mod = _load_module()
        result = mod.scan_and_bookmark("/nonexistent/test.pdf")
        assert isinstance(result, dict)
        assert result["success"] is False
        assert result["bookmarks"] == 0

    def test_doc_patterns_defined(self):
        """DOC_PATTERNS list should have reasonable number of patterns."""
        mod = _load_module()
        assert hasattr(mod, "DOC_PATTERNS")
        assert len(mod.DOC_PATTERNS) > 20, "Should define many court document patterns"


# ===================================================================
# 2. Missing data
# ===================================================================


class TestMissingData:
    def test_extract_roc_date_no_date(self):
        mod = _load_module()
        assert mod._extract_roc_date("這段文字沒有日期") is None

    def test_extract_roc_date_valid(self):
        mod = _load_module()
        result = mod._extract_roc_date("113年3月15日開庭")
        assert result is not None
        assert "113" in result

    def test_extract_party_no_match(self):
        mod = _load_module()
        result = mod._extract_party("這段文字沒有當事人名稱")
        assert result == ""

    def test_extract_party_with_defendant(self):
        mod = _load_module()
        result = mod._extract_party("被 告 王小明\n其餘內容")
        assert result == "王小明"

    def test_detect_doc_type_unknown_text(self):
        mod = _load_module()
        label, level = mod._detect_doc_type("這是一段普通文字，不包含任何法律文件標誌。")
        assert label is None

    def test_show_toc_missing_file(self):
        mod = _load_module()
        result = mod.show_toc("/nonexistent/file.pdf")
        assert "找不到" in result

    def test_batch_process_missing_folder(self):
        mod = _load_module()
        result = mod.batch_process("/nonexistent/folder")
        assert "不存在" in result


# ===================================================================
# 3. Boundary
# ===================================================================


class TestBoundary:
    def test_detect_doc_type_all_known_types(self):
        """Spot-check that key document types are recognized."""
        mod = _load_module()
        test_cases = [
            ("準備程序筆錄", "準備程序筆錄"),
            ("民事判決書", "判決"),
            ("起訴書", "起訴書"),
            ("送達證書", "送達證書"),
            ("鑑定報告書", "鑑定報告"),
        ]
        for text_fragment, expected_label in test_cases:
            label, level = mod._detect_doc_type(text_fragment)
            assert label is not None, f"Failed to detect: {text_fragment}"
            assert expected_label in label, f"Expected '{expected_label}' in '{label}' for '{text_fragment}'"

    def test_roc_date_western_year(self):
        mod = _load_module()
        result = mod._extract_roc_date("2024年3月15日提出")
        assert result is not None
        assert "113" in result

    def test_ola_separator_detection(self):
        mod = _load_module()
        assert mod._is_ola_separator("司法院線上閱卷系統作業平台\n")
        assert not mod._is_ola_separator("這是正常的法律文件內容" * 10)

    def test_is_prior_record_page(self):
        mod = _load_module()
        text = "臺灣高等法院被告前案紀錄表\n查詢條件：姓名 王大明\n報表編號 HHD4D01"
        assert mod._is_prior_record_page(text)

    def test_normalize_doc_type_aliases(self):
        mod = _load_module()
        assert mod._normalize_doc_type("上訴抗告狀") == "上訴/抗告狀"
        assert mod._normalize_doc_type("調解筆錄") == "調解/和解筆錄"

    def test_classify_no_boundary_single_doc_hint(self):
        mod = _load_module()
        classification, reason = mod._classify_no_boundary_case(
            pdf_path="/tmp/20250718 告知上訴權益同意書(余秋菊)_已簽名.pdf",
            page_count=1,
            meaningful_counts=[8],
            detected_doc_types=set(),
        )
        assert classification == "legitimate_single_doc"
        assert reason

    def test_classify_no_boundary_filename_hint_with_multi_doc_signals_goes_manual_review(self):
        mod = _load_module()
        page_texts = [
            "臺灣花蓮地方檢察署 起訴書\n收文章：114000123\n被告王小明",
            "臺灣花蓮地方法院刑事判決\n主文 被告王小明有罪\n第 1 頁",
        ]
        classification, reason = mod._classify_no_boundary_case(
            pdf_path="/tmp/20250718 告知上訴權益同意書(余秋菊)_已簽名.pdf",
            page_count=2,
            meaningful_counts=[120, 130],
            detected_doc_types=set(),
            page_texts=page_texts,
        )
        assert classification == "needs_manual_review"
        assert "multi_doc_signal" in reason


# ===================================================================
# 4. Should reject
# ===================================================================


class TestShouldReject:
    def test_no_eval_or_exec(self):
        source = ACTION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in ("eval", "exec"), (
                    f"action.py uses dangerous {node.func.id}()"
                )

    def test_scan_and_bookmark_non_pdf_path(self):
        """Should not crash on completely bogus path."""
        mod = _load_module()
        result = mod.scan_and_bookmark("")
        assert isinstance(result, dict)
        assert result["success"] is False

    def test_no_shell_true(self):
        source = ACTION_PY.read_text(encoding="utf-8")
        assert "shell=True" not in source
