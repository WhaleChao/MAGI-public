from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from magi_v3.errors import ConfigurationError
from magi_v3.service_manifest import load_bound_service_manifest, load_service_manifest
from magi_v3.service_runtime import ServiceIdentity, ServiceRuntimeError


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "config" / "v3_service_manifest.json"
VALIDATION_MANIFEST = ROOT / "config" / "v3_live_validation_service_manifest.json"


def test_production_service_manifest_has_exact_single_owner_topology() -> None:
    manifest = load_service_manifest(MANIFEST)

    assert manifest.deployment_mode == "production"
    assert {service.port: service.role for service in manifest.services if service.port} == {
        5002: "gateway",
        5003: "gateway",
        8088: "control",
    }
    assert {service.service_id for service in manifest.for_role("supervisor")} == {
        "discord",
        "file_review_auto",
        "heartbeat",
        "legacy_background",
        "osc_shell_nas_helper",
        "menubar",
    }
    assert all(service.required for service in manifest.services)
    assert manifest.forbidden_release_processes == ("daemon.py",)


def test_isolated_live_validation_manifest_has_no_production_background_children() -> None:
    manifest = load_service_manifest(VALIDATION_MANIFEST)

    assert manifest.deployment_mode == "isolated_live_validation"
    assert {service.port for service in manifest.services if service.port} == {5002, 5003, 8088}
    children = manifest.for_role("supervisor")
    assert len(children) == 1
    assert children[0].service_id == "live_validation_probe"
    assert children[0].argv == ("{python}", "magi_v3/live_validation_probe_service.py")
    assert not {
        "discord",
        "file_review_auto",
        "heartbeat",
        "legacy_background",
        "osc_shell_nas_helper",
        "menubar",
    }.intersection(service.service_id for service in manifest.services)


def test_runtime_manifest_selection_is_release_hash_and_safety_bound(tmp_path: Path) -> None:
    release = tmp_path / "release"
    selected = release / "config" / VALIDATION_MANIFEST.name
    selected.parent.mkdir(parents=True)
    selected.write_bytes(VALIDATION_MANIFEST.read_bytes())
    digest = hashlib.sha256(selected.read_bytes()).hexdigest()
    release_manifest = release / "release-manifest.json"
    release_manifest.write_text("{}\n", encoding="utf-8")
    identity = ServiceIdentity(
        role="gateway",
        release_id="v3-test",
        release_root=release,
        release_manifest=release_manifest,
        release_manifest_sha256="0" * 64,
        runtime_root=tmp_path / "runtime",
        pid_file=tmp_path / "runtime" / "pids" / "gateway.pid",
        release_files={f"config/{selected.name}": digest},
    )
    environment = {
        "MAGI_V3_DEPLOYMENT_MODE": "isolated_live_validation",
        "MAGI_V3_SERVICE_MANIFEST": str(selected),
        "MAGI_V3_SERVICE_MANIFEST_SHA256": digest,
        "MAGI_V3_LIVE_VALIDATION": "1",
        "MAGI_V3_EXTERNAL_WRITES_ENABLED": "0",
        "MAGI_V3_NOTIFICATIONS_ENABLED": "0",
        "MAGI_V3_SCHEDULER_ENABLED": "0",
    }

    path, manifest = load_bound_service_manifest(identity, environment)
    assert path == selected.resolve()
    assert manifest.deployment_mode == "isolated_live_validation"

    with pytest.raises(ServiceRuntimeError, match="safety binding mismatch"):
        load_bound_service_manifest(
            identity,
            {**environment, "MAGI_V3_SCHEDULER_ENABLED": "1"},
        )
    with pytest.raises(ServiceRuntimeError, match="SHA-256 mismatch"):
        load_bound_service_manifest(
            identity,
            {**environment, "MAGI_V3_SERVICE_MANIFEST_SHA256": "f" * 64},
        )
    outside = tmp_path / "outside.json"
    outside.write_bytes(selected.read_bytes())
    with pytest.raises(ServiceRuntimeError, match="inside the immutable release"):
        load_bound_service_manifest(
            identity,
            {**environment, "MAGI_V3_SERVICE_MANIFEST": str(outside)},
        )


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda data: data["services"].append(dict(data["services"][0])), "duplicate service"),
        (lambda data: data["services"][0].update(port=8088), "wrong role|duplicate"),
        (lambda data: data["services"].pop(1), "missing required production ports"),
        (
            lambda data: next(row for row in data["services"] if row["kind"] == "process").update(
                argv=["/usr/bin/python3", "../../daemon.py"]
            ),
            "release Python placeholder",
        ),
    ],
)
def test_manifest_ownership_and_process_escape_fail_closed(
    tmp_path: Path,
    mutation,
    match: str,
) -> None:
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    mutation(data)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=match):
        load_service_manifest(path)
