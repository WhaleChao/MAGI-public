from __future__ import annotations

from pathlib import Path

from skills.evolution import skill_genesis


def test_track_skill_usage_builds_score_and_plan(tmp_path: Path, monkeypatch):
    usage_path = tmp_path / "usage.jsonl"
    monkeypatch.setattr(skill_genesis, "SKILL_USAGE_TRACKER_FILE", str(usage_path))

    payload = skill_genesis._track_skill_usage(
        "judgment-collector",
        {
            "success": False,
            "error": "timeout",
            "trace": [{"duration_ms": 12050}],
            "auto_repaired": False,
        },
        task="實務見解 侵權行為",
    )

    assert payload["score"]["bucket"] == "needs_improvement"
    assert payload["summary_7d"]["top_failure_reason"] == "timeout"
    assert any("timeout" in item for item in payload["improvement_plan"]["suggestions"])
    assert usage_path.exists()


def test_run_skill_action_attaches_usage_tracking(monkeypatch, tmp_path: Path):
    skill_dir = tmp_path / "dummy-skill"
    skill_dir.mkdir()
    (skill_dir / "action.py").write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr(
        skill_genesis,
        "_resolve_run_target",
        lambda skill, route_key="", force_non_canary=False: {
            "success": True,
            "skill_dir": str(skill_dir),
            "channel": "live",
            "version_id": "",
            "state": {},
        },
    )
    monkeypatch.setattr(
        skill_genesis,
        "_isolated_run",
        lambda cmd, cwd, timeout_sec: {"rc": 0, "stdout": "ok", "stderr": "", "duration_ms": 321},
    )
    monkeypatch.setattr(skill_genesis, "_ensure_skill_runtime_dependencies", lambda *args, **kwargs: {"installed": [], "errors": []})
    monkeypatch.setattr(skill_genesis, "_record_skill_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(skill_genesis, "SKILL_USAGE_TRACKER_FILE", str(tmp_path / "usage.jsonl"))

    result = skill_genesis.run_skill_action("dummy-skill", "help", auto_repair=False, auto_install_deps=False)

    assert result["success"] is True
    assert result["usage_tracking"]["score"]["bucket"] == "good"
