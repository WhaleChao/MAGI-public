from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from magi_v3.supply_chain import (
    LOCK_SCHEMA,
    RELEASE_BINDING_SCHEMA,
    SupplyChainError,
    VULNERABILITY_SCHEMA,
    audit_runtime_install_policy,
    canonical_digest,
    cyclonedx_sbom,
    runtime_lock,
    scan_release_secrets,
    validate_vulnerability_receipt,
    validate_release_supply_chain_binding,
    pip_audit_receipt,
    verify_wheelhouse,
    wheelhouse_manifest,
)


def _components() -> list[dict]:
    return [
        {
            "name": "Example",
            "normalized_name": "example",
            "version": "1.2.3",
            "purl": "pkg:pypi/example@1.2.3",
            "metadata_sha256": "a" * 64,
            "record_sha256": "b" * 64,
            "requires_dist": [],
        }
    ]


def test_runtime_lock_and_sbom_are_deterministic() -> None:
    first = runtime_lock(python_version="3.14.0", platform="macos-arm64", components=_components())
    second = runtime_lock(python_version="3.14.0", platform="macos-arm64", components=_components())
    assert first == second
    assert first["schema"] == LOCK_SCHEMA
    sbom = cyclonedx_sbom(components=_components(), serial_seed=first["packages_sha256"])
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["components"][0]["purl"] == "pkg:pypi/example@1.2.3"


def test_wheelhouse_manifest_detects_drift(tmp_path: Path) -> None:
    wheel = tmp_path / "example-1.2.3-py3-none-any.whl"
    wheel.write_bytes(b"wheel-one")
    manifest = wheelhouse_manifest(tmp_path)
    verify_wheelhouse(tmp_path, manifest)
    wheel.write_bytes(b"tampered")
    with pytest.raises(SupplyChainError, match="drift"):
        verify_wheelhouse(tmp_path, manifest)


