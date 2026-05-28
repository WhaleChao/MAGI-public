from __future__ import annotations

import json

from api.domains import judicial_api_policy as policy


def test_tlr_smart_policy_is_low_load(monkeypatch):
    monkeypatch.delenv("MAGI_JUDICIAL_API_LOAD_MODE", raising=False)

    report = policy.judicial_api_policy_report()

    assert report["mode"] == "tlr_smart"
    assert report["enable_day_process"] == "0"
    assert int(report["night_max_jdocs"]) <= 300
    assert int(report["day_max_docs"]) <= 60
    assert report["day_summary_mode"] == "extractive"
    assert report["day_skip_assets"] == "1"
    assert report["day_vector_ingest"] == "0"


def test_legacy_policy_preserves_old_manual_escape_hatch(monkeypatch):
    monkeypatch.setenv("MAGI_JUDICIAL_API_LOAD_MODE", "legacy")

    report = policy.judicial_api_policy_report()

    assert report["mode"] == "legacy"
    assert report["night_max_jdocs"] == "25000"
    assert report["day_summary_mode"] == "llm"
    assert report["day_vector_ingest"] == "1"


def test_tune_judicial_api_load_disables_heavy_day_batches(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGI_JUDICIAL_API_LOAD_MODE", raising=False)
    cron_path = tmp_path / "cron_jobs.json"
    cron_path.write_text(
        json.dumps(
            [
                {"id": "job_judicial_api_night_pull", "command": "old", "enabled": True},
                {"id": "job_judicial_api_morning", "command": "old", "enabled": True},
                {"id": "job_judicial_api_noon", "command": "old", "enabled": True},
                {"id": "job_judicial_api_afternoon", "command": "old", "enabled": True},
                {"id": "job_judicial_api_evening", "command": "old", "enabled": True},
                {"id": "job_judicial_api_backlog_clear", "command": "old", "enabled": True},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from scripts.ops.tune_judicial_api_load import tune_jobs

    result = tune_jobs(cron_path, apply=True)
    jobs = {job["id"]: job for job in json.loads(cron_path.read_text(encoding="utf-8"))}

    assert result["ok"] is True
    assert jobs["job_judicial_api_night_pull"]["enabled"] is True
    assert '\\"max_jdocs\\":300' in jobs["job_judicial_api_night_pull"]["command"]
    assert jobs["job_judicial_api_morning"]["enabled"] is True
    assert '\\"max_docs\\":60' in jobs["job_judicial_api_morning"]["command"]
    assert jobs["job_judicial_api_noon"]["enabled"] is False
    assert jobs["job_judicial_api_backlog_clear"]["enabled"] is False
