from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _clean_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)
    environment.pop("PYTEST_ADDOPTS", None)
    # Candidate probes run from their own disposable working tree.  Inheriting
    # a caller-supplied PYTHONPATH can import the immutable release's
    # sitecustomize before the probe starts and write bytecode back into that
    # release when this helper intentionally removes PYTHONDONTWRITEBYTECODE.
    environment.pop("PYTHONPATH", None)
    return environment


def test_clean_environment_cannot_import_from_parent_candidate(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    assert "PYTHONPATH" not in _clean_environment()


def _copy_package(tmp_path: Path) -> Path:
    candidate = tmp_path / "candidate"
    shutil.copytree(ROOT / "magi_v3", candidate / "magi_v3", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return candidate


def test_route_parity_imports_gateway_without_writing_candidate_bytecode(tmp_path: Path) -> None:
    candidate = _copy_package(tmp_path)
    script = candidate / "scripts" / "v3_route_parity.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "v3_route_parity.py", script)
    probe = (
        "import importlib,runpy; "
        f"runpy.run_path({str(script)!r}, run_name='route_parity_probe'); "
        "importlib.import_module('magi_v3.gateway')"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=candidate,
        env=_clean_environment(),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert not (candidate / "magi_v3" / "__pycache__").exists()
    assert not list((candidate / "magi_v3").rglob("*.pyc"))


def test_candidate_pytest_collection_disables_bytecode_without_launcher(tmp_path: Path) -> None:
    candidate = _copy_package(tmp_path)
    tests = candidate / "tests"
    tests.mkdir()
    shutil.copy2(ROOT / "tests" / "conftest.py", tests / "conftest.py")
    shutil.copytree(ROOT / "tests" / "support", tests / "support", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (tests / "test_import_gateway.py").write_text(
        "def test_import_gateway():\n"
        "    import magi_v3.gateway\n"
        "    assert magi_v3.gateway is not None\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(tests)],
        cwd=candidate,
        env=_clean_environment(),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (candidate / "magi_v3" / "__pycache__").exists()
    assert not list((candidate / "magi_v3").rglob("*.pyc"))


def test_isolated_candidate_import_probes_do_not_escape_pytest_bytecode_fuse(tmp_path: Path) -> None:
    candidate = _copy_package(tmp_path)
    tests = candidate / "tests"
    tests.mkdir()
    shutil.copy2(ROOT / "tests" / "conftest.py", tests / "conftest.py")
    shutil.copytree(ROOT / "tests" / "support", tests / "support", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    target = tests / "v3"
    target.mkdir()
    shutil.copy2(ROOT / "tests" / "v3" / "test_gateway.py", target / "test_gateway.py")
    shutil.copy2(ROOT / "tests" / "v3" / "test_core_health.py", target / "test_core_health.py")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            f"{target / 'test_gateway.py'}::test_import_is_side_effect_free_and_does_not_load_apps_waitress_ml_or_ocr",
            f"{target / 'test_core_health.py'}::test_importing_health_does_not_import_heavy_frameworks",
        ],
        cwd=candidate,
        env=_clean_environment(),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not (candidate / "magi_v3" / "__pycache__").exists()
    assert not list((candidate / "magi_v3").rglob("*.pyc"))