def test_secret_scan_rejects_credentials_but_not_normal_source(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("answer = 42\n", encoding="utf-8")
    assert scan_release_secrets(tmp_path) == []
    (tmp_path / ".env").write_text("TOKEN=not-exported\n", encoding="utf-8")
    assert scan_release_secrets(tmp_path) == ["secret_filename:.env"]


def test_secret_scan_allows_explicit_offline_fixtures_but_rejects_real_looking_values(
    tmp_path: Path,
) -> None:
    fixture = tmp_path / "tests" / "test_fixture.py"
    fixture.parent.mkdir()
    fixture.write_text(
        "token = " + repr("offline-confirmation-token-0001") + "\n",
        encoding="utf-8",
    )
    assert scan_release_secrets(tmp_path) == []

    fixture.write_text(
        "token = " + repr("9f8e7d6c5b4a3210fedcba9876543210") + "\n",
        encoding="utf-8",
    )
    assert scan_release_secrets(tmp_path) == ["secret_literal:tests/test_fixture.py"]


def test_secret_scan_never_allows_fixture_prefixes_in_production_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "api" / "service.py"
    source.parent.mkdir()
    source.write_text(
        "api_key = " + repr("test-looking-but-production-value") + "\n",
        encoding="utf-8",
    )

    assert scan_release_secrets(tmp_path) == ["secret_literal:api/service.py"]


def test_runtime_install_policy_has_only_guarded_call_sites() -> None:
    root = Path(__file__).resolve().parents[2]
    assert audit_runtime_install_policy(root) == []


def test_vulnerability_receipt_is_bound_and_rejects_high_findings() -> None:
    digest = "c" * 64
    receipt = {
        "schema": VULNERABILITY_SCHEMA,
        "scanner": "pip-audit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "packages_sha256": digest,
        "vulnerability_count": 0,
        "vulnerabilities": [],
        "ok": True,
    }
    validate_vulnerability_receipt(receipt, expected_packages_sha256=digest)
    receipt["vulnerabilities"] = [{"id": "CVE-X", "severity": "high", "status": "open"}]
    receipt["vulnerability_count"] = 1
    receipt["ok"] = False
    with pytest.raises(SupplyChainError, match="not release-clean"):
        validate_vulnerability_receipt(receipt, expected_packages_sha256=digest)


def test_pip_audit_report_normalizes_to_release_bound_zero_vulnerability_receipt() -> None:
    digest = "d" * 64
    receipt = pip_audit_receipt(
        {"dependencies": [{"name": "safe-package", "version": "1.0", "vulns": []}]},
        packages_sha256=digest,
        scanner_version="2.10.1",
    )
    assert receipt["ok"] is True
    assert receipt["packages_sha256"] == digest
    assert receipt["vulnerability_count"] == 0
    validate_vulnerability_receipt(receipt, expected_packages_sha256=digest)


def test_pip_audit_preserves_failed_receipt_but_release_validation_rejects_it() -> None:
    digest = "e" * 64
    receipt = pip_audit_receipt(
        {
            "dependencies": [
                {
                    "name": "unsafe-package",
                    "version": "1.0",
                    "vulns": [{"id": "CVE-X", "aliases": [], "fix_versions": ["2.0"]}],
                }
            ]
        },
        packages_sha256=digest,
        scanner_version="2.10.1",
    )
    assert receipt["ok"] is False
    assert receipt["vulnerability_count"] == 1
    with pytest.raises(SupplyChainError, match="not release-clean"):
        validate_vulnerability_receipt(receipt, expected_packages_sha256=digest)


def _write_release_binding(root: Path) -> tuple[Path, dict]:
    evidence = root / "config/supply-chain/test"
    evidence.mkdir(parents=True)
    lock = runtime_lock(
        python_version="3.14.0",
        platform="macos-arm64",
        components=_components(),
    )
    sbom = cyclonedx_sbom(
        components=_components(),
        serial_seed=lock["packages_sha256"],
    )
    wheels = {
        "schema": "magi.wheelhouse-manifest/v1",
        "files": [{"filename": "example.whl", "size": 5, "sha256": "f" * 64}],
    }
    wheels["files_sha256"] = canonical_digest({"files": wheels["files"]})
    vulnerability = {
        "schema": VULNERABILITY_SCHEMA,
        "scanner": "pip-audit",
        "scanner_version": "2.10.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "packages_sha256": lock["packages_sha256"],
        "dependency_count": 1,
        "vulnerability_count": 0,
        "vulnerabilities": [],
        "ok": True,
    }
    values = {
        "runtime_lock": ("python-runtime-lock.json", lock),
        "sbom": ("sbom.cdx.json", sbom),
        "wheelhouse_manifest": ("wheelhouse-manifest.json", wheels),
        "vulnerability_receipt": ("vulnerability-receipt.json", vulnerability),
    }
    descriptors = {}
    for role, (name, value) in values.items():
        path = evidence / name
        path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
        descriptors[role] = {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }
    base_manifest = root.parent / "base-runtime-manifest.json"
    base_manifest.write_text(json.dumps({"tree_sha256": "a" * 64}), encoding="utf-8")
    binding = {
        "schema": RELEASE_BINDING_SCHEMA,
        "runtime_strategy": "immutable_base_plus_hashed_offline_overlay",
        "packages_sha256": lock["packages_sha256"],
        "base_runtime": {
            "runtime_id": "runtime-test",
            "manifest_sha256": hashlib.sha256(base_manifest.read_bytes()).hexdigest(),
            "tree_sha256": "a" * 64,
        },
        "artifacts": descriptors,
    }
    binding["binding_sha256"] = canonical_digest(binding)
    (root / "config/v3_supply_chain_binding.json").write_text(
        json.dumps(binding, sort_keys=True), encoding="utf-8"
    )
    return base_manifest, binding


def test_release_binding_covers_lock_sbom_overlay_and_clean_vulnerability_receipt(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    (release / "config").mkdir(parents=True)
    base_manifest, binding = _write_release_binding(release)

    summary = validate_release_supply_chain_binding(
        release,
        base_runtime_manifest=base_manifest,
    )

    assert summary["ok"] is True
    assert summary["binding_sha256"] == binding["binding_sha256"]
    assert summary["package_count"] == 1
    assert summary["wheel_count"] == 1
    assert summary["base_runtime"]["externally_verified"] is True


def test_release_binding_rejects_artifact_tampering(tmp_path: Path) -> None:
    release = tmp_path / "release"
    (release / "config").mkdir(parents=True)
    _base_manifest, binding = _write_release_binding(release)
    lock = release / binding["artifacts"]["runtime_lock"]["path"]
    lock.write_text("{}\n", encoding="utf-8")

    with pytest.raises(SupplyChainError, match="digest mismatch"):
        validate_release_supply_chain_binding(release)
