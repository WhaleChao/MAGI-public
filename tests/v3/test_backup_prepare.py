from __future__ import annotations

import hashlib
import json
import sqlite3
import tarfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import v3_backup_prepare as backup
from scripts.v3_backup_prepare import BackupBlocked, prepare_backup, verify_backup


def _database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO items(value) VALUES('保留')")


def _website(path: Path) -> Path:
    (path / "data").mkdir(parents=True)
    (path / "assets" / "gallery" / "empty").mkdir(parents=True)
    (path / "data" / "site-data.json").write_text('{"title":"保留"}\n', encoding="utf-8")
    (path / "data" / "content.json").write_text('{"items":[1]}\n', encoding="utf-8")
    (path / "assets" / "profile.jpg").write_bytes(b"\x00\xffwebsite-asset")
    return path


def test_online_backup_is_actually_restored_and_hash_bound(tmp_path: Path) -> None:
    source = tmp_path / "v2"
    source.mkdir()
    database = source / ".runtime" / "state.sqlite3"
    _database(database)
    # Production V2 currently keeps the website below the V2 runtime root.
    website = _website(source / "whalechao.github.io")
    website_before = {
        path.relative_to(website).as_posix(): (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in website.rglob("*")
        if path.is_file()
    }
    output = tmp_path / "evidence" / "backup-1"
    output.parent.mkdir()
    now = datetime(2026, 7, 14, 7, 0, tzinfo=timezone.utc)

    metadata = prepare_backup(
        source_root=source,
        database_paths=[database],
        website_root=website,
        output_dir=output,
        campaign_id="campaign-1",
        release_sha="a" * 40,
        hardware_id="mac-test",
        gate_config_sha256="b" * 64,
        now=now,
    )

    archive = output / metadata["artifact_path"]
    drill = output / metadata["restore_drill"]["evidence_path"]
    assert metadata["created_at"] == now.isoformat()
    assert metadata["sha256"] == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert metadata["restore_drill"]["actual_restore_performed"] is True
    assert metadata["restore_drill"]["evidence_sha256"] == hashlib.sha256(drill.read_bytes()).hexdigest()
    assert json.loads(drill.read_text())["status"] == "passed"
    assert metadata["coverage"] == ["sqlite", "website_assets", "website_data"]
    assert metadata["mutable_file_count"] == 3
    manifest = json.loads((output / metadata["content_manifest"]["path"]).read_text())
    assert {row["source"] for row in manifest["mutable_files"]} == {
        "data/content.json",
        "data/site-data.json",
        "assets/profile.jpg",
    }

    restored = tmp_path / "verified-restore"
    verification = verify_backup(
        archive_path=archive,
        archive_sha256=metadata["sha256"],
        restore_dir=restored,
    )
    assert verification["status"] == "passed"
    assert (restored / "website" / "data" / "site-data.json").read_text() == '{"title":"保留"}\n'
    assert (restored / "website" / "assets" / "profile.jpg").read_bytes() == b"\x00\xffwebsite-asset"
    assert (restored / "website" / "assets" / "gallery" / "empty").is_dir()
    assert not any(path.name == "backup-metadata.json" for path in website.rglob("*"))
    assert website_before == {
        path.relative_to(website).as_posix(): (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in website.rglob("*")
        if path.is_file()
    }


def test_empty_escape_duplicate_and_existing_output_fail_closed(tmp_path: Path) -> None:
    source = tmp_path / "v2"
    source.mkdir()
    database = source / "state.sqlite3"
    _database(database)
    website = _website(tmp_path / "whalechao.github.io")
    kwargs = dict(
        source_root=source,
        website_root=website,
        campaign_id="campaign-1",
        release_sha="a" * 40,
        hardware_id="mac-test",
        gate_config_sha256="b" * 64,
    )

    with pytest.raises(BackupBlocked, match="at least one"):
        prepare_backup(database_paths=[], output_dir=tmp_path / "empty", **kwargs)
    outside = tmp_path / "outside.sqlite3"
    _database(outside)
    with pytest.raises(BackupBlocked, match="escapes"):
        prepare_backup(database_paths=[outside], output_dir=tmp_path / "escape", **kwargs)
    with pytest.raises(BackupBlocked, match="duplicates"):
        prepare_backup(database_paths=[database, database], output_dir=tmp_path / "duplicate", **kwargs)
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(BackupBlocked, match="must not already exist"):
        prepare_backup(database_paths=[database], output_dir=existing, **kwargs)


def test_website_symlink_special_scope_and_source_drift_fail_without_success_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "v2"
    source.mkdir()
    database = source / "state.sqlite3"
    _database(database)
    website = _website(tmp_path / "whalechao.github.io")
    linked = website / "assets" / "linked.jpg"
    linked.symlink_to(website / "assets" / "profile.jpg")
    kwargs = dict(
        source_root=source,
        database_paths=[database],
        website_root=website,
        campaign_id="campaign-1",
        release_sha="a" * 40,
        hardware_id="mac-test",
        gate_config_sha256="b" * 64,
    )

    with pytest.raises(BackupBlocked, match="symlink"):
        prepare_backup(output_dir=tmp_path / "symlink-output", **kwargs)
    linked.unlink()

    original = backup._copy_mutable_file
    changed = False

    def mutate_after_first_copy(entry, destination):
        nonlocal changed
        result = original(entry, destination)
        if not changed:
            changed = True
            (website / "data" / "site-data.json").write_text('{"title":"changed"}\n')
        return result

    monkeypatch.setattr(backup, "_copy_mutable_file", mutate_after_first_copy)
    output = tmp_path / "drift-output"
    with pytest.raises(BackupBlocked, match="changed"):
        prepare_backup(output_dir=output, **kwargs)
    assert not (output / "backup-metadata.json").exists()


def test_verify_rejects_archive_tamper_extra_member_and_protected_restore_root(
    tmp_path: Path,
) -> None:
    source = tmp_path / "v2"
    source.mkdir()
    database = source / "state.sqlite3"
    _database(database)
    website = _website(tmp_path / "whalechao.github.io")
    output = tmp_path / "backup"
    metadata = prepare_backup(
        source_root=source,
        database_paths=[database],
        website_root=website,
        output_dir=output,
        campaign_id="campaign-1",
        release_sha="a" * 40,
        hardware_id="mac-test",
        gate_config_sha256="b" * 64,
    )
    archive = output / metadata["artifact_path"]

    with pytest.raises(BackupBlocked, match="SHA-256 mismatch"):
        verify_backup(
            archive_path=archive,
            archive_sha256="0" * 64,
            restore_dir=tmp_path / "wrong-hash-restore",
        )
    with pytest.raises(BackupBlocked, match="protected source"):
        verify_backup(
            archive_path=archive,
            archive_sha256=metadata["sha256"],
            restore_dir=website / "restore-attempt",
        )

    expanded = tmp_path / "expanded"
    expanded.mkdir()
    with tarfile.open(archive, "r:gz") as bundle:
        bundle.extractall(expanded, filter="data")
    (expanded / "unexpected.txt").write_text("not in manifest")
    altered = tmp_path / "altered.tar.gz"
    with tarfile.open(altered, "w:gz") as bundle:
        for path in sorted(expanded.rglob("*")):
            bundle.add(path, arcname=path.relative_to(expanded), recursive=False)
    with pytest.raises(BackupBlocked, match="members differ"):
        verify_backup(
            archive_path=altered,
            archive_sha256=hashlib.sha256(altered.read_bytes()).hexdigest(),
            restore_dir=tmp_path / "extra-member-restore",
        )
    assert not (tmp_path / "extra-member-restore").exists()
