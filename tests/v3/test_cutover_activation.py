from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

import scripts.v3_cutover.activation as activation_module
from scripts.v3_cutover.activation import (
    ACTIVE_V3_RESTART_PHASES,
    ActivationTransaction,
    ACTIVE_RELEASE_ADMISSION_LOCK,
    V3RotationTransaction,
    acquire_active_release_admission,
    active_release_marker,
    load_verified_active_release_deployment,
    verify_active_release_deployment,
    verify_active_release_snapshot,
)
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


def test_active_release_snapshot_binds_stable_identity_and_active_phase(
    tmp_path: Path,
) -> None:
    transaction = _begin(tmp_path)
    transaction.advance("v2_zero")
    transaction.advance("v3_files_installed")
    transaction.commit_release(
        release="v3",
        release_id="v3-test",
        release_root=(tmp_path / "release").resolve(),
        release_manifest_sha256="2" * 64,
    )
    marker = json.loads(transaction.marker_path.read_text(encoding="utf-8"))

    verified = verify_active_release_snapshot(
        marker,
        transaction.document(),
        expected_release="v3",
        allowed_phases=ACTIVE_V3_RESTART_PHASES,
    )

    assert verified["phase"] == "v3_committed"
    assert verified["transaction_id"] == transaction.transaction_id
    assert verified["release_root_sha256"] != str(tmp_path / "release")
    assert len(verified["active_release_identity_sha256"]) == 64
    assert verified["pii_included"] is False

    transaction.advance("v3_active")
    active = verify_active_release_snapshot(
        json.loads(transaction.marker_path.read_text(encoding="utf-8")),
        transaction.document(),
        expected_release="v3",
        allowed_phases=ACTIVE_V3_RESTART_PHASES,
    )
    assert active["active_release_identity_sha256"] == verified[
        "active_release_identity_sha256"
    ]
    assert active["phase"] == "v3_active"


def _begin_v3_rotation(tmp_path: Path) -> V3RotationTransaction:
    previous_root = tmp_path / "previous-release"
    candidate_root = tmp_path / "candidate-release"
    previous_root.mkdir()
    candidate_root.mkdir()
    previous = ActivationTransaction.begin(
        state_parent=tmp_path.resolve(),
        plan_sha256="1" * 64,
        release_id="v3-previous",
        release_root=previous_root.resolve(),
        release_manifest_sha256="2" * 64,
        reconciliation_before={"count": 0},
        clock=lambda: "2026-08-29T00:00:00+00:00",
    )
    previous.advance("v2_zero")
    previous.advance("v3_files_installed")
    previous.commit_release(
        release="v3",
        release_id="v3-previous",
        release_root=previous_root.resolve(),
        release_manifest_sha256="2" * 64,
    )
    previous.advance("v3_active")
    marker_sha256 = hashlib.sha256(previous.marker_path.read_bytes()).hexdigest()
    journal_sha256 = hashlib.sha256(previous.journal_path.read_bytes()).hexdigest()
    return V3RotationTransaction.begin(
        state_parent=tmp_path.resolve(),
        plan_sha256="3" * 64,
        previous_marker_sha256=marker_sha256,
        previous_journal_sha256=journal_sha256,
        previous_release_id="v3-previous",
        previous_release_root=previous_root.resolve(),
        previous_release_manifest_sha256="2" * 64,
        candidate_release_id="v3-candidate",
        candidate_release_root=candidate_root.resolve(),
        candidate_release_manifest_sha256="4" * 64,
        candidate_deployment_manifest_sha256="5" * 64,
        rollback_deployment_manifest_sha256="6" * 64,
        reconciliation_before={"owner": "v3-previous", "count": 3},
        clock=lambda: "2026-08-30T00:00:00+00:00",
    )


def test_v3_rotation_commits_hash_bound_candidate_and_remains_restartable(
    tmp_path: Path,
) -> None:
    transaction = _begin_v3_rotation(tmp_path)
    with pytest.raises(CutoverError, match="invalid V3 rotation phase"):
        transaction.advance("candidate_files_installed")
    transaction.advance("previous_v3_zero", zero_owner=True)
    transaction.advance("candidate_files_installed", installed=True)
    receipt = transaction.commit_candidate()

    marker = json.loads(transaction.marker_path.read_text(encoding="utf-8"))
    committed = verify_active_release_snapshot(
        marker,
        transaction.document(),
        expected_release="v3",
        allowed_phases=ACTIVE_V3_RESTART_PHASES,
    )
    assert committed["release_id"] == "v3-candidate"
    assert committed["phase"] == "candidate_committed"
    assert receipt["active_release_marker_sha256"] == hashlib.sha256(
        activation_module._canonical_json(marker)
    ).hexdigest()

    transaction.mark_active(reconciliation_after={"owner": "v3-candidate"})
    active = verify_active_release_snapshot(
        json.loads(transaction.marker_path.read_text(encoding="utf-8")),
        transaction.document(),
        expected_release="v3",
        allowed_phases=ACTIVE_V3_RESTART_PHASES,
    )
    assert active["phase"] == "candidate_active"
    assert active["active_release_identity_sha256"] == committed[
        "active_release_identity_sha256"
    ]


