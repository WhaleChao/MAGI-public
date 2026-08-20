from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts.v3_validation.perf_certification import (
    BLOCKER_CODE,
    LIVE_ROOT,
    REQUEST_PLAN,
    SCHEMA,
    PerformanceCertificationError,
    _prepare_sandbox,
    compare_arm_reports,
    request_plan_sha256,
    verify_performance_certification,
)


def _arm(arm: str, *, p95: int = 100, folder: bool = True, archive: bool = True) -> dict:
    responses = {
        "unauthorized_get": {"status": 401, "unauthorized": True},
        "authenticated_get": {"status": 200, "ok": True, "case_numbers": ["2099-9001"]},
        "idempotent_upsert": {
            "status": 200,
            "ok": True,
            "id": "perf-upsert",
            "case_number": "2099-9001",
            "mode": "upsert",
            "folder_ok": False,
            "archive_ok": False,
            "error": "",
        },
        "create_case_folder": {"status": 200 if folder else 501, "folder_ok": folder},
        "archive_closed_case": {"status": 200 if archive else 501, "archive_ok": archive},
    }
    return {
        "arm": arm,
        "request_plan_sha256": request_plan_sha256(),
        "release_binding": {
            "python_executable_sha256": "1" * 64,
            "script_sha256": "2" * 64,
        },
        "backend": {"engine": "MariaDB", "tcp_networking": False},
        "responses": responses,
        "scenario_latency_us": {
            "authenticated_get": p95,
            "idempotent_upsert": p95,
            "create_case_folder": p95,
            "archive_closed_case": p95,
        },
        "warm": {"p95_us": p95},
        "filesystem": {"entries": [{"path": "matched"}]},
        "database_state": [{"id": "matched"}],
    }


def test_request_plan_covers_session_mariadb_folder_and_archive() -> None:
    assert {row["scope"] for row in REQUEST_PLAN} == {
        "session",
        "mariadb_session",
        "nas_folder",
        "nas_archive",
    }
    assert len(request_plan_sha256()) == 64
    assert [row["id"] for row in REQUEST_PLAN] == [
        "unauthorized_get",
        "authenticated_get",
        "idempotent_upsert",
        "create_case_folder",
        "archive_closed_case",
    ]


def test_matched_comparator_clears_only_complete_equivalent_plan() -> None:
    result = compare_arm_reports([_arm("v2")], [_arm("v3", p95=104)], p95_regression_limit=0.05)

    assert result["same_host_sequential"] is True
    assert result["mariadb_backend"] is True
    assert result["session_passed"] is True
    assert result["folder_passed"] is True
    assert result["archive_passed"] is True
    assert result["filesystem_transcript_equivalent"] is True
    assert result["database_state_equivalent"] is True
    assert result["semantic_equivalence_passed"] is True
    assert result["performance"]["passed"] is True
    assert result["eligible_to_clear_full_v2_v3_performance_blocker"] is True
    assert result["gaps"] == []


def test_missing_v3_folder_or_archive_fails_closed() -> None:
    result = compare_arm_reports(
        [_arm("v2")], [_arm("v3", folder=False, archive=False)], p95_regression_limit=0.05
    )

    assert result["eligible_to_clear_full_v2_v3_performance_blocker"] is False
    assert result["folder_passed"] is False
    assert result["archive_passed"] is False
    assert any("case-folder" in gap for gap in result["gaps"])
    assert any("archive" in gap for gap in result["gaps"])


def test_runtime_or_request_plan_drift_is_rejected() -> None:
    drift = _arm("v3")
    drift["request_plan_sha256"] = "0" * 64
    with pytest.raises(PerformanceCertificationError, match="request-plan"):
        compare_arm_reports([_arm("v2")], [drift], p95_regression_limit=0.05)


def test_folder_marker_creation_timestamp_is_the_only_normalized_filesystem_field() -> None:
    v2 = _arm("v2")
    v3 = _arm("v3")
    marker = "01_案件/案件/.gitkeep"
    def marker_row(timestamp: str) -> dict:
        raw = f"# 案件 - 建立於 {timestamp}"
        normalized = "# 案件 - 建立於 <YYYY-MM-DD HH:MM:SS>"
        return {
            "path": marker,
            "kind": "file",
            "size": len(raw.encode()),
            "sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "raw_content": raw,
            "raw_content_sha256": hashlib.sha256(raw.encode()).hexdigest(),
            "normalized_content": normalized,
            "normalized_content_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
        }
    v2["filesystem"] = {
        "entries": [marker_row("2026-07-17 02:03:04")]
    }
    v3["filesystem"] = {
        "entries": [marker_row("2026-07-17 02:03:05")]
    }
    assert compare_arm_reports(
        [v2], [v3], p95_regression_limit=0.05
    )["filesystem_transcript_equivalent"] is True

    v3["filesystem"]["entries"][0]["size"] += 1
    with pytest.raises(PerformanceCertificationError, match="marker size"):
        compare_arm_reports([v2], [v3], p95_regression_limit=0.05)


@pytest.mark.parametrize(
    ("path", "raw"),
    [
        ("01_案件/案件/.gitkeep", "# 案件 - 建立於 2026-99-99 25:61:61"),
        ("01_案件/錯誤/.gitkeep", "# 案件 - 建立於 2026-07-17 02:03:04"),
        ("01_案件/案件/.gitkeep", "# 案件 - 建立於 2026-07-17 02:03:04\nextra"),
        ("01_案件/案件/marker.txt", "# 案件 - 建立於 2026-07-17 02:03:04"),
    ],
)
def test_folder_marker_normalization_rejects_invalid_date_parent_extra_or_non_gitkeep(
    path: str, raw: str
) -> None:
    normalized = f"# {Path(path).parent.name} - 建立於 <YYYY-MM-DD HH:MM:SS>"
    row = {
        "path": path,
        "kind": "file",
        "size": len(raw.encode()),
        "sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "raw_content": raw,
        "raw_content_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "normalized_content": normalized,
        "normalized_content_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
    }
    v2, v3 = _arm("v2"), _arm("v3")
    v2["filesystem"] = {"entries": [row]}
    v3["filesystem"] = {"entries": [row]}
    with pytest.raises(PerformanceCertificationError):
        compare_arm_reports([v2], [v3], p95_regression_limit=0.05)


def test_performance_certification_hash_fails_closed_after_tamper() -> None:
    evidence = {"schema": SCHEMA, "status": "blocked", "gate": {"blocker_code": BLOCKER_CODE}}
    unsigned = json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    evidence["evidence_sha256"] = hashlib.sha256(unsigned).hexdigest()
    verify_performance_certification(evidence)
    evidence["status"] = "certified"
    with pytest.raises(PerformanceCertificationError, match="does not match"):
        verify_performance_certification(evidence)


def test_performance_sandbox_rejects_live_source_nonempty_and_symlink(tmp_path: Path) -> None:
    with pytest.raises(PerformanceCertificationError, match="live MAGI"):
        _prepare_sandbox(LIVE_ROOT)
    source = Path(__file__).resolve().parents[2]
    with pytest.raises(PerformanceCertificationError, match="source tree"):
        _prepare_sandbox(source / "forbidden-perf")
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "preserve").write_text("preserve", encoding="utf-8")
    with pytest.raises(PerformanceCertificationError, match="empty"):
        _prepare_sandbox(occupied)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(PerformanceCertificationError, match="symlink"):
        _prepare_sandbox(link)
