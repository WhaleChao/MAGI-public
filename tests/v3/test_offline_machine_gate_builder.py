from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts.v3_validation import offline_machine_gate_builder as builder
from scripts.v3_validation.isolated_live_execute import (
    DEPLOYMENT_MODE,
    OFFLINE_MACHINE_EVIDENCE,
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes | str, *, mode: int = 0o644) -> str:
    raw = data.encode() if isinstance(data, str) else data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)
    return _sha(raw)


def _json(path: Path, payload: Any) -> str:
    return _write(
        path,
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
    )


@dataclass(frozen=True)
class BuilderFixture:
    candidate: builder.CandidateBinding
    campaign: Path
    backup: Path
    marker: Path
    evidence_dir: Path
    output: Path
    context: dict[str, str]


def _fixture(tmp_path: Path) -> BuilderFixture:
    root = (tmp_path / "candidate").resolve()
    gate = root / "config/v3_cutover_gates.json"
    gate_sha = _json(gate, {"schema_version": 1, "required_evidence": []})
    launcher = root / "bin/magi-v3-python"
    launcher_sha = _write(launcher, "#!/bin/sh\nexit 0\n", mode=0o755)
    builder_source = root / "scripts/v3_validation/offline_machine_gate_builder.py"
    builder_sha = _write(builder_source, "SCHEMA = 'magi.v3.offline-machine-gate/v1'\n")
    release_sha = "a" * 64
    release_manifest = root / "release-manifest.json"
    release_manifest_sha = _json(
        release_manifest,
        {
            "schema_version": 1,
            "release_id": "v3-offline-test",
            "immutable": True,
            "release_sha256": release_sha,
            "source_snapshot_sha256": release_sha,
            "files": [],
        },
    )
    candidate = builder.CandidateBinding(
        root=root,
        launcher=launcher,
        release_manifest=builder._freeze(release_manifest, "test release manifest"),
        release_id="v3-offline-test",
        release_sha=release_sha,
        file_hashes={
            "bin/magi-v3-python": launcher_sha,
            "scripts/v3_validation/offline_machine_gate_builder.py": builder_sha,
        },
        python_runtime_sha256="b" * 64,
    )
    deploy = (tmp_path / "deploy").resolve()
    deploy_manifest = deploy / "deploy-manifest.json"
    deploy_sha = _json(
        deploy_manifest,
        {
            "schema_version": 1,
            "status": "prepared_not_installed",
            "mutation_performed": False,
            "deployment_mode": DEPLOYMENT_MODE,
            "release_id": candidate.release_id,
            "release_manifest": str(release_manifest),
            "release_manifest_sha256": release_manifest_sha,
        },
    )
    marker = deploy / "DEPLOY_PREPARED.json"
    _json(
        marker,
        {
            "schema_version": 1,
            "status": "prepared_not_installed",
            "ready_to_install": True,
            "mutation_performed": False,
            "deployment_mode": DEPLOYMENT_MODE,
            "release_id": candidate.release_id,
            "release_manifest_sha256": release_manifest_sha,
            "manifest": deploy_manifest.name,
            "manifest_sha256": deploy_sha,
        },
    )
    campaign = (tmp_path / "campaign.json").resolve()
    backup = (tmp_path / "backup.json").resolve()
    _json(campaign, {"schema_version": 1})
    _json(backup, {"schema_version": 1})
    return BuilderFixture(
        candidate=candidate,
        campaign=campaign,
        backup=backup,
        marker=marker,
        evidence_dir=(tmp_path / "normalized-evidence").resolve(),
        output=(tmp_path / "offline-gate.json").resolve(),
        context={
            "campaign_id": "campaign-test",
            "release_sha": release_sha,
            "hardware_id": "test-hardware",
            "gate_config_sha256": gate_sha,
        },
    )


