from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from magi_v3.external_inputs import (
    ExternalInputError,
    bound_shared_directory,
    laf_download_directory,
    load_bound_cron_jobs,
)
from scripts.ops import (
    audit_operational_hardening,
    business_module_live_check,
    business_readiness_snapshot,
    function_health_index,
    system_diagnostic_report,
)
from skills.magi import night_talk
from skills.ops import self_repair_reporter, system_test


ROOT = Path(__file__).resolve().parents[2]


def _bind_candidate_cron(tmp_path: Path, monkeypatch) -> tuple[Path, Path, list[dict]]:
    release = tmp_path / "sealed-candidate"
    (release / "config").mkdir(parents=True)
    jobs = [
        {
            "id": "job_file_review_check",
            "enabled": True,
            "cron": "15 * * * *",
            "command": "python3 skills/file-review-orchestrator/action.py --task scheduled_check",
        },
        {
            "id": "job_drive_case_sync_all_files",
            "enabled": True,
            "cron": "0 3 * * *",
            "command": "python3 scripts/drive_case_sync_worker.py --direct-all-cases",
        },
    ]
    snapshot = tmp_path / "deployment/runtime-inputs/cron_jobs.v3.json"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text(json.dumps(jobs), encoding="utf-8")
    monkeypatch.setenv("MAGI_CRON_JOBS_FILE", str(snapshot))
    monkeypatch.setenv(
        "MAGI_CRON_JOBS_SHA256", hashlib.sha256(snapshot.read_bytes()).hexdigest()
    )
    source_sha = "a" * 64
    monkeypatch.setenv("MAGI_CRON_JOBS_SOURCE_SHA256", source_sha)
    (release / "config/v3_schedule_dispatch_policy.json").write_text(
        json.dumps({"schema_version": 1, "cron_jobs_sha256": source_sha}),
        encoding="utf-8",
    )
    assert not (release / "cron_jobs.json").exists()
    assert not (release / "venv").exists()
    return release, snapshot, jobs


def test_candidate_diagnostics_use_hash_bound_cron_without_release_copy(
    tmp_path: Path, monkeypatch
) -> None:
    release, snapshot, jobs = _bind_candidate_cron(tmp_path, monkeypatch)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "cron_state.json").write_text("{}\n", encoding="utf-8")

    monkeypatch.setattr(audit_operational_hardening, "ROOT", release)
    monkeypatch.setattr(self_repair_reporter, "_PROJECT_ROOT", str(release))
    monkeypatch.setattr(night_talk, "_MAGI_ROOT", str(release))
    monkeypatch.setattr(system_test, "MAGI_DIR", str(release))
    monkeypatch.setattr(
        system_test._health_probes,
        "python_script_process_running",
        lambda _script: (True, "123"),
    )

    assert audit_operational_hardening._cron_jobs() == jobs
    assert set(business_module_live_check._cron_semantic_map(release)) == {
        "job_file_review_check",
        "job_drive_case_sync_all_files",
    }
    assert business_module_live_check._enabled_drive_sync_worker_kinds(release) == {
        "all_files"
    }
    assert business_readiness_snapshot._scheduled_file_review_download_enabled(release)
    assert set(self_repair_reporter._load_cron_job_map()) == {
        "job_file_review_check",
        "job_drive_case_sync_all_files",
    }
    assert night_talk._enabled_cron_jobs() == {
        "job_file_review_check",
        "job_drive_case_sync_all_files",
    }
    assert system_test.test_autopilot_schedule()["pass"] is True
    cron_health, _expected = function_health_index.discover_cron_jobs(
        release, runtime, datetime.now(timezone.utc)
    )
    assert cron_health["present"] is True
    assert cron_health["source"] == str(snapshot)
    assert cron_health["total"] == 2
    assert cron_health["enabled"] == 2
    diagnostic = system_diagnostic_report._schedule_summary(
        snapshot,
        runtime / "cron_state.json",
        verified_jobs=load_bound_cron_jobs(release).jobs,
    )
    assert diagnostic["definitions"] == 2
    assert diagnostic["enabled"] == 2


def test_candidate_cron_binding_rejects_hash_drift(tmp_path: Path, monkeypatch) -> None:
    release, _snapshot, _jobs = _bind_candidate_cron(tmp_path, monkeypatch)
    monkeypatch.setenv("MAGI_CRON_JOBS_SHA256", "0" * 64)

    with pytest.raises(ExternalInputError, match="SHA-256 mismatched"):
        load_bound_cron_jobs(release)


