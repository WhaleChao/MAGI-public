from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

import scripts.v3_static_external_staging as staging_module
from scripts.v3_static_external_staging import (
    RECEIPT_NAME,
    StaticExternalStagingError,
    main,
    snapshot_static_sources,
    stage_static_external,
    verify_static_external,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, payload: bytes, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)
    return path.resolve()


def _write_json(path: Path, payload: object, mode: int = 0o600) -> Path:
    return _write(path, (json.dumps(payload, sort_keys=True) + "\n").encode(), mode)


def _fixture(tmp_path: Path) -> tuple[Path, str, dict[str, Path]]:
    sources = tmp_path / "v2-sources"
    paths = {
        "environment": _write(sources / ".env", b"PRIVATE_TOKEN=do-not-record\n"),
        "runtime_config": _write(sources / "config.json", b'{"laf":"private"}\n'),
        "google_credentials": _write(
            sources / "credentials.json", b'{"client_secret":"generic"}\n'
        ),
        "accounting_credentials": _write(
            sources / "accounting.json", b'{"private_key":"accounting"}\n'
        ),
    }
    website = sources / "website"
    (website / "assets/empty").mkdir(parents=True)
    _write(website / "index.html", b"<h1>OSC</h1>\n", 0o644)
    _write(website / "assets/app.js", b"console.log('ok')\n", 0o644)
    paths["website"] = website.resolve()
    release_manifest = _write_json(
        tmp_path / "release/release-manifest.json",
        {
            "schema_version": 1,
            "release_id": "v3-test-rc1",
            "immutable": True,
            "source_snapshot_sha256": "a" * 64,
            "release_sha256": "a" * 64,
            "files": [],
        },
    )
    return release_manifest, _sha(release_manifest), paths


def _source_kwargs(paths: dict[str, Path]) -> dict[str, Path]:
    return {
        "env_file": paths["environment"],
        "website_root": paths["website"],
        "config_file": paths["runtime_config"],
        "google_credentials_file": paths["google_credentials"],
        "accounting_credentials_file": paths["accounting_credentials"],
    }


def _stage(tmp_path: Path) -> tuple[Path, str, dict[str, Path], Path, dict[str, object]]:
    release, release_sha, paths = _fixture(tmp_path)
    snapshot = snapshot_static_sources(
        release,
        expected_release_manifest_sha256=release_sha,
        **_source_kwargs(paths),
    )
    target = tmp_path / "runtime/shared/external"
    report = stage_static_external(
        release,
        expected_release_manifest_sha256=release_sha,
        expected_source_snapshot_sha256=snapshot["source_snapshot_sha256"],
        target_root=target,
        **_source_kwargs(paths),
    )
    return release, release_sha, paths, target, report


def test_stage_has_no_deploy_prerequisite_and_reverifies_exact_five_inputs(
    tmp_path: Path,
) -> None:
    release, release_sha, paths = _fixture(tmp_path)
    assert not list(tmp_path.rglob("deploy-manifest.json"))
    snapshot = snapshot_static_sources(
        release,
        expected_release_manifest_sha256=release_sha,
        **_source_kwargs(paths),
    )
    target = tmp_path / "runtime/shared/external"
    report = stage_static_external(
        release,
        expected_release_manifest_sha256=release_sha,
        expected_source_snapshot_sha256=snapshot["source_snapshot_sha256"],
        target_root=target,
        **_source_kwargs(paths),
    )

    assert report["source_verified"] is True
    assert report["source_snapshot_sha256"] == report["target_snapshot_sha256"]
    assert len(report["logical_inputs"]) == 5
    assert target.stat().st_mode & 0o777 == 0o700
    for leaf in (
        ".env", "config.json", "google-credentials.json",
        "accounting-credentials.json", "website/index.html",
        "website/assets/app.js", RECEIPT_NAME,
    ):
        assert (target / leaf).stat().st_mode & 0o777 == 0o600
    assert (target / "website/assets/empty").stat().st_mode & 0o777 == 0o700
    receipt_text = (target / RECEIPT_NAME).read_text()
    assert "do-not-record" not in receipt_text
    assert all(str(path) not in receipt_text for path in paths.values())
    assert json.loads(receipt_text)["context"] == {
        "release_id": "v3-test-rc1",
        "release_manifest_sha256": release_sha,
        "release_snapshot_sha256": "a" * 64,
    }