@pytest.mark.parametrize("target", ["marker", "journal"])
def test_v3_rotation_refuses_previous_state_drift(
    tmp_path: Path,
    target: str,
) -> None:
    previous_root = tmp_path / "previous-release"
    candidate_root = tmp_path / "candidate-release"
    previous_root.mkdir()
    candidate_root.mkdir()
    previous = ActivationTransaction.begin(
        state_parent=tmp_path.resolve(),
        plan_sha256="1" * 64,
        release_id="v3-previous",
        release_root=previous_root.resolve(),
        release_manifest_sha256="2" * 64,
        reconciliation_before={"count": 0},
    )
    previous.advance("v2_zero")
    previous.advance("v3_files_installed")
    previous.commit_release(
        release="v3",
        release_id="v3-previous",
        release_root=previous_root.resolve(),
        release_manifest_sha256="2" * 64,
    )
    marker_sha256 = hashlib.sha256(previous.marker_path.read_bytes()).hexdigest()
    journal_sha256 = hashlib.sha256(previous.journal_path.read_bytes()).hexdigest()
    drift_path = previous.marker_path if target == "marker" else previous.journal_path
    drift_path.write_bytes(drift_path.read_bytes() + b" ")
    drift_path.chmod(0o600)

    with pytest.raises(CutoverError, match=f"previous {target} drifted"):
        V3RotationTransaction.begin(
            state_parent=tmp_path.resolve(),
            plan_sha256="3" * 64,
            previous_marker_sha256=marker_sha256,
            previous_journal_sha256=journal_sha256,
            previous_release_id="v3-previous",
            previous_release_root=previous_root.resolve(),
            previous_release_manifest_sha256="2" * 64,
            candidate_release_id="v3-candidate",
            candidate_release_root=candidate_root.resolve(),
            candidate_release_manifest_sha256="4" * 64,
            candidate_deployment_manifest_sha256="5" * 64,
            rollback_deployment_manifest_sha256="6" * 64,
            reconciliation_before={"count": 3},
        )


def test_v3_rotation_snapshot_rejects_commit_chain_rewrite(tmp_path: Path) -> None:
    transaction = _begin_v3_rotation(tmp_path)
    transaction.advance("previous_v3_zero")
    transaction.advance("candidate_files_installed")
    transaction.commit_candidate()
    marker = json.loads(transaction.marker_path.read_text(encoding="utf-8"))
    journal = transaction.document()
    journal["history"][-1]["evidence"]["active_release_marker_sha256"] = "0" * 64
    with pytest.raises(CutoverError, match="hash chain"):
        verify_active_release_snapshot(
            marker,
            journal,
            expected_release="v3",
            allowed_phases=ACTIVE_V3_RESTART_PHASES,
        )


def test_active_release_snapshot_revokes_on_rollback_before_service_stop(
    tmp_path: Path,
) -> None:
    transaction = _begin(tmp_path)
    transaction.advance("v2_zero")
    transaction.advance("v3_files_installed")
    transaction.commit_release(
        release="v3",
        release_id="v3-test",
        release_root=(tmp_path / "release").resolve(),
        release_manifest_sha256="2" * 64,
    )
    transaction.advance("v3_active")
    transaction.advance("rollback_started")
    with pytest.raises(CutoverError, match="allowed active release state"):
        verify_active_release_snapshot(
            json.loads(transaction.marker_path.read_text(encoding="utf-8")),
            transaction.document(),
            expected_release="v3",
            allowed_phases=ACTIVE_V3_RESTART_PHASES,
        )


