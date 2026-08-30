"""Registry-ready bounded fixtures for the final non-storage schedule batch.

The authoritative registries are intentionally not imported or modified here.
"""

from __future__ import annotations

import copy
import json
import shlex
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
JOBS = (
    "job_1770948489644_0726cf",
    "job_1770948489644_c5a469",
    "job_nightly_autopilot",
    "job_operational_hardening_audit",
)


def _contract(
    job_id: str, checks: tuple[str, ...], *, providers: dict[str, str]
) -> dict[str, Any]:
    equals: dict[str, Any] = {
        "schema": "magi.schedule-nonstorage-result/v1",
        "job_id": job_id,
        "success": True,
        "status": "passed",
        "safety.external_network_accessed": False,
        "safety.production_database_accessed": False,
        "safety.production_state_written": False,
        "safety.nas_accessed": False,
        "safety.writes_bounded_to_fixture": True,
        "safety.subprocess_spawned": False,
        "safety.dispatcher_invoked": False,
    }
    equals.update({f"safety.{key}": value for key, value in providers.items()})
    equals.update({f"checks.{check}": True for check in checks})
    return {
        "type": "json_file",
        "path": "outputs/result.json",
        "equals": equals,
        "minimum": {"fixture_sample_id": 1},
        "lengths": {},
    }


_COMMON_ENV = {
    "MAGI_V3_SCHEDULE_FIXTURE": "1",
    "MAGI_V3_SCHEDULE_FIXTURE_ROOT": "<FIXTURE>",
    "MAGI_RUNTIME_DIR": "<FIXTURE>/workspace/runtime",
    "MAGI_AGENT_DIR": "<FIXTURE>/workspace/agent",
}


_ADAPTERS: dict[str, dict[str, Any]] = {
    "job_1770948489644_0726cf": {
        "job_id": "job_1770948489644_0726cf",
        "production_entrypoint": "skills/memory/cortex_sync.py",
        "safety_class": "bounded_cortex_state_and_memory_provider",
        "fixture_kind": "product_cortex_sync_terminal",
        "argv": [
            "<PYTHON>",
            "<ROOT>/skills/memory/cortex_sync.py",
            "--schedule-fixture-root",
            "<FIXTURE>",
            "--json-out",
            "<FIXTURE>/outputs/result.json",
        ],
        "environment": dict(_COMMON_ENV),
        "success_contract": _contract(
            "job_1770948489644_0726cf",
            (
                "fixture_sample_bound",
                "typed_state_and_rows",
                "sync_reached_terminal_state",
                "added_counts_match_expected",
                "state_checkpoint_matches_expected",
                "memory_writes_use_isolated_provider",
                "no_dispatch_or_subprocess",
            ),
            providers={
                "database_provider": "fixture_source",
                "model_provider": "fixture_memory",
                "notification_provider": "not_used",
            },
        ),
    },
    "job_1770948489644_c5a469": {
        "job_id": "job_1770948489644_c5a469",
        "production_entrypoint": "skills/magi-autopilot/action.py",
        "safety_class": "bounded_autopilot_tick_state_repair_terminal",
        "fixture_kind": "product_autopilot_tick_terminal",
        "argv": [
            "<PYTHON>",
            "<ROOT>/skills/magi-autopilot/action.py",
            "--task",
            "tick",
            "--schedule-fixture-root",
            "<FIXTURE>",
            "--json-out",
            "<FIXTURE>/outputs/result.json",
        ],
        "environment": dict(_COMMON_ENV),
        "success_contract": {
            "type": "autopilot_terminal_fixture",
            "job_id": "job_1770948489644_c5a469",
            "task": "tick",
        },
    },
    "job_nightly_autopilot": {
        "job_id": "job_nightly_autopilot",
        "production_entrypoint": "skills/magi-autopilot/action.py",
        "safety_class": "bounded_autopilot_nightly_state_repair_terminal",
        "fixture_kind": "product_autopilot_nightly_terminal",
        "argv": [
            "<PYTHON>",
            "<ROOT>/skills/magi-autopilot/action.py",
            "--task",
            "nightly",
            "--schedule-fixture-root",
            "<FIXTURE>",
            "--json-out",
            "<FIXTURE>/outputs/result.json",
        ],
        "environment": dict(_COMMON_ENV),
        "success_contract": {
            "type": "autopilot_terminal_fixture",
            "job_id": "job_nightly_autopilot",
            "task": "nightly",
        },
    },
    "job_operational_hardening_audit": {
        "job_id": "job_operational_hardening_audit",
        "production_entrypoint": "scripts/ops/audit_operational_hardening.py",
        "safety_class": "bounded_operational_audit_and_repair_terminal",
        "fixture_kind": "product_operational_hardening_terminal",
        "argv": [
            "<PYTHON>",
            "<ROOT>/scripts/ops/audit_operational_hardening.py",
            "--cleanup-stale-locks",
            "--fail-on-red",
            "--schedule-fixture-root",
            "<FIXTURE>",
            "--json-out",
            "<FIXTURE>/outputs/result.json",
        ],
        "environment": dict(_COMMON_ENV),
        "success_contract": {
            "type": "operational_hardening_formal_fixture",
        },
    },
}


