from __future__ import annotations

import gzip
import hashlib
import subprocess
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


def test_strict_public_release_audit_uses_public_isolation(monkeypatch):
    calls = []

    def fake_run_json(cmd, **kwargs):
        calls.append(cmd)
        return True, {"ok": True, "errors": 0, "warnings": 0}, "{}", 0.01

    monkeypatch.setattr(gate, "_run_json", fake_run_json)

    result = gate.check_public_release_audit("python3", strict=True)

    assert result.ok is True
    assert calls == [
        [
            "python3",
            "scripts/public_release_audit.py",
            "--json",
            "--public-isolation",
            "--strict",
        ]
    ]


def test_public_cleanroom_snapshot_uses_current_worktree(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.PIPE)
    (repo / ".gitignore").write_text(".runtime/\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("indexed\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "tracked.txt"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("current worktree\n", encoding="utf-8")
    (repo / "new_public.txt").write_text("new file\n", encoding="utf-8")
    (repo / ".runtime").mkdir()
    (repo / ".runtime" / "private.json").write_text("secret\n", encoding="utf-8")

    monkeypatch.setattr(gate, "MAGI_ROOT", repo)
    dest = tmp_path / "snapshot"

    result = gate._snapshot_current_worktree(dest)

    assert result["copied_files"] >= 3
    assert (dest / "tracked.txt").read_text(encoding="utf-8") == "current worktree\n"
    assert (dest / "new_public.txt").read_text(encoding="utf-8") == "new file\n"
    assert not (dest / ".runtime" / "private.json").exists()


def test_live_conflict_audit_check_uses_business_module_audit(monkeypatch):
    monkeypatch.setattr(
        gate,
        "MAGI_ROOT",
        Path("/tmp/magi-test-root"),
    )

    from scripts.ops import business_module_live_check as live_check

    calls = []

    def fake_audit(root):
        calls.append(root)
        return {"ok": True, "error_count": 0, "warning_count": 2}

    monkeypatch.setattr(live_check, "audit_live_conflicts", fake_audit)

    result = gate.check_live_conflict_audit("python3")

    assert result.ok is True
    assert result.status == "pass"
    assert result.detail == "errors=0 warnings=2"
    assert calls == [Path("/tmp/magi-test-root")]


def test_live_validation_commands_include_required_probe_paths():
    commands = gate.live_validation_commands("python3")

    assert commands["production_live"][:4] == ["python3", "scripts/ops/run_test_suite.py", "--suite", "production-live"]
    assert commands["business_modules"][:2] == ["python3", "scripts/ops/business_module_live_check.py"]
    assert commands["conflict_audit"][:3] == ["python3", "scripts/ops/business_module_live_check.py", "--conflict-audit"]
    assert commands["manual_probe"] == ["curl", "-fsS", "http://127.0.0.1:${MAGI_SERVER_PORT:-5002}/health"]
