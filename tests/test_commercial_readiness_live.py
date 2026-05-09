from __future__ import annotations

import gzip
import hashlib
from pathlib import Path

from scripts.ops import commercial_readiness_live as gate


def test_db_backup_drill_requires_restore_confirmation(monkeypatch, tmp_path):
    backup = tmp_path / "law_firm_data_local_20260509_010203.sql.gz"
    with gzip.open(backup, "wb") as f:
        f.write(b"CREATE TABLE smoke(id INT);\n")
    sha = hashlib.sha256(backup.read_bytes()).hexdigest()

    class FakeBackupRestore:
        DEFAULT_BACKUP_DIR = str(tmp_path)

        @staticmethod
        def run_list(out_dir: Path, limit: int):
            return {
                "ok": True,
                "items": [
                    {
                        "target": "local",
                        "path": str(backup),
                        "sha256": sha,
                    }
                ],
            }

        @staticmethod
        def run_restore(**kwargs):
            assert kwargs["confirmed"] is False
            return {"ok": False, "error": "confirm_required"}

    import skills.ops.database as database_pkg

    monkeypatch.setattr(database_pkg, "backup_restore", FakeBackupRestore, raising=False)
    monkeypatch.setitem(__import__("sys").modules, "skills.ops.database.backup_restore", FakeBackupRestore)
    monkeypatch.setenv("MAGI_DB_BACKUP_DIR", str(tmp_path))

    result = gate.check_db_backup_drill("python3", skip_backup=True)

    assert result.ok is True
    assert "restore_gate=confirm_required" in result.detail


def test_run_json_reads_trailing_json(monkeypatch):
    class Proc:
        returncode = 0
        stdout = "log line\n{\"ok\": true, \"value\": 3}\n"

    monkeypatch.setattr(gate.subprocess, "run", lambda *a, **k: Proc())

    ok, payload, raw, elapsed = gate._run_json(["fake"])

    assert ok is True
    assert payload["value"] == 3
    assert raw.endswith("}")
    assert elapsed >= 0