def test_sealed_candidate_without_external_cron_binding_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    release = tmp_path / "sealed-candidate"
    release.mkdir()
    (release / "release-manifest.json").write_text("{}\n", encoding="utf-8")
    for name in (
        "MAGI_CRON_JOBS_FILE",
        "MAGI_CRON_JOBS_SHA256",
        "MAGI_CRON_JOBS_SOURCE_SHA256",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ExternalInputError, match="sealed V3 release requires"):
        load_bound_cron_jobs(release)

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "cron_state.json").write_text("{}\n", encoding="utf-8")
    cron_health, _expected = function_health_index.discover_cron_jobs(
        release, runtime, datetime.now(timezone.utc)
    )
    assert cron_health["present"] is False
    assert "sealed V3 release requires" in cron_health["error"]


def test_candidate_cron_source_must_match_release_policy(
    tmp_path: Path, monkeypatch
) -> None:
    release, _snapshot, _jobs = _bind_candidate_cron(tmp_path, monkeypatch)
    monkeypatch.setenv("MAGI_CRON_JOBS_SOURCE_SHA256", "b" * 64)

    with pytest.raises(ExternalInputError, match="source/policy binding mismatched"):
        load_bound_cron_jobs(release)


def test_candidate_child_processes_select_verified_launcher_without_venv(
    tmp_path: Path, monkeypatch
) -> None:
    release = tmp_path / "sealed-candidate"
    launcher = release / "bin/magi-v3-python"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o555)
    monkeypatch.setenv("MAGI_V3_EXECUTABLE_PATH", str(launcher))
    monkeypatch.delenv("MAGI_SKILL_PYTHON", raising=False)

    import scripts.nightly_distill_gemma as distill

    monkeypatch.setattr(distill, "MAGI_ROOT", release)
    assert distill._child_python() == str(launcher)
    assert not (release / "venv").exists()

    switch_source = (ROOT / "config/bin/omlx_switch_model.sh").read_text(
        encoding="utf-8"
    )
    assert 'GATEKEEPER_PY="$MAGI_PYTHON"' in switch_source
    assert 'local py="$MAGI_PYTHON"' in switch_source
    assert 'GATEKEEPER_PY="$MAGI_ROOT_DIR/venv/bin/python3"' not in switch_source
    assert "pid=${pid}）" in switch_source
    assert "pid=$pid）" not in switch_source


def test_candidate_mutable_preflight_and_laf_state_stay_outside_release(
    tmp_path: Path, monkeypatch
) -> None:
    release = tmp_path / "sealed-candidate"
    shared = tmp_path / "shared"
    runtime = shared / "runtime"
    agent = shared / "agent"
    mutable_static = shared / "static"
    (release / "api").mkdir(parents=True)
    (release / "api/discord_bot.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    for directory in (runtime, agent, mutable_static):
        directory.mkdir(parents=True)
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "candidate-test")
    monkeypatch.setenv("MAGI_V3_SHARED_STATE_DIR", str(shared))
    monkeypatch.setenv("MAGI_SHARED_STATE_DIR", str(shared))
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("MAGI_AGENT_DIR", str(agent))
    monkeypatch.setenv("MAGI_MUTABLE_STATIC_DIR", str(mutable_static))

    import scripts.ops.nightly_regression as nightly_regression
    from casper_ecosystem.law_firm_orchestrators import laf_orchestrator

    monkeypatch.setattr(nightly_regression, "MAGI_DIR", release)
    monkeypatch.setattr(laf_orchestrator, "MAGI_DIR", release)
    monkeypatch.setattr(
        nightly_regression,
        "_discord_bot_process",
        lambda: (False, ""),
    )

    class ExitedProcess:
        pid = 123
        returncode = 2

        @staticmethod
        def poll():
            return 2

    monkeypatch.setattr(
        nightly_regression.subprocess,
        "Popen",
        lambda *_args, **_kwargs: ExitedProcess(),
    )
    config = tmp_path / "external-config.json"
    config.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("MAGI_CONFIG_PATH", str(config.resolve()))
    monkeypatch.setenv(
        "MAGI_CONFIG_SHA256", hashlib.sha256(config.read_bytes()).hexdigest()
    )
    release.chmod(0o555)
    try:
        result = nightly_regression.ensure_discord_bot_for_regression(wait_sec=1)
        assert result["log_path"] == str(runtime / "nightly_discord_preflight.log")
        assert not (release / ".runtime").exists()

        orchestrator = laf_orchestrator.LAFOrchestrator(dry_run=True)
        assert orchestrator._portal_retry_state_path.parent == agent
        assert orchestrator._portal_retry_lock_path.parent == agent
        assert orchestrator._portal_retry_state_lock_path.parent == agent
        assert orchestrator._portal_seed_skip_path.parent == agent
        assert orchestrator._portal_retry_heartbeat_path.parent == mutable_static
        assert orchestrator.laf_config["download_folder"] == str(
            shared / "laf-downloads"
        )
        assert not (release / ".agent").exists()
        assert not (release / "static").exists()
    finally:
        release.chmod(0o755)


