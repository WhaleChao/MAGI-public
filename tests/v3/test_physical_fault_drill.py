from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts.v3_validation import physical_fault_drill as drill


CTX = {
    "campaign_id": "campaign-physical-1",
    "release_sha": "a" * 64,
    "hardware_id": "mac-test-1",
    "gate_config_sha256": "b" * 64,
}


def _device(mount: str = "/Volumes/MAGI_PHYSICAL_TEST") -> dict[str, object]:
    return {
        "volume_uuid": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
        "media_uuid": "11111111-2222-3333-4444-555555555555",
        "device_identifier": "disk9s1",
        "parent_whole_disk": "disk9",
        "mount_point": mount,
        "filesystem": "apfs",
        "internal": False,
        "device_location": "External",
        "virtual_or_physical": "Physical",
        "system_parent_whole_disk": "disk3",
        "total_size_bytes": 512 * 1024 * 1024,
        "free_space_bytes_at_plan": 480 * 1024 * 1024,
    }


def _raw_command(
    sequence: int, command_class: str, argv: list[str], *, returncode: int
) -> dict[str, object]:
    row: dict[str, object] = {
        "sequence": sequence,
        "command_class": command_class,
        "argv": argv,
        "argv_sha256": hashlib.sha256(drill._canonical(argv)).hexdigest(),
        "allowed_by_backend_policy": True,
        "started_at": "2026-07-17T00:00:00+00:00",
        "completed_at": "2026-07-17T00:00:01+00:00",
        "returncode": returncode,
    }
    if command_class == "owned_power_writer":
        row["owned_pid"] = 9001
    return row


def _power_measurement(device: dict[str, object]) -> dict[str, object]:
    raw = [
        _raw_command(
            0,
            "owned_power_writer",
            [
                "/release/bin/python",
                "/release/physical_fault_drill.py",
                "power-writer-child",
                f"{device['mount_point']}/.magi-v3-physical-fault-test/power.sqlite3",
            ],
            returncode=74,
        ),
        _raw_command(
            1,
            "diskutil_info",
            ["/usr/sbin/diskutil", "info", "-plist", str(device["volume_uuid"])],
            returncode=1,
        ),
    ]
    return {
        "status": "passed",
        "physical_disappearance_observed": True,
        "device_node_absent_observed": True,
        "mount_absent_observed": True,
        "disappeared_at": "2026-07-17T00:00:02+00:00",
        "same_uuid_remounted": True,
        "remounted_device": device,
        "remounted_free_space_bytes": 400 * 1024 * 1024,
        "writer_pid": 9001,
        "writer_active_at_disconnect_prompt": True,
        "transaction_window_markers_observed": [
            "transaction_begin",
            "transaction_write_active",
            "durable_commit_ack",
        ],
        "last_transaction_stage_before_disappearance": "transaction_write_active",
        "child_io_failure_after_disappearance": True,
        "writer_returncode": 74,
        "parent_or_child_sigkill_used_for_power_loss": False,
        "forced_process_cleanup": False,
        "acknowledged_commit_count": 8,
        "durable_parent_ack_ledger_sha256": "c" * 64,
        "durable_parent_ack_ids_sha256": "d" * 64,
        "command_audit": drill._command_audit_metrics(raw),
        "acknowledged_commits_lost": 0,
        "partially_visible_transactions": 0,
        "duplicate_jobs": 0,
        "integrity_check": "ok",
    }


