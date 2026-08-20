from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import scripts.v3_validation.controlled_restart_evidence as controlled
from scripts.v3_validation.controlled_restart_evidence import (
    ControlledRestartBlocked,
    HostBackend,
    HostObservation,
    REQUIRED_ENDPOINTS,
    finalize_plan,
    prepare_plan,
    verify_report,
)


ROOT = Path(__file__).resolve().parents[2]
CTX = {
    "campaign_id": "restart-campaign",
    "release_sha": "1" * 64,
    "hardware_id": "test-mac",
    "gate_config_sha256": "2" * 64,
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, value: object) -> str:
    data = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _release(tmp_path: Path) -> tuple[Path, str]:
    release = tmp_path / "release"
    source = release / "scripts/v3_validation/controlled_restart_evidence.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(
        (ROOT / "scripts/v3_validation/controlled_restart_evidence.py").read_bytes()
    )
    manifest = {
        "immutable": True,
        "files": [
            {
                "path": "scripts/v3_validation/controlled_restart_evidence.py",
                "sha256": _sha(source),
            }
        ],
    }
    manifest_path = release / "release-manifest.json"
    return release, _write(manifest_path, manifest)


def _observation(
    session: str,
    boot_time: float,
    *,
    v3_labels: tuple[str, ...] = (),
    v3_pids: tuple[int, ...] = (),
) -> HostObservation:
    return HostObservation(
        boot_session_uuid=session,
        boot_time_epoch=boot_time,
        endpoints=tuple(
            {
                "port": port,
                "path": path,
                "status": 200,
                "body_sha256": hashlib.sha256(f"{port}{path}".encode()).hexdigest(),
            }
            for port, path in REQUIRED_ENDPOINTS
        ),
        listener_pids={port: (10_000 + port,) for port, _path in REQUIRED_ENDPOINTS},
        v3_loaded_labels=v3_labels,
        v3_process_pids=v3_pids,
    )


class FakeBackend:
    def __init__(self, observations: list[HostObservation]) -> None:
        self.observations = observations

    def observe(self, _release_root: Path) -> HostObservation:
        assert self.observations
        return self.observations.pop(0)


def test_host_process_probe_excludes_only_observer_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process_rows = "\n".join(
        (
            " 10 1 /bin/zsh /candidate/release/certifier-wrapper",
            " 100 10 /candidate/release/venv/bin/python controlled_restart_evidence.py",
            " 101 100 /usr/sbin/lsof /candidate/release",
            " 200 1 /candidate/release/venv/bin/python -m magi_v3.gateway",
            " 201 1 /usr/bin/logger MAGI_v3 documentation only",
        )
    )
    monkeypatch.setattr(controlled.os, "getpid", lambda: 100)
    monkeypatch.setattr(
        HostBackend,
        "_run",
        staticmethod(
            lambda _argv, timeout=10: controlled.subprocess.CompletedProcess(
                _argv, 0, process_rows, ""
            )
        ),
    )
    assert HostBackend._v3_processes(Path("/candidate/release")) == (200,)


def _prepare(tmp_path: Path, before: HostObservation) -> tuple[Path, str, Path]:
    release, manifest_sha = _release(tmp_path)
    plan = tmp_path / "evidence/plan.json"
    result = prepare_plan(
        release_root=release,
        release_manifest_sha256=manifest_sha,
        output=plan,
        workdir=tmp_path / "evidence/workdir",
        authorization_statement_sha256="3" * 64,
        expected_context=CTX,
        backend=FakeBackend([before]),
        now=datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc),
    )
    return plan, result["plan_file_sha256"], release


