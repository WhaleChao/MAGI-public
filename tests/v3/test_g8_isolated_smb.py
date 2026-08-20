from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from scripts.v3_validation import g8_isolated_smb as g8
from scripts.v3_validation.human_approval import (
    build_conditional_approval_request,
    capture_conditional_local_approval,
)


def _receipt(argv: Sequence[str], stdout: str = "", returncode: int = 0) -> dict[str, Any]:
    return {
        "argv": list(argv),
        "returncode": returncode,
        "stdout": stdout,
        "stderr": "",
        "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
    }


class FakeSMB:
    def __init__(self, mount: Path) -> None:
        self.mount = mount
        self.ps = "1 /sbin/launchd\n"
        self.lsof = ""
        self.launchctl = "PID Status Label\n"
        self.v3_multiplier = 1.0
        self.temporal_multipliers: list[float] = []
        self.sample_multipliers: dict[tuple[str, int], float] = {}
        self.arm_call_count = 0
        self.cleanup_entries: list[str] = []
        self.calls: list[tuple[str, Path]] = []

    def mount_receipt(self, target: Path) -> Mapping[str, Any]:
        self.calls.append(("mount", target))
        return _receipt(
            ["/sbin/mount"],
            f"//owner@nas.example/validation on {self.mount} (smbfs, nodev)\n",
        )

    def state_snapshot(self) -> Mapping[str, Any]:
        return {
            "ps": _receipt(["/bin/ps", "-axo", "pid=,command="], self.ps),
            "lsof": _receipt(
                ["/usr/sbin/lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
                self.lsof,
                1 if not self.lsof else 0,
            ),
            "launchctl": _receipt(["/bin/launchctl", "list"], self.launchctl),
        }

    def run_arm(
        self,
        target: Path,
        arm: str,
        samples: int,
        payload: bytes,
        *,
        sample_offset: int = 0,
    ) -> Mapping[str, Any]:
        self.calls.append((f"arm-{arm}", target))
        temporal = (
            self.temporal_multipliers[self.arm_call_count]
            if self.arm_call_count < len(self.temporal_multipliers)
            else 1.0
        )
        self.arm_call_count += 1
        digest = hashlib.sha256(payload).hexdigest()
        rows = []
        for sample in range(sample_offset, sample_offset + samples):
            name = f".magi-g8-probe-{arm}-{sample:04d}-{'a' * 32}"
            transcript = [
                {"op": "scandir", "path": ".", "entries": []},
                {"op": "open_exclusive", "path": name},
                {"op": "write", "path": name, "bytes": len(payload), "sha256": digest},
                {"op": "fsync_file", "path": name},
                {"op": "close_write", "path": name},
                {"op": "open_readonly", "path": name},
                {"op": "read", "path": name, "bytes": len(payload), "sha256": digest},
                {"op": "close_read", "path": name},
                {"op": "unlink", "path": name},
                {"op": "fsync_directory", "path": "."},
                {"op": "scandir", "path": ".", "entries": []},
            ]
            base = 1_000_000 + sample
            rows.append(
                {
                    "sample": sample,
                    "duration_ns": int(
                        base
                        * temporal
                        * self.sample_multipliers.get((arm, sample), 1.0)
                        * (self.v3_multiplier if arm == "v3" else 1)
                    ),
                    "filename": name,
                    "transcript": transcript,
                }
            )
        return {"arm": arm, "samples": rows}

    def cleanup_owned(self, target: Path) -> Mapping[str, Any]:
        self.calls.append(("cleanup", target))
        return {"removed_owned_names": [], "entries_after": list(self.cleanup_entries)}

    def entries(self, target: Path) -> Sequence[str]:
        self.calls.append(("entries", target))
        return tuple(self.cleanup_entries)


def _layout(tmp_path: Path) -> tuple[dict[str, Path], FakeSMB]:
    from tests.v3 import test_campaign_runner as campaign_fixtures

    release = tmp_path / "release"
    service = release / "config/v3_service_manifest.json"
    service.parent.mkdir(parents=True)
    service.write_text('{"schema_version":1,"services":[]}\n')
    service_sha = hashlib.sha256(service.read_bytes()).hexdigest()
    certifier = release / "scripts/v3_validation/perf_certification.py"
    certifier.parent.mkdir(parents=True)
    certifier.write_bytes(
        Path(g8.__file__).with_name("perf_certification.py").read_bytes()
    )
    certifier_sha = hashlib.sha256(certifier.read_bytes()).hexdigest()
    manifest = release / "release-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "release_id": "v3-test",
                "release_sha256": "a" * 64,
                "files": [
                    {"path": "config/v3_service_manifest.json", "sha256": service_sha},
                    {
                        "path": "scripts/v3_validation/perf_certification.py",
                        "sha256": certifier_sha,
                    },
                ],
            },
            sort_keys=True,
        )
        + "\n"
    )
    ownership = tmp_path / "ownership.json"
    ownership.write_text(
        json.dumps(
            {
                "release_id": "v3-test",
                "service_manifest": str(service),
                "service_manifest_sha256": service_sha,
                "roles": ["control", "gateway", "supervisor"],
            },
            sort_keys=True,
        )
        + "\n"
    )
    matched = tmp_path / "matched-performance.json"
    matched.write_text(
        json.dumps(
            campaign_fixtures._passing_matched_performance_report(
                certifier_sha256=certifier_sha,
                runtime_sha256="c" * 64,
            ),
            sort_keys=True,
        )
        + "\n"
    )
    mount = tmp_path / "remote-mount"
    target = mount / "operator-created-empty-validation"
    target.mkdir(parents=True)
    root = tmp_path / "evidence"
    root.mkdir()
    paths = {
        "release": release,
        "manifest": manifest,
        "service": service,
        "ownership": ownership,
        "matched": matched,
        "mount": mount,
        "target": target,
        "plan": root / "plan.json",
        "token": root / "token.txt",
        "auth": root / "authorization.json",
        "report": root / "report.json",
    }
    return paths, FakeSMB(mount)