def _passing_report() -> dict[str, object]:
    device = _device()
    report: dict[str, object] = {
        "schema": drill.REPORT_SCHEMA,
        "schema_version": 2,
        "status": "passed",
        **CTX,
        "generated_at": "2026-07-17T00:02:00+00:00",
        "started_at": "2026-07-17T00:01:00+00:00",
        "plan_file_sha256": "e" * 64,
        "authorization_sha256": "f" * 64,
        "device": device,
        "preflight": {
            "ok": True,
            "device": device,
            "observed_total_size_bytes": device["total_size_bytes"],
            "observed_free_space_bytes": device["free_space_bytes_at_plan"],
            "mount_is_mounted": True,
            "mount_root_empty_before_workdir": True,
        },
        "measurements": {
            "physical_apfs_enospc": {
                "status": "passed",
                "filesystem_enospc_observed": True,
                "sqlite_full_observed": True,
                "baseline_rows": 1,
                "partial_rows": 0,
                "integrity_check": "ok",
                "filler_removed": True,
            },
            "external_device_power_disconnect": _power_measurement(device),
            "random_transaction_stage_sigkill": {
                "status": "passed",
                "cycles": 64,
                "offset_source": "secrets.choice(transaction_stage_markers)",
                "distinct_transaction_stages": 16,
                "acknowledged_commits_lost": 0,
                "partially_visible_transactions": 0,
                "duplicate_jobs": 0,
                "lost_jobs_after_recovery": 0,
                "cycle_receipt_sha256": "1" * 64,
            },
        },
        "claims": {
            "physical_apfs_enospc_certified": True,
            "external_device_power_disconnect_certified": True,
            "whole_machine_power_cut_certified": False,
            "random_transaction_stage_sigkill_certified": True,
            "arbitrary_machine_instruction_sigkill_certified": False,
        },
        "reconciliation": {
            "acknowledged_commits_lost": 0,
            "partially_visible_transactions": 0,
            "duplicate_jobs": 0,
            "lost_jobs_after_recovery": 0,
        },
        "safety": {
            "external_physical_non_system_apfs_only": True,
            "disk_image_or_sparse_image_used": False,
            "diskutil_unmount_or_eject_invoked": False,
            "live_magi_state_accessed": False,
            "system_disk_touched": False,
            "signals_sent_only_to_owned_children": True,
            "owned_workdir_removed": True,
        },
    }
    report["evidence_sha256"] = drill._semantic(report)
    return report


def _rehash(report: dict[str, object]) -> None:
    report.pop("evidence_sha256", None)
    report["evidence_sha256"] = drill._semantic(report)


@pytest.mark.parametrize(
    "mutation",
    [
        {"Internal": True},
        {"DeviceLocation": "Internal"},
        {"VirtualOrPhysical": "Virtual"},
        {"FilesystemType": "hfs"},
        {"DiskImage": True},
        {"ParentWholeDisk": "disk3"},
        {"MountPoint": "/tmp/not-a-volume"},
        {"TotalSize": drill.MAXIMUM_PHYSICAL_VOLUME_BYTES + 1},
        {"FreeSpace": drill.MINIMUM_PHYSICAL_VOLUME_BYTES - 1},
    ],
)
def test_selected_device_rejects_nonphysical_or_system_targets(
    mutation: dict[str, object],
) -> None:
    info: dict[str, object] = {
        "MountPoint": "/Volumes/MAGI_PHYSICAL_TEST",
        "FilesystemType": "apfs",
        "DeviceIdentifier": "disk9s1",
        "ParentWholeDisk": "disk9",
        "VolumeUUID": "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE",
        "MediaUUID": "11111111-2222-3333-4444-555555555555",
        "VirtualOrPhysical": "Physical",
        "Internal": False,
        "DeviceLocation": "External",
        "TotalSize": 512 * 1024 * 1024,
        "FreeSpace": 480 * 1024 * 1024,
    }
    info.update(mutation)
    with pytest.raises(drill.PhysicalFaultBlocked, match="external physical"):
        drill._selected_device(info, {"ParentWholeDisk": "disk3"})


def test_prepare_plan_requires_mounted_empty_root_and_writes_owner_only_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    volume = tmp_path / "volume"
    volume.mkdir()
    plan = tmp_path / "plan.json"
    token = tmp_path / "token.json"
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) == volume)
    monkeypatch.setattr(
        drill,
        "_selected_device",
        lambda _info, _system: _device(str(volume)),
    )
    result = drill.prepare_plan(
        volume=volume,
        output=plan,
        token_output=token,
        expected_context=CTX,
        disk_info=lambda _target: {},
    )
    assert result["status"] == "prepared_not_authorized"
    assert plan.stat().st_mode & 0o777 == 0o400
    assert token.stat().st_mode & 0o777 == 0o600
    assert json.loads(plan.read_text())["mutation_performed"] is False

    other = tmp_path / "other"
    other.mkdir()
    (other / "occupied").write_text("x")
    monkeypatch.setattr(os.path, "ismount", lambda path: Path(path) in {volume, other})
    with pytest.raises(drill.PhysicalFaultBlocked, match="empty"):
        drill.prepare_plan(
            volume=other,
            output=tmp_path / "never.json",
            token_output=tmp_path / "never-token.json",
            expected_context=CTX,
            disk_info=lambda _target: {},
        )


