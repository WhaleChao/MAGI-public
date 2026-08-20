from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from magi_v3.external_inputs import (
    ExternalInputError,
    load_bound_laf_config,
    verify_sealed_runtime_inputs,
)
from scripts.v3_credential_handoff_prepare import (
    SecretHandoffError,
    materialize_secret_handoff,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path: Path, payload: bytes, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    path.chmod(mode)
    return path.resolve()


def _runtime_contract(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    release = tmp_path / "release"
    release.mkdir()
    (release / "RELEASE_COMPLETE.json").write_text("{}\n")
    shared = (tmp_path / "runtime/shared").resolve()
    config = _write(tmp_path / "external/config.json", b"{}\n")
    credentials = _write(tmp_path / "external/credentials.json", b"{}\n")
    accounting_credentials = _write(
        tmp_path / "external/accounting-credentials.json", b"{}\n"
    )
    token_paths = {
        "MAGI_GOOGLE_CALENDAR_TOKEN_PATH": shared / "secrets/google_calendar_token.json",
        "MAGI_LAF_GMAIL_TOKEN_PATH": shared / "secrets/laf_gmail_token.pickle",
        "MAGI_FILE_REVIEW_TOKEN_PATH": shared / "secrets/filereview_token.pickle",
        "MAGI_ACCOUNTING_GOOGLE_SHEETS_TOKEN": shared
        / "secrets/accounting_sheets_token.json",
        "MAGI_DRIVE_SYNC_TOKEN": shared / "secrets/drive_sync_token.json",
        "MAGI_DRIVE_SYNC_WRITE_TOKEN": shared / "secrets/drive_sync_write_token.json",
    }
    for path in token_paths.values():
        _write(path, b"refreshable-token\n")
    ocr = tmp_path / ".magi_nas_ocr_queue.db"
    with sqlite3.connect(ocr) as connection:
        connection.execute("CREATE TABLE queue (id INTEGER PRIMARY KEY)")
    env = {
        "MAGI_V3_RELEASE_ID": "candidate",
        "MAGI_V3_SHARED_STATE_DIR": str(shared),
        "MAGI_CONFIG_PATH": str(config),
        "MAGI_CONFIG_SHA256": _sha(config),
        "MAGI_CONFIG_MODE": "0600",
        "MAGI_LAF_CONFIG_FILE": str(config),
        "MAGI_LAF_CONFIG_SHA256": _sha(config),
        "MAGI_JSON_DIR": str(config.parent),
        "MAGI_GOOGLE_CREDENTIALS_PATH": str(credentials),
        "MAGI_GOOGLE_CREDENTIALS_SHA256": _sha(credentials),
        "MAGI_GOOGLE_CREDENTIALS_MODE": "0600",
        "MAGI_GMAIL_CREDENTIALS_PATH": str(credentials),
        "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_PATH": str(accounting_credentials),
        "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_SHA256": _sha(accounting_credentials),
        "MAGI_ACCOUNTING_GOOGLE_CREDENTIALS_MODE": "0600",
        "MAGI_GMAIL_COMPOSE_TOKEN_PATH": str(
            shared / "secrets/gmail_compose_token.json"
        ),
        "MAGI_NAS_OCR_QUEUE_DB_PATH": str(ocr.resolve()),
        **{name: str(path.resolve()) for name, path in token_paths.items()},
    }
    return release.resolve(), env


def test_sealed_runtime_verifies_static_hash_and_allows_atomic_token_refresh(
    tmp_path: Path,
) -> None:
    release, env = _runtime_contract(tmp_path)
    verify_sealed_runtime_inputs(release, env)

    token = Path(env["MAGI_GOOGLE_CALENDAR_TOKEN_PATH"])
    replacement = token.with_name(".replacement")
    _write(replacement, b"legitimate-refreshed-token\n")
    os.replace(replacement, token)
    token.chmod(0o600)

    verify_sealed_runtime_inputs(release, env)


def test_sealed_runtime_rejects_static_drift_and_unsafe_mutable_mode(tmp_path: Path) -> None:
    release, env = _runtime_contract(tmp_path)
    Path(env["MAGI_CONFIG_PATH"]).write_text('{"drift":true}\n')
    with pytest.raises(ExternalInputError, match="SHA-256"):
        verify_sealed_runtime_inputs(release, env)

    env["MAGI_CONFIG_SHA256"] = _sha(Path(env["MAGI_CONFIG_PATH"]))
    env["MAGI_LAF_CONFIG_SHA256"] = env["MAGI_CONFIG_SHA256"]
    Path(env["MAGI_LAF_GMAIL_TOKEN_PATH"]).chmod(0o644)
    with pytest.raises(ExternalInputError, match="owner, or mode"):
        verify_sealed_runtime_inputs(release, env)


def test_sealed_laf_reader_has_no_candidate_fallback_and_detects_hash_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "RELEASE_COMPLETE.json").write_text("{}\n")
    monkeypatch.delenv("MAGI_CONFIG_PATH", raising=False)
    monkeypatch.delenv("MAGI_CONFIG_SHA256", raising=False)
    monkeypatch.delenv("MAGI_LAF_CONFIG_FILE", raising=False)
    monkeypatch.delenv("MAGI_LAF_CONFIG_SHA256", raising=False)
    with pytest.raises(ExternalInputError, match="requires a complete"):
        load_bound_laf_config(release)

    config = _write(tmp_path / "external/config.json", b'{"laf":{}}\n')
    monkeypatch.setenv("MAGI_CONFIG_PATH", str(config))
    monkeypatch.setenv("MAGI_CONFIG_SHA256", _sha(config))
    assert load_bound_laf_config(release).config == {"laf": {}}
    config.write_text("{}\n")
    with pytest.raises(ExternalInputError, match="SHA-256"):
        load_bound_laf_config(release)


def _handoff_manifest(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    runtime = (tmp_path / "runtime").resolve()
    sources: dict[str, Path] = {}
    external: dict[str, object] = {}
    rows = (
        ("google_calendar_token", "google_calendar_token.json"),
        ("laf_gmail_token", "laf_gmail_token.pickle"),
        ("file_review_token", "filereview_token.pickle"),
        ("accounting_sheets_token", "accounting_sheets_token.json"),
        ("drive_sync_token", "drive_sync_token.json"),
        ("drive_sync_write_token", "drive_sync_write_token.json"),
    )
    for key, leaf in rows:
        source = _write(tmp_path / "source" / leaf, f"{key}\n".encode())
        sources[key] = source
        external[f"{key}_source_file"] = str(source)
        external[f"{key}_source_sha256"] = _sha(source)
        external[f"{key}_file"] = str(runtime / "shared/secrets" / leaf)
    external.update(
        {
            "gmail_compose_token_source_file": None,
            "gmail_compose_token_source_sha256": None,
            "gmail_compose_token_file": str(
                runtime / "shared/secrets/gmail_compose_token.json"
            ),
        }
    )
    manifest = tmp_path / "deploy/deploy-manifest.json"
    manifest.parent.mkdir()
    manifest.write_text(
        json.dumps(
            {
                "status": "prepared_not_installed",
                "runtime_root": str(runtime),
                "external_inputs": external,
            },
            sort_keys=True,
        )
    )
    return manifest.resolve(), sources


def test_secret_handoff_materializes_refreshable_targets_without_content_in_receipt(
    tmp_path: Path,
) -> None:
    manifest, sources = _handoff_manifest(tmp_path)
    receipt = materialize_secret_handoff(
        manifest,
        expected_manifest_sha256=_sha(manifest),
    )
    receipt_text = json.dumps(receipt, sort_keys=True)
    assert receipt["sensitive_content_recorded"] is False
    assert "google_calendar_token\n" not in receipt_text
    for row in receipt["rows"]:
        if row["status"] != "materialized":
            continue
        target = Path(row["target"])
        assert target.stat().st_mode & 0o777 == 0o600
        assert row["source_sha256"] == row["target_sha256"] == _sha(target)
    assert not Path(
        json.loads(manifest.read_text())["external_inputs"]["gmail_compose_token_file"]
    ).exists()

    sources["google_calendar_token"].write_bytes(b"drift\n")
    with pytest.raises(SecretHandoffError, match="source SHA-256 mismatch"):
        materialize_secret_handoff(
            manifest,
            expected_manifest_sha256=_sha(manifest),
            receipt_path=tmp_path / "second-receipt.json",
        )