def adapter_proposals() -> list[dict[str, Any]]:
    return [copy.deepcopy(_ADAPTERS[job_id]) for job_id in JOBS]


def _news(row_id: int) -> dict[str, Any]:
    return {
        "id": row_id,
        "title": f"法律新聞 {row_id}",
        "snippet": "法院公告之程序與權利保障摘要。",
        "url": f"https://fixture.invalid/news/{row_id}",
        "published_date": "2026-07-17",
        "source": "fixture-gazette",
    }


def _judgment(row_id: int) -> dict[str, Any]:
    return {
        "id": row_id,
        "jid": f"fixture-{row_id}",
        "case_number": f"115年度台上字第{row_id}號",
        "court_name": "最高法院",
        "summary": "法院依證據與舉證責任判斷請求是否有理由。",
        "judgment_date": "2026-07-17",
    }


def _cortex_input(sample_id: int) -> dict[str, Any]:
    if sample_id == 1:
        return {
            "sample_id": sample_id,
            "initial_state": {},
            "legal_news": [_news(1), _news(2)],
            "judgments": [_judgment(4)],
            "expected_added": {"news": 2, "judgments": 1},
            "expected_final_state": {"legal_news_last_id": 2, "judgments_last_id": 4},
        }
    if sample_id == 2:
        return {
            "sample_id": sample_id,
            "initial_state": {"legal_news_last_id": 10, "judgments_last_id": 5},
            "legal_news": [_news(8), _news(12)],
            "judgments": [_judgment(5), _judgment(6)],
            "expected_added": {"news": 1, "judgments": 1},
            "expected_final_state": {"legal_news_last_id": 12, "judgments_last_id": 6},
        }
    return {
        "sample_id": sample_id,
        "initial_state": {"legal_news_last_id": 20, "judgments_last_id": 30},
        "legal_news": [],
        "judgments": [],
        "expected_added": {"news": 0, "judgments": 0},
        "expected_final_state": {"legal_news_last_id": 20, "judgments_last_id": 30},
    }