def test_authorization_requires_allowlisted_interactive_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    volume = tmp_path / "volume"
    volume.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda _path: True)
    monkeypatch.setattr(drill, "_selected_device", lambda *_args: _device(str(volume)))
    plan = tmp_path / "plan.json"
    drill.prepare_plan(
        volume=volume,
        output=plan,
        token_output=tmp_path / "token.json",
        expected_context=CTX,
        disk_info=lambda _target: {},
    )
    with pytest.raises(drill.PhysicalFaultBlocked, match="interactive"):
        drill.authorize_plan(
            plan_path=plan,
            output=tmp_path / "no.json",
            isatty=lambda: False,
            uid=501,
            user="ai",
        )
    plan_doc = json.loads(plan.read_text())
    phrase = f"AUTHORIZE PHYSICAL FAULT {plan_doc['plan_id']} {plan_doc['device']['volume_uuid']}"
    receipt = drill.authorize_plan(
        plan_path=plan,
        output=tmp_path / "authorization.json",
        input_reader=lambda _prompt: phrase,
        isatty=lambda: True,
        uid=501,
        user="ai",
        tty_name="/dev/ttys001",
    )
    assert receipt["status"] == "authorized"


def test_authorization_rejects_legacy_plan_schema_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    volume = tmp_path / "volume"
    volume.mkdir()
    monkeypatch.setattr(os.path, "ismount", lambda _path: True)
    monkeypatch.setattr(drill, "_selected_device", lambda *_args: _device(str(volume)))
    plan_path = tmp_path / "plan.json"
    drill.prepare_plan(
        volume=volume,
        output=plan_path,
        token_output=tmp_path / "token.json",
        expected_context=CTX,
        disk_info=lambda _target: {},
    )
    plan = json.loads(plan_path.read_text())
    plan["schema_version"] = 1
    plan["plan_sha256"] = drill._semantic(
        {key: value for key, value in plan.items() if key != "plan_sha256"}
    )
    plan_path.chmod(0o600)
    plan_path.write_bytes(drill._canonical(plan) + b"\n")
    phrase = f"AUTHORIZE PHYSICAL FAULT {plan['plan_id']} {plan['device']['volume_uuid']}"
    with pytest.raises(drill.PhysicalFaultBlocked, match="authorization"):
        drill.authorize_plan(
            plan_path=plan_path,
            output=tmp_path / "authorization.json",
            input_reader=lambda _prompt: phrase,
            isatty=lambda: True,
            uid=501,
            user="ai",
            tty_name="/dev/ttys001",
        )