def test_release_identity_accepts_only_sealed_allowlisted_service_manifests(
    tmp_path: Path,
) -> None:
    paths, _adapter = _layout(tmp_path)
    manifest = json.loads(paths["manifest"].read_text())
    live_service = paths["release"] / "config/v3_live_validation_service_manifest.json"
    live_service.write_text('{"schema_version":1,"services":[]}\n')
    live_sha = hashlib.sha256(live_service.read_bytes()).hexdigest()
    manifest["files"].append(
        {
            "path": "config/v3_live_validation_service_manifest.json",
            "sha256": live_sha,
        }
    )
    paths["manifest"].write_text(json.dumps(manifest, sort_keys=True) + "\n")
    assert g8._release_identity(paths["manifest"], live_service) == (
        "v3-test",
        "a" * 64,
        live_sha,
        "config/v3_live_validation_service_manifest.json",
    )

    unlisted = paths["release"] / "config/unlisted-service-manifest.json"
    unlisted.write_text('{"schema_version":1,"services":[]}\n')
    with pytest.raises(g8.G8SMBBlocked, match="allowlisted"):
        g8._release_identity(paths["manifest"], unlisted)


def _execute(
    tmp_path: Path,
    *,
    temporal_multipliers: list[float] | None = None,
    sample_multipliers: dict[tuple[str, int], float] | None = None,
    samples_per_arm: int = 10,
) -> tuple[dict[str, Any], dict[str, Path], FakeSMB]:
    paths, adapter = _layout(tmp_path)
    adapter.temporal_multipliers = list(temporal_multipliers or [])
    adapter.sample_multipliers = dict(sample_multipliers or {})
    prepared = g8.prepare_plan(
        target=paths["target"],
        release_manifest=paths["manifest"],
        service_manifest=paths["service"],
        ownership_manifest=paths["ownership"],
        matched_performance_report=paths["matched"],
        output=paths["plan"],
        token_output=paths["token"],
        adapter=adapter,
        samples_per_arm=samples_per_arm,
    )
    plan = json.loads(paths["plan"].read_text())
    phrase = f"AUTHORIZE MAGI G8 SMB {plan['plan_id']} {plan['target_sha256']}"
    authorization = g8.authorize_plan(
        plan_path=paths["plan"],
        plan_file_sha256=prepared["plan_file_sha256"],
        output=paths["auth"],
        input_reader=lambda _prompt: phrase,
        local_uid=501,
        local_user="ai",
        tty_name="/dev/ttys-test",
    )
    report = g8.execute_plan(
        plan_path=paths["plan"],
        plan_file_sha256=prepared["plan_file_sha256"],
        authorization_path=paths["auth"],
        authorization_sha256=authorization["authorization_sha256"],
        token_file=paths["token"],
        output=paths["report"],
        adapter=adapter,
    )
    return report, paths, adapter


