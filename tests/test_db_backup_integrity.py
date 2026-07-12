from __future__ import annotations

import gzip
import json
from pathlib import Path

from skills.ops.database import backup_restore


def _write_backup(path: Path, content: bytes = b"SELECT 1;\n") -> Path:
    with gzip.open(path, "wb") as handle:
        handle.write(content)
    meta = {"sha256": backup_restore._sha256(path)}
    Path(str(path) + ".meta.json").write_text(json.dumps(meta), encoding="utf-8")
    return path


def test_verify_backup_integrity_accepts_matching_hash_and_valid_gzip(tmp_path):
    path = _write_backup(tmp_path / "backup.sql.gz")

    result = backup_restore._verify_backup_integrity(path)

    assert result["ok"] is True
    assert result["sha256"] == backup_restore._sha256(path)


def test_verify_backup_integrity_rejects_tampered_file(tmp_path):
    path = _write_backup(tmp_path / "backup.sql.gz")
    path.write_bytes(path.read_bytes() + b"tampered")

    result = backup_restore._verify_backup_integrity(path)

    assert result["ok"] is False
    assert result["error"] == "backup_checksum_mismatch"


def test_restore_stops_before_db_probe_when_integrity_is_missing(tmp_path, monkeypatch):
    path = tmp_path / "legacy.sql.gz"
    with gzip.open(path, "wb") as handle:
        handle.write(b"SELECT 1;\n")
    calls = []
    monkeypatch.setattr(backup_restore, "_load_profiles", lambda: calls.append("profiles"))

    result = backup_restore.run_restore(
        file_path=path,
        restore_target="local",
        out_dir=tmp_path,
        pre_backup=False,
        keep_days=30,
        confirmed=True,
    )

    assert result["ok"] is False
    assert result["error"] == "backup_integrity_failed"
    assert result["integrity"]["error"] == "backup_metadata_missing"
    assert calls == []
