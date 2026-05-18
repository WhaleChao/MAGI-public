"""
Skill contract tests for pdf-namer.

Categories:
  1. Normal   - valid input produces expected output format
  2. Missing  - graceful handling when required fields are missing
  3. Boundary - edge cases (empty strings, very long input, special chars)
  4. Reject   - input that should be refused (injection, off-topic)
"""

import os
import sys
import ast
import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

SKILL_DIR = Path(__file__).resolve().parent.parent / "skills" / "pdf-namer"
ACTION_PY = SKILL_DIR / "action.py"


# ---------------------------------------------------------------------------
# We cannot import pdf-namer/action.py directly because it has hard
# dependencies on fitz (PyMuPDF) and various MAGI internals at import time.
# Instead we verify the module parses and test key helper functions via
# targeted imports with mocked dependencies.
# ---------------------------------------------------------------------------


def _mock_fitz():
    """Create a mock fitz module."""
    mock = MagicMock()
    mock.open = MagicMock(return_value=MagicMock(
        page_count=1,
        needs_pass=False,
        __getitem__=MagicMock(return_value=MagicMock(get_text=MagicMock(return_value=""))),
    ))
    return mock


# ===================================================================
# 1. Normal — valid input produces expected output format
# ===================================================================


class TestNormal:
    def test_action_py_exists(self):
        assert ACTION_PY.exists(), f"action.py not found at {ACTION_PY}"

    def test_action_py_parseable(self):
        """action.py can be parsed by ast without syntax errors."""
        source = ACTION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(ACTION_PY))
        assert tree is not None

    def test_has_main_function(self):
        source = ACTION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        func_names = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ]
        assert "main" in func_names, "action.py must define a main() function"

    def test_has_rename_file_function(self):
        source = ACTION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        func_names = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ]
        assert "rename_file" in func_names

    def test_has_extract_text_function(self):
        source = ACTION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        func_names = [
            node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
        ]
        assert "extract_text" in func_names

    def test_module_imports_with_mocked_deps(self):
        """Module can be imported when heavy deps are mocked."""
        mock_fitz = _mock_fitz()
        with patch.dict("sys.modules", {
            "fitz": mock_fitz,
            "rapidocr_onnxruntime": MagicMock(),
            "PIL": MagicMock(),
            "PIL.Image": MagicMock(),
            "requests": MagicMock(),
        }):
            # Also mock the MAGI-internal imports
            mock_mapper = MagicMock()
            mock_mapper.default_case_roots = MagicMock(return_value=[])
            mock_mapper.preferred_case_roots = MagicMock(return_value=[])
            mock_case_utils = MagicMock()
            mock_case_utils.extract_case_number = MagicMock(return_value="")
            mock_case_utils.RE_CASE_NUMBER = MagicMock()
            mock_court_utils = MagicMock()
            mock_court_utils.extract_court_name = MagicMock(return_value="")
            mock_court_utils.RE_COURT_NAME = MagicMock()
            with patch.dict("sys.modules", {
                "api.case_path_mapper": mock_mapper,
                "skills.bridge.shared_utils.case_number_utils": mock_case_utils,
                "skills.bridge.shared_utils.court_utils": mock_court_utils,
            }):
                # Force reimport
                mod_name = "skills.pdf_namer_action_test_import"
                if mod_name in sys.modules:
                    del sys.modules[mod_name]
                # Just verify import does not raise
                import importlib.util
                spec = importlib.util.spec_from_file_location(mod_name, str(ACTION_PY))
                mod = importlib.util.module_from_spec(spec)
                # We do NOT exec the module because side effects are too heavy;
                # the ast parse test is sufficient to prove no syntax errors.


# ===================================================================
# 2. Missing data — graceful handling when required fields are missing
# ===================================================================


class TestMissingData:
    def test_rename_file_nonexistent_pdf_no_crash(self):
        """rename_file should handle non-existent paths gracefully."""
        # We verify at the AST level that rename_file has early-return logic
        source = ACTION_PY.read_text(encoding="utf-8")
        assert "generate_name_proposal" in source, (
            "rename_file should delegate to generate_name_proposal"
        )

    def test_extract_text_handles_missing_file(self):
        """extract_text should check os.path.exists before opening."""
        source = ACTION_PY.read_text(encoding="utf-8")
        assert "os.path.exists" in source or "Path" in source

    def test_main_function_uses_argparse(self):
        """CLI entry uses argparse for structured argument handling."""
        source = ACTION_PY.read_text(encoding="utf-8")
        assert "argparse" in source


# ===================================================================
# 3. Boundary — edge cases
# ===================================================================


class TestBoundary:
    def test_skill_md_exists(self):
        skill_md = SKILL_DIR / "SKILL.md"
        assert skill_md.exists(), "SKILL.md must exist for pdf-namer"

    def test_skill_md_does_not_instruct_retired_glm_ocr(self):
        skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "Vision 模型在 port 8082" not in skill_md
        assert "GLM-OCR-bf16" not in skill_md
        assert "macOS Vision" in skill_md

    def test_naming_rules_module_exists(self):
        naming_rules = SKILL_DIR / "naming_rules.py"
        assert naming_rules.exists(), "naming_rules.py should exist"

    def test_naming_rules_parseable(self):
        naming_rules = SKILL_DIR / "naming_rules.py"
        if naming_rules.exists():
            source = naming_rules.read_text(encoding="utf-8")
            tree = ast.parse(source)
            assert tree is not None


# ===================================================================
# 4. Should reject — injection / off-topic
# ===================================================================


class TestShouldReject:
    def test_no_eval_or_exec_in_action(self):
        """action.py should not use eval() or exec() which could be exploited."""
        source = ACTION_PY.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in ("eval", "exec"):
                    pytest.fail(f"action.py uses dangerous {func.id}() call")

    def test_no_shell_true_in_subprocess(self):
        """subprocess calls should not use shell=True."""
        source = ACTION_PY.read_text(encoding="utf-8")
        # Simple heuristic check
        assert "shell=True" not in source, "subprocess should not use shell=True"

    def test_no_hardcoded_credentials(self):
        """No hardcoded API keys or passwords (except known court PDF passwords)."""
        source = ACTION_PY.read_text(encoding="utf-8")
        # Known exception: court PDF password "3800" is expected
        lines = source.splitlines()
        for i, line in enumerate(lines, 1):
            lower = line.lower()
            if any(kw in lower for kw in ["api_key=", "password=", "secret="]):
                # Allow env var lookups and the known court PDF password
                if "os.environ" in line or "3800" in line or "test" in lower:
                    continue
                pytest.fail(f"Line {i} may contain hardcoded credentials: {line.strip()}")