def test_conditional_preauthorization_binds_one_g8_target_without_consuming_final_marker(
    tmp_path: Path,
) -> None:
    paths, adapter = _layout(tmp_path)
    context = {
        "campaign_id": "g8-test",
        "release_sha": "a" * 64,
        "hardware_id": "test-mac",
        "gate_config_sha256": "b" * 64,
    }
    instant = datetime(2026, 7, 27, 7, 0, tzinfo=timezone.utc)
    prepared = g8.prepare_plan(
        target=paths["target"], release_manifest=paths["manifest"], service_manifest=paths["service"],
        ownership_manifest=paths["ownership"], matched_performance_report=paths["matched"],
        output=paths["plan"], token_output=paths["token"], adapter=adapter, samples_per_arm=10,
        approval_context=context, now=instant,
    )
    plan = json.loads(paths["plan"].read_text())
    window = {
        "starts_at": (instant + timedelta(hours=1)).isoformat(),
        "ends_at": (instant + timedelta(hours=1, minutes=20)).isoformat(),
        "timezone": "Asia/Taipei",
    }
    request = tmp_path / "conditional-request.json"
    pre = build_conditional_approval_request(
        expected_context=context, cutover_window=window, output=request, now=instant,
        g8_smb_target_sha256=plan["target_sha256"],
        release_manifest_sha256=plan["release_manifest"]["sha256"], release_id=plan["release_id"],
        g8_plan_id=plan["plan_id"], g8_plan_file_sha256=prepared["plan_file_sha256"],
        g8_plan_semantic_sha256=plan["plan_sha256"],
        g8_usage_receipt_path=(tmp_path / "stable-g8-usage.json").resolve(),
    )
    receipt = tmp_path / "conditional-receipt.json"
    capture_conditional_local_approval(
        request_path=request, output=receipt, input_reader=lambda _prompt: pre["approval_phrase"],
        isatty=lambda: True, uid=501, user="ai", tty_name="/dev/ttys-test", now=instant + timedelta(minutes=1),
    )
    authorization = g8.authorize_plan(
        plan_path=paths["plan"], plan_file_sha256=prepared["plan_file_sha256"], output=paths["auth"],
        input_reader=lambda _prompt: (_ for _ in ()).throw(AssertionError("must not prompt again")),
        conditional_request_path=request, conditional_receipt_path=receipt, isatty=lambda: False,
        now=instant + timedelta(hours=1, minutes=1),
    )
    auth = json.loads(paths["auth"].read_text())
    usage = Path(auth["conditional_g8_usage"]["path"])
    assert usage.is_file()
    assert json.loads(usage.read_text())["final_cutover_consumption_performed"] is False
    assert not Path(pre["consumption_marker"]).exists()
    assert authorization["status"] == "authorized_not_executed"
    with pytest.raises(g8.G8SMBBlocked, match="conditional G8 approval is invalid"):
        g8.authorize_plan(
            plan_path=paths["plan"], plan_file_sha256=prepared["plan_file_sha256"],
            output=tmp_path / "auth-at-end.json", conditional_request_path=request,
            conditional_receipt_path=receipt, now=instant + timedelta(hours=1, minutes=20),
        )
    with pytest.raises(FileExistsError):
        g8.authorize_plan(
            plan_path=paths["plan"], plan_file_sha256=prepared["plan_file_sha256"],
            output=tmp_path / "different-authorization-output.json", conditional_request_path=request,
            conditional_receipt_path=receipt, now=instant + timedelta(hours=1, minutes=2),
        )
    with pytest.raises(g8.G8SMBBlocked, match="cannot be reverified"):
        g8.execute_plan(
            plan_path=paths["plan"],
            plan_file_sha256=prepared["plan_file_sha256"],
            authorization_path=paths["auth"],
            authorization_sha256=authorization["authorization_sha256"],
            token_file=paths["token"],
            output=paths["report"],
            adapter=adapter,
            now=instant + timedelta(hours=1, minutes=20),
        )
    report = g8.execute_plan(
        plan_path=paths["plan"],
        plan_file_sha256=prepared["plan_file_sha256"],
        authorization_path=paths["auth"],
        authorization_sha256=authorization["authorization_sha256"],
        token_file=paths["token"],
        output=paths["report"],
        adapter=adapter,
        now=instant + timedelta(hours=1, minutes=3),
    )
    assert report["status"] == "passed"


