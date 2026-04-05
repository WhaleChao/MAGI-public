"""Tests for CI gate scripts (check_hardcodes, check_monolith_size)."""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_script(name: str) -> subprocess.CompletedProcess:
    script = REPO_ROOT / "scripts" / "ci" / name
    return subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


class TestCheckHardcodes:
    def test_passes_on_current_codebase(self):
        result = _run_script("check_hardcodes.py")
        assert result.returncode == 0, (
            f"check_hardcodes failed unexpectedly:\n{result.stdout}\n{result.stderr}"
        )

    def test_output_contains_pass(self):
        result = _run_script("check_hardcodes.py")
        assert "PASS" in result.stdout


class TestCheckMonolithSize:
    def test_passes_on_current_codebase(self):
        result = _run_script("check_monolith_size.py")
        assert result.returncode == 0, (
            f"check_monolith_size failed unexpectedly:\n{result.stdout}\n{result.stderr}"
        )

    def test_output_contains_pass(self):
        result = _run_script("check_monolith_size.py")
        assert "PASS" in result.stdout