class FakeCodeOwnedRunner:
    def __init__(
        self,
        context: dict[str, str],
        *,
        missing: str | None = None,
        failed: str | None = None,
        tamper: str | None = None,
        generated_at: datetime | None = None,
    ) -> None:
        self.context = context
        self.missing = missing
        self.failed = failed
        self.tamper = tamper
        self.generated_at = generated_at or datetime.now(timezone.utc)
        self.argvs: list[list[str]] = []

    @staticmethod
    def _value(argv: list[str], flag: str) -> str:
        return argv[argv.index(flag) + 1]

    def __call__(self, argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.argvs.append(argv)
        if argv[1].endswith("v3_evidence_compiler.py"):
            output = Path(self._value(argv, "--output"))
            emitted: dict[str, str] = {}
            for evidence_id in sorted(OFFLINE_MACHINE_EVIDENCE):
                if evidence_id == self.missing:
                    continue
                producer = output / "reports" / f"{evidence_id}.json"
                producer_sha = _json(
                    producer,
                    {"schema_version": 1, "evidence_id": evidence_id, **self.context},
                )
                _json(
                    output / f"{evidence_id}.json",
                    {
                        "schema_version": 1,
                        "evidence_id": evidence_id,
                        "status": "failed" if evidence_id == self.failed else "passed",
                        "generated_at": self.generated_at.isoformat(),
                        **self.context,
                        "artifacts": [
                            {
                                "role": "producer_report",
                                "path": f"reports/{evidence_id}.json",
                                "sha256": producer_sha,
                            }
                        ],
                    },
                )
                emitted[evidence_id] = "failed" if evidence_id == self.failed else "passed"
            _json(
                output / "evidence-compile-summary.json",
                {
                    "schema_version": 1,
                    "generated_at": self.generated_at.isoformat(),
                    **self.context,
                    "normalizer": "scripts.v3_evidence_compiler",
                    "service_start_performed": False,
                    "live_state_accessed": False,
                    "emitted": emitted,
                    "decision": "EVIDENCE_INCOMPLETE",
                },
            )
            return subprocess.CompletedProcess(argv, 1, "", "")

        evidence_dir = Path(self._value(argv, "--evidence-dir"))
        output = Path(self._value(argv, "--output"))
        passed = set(OFFLINE_MACHINE_EVIDENCE)
        missing = ["human_go_approval_recorded"]
        invalid: dict[str, list[str]] = {}
        if self.missing:
            passed.remove(self.missing)
            missing.append(self.missing)
        failed: list[str] = []
        if self.failed:
            passed.remove(self.failed)
            failed.append(self.failed)
        if self.tamper:
            passed.remove(self.tamper)
            invalid[self.tamper] = ["artifact SHA-256 mismatch"]
            (evidence_dir / "reports" / f"{self.tamper}.json").write_text(
                "tampered\n", encoding="utf-8"
            )
        _json(
            output,
            {
                "schema_version": 1,
                "decision": "NO_GO",
                "fail_closed": True,
                "expected_context": self.context,
                "required_count": 28,
                "passed": sorted(passed),
                "missing": missing,
                "failed": failed,
                "invalid": invalid,
            },
        )
        return subprocess.CompletedProcess(argv, 2, "", "")


def _build(fixture: BuilderFixture, runner: FakeCodeOwnedRunner, *, now: datetime | None = None) -> dict[str, Any]:
    return builder.build_from_verified_candidate(
        candidate=fixture.candidate,
        campaign_report=fixture.campaign,
        backup_metadata=fixture.backup,
        deploy_prepared_marker=fixture.marker,
        evidence_dir=fixture.evidence_dir,
        output=fixture.output,
        context=fixture.context,
        runner=runner,
        now=now,
    )


def test_builder_emits_go_only_for_all_19_code_owned_passes(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    runner = FakeCodeOwnedRunner(fixture.context)

    report = _build(fixture, runner)

    assert report["status"] == "GO"
    assert report["counts"] == {
        "required": 19,
        "passed": 19,
        "failed": 0,
        "missing": 0,
        "invalid": 0,
    }
    assert report["unproven_gaps"] == []
    assert len(runner.argvs) == 2
    assert all(argv[0] == str(fixture.candidate.launcher) for argv in runner.argvs)
    assert runner.argvs[0][1].endswith("scripts/v3_evidence_compiler.py")
    assert runner.argvs[1][1].endswith("scripts/v3_release_gate.py")


@pytest.mark.parametrize("mode", ["missing", "failed", "tamper", "stale"])
def test_builder_fail_closes_incomplete_or_untrustworthy_evidence(
    tmp_path: Path, mode: str
) -> None:
    fixture = _fixture(tmp_path)
    target = sorted(OFFLINE_MACHINE_EVIDENCE)[0]
    now = datetime.now(timezone.utc)
    runner = FakeCodeOwnedRunner(
        fixture.context,
        missing=target if mode == "missing" else None,
        failed=target if mode == "failed" else None,
        tamper=target if mode == "tamper" else None,
        generated_at=now - timedelta(hours=25) if mode == "stale" else now,
    )

    report = _build(fixture, runner, now=now)

    assert report["status"] == "NO_GO"
    assert report["unproven_gaps"]
    assert report["counts"]["passed"] < 19


def test_builder_rejects_compiler_context_drift(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    runner = FakeCodeOwnedRunner({**fixture.context, "hardware_id": "another-mac"})

    with pytest.raises(builder.OfflineMachineGateError, match="context/safety"):
        _build(fixture, runner)


def test_candidate_runtime_binding_rejects_wrong_launcher_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = (tmp_path / "candidate-runtime").resolve()
    context = {
        "campaign_id": "campaign-runtime",
        "release_sha": "c" * 64,
        "hardware_id": "test-mac",
        "gate_config_sha256": "d" * 64,
    }
    inventory: list[dict[str, str]] = []
    for relative in builder.REQUIRED_RELEASE_SOURCES:
        path = root / relative
        content = b"{}\n" if path.suffix == ".json" else b"# test\n"
        inventory.append({"path": relative, "sha256": _write(path, content)})
    launcher = root / "bin/magi-v3-python"
    inventory.append(
        {"path": "bin/magi-v3-python", "sha256": _write(launcher, "#!/bin/sh\nexit 0\n", mode=0o755)}
    )
    manifest = root / "release-manifest.json"
    manifest_sha = _json(
        manifest,
        {
            "schema_version": 1,
            "release_id": "v3-runtime-test",
            "immutable": True,
            "release_sha256": context["release_sha"],
            "source_snapshot_sha256": context["release_sha"],
            "files": inventory,
        },
    )
    runtime = Path(sys.executable).resolve(strict=True)
    runtime_sha = builder._hash_runtime(runtime)
    monkeypatch.setattr(
        builder,
        "__file__",
        str(root / "scripts/v3_validation/offline_machine_gate_builder.py"),
    )
    monkeypatch.setenv("MAGI_V3_RELEASE_MANIFEST", str(manifest))
    monkeypatch.setenv("MAGI_V3_RELEASE_MANIFEST_SHA256", manifest_sha)
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME_SHA256", runtime_sha)
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME_REALPATH", str(runtime))
    monkeypatch.setenv("MAGI_ROOT", str(tmp_path.resolve()))

    with pytest.raises(builder.OfflineMachineGateError, match="exact candidate"):
        builder.verify_candidate_runtime(root, context)

    monkeypatch.setenv("MAGI_ROOT", str(root))
    binding = builder.verify_candidate_runtime(root, context)
    assert binding.release_manifest.sha256 == manifest_sha
    assert binding.python_runtime_sha256 == runtime_sha
