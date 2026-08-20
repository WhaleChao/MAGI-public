from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.v3_cutover.activation import ActivationTransaction, active_release_marker
from scripts.v3_cutover.core import CutoverError
from magi_v3.service_runtime import (
    DefaultOwnershipProbe,
    ProcessRecord,
    ServiceRuntimeError,
)


def _begin(tmp_path: Path) -> ActivationTransaction:
    return ActivationTransaction.begin(
        state_parent=tmp_path.resolve(),
        plan_sha256="1" * 64,
        release_id="v3-test",
        release_root=(tmp_path / "release").resolve(),
        release_manifest_sha256="2" * 64,
        reconciliation_before={"committed_ids_sha256": "3" * 64, "count": 2},
        clock=lambda: "2026-07-17T02:00:00+08:00",
    )


def test_activation_commit_is_atomic_phase_bound_and_resumable(tmp_path: Path) -> None:
    transaction = _begin(tmp_path)
    transaction.advance("v2_zero")
    transaction.advance("v3_files_installed")
    receipt = transaction.commit_release(
        release="v3",
        release_id="v3-test",
        release_root=(tmp_path / "release").resolve(),
        release_manifest_sha256="2" * 64,
    )
    transaction.advance("v3_active")

    marker = active_release_marker(
        transaction.marker_path,
        expected_release="v3",
        expected_release_id="v3-test",
        expected_release_root=(tmp_path / "release").resolve(),
        expected_manifest_sha256="2" * 64,
    )
    assert receipt["active_release_marker_sha256"]
    assert marker["transaction_id"] == transaction.transaction_id
    assert ActivationTransaction.resume(state_parent=tmp_path.resolve()).document()["phase"] == "v3_active"


def test_marker_mismatch_interrupted_journal_and_invalid_transition_fail_closed(
    tmp_path: Path,
) -> None:
    transaction = _begin(tmp_path)
    with pytest.raises(CutoverError, match="incomplete"):
        _begin(tmp_path)
    with pytest.raises(CutoverError, match="invalid activation phase"):
        transaction.advance("v3_committed")
    transaction.advance("v2_zero")
    transaction.advance("v3_files_installed")
    transaction.commit_release(
        release="v3",
        release_id="v3-test",
        release_root=(tmp_path / "release").resolve(),
        release_manifest_sha256="2" * 64,
    )
    payload = json.loads(transaction.marker_path.read_text(encoding="utf-8"))
    payload["release_id"] = "other-release"
    transaction.marker_path.write_text(json.dumps(payload), encoding="utf-8")
    transaction.marker_path.chmod(0o600)
    with pytest.raises(CutoverError, match="release_id mismatch"):
        active_release_marker(
            transaction.marker_path,
            expected_release="v3",
            expected_release_id="v3-test",
        )


def test_production_ownership_refuses_ready_or_write_before_exact_commit_marker(
    tmp_path: Path,
) -> None:
    release = (tmp_path / "release").resolve()
    release.mkdir()
    manifest = release / "release-manifest.json"
    manifest.write_text(
        json.dumps({"schema_version": 1, "immutable": True, "release_id": "v3-test", "files": []}),
        encoding="utf-8",
    )
    marker = (tmp_path / "active-release.json").resolve()
    probe = DefaultOwnershipProbe(
        process_reader=lambda: (ProcessRecord(1, 1, "/sbin/launchd"),),
        listener_reader=lambda _port: frozenset(),
        v2_launchagent_loaded=lambda: False,
        active_release_marker=marker,
        require_active_release_marker=True,
    )
    with pytest.raises(ServiceRuntimeError, match="marker is unavailable"):
        probe.assert_exclusive(release)

    transaction = ActivationTransaction.begin(
        state_parent=tmp_path.resolve(),
        plan_sha256="1" * 64,
        release_id="v3-test",
        release_root=release,
        release_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
        reconciliation_before={"count": 0},
    )
    transaction.advance("v2_zero")
    transaction.advance("v3_files_installed")
    transaction.commit_release(
        release="v3",
        release_id="v3-test",
        release_root=release,
        release_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest(),
    )
    probe.assert_exclusive(release)

    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["release"] = "v2"
    marker.write_text(json.dumps(payload), encoding="utf-8")
    marker.chmod(0o600)
    with pytest.raises(ServiceRuntimeError, match="does not commit"):
        probe.assert_exclusive(release)