def test_report_verifier_rejects_fake_physical_and_reconciliation_claims() -> None:
    report = _passing_report()
    drill.verify_report(report, CTX)
    mutations = [
        ("safety", "disk_image_or_sparse_image_used", True),
        ("safety", "diskutil_unmount_or_eject_invoked", True),
        ("safety", "system_disk_touched", True),
        ("reconciliation", "acknowledged_commits_lost", 1),
        ("reconciliation", "partially_visible_transactions", 1),
        ("reconciliation", "duplicate_jobs", 1),
    ]
    for section, field, value in mutations:
        tampered = copy.deepcopy(report)
        tampered[section][field] = value  # type: ignore[index]
        _rehash(tampered)
        with pytest.raises(drill.PhysicalFaultBlocked, match="real-device"):
            drill.verify_report(tampered, CTX)

    for field, value in [
        ("whole_machine_power_cut_certified", True),
        ("arbitrary_machine_instruction_sigkill_certified", True),
        ("external_device_power_disconnect_certified", False),
    ]:
        tampered = copy.deepcopy(report)
        tampered["claims"][field] = value  # type: ignore[index]
        _rehash(tampered)
        with pytest.raises(drill.PhysicalFaultBlocked, match="real-device"):
            drill.verify_report(tampered, CTX)

    for field, value in [
        ("writer_active_at_disconnect_prompt", False),
        ("child_io_failure_after_disappearance", False),
        ("parent_or_child_sigkill_used_for_power_loss", True),
        ("acknowledged_commit_count", 7),
    ]:
        tampered = copy.deepcopy(report)
        tampered["measurements"]["external_device_power_disconnect"][field] = value  # type: ignore[index]
        _rehash(tampered)
        with pytest.raises(drill.PhysicalFaultBlocked, match="real-device"):
            drill.verify_report(tampered, CTX)

    nested_mutations = [
        ("device", "internal", True),
        ("preflight", "mount_is_mounted", False),
        ("preflight", "mount_root_empty_before_workdir", False),
    ]
    for section, field, value in nested_mutations:
        tampered = copy.deepcopy(report)
        tampered[section][field] = value  # type: ignore[index]
        _rehash(tampered)
        with pytest.raises(drill.PhysicalFaultBlocked, match="real-device"):
            drill.verify_report(tampered, CTX)


def test_command_audit_detects_unmount_even_if_boolean_is_forged() -> None:
    report = _passing_report()
    power = report["measurements"]["external_device_power_disconnect"]  # type: ignore[index]
    raw = power["command_audit"]["raw_commands"]  # type: ignore[index]
    raw[1]["argv"] = ["/usr/sbin/diskutil", "unmount", "disk9s1"]
    raw[1]["argv_sha256"] = hashlib.sha256(drill._canonical(raw[1]["argv"])).hexdigest()
    power["command_audit"] = drill._command_audit_metrics(raw)  # type: ignore[index]
    power["command_audit"]["diskutil_unmount_or_eject_invoked"] = False  # type: ignore[index]
    _rehash(report)
    with pytest.raises(drill.PhysicalFaultBlocked, match="real-device"):
        drill.verify_report(report, CTX)


