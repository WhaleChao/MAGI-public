"""
Skill smoke tests for the evaluation system.

Extends existing smoke tests with:
  1. test_skill_definitions_complete  - all skills have SKILL.md or action.py
  2. test_skill_imports_clean         - action.py can be parsed without syntax errors
  3. test_priority_skills_have_contracts - the 6 priority skills have contract test files
"""

import os
import sys
import ast
import pytest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO_ROOT / "skills"
TESTS_DIR = REPO_ROOT / "tests"

# The 6 priority skills that must have contract tests
PRIORITY_SKILLS = [
    "contract-review",
    "pdf-namer",
    "pdf-bookmarker",
    "worldmonitor-intel",
    "trial-prep",
    "market-briefing",
]


def _all_skill_dirs() -> list[Path]:
    """Return all directories under skills/ that look like skill packages."""
    if not SKILLS_DIR.is_dir():
        return []
    return sorted(
        d for d in SKILLS_DIR.iterdir()
        if d.is_dir()
        and not d.name.startswith(".")
        and not d.name.startswith("_")
        and not d.name == "__pycache__"
    )


# ===================================================================
# 1. test_skill_definitions_complete
# ===================================================================


class TestSkillDefinitionsComplete:
    """Every skill directory should have at least a SKILL.md or an action.py."""

    def test_skills_dir_exists(self):
        assert SKILLS_DIR.is_dir(), f"Skills directory not found: {SKILLS_DIR}"

    def test_all_skills_have_definition_file(self):
        missing = []
        for skill_dir in _all_skill_dirs():
            has_skill_md = (skill_dir / "SKILL.md").exists()
            has_action_py = (skill_dir / "action.py").exists()
            has_init = (skill_dir / "__init__.py").exists()
            has_any_py = any(skill_dir.glob("*.py"))
            # Anthropic document skill bundles (pptx/xlsx/bilingual-docx) ship
            # as `scripts/` subdirs + reference markdown instead of action.py;
            # accept that shape too.
            has_scripts = (skill_dir / "scripts").is_dir()
            has_md_docs = any(skill_dir.glob("*.md"))
            if not (
                has_skill_md or has_action_py or has_init or has_any_py
                or has_scripts or has_md_docs
            ):
                missing.append(skill_dir.name)
        assert missing == [], (
            f"Skills without SKILL.md or action.py or any .py: {missing}"
        )

    def test_priority_skills_have_skill_md(self):
        missing = []
        for name in PRIORITY_SKILLS:
            skill_dir = SKILLS_DIR / name
            if not skill_dir.is_dir():
                missing.append(f"{name} (dir missing)")
                continue
            if not (skill_dir / "SKILL.md").exists():
                missing.append(name)
        assert missing == [], f"Priority skills missing SKILL.md: {missing}"

    def test_priority_skills_have_action_py(self):
        missing = []
        for name in PRIORITY_SKILLS:
            skill_dir = SKILLS_DIR / name
            if not skill_dir.is_dir():
                missing.append(f"{name} (dir missing)")
                continue
            if not (skill_dir / "action.py").exists():
                missing.append(name)
        assert missing == [], f"Priority skills missing action.py: {missing}"


# ===================================================================
# 2. test_skill_imports_clean
# ===================================================================


class TestSkillImportsClean:
    """Every action.py in the skills directory should parse without syntax errors."""

    @pytest.fixture
    def action_py_files(self) -> list[Path]:
        return sorted(SKILLS_DIR.rglob("action.py"))

    def test_at_least_some_action_files_found(self, action_py_files):
        assert len(action_py_files) >= 6, (
            f"Expected at least 6 action.py files, found {len(action_py_files)}"
        )

    def test_all_action_py_parseable(self, action_py_files):
        errors = []
        for path in action_py_files:
            try:
                source = path.read_text(encoding="utf-8")
                ast.parse(source, filename=str(path))
            except SyntaxError as e:
                rel = path.relative_to(REPO_ROOT)
                errors.append(f"{rel}: {e}")
        assert errors == [], (
            f"Syntax errors in action.py files:\n" + "\n".join(errors)
        )

    @pytest.mark.parametrize("skill_name", PRIORITY_SKILLS)
    def test_priority_skill_action_parseable(self, skill_name):
        action_py = SKILLS_DIR / skill_name / "action.py"
        assert action_py.exists(), f"{skill_name}/action.py not found"
        source = action_py.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(action_py))
        assert tree is not None

    @pytest.mark.parametrize("skill_name", PRIORITY_SKILLS)
    def test_priority_skill_has_main_or_entry(self, skill_name):
        """Each priority skill's action.py should have a main() or clearly defined entry point."""
        action_py = SKILLS_DIR / skill_name / "action.py"
        source = action_py.read_text(encoding="utf-8")
        tree = ast.parse(source)
        func_names = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        # Should have main() or at least one public function
        has_main = "main" in func_names
        has_public = any(not name.startswith("_") for name in func_names)
        assert has_main or has_public, (
            f"{skill_name}/action.py has no main() and no public functions"
        )


# ===================================================================
# 3. test_priority_skills_have_contracts
# ===================================================================


class TestPrioritySkillsHaveContracts:
    """All 6 priority skills should have corresponding contract test files."""

    def _contract_test_name(self, skill_name: str) -> str:
        """Convert skill name to expected test filename."""
        safe_name = skill_name.replace("-", "_")
        return f"test_skill_contract_{safe_name}.py"

    @pytest.mark.parametrize("skill_name", PRIORITY_SKILLS)
    def test_contract_test_exists(self, skill_name):
        test_file = TESTS_DIR / self._contract_test_name(skill_name)
        assert test_file.exists(), (
            f"Contract test missing for {skill_name}: expected {test_file.name}"
        )

    @pytest.mark.parametrize("skill_name", PRIORITY_SKILLS)
    def test_contract_test_parseable(self, skill_name):
        test_file = TESTS_DIR / self._contract_test_name(skill_name)
        if not test_file.exists():
            pytest.skip(f"Contract test not found: {test_file.name}")
        source = test_file.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(test_file))
        assert tree is not None

    @pytest.mark.parametrize("skill_name", PRIORITY_SKILLS)
    def test_contract_test_has_four_categories(self, skill_name):
        """Each contract test should have the 4 required test categories."""
        test_file = TESTS_DIR / self._contract_test_name(skill_name)
        if not test_file.exists():
            pytest.skip(f"Contract test not found: {test_file.name}")
        source = test_file.read_text(encoding="utf-8")
        tree = ast.parse(source)
        class_names = {
            node.name for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        }
        expected = {"TestNormal", "TestMissingData", "TestBoundary", "TestShouldReject"}
        missing = expected - class_names
        assert not missing, (
            f"Contract test for {skill_name} is missing categories: {missing}"
        )

    def test_all_priority_contracts_present(self):
        """Summary check: all 6 priority skills have their contract test file."""
        missing = []
        for skill_name in PRIORITY_SKILLS:
            test_file = TESTS_DIR / self._contract_test_name(skill_name)
            if not test_file.exists():
                missing.append(skill_name)
        assert missing == [], f"Missing contract tests for: {missing}"
