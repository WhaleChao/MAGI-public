from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.v3_release_gate import BoundArtifact, _authoritative_normalized_metrics
from scripts.v3_validation.v3_rotation_drill import (
    PHASES,
    SCHEMA,
    V3RotationDrillBlocked,
    _digest,
    _phase,
    _validate_control_paths,
    derive_v3_rotation_metrics,
)


ROOT = Path(__file__).resolve().parents[2]
CONFIG = json.loads((ROOT / "config" / "v3_cutover_gates.json").read_text())
CONTEXT = {
    "campaign_id": "rc643-v3-rotation-test",
    "release_sha": "c" * 64,
    "hardware_id": "magi-test-mac",
    "gate_config_sha256": "d" * 64,
}


def _release(release_id: str, release_sha: str, suffix: str) -> dict[str, str]:
    return {
        "release_root": f"/immutable/releases/{release_id}",
        "release_id": release_id,
        "release_manifest_sha256": suffix * 64,
        "release_sha": release_sha,
        "source_commit": suffix * 40,
        "launcher_sha256": hashlib.sha256(f"launcher-{suffix}".encode()).hexdigest(),
    }


def _marker(transaction: str, release: dict[str, str]) -> dict[str, object]:
    return {
        "schema": "magi.v3.active-release/v1",
        "schema_version": 1,
        "transaction_id": transaction,
        "release": "v3",
        "release_id": release["release_id"],
        "release_root": release["release_root"],
        "release_manifest_sha256": release["release_manifest_sha256"],
        "committed_at": datetime.now(timezone.utc).isoformat(),
    }


def _sentinel() -> dict[str, object]:
    return {
        "database_sha256": hashlib.sha256(b"sentinel").hexdigest(),
        "committed_id_hashes": [
            hashlib.sha256(f"committed-{index}".encode()).hexdigest()
            for index in range(8)
        ],
        "outbox_id_hashes": [
            hashlib.sha256(f"outbox-{index}".encode()).hexdigest()
            for index in range(5)
        ],
        "duplicate_committed_jobs": 0,
        "duplicate_outbox_entries": 0,
    }


def _report() -> dict[str, object]:
    report_started = datetime.now(timezone.utc)
    previous = _release("v3-previous-r59", "b" * 64, "a")
    candidate = _release("v3-candidate-r66", CONTEXT["release_sha"], "e")
    runs = []
    for run_index in range(1, 4):
        transaction = f"{run_index:032x}"
        previous_marker = _marker(transaction, previous)
        candidate_marker = _marker(transaction, candidate)
        phases = []
        owner_rows = {
            "previous_active": previous,
            "candidate_active": candidate,
            "candidate_restarted": candidate,
            "previous_restored": previous,
        }
        for sequence, name in enumerate(PHASES, start=1):
            release = candidate if name.startswith("candidate") else previous
            marker = candidate_marker if name.startswith("candidate") else previous_marker
            owner_release = owner_rows.get(name)
            receipt = None
            if owner_release is not None:
                nonce = hashlib.sha256(f"{run_index}-{name}".encode()).hexdigest()[:32]
                receipt = {
                    "schema": "magi.v3.v3-rotation-owner-probe/v1",
                    "release_id": owner_release["release_id"],
                    "release_manifest_sha256": owner_release[
                        "release_manifest_sha256"
                    ],
                    "pid": run_index * 100 + sequence,
                    "nonce": nonce,
                    "marker_sha256": hashlib.sha256(
                        json.dumps(
                            marker, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                }
            publication = None
            if name in {"candidate_committed", "previous_committed"}:
                publication = {
                    "atomic_replace": True,
                    "before_inode": run_index * 10 + sequence,
                    "after_inode": run_index * 10 + sequence + 100,
                    "sha256": _digest(marker),
                }
            phases.append(
                _phase(
                    transaction_id=transaction,
                    sequence=sequence,
                    name=name,
                    previous_entry_sha256=(
                        phases[-1]["entry_sha256"] if phases else "0" * 64
                    ),
                    marker=marker,
                    owners=[owner_release["release_id"]] if owner_release else [],
                    process_receipt=receipt,
                    marker_publication=publication,
                )
            )
        snapshot = _sentinel()
        runs.append(
            {
                "run_id": f"run-{run_index}",
                "transaction_id": transaction,
                "started_at": phases[0]["at"],
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "phases": phases,
                "maximum_simultaneous_owners": 1,
                "owner_overlap_detected": False,
                "candidate_restart_verified": True,
                "cold_rollback_verified": True,
                "rollback_rto_seconds": 0.0,
                "sentinel_before": snapshot,
                "sentinel_after": deepcopy(snapshot),
                "lost_committed_jobs": 0,
                "duplicate_committed_jobs": 0,
                "final_isolated_owner_count": 0,
            }
        )
    active_marker_sha = hashlib.sha256(b"production-marker").hexdigest()
    report: dict[str, object] = {
        "schema": SCHEMA,
        "status": "passed",
        "report_kind": "v3_to_v3_atomic_rotation_and_cold_rollback",
        **CONTEXT,
        "started_at": report_started.isoformat(),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "required_runs": 3,
        "previous_release": previous,
        "candidate_release": candidate,
        "previous_deploy_marker_sha256": "1" * 64,
        "previous_deploy_manifest_sha256": "2" * 64,
        "candidate_deploy_marker_sha256": "3" * 64,
        "candidate_deploy_manifest_sha256": "4" * 64,
        "production_active_marker_sha256_before": active_marker_sha,
        "production_active_marker_sha256_after": active_marker_sha,
        "runs": runs,
        "safety": {
            "production_active_marker_mutated": False,
            "production_service_started": False,
            "production_port_accessed": False,
            "launchctl_invoked": False,
            "network_accessed": False,
            "live_business_state_accessed": False,
            "isolated_release_bound_processes_started": True,
            "isolated_state_root_sha256": hashlib.sha256(b"state-root").hexdigest(),
        },
    }
    report["evidence_sha256"] = _digest(report)
    return report


def _write_report(path: Path, report: dict[str, object]) -> None:
    path.write_text(json.dumps(report, sort_keys=True), encoding="utf-8")


def test_v3_rotation_report_derives_atomic_restart_and_rollback_metrics(
    tmp_path: Path,
) -> None:
    path = tmp_path / "rotation.json"
    _write_report(path, _report())

    metrics = derive_v3_rotation_metrics(
        path, expected_context=CONTEXT, gate_config=CONFIG
    )

    assert metrics == {
        "controlled_cold_restart_verified": True,
        "atomic_switch_verified": True,
        "cold_rollback_verified": True,
        "rollback_rto_seconds": 0.0,
        "lost_committed_jobs": 0,
        "duplicate_committed_jobs": 0,
    }


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda report: report["runs"][0].__setitem__(
                "maximum_simultaneous_owners", 2
            ),
            "identity or outcome",
        ),
        (
            lambda report: report["runs"][0]["phases"][2]["state"][
                "marker_publication"
            ].__setitem__("after_inode", report["runs"][0]["phases"][2]["state"]["marker_publication"]["before_inode"]),
            "phase chain/state|atomic marker publication",
        ),
        (
            lambda report: report["runs"][0]["sentinel_after"].__setitem__(
                "committed_id_hashes", []
            ),
            "durable sentinel drifted",
        ),
    ],
)
def test_v3_rotation_report_fails_closed_on_semantic_tamper(
    tmp_path: Path, mutate, message: str
) -> None:
    report = _report()
    mutate(report)
    report["evidence_sha256"] = _digest(
        {key: value for key, value in report.items() if key != "evidence_sha256"}
    )
    path = tmp_path / "rotation.json"
    _write_report(path, report)

    with pytest.raises(V3RotationDrillBlocked, match=message):
        derive_v3_rotation_metrics(path, expected_context=CONTEXT, gate_config=CONFIG)