def test_prepare_and_finalize_prove_changed_boot_and_v2_restoration(tmp_path: Path) -> None:
    before = _observation("AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", 1_784_760_000.0)
    after = _observation("BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", 1_784_768_500.0)
    plan_path, plan_sha, _release_root = _prepare(tmp_path, before)
    output = tmp_path / "evidence/report.json"

    report = finalize_plan(
        plan_path=plan_path,
        plan_sha256=plan_sha,
        output=output,
        backend=FakeBackend([after]),
        now=datetime(2026, 7, 23, 1, 5, tzinfo=timezone.utc),
    )

    assert report["status"] == "passed"
    assert report["claims"]["controlled_cold_restart_verified"] is True
    assert report["claims"]["v2_readiness_restored"] is True
    assert report["claims"]["v3_absent_after_restart"] is True
    assert report["safety"]["hard_power_removal_required"] is False
    assert report["safety"]["external_device_required"] is False
    plan = json.loads(plan_path.read_text())
    verify_report(report, plan=plan, plan_sha256=plan_sha)
    assert output.stat().st_mode & 0o222 == 0


def test_finalize_rejects_same_boot_session(tmp_path: Path) -> None:
    before = _observation("AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", 1_784_760_000.0)
    plan_path, plan_sha, _release_root = _prepare(tmp_path, before)
    output = tmp_path / "evidence/report.json"

    with pytest.raises(ControlledRestartBlocked, match="later macOS boot session"):
        finalize_plan(
            plan_path=plan_path,
            plan_sha256=plan_sha,
            output=output,
            backend=FakeBackend([before]),
            now=datetime(2026, 7, 23, 1, 5, tzinfo=timezone.utc),
        )
    assert not output.exists()


def test_prepare_and_finalize_fail_closed_on_v3_or_duplicate_owner(tmp_path: Path) -> None:
    unsafe = _observation(
        "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        100.0,
        v3_labels=("com.magi.v3.gateway",),
    )
    release, manifest_sha = _release(tmp_path)
    with pytest.raises(ControlledRestartBlocked, match="single-owner"):
        prepare_plan(
            release_root=release,
            release_manifest_sha256=manifest_sha,
            output=tmp_path / "evidence/plan.json",
            workdir=tmp_path / "evidence/workdir",
            authorization_statement_sha256="3" * 64,
            expected_context=CTX,
            backend=FakeBackend([unsafe]),
        )

    before = _observation("BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", 1_784_768_300.0)
    plan_path, plan_sha, _release_root = _prepare(tmp_path / "second", before)
    duplicate = _observation("CCCCCCCC-CCCC-CCCC-CCCC-CCCCCCCCCCCC", 1_784_769_000.0)
    duplicate.listener_pids[5002] = (1, 2)
    with pytest.raises(ControlledRestartBlocked, match="single-owner"):
        finalize_plan(
            plan_path=plan_path,
            plan_sha256=plan_sha,
            output=tmp_path / "second/evidence/report.json",
            backend=FakeBackend([duplicate]),
            now=datetime(2026, 7, 23, 1, 5, tzinfo=timezone.utc),
        )


def test_plan_and_report_tampering_are_rejected(tmp_path: Path) -> None:
    before = _observation("AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA", 1_784_760_000.0)
    after = _observation("BBBBBBBB-BBBB-BBBB-BBBB-BBBBBBBBBBBB", 1_784_768_500.0)
    plan_path, plan_sha, _release_root = _prepare(tmp_path, before)
    with pytest.raises(ControlledRestartBlocked, match="SHA-256 mismatch"):
        finalize_plan(
            plan_path=plan_path,
            plan_sha256="0" * 64,
            output=tmp_path / "evidence/rejected.json",
            backend=FakeBackend([after]),
        )

    output = tmp_path / "evidence/report.json"
    report = finalize_plan(
        plan_path=plan_path,
        plan_sha256=plan_sha,
        output=output,
        backend=FakeBackend([after]),
        now=datetime(2026, 7, 23, 1, 5, tzinfo=timezone.utc),
    )
    tampered = copy.deepcopy(report)
    tampered["claims"]["v3_absent_after_restart"] = False
    plan = json.loads(plan_path.read_text())
    with pytest.raises(ControlledRestartBlocked, match="not certifying"):
        verify_report(tampered, plan=plan, plan_sha256=plan_sha)
