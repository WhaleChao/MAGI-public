from pathlib import Path

from scripts import first_run_setup
from scripts.install_magi import build_install_plan, venv_python
from scripts.magi_doctor import collect_report
from scripts.seed_cron_jobs import seed_jobs


def test_doctor_collect_report_without_live_probe_has_expected_shape():
    report = collect_report(live=False)

    assert {"ok", "status", "system", "summary", "checks"} <= set(report)
    assert any(item["name"] == "python" for item in report["checks"])


def test_install_plan_uses_requested_venv_dir():
    venv_dir = Path("/tmp/magi-test-venv")
    plan = build_install_plan(include_optional=False, venv_dir=venv_dir)

    assert [step.name for step in plan] == ["create_venv", "upgrade_pip", "install_core", "seed_cron_jobs", "doctor"]
    assert str(venv_python(venv_dir)) in plan[1].command


def test_first_run_checklist_reports_missing_env_without_leaking_values(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "FLASK_SECRET_KEY=abc123",
                "MAGI_API_KEY=def456",
                "DB_HOST=127.0.0.1",
                "DB_USER=casper",
                "DB_PASSWORD=<your-db-password>",
                "DISCORD_BOT_TOKEN=secret-token-value",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(first_run_setup, "_public_isolation_findings", lambda: [])

    result = first_run_setup.build_first_run_checklist(public_mode=True, env_path=env_path)
    joined = str(result)

    assert result["ok"] is True
    assert result["summary"]["warn"] >= 1
    assert "DB_PASSWORD" in joined
    assert "secret-token-value" not in joined
    assert "abc123" not in joined


def test_first_run_write_env_generates_local_secrets(tmp_path, monkeypatch):
    example = tmp_path / ".env.example"
    env = tmp_path / ".env"
    example.write_text(
        "\n".join(
            [
                "FLASK_SECRET_KEY=<random-hex-string>",
                "MAGI_API_KEY=<random-hex-string>",
                "MAGI_ROOT_DIR=/path/to/MAGI_v2",
                "MAGI_SKILL_PYTHON=/path/to/MAGI_v2/venv/bin/python3",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(first_run_setup, "ENV_EXAMPLE", example)
    monkeypatch.setattr(first_run_setup, "REPO_ROOT", tmp_path)

    result = first_run_setup._write_env_from_example(env)
    text = env.read_text(encoding="utf-8")

    assert result["created"] is True
    assert "<random-hex-string>" not in text
    assert f"MAGI_ROOT_DIR={tmp_path}" in text


def test_first_run_public_mode_flags_private_markers(monkeypatch, tmp_path):
    monkeypatch.setattr(first_run_setup, "_public_isolation_findings", lambda: ["skills/private-legal-db/action.py"])

    result = first_run_setup.build_first_run_checklist(public_mode=True, env_path=tmp_path / ".env")

    assert result["ok"] is False
    assert result["summary"]["fail"] == 1


def test_seed_cron_jobs_creates_worldmonitor_daily_job(tmp_path):
    result = seed_jobs(tmp_path, python_path=tmp_path / ".venv" / "bin" / "python")
    cron_text = (tmp_path / "cron_jobs.json").read_text(encoding="utf-8")

    assert result["ok"] is True
    assert "job_worldmonitor_intel" in cron_text
    assert "worldmonitor-intel/action.py --task collect --no-reasoning --plain-output" in cron_text