def _conditional_context_case(
    tmp_path: Path,
    *,
    request_context: Mapping[str, str] | None = None,
) -> tuple[dict[str, Path], dict[str, Any], dict[str, str], Path, Path, datetime]:
    paths, adapter = _layout(tmp_path)
    context = {
        "campaign_id": "g8-context-campaign",
        "release_sha": "a" * 64,
        "hardware_id": "g8-context-hardware",
        "gate_config_sha256": "b" * 64,
    }
    instant = datetime(2026, 7, 27, 7, 0, tzinfo=timezone.utc)
    prepared = g8.prepare_plan(
        target=paths["target"],
        release_manifest=paths["manifest"],
        service_manifest=paths["service"],
        ownership_manifest=paths["ownership"],
        matched_performance_report=paths["matched"],
        output=paths["plan"],
        token_output=paths["token"],
        adapter=adapter,
        samples_per_arm=10,
        approval_context=context,
        now=instant,
    )
    plan = json.loads(paths["plan"].read_text())
    request = tmp_path / "conditional-context-request.json"
    pre = build_conditional_approval_request(
        expected_context=request_context or context,
        cutover_window={
            "starts_at": (instant + timedelta(hours=1)).isoformat(),
            "ends_at": (instant + timedelta(hours=2)).isoformat(),
            "timezone": "Asia/Taipei",
        },
        output=request,
        now=instant,
        g8_smb_target_sha256=plan["target_sha256"],
        release_manifest_sha256=plan["release_manifest"]["sha256"],
        release_id=plan["release_id"],
        g8_plan_id=plan["plan_id"],
        g8_plan_file_sha256=prepared["plan_file_sha256"],
        g8_plan_semantic_sha256=plan["plan_sha256"],
        g8_usage_receipt_path=(tmp_path / "context-g8-usage.json").resolve(),
    )
    receipt = tmp_path / "conditional-context-receipt.json"
    capture_conditional_local_approval(
        request_path=request,
        output=receipt,
        input_reader=lambda _prompt: pre["approval_phrase"],
        isatty=lambda: True,
        uid=501,
        user="ai",
        tty_name="/dev/ttys-context",
        now=instant + timedelta(minutes=1),
    )
    return paths, prepared, context, request, receipt, instant


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("campaign_id", "different-campaign"),
        ("hardware_id", "different-hardware"),
        ("gate_config_sha256", "c" * 64),
        ("release_sha", "d" * 64),
    ],
)
def test_conditional_g8_rejects_cross_context_receipts(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    base = {
        "campaign_id": "g8-context-campaign",
        "release_sha": "a" * 64,
        "hardware_id": "g8-context-hardware",
        "gate_config_sha256": "b" * 64,
    }
    base[field] = replacement
    paths, prepared, _context, request, receipt, instant = _conditional_context_case(
        tmp_path,
        request_context=base,
    )
    with pytest.raises(g8.G8SMBBlocked, match="conditional G8 approval is invalid"):
        g8.authorize_plan(
            plan_path=paths["plan"],
            plan_file_sha256=prepared["plan_file_sha256"],
            output=paths["auth"],
            conditional_request_path=request,
            conditional_receipt_path=receipt,
            now=instant + timedelta(hours=1, minutes=1),
        )


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("approver_id", "local:501:someone-else"),
        ("tty_session_sha256", "not-a-sha"),
        ("approval_scope", "different-scope"),
        ("approved_at", "2026-07-27T08:01:00+00:00"),
        ("human_interaction_performed", False),
    ],
)
def test_conditional_g8_rejects_malformed_or_stale_receipts(
    tmp_path: Path,
    field: str,
    replacement: Any,
) -> None:
    paths, prepared, _context, request, receipt, instant = _conditional_context_case(tmp_path)
    mutated = json.loads(receipt.read_text())
    mutated[field] = replacement
    receipt.chmod(0o600)
    receipt.write_text(json.dumps(mutated, sort_keys=True) + "\n")
    with pytest.raises(g8.G8SMBBlocked, match="conditional G8 approval is invalid"):
        g8.authorize_plan(
            plan_path=paths["plan"],
            plan_file_sha256=prepared["plan_file_sha256"],
            output=paths["auth"],
            conditional_request_path=request,
            conditional_receipt_path=receipt,
            now=instant + timedelta(hours=1, minutes=1),
        )