def test_artifact_chain_binds_plan_authorization_report_and_expiry(tmp_path: Path) -> None:
    now = datetime(2026, 7, 17, 0, 0, tzinfo=timezone.utc)
    device = _device()
    plan: dict[str, object] = {
        "schema": drill.PLAN_SCHEMA,
        "schema_version": 2,
        "plan_id": "plan-physical-1",
        **CTX,
        "device": device,
        "owned_workdir": "/Volumes/MAGI_PHYSICAL_TEST/.magi-v3-physical-fault-test",
        "token_sha256": "2" * 64,
        "authorized_actions": drill.AUTHORIZED_ACTIONS,
        "minimum_sigkill_cycles": 64,
        "prepared_at": now.isoformat(),
        "expires_at": (now + timedelta(hours=2)).isoformat(),
        "mutation_performed": False,
    }
    plan["plan_sha256"] = drill._semantic(plan)
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(drill._canonical(plan) + b"\n")
    authorization: dict[str, object] = {
        "schema": drill.AUTH_SCHEMA,
        "schema_version": 2,
        "status": "authorized",
        **CTX,
        "plan_id": plan["plan_id"],
        "plan_file_sha256": drill._sha(plan_path),
        "device": device,
        "authorized_actions": drill.AUTHORIZED_ACTIONS,
        "approver_uid": 501,
        "approver_user": "ai",
        "auth_method": "allowlisted_local_owner_interactive_tty",
        "tty_session_sha256": "3" * 64,
        "authorized_at": (now + timedelta(minutes=1)).isoformat(),
        "expires_at": (now + timedelta(minutes=31)).isoformat(),
        "human_interaction_performed": True,
    }
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_bytes(drill._canonical(authorization) + b"\n")
    report = _passing_report()
    report["started_at"] = (now + timedelta(minutes=2)).isoformat()
    report["generated_at"] = (now + timedelta(minutes=3)).isoformat()
    report["plan_file_sha256"] = drill._sha(plan_path)
    report["authorization_sha256"] = drill._sha(authorization_path)
    _rehash(report)
    report_path = tmp_path / "report.json"
    report_path.write_bytes(drill._canonical(report) + b"\n")
    drill.verify_artifact_chain(
        report=report,
        report_sha256=drill._sha(report_path),
        plan=plan,
        plan_sha256=drill._sha(plan_path),
        authorization=authorization,
        authorization_sha256=drill._sha(authorization_path),
        expected_context=CTX,
    )
    with pytest.raises(drill.PhysicalFaultBlocked, match="chain"):
        drill.verify_artifact_chain(
            report=report,
            report_sha256="0" * 64,
            plan=plan,
            plan_sha256=drill._sha(plan_path),
            authorization=authorization,
            authorization_sha256=drill._sha(authorization_path),
            expected_context=CTX,
        )

    for plan_version, authorization_version, report_version in (
        (1, 2, 2),
        (2, 1, 2),
        (2, 2, 1),
    ):
        versioned_plan = copy.deepcopy(plan)
        versioned_plan["schema_version"] = plan_version
        versioned_plan["plan_sha256"] = drill._semantic(
            {
                key: value
                for key, value in versioned_plan.items()
                if key != "plan_sha256"
            }
        )
        versioned_plan_sha = hashlib.sha256(
            drill._canonical(versioned_plan) + b"\n"
        ).hexdigest()
        versioned_authorization = copy.deepcopy(authorization)
        versioned_authorization["schema_version"] = authorization_version
        versioned_authorization["plan_file_sha256"] = versioned_plan_sha
        versioned_authorization_sha = hashlib.sha256(
            drill._canonical(versioned_authorization) + b"\n"
        ).hexdigest()
        versioned_report = copy.deepcopy(report)
        versioned_report["schema_version"] = report_version
        versioned_report["plan_file_sha256"] = versioned_plan_sha
        versioned_report["authorization_sha256"] = versioned_authorization_sha
        _rehash(versioned_report)
        versioned_report_sha = hashlib.sha256(
            drill._canonical(versioned_report) + b"\n"
        ).hexdigest()
        with pytest.raises(drill.PhysicalFaultBlocked):
            drill.verify_artifact_chain(
                report=versioned_report,
                report_sha256=versioned_report_sha,
                plan=versioned_plan,
                plan_sha256=versioned_plan_sha,
                authorization=versioned_authorization,
                authorization_sha256=versioned_authorization_sha,
                expected_context=CTX,
            )


class _FakeBackend:
    def __init__(self, device: dict[str, object]) -> None:
        self.device = device
        self.calls: list[str] = []

    def revalidate(self, _device: dict[str, object]) -> dict[str, object]:
        self.calls.append("revalidate")
        return {
            "ok": True,
            "device": self.device,
            "observed_total_size_bytes": self.device["total_size_bytes"],
            "observed_free_space_bytes": self.device["free_space_bytes_at_plan"],
            "mount_is_mounted": True,
            "mount_root_empty_before_workdir": True,
        }

    def enospc(self, _workdir: Path) -> dict[str, object]:
        self.calls.append("enospc")
        return _passing_report()["measurements"]["physical_apfs_enospc"]  # type: ignore[index]

    def random_transaction_stage_sigkill(
        self, _workdir: Path, cycles: int
    ) -> dict[str, object]:
        self.calls.append("sigkill")
        row = _passing_report()["measurements"]["random_transaction_stage_sigkill"]  # type: ignore[index]
        assert row["cycles"] == cycles
        return row

    def external_device_disconnect(
        self, _workdir: Path, _device: dict[str, object]
    ) -> dict[str, object]:
        self.calls.append("power")
        return _power_measurement(self.device)

    def cleanup(self, workdir: Path) -> dict[str, object]:
        self.calls.append("cleanup")
        shutil.rmtree(workdir)
        return {"ok": True, "owned_workdir_removed": True}