def test_sealed_mutable_directory_missing_or_inside_release_fails_closed(
    tmp_path: Path, monkeypatch
) -> None:
    release = tmp_path / "sealed-candidate"
    shared = tmp_path / "shared"
    release.mkdir()
    shared.mkdir()
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "candidate-test")
    monkeypatch.setenv("MAGI_V3_SHARED_STATE_DIR", str(shared))
    monkeypatch.setenv("MAGI_SHARED_STATE_DIR", str(shared))
    monkeypatch.delenv("MAGI_RUNTIME_DIR", raising=False)
    with pytest.raises(ExternalInputError, match="requires MAGI_RUNTIME_DIR"):
        bound_shared_directory(
            release,
            env_name="MAGI_RUNTIME_DIR",
            shared_leaf="runtime",
            source_fallback=".runtime",
        )


def test_sealed_laf_download_directory_uses_only_canonical_shared_path(
    tmp_path: Path, monkeypatch
) -> None:
    release = tmp_path / "sealed-candidate"
    shared = tmp_path / "shared"
    release.mkdir()
    shared.mkdir()
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "candidate-test")
    monkeypatch.setenv("MAGI_V3_SHARED_STATE_DIR", str(shared))
    monkeypatch.delenv("MAGI_LAF_DOWNLOAD_FOLDER", raising=False)

    assert laf_download_directory(release) == shared / "laf-downloads"

    monkeypatch.setenv("MAGI_LAF_DOWNLOAD_FOLDER", str(release / "laf_downloads"))
    with pytest.raises(ExternalInputError, match="canonical shared-state"):
        laf_download_directory(release)

    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(release / ".runtime"))
    with pytest.raises(ExternalInputError, match="canonical shared-state"):
        bound_shared_directory(
            release,
            env_name="MAGI_RUNTIME_DIR",
            shared_leaf="runtime",
            source_fallback=".runtime",
        )


def test_bound_child_python_does_not_fallback_from_invalid_deployment_binding(
    tmp_path: Path, monkeypatch
) -> None:
    import scripts.nightly_distill_gemma as distill

    monkeypatch.setenv("MAGI_V3_EXECUTABLE_PATH", str(tmp_path / "missing-launcher"))
    monkeypatch.setenv("MAGI_SKILL_PYTHON", os.devnull)
    with pytest.raises(RuntimeError, match="unavailable or unsafe"):
        distill._child_python()


def test_sealed_supplement_ocr_caches_bind_runtime_outside_release(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from src.supplement_core import attachment_matcher, ruling_text_loader

    release = tmp_path / "sealed-candidate"
    shared = tmp_path / "shared"
    runtime = shared / "runtime"
    release.mkdir()
    shared.mkdir()
    probe = tmp_path / "synthetic-ruling.pdf"
    probe.write_bytes(b"synthetic-pdf-probe")
    monkeypatch.setenv("MAGI_ROOT", str(release))
    monkeypatch.setenv("MAGI_V3_RELEASE_ID", "candidate-supplement-test")
    monkeypatch.setenv("MAGI_V3_SHARED_STATE_DIR", str(shared))
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(runtime))

    assert ruling_text_loader._cache_dir() == runtime / "supplement_cache"
    assert Path(attachment_matcher._ocr_cache_path(str(probe))).parent == (
        runtime / "supplement_cache"
    )
    assert not (release / "runtime").exists()


def test_sealed_omlx_switch_rejects_missing_verified_launcher(
    tmp_path: Path,
) -> None:
    result = subprocess.run(
        ["/bin/bash", str(ROOT / "config/bin/omlx_switch_model.sh"), "auto"],
        cwd=tmp_path,
        env={
            "HOME": str(tmp_path),
            "MAGI_ROOT_DIR": str(tmp_path / "sealed-candidate"),
            "MAGI_V3_RELEASE_ID": "candidate-test",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert "requires its verified Python launcher" in result.stderr