def test_full_fake_remote_smb_plan_approval_execute_and_raw_recompute(tmp_path: Path) -> None:
    report, paths, adapter = _execute(tmp_path)
    derived = g8.verify_report(
        report,
        expected_release_id="v3-test",
        expected_release_manifest_sha256=hashlib.sha256(paths["manifest"].read_bytes()).hexdigest(),
    )
    assert derived["threshold_passed"] is True
    assert derived["v2_zero_owner_snapshots"] == 5
    assert derived["balanced_abba_blocks"] is True
    assert derived["cleanup_verified_empty"] is True
    assert [operation for operation, _path in adapter.calls if operation.startswith("arm-")] == [
        "arm-v2",
        "arm-v3",
        "arm-v3",
        "arm-v2",
    ]
    assert all(path == paths["target"] for _operation, path in adapter.calls)
    assert not tuple(paths["target"].iterdir())


def test_host_adapter_preserves_primary_smb_fsync_error_when_close_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "remote-target"
    target.mkdir()
    adapter = g8.HostSMBAdapter()
    original_open = g8.os.open
    original_close = g8.os.close
    original_fsync = g8.os.fsync
    writable: set[int] = set()
    invalidated: set[int] = set()

    def tracked_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if flags & os.O_WRONLY:
            writable.add(descriptor)
        return descriptor

    def failed_fsync(descriptor: int) -> None:
        if descriptor in writable:
            writable.remove(descriptor)
            original_close(descriptor)
            invalidated.add(descriptor)
            raise OSError(errno.ENXIO, "Device not configured")
        original_fsync(descriptor)

    def failed_secondary_close(descriptor: int) -> None:
        if descriptor in invalidated:
            invalidated.remove(descriptor)
            raise OSError(errno.EBADF, "Bad file descriptor")
        original_close(descriptor)

    monkeypatch.setattr(g8.os, "open", tracked_open)
    monkeypatch.setattr(g8.os, "fsync", failed_fsync)
    monkeypatch.setattr(g8.os, "close", failed_secondary_close)

    with pytest.raises(OSError) as captured:
        adapter.run_arm(target, "v2", 1, b"payload")

    assert captured.value.errno == errno.ENXIO
    assert any(
        "secondary descriptor close failed" in note
        for note in captured.value.__notes__
    )
    assert list(target.iterdir()) == []


def test_balanced_abba_blocks_remove_fixed_order_time_bias(tmp_path: Path) -> None:
    report, paths, _adapter = _execute(
        tmp_path,
        temporal_multipliers=[1.0, 1.5, 1.0, 1.5],
    )
    derived = g8.verify_report(
        report,
        expected_release_id="v3-test",
        expected_release_manifest_sha256=hashlib.sha256(
            paths["manifest"].read_bytes()
        ).hexdigest(),
    )
    assert derived["balanced_abba_blocks"] is True
    assert derived["threshold_passed"] is True
    assert derived["v3_to_v2_p95_ratio"] < 1.01


def test_default_repeated_abba_microblocks_resist_sparse_smb_tail_jitter(
    tmp_path: Path,
) -> None:
    report, paths, adapter = _execute(
        tmp_path,
        samples_per_arm=g8.DEFAULT_SAMPLES_PER_ARM,
        sample_multipliers={
            ("v3", 12): 4.0,
            ("v3", 37): 4.0,
            ("v3", 62): 4.0,
            ("v3", 87): 4.0,
            ("v2", 25): 8.0,
        },
    )
    derived = g8.verify_report(
        report,
        expected_release_id="v3-test",
        expected_release_manifest_sha256=hashlib.sha256(
            paths["manifest"].read_bytes()
        ).hexdigest(),
    )
    plan = report["plan"]["workload"]
    assert plan["samples_per_arm"] == 100
    assert plan["samples_per_block"] == 5
    assert plan["abba_cycle_count"] == 10
    assert len(plan["arm_order"]) == 40
    assert plan["arm_order"] == list(g8.BALANCED_ARM_ORDER) * 10
    assert derived["repeated_abba_microblocks"] is True
    assert derived["abba_cycle_count"] == 10
    assert derived["threshold_passed"] is True
    assert len(
        [operation for operation, _path in adapter.calls if operation.startswith("arm-")]
    ) == 40


