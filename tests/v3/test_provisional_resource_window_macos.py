from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.v3_validation import provisional_resource_window_macos as module


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sealed(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    path.chmod(0o400)
    return path.resolve()


def test_cli_verifies_provisional_gate_before_building_real_machine(
    tmp_path: Path, monkeypatch
) -> None:
    release = _sealed(tmp_path / "release.json", {"release": True})
    gate = _sealed(tmp_path / "gate.json", {"gate": True})
    outer = _sealed(tmp_path / "outer.json", {})
    inner = _sealed(tmp_path / "inner.json", {})
    deploy = _sealed(tmp_path / "deploy.json", {})
    marker = _sealed(tmp_path / "marker.json", {})
    outer_token = tmp_path / "outer.token"
    inner_token = tmp_path / "inner.token"
    outer_token.write_text("outer", encoding="utf-8")
    inner_token.write_text("inner", encoding="utf-8")
    outer_token.chmod(0o600)
    inner_token.chmod(0o600)
    observed: dict[str, object] = {}

    def fake_verify_plan(*_args):
        observed["gate_verified"] = True
        return (
            {
                "release_manifest": {"path": str(release), "sha256": _sha(release)},
                "provisional_gate_report": {"path": str(gate), "sha256": _sha(gate)},
            },
            {},
        )

    def fake_static(plan, *, require_offline_machine_gate=True):
        observed["static_plan"] = plan
        observed["requires_19"] = require_offline_machine_gate
        return object()

    class FakeMachine:
        def __init__(self, deployment, *, artifact_directory):
            observed["deployment"] = deployment
            observed["artifact_directory"] = artifact_directory

    class FakeExecutor:
        def __init__(self, **kwargs):
            observed["executor"] = kwargs

        def execute(self):
            return {"status": "window_completed_v2_restored", "ok": True, "v2_restored": True}

    monkeypatch.setattr(module, "verify_plan", fake_verify_plan)
    monkeypatch.setattr(module, "verify_static_plan", fake_static)
    monkeypatch.setattr(module, "MacOSIsolatedLiveMachine", FakeMachine)
    monkeypatch.setattr(module, "ProvisionalResourceWindowExecutor", FakeExecutor)
    report = (tmp_path / "report.json").resolve()

    returncode = module.main(
        [
            "--outer-plan", str(outer),
            "--outer-plan-sha256", _sha(outer),
            "--outer-token", str(outer_token.resolve()),
            "--inner-plan", str(inner),
            "--inner-plan-sha256", _sha(inner),
            "--inner-token", str(inner_token.resolve()),
            "--deploy-manifest", str(deploy),
            "--deploy-manifest-sha256", _sha(deploy),
            "--deploy-prepared-marker", str(marker),
            "--deploy-prepared-marker-sha256", _sha(marker),
            "--artifact-directory", str((tmp_path / "artifacts").resolve()),
            "--collector-output", str((tmp_path / "collector.json").resolve()),
            "--report-output", str(report),
        ]
    )

    assert returncode == 0
    assert observed["gate_verified"] is True
    assert observed["requires_19"] is False
    assert report.stat().st_mode & 0o777 == 0o400
    assert json.loads(report.read_text())["v2_restored"] is True
