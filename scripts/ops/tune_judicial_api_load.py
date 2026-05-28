#!/usr/bin/env python3
"""Tune local Judicial Yuan API cron jobs for MAGI's TLR-smart load mode."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.domains.judicial_api_policy import DEFAULT_LOAD_MODE, judicial_api_policy_report  # noqa: E402


def _python_bin(root: Path) -> str:
    candidate = root / "venv" / "bin" / "python3"
    return str(candidate if candidate.exists() else Path(sys.executable))


def _job_command(root: Path, job_id: str, task_name: str, payload: dict[str, Any], *, window_env: bool = False) -> str:
    py = _python_bin(root)
    resource_guard = root / "scripts" / "ops" / "resource_guarded_run.py"
    run_with_env = root / "scripts" / "ops" / "run_with_env.py"
    collector = root / "skills" / "judgment-collector" / "action.py"
    env_parts = [
        f"MAGI_JUDICIAL_API_LOAD_MODE={judicial_api_policy_report().get('mode') or DEFAULT_LOAD_MODE}",
        "MAGI_PREFER_LOCAL_DB=1",
        "MAGI_NO_DELETE=1",
    ]
    if window_env:
        env_parts.extend(["JUDICIAL_API_WINDOW_START_HOUR=0", "JUDICIAL_API_WINDOW_END_HOUR=6"])
    task = f"{task_name} {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}"
    return (
        f"{py} {resource_guard} --job-id {job_id} --block-at throttle -- "
        f"{py} {run_with_env} {' '.join(env_parts)} -- "
        f"{py} {collector} --task {json.dumps(task, ensure_ascii=False)}"
    )


def _find(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any] | None:
    for job in jobs:
        if str(job.get("id") or "") == job_id:
            return job
    return None


def tune_jobs(path: Path, *, apply: bool = False) -> dict[str, Any]:
    policy = judicial_api_policy_report()
    jobs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not isinstance(jobs, list):
        raise SystemExit(f"{path} is not a cron job list")

    night_payload = {
        "max_jdocs": int(policy["night_max_jdocs"]),
        "max_days": int(policy["night_max_days"]),
        "force": False,
        "notify": True,
    }
    day_payload = {
        "max_docs": int(policy["day_max_docs"]),
        "summarize_max": int(policy["day_summary_max"]),
        "summary_mode": policy["day_summary_mode"],
        "skip_assets": policy["day_skip_assets"] in {"1", "true", "yes", "on"},
        "vector_ingest": policy["day_vector_ingest"] in {"1", "true", "yes", "on"},
        "force": False,
        "notify": False,
    }

    desired: dict[str, dict[str, Any]] = {
        "job_judicial_api_night_pull": {
            "cron": "5 0 * * *",
            "command": _job_command(path.parent, "job_judicial_api_night_pull", "official_api_night_pull", night_payload, window_env=True),
            "desc": "司法院API小量增量夜拉（TLR 智慧低負載；只補近 2 日快取）",
            "enabled": True,
            "timeout_sec": int(policy["night_timeout_sec"]),
            "no_catchup": True,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        "job_judicial_api_morning": {
            "cron": "40 6 * * *",
            "command": _job_command(path.parent, "job_judicial_api_morning", "official_api_day_process", {**day_payload, "notify": True}),
            "desc": "司法院API晨間小量整理（TLR 智慧低負載；抽取摘要、不下載附件）",
            "enabled": True,
            "timeout_sec": int(policy["day_timeout_sec"]),
            "no_catchup": True,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        "job_judicial_api_noon": {
            "cron": "30 11 * * *",
            "command": _job_command(path.parent, "job_judicial_api_noon", "official_api_day_process", day_payload),
            "desc": "司法院API午批（已停用；TLR 智慧低負載模式避免白天大批次）",
            "enabled": False,
            "timeout_sec": int(policy["day_timeout_sec"]),
            "no_catchup": True,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        "job_judicial_api_afternoon": {
            "cron": "30 15 * * *",
            "command": _job_command(path.parent, "job_judicial_api_afternoon", "official_api_day_process", day_payload),
            "desc": "司法院API午後批（已停用；TLR 智慧低負載模式避免白天大批次）",
            "enabled": False,
            "timeout_sec": int(policy["day_timeout_sec"]),
            "no_catchup": True,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        "job_judicial_api_evening": {
            "cron": "30 19 * * *",
            "command": _job_command(path.parent, "job_judicial_api_evening", "official_api_day_process", day_payload),
            "desc": "司法院API晚批（已停用；TLR 智慧低負載模式避免白天大批次）",
            "enabled": False,
            "timeout_sec": int(policy["day_timeout_sec"]),
            "no_catchup": True,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        "job_judicial_api_backlog_clear": {
            "cron": "30 21 * * *",
            "command": _job_command(path.parent, "job_judicial_api_backlog_clear", "official_api_day_process", {**day_payload, "notify": True}),
            "desc": "司法院API backlog 大批次清理（已停用；改由 TLR 查詢與小量快取）",
            "enabled": False,
            "timeout_sec": int(policy["day_timeout_sec"]),
            "no_catchup": True,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
    }

    changed: list[str] = []
    missing: list[str] = []
    for job_id, patch in desired.items():
        job = _find(jobs, job_id)
        if not job:
            missing.append(job_id)
            continue
        before = dict(job)
        job.update(patch)
        if job != before:
            changed.append(job_id)

    if apply and changed:
        path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "path": str(path), "mode": policy["mode"], "apply": apply, "changed": changed, "missing": missing}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes to cron_jobs.json")
    ap.add_argument("--path", default=str(ROOT / "cron_jobs.json"))
    args = ap.parse_args()
    result = tune_jobs(Path(args.path), apply=bool(args.apply))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