def test_source_snapshot_must_be_prebound_and_source_drift_is_detected(tmp_path: Path) -> None:
    release, release_sha, paths = _fixture(tmp_path)
    snapshot = snapshot_static_sources(
        release,
        expected_release_manifest_sha256=release_sha,
        **_source_kwargs(paths),
    )
    with pytest.raises(StaticExternalStagingError, match="source snapshot SHA-256 mismatch"):
        stage_static_external(
            release,
            expected_release_manifest_sha256=release_sha,
            expected_source_snapshot_sha256="f" * 64,
            target_root=tmp_path / "wrong-hash",
            **_source_kwargs(paths),
        )

    paths["environment"].write_bytes(b"PRIVATE_TOKEN=drift\n")
    with pytest.raises(StaticExternalStagingError, match="source snapshot SHA-256 mismatch"):
        stage_static_external(
            release,
            expected_release_manifest_sha256=release_sha,
            expected_source_snapshot_sha256=snapshot["source_snapshot_sha256"],
            target_root=tmp_path / "drifted",
            **_source_kwargs(paths),
        )


def test_existing_target_requires_exact_refresh_snapshot(tmp_path: Path) -> None:
    release, release_sha, paths, target, initial = _stage(tmp_path)
    snapshot = snapshot_static_sources(
        release,
        expected_release_manifest_sha256=release_sha,
        **_source_kwargs(paths),
    )
    with pytest.raises(StaticExternalStagingError, match="target exists"):
        stage_static_external(
            release,
            expected_release_manifest_sha256=release_sha,
            expected_source_snapshot_sha256=snapshot["source_snapshot_sha256"],
            target_root=target,
            **_source_kwargs(paths),
        )
    with pytest.raises(StaticExternalStagingError, match="refresh target snapshot"):
        stage_static_external(
            release,
            expected_release_manifest_sha256=release_sha,
            expected_source_snapshot_sha256=snapshot["source_snapshot_sha256"],
            target_root=target,
            refresh_expected_target_snapshot_sha256="e" * 64,
            **_source_kwargs(paths),
        )
    refreshed = stage_static_external(
        release,
        expected_release_manifest_sha256=release_sha,
        expected_source_snapshot_sha256=snapshot["source_snapshot_sha256"],
        target_root=target,
        refresh_expected_target_snapshot_sha256=initial["target_snapshot_sha256"],
        **_source_kwargs(paths),
    )
    assert refreshed["target_snapshot_sha256"] == initial["target_snapshot_sha256"]


@pytest.mark.parametrize(
    "logical", ["environment", "runtime_config", "google_credentials", "accounting_credentials"]
)
def test_file_source_symlinks_are_rejected(tmp_path: Path, logical: str) -> None:
    release, release_sha, paths = _fixture(tmp_path)
    original = paths[logical]
    real = original.with_name(original.name + ".real")
    original.rename(real)
    original.symlink_to(real)
    paths[logical] = original
    with pytest.raises(StaticExternalStagingError, match="canonical and non-symlinked"):
        snapshot_static_sources(
            release,
            expected_release_manifest_sha256=release_sha,
            **_source_kwargs(paths),
        )


def test_website_symlink_special_file_and_target_symlink_are_rejected(tmp_path: Path) -> None:
    release, release_sha, paths = _fixture(tmp_path)
    (paths["website"] / "escape").symlink_to(tmp_path)
    with pytest.raises(StaticExternalStagingError, match="path escape"):
        snapshot_static_sources(
            release,
            expected_release_manifest_sha256=release_sha,
            **_source_kwargs(paths),
        )
    (paths["website"] / "escape").unlink()
    fifo = paths["website"] / "special"
    os.mkfifo(fifo)
    with pytest.raises(StaticExternalStagingError, match="only regular"):
        snapshot_static_sources(
            release,
            expected_release_manifest_sha256=release_sha,
            **_source_kwargs(paths),
        )
    fifo.unlink()
    real_target = tmp_path / "real-target"
    real_target.mkdir()
    linked_target = tmp_path / "linked-target"
    linked_target.symlink_to(real_target)
    snapshot = snapshot_static_sources(
        release,
        expected_release_manifest_sha256=release_sha,
        **_source_kwargs(paths),
    )
    with pytest.raises(StaticExternalStagingError, match="must not be a symlink"):
        stage_static_external(
            release,
            expected_release_manifest_sha256=release_sha,
            expected_source_snapshot_sha256=snapshot["source_snapshot_sha256"],
            target_root=linked_target,
            **_source_kwargs(paths),
        )


