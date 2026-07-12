import importlib.util
import sys
from pathlib import Path


def test_nightly_diagnostic_is_non_destructive(monkeypatch, tmp_path: Path):
    action_path = Path(__file__).resolve().parents[1] / "skills" / "magi-autopilot" / "action.py"
    spec = importlib.util.spec_from_file_location("magi_autopilot_diagnostic_test", action_path)
    assert spec and spec.loader
    action = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = action
    spec.loader.exec_module(action)

    skill_root = tmp_path / "skills"
    for name in (
        "iron-dome",
        "osc-orchestrator",
        "file-review-orchestrator",
        "transcript-downloader",
    ):
        path = skill_root / name / "action.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    run_dir = tmp_path / "runs" / "nightly"
    run_dir.mkdir(parents=True)
    monkeypatch.setenv("MAGI_NIGHTLY_DIAGNOSTIC_MODE", "1")
    monkeypatch.setenv("MAGI_NIGHTLY_TOTAL_BUDGET_SEC", "90")
    monkeypatch.setattr(action, "MAGI_ROOT_DIR", str(tmp_path))
    monkeypatch.setattr(action, "_skill_action", lambda name: str(skill_root / name / "action.py"))
    monkeypatch.setattr(action, "_cron_job_enabled", lambda job_id: job_id == "job_nightly_autopilot")

    result = action.run_nightly(str(run_dir))

    assert result["ok"] is True
    assert result["diagnostic_mode"] is True
    assert result["non_destructive"] is True
    assert result["steps"] == {}
    assert result["configured_budget_sec"] == 90
