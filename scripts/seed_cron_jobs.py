#!/usr/bin/env python3
"""Seed beginner-safe local cron jobs for a fresh MAGI checkout."""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


def default_python_path(repo_root: Path = REPO_ROOT) -> Path:
    env_python = os.environ.get("MAGI_CRON_PYTHON")
    if env_python:
        return Path(env_python).expanduser()
    if os.environ.get("MAGI_VENV_DIR"):
        venv_dir = Path(os.environ["MAGI_VENV_DIR"]).expanduser()
    elif (repo_root / ".venv").exists():
        venv_dir = repo_root / ".venv"
    else:
        venv_dir = repo_root / "venv"
    if platform.system() == "Windows":
        return venv_dir / "Scripts" / "python.exe"
    if venv_dir.name == ".venv":
        return venv_dir / "bin" / "python"
    return venv_dir / "bin" / "python3"


def worldmonitor_job(repo_root: Path = REPO_ROOT, python_path: Path | None = None) -> dict[str, Any]:
    python_bin = python_path or default_python_path(repo_root)
    action_path = repo_root / "skills" / "worldmonitor-intel" / "action.py"
    return {
        "id": "job_worldmonitor_intel",
        "cron": "0 8 * * *",
        "command": f"{python_bin} {action_path} --task collect --no-reasoning --plain-output",
        "desc": "每日全球新聞網收集摘要（worldmonitor-intel）",
        "channel_id": None,
        "last_run": None,
        "last_run_minute": None,
        "enabled": True,
    }


def business_jobs(repo_root: Path = REPO_ROOT, python_path: Path | None = None) -> list[dict[str, Any]]:
    """Core single-machine business jobs that must exist on fresh installs."""
    python_bin = python_path or default_python_path(repo_root)
    run_with_env = repo_root / "scripts" / "ops" / "run_with_env.py"
    accounting_account_hint = os.environ.get("MAGI_ACCOUNTING_GOOGLE_ACCOUNT_HINT", "primary").strip() or "primary"
    return [
        {
            "id": "job_laf_pending_scan",
            "cron": "30 8 * * *",
            "command": "@MAGI 法扶未開辦掃描",
            "desc": "法扶未開辦/待報結案件提醒（08:30）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
        },
        {
            "id": "job_laf_nightly_audit",
            "cron": "50 2 * * *",
            "command": f"{python_bin} {repo_root / 'scripts' / 'laf_nightly_audit.py'}",
            "desc": "法扶夜間審計",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
        },
        {
            "id": "job_laf_condition_dedup_scan",
            "cron": "35 8 * * *",
            "command": f"{python_bin} {repo_root / 'casper_ecosystem' / 'law_firm_orchestrators' / 'laf_orchestrator.py'} --mode condition-mark-by-mediation",
            "desc": "法扶二階段去重標記（每日 08:35；調解/和解已完成者不重報）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
        },
        {
            "id": "job_laf_condition_draft",
            "cron": "40 8 * * *",
            "command": f"{python_bin} {repo_root / 'casper_ecosystem' / 'law_firm_orchestrators' / 'laf_orchestrator.py'} --mode condition-draft --max-cases 3",
            "desc": "法扶二階段批次暫存（每日 08:40；永久去重，不重報）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
        },
        {
            "id": "job_file_review_check",
            "cron": "0 10,15 * * 1-5",
            "command": f"{python_bin} {repo_root / 'skills' / 'file-review-orchestrator' / 'action.py'} --task download",
            "desc": "閱卷通知與下載檢查（平日 10:00, 15:00；下載前去重）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
        },
        {
            "id": "job_transcript_sync",
            "cron": "0 6,21 * * *",
            "command": f"{python_bin} {repo_root / 'skills' / 'transcript-downloader' / 'action.py'} --task sync",
            "desc": "筆錄同步（每日 06:00, 21:00）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
        },
        {
            "id": "job_transcript_self_test",
            "cron": "5 3 * * *",
            "command": f"{python_bin} {repo_root / 'skills' / 'transcript-downloader' / 'action.py'} --task self_test",
            "desc": "筆錄系統健康檢查（daily 03:00，驗證 import/credentials/DB/網站可達）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
        },
        {
            "id": "job_business_module_live_check",
            "cron": "10 3 * * *",
            "command": f"{python_bin} {run_with_env} MAGI_BUSINESS_LIVE_CHECK_NOTIFY=1 -- {python_bin} {repo_root / 'scripts' / 'ops' / 'business_module_live_check.py'}",
            "desc": "業務三模組 LIVE/健康檢查（法扶/閱卷/筆錄）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
        },
        {
            "id": "job_accounting_sheet_import",
            "cron": "20 9 * * 1,5",
            "command": f"{python_bin} {run_with_env} -- {python_bin} {repo_root / 'scripts' / 'import_accounting_sheet.py'} --commit --include-previous --account-hint {accounting_account_hint}",
            "desc": "同事帳務 Google Sheet 匯入（每週一、五 09:20；跳過標識俊儒，檢查本月與前月並去重）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 300,
        },
        {
            "id": "job_osc_events_refresh",
            "cron": "5 */6 * * *",
            "command": f"{python_bin} {run_with_env} MAGI_GCAL_DEDUP_ENABLED=1 MAGI_GCAL_INCREMENTAL_IMPORT=1 -- {python_bin} {repo_root / 'scripts' / 'ops' / 'osc_events_refresh.py'}",
            "desc": "OSC 建立待辦與行事曆事件更新（每 6 小時；bounded NAS scan + incremental GCal import）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 1500,
        },
    ]