def _autopilot_input(task: str, sample_id: int) -> dict[str, Any]:
    prefix = "tick" if task == "tick" else "nightly"
    if sample_id == 1:
        steps = [
            {
                "name": f"{prefix}_model_health",
                "provider": "fixture_model",
                "initial_state": "healthy",
                "repair_result": "not_needed",
            },
            {
                "name": f"{prefix}_database_probe",
                "provider": "fixture_database",
                "initial_state": "healthy",
                "repair_result": "not_needed",
            },
        ]
        initial = {"runs": 0, "repairs": 0}
        expected_repairs = 0
    elif sample_id == 2:
        steps = [
            {
                "name": f"{prefix}_database_repair",
                "provider": "fixture_database",
                "initial_state": "degraded",
                "repair_result": "recovered",
            },
            {
                "name": f"{prefix}_audit_checkpoint",
                "provider": "fixture_internal",
                "initial_state": "healthy",
                "repair_result": "not_needed",
            },
        ]
        initial = {"runs": 4, "repairs": 1}
        expected_repairs = 2
    else:
        steps = [
            {
                "name": f"{prefix}_model_requires_human",
                "provider": "fixture_model",
                "initial_state": "blocked",
                "repair_result": "needs_human",
            },
            {
                "name": f"{prefix}_notification_audit",
                "provider": "fixture_notification",
                "initial_state": "healthy",
                "repair_result": "not_needed",
            },
        ]
        initial = {"runs": 9, "repairs": 3}
        expected_repairs = 3
    terminal = {
        step["name"]: {
            "healthy": "completed",
            "degraded": "recovered",
            "blocked": "needs_human",
        }[step["initial_state"]]
        for step in steps
    }
    return {
        "sample_id": sample_id,
        "task": task,
        "initial_state": initial,
        "steps": steps,
        "expected_terminal_states": terminal,
        "expected_repairs": expected_repairs,
    }


def _audit_input(sample_id: int) -> dict[str, Any]:
    python = ROOT / "venv" / "bin" / "python3"
    script = ROOT / "scripts" / "magi_doctor.py"
    jobs = [
        {
            "id": f"fixture_health_{sample_id}",
            "cron": "5 1 * * *",
            # A sealed MAGI release lives below ``Application Support`` on
            # macOS.  The production parser correctly honours shell quoting;
            # the fixture must therefore preserve each absolute path as one
            # argv element instead of accidentally testing a split path.
            "command": shlex.join([str(python), str(script), "--json"]),
        },
        {
            "id": f"fixture_macro_{sample_id}",
            "cron": "10 1 * * *",
            "command": "@MAGI 系統狀態",
        },
    ]
    if sample_id == 1:
        locks = [{"name": "scheduler", "state": "active"}]
        findings = [{"name": "gmail_mode", "ok": True}]
    elif sample_id == 2:
        locks = [
            {"name": "autopilot", "state": "stale"},
            {"name": "scheduler", "state": "active"},
        ]
        findings = [{"name": "gmail_mode", "ok": True}]
    else:
        locks = [
            {"name": "autopilot", "state": "stale"},
            {"name": "pdf_mutation", "state": "stale"},
        ]
        findings = [
            {"name": "omlx_profile", "ok": True},
            {"name": "osc_route_integrity", "ok": True},
        ]
    repaired_locks = [lock["name"] for lock in locks if lock["state"] == "stale"]
    return {
        "sample_id": sample_id,
        "cron_jobs": jobs,
        "locks": locks,
        "findings": findings,
        "expected": {
            "parse_failure_count": 0,
            "collision_count": 0,
            "initial_red_count": len(repaired_locks),
            "repaired_locks": repaired_locks,
            "terminal_state": "green",
        },
    }


def _product_input(job_id: str, sample_id: int) -> dict[str, Any]:
    if job_id == "job_1770948489644_0726cf":
        return _cortex_input(sample_id)
    if job_id == "job_1770948489644_c5a469":
        return _autopilot_input("tick", sample_id)
    if job_id == "job_nightly_autopilot":
        return _autopilot_input("nightly", sample_id)
    if job_id == "job_operational_hardening_audit":
        return _audit_input(sample_id)
    raise ValueError(f"unsupported non-storage fixture job: {job_id}")


def populate_nonstorage_fixture(
    fixture_root: Path, *, job_id: str, sample_id: int
) -> dict[str, Any]:
    if job_id not in JOBS or type(sample_id) is not int or not 1 <= sample_id <= 3:
        raise ValueError("non-storage fixture job/sample is invalid")
    root = fixture_root.resolve(strict=True)
    (root / "inputs").mkdir(mode=0o700, exist_ok=False)
    manifest = {
        "schema": "magi.schedule-product-fixture/v1",
        "job_id": job_id,
        "product_input": _product_input(job_id, sample_id),
    }
    (root / "fixture.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


__all__ = ["JOBS", "adapter_proposals", "populate_nonstorage_fixture"]
