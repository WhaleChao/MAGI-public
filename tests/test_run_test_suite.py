from __future__ import annotations

import json
import sys
from pathlib import Path

from scripts.ops import run_test_suite


def test_resolve_command_replaces_tokens():
    command = run_test_suite.resolve_command(["{python}", "{root}", "x"])
    assert command[0] == sys.executable
    assert command[1].endswith("MAGI_v2")
    assert command[2] == "x"


def test_matrix_has_expected_suites():
    matrix = run_test_suite.load_matrix(run_test_suite.DEFAULT_MATRIX)
    suites = matrix["suites"]
    assert {"ci", "smoke50", "production-live", "commercial-release"} <= set(suites)
    assert len(suites["smoke50"]["checks"]) == 1
    assert len(suites["production-live"]["checks"]) >= 6


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
