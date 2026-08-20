from __future__ import annotations

import json

from scripts.ops import run_auto_skill_import as daily
from skills.management import auto_skill


class _NoChangeAutoSkill:
    def __init__(self):
        self.knowledge = [
            {"context": "toolsai-auto-skill-kb", "tip": "known"},
            {"context": "manual", "tip": "local"},
        ]

    def import_toolsai_auto_skill(self, **_kwargs):
        return {
            "success": True,
            "learned": 0,
            "imported_files": [{"file": "/private/source.md", "learned": 0}],
            "skipped": [],
            "vector_mirror": {"success": True, "mirrored": 0},
            "repo": "https://should-not-appear.invalid/project.git",
        }


class _ImprovedAutoSkill(_NoChangeAutoSkill):
    def __init__(self):
        self.knowledge = [
            {"context": "toolsai-auto-skill-kb", "tip": f"known-{idx}"}
            for idx in range(100)
        ]

    def import_toolsai_auto_skill(self, **_kwargs):
        self.knowledge.append({"context": "toolsai-auto-skill-exp", "tip": "new"})
        return {
            "success": True,
            "learned": 1,
            "imported_files": [{"file": "/private/source.md", "learned": 1}],
            "skipped": [],
            "vector_mirror": {"success": True, "mirrored": 1},
        }


def test_daily_evolution_reports_honest_zero_without_repository_link(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(auto_skill, "AutoSkill", _NoChangeAutoSkill)
    monkeypatch.setattr(
        daily,
        "_plan_controlled_candidates",
        lambda _runtime: {
            "ok": True,
            "new_proposal_count": 0,
            "open_proposal_count": 0,
            "proposal_ids": [],
            "auto_deploy": False,
        },
    )

    result = daily.run_daily_evolution(notify=False)

    assert result["status"] == "no_measurable_change"
    assert result["metrics"]["knowledge_new"] == 0
    assert result["metrics"]["capability_gain_percent"] == 0.0
    assert result["metrics"]["target_met"] is False
    assert result["deployment_policy"]["auto_deploy"] is False
    assert "http" not in daily._summary_text(result)
    assert "should-not-appear" not in json.dumps(result, ensure_ascii=False)
    receipt = json.loads((tmp_path / "daily_self_evolution_latest.json").read_text())
    assert receipt["status"] == "no_measurable_change"


def test_daily_evolution_counts_persisted_one_percent_gain(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(auto_skill, "AutoSkill", _ImprovedAutoSkill)
    monkeypatch.setattr(
        daily,
        "_plan_controlled_candidates",
        lambda _runtime: {
            "ok": True,
            "new_proposal_count": 0,
            "open_proposal_count": 0,
            "proposal_ids": [],
            "auto_deploy": False,
        },
    )

    result = daily.run_daily_evolution(notify=False)

    assert result["status"] == "improved"
    assert result["metrics"]["knowledge_before"] == 100
    assert result["metrics"]["knowledge_after"] == 101
    assert result["metrics"]["knowledge_new"] == 1
    assert result["metrics"]["capability_gain_percent"] == 1.0
    assert result["metrics"]["target_met"] is True


def test_daily_evolution_proposals_never_claim_deployment(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(auto_skill, "AutoSkill", _NoChangeAutoSkill)
    monkeypatch.setattr(
        daily,
        "_plan_controlled_candidates",
        lambda _runtime: {
            "ok": True,
            "new_proposal_count": 1,
            "open_proposal_count": 1,
            "proposal_ids": ["ce-safe"],
            "auto_deploy": False,
        },
    )

    result = daily.run_daily_evolution(notify=False)

    assert result["status"] == "candidate_planned"
    assert result["controlled_candidates"]["auto_deploy"] is False
    assert result["deployment_policy"]["auto_deploy"] is False
    assert result["metrics"]["target_met"] is False


def test_existing_open_proposal_is_not_misreported_as_daily_evolution(tmp_path, monkeypatch):
    monkeypatch.setenv("MAGI_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(auto_skill, "AutoSkill", _NoChangeAutoSkill)
    monkeypatch.setattr(
        daily,
        "_plan_controlled_candidates",
        lambda _runtime: {
            "ok": True,
            "new_proposal_count": 0,
            "open_proposal_count": 1,
            "proposal_ids": ["ce-existing"],
            "auto_deploy": False,
        },
    )

    result = daily.run_daily_evolution(notify=False)

    assert result["status"] == "no_measurable_change"
    assert result["controlled_candidates"]["open_proposal_count"] == 1


def test_daily_evolution_reads_only_fresh_aggregate_business_failures(tmp_path):
    (tmp_path / "business_module_live_check_latest.json").write_text(
        json.dumps(
            {
                "generated_at": daily.datetime.now(daily.timezone.utc).isoformat(),
                "results": {
                    "calendar_todo_status_live": {
                        "ok": False,
                        "parsed": {
                            "reason": "calendar_import_failed",
                            "private_path": "/private/case.pdf",
                        },
                    },
                    "notification_delivery_status_live": {"ok": True},
                },
            }
        ),
        encoding="utf-8",
    )

    signals = daily._open_business_signals(tmp_path)

    assert signals == [
        {
            "id": "business:calendar_todo_status_live",
            "source": "business_module_live_check",
            "category": "calendar_todo_status_live",
            "severity": "error",
            "status": "open",
            "reason_code": "calendar_import_failed",
            "summary": "calendar_todo_status_live",
        }
    ]
    assert "/private/" not in json.dumps(signals)
