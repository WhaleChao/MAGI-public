#!/usr/bin/env python3
"""Tune local Judicial Yuan API cron jobs for MAGI's TLR-smart load mode."""
from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) in sys.path:
    sys.path.remove(str(ROOT))
sys.path.insert(0, str(ROOT))
for _module_name, _module in list(sys.modules.items()):
    if _module_name == "api" or _module_name.startswith("api."):
        _module_file = str(getattr(_module, "__file__", "") or "")
        if _module_file and not _module_file.startswith(str(ROOT)):
            sys.modules.pop(_module_name, None)

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=False)
except Exception:
    pass

from api.domains.judicial_api_cache import NAS_FALLBACK_ENV, preferred_nas_judgment_cache_root  # noqa: E402
from api.domains.judicial_api_policy import DEFAULT_LOAD_MODE, judicial_api_policy_report  # noqa: E402

DAY_PROCESS_RUNS_PER_DAY = 5


def _python_bin(root: Path) -> str:
    candidate = root / "venv" / "bin" / "python3"
    return str(candidate if candidate.exists() else Path(sys.executable))


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_assignment(key: str, value: Any) -> str:
    return shlex.quote(f"{key}={value}")


def _job_command(
    root: Path,
    job_id: str,
    task_name: str,
    payload: dict[str, Any],
    *,
    window_env: bool = False,
    day_process: bool = False,
) -> str:
    py = _python_bin(root)
    resource_guard = root / "scripts" / "ops" / "resource_guarded_run.py"
    run_with_env = root / "scripts" / "ops" / "run_with_env.py"
    collector = root / "skills" / "judgment-collector" / "action.py"
    env_map: dict[str, Any] = {
        "MAGI_JUDICIAL_API_LOAD_MODE": judicial_api_policy_report().get("mode") or DEFAULT_LOAD_MODE,
        "MAGI_PREFER_LOCAL_DB": "1",
        "MAGI_NO_DELETE": "1",
    }
    nas_fallback = preferred_nas_judgment_cache_root(create=False)
    if nas_fallback is not None:
        env_map[NAS_FALLBACK_ENV] = str(nas_fallback)
    if day_process:
        env_map["JUDICIAL_API_DAY_RUNS_PER_DAY"] = str(DAY_PROCESS_RUNS_PER_DAY)
    if window_env:
        env_map["JUDICIAL_API_WINDOW_START_HOUR"] = "0"
        env_map["JUDICIAL_API_WINDOW_END_HOUR"] = "6"
    env_parts = [_env_assignment(key, value) for key, value in env_map.items()]
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
    path = path.resolve()
    policy = judicial_api_policy_report()
    jobs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not isinstance(jobs, list):
        raise SystemExit(f"{path} is not a cron job list")
    enable_night_pull = _truthy(policy["enable_night_pull"])
    enable_day_process = _truthy(policy["enable_day_process"])

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
            "desc": "司法院API NAS 增量夜拉（TLR 智慧模式；補近 5 日並揭露來源積欠）",
            "enabled": enable_night_pull,
            "timeout_sec": int(policy["night_timeout_sec"]),
            "no_catchup": True,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        "job_judicial_api_morning": {
            "cron": "40 6 * * *",
            "command": _job_command(path.parent, "job_judicial_api_morning", "official_api_day_process", {**day_payload, "notify": True}, day_process=True),
            "desc": "司法院API晨間 NAS 加速整理（抽取摘要、不下載附件）",
            "enabled": enable_day_process,
            "timeout_sec": int(policy["day_timeout_sec"]),
            "no_catchup": True,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        "job_judicial_api_noon": {
            "cron": "30 11 * * *",
            "command": _job_command(path.parent, "job_judicial_api_noon", "official_api_day_process", day_payload, day_process=True),
            "desc": "司法院API午批 NAS 加速整理（抽取摘要、不下載附件）",
            "enabled": enable_day_process,
            "timeout_sec": int(policy["day_timeout_sec"]),
            "no_catchup": True,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        "job_judicial_api_afternoon": {
            "cron": "30 15 * * *",
            "command": _job_command(path.parent, "job_judicial_api_afternoon", "official_api_day_process", day_payload, day_process=True),
            "desc": "司法院API午後批 NAS 加速整理（抽取摘要、不下載附件）",
            "enabled": enable_day_process,
            "timeout_sec": int(policy["day_timeout_sec"]),
            "no_catchup": True,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        "job_judicial_api_evening": {
            "cron": "30 19 * * *",
            "command": _job_command(path.parent, "job_judicial_api_evening", "official_api_day_process", day_payload, day_process=True),
            "desc": "司法院API晚批 NAS 加速整理（抽取摘要、不下載附件）",
            "enabled": enable_day_process,
            "timeout_sec": int(policy["day_timeout_sec"]),
            "no_catchup": True,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        "job_judicial_api_backlog_clear": {
            "cron": "30 21 * * *",
            "command": _job_command(path.parent, "job_judicial_api_backlog_clear", "official_api_day_process", {**day_payload, "notify": True}, day_process=True),
            "desc": "司法院API backlog NAS 追趕清理（抽取摘要、不下載附件）",
            "enabled": enable_day_process,
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
