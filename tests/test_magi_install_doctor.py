from pathlib import Path

from scripts.install_magi import build_install_plan, venv_python
from scripts.magi_doctor import collect_report
from scripts.seed_cron_jobs import default_python_path, seed_jobs


def test_doctor_collect_report_without_live_probe_has_expected_shape():
    report = collect_report(live=False)

    assert {"ok", "status", "system", "summary", "checks"} <= set(report)
    assert any(item["name"] == "python" for item in report["checks"])


def test_install_plan_uses_requested_venv_dir():
    venv_dir = Path("/tmp/magi-test-venv")
    plan = build_install_plan(include_optional=False, venv_dir=venv_dir)

    assert [step.name for step in plan] == ["create_venv", "upgrade_pip", "install_core", "seed_cron_jobs", "doctor"]
    assert str(venv_python(venv_dir)) in plan[1].command


def test_seed_cron_jobs_creates_worldmonitor_and_business_jobs(tmp_path):
    result = seed_jobs(tmp_path, python_path=tmp_path / ".venv" / "bin" / "python")
    cron_text = (tmp_path / "cron_jobs.json").read_text(encoding="utf-8")

    assert result["ok"] is True
    assert "job_worldmonitor_intel" in cron_text
    assert "worldmonitor-intel/action.py --task collect --no-reasoning --plain-output" in cron_text
    assert "job_laf_nightly_audit" in cron_text
    assert "job_laf_condition_draft" in cron_text
    assert "job_file_review_check" in cron_text
    assert "job_transcript_sync" in cron_text
    assert "job_business_module_live_check" in cron_text
    assert "job_omlx_profile_guard" in cron_text
    assert "omlx_switch_model.sh auto" in cron_text
    assert "job_distill_train_gemma" in cron_text
    assert "pdfnamer_docling_layout" in cron_text
    assert "MAGI_PDF_NAMER_DOCLING_ENABLED=1" in cron_text


def test_seed_cron_jobs_default_python_matches_safe_process(tmp_path, monkeypatch):
    monkeypatch.delenv("MAGI_CRON_PYTHON", raising=False)
    monkeypatch.delenv("MAGI_VENV_DIR", raising=False)

    result = seed_jobs(tmp_path)
    cron_text = (tmp_path / "cron_jobs.json").read_text(encoding="utf-8")

    assert result["ok"] is True
    assert str(default_python_path(tmp_path)) == str(tmp_path / "venv" / "bin" / "python3")
    assert f"{tmp_path}/.venv/bin/python" not in cron_text
    assert f"{tmp_path}/venv/bin/python3" in cron_text
    assert '"id": "job_resource_governor"' in cron_text
    assert '"cron": "20 * * * *"' in cron_text