def test_execute_rejects_plan_matched_evidence_identity_drift(tmp_path: Path) -> None:
    paths, adapter = _layout(tmp_path)
    prepared = g8.prepare_plan(
        target=paths["target"],
        release_manifest=paths["manifest"],
        service_manifest=paths["service"],
        ownership_manifest=paths["ownership"],
        matched_performance_report=paths["matched"],
        output=paths["plan"],
        token_output=paths["token"],
        adapter=adapter,
        samples_per_arm=10,
    )
    plan = json.loads(paths["plan"].read_text())
    plan["matched_performance_report"]["evidence_sha256"] = "0" * 64
    plan.pop("plan_sha256")
    plan["plan_sha256"] = g8.sha256_json(plan)
    os.chmod(paths["plan"], 0o600)
    paths["plan"].write_bytes(g8._canonical(plan))
    os.chmod(paths["plan"], 0o400)
    prepared["plan_file_sha256"] = hashlib.sha256(paths["plan"].read_bytes()).hexdigest()
    phrase = f"AUTHORIZE MAGI G8 SMB {plan['plan_id']} {plan['target_sha256']}"
    authorization = g8.authorize_plan(
        plan_path=paths["plan"],
        plan_file_sha256=prepared["plan_file_sha256"],
        output=paths["auth"],
        input_reader=lambda _prompt: phrase,
        local_uid=501,
        local_user="ai",
        tty_name="/dev/ttys-test",
    )
    with pytest.raises(g8.G8SMBBlocked, match="matched service evidence"):
        g8.execute_plan(
            plan_path=paths["plan"],
            plan_file_sha256=prepared["plan_file_sha256"],
            authorization_path=paths["auth"],
            authorization_sha256=authorization["authorization_sha256"],
            token_file=paths["token"],
            output=paths["report"],
            adapter=adapter,
        )


def test_consumption_receipt_refuses_replay_even_when_token_remains(tmp_path: Path) -> None:
    _report, paths, adapter = _execute(tmp_path)
    with pytest.raises(FileExistsError):
        g8.execute_plan(
            plan_path=paths["plan"],
            plan_file_sha256=hashlib.sha256(paths["plan"].read_bytes()).hexdigest(),
            authorization_path=paths["auth"],
            authorization_sha256=hashlib.sha256(paths["auth"].read_bytes()).hexdigest(),
            token_file=paths["token"],
            output=paths["report"].with_name("replay.json"),
            adapter=adapter,
        )


@pytest.mark.parametrize("fault", ["share_root", "not_smb", "nonempty"])
def test_plan_rejects_unsafe_or_nonempty_target(tmp_path: Path, fault: str) -> None:
    paths, adapter = _layout(tmp_path)
    if fault == "share_root":
        paths["target"] = paths["mount"]
    elif fault == "not_smb":
        adapter.mount_receipt = lambda _target: _receipt(
            ["/sbin/mount"], f"/dev/disk9 on {paths['mount']} (apfs, local)\n"
        )
    else:
        (paths["target"] / "unexpected-client-file").write_text("do not touch")
    with pytest.raises(g8.G8SMBBlocked):
        g8.prepare_plan(
            target=paths["target"], release_manifest=paths["manifest"],
            service_manifest=paths["service"], ownership_manifest=paths["ownership"],
            matched_performance_report=paths["matched"],
            output=paths["plan"], token_output=paths["token"], adapter=adapter,
            samples_per_arm=10,
        )


