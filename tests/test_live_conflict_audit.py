from __future__ import annotations

import json
import plistlib
from pathlib import Path

from scripts.ops import business_module_live_check as live_check


def _skill(root: Path, name: str, body: str = "") -> None:
    path = root / "skills" / name
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(body or f"---\nname: {name}\n---\n# {name}\n", encoding="utf-8")


def test_conflict_audit_flags_duplicate_skills_but_allows_internal_alias(tmp_path):
    _skill(tmp_path, "case-helper", "---\nname: case-helper\n---\n# case-helper\n")
    _skill(tmp_path, "case_helper", "---\nname: case_helper\n---\n# case_helper\n")
    _skill(tmp_path, "iron-dome", "---\nname: iron-dome\n---\n# iron-dome\n")
    _skill(
        tmp_path,
        "iron_dome",
        "---\nname: iron_dome\ntype: internal-alias\nalias_of: iron-dome\n---\n# alias\n",
    )

    report = live_check._audit_duplicate_skills(tmp_path)

    assert report["ok"] is False
    assert report["duplicate_count"] == 1
    assert report["duplicates"][0]["normalized"] == "case-helper"
    assert report["allowed_alias_count"] == 1


def test_conflict_audit_detects_legacy_dispatch_and_deprecated_auto_route(tmp_path):
    _skill(
        tmp_path,
        "pdf-annotator",
        "---\nname: pdf-annotator\nmetadata:\n  deprecated: true\n---\n# pdf-annotator\n",
    )
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "single_source_of_truth.json").write_text(
        json.dumps(
            {
                "features": {
                    "file_review_monitor": {
                        "canonical": "skills.file-review-orchestrator.action",
                        "legacy_modules": ["legacy.file_review"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    pipeline = tmp_path / "api" / "pipelines"
    pipeline.mkdir(parents=True)
    (pipeline / "skill_dispatch.py").write_text(
        "from legacy.file_review import OldDispatcher\nsafe_skills = {'pdf_annotate': 'command'}\n",
        encoding="utf-8",
    )

    report = live_check._audit_deprecated_auto_dispatch(tmp_path)

    assert report["ok"] is False
    assert report["legacy_hit_count"] == 1
    assert report["legacy_hits"][0]["feature"] == "file_review_monitor"
    assert report["deprecated_auto_route_count"] == 1
    assert report["deprecated_auto_routes"][0]["alias"] == "pdf_annotate"


def test_conflict_audit_detects_cron_launchd_dual_executor(tmp_path):
    (tmp_path / "config" / "launchagents").mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_worker",
                    "enabled": True,
                    "cron": "*/5 * * * *",
                    "command": "/Users/ai/Desktop/MAGI_v2/venv/bin/python /Users/ai/Desktop/MAGI_v2/scripts/ops/worker.py",
                }
            ]
        ),
        encoding="utf-8",
    )
    plist_data = {
        "Label": "com.magi.worker",
        "ProgramArguments": [
            "/Users/ai/Desktop/MAGI_v2/venv/bin/python",
            "/Users/ai/Desktop/MAGI_v2/scripts/ops/worker.py",
        ],
        "KeepAlive": True,
    }
    (tmp_path / "config" / "launchagents" / "com.magi.worker.plist").write_bytes(plistlib.dumps(plist_data))

    report = live_check._audit_cron_dual_executor(tmp_path)

    assert report["ok"] is False
    assert report["conflicts"][0]["script"] == "scripts/ops/worker.py"


def test_conflict_audit_ignores_one_shot_launchd_restore(tmp_path):
    (tmp_path / "config" / "launchagents").mkdir(parents=True)
    (tmp_path / "cron_jobs.json").write_text(
        json.dumps(
            [
                {
                    "id": "job_omlx_profile_guard",
                    "enabled": True,
                    "cron": "*/15 * * * *",
                    "command": "/Users/ai/Desktop/MAGI_v2/config/bin/omlx_switch_model.sh auto",
                }
            ]
        ),
        encoding="utf-8",
    )
    plist_data = {
        "Label": "com.magi.omlx-restore",
        "ProgramArguments": ["/bin/bash", "-c", "/Users/ai/Desktop/MAGI_v2/config/bin/omlx_switch_model.sh auto"],
        "RunAtLoad": True,
        "KeepAlive": False,
    }
    (tmp_path / "config" / "launchagents" / "com.magi.omlx-restore.plist").write_bytes(plistlib.dumps(plist_data))

    report = live_check._audit_cron_dual_executor(tmp_path)

    assert report["ok"] is True
    assert report["conflict_count"] == 0


def test_conflict_audit_detects_high_risk_endpoint_collision(tmp_path):
    api = tmp_path / "api"
    api.mkdir()
    (api / "a.py").write_text("@app.route('/line/webhook', methods=['POST'])\ndef a(): pass\n", encoding="utf-8")
    (api / "b.py").write_text("@bp.route('/line/webhook', methods=['GET', 'POST'])\ndef b(): pass\n", encoding="utf-8")

    report = live_check._audit_high_risk_endpoint_collisions(tmp_path)

    assert report["ok"] is False
    assert report["collision_count"] == 1
    assert report["collisions"][0]["route"] == "/line/webhook"
    assert report["collisions"][0]["method"] == "POST"


def test_live_conflict_audit_payload_includes_validation_commands(tmp_path):
    (tmp_path / "cron_jobs.json").write_text("[]", encoding="utf-8")

    report = live_check.audit_live_conflicts(tmp_path)

    assert report["ok"] is True
    assert {"production_live", "business_modules", "conflict_audit", "manual_probe"} <= set(report["commands"])
