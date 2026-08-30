from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.v3_cutover.activation import ActivationTransaction
from scripts.v3_cutover.core import CutoverError
from scripts.v3_cutover.v3_rotation_execute import (
    LABEL_BY_ROLE,
    ROLE_ORDER,
    STOP_ORDER,
    V3RotationExecutor,
    load_bound_deployment,
    recover_previous_from_snapshot,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, raw: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)


def _release(root: Path, release_id: str) -> tuple[Path, str]:
    members = {
        "bin/magi-v3-python": b"#!/bin/sh\nexit 0\n",
        "magi_v3/release_identity.py": f"RELEASE_ID = {release_id!r}\n".encode(),
    }
    rows = []
    for relative, raw in sorted(members.items()):
        mode = 0o555 if relative.startswith("bin/") else 0o444
        path = root / relative
        _write(path, raw, mode)
        rows.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "mode": f"0{mode:03o}",
            }
        )
    release_sha = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    manifest = {
        "schema_version": 1,
        "immutable": True,
        "release_id": release_id,
        "commit": "a" * 40,
        "source_snapshot_sha256": release_sha,
        "release_sha256": release_sha,
        "files": rows,
    }
    raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path = root / "release-manifest.json"
    _write(path, raw, 0o444)
    return path, hashlib.sha256(raw).hexdigest()