def operational_jobs(repo_root: Path = REPO_ROOT, python_path: Path | None = None) -> list[dict[str, Any]]:
    """Core operational safeguards that keep a single-node MAGI self-correcting."""
    python_bin = python_path or default_python_path(repo_root)
    run_with_env = repo_root / "scripts" / "ops" / "run_with_env.py"
    omlx_switch = repo_root / "config" / "bin" / "omlx_switch_model.sh"
    return [
        {
            "id": "job_omlx_profile_guard",
            "cron": "*/15 * * * *",
            "command": f"{python_bin} {run_with_env} -- /bin/bash {omlx_switch} auto",
            "desc": "oMLX 日夜模型 profile guard（每 15 分鐘冪等檢查，漏跑切換時自動修復）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 1800,
            "no_catchup": True,
        },
        {
            "id": "job_resource_governor",
            "cron": "20 * * * *",
            "command": f"{python_bin} {repo_root / 'scripts' / 'ops' / 'resource_governor.py'} --json status",
            "desc": "MAGI 資源治理守門（磁碟/swap/記憶體分級，重型任務降級依據）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 120,
            "no_catchup": True,
        },
        {
            "id": "job_reboot_before_day_model_switch",
            "cron": "35 6 * * *",
            "command": f"{python_bin} {run_with_env} MAGI_ALLOW_SCHEDULED_REBOOT=1 -- {python_bin} {repo_root / 'scripts' / 'ops' / 'scheduled_reboot_guard.py'} --mode day --apply --json",
            "desc": "日間模型切換前重開守門（06:35；避開 06:30 健康報告與 06:40 司法院晨報，重開後由 LaunchAgent/模型 guard 載入 4B）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": False,
            "timeout_sec": 60,
            "no_catchup": True,
        },
        {
            "id": "job_reboot_before_night_model_switch",
            "cron": "35 21 * * *",
            "command": f"{python_bin} {run_with_env} MAGI_ALLOW_SCHEDULED_REBOOT=1 -- {python_bin} {repo_root / 'scripts' / 'ops' / 'scheduled_reboot_guard.py'} --mode night --apply --json",
            "desc": "夜間模型切換前重開守門（預設不啟用；私有單機可開啟，重開後由 LaunchAgent/模型 guard 載入 26B）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": False,
            "timeout_sec": 60,
            "no_catchup": True,
        },
        {
            "id": "job_disk_low_water_alarm",
            "cron": "5 * * * *",
            "command": f"{python_bin} {repo_root / 'scripts' / 'ops' / 'disk_low_water_alarm.py'}",
            "desc": "磁碟低水位守門（每小時；低於門檻自動執行保守回收並通知）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 600,
            "no_catchup": True,
        },
        {
            "id": "job_disk_cleanup_healthcheck",
            "cron": "55 3 * * *",
            "command": f"{python_bin} {run_with_env} MAGI_DISK_CLEANUP_DRY_RUN=0 MAGI_DISK_NAS_RECYCLE_ENABLE=1 -- {python_bin} {repo_root / 'scripts' / 'ops' / 'disk_cleanup_healthcheck.py'} --apply",
            "desc": "磁碟自動清理與壓縮（每日 03:55；快取上限、舊報告 gzip、備份保留、NAS 回收筒保留 14 天）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 900,
            "no_catchup": True,
        },
        {
            "id": "job_weekly_cache_cleanup",
            "cron": "10 4 * * 0",
            "command": f"{python_bin} {repo_root / 'scripts' / 'ops' / 'weekly_cache_cleanup.py'}",
            "desc": "每週可重建快取清理（週日 04:10；保護模型本體、訓練成果、DB 與單機 JSON）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 1800,
            "no_catchup": True,
        },
        {
            "id": "job_model_live_gate",
            "cron": "10 * * * *",
            "command": f"{python_bin} {repo_root / 'scripts' / 'ops' / 'model_live_gate.py'} --expect auto --json --json-out {repo_root / '.runtime' / 'model_live_gate_latest.json'}",
            "desc": "MAGI 日夜模型拓撲守門（確認 8080/8081/8082/8083 與日夜 profile 一致）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 120,
            "no_catchup": True,
        },
        {
            "id": "job_benchmark_pdf_bookmarker",
            "cron": "40 14 * * *",
            "command": f"{python_bin} {repo_root / 'scripts' / 'ops' / 'benchmark_pdf_bookmarker.py'}",
            "desc": "PDF 頁籤品質基準測試（每日 14:40，bookmark_recall ≥ 80%；維持健康頁 48h freshness）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 900,
            "no_catchup": True,
        },
        {
            "id": "job_tailscale_funnel_healthcheck",
            "cron": "*/10 * * * *",
            "command": f"{python_bin} {repo_root / 'scripts' / 'ops' / 'tailscale_funnel_healthcheck.py'} --apply --json-out {repo_root / '.runtime' / 'tailscale_funnel_health_latest.json'}",
            "desc": "Tailscale Funnel 外網入口巡檢（每 10 分鐘；用公開 DNS 實測並自動重建假啟動 Funnel）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 90,
            "no_catchup": True,
        },
        {
            "id": "job_distill_train_gemma",
            "cron": "0 11 * * 0",
            "command": f"{python_bin} {repo_root / 'scripts' / 'nightly_distill_gemma.py'}",
            "desc": "Gemma E4B 知識蒸餾（週日 11:00，validation-gated，僅產出 pending deploy）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "long_job": True,
            "timeout_sec": 5400,
        },
        {
            "id": "pdfnamer_docling_layout",
            "cron": "40 2 * * *",
            "command": f"{python_bin} {run_with_env} MAGI_PDF_NAMER_DOCLING_ENABLED=1 -- {python_bin} {repo_root / 'skills' / 'pdf-namer' / 'nightly_layout.py'}",
            "desc": "夜間 docling layout sidecar 補跑（最近 24h 命名 PDF，bounded scan）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 1800,
            "no_catchup": True,
        },
    ]


def load_jobs(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def seed_jobs(repo_root: Path = REPO_ROOT, *, python_path: Path | None = None) -> dict[str, Any]:
    cron_path = repo_root / "cron_jobs.json"
    jobs = load_jobs(cron_path)
    desired_jobs = [
        worldmonitor_job(repo_root, python_path),
        *business_jobs(repo_root, python_path),
        *operational_jobs(repo_root, python_path),
    ]
    changed = False

    for job in desired_jobs:
        for idx, existing in enumerate(jobs):
            if existing.get("id") == job["id"]:
                merged = {**existing, **job}
                if existing != merged:
                    jobs[idx] = merged
                    changed = True
                break
        else:
            jobs.append(job)
            changed = True

    # Remove the old single-job seed drift by making the three business
    # modules part of the install contract, not hand-edited local state.
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for job in jobs:
        job_id = str(job.get("id") or "")
        if job_id and job_id in seen:
            changed = True
            continue
        if job_id:
            seen.add(job_id)
        deduped.append(job)
    if len(deduped) != len(jobs):
        jobs = deduped
        changed = True

    if changed:
        cron_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {"ok": True, "path": str(cron_path), "changed": changed, "jobs": len(jobs)}


def main() -> int:
    result = seed_jobs()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