def test_executor_consumes_one_time_token_and_uses_only_injected_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    volume = tmp_path / "volume"
    volume.mkdir()
    device = _device(str(volume))
    monkeypatch.setattr(os.path, "ismount", lambda _path: True)
    monkeypatch.setattr(drill, "_selected_device", lambda *_args: device)
    plan_path = tmp_path / "plan.json"
    token_path = tmp_path / "token.json"
    drill.prepare_plan(
        volume=volume,
        output=plan_path,
        token_output=token_path,
        expected_context=CTX,
        disk_info=lambda _target: {},
    )
    plan = json.loads(plan_path.read_text())
    phrase = f"AUTHORIZE PHYSICAL FAULT {plan['plan_id']} {plan['device']['volume_uuid']}"
    authorization = tmp_path / "authorization.json"
    drill.authorize_plan(
        plan_path=plan_path,
        output=authorization,
        input_reader=lambda _prompt: phrase,
        isatty=lambda: True,
        uid=501,
        user="ai",
        tty_name="/dev/ttys001",
    )
    legacy_plan = copy.deepcopy(plan)
    legacy_plan["schema_version"] = 1
    legacy_plan["plan_sha256"] = drill._semantic(
        {key: value for key, value in legacy_plan.items() if key != "plan_sha256"}
    )
    legacy_plan_path = tmp_path / "legacy-plan.json"
    legacy_plan_path.write_bytes(drill._canonical(legacy_plan) + b"\n")
    valid_authorization = json.loads(authorization.read_text())
    legacy_plan_authorization = copy.deepcopy(valid_authorization)
    legacy_plan_authorization["plan_file_sha256"] = drill._sha(legacy_plan_path)
    legacy_plan_authorization_path = tmp_path / "legacy-plan-authorization.json"
    legacy_plan_authorization_path.write_bytes(
        drill._canonical(legacy_plan_authorization) + b"\n"
    )
    with pytest.raises(drill.PhysicalFaultBlocked, match="invalid or expired"):
        drill.execute_plan(
            plan_path=legacy_plan_path,
            token_path=token_path,
            authorization_path=legacy_plan_authorization_path,
            output=tmp_path / "legacy-plan-report.json",
            backend=_FakeBackend(device),
        )

    legacy_authorization = copy.deepcopy(valid_authorization)
    legacy_authorization["schema_version"] = 1
    legacy_authorization_path = tmp_path / "legacy-authorization.json"
    legacy_authorization_path.write_bytes(drill._canonical(legacy_authorization) + b"\n")
    with pytest.raises(drill.PhysicalFaultBlocked, match="invalid or expired"):
        drill.execute_plan(
            plan_path=plan_path,
            token_path=token_path,
            authorization_path=legacy_authorization_path,
            output=tmp_path / "legacy-authorization-report.json",
            backend=_FakeBackend(device),
        )

    fake = _FakeBackend(device)
    monkeypatch.setattr(drill, "verify_report", lambda *_args, **_kwargs: None)
    result = drill.execute_plan(
        plan_path=plan_path,
        token_path=token_path,
        authorization_path=authorization,
        output=tmp_path / "report.json",
        backend=fake,
    )
    assert result["status"] == "passed"
    assert fake.calls == ["revalidate", "enospc", "sigkill", "power", "cleanup"]
    assert not token_path.exists()


def test_internal_writer_and_execute_cli_are_inert_without_capability_and_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAGI_PHYSICAL_WRITER_CAPABILITY_FD", raising=False)
    monkeypatch.delenv("MAGI_PHYSICAL_WRITER_CAPABILITY_SHA256", raising=False)
    with pytest.raises(drill.PhysicalFaultBlocked, match="capability"):
        drill._power_writer_child(tmp_path / "power.sqlite3")
    with pytest.raises(SystemExit) as error:
        drill.main(
            [
                "execute",
                "--plan",
                str(tmp_path / "plan.json"),
                "--token",
                str(tmp_path / "token.json"),
                "--authorization",
                str(tmp_path / "authorization.json"),
                "--output",
                str(tmp_path / "report.json"),
                "--confirm-volume-uuid",
                "never",
            ]
        )
    assert error.value.code == 2