def _deployment(
    root: Path,
    *,
    release_manifest: Path,
    release_manifest_sha256: str,
    release_id: str,
    ownership_target: Path,
    variant: str,
) -> None:
    ownership_raw = (json.dumps({"release_id": release_id, "variant": variant}) + "\n").encode()
    ownership_source = root / "ownership/ownership-manifest.json"
    _write(ownership_source, ownership_raw, 0o444)
    artifacts = [
        {
            "path": "ownership/ownership-manifest.json",
            "sha256": hashlib.sha256(ownership_raw).hexdigest(),
            "size": len(ownership_raw),
        }
    ]
    roles = []
    for index, role in enumerate(ROLE_ORDER):
        label = LABEL_BY_ROLE[role]
        arguments = [
            str(release_manifest.parent / "bin/magi-v3-python"),
            "-c",
            f"print({role!r})",
            f"owner-{release_id}",
        ]
        plist = {
            "Label": label,
            "ProgramArguments": arguments,
            "WorkingDirectory": str(release_manifest.parent),
        }
        raw = plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=True)
        source = root / f"launchagents/{label}.plist"
        _write(source, raw, 0o444)
        artifacts.append(
            {
                "path": f"launchagents/{label}.plist",
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
            }
        )
        roles.append(
            {
                "role": role,
                "label": label,
                "ProgramArguments": arguments,
                "WorkingDirectory": str(release_manifest.parent),
                "ports": [5002 + index] if role != "supervisor" else [],
            }
        )
    manifest = {
        "artifacts": artifacts,
        "deployment_mode": "production",
        "generated_at": "2026-08-30T00:00:00+00:00",
        "mutation_performed": False,
        "ownership_manifest": str(ownership_target),
        "ownership_manifest_sha256": hashlib.sha256(ownership_raw).hexdigest(),
        "release_id": release_id,
        "release_manifest": str(release_manifest),
        "release_manifest_sha256": release_manifest_sha256,
        "roles": roles,
    }
    manifest_raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    _write(root / "deploy-manifest.json", manifest_raw, 0o444)
    marker = {
        "deployment_mode": "production",
        "manifest": "deploy-manifest.json",
        "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "mutation_performed": False,
        "ownership_manifest_sha256": hashlib.sha256(ownership_raw).hexdigest(),
        "ready_to_install": True,
        "release_id": release_id,
        "release_manifest_sha256": release_manifest_sha256,
        "schema_version": 1,
        "status": "prepared_not_installed",
    }
    _write(
        root / "DEPLOY_PREPARED.json",
        (json.dumps(marker, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        0o444,
    )


class FakeMachine:
    def __init__(self, release_by_root: dict[str, str], initial_release: str) -> None:
        self.release_by_root = release_by_root
        self.loaded = {role: initial_release for role in ROLE_ORDER}
        self.commands: list[tuple[str, ...]] = []
        self.maximum_owner_count = len(self.loaded)

    def run(self, argv) -> SimpleNamespace:
        command = tuple(argv)
        self.commands.append(command)
        if len(command) >= 3 and command[1] == "bootout":
            label = command[2].rsplit("/", 1)[-1]
            role = next(role for role, value in LABEL_BY_ROLE.items() if value == label)
            self.loaded.pop(role, None)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if len(command) >= 4 and command[1] == "bootstrap":
            plist = plistlib.loads(Path(command[3]).read_bytes())
            role = next(
                role for role, value in LABEL_BY_ROLE.items() if value == plist["Label"]
            )
            self.loaded[role] = self.release_by_root[plist["WorkingDirectory"]]
            self.maximum_owner_count = max(self.maximum_owner_count, len(self.loaded))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="missing")

    def observe(self, expected) -> dict:
        expected_id = "zero" if expected is None else expected.release_id
        ok = not self.loaded if expected is None else (
            set(self.loaded) == set(ROLE_ORDER)
            and set(self.loaded.values()) == {expected.release_id}
        )
        return {
            "ok": ok,
            "expected": expected_id,
            "owner_count": len(self.loaded),
            "roles": dict(self.loaded),
        }

    def observe_roles(self, expected, roles) -> dict:
        selected = tuple(roles)
        ok = all(self.loaded.get(role) == expected.release_id for role in selected)
        return {
            "ok": ok,
            "expected": expected.release_id,
            "owner_count": len(selected) if ok else 0,
            "roles": list(selected),
        }


@pytest.fixture
def rotation_fixture(tmp_path: Path):
    previous_release = tmp_path / "releases/v3-r59"
    candidate_release = tmp_path / "releases/v3-r67"
    previous_manifest, previous_sha = _release(previous_release, "v3-r59")
    candidate_manifest, candidate_sha = _release(candidate_release, "v3-r67")
    ownership_target = tmp_path / "runtime/MAGI_v3/ownership/ownership-manifest.json"
    previous_deploy = tmp_path / "deploy/r59-current"
    candidate_deploy = tmp_path / "deploy/r67-candidate"
    rollback_deploy = tmp_path / "deploy/r59-rollback-r2"
    _deployment(
        previous_deploy,
        release_manifest=previous_manifest,
        release_manifest_sha256=previous_sha,
        release_id="v3-r59",
        ownership_target=ownership_target,
        variant="current",
    )
    _deployment(
        candidate_deploy,
        release_manifest=candidate_manifest,
        release_manifest_sha256=candidate_sha,
        release_id="v3-r67",
        ownership_target=ownership_target,
        variant="candidate",
    )
    _deployment(
        rollback_deploy,
        release_manifest=previous_manifest,
        release_manifest_sha256=previous_sha,
        release_id="v3-r59",
        ownership_target=ownership_target,
        variant="rollback-r2",
    )
    launchagents = tmp_path / "Library/LaunchAgents"
    launchagents.mkdir(parents=True)
    previous = load_bound_deployment(previous_deploy)
    _write(ownership_target, previous.ownership_source.path.read_bytes(), 0o600)
    for role in ROLE_ORDER:
        _write(
            launchagents / f"{LABEL_BY_ROLE[role]}.plist",
            previous.plists[role].path.read_bytes(),
            0o644,
        )
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    activation = ActivationTransaction.begin(
        state_parent=state.resolve(),
        plan_sha256="1" * 64,
        release_id="v3-r59",
        release_root=previous_release.resolve(),
        release_manifest_sha256=previous_sha,
        reconciliation_before={"owner": "v2"},
        clock=lambda: "2026-08-29T00:00:00+00:00",
    )
    activation.advance("v2_zero")
    activation.advance("v3_files_installed")
    activation.commit_release(
        release="v3",
        release_id="v3-r59",
        release_root=previous_release.resolve(),
        release_manifest_sha256=previous_sha,
    )
    activation.advance("v3_active")
    gate = tmp_path / "gate.json"
    _write(gate, b"{}\n", 0o600)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    return {
        "previous_release": previous_release,
        "candidate_release": candidate_release,
        "previous_deploy": previous_deploy,
        "candidate_deploy": candidate_deploy,
        "rollback_deploy": rollback_deploy,
        "ownership_target": ownership_target,
        "launchagents": launchagents,
        "state": state,
        "gate": gate,
        "evidence": evidence,
    }


def _executor(fixture, machine: FakeMachine, readiness) -> V3RotationExecutor:
    executor = V3RotationExecutor(
        previous_deploy_root=fixture["previous_deploy"],
        candidate_deploy_root=fixture["candidate_deploy"],
        rollback_deploy_root=fixture["rollback_deploy"],
        state_parent=fixture["state"],
        launchagents_directory=fixture["launchagents"],
        evidence_dir=fixture["evidence"],
        gate_config=fixture["gate"],
        campaign_id="campaign-test",
        hardware_id="hardware-test",
        gate_config_sha256=_sha(fixture["gate"]),
        rollback_snapshot_directory=fixture["state"].parent / "rollback-snapshot",
        report_output=fixture["state"].parent / "rotation-report.json",
        runner=machine.run,
        observer=machine.observe,
        role_observer=machine.observe_roles,
        readiness_probe=readiness,
        sleeper=lambda _seconds: None,
        uid=501,
    )
    executor._verify_gate = lambda candidate: {  # type: ignore[method-assign]
        "decision": "GO",
        "release_sha": candidate.release_sha,
    }
    return executor


def test_v3_rotation_activates_candidate_without_mutating_r59_release(
    rotation_fixture,
) -> None:
    fixture = rotation_fixture
    release_by_root = {
        str(fixture["previous_release"]): "v3-r59",
        str(fixture["candidate_release"]): "v3-r67",
    }
    machine = FakeMachine(release_by_root, "v3-r59")
    predecessor_hashes = {
        path.relative_to(fixture["previous_release"]).as_posix(): _sha(path)
        for path in fixture["previous_release"].rglob("*")
        if path.is_file()
    }
    original_marker = (fixture["state"] / "active-release.json").read_bytes()
    report = _executor(
        fixture, machine, lambda _urls: (True, {"all": "ready"})
    ).execute()

    assert report["status"] == "candidate_active"
    assert report["ok"] is True
    marker = json.loads((fixture["state"] / "active-release.json").read_text())
    journal = json.loads((fixture["state"] / "cutover-activation.json").read_text())
    assert marker["release_id"] == "v3-r67"
    assert journal["phase"] == "candidate_active"
    assert journal["schema"] == "magi.v3.activation-transaction/v2"
    snapshot = fixture["state"].parent / "rollback-snapshot"
    assert (snapshot / "active-release.json").read_bytes() == original_marker
    candidate = load_bound_deployment(fixture["candidate_deploy"])
    assert _sha(fixture["ownership_target"]) == candidate.ownership_source.sha256
    assert machine.maximum_owner_count == 3
    assert [command[2].rsplit("/", 1)[-1] for command in machine.commands[:3]] == [
        LABEL_BY_ROLE[role] for role in STOP_ORDER
    ]
    assert predecessor_hashes == {
        path.relative_to(fixture["previous_release"]).as_posix(): _sha(path)
        for path in fixture["previous_release"].rglob("*")
        if path.is_file()
    }


def test_v3_rotation_waits_for_delayed_launchd_teardown_and_bootstrap(
    rotation_fixture,
) -> None:
    fixture = rotation_fixture
    machine = FakeMachine(
        {
            str(fixture["previous_release"]): "v3-r59",
            str(fixture["candidate_release"]): "v3-r67",
        },
        "v3-r59",
    )
    executor = _executor(fixture, machine, lambda _urls: (True, {"ready": True}))
    calls = {"zero": 0, "v3-r67": 0}

    def delayed_observer(expected):
        expected_id = "zero" if expected is None else expected.release_id
        if expected_id in calls:
            calls[expected_id] += 1
            if calls[expected_id] == 1:
                return {
                    "ok": False,
                    "expected": expected_id,
                    "owner_count": 1 if expected is None else 2,
                    "reason": "launchd_transition_in_progress",
                }
        return machine.observe(expected)

    executor.observer = delayed_observer
    report = executor.execute()

    assert report["status"] == "candidate_active"
    assert calls["zero"] >= 3
    assert calls["v3-r67"] >= 3
    ownership_events = [
        event for event in report["events"] if event["action"] == "ownership"
    ]
    assert any(
        event["expected"] == "zero" and event["attempts"] >= 3
        for event in ownership_events
    )
    assert any(
        event["expected"] == "v3-r67" and event["attempts"] >= 3
        for event in ownership_events
    )


def test_v3_rotation_stabilizes_control_before_bootstrapping_dependents(
    rotation_fixture,
) -> None:
    fixture = rotation_fixture

    class StartupRaceMachine(FakeMachine):
        def __init__(self, release_by_root, initial_release):
            super().__init__(release_by_root, initial_release)
            self.control_stable = False

        def run(self, argv):
            command = tuple(argv)
            if len(command) >= 4 and command[1] == "bootstrap":
                plist = plistlib.loads(Path(command[3]).read_bytes())
                role = next(
                    role
                    for role, value in LABEL_BY_ROLE.items()
                    if value == plist["Label"]
                )
                if role != "control" and not self.control_stable:
                    raise AssertionError("dependent role started before control stabilized")
            return super().run(argv)

        def observe_roles(self, expected, roles):
            result = super().observe_roles(expected, roles)
            if tuple(roles) == ("control",) and result["ok"]:
                self.control_stable = True
            return result

    machine = StartupRaceMachine(
        {
            str(fixture["previous_release"]): "v3-r59",
            str(fixture["candidate_release"]): "v3-r67",
        },
        "v3-r59",
    )
    report = _executor(fixture, machine, lambda _urls: (True, {"ready": True})).execute()

    assert report["status"] == "candidate_active"
    startup_events = [event for event in report["events"] if event["action"] == "startup_stage"]
    assert [event["roles"] for event in startup_events] == [["control"]]
    bootstraps = [command for command in machine.commands if command[1] == "bootstrap"]
    assert [
        plistlib.loads(Path(command[3]).read_bytes())["Label"] for command in bootstraps
    ] == [LABEL_BY_ROLE[role] for role in ROLE_ORDER]


def test_ownership_observation_retries_lsof_timeout(rotation_fixture) -> None:
    fixture = rotation_fixture
    machine = FakeMachine(
        {
            str(fixture["previous_release"]): "v3-r59",
            str(fixture["candidate_release"]): "v3-r67",
        },
        "v3-r59",
    )
    executor = _executor(fixture, machine, lambda _urls: (True, {}))
    previous = load_bound_deployment(fixture["previous_deploy"])
    calls = 0

    def timeout_once(expected):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise subprocess.TimeoutExpired(("/usr/sbin/lsof", "-nP"), 3)
        return machine.observe(expected)

    executor.observer = timeout_once
    observation = executor._require_observation(previous)

    assert observation["ok"] is True
    assert calls == 3
    assert executor.events[-1]["attempts"] == 3


def test_ownership_observation_deadline_remains_fail_closed(rotation_fixture) -> None:
    fixture = rotation_fixture
    machine = FakeMachine(
        {
            str(fixture["previous_release"]): "v3-r59",
            str(fixture["candidate_release"]): "v3-r67",
        },
        "v3-r59",
    )
    executor = _executor(fixture, machine, lambda _urls: (True, {}))
    previous = load_bound_deployment(fixture["previous_deploy"])
    elapsed = 0.0

    def monotonic() -> float:
        return elapsed

    def sleep(seconds: float) -> None:
        nonlocal elapsed
        elapsed += seconds

    executor.monotonic = monotonic
    executor.sleeper = sleep
    executor.ownership_observation_timeout_seconds = 0.5
    executor.ownership_observation_interval_seconds = 0.25
    executor.observer = lambda expected: {
        "ok": False,
        "expected": expected.release_id,
        "owner_count": 2,
        "reason": "transition_never_completed",
    }

    with pytest.raises(CutoverError, match="ownership proof timed out"):
        executor._require_observation(previous)

    assert elapsed == 0.5
    assert executor.events[-1]["action"] == "ownership_timeout"
    assert executor.events[-1]["last_observation"]["owner_count"] == 2


def test_v3_rotation_candidate_failure_restores_exact_state_and_r59_rollback_r2(
    rotation_fixture,
) -> None:
    fixture = rotation_fixture
    machine = FakeMachine(
        {
            str(fixture["previous_release"]): "v3-r59",
            str(fixture["candidate_release"]): "v3-r67",
        },
        "v3-r59",
    )
    readiness_results = iter(
        [(False, {"candidate": "failed"}), (True, {"rollback": "ready"})]
    )
    marker_before = (fixture["state"] / "active-release.json").read_bytes()
    journal_before = (fixture["state"] / "cutover-activation.json").read_bytes()
    report = _executor(
        fixture, machine, lambda _urls: next(readiness_results)
    ).execute()

    assert report["status"] == "rolled_back_to_previous"
    assert report["ok"] is False
    assert report["rollback_performed"] is True
    assert (fixture["state"] / "active-release.json").read_bytes() == marker_before
    assert (fixture["state"] / "cutover-activation.json").read_bytes() == journal_before
    rollback = load_bound_deployment(fixture["rollback_deploy"])
    assert _sha(fixture["ownership_target"]) == rollback.ownership_source.sha256
    for role in ROLE_ORDER:
        assert _sha(
            fixture["launchagents"] / f"{LABEL_BY_ROLE[role]}.plist"
        ) == rollback.plists[role].sha256
    bootstraps = [command for command in machine.commands if command[1] == "bootstrap"]
    assert len(bootstraps) == 6
    assert machine.loaded == {role: "v3-r59" for role in ROLE_ORDER}
    assert machine.maximum_owner_count == 3


def test_v3_rotation_pre_gate_failure_never_stops_r59_or_creates_snapshot(
    rotation_fixture,
) -> None:
    fixture = rotation_fixture
    machine = FakeMachine(
        {
            str(fixture["previous_release"]): "v3-r59",
            str(fixture["candidate_release"]): "v3-r67",
        },
        "v3-r59",
    )
    executor = _executor(fixture, machine, lambda _urls: (True, {}))

    def blocked(_candidate):
        raise CutoverError("NO_GO")

    executor._verify_gate = blocked  # type: ignore[method-assign]
    with pytest.raises(CutoverError, match="NO_GO"):
        executor.execute()
    assert machine.commands == []
    assert not (fixture["state"].parent / "rollback-snapshot").exists()
    assert machine.loaded == {role: "v3-r59" for role in ROLE_ORDER}


def test_bound_deployment_rejects_release_member_drift(rotation_fixture) -> None:
    member = rotation_fixture["candidate_release"] / "magi_v3/release_identity.py"
    member.chmod(0o644)
    member.write_text("drifted\n", encoding="utf-8")
    member.chmod(0o444)
    with pytest.raises(CutoverError, match="release member drifted"):
        load_bound_deployment(rotation_fixture["candidate_deploy"])


def test_explicit_recovery_uses_hash_bound_snapshot_after_uncatchable_interrupt(
    rotation_fixture,
) -> None:
    fixture = rotation_fixture
    machine = FakeMachine(
        {
            str(fixture["previous_release"]): "v3-r59",
            str(fixture["candidate_release"]): "v3-r67",
        },
        "v3-r59",
    )
    executor = _executor(fixture, machine, lambda _urls: (True, {"ready": True}))
    assert executor.execute()["status"] == "candidate_active"
    snapshot = fixture["state"].parent / "rollback-snapshot"
    snapshot_sha = _sha(snapshot / "snapshot-manifest.json")

    report = recover_previous_from_snapshot(
        rollback_deploy_root=fixture["rollback_deploy"],
        state_parent=fixture["state"],
        launchagents_directory=fixture["launchagents"],
        rollback_snapshot_directory=snapshot,
        expected_snapshot_manifest_sha256=snapshot_sha,
        report_output=fixture["state"].parent / "recovery-report.json",
        runner=machine.run,
        observer=machine.observe,
        role_observer=machine.observe_roles,
        readiness_probe=lambda _urls: (True, {"rollback": "ready"}),
        uid=501,
    )

    assert report["status"] == "previous_recovered"
    assert report["release_id"] == "v3-r59"
    marker = json.loads((fixture["state"] / "active-release.json").read_text())
    assert marker["release_id"] == "v3-r59"
    assert machine.loaded == {role: "v3-r59" for role in ROLE_ORDER}


def test_explicit_recovery_rejects_wrong_snapshot_sha_without_stopping_owner(
    rotation_fixture,
) -> None:
    fixture = rotation_fixture
    machine = FakeMachine(
        {
            str(fixture["previous_release"]): "v3-r59",
            str(fixture["candidate_release"]): "v3-r67",
        },
        "v3-r59",
    )
    executor = _executor(fixture, machine, lambda _urls: (True, {}))
    executor.execute()
    commands_before = list(machine.commands)

    with pytest.raises(CutoverError, match="manifest drifted"):
        recover_previous_from_snapshot(
            rollback_deploy_root=fixture["rollback_deploy"],
            state_parent=fixture["state"],
            launchagents_directory=fixture["launchagents"],
            rollback_snapshot_directory=fixture["state"].parent / "rollback-snapshot",
            expected_snapshot_manifest_sha256="0" * 64,
            report_output=fixture["state"].parent / "recovery-report.json",
            runner=machine.run,
            observer=machine.observe,
            readiness_probe=lambda _urls: (True, {}),
            uid=501,
        )
    assert machine.commands == commands_before
    assert machine.loaded == {role: "v3-r67" for role in ROLE_ORDER}
