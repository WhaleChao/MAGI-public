from __future__ import annotations

import shlex
from pathlib import Path

from scripts import seed_cron_jobs


def test_canonicalize_job_command_quotes_runtime_root_with_spaces():
    runtime_root = Path("/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2")
    raw = (
        "/Users/ai/Desktop/MAGI_v2/venv/bin/python3 "
        "/Users/ai/Desktop/MAGI_v2/scripts/ops/audit_operational_hardening.py "
        "--json-out /Users/ai/Desktop/MAGI_v2/.runtime/operational_hardening_audit_latest.json"
    )

    job, changed = seed_cron_jobs.canonicalize_job_command({"command": raw}, runtime_root)

    assert changed is True
    parts = shlex.split(job["command"])
    assert parts[0] == str(runtime_root / "venv" / "bin" / "python3")
    assert parts[1] == str(runtime_root / "scripts" / "ops" / "audit_operational_hardening.py")
    assert parts[-1] == str(runtime_root / ".runtime" / "operational_hardening_audit_latest.json")


def test_canonicalize_job_command_quotes_unquoted_env_assignment_with_runtime_spaces():
    runtime_root = Path("/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2")
    raw = (
        "/Users/ai/Desktop/MAGI_v2/venv/bin/python3 "
        "/Users/ai/Desktop/MAGI_v2/scripts/ops/run_with_env.py "
        "MAGI_OBSIDIAN_AGENT_DIR=/Users/ai/Desktop/MAGI_v2/.agent -- "
        "/Users/ai/Desktop/MAGI_v2/skills/obsidian/action.py"
    )

    job, changed = seed_cron_jobs.canonicalize_job_command({"command": raw}, runtime_root)

    assert changed is True
    parts = shlex.split(job["command"])
    assert parts[2] == f"MAGI_OBSIDIAN_AGENT_DIR={runtime_root / '.agent'}"
    assert len(parts) == 5


def test_canonicalize_job_command_preserves_quoted_env_assignment_with_runtime_spaces():
    runtime_root = Path("/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2")
    raw = (
        f"{shlex.quote(str(runtime_root / 'venv' / 'bin' / 'python3'))} "
        f"'MAGI_OBSIDIAN_AGENT_DIR=/Users/ai/Desktop/MAGI_v2/.agent' -- "
        "'/Users/ai/Desktop/MAGI_v2/skills/obsidian/action.py'"
    )

    job, changed = seed_cron_jobs.canonicalize_job_command({"command": raw}, runtime_root)

    assert changed is True
    parts = shlex.split(job["command"])
    assert parts[1] == f"MAGI_OBSIDIAN_AGENT_DIR={runtime_root / '.agent'}"
    assert len(parts) == 4


def test_business_jobs_seed_obsidian_pipeline_with_shared_agent_dir(tmp_path):
    repo_root = tmp_path / "MAGI_v2"
    python = repo_root / "venv" / "bin" / "python3"
    jobs = seed_cron_jobs.business_jobs(repo_root=repo_root, python_path=python)
    by_id = {job["id"]: job for job in jobs}

    expected = {
        "job_case_index_sync",
        "job_obsidian_ingest",
        "job_obsidian_repair_notes",
        "job_obsidian_duplicate_cleanup",
        "job_wiki_synthesizer",
        "job_obsidian_vector_reindex_notes",
        "job_obsidian_vector_reindex_wiki",
        "job_knowledge_lint",
        "job_obsidian_acceptance_gate",
    }
    assert expected <= set(by_id)
    assert "MAGI_OBSIDIAN_AGENT_DIR=" in by_id["job_obsidian_ingest"]["command"]
    assert by_id["job_obsidian_ingest"]["cron"] == "35 2 * * *"
    assert "MAGI_OBSIDIAN_PDF_EXTRACTOR_TIMEOUT_SEC=45" in by_id["job_obsidian_repair_notes"]["command"]
    assert "--limit 20" in by_id["job_obsidian_repair_notes"]["command"]
    assert by_id["job_wiki_synthesizer"]["cron"] == "30 4 * * *"
    assert "MAGI_OBSIDIAN_INGEST_ZERO_CHUNKS_FIRST=1" in by_id["job_obsidian_vector_reindex_notes"]["command"]
    assert by_id["job_knowledge_lint"]["cron"] == "47 5 * * *"
    assert by_id["job_obsidian_acceptance_gate"]["cron"] == "0 6 * * *"
    assert by_id["job_obsidian_repair_notes"]["cron"] == "27 3 * * *"
    assert by_id["job_file_review_staging_cleanup"]["cron"] == "47 3 * * *"
    assert by_id["job_knowledge_lint"]["cron"] == "47 5 * * *"


def test_business_live_check_seed_has_enough_time_for_strict_portal_probe(tmp_path):
    jobs = seed_cron_jobs.business_jobs(repo_root=tmp_path, python_path=tmp_path / "venv" / "bin" / "python3")
    by_id = {job["id"]: job for job in jobs}

    assert by_id["job_business_module_live_check"]["timeout_sec"] == 960


def test_operational_jobs_seed_health_guardian_and_reporter_order(tmp_path):
    jobs = seed_cron_jobs.operational_jobs(repo_root=tmp_path, python_path=tmp_path / "venv" / "bin" / "python3")
    by_id = {job["id"]: job for job in jobs}

    health = by_id["job_function_health_index"]
    assert health["cron"] == "10 6 * * *"
    assert health["timeout_sec"] == 180
    assert health["no_catchup"] is True
    assert "function_health_index.py" in health["command"]
    assert "--fail-on-health" in health["command"]

    guardian = by_id["job_magi_self_repair_guardian"]
    assert guardian["cron"] == "15 6 * * *"
    assert guardian["timeout_sec"] == 300
    assert guardian["no_catchup"] is True
    assert "--mode repair-safe" in guardian["command"]
    assert "--fail-on-issues" in guardian["command"]
    assert "magi_self_repair_guardian_latest.json" in guardian["command"]

    reporter = by_id["job_self_repair_reporter"]
    assert reporter["cron"] == "20 6 * * *"
    assert reporter["timeout_sec"] == 120
    assert reporter["no_catchup"] is True


def test_seed_jobs_assigns_explicit_timeout_to_every_enabled_job(tmp_path):
    (tmp_path / "cron_jobs.json").write_text(
        '[{"id":"short","enabled":true,"command":"@MAGI status"},'
        '{"id":"job_nightly_regression","enabled":true,"command":"@MAGI test"},'
        '{"id":"disabled","enabled":false,"command":"@MAGI off"}]',
        encoding="utf-8",
    )

    seed_cron_jobs.seed_jobs(repo_root=tmp_path, python_path=tmp_path / "venv" / "bin" / "python3")

    jobs = seed_cron_jobs.load_jobs(tmp_path / "cron_jobs.json")
    by_id = {job["id"]: job for job in jobs}
    assert by_id["short"]["timeout_sec"] == 600
    assert by_id["job_nightly_regression"]["timeout_sec"] == 7200
    assert "timeout_sec" not in by_id["disabled"]
