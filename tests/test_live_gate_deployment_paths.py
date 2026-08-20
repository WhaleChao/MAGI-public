from __future__ import annotations

import hashlib
import os
from pathlib import Path

from scripts.ops import commercial_readiness_live as commercial
from scripts.ops import smoke_test_full as smoke


def test_smoke_accepts_bound_runtime_and_environment_for_installed_release(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "release"
    root.mkdir()
    (root / "release-manifest.json").write_text("{}", encoding="utf-8")
    (root / "RELEASE_COMPLETE.json").write_text("{}", encoding="utf-8")
    runtime = tmp_path / "runtime" / "bin" / "python3"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("#!/bin/sh\n", encoding="utf-8")
    runtime.chmod(0o755)
    env_file = tmp_path / "external" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("MAGI_TEST=1\n", encoding="utf-8")

    monkeypatch.setattr(smoke, "MAGI_ROOT", root)
    monkeypatch.setenv("MAGI_V3_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("MAGI_V3_PYTHON_RUNTIME", str(runtime))
    monkeypatch.setenv("MAGI_ENV_FILE", str(env_file))

    assert smoke.test_venv()[0] is True
    assert smoke.test_env_file()[0] is True
    assert smoke.test_env_example()[0] is True
    assert smoke.test_gitignore_coverage()[0] is True
    assert smoke.test_license_exists()[0] is True
    assert smoke.test_ci_pipeline()[0] is True


def test_smoke_loads_hash_bound_external_environment(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "external" / ".env"
    env_file.parent.mkdir()
    payload = b"MAGI_SMOKE_BOUND_VALUE=verified\n"
    env_file.write_bytes(payload)
    monkeypatch.setenv("MAGI_ENV_FILE", str(env_file))
    monkeypatch.setenv("MAGI_ENV_FILE_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.delenv("MAGI_SMOKE_BOUND_VALUE", raising=False)

    assert smoke._load_runtime_env() is True
    assert os.environ["MAGI_SMOKE_BOUND_VALUE"] == "verified"


def test_smoke_rejects_drifted_external_environment(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "external" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("MAGI_SMOKE_BOUND_VALUE=drifted\n", encoding="utf-8")
    monkeypatch.setenv("MAGI_ENV_FILE", str(env_file))
    monkeypatch.setenv("MAGI_ENV_FILE_SHA256", "0" * 64)

    try:
        smoke._load_runtime_env()
    except RuntimeError as exc:
        assert "digest mismatch" in str(exc)
    else:
        raise AssertionError("drifted bound environment was accepted")


def test_minimal_ready_payload_is_valid_but_explicit_false_is_not() -> None:
    assert smoke._health_json_ok(200, '{"status":"ready"}', healthy_statuses={"ready"})[0]
    assert not smoke._health_json_ok(
        200, '{"ok":false,"status":"ready"}', healthy_statuses={"ready"}
    )[0]


def test_production_backup_directory_uses_mutable_shared_state(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("MAGI_DB_BACKUP_DIR", raising=False)
    monkeypatch.setenv("MAGI_SHARED_STATE_DIR", str(tmp_path / "shared"))
    observed: dict[str, object] = {}

    class Backup:
        DEFAULT_BACKUP_DIR = str(tmp_path / "immutable-release" / "_db_backups")

        @staticmethod
        def run_list(out_dir, _limit):
            observed["out_dir"] = Path(out_dir)
            return {"items": []}

    from skills.ops import database

    monkeypatch.setattr(database, "backup_restore", Backup, raising=False)
    result = commercial.check_db_backup_drill(os.sys.executable, skip_backup=True)

    expected = tmp_path / "shared" / "db-backups" / "law_firm_data"
    assert result.ok is False
    assert observed["out_dir"] == expected
    assert expected.is_dir()


def test_installed_release_without_launchd_binding_returns_structured_failure(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "release"
    root.mkdir()
    (root / "release-manifest.json").write_text("{}", encoding="utf-8")
    (root / "RELEASE_COMPLETE.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(commercial, "MAGI_ROOT", root)
    monkeypatch.setattr(commercial, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setenv("MAGI_V3_DEPLOYMENT_MODE", "production")
    for name in (
        "MAGI_CRON_JOBS_FILE",
        "MAGI_CRON_JOBS_SHA256",
        "MAGI_CRON_JOBS_SOURCE_SHA256",
    ):
        monkeypatch.delenv(name, raising=False)

    result = commercial.run_gate(
        json_out=tmp_path / "runtime" / "commercial.json",
        strict_public=True,
        skip_backup=True,
        skip_db=True,
    )

    assert result["ok"] is False
    assert result["checks"][0]["name"] == "deployment_bindings"
    assert result["checks"][0]["status"] == "fail"
    assert "MAGI_CRON_JOBS_FILE" in result["checks"][0]["detail"]
    assert all("traceback" not in item["detail"].lower() for item in result["checks"])


def test_installed_release_accepts_hash_bound_cron_binding(tmp_path: Path, monkeypatch) -> None:
    cron = tmp_path / "runtime-inputs" / "cron_jobs.v3.json"
    cron.parent.mkdir()
    cron.write_text("[]\n", encoding="utf-8")
    digest = commercial._sha256(cron)
    monkeypatch.setenv("MAGI_CRON_JOBS_FILE", str(cron))
    monkeypatch.setenv("MAGI_CRON_JOBS_SHA256", digest)
    monkeypatch.setenv("MAGI_CRON_JOBS_SOURCE_SHA256", "a" * 64)

    check = commercial.check_deployment_bindings()
    assert check.ok is True
    assert check.status == "pass"
    assert check.artifact == str(cron)


def test_installed_release_loads_hash_bound_external_environment(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "release"
    root.mkdir()
    (root / "release-manifest.json").write_text("{}", encoding="utf-8")
    (root / "RELEASE_COMPLETE.json").write_text("{}", encoding="utf-8")
    env_file = tmp_path / "external" / ".env"
    env_file.parent.mkdir()
    payload = b"MAGI_LOCAL_DB_HOST=database.internal\n"
    env_file.write_bytes(payload)
    monkeypatch.setattr(commercial, "MAGI_ROOT", root)
    monkeypatch.setattr(commercial, "_DOTENV_LOADED", False)
    monkeypatch.setenv("MAGI_V3_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("MAGI_ENV_FILE", str(env_file))
    monkeypatch.setenv("MAGI_ENV_FILE_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.delenv("MAGI_LOCAL_DB_HOST", raising=False)

    check = commercial._load_runtime_env()

    assert check is not None and check.ok is True
    assert os.environ["MAGI_LOCAL_DB_HOST"] == "database.internal"


def test_installed_release_rejects_drifted_external_environment(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "release"
    root.mkdir()
    (root / "release-manifest.json").write_text("{}", encoding="utf-8")
    (root / "RELEASE_COMPLETE.json").write_text("{}", encoding="utf-8")
    env_file = tmp_path / "external" / ".env"
    env_file.parent.mkdir()
    env_file.write_text("DB_HOST=database.internal\n", encoding="utf-8")
    monkeypatch.setattr(commercial, "MAGI_ROOT", root)
    monkeypatch.setattr(commercial, "_DOTENV_LOADED", False)
    monkeypatch.setenv("MAGI_V3_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("MAGI_ENV_FILE", str(env_file))
    monkeypatch.setenv("MAGI_ENV_FILE_SHA256", "0" * 64)

    check = commercial._load_runtime_env()

    assert check is not None and check.ok is False
    assert "digest mismatch" in check.detail