def test_target_and_receipt_tampering_fail_closed(tmp_path: Path) -> None:
    release, release_sha, _paths, target, report = _stage(tmp_path)
    (target / "config.json").chmod(0o644)
    with pytest.raises(StaticExternalStagingError, match="mode/owner"):
        verify_static_external(
            release,
            expected_release_manifest_sha256=release_sha,
            target_root=target,
        )
    (target / "config.json").chmod(0o600)
    receipt = json.loads((target / RECEIPT_NAME).read_text())
    receipt["context"]["release_id"] = "wrong"
    _write_json(target / RECEIPT_NAME, receipt)
    with pytest.raises(StaticExternalStagingError, match="release context mismatch"):
        verify_static_external(
            release,
            expected_release_manifest_sha256=release_sha,
            target_root=target,
            expected_target_snapshot_sha256=report["target_snapshot_sha256"],
        )


def test_source_and_target_snapshots_can_be_reverified_independently(tmp_path: Path) -> None:
    release, release_sha, paths, target, report = _stage(tmp_path)
    target_only = verify_static_external(
        release,
        expected_release_manifest_sha256=release_sha,
        target_root=target,
        expected_target_snapshot_sha256=report["target_snapshot_sha256"],
    )
    assert target_only["source_verified"] is False
    paths["website"].joinpath("index.html").write_bytes(b"drift\n")
    with pytest.raises(StaticExternalStagingError, match="source snapshot no longer matches"):
        verify_static_external(
            release,
            expected_release_manifest_sha256=release_sha,
            target_root=target,
            source_paths=paths,
            expected_source_snapshot_sha256=report["source_snapshot_sha256"],
        )


def test_shared_payload_receipt_is_not_rebound_when_next_release_is_prepared(
    tmp_path: Path,
) -> None:
    release1, release1_sha, _paths, target, payload_report = _stage(tmp_path)
    payload_receipt = target / RECEIPT_NAME
    payload_receipt_before = payload_receipt.read_bytes()
    payload_receipt_sha_before = _sha(payload_receipt)
    release2 = _write_json(
        tmp_path / "release2/release-manifest.json",
        {
            "schema_version": 1,
            "release_id": "v3-test-rc2",
            "immutable": True,
            "source_snapshot_sha256": "b" * 64,
            "release_sha256": "b" * 64,
            "files": [],
        },
    )
    release2_sha = _sha(release2)
    binding_path = (
        tmp_path
        / "deployments/v3-test-rc2/runtime-inputs"
        / staging_module.RELEASE_BINDING_RECEIPT_NAME
    )
    binding_bytes, rendered = staging_module.render_static_external_release_binding(
        release2,
        expected_release_manifest_sha256=release2_sha,
        target_root=target,
        binding_receipt=binding_path,
    )
    _write(binding_path, binding_bytes)

    assert payload_receipt.read_bytes() == payload_receipt_before
    assert _sha(payload_receipt) == payload_receipt_sha_before
    verified = staging_module.verify_static_external_release_binding(
        release2,
        expected_release_manifest_sha256=release2_sha,
        binding_receipt=binding_path,
        expected_binding_receipt_sha256=rendered["binding_receipt_sha256"],
        target_root=target,
        expected_target_snapshot_sha256=payload_report["target_snapshot_sha256"],
    )
    assert verified["context"]["release_id"] == "v3-test-rc2"
    assert verified["receipt_context"]["release_id"] == "v3-test-rc1"
    with pytest.raises(StaticExternalStagingError, match="context/payload mismatch"):
        staging_module.verify_static_external_release_binding(
            release1,
            expected_release_manifest_sha256=release1_sha,
            binding_receipt=binding_path,
            expected_binding_receipt_sha256=rendered["binding_receipt_sha256"],
            target_root=target,
        )


def test_release_binding_fails_if_shared_payload_receipt_is_rewritten(
    tmp_path: Path,
) -> None:
    release, release_sha, _paths, target, _payload_report = _stage(tmp_path)
    binding_path = (
        tmp_path / "deployment/runtime-inputs" / staging_module.RELEASE_BINDING_RECEIPT_NAME
    )
    binding_bytes, rendered = staging_module.render_static_external_release_binding(
        release,
        expected_release_manifest_sha256=release_sha,
        target_root=target,
        binding_receipt=binding_path,
    )
    _write(binding_path, binding_bytes)
    payload_receipt = json.loads((target / RECEIPT_NAME).read_text(encoding="utf-8"))
    payload_receipt["context"]["release_id"] = "v3-tampered"
    _write_json(target / RECEIPT_NAME, payload_receipt)

    with pytest.raises(StaticExternalStagingError, match="context/payload mismatch"):
        staging_module.verify_static_external_release_binding(
            release,
            expected_release_manifest_sha256=release_sha,
            binding_receipt=binding_path,
            expected_binding_receipt_sha256=rendered["binding_receipt_sha256"],
            target_root=target,
        )