@pytest.mark.parametrize("target", ["marker", "journal", "history"])
def test_active_release_snapshot_rejects_marker_journal_or_chain_drift(
    tmp_path: Path,
    target: str,
) -> None:
    transaction = _begin(tmp_path)
    transaction.advance("v2_zero")
    transaction.advance("v3_files_installed")
    transaction.commit_release(
        release="v3",
        release_id="v3-test",
        release_root=(tmp_path / "release").resolve(),
        release_manifest_sha256="2" * 64,
    )
    marker = json.loads(transaction.marker_path.read_text(encoding="utf-8"))
    journal = transaction.document()
    if target == "marker":
        marker["release_manifest_sha256"] = "0" * 64
    elif target == "journal":
        journal["release_id"] = "different-release"
    else:
        journal["history"][-1]["phase"] = "v3_active"
    with pytest.raises(CutoverError):
        verify_active_release_snapshot(
            marker,
            journal,
            expected_release="v3",
            allowed_phases=ACTIVE_V3_RESTART_PHASES,
        )


def test_active_release_snapshot_rejects_joint_top_level_identity_rewrite(
    tmp_path: Path,
) -> None:
    transaction = _begin(tmp_path)
    transaction.advance("v2_zero")
    transaction.advance("v3_files_installed")
    transaction.commit_release(
        release="v3",
        release_id="v3-test",
        release_root=(tmp_path / "release").resolve(),
        release_manifest_sha256="2" * 64,
    )
    transaction.advance("v3_active")
    marker = json.loads(transaction.marker_path.read_text(encoding="utf-8"))
    journal = transaction.document()

    # Rewriting both unsigned top-level views used to preserve their mutual
    # equality while leaving the append-only history untouched.  Authority
    # must instead come from the commit entry inside the validated hash chain.
    marker["release_id"] = "rewritten-release"
    journal["release_id"] = "rewritten-release"
    journal["active_release_marker"] = marker
    journal["active_release_marker_sha256"] = hashlib.sha256(
        activation_module._canonical_json(marker)
    ).hexdigest()

    with pytest.raises(CutoverError, match="committed active release"):
        verify_active_release_snapshot(
            marker,
            journal,
            expected_release="v3",
            allowed_phases=ACTIVE_V3_RESTART_PHASES,
        )


def test_active_release_snapshot_derives_v2_rollback_commit_from_chain(
    tmp_path: Path,
) -> None:
    transaction = _begin(tmp_path)
    transaction.advance("v2_zero")
    transaction.advance("v3_files_installed")
    transaction.commit_release(
        release="v3",
        release_id="v3-test",
        release_root=(tmp_path / "release").resolve(),
        release_manifest_sha256="2" * 64,
    )
    transaction.advance("v3_active")
    transaction.advance("rollback_started")
    transaction.advance("v3_zero")
    transaction.commit_release(
        release="v2",
        release_id="v2-rollback",
        release_root=(tmp_path / "v2-release").resolve(),
        release_manifest_sha256="4" * 64,
    )

    state = verify_active_release_snapshot(
        json.loads(transaction.marker_path.read_text(encoding="utf-8")),
        transaction.document(),
        expected_release="v2",
        allowed_phases=frozenset({"v2_committed"}),
    )

    assert state["release"] == "v2"
    assert state["release_id"] == "v2-rollback"
    assert state["release_manifest_sha256"] == "4" * 64


def test_active_release_snapshot_rejects_private_release_identifier(
    tmp_path: Path,
) -> None:
    transaction = ActivationTransaction.begin(
        state_parent=tmp_path.resolve(),
        plan_sha256="1" * 64,
        release_id="operator@example.com",
        release_root=(tmp_path / "release").resolve(),
        release_manifest_sha256="2" * 64,
        reconciliation_before={"count": 0},
        clock=lambda: "2026-07-17T02:00:00+08:00",
    )
    transaction.advance("v2_zero")
    transaction.advance("v3_files_installed")
    transaction.commit_release(
        release="v3",
        release_id="operator@example.com",
        release_root=(tmp_path / "release").resolve(),
        release_manifest_sha256="2" * 64,
    )

    with pytest.raises(CutoverError, match="marker identity"):
        verify_active_release_snapshot(
            json.loads(transaction.marker_path.read_text(encoding="utf-8")),
            transaction.document(),
            expected_release="v3",
            allowed_phases=ACTIVE_V3_RESTART_PHASES,
        )


def test_active_release_admission_lock_is_private_and_release_is_idempotent(
    tmp_path: Path,
) -> None:
    state_parent = tmp_path / "runtime"
    state_parent.mkdir(mode=0o700)
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        source = Path(activation_module.__file__).read_text(encoding="utf-8")
        assert "fcntl.flock" in source
        assert "stat.S_IMODE(before.st_mode) != 0o600" in source
        return

    admission = acquire_active_release_admission(state_parent)
    leaf = state_parent.parent / ACTIVE_RELEASE_ADMISSION_LOCK
    assert stat.S_IMODE(leaf.stat().st_mode) == 0o600
    assert leaf.stat().st_nlink == 1
    admission.release()
    admission.release()


