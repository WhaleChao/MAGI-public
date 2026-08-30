from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]


def _load(relative: str) -> Any:
    return json.loads((ROOT / relative).read_text(encoding="utf-8"))


def _strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)


def _declared_test_paths(value: Any) -> set[str]:
    return {item for item in _strings(value) if item.startswith("tests/")}


def test_active_test_matrix_never_references_missing_tests() -> None:
    matrix = _load("config/test_matrix.json")
    declared = _declared_test_paths(matrix)
    assert declared
    assert sorted(path for path in declared if not (ROOT / path).is_file()) == []


def test_v3_quality_manifest_never_references_missing_or_v2_tests() -> None:
    manifest = _load("config/v3_release_quality_suites.json")
    assert manifest["legacy_v2_validation"]["mode"] == "disabled"
    declared = _declared_test_paths(manifest)
    assert declared
    assert sorted(path for path in declared if not (ROOT / path).is_file()) == []
    assert sorted(path for path in declared if "/v2/" in path.lower()) == []


def test_py_compile_gate_never_references_missing_source_files() -> None:
    matrix = _load("config/test_matrix.json")
    checks = matrix["suites"]["ci"]["checks"]
    compile_check = next(item for item in checks if item["id"] == "py_compile_core")
    declared = [item for item in compile_check["command"] if str(item).endswith(".py")]
    assert declared
    assert sorted(path for path in declared if not (ROOT / path).is_file()) == []


def test_pre_cutover_suites_never_mirror_evidence_into_live_runtime() -> None:
    matrix = _load("config/test_matrix.json")
    for suite_name in ("ci", "commercial-release"):
        commands = [
            token
            for check in matrix["suites"][suite_name]["checks"]
            for token in check["command"]
        ]
        assert "--mirror-live-runtime" not in commands

    live_commands = [
        token
        for check in matrix["suites"]["production-live"]["checks"]
        for token in check["command"]
    ]
    assert "--mirror-live-runtime" in live_commands


def test_user_reported_regression_manifest_is_complete_and_resolvable() -> None:
    manifest = _load("config/v3_regression_scenarios.json")
    scenarios = manifest["scenarios"]
    required_ids = {
        "file_review_huang_taicheng_payment_identity_change",
        "laf_existing_nas_attachments_not_missing",
        "laf_archived_case_retry_reconciliation",
        "laf_case_stage_and_path_reference_repair",
        "scheduled_deferred_is_not_failed",
        "funnel_external_outage_requires_offhost_evidence",
        "predecessor_failure_cannot_redlight_active_release",
    }
    assert {row["id"] for row in scenarios} == required_ids

    release_tests = _declared_test_paths(_load("config/v3_release_quality_suites.json"))
    for scenario in scenarios:
        assert scenario["reason_code"]
        assert scenario["tests"]
        for nodeid in scenario["tests"]:
            path_text, separator, function_name = nodeid.partition("::")
            path = ROOT / path_text
            assert separator and function_name.startswith("test_")
            assert path.is_file()
            assert f"def {function_name}(" in path.read_text(encoding="utf-8")
            assert path_text in release_tests


def test_suite_timeout_preserves_binary_partial_output_as_text(monkeypatch) -> None:
    from scripts.ops import run_test_suite

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(
            ["pytest"],
            1,
            output=b"partial-stdout\xff",
            stderr=b"partial-stderr\xfe",
        )

    monkeypatch.setattr(run_test_suite.subprocess, "run", timeout)
    result = run_test_suite.run_check(
        {
            "id": "timeout-proof",
            "name": "timeout proof",
            "command": ["pytest"],
            "timeout_sec": 1,
        },
        dry_run=False,
        suite="ci",
    )

    assert result.ok is False
    assert isinstance(result.stdout_tail, str)
    assert isinstance(result.stderr_tail, str)
    assert result.stdout_tail.startswith("partial-stdout")
    assert result.stderr_tail.startswith("partial-stderr")
    assert result.message == "timeout after 1s"