def test_deploy_cross_verification_rejects_wrong_context_and_paths(tmp_path: Path) -> None:
    release, release_sha, _paths, target, report = _stage(tmp_path)
    summaries = {row["logical_id"]: row for row in report["logical_inputs"]}
    deploy = {
        "status": "prepared_not_installed",
        "release_id": "v3-test-rc1",
        "release_manifest_sha256": release_sha,
        "static_external_receipt": str(target / RECEIPT_NAME),
        "static_external_receipt_sha256": report["receipt_sha256"],
        "static_external_target_snapshot_sha256": report["target_snapshot_sha256"],
        "external_inputs": {
            "env_file": str(target / ".env"),
            "env_file_sha256": summaries["environment"]["content_sha256"],
            "website_root": str(target / "website"),
            "laf_config_file": str(target / "config.json"),
            "laf_config_sha256": summaries["runtime_config"]["content_sha256"],
            "google_credentials_file": str(target / "google-credentials.json"),
            "google_credentials_sha256": summaries["google_credentials"]["content_sha256"],
            "accounting_credentials_file": str(target / "accounting-credentials.json"),
            "accounting_credentials_sha256": summaries["accounting_credentials"]["content_sha256"],
            "static_external_receipt": str(target / RECEIPT_NAME),
            "static_external_receipt_sha256": report["receipt_sha256"],
            "static_external_target_snapshot_sha256": report["target_snapshot_sha256"],
        },
    }
    deploy_path = _write_json(tmp_path / "deployment/deploy-manifest.json", deploy)
    verified = verify_static_external(
        release,
        expected_release_manifest_sha256=release_sha,
        target_root=target,
        deploy_manifest=deploy_path,
        expected_deploy_manifest_sha256=_sha(deploy_path),
    )
    assert verified["deploy_manifest_sha256"] == _sha(deploy_path)
    deploy["external_inputs"]["env_file"] = str(tmp_path / "v2/.env")
    _write_json(deploy_path, deploy)
    with pytest.raises(StaticExternalStagingError, match="paths/hashes mismatch"):
        verify_static_external(
            release,
            expected_release_manifest_sha256=release_sha,
            target_root=target,
            deploy_manifest=deploy_path,
            expected_deploy_manifest_sha256=_sha(deploy_path),
        )


def test_cli_snapshot_then_stage_without_deploy_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    release, release_sha, paths = _fixture(tmp_path)
    common = [
        "--release-manifest", str(release), "--release-manifest-sha256", release_sha,
        "--env-file", str(paths["environment"]), "--website-root", str(paths["website"]),
        "--config-file", str(paths["runtime_config"]),
        "--google-credentials-file", str(paths["google_credentials"]),
        "--accounting-credentials-file", str(paths["accounting_credentials"]),
    ]
    assert main(["snapshot", *common]) == 0
    snapshot = json.loads(capsys.readouterr().out)
    target = tmp_path / "target"
    assert main(
        ["stage", *common, "--expected-source-snapshot-sha256",
         snapshot["source_snapshot_sha256"], "--target-root", str(target)]
    ) == 0
    staged = json.loads(capsys.readouterr().out)
    assert staged["status"] == "verified"
    assert main(
        [
            "verify",
            "--release-manifest",
            str(release),
            "--release-manifest-sha256",
            release_sha,
            "--target-root",
            str(target),
            "--verify-source",
            *common[4:],
            "--expected-source-snapshot-sha256",
            snapshot["source_snapshot_sha256"],
        ]
    ) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["source_verified"] is True
    assert not list(tmp_path.rglob("deploy-manifest.json"))


def test_stable_reader_detects_in_place_change_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _write(tmp_path / "large", b"a" * (1024 * 1024 + 32))
    original_read = os.read
    calls = 0

    def mutating_read(descriptor: int, size: int) -> bytes:
        nonlocal calls
        payload = original_read(descriptor, size)
        calls += 1
        if calls == 1:
            with source.open("ab") as stream:
                stream.write(b"drift")
        return payload

    monkeypatch.setattr(staging_module.os, "read", mutating_read)
    with pytest.raises(StaticExternalStagingError, match="changed during"):
        staging_module._stable_regular_bytes(source, label="test source")


def test_receipt_is_regular_single_link_and_directories_are_private(tmp_path: Path) -> None:
    _release, _release_sha, _paths, target, _report = _stage(tmp_path)
    receipt = (target / RECEIPT_NAME).lstat()
    assert stat.S_ISREG(receipt.st_mode) and receipt.st_nlink == 1
    for directory in (
        target, target / "website", target / "website/assets", target / "website/assets/empty"
    ):
        assert directory.stat().st_mode & 0o777 == 0o700
