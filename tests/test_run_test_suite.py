from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.ops import run_test_suite


def test_resolve_command_replaces_tokens():
    command = run_test_suite.resolve_command(["{python}", "{root}", "x"])
    assert command[0] == sys.executable
    assert command[1] == str(run_test_suite.MAGI_ROOT)
    assert command[2] == "x"


def test_matrix_has_expected_suites():
    matrix = run_test_suite.load_matrix(run_test_suite.DEFAULT_MATRIX)
    suites = matrix["suites"]
    assert {"ci", "smoke50", "production-live", "commercial-release"} <= set(suites)
    assert len(suites["smoke50"]["checks"]) == 1
    assert len(suites["production-live"]["checks"]) >= 6


def test_commercial_release_runs_public_isolation_audit():
    matrix = run_test_suite.load_matrix(run_test_suite.DEFAULT_MATRIX)
    checks = matrix["suites"]["commercial-release"]["checks"]
    audit = next(check for check in checks if check["id"] == "public_release_audit")

    assert audit["command"] == [
        "{python}",
        "scripts/public_release_audit.py",
        "--public-isolation",
        "--strict",
    ]


def test_commercial_release_runs_tool_confusion_guard():
    matrix = run_test_suite.load_matrix(run_test_suite.DEFAULT_MATRIX)
    checks = {check["id"]: check for check in matrix["suites"]["commercial-release"]["checks"]}

    assert checks["tool_confusion_guard"]["command"][:2] == [
        "{python}",
        "scripts/ops/tool_confusion_guard.py",
    ]


def test_ci_suite_includes_static_safety_guards():
    matrix = run_test_suite.load_matrix(run_test_suite.DEFAULT_MATRIX)
    checks = {check["id"]: check for check in matrix["suites"]["ci"]["checks"]}

    assert checks["hardcoded_runtime_guard"]["command"] == ["{python}", "scripts/ci/check_hardcodes.py"]
    assert checks["shell_true_guard"]["command"] == ["{python}", "scripts/ci/check_shell_true.py"]
    assert "--fail-on-health" not in checks["function_health_index"]["command"]
    assert checks["live_conflict_audit"]["command"][:3] == [
        "{python}",
        "scripts/ops/business_module_live_check.py",
        "--conflict-audit",
    ]


def test_commercial_core_routes_writes_health_artifact():
    matrix = run_test_suite.load_matrix(run_test_suite.DEFAULT_MATRIX)
    checks = {check["id"]: check for check in matrix["suites"]["commercial-release"]["checks"]}

    command = checks["core_routes_heavy"]["command"]
    assert "--json-out" in command
    assert ".runtime/smoke_core_routes_release_latest.json" in command


def test_production_live_business_modules_writes_canonical_health_artifact():
    matrix = run_test_suite.load_matrix(run_test_suite.DEFAULT_MATRIX)
    checks = {check["id"]: check for check in matrix["suites"]["production-live"]["checks"]}

    command = checks["business_modules_live"]["command"]
    assert command[:2] == ["{python}", "scripts/ops/business_module_live_check.py"]
    assert "--json-out" in command
    assert ".runtime/business_module_live_check_latest.json" in command


def test_commercial_release_runs_function_health_gate():
    matrix = run_test_suite.load_matrix(run_test_suite.DEFAULT_MATRIX)
    checks = {check["id"]: check for check in matrix["suites"]["commercial-release"]["checks"]}

    command = checks["function_health_gate"]["command"]
    assert command[:2] == ["{python}", "scripts/ops/function_health_index.py"]
    assert "--fail-on-health" in command


def test_dry_run_suite_writes_all_checks(tmp_path: Path):
    matrix = {
        "suites": {
            "tiny": {
                "checks": [
                    {
                        "id": "hello",
                        "name": "Hello",
                        "command": ["{python}", "-c", "print('hello')"],
                    }
                ]
            }
        }
    }
    matrix_path = tmp_path / "matrix.json"
    matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
    loaded = run_test_suite.load_matrix(matrix_path)
    report = run_test_suite.run_suite(loaded, matrix_path, "tiny", dry_run=True)
    assert report.ok is True
    assert report.total == 1
    assert report.skipped == 1
    assert report.results[0]["command"][0] == sys.executable


def test_run_check_prepends_repo_root_to_pythonpath():
    code = (
        "import os, pathlib; "
        "parts=os.environ.get('PYTHONPATH','').split(os.pathsep); "
        f"assert pathlib.Path(parts[0]) == pathlib.Path({str(run_test_suite.MAGI_ROOT)!r})"
    )
    result = run_test_suite.run_check(
        {
            "id": "pythonpath",
            "name": "PYTHONPATH root",
            "command": ["{python}", "-c", code],
            "env": {"PYTHONPATH": "/tmp/example"},
        },
        dry_run=False,
    )
    assert result.ok is True
