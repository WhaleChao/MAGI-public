from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scripts.ops import function_health_index as index


NOW = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: object) -> None:
    _write(path, json.dumps(payload, ensure_ascii=False, indent=2))


def test_build_index_discovers_inventory_and_health_findings(tmp_path: Path):
    _write(
        tmp_path / "api" / "blueprints" / "demo.py",
        """
from flask import Blueprint
bp = Blueprint("demo", __name__)

@bp.route("/api/osc/cases", methods=["GET", "POST"])
def cases():
    pass

@bp.route("/health")
def health():
    pass
""",
    )
    _write(
        tmp_path / "api" / "server.py",
        """
@app.route("/telegram/webhook", methods=["POST"])
def telegram():
    pass
""",
    )
    _write(
        tmp_path / "skills" / "translator" / "SKILL.md",
        "---\nname: translator\ndescription: Translate text\n---\n",
    )
    _write(tmp_path / "skills" / "translator" / "action.py", "print('ok')\n")
    _write(tmp_path / "skills" / "action-only" / "action.py", "print('ok')\n")
    _write(tmp_path / "skills" / "doc-only" / "SKILL.md", "# Doc only\n")

    _write_json(
        tmp_path / "config" / "test_matrix.json",
        {
            "suites": {
                "ci": {
                    "checks": [
                        {
                            "id": "function_health_index",
                            "name": "Function health index",
                            "command": [
                                "{python}",
                                "scripts/ops/function_health_index.py",
                                "--json-out",
                                ".runtime/function_health_index_ci_latest.json",
                            ],
                        },
                        {
                            "id": "health",
                            "name": "Health",
                            "command": [
                                "{python}",
                                "scripts/ops/demo.py",
                                "--json-out",
                                ".runtime/missing_check_latest.json",
                            ],
                        }
                    ]
                }
            }
        },
    )
    _write_json(
        tmp_path / "cron_jobs.json",
        [
            {
                "id": "job_ok",
                "enabled": True,
                "cron": "0 * * * *",
                "command": "python scripts/ops/demo.py --json-out .runtime/job_ok_latest.json",
            },
            {
                "id": "job_missing",
                "enabled": True,
                "cron": "0 8 * * *",
                "command": "@MAGI 系統狀態",
            },
            {
                "id": "job_stale",
                "enabled": True,
                "cron": "0 8 * * *",
                "command": "python scripts/ops/stale.py",
            },
            {
                "id": "job_disabled",
                "enabled": False,
                "cron": "0 8 * * *",
                "command": "python scripts/ops/disabled.py",
            },
        ],
    )
    _write_json(
        tmp_path / ".runtime" / "cron_state.json",
        {
            "job_ok": {"last_run": "2026-06-29T11:00:00+00:00", "ok": True},
            "job_stale": {"last_run": "2026-06-01T08:00:00+00:00", "ok": True},
        },
    )
    _write_json(tmp_path / ".runtime" / "job_ok_latest.json", {"ok": False, "generated_at": NOW.isoformat()})
    _write_json(tmp_path / ".runtime" / "old_latest.json", {"ok": True, "generated_at": "2026-06-20T00:00:00+00:00"})

    report = index.build_index(
        root=tmp_path,
        matrix_path=tmp_path / "config" / "test_matrix.json",
        runtime_dir=tmp_path / ".runtime",
        now=NOW,
        max_health_age_hours=24,
        include_static=False,
    )

    assert report["ok"] is False
    assert report["api_routes"]["domains"]["osc"]["count"] == 1
    assert report["api_routes"]["domains"]["health"]["count"] == 1
    assert report["api_routes"]["domains"]["webhooks"]["count"] == 1
    assert report["skills"]["total"] == 3
    assert "action-only" in report["skills"]["missing_skill_md"]
    assert "doc-only" in report["skills"]["missing_action"]
    assert any(item["id"] == "job_missing" for item in report["cron_jobs"]["missing_state"])
    assert any(item["id"] == "job_stale" for item in report["cron_jobs"]["stale"])
    assert any(item["path"] == ".runtime/job_ok_latest.json" for item in report["runtime_health"]["failed"])
    assert report["summary"]["observed_stale_health_count"] == 0
    assert any(
        item["path"] == ".runtime/old_latest.json"
        for item in report["runtime_health"]["artifact_hygiene"]["archived_observed_stale"]
    )
    assert any(item["path"] == ".runtime/missing_check_latest.json" for item in report["runtime_health"]["missing"])
    assert not any(
        item["path"] == ".runtime/function_health_index_ci_latest.json"
        for item in report["runtime_health"]["expected"]
    )


def test_cli_writes_json_and_does_not_fail_without_fail_on_health(tmp_path: Path, capsys):
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})

    out = tmp_path / ".runtime" / "function_health_index_latest.json"
    rc = index.main([
        "--root",
        str(tmp_path),
        "--json-out",
        str(out),
        "--no-static",
        "--compact",
    ])

    assert rc == 0
    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["summary"]["test_suite_count"] == 1
    assert payload["summary"]["missing_health_count"] >= 1
    printed = json.loads(capsys.readouterr().out)
    assert printed["runtime_dir"] == ".runtime"


def test_fail_on_health_returns_nonzero_for_missing_health(tmp_path: Path):
    _write_json(tmp_path / "config" / "test_matrix.json", {"suites": {"ci": {"checks": []}}})

    rc = index.main([
        "--root",
        str(tmp_path),
        "--no-static",
        "--compact",
        "--fail-on-health",
    ])

    assert rc == 1