def test_active_release_admission_busy_fails_without_blocking(
    tmp_path: Path,
) -> None:
    state_parent = tmp_path / "runtime"
    state_parent.mkdir(mode=0o700)
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        return
    admission = acquire_active_release_admission(state_parent)
    try:
        with pytest.raises(CutoverError, match="unavailable"):
            acquire_active_release_admission(state_parent)
    finally:
        admission.release()


def test_active_release_admission_rejects_nonprivate_parent(tmp_path: Path) -> None:
    state_parent = tmp_path / "runtime"
    state_parent.mkdir(mode=0o755)
    state_parent.chmod(0o755)
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        return
    with pytest.raises(CutoverError, match="not private"):
        acquire_active_release_admission(state_parent)


def test_active_release_admission_rejects_post_lock_leaf_mode_drift(
    monkeypatch, tmp_path: Path,
) -> None:
    state_parent = tmp_path / "runtime"
    state_parent.mkdir(mode=0o700)
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        return
    actual_stat = activation_module.os.stat
    leaf_calls = 0

    def drift_on_recheck(path, *args, **kwargs):
        nonlocal leaf_calls
        if path == ACTIVE_RELEASE_ADMISSION_LOCK:
            leaf_calls += 1
            if leaf_calls == 2:
                (state_parent.parent / ACTIVE_RELEASE_ADMISSION_LOCK).chmod(0o644)
        return actual_stat(path, *args, **kwargs)

    monkeypatch.setattr(activation_module.os, "stat", drift_on_recheck)
    with pytest.raises(CutoverError, match="identity drifted"):
        acquire_active_release_admission(state_parent)
    assert leaf_calls == 2


def _deployment_fixture(tmp_path: Path):
    state_parent = tmp_path / "deployment-state"
    state_parent.mkdir(mode=0o700)
    release_root = tmp_path / "deployment-release"
    release_root.mkdir(mode=0o755)
    source_commit = "a" * 40
    rows = [
        {
            "path": "magi_v3/federation/protocol.py",
            "sha256": hashlib.sha256(b"protocol-source").hexdigest(),
            "size": len(b"protocol-source"),
            "mode": "0444",
        },
        {
            "path": "scripts/melchior_federation/formal_gateway_entry.py",
            "sha256": hashlib.sha256(b"entry-source").hexdigest(),
            "size": len(b"entry-source"),
            "mode": "0444",
        },
    ]
    snapshot = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "immutable": True,
        "release_id": "v3-20260825-rc643-target",
        "commit": source_commit,
        "source_snapshot_sha256": snapshot,
        "release_sha256": snapshot,
        "files": rows,
    }
    raw = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    manifest_path = release_root / "release-manifest.json"
    manifest_path.write_bytes(raw)
    manifest_path.chmod(0o444)
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    transaction = ActivationTransaction.begin(
        state_parent=state_parent,
        plan_sha256=hashlib.sha256(b"deployment-plan").hexdigest(),
        release_id=manifest["release_id"],
        release_root=release_root.resolve(),
        release_manifest_sha256=manifest_sha256,
        reconciliation_before={"owner": "v2"},
        clock=lambda: "2026-08-25T12:00:00+00:00",
    )
    transaction.advance("v2_zero")
    transaction.advance("v3_files_installed")
    transaction.commit_release(
        release="v3",
        release_id=manifest["release_id"],
        release_root=release_root.resolve(),
        release_manifest_sha256=manifest_sha256,
    )
    marker = json.loads(transaction.marker_path.read_text(encoding="utf-8"))
    state = verify_active_release_snapshot(
        marker,
        transaction.document(),
        expected_release="v3",
        allowed_phases=ACTIVE_V3_RESTART_PHASES,
    )
    return marker, state, manifest, raw, source_commit, manifest_path


def _state_for_manifest(state: dict, raw: bytes) -> dict:
    changed = dict(state)
    changed["release_manifest_sha256"] = hashlib.sha256(raw).hexdigest()
    stable = {
        field: changed[field]
        for field in (
            "transaction_id", "release", "release_id",
            "release_root_sha256", "release_manifest_sha256", "plan_sha256",
        )
    }
    changed["active_release_identity_sha256"] = hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return changed