@pytest.mark.parametrize("fault", ["v2_process", "listener", "launchd", "slow_v3", "cleanup"])
def test_execution_fails_closed_on_nonisolated_or_degraded_window(tmp_path: Path, fault: str) -> None:
    paths, adapter = _layout(tmp_path)
    prepared = g8.prepare_plan(
        target=paths["target"], release_manifest=paths["manifest"],
        service_manifest=paths["service"], ownership_manifest=paths["ownership"],
        matched_performance_report=paths["matched"],
        output=paths["plan"], token_output=paths["token"], adapter=adapter,
        samples_per_arm=10,
    )
    plan = json.loads(paths["plan"].read_text())
    phrase = f"AUTHORIZE MAGI G8 SMB {plan['plan_id']} {plan['target_sha256']}"
    auth = g8.authorize_plan(
        plan_path=paths["plan"], plan_file_sha256=prepared["plan_file_sha256"],
        output=paths["auth"], input_reader=lambda _prompt: phrase,
        local_uid=501, local_user="ai", tty_name="/dev/ttys-test",
    )
    if fault == "v2_process":
        adapter.ps += "99 scripts/ops/run_daemon_no_site.py\n"
    elif fault == "listener":
        adapter.lsof = "python 99 ai 10u IPv4 TCP *:5002 (LISTEN)\n"
    elif fault == "launchd":
        adapter.launchctl += "99 0 com.magi.v3.gateway\n"
    elif fault == "slow_v3":
        adapter.v3_multiplier = 1.06
    else:
        adapter.cleanup_entries = ["leftover"]
    with pytest.raises(g8.G8SMBBlocked):
        g8.execute_plan(
            plan_path=paths["plan"], plan_file_sha256=prepared["plan_file_sha256"],
            authorization_path=paths["auth"], authorization_sha256=auth["authorization_sha256"],
            token_file=paths["token"], output=paths["report"], adapter=adapter,
        )
    if fault == "slow_v3":
        diagnostic_path = paths["report"].with_name("report-failed-diagnostic.json")
        diagnostic = json.loads(diagnostic_path.read_text())
        assert diagnostic["status"] == "failed"
        assert diagnostic["failure"]["formal_gate_cleared"] is False
        assert diagnostic["raw_block_order"] == [
            {"block": 1, "arm": "v2", "sample_start": 0, "sample_count": 5},
            {"block": 2, "arm": "v3", "sample_start": 0, "sample_count": 5},
            {"block": 3, "arm": "v3", "sample_start": 5, "sample_count": 5},
            {"block": 4, "arm": "v2", "sample_start": 5, "sample_count": 5},
        ]


@pytest.mark.parametrize("fault", ["summary", "transcript", "mount", "artifact"])
def test_verifier_recomputes_from_raw_and_rejects_tamper(tmp_path: Path, fault: str) -> None:
    report, paths, _adapter = _execute(tmp_path)
    mutated = copy.deepcopy(report)
    if fault == "summary":
        mutated["derived"]["v3_to_v2_p95_ratio"] = 0.0
    elif fault == "transcript":
        mutated["raw_arms"]["v3"]["samples"][0]["transcript"][1]["path"] = "../escape"
    elif fault == "mount":
        mutated["raw_mount_before"]["stdout"] = mutated["raw_mount_before"]["stdout"].replace("smbfs", "apfs")
        mutated["raw_mount_before"]["stdout_sha256"] = hashlib.sha256(
            mutated["raw_mount_before"]["stdout"].encode()
        ).hexdigest()
    else:
        mutated["raw_artifacts_b64"]["service_manifest"] = "e30K"
    mutated.pop("evidence_sha256", None)
    mutated["evidence_sha256"] = g8.sha256_json(mutated)
    with pytest.raises(g8.G8SMBBlocked):
        g8.verify_report(
            mutated, expected_release_id="v3-test",
            expected_release_manifest_sha256=hashlib.sha256(paths["manifest"].read_bytes()).hexdigest(),
        )


def test_release_artifact_drift_after_plan_blocks_before_remote_write(tmp_path: Path) -> None:
    paths, adapter = _layout(tmp_path)
    prepared = g8.prepare_plan(
        target=paths["target"], release_manifest=paths["manifest"],
        service_manifest=paths["service"], ownership_manifest=paths["ownership"],
        matched_performance_report=paths["matched"],
        output=paths["plan"], token_output=paths["token"], adapter=adapter,
        samples_per_arm=10,
    )
    plan = json.loads(paths["plan"].read_text())
    phrase = f"AUTHORIZE MAGI G8 SMB {plan['plan_id']} {plan['target_sha256']}"
    auth = g8.authorize_plan(
        plan_path=paths["plan"], plan_file_sha256=prepared["plan_file_sha256"],
        output=paths["auth"], input_reader=lambda _prompt: phrase,
        local_uid=501, local_user="ai", tty_name="/dev/ttys-test",
    )
    paths["ownership"].write_text("{}\n")
    with pytest.raises(g8.G8SMBBlocked, match="changed"):
        g8.execute_plan(
            plan_path=paths["plan"], plan_file_sha256=prepared["plan_file_sha256"],
            authorization_path=paths["auth"], authorization_sha256=auth["authorization_sha256"],
            token_file=paths["token"], output=paths["report"], adapter=adapter,
        )
