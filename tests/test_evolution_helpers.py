from __future__ import annotations

from pathlib import Path

from skills.engine.trajectory_compressor import TrajectoryCompressor
from skills.engine.user_insights import UserInsightsEngine
from skills.evolution.skill_improver import build_improvement_plan
from skills.evolution.skill_scorer import score_skill_run
from skills.evolution.usage_tracker import UsageTracker


def test_trajectory_compressor_preserves_milestones():
    compressor = TrajectoryCompressor()
    messages = [{"role": "system", "content": "你是 MAGI"}]
    messages.extend({"role": "user", "content": f"一般訊息 {idx}"} for idx in range(80))
    messages.append({"role": "user", "content": "請處理法扶與閱卷流程"})

    result = compressor.compress(messages, max_tokens=80)

    assert result[0]["role"] == "system"
    assert any("法扶" in msg["content"] for msg in result)
    assert len(result) <= 20


def test_user_insights_engine_reports_top_skills(tmp_path: Path):
    path = tmp_path / "events.jsonl"
    tracker = UsageTracker(str(path))
    tracker.record("judgment-collector", True, 900, intent="實務見解")
    tracker.record("judgment-collector", True, 1100, intent="實務見解")
    tracker.record("statutes-vdb", False, 600, intent="法規搜尋", failure_reason="timeout")

    engine = UserInsightsEngine(str(path))
    insights = engine.extract_insights(days=7)
    summary = tracker.summarize(days=7)

    assert insights["event_count"] == 3
    assert insights["top_skills"][0][0] == "judgment-collector"
    assert summary["top_failure_reason"] == "timeout"
    assert "高頻失敗原因：timeout" in tracker.daily_report(days=7)
    assert "常用技能" in engine.get_personalization_context(days=7)


def test_skill_scorer_and_improver():
    scored = score_skill_run({"skill": "judgment-collector", "success": False, "latency_ms": 12000})
    assert scored["bucket"] == "needs_improvement"

    plan = build_improvement_plan(
        "judgment-collector",
        {"success_rate": 0.4, "top_failure_reason": "timeout"},
    )
    assert plan["skill"] == "judgment-collector"
    assert any("timeout" in item for item in plan["suggestions"])