def test_active_release_deployment_binds_distinct_target_source_and_manifest(
    tmp_path: Path,
) -> None:
    marker, state, _manifest, raw, source_commit, _path = _deployment_fixture(
        tmp_path
    )
    verified = verify_active_release_deployment(
        state,
        raw,
        expected_source_git_commit=source_commit,
    )
    loaded = load_verified_active_release_deployment(
        marker,
        state,
        expected_source_git_commit=source_commit,
    )
    assert loaded == verified
    assert verified["release_id"] == "v3-20260825-rc643-target"
    assert verified["source_git_commit"] == source_commit
    assert verified["pii_included"] is False


def test_active_release_deployment_rejects_source_and_state_identity_drift(
    tmp_path: Path,
) -> None:
    _marker, state, _manifest, raw, source_commit, _path = _deployment_fixture(
        tmp_path
    )
    with pytest.raises(CutoverError, match="identity/source drift"):
        verify_active_release_deployment(
            state,
            raw,
            expected_source_git_commit="b" * 40,
        )
    invalid_state = dict(state)
    invalid_state["active_release_identity_sha256"] = "0" * 64
    with pytest.raises(CutoverError, match="state identity drift"):
        verify_active_release_deployment(
            invalid_state,
            raw,
            expected_source_git_commit=source_commit,
        )


@pytest.mark.parametrize("target", ["ordering", "snapshot"])
def test_active_release_deployment_rejects_manifest_inventory_drift(
    tmp_path: Path,
    target: str,
) -> None:
    _marker, state, manifest, _raw, source_commit, _path = _deployment_fixture(
        tmp_path
    )
    changed = json.loads(json.dumps(manifest))
    if target == "ordering":
        changed["files"] = list(reversed(changed["files"]))
    else:
        changed["source_snapshot_sha256"] = "0" * 64
        changed["release_sha256"] = "0" * 64
    raw = (
        json.dumps(changed, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    with pytest.raises(CutoverError, match="inventory|snapshot"):
        verify_active_release_deployment(
            _state_for_manifest(state, raw),
            raw,
            expected_source_git_commit=source_commit,
        )


def test_active_release_deployment_loader_rejects_symlink_manifest(
    tmp_path: Path,
) -> None:
    marker, state, _manifest, raw, source_commit, manifest_path = (
        _deployment_fixture(tmp_path)
    )
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        assert "O_NOFOLLOW" in Path(activation_module.__file__).read_text()
        return
    replacement = tmp_path / "replacement-release-manifest.json"
    replacement.write_bytes(raw)
    manifest_path.unlink()
    manifest_path.symlink_to(replacement)
    with pytest.raises(CutoverError, match="unsafe"):
        load_verified_active_release_deployment(
            marker,
            state,
            expected_source_git_commit=source_commit,
        )


def test_active_release_deployment_snapshots_state_before_marker_callback(
    tmp_path: Path,
) -> None:
    marker, state, _manifest, _raw, source_commit, _path = _deployment_fixture(
        tmp_path
    )
    trusted = dict(state)
    trusted["phase"] = "rollback_started"

    class MutatingMarker(dict):
        def items(self):
            trusted.clear()
            trusted.update(state)
            return super().items()

    with pytest.raises(CutoverError, match="state is invalid"):
        load_verified_active_release_deployment(
            MutatingMarker(marker),
            trusted,
            expected_source_git_commit=source_commit,
        )
    assert trusted["phase"] == "v3_committed"


def test_active_release_deployment_rejects_release_root_replacement(
    monkeypatch,
    tmp_path: Path,
) -> None:
    marker, state, _manifest, raw, source_commit, _path = _deployment_fixture(
        tmp_path
    )
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        return
    release_root = Path(marker["release_root"])
    displaced = tmp_path / "displaced-release"
    actual_open = activation_module.os.open
    root_opens = 0

    def replace_before_reopen(path, flags, *args, **kwargs):
        nonlocal root_opens
        if Path(path) == release_root and "dir_fd" not in kwargs:
            root_opens += 1
            if root_opens == 2:
                release_root.rename(displaced)
                release_root.mkdir(mode=0o755)
                replacement = release_root / "release-manifest.json"
                replacement.write_bytes(raw)
                replacement.chmod(0o444)
        return actual_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(activation_module.os, "open", replace_before_reopen)
    with pytest.raises(CutoverError, match="changed while being read"):
        load_verified_active_release_deployment(
            marker,
            state,
            expected_source_git_commit=source_commit,
        )
    assert root_opens == 2