def test_v3_only_release_gate_accepts_only_the_single_rotation_report_role(
    tmp_path: Path,
) -> None:
    report = _report()
    raw = json.dumps(report, sort_keys=True).encode()
    artifact = BoundArtifact(
        role="upstream_v3_rotation_drill_report",
        media_type="application/json",
        path="sources/v3-rotation-report.json",
        sha256=hashlib.sha256(raw).hexdigest(),
        data=raw,
    )

    metrics = _authoritative_normalized_metrics(
        "atomic_release_switch_and_cold_rollback_drill_passed",
        [artifact],
        CONTEXT,
        CONFIG,
        {},
    )
    assert metrics["atomic_switch_verified"] is True

    legacy = BoundArtifact(
        role="upstream_controlled_restart_plan",
        media_type="application/json",
        path="sources/legacy.json",
        sha256=hashlib.sha256(b"{}").hexdigest(),
        data=b"{}",
    )
    with pytest.raises(ValueError, match="V3-only rotation.*not exact"):
        _authoritative_normalized_metrics(
            "atomic_release_switch_and_cold_rollback_drill_passed",
            [legacy],
            CONTEXT,
            CONFIG,
            {},
        )


def test_rotation_control_paths_cannot_overlap_production_runtime(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "production-runtime"
    runtime.mkdir()
    marker = runtime / "active-release.json"
    marker.write_text("{}", encoding="utf-8")
    protected = tmp_path / "deploy-manifest.json"
    protected.write_text("{}", encoding="utf-8")

    _validate_control_paths(
        report_output=tmp_path / "report.json",
        state_root=tmp_path / "isolated-state",
        production_active_marker=marker,
        protected_inputs=(protected,),
    )

    with pytest.raises(V3RotationDrillBlocked, match="overlaps the production runtime"):
        _validate_control_paths(
            report_output=tmp_path / "report.json",
            state_root=runtime / "drill-state",
            production_active_marker=marker,
            protected_inputs=(protected,),
        )
    with pytest.raises(V3RotationDrillBlocked, match="report output path is unsafe"):
        _validate_control_paths(
            report_output=runtime / "report.json",
            state_root=tmp_path / "isolated-state",
            production_active_marker=marker,
            protected_inputs=(protected,),
        )
