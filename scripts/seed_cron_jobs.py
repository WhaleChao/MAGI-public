#!/usr/bin/env python3
"""Seed beginner-safe local cron jobs for a fresh MAGI checkout."""

from __future__ import annotations

import json
import os
import platform
import re
import shlex
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from skills.ops.cron_runtime_policy import cron_job_timeout

DEPRECATED_JOB_IDS = {
    # Replaced by pdf-bookmarker/pdf-namer. Keeping it enabled caused an extra
    # 02:10 NAS scan and duplicated PDF tagging logic.
    "job_1772867062892_e33b6a",
    # Replaced by canonical job_wiki_synthesizer seeded below.
    "job_1776221713533_0a5366",
}

KNOWN_MAGI_ROOTS = tuple(
    dict.fromkeys(
        root
        for root in (
            str(REPO_ROOT),
            os.environ.get("MAGI_SOURCE_ROOT", "").strip(),
            str(Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v3"),
        )
        if root
    )
)


def qcmd(*parts: object) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts)


def quote_repo_root_paths(command: str, repo_root: Path) -> str:
    root_text = str(repo_root)
    if " " not in root_text or root_text not in command:
        return command
    # Only rewrite unquoted shell segments. A correctly quoted assignment such
    # as 'MAGI_OBSIDIAN_AGENT_DIR=/.../Application Support/...' must remain a
    # single token after canonicalization.
    quoted_segment = re.compile(r"'(?:[^']*)'|\"(?:[^\"]*)\"")
    path_pattern = re.compile(rf"{re.escape(root_text)}(?:/[^'\"\s]+)*")
    parts = quoted_segment.split(command)
    quoted = quoted_segment.findall(command)
    rebuilt: list[str] = []
    for index, part in enumerate(parts):
        rebuilt.append(path_pattern.sub(lambda match: shlex.quote(match.group(0)), part))
        if index < len(quoted):
            rebuilt.append(quoted[index])
    return "".join(rebuilt)


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


def guarded_cron_command(
    repo_root: Path,
    python_bin: Path,
    job_id: str,
    command: str,
    *,
    block_at: str = "core_only",
    require_disk_free_gb: float | None = None,
    require_free_inactive_gb: float | None = None,
    timeout_sec: int | None = None,
) -> str:
    """Wrap rebuildable/heavy cron commands with the resource guard.

    The guard never touches DB or user files; it only skips the wrapped command
    when the machine is already low on disk/memory.
    """
    guard = repo_root / "scripts" / "ops" / "resource_guarded_run.py"
    parts = [
        shlex.quote(str(python_bin)),
        shlex.quote(str(guard)),
        "--job-id",
        shlex.quote(job_id),
        "--block-at",
        shlex.quote(block_at),
    ]
    if require_disk_free_gb is not None:
        parts.extend(["--require-disk-free-gb", f"{require_disk_free_gb:g}"])
    if require_free_inactive_gb is not None:
        parts.extend(["--require-free-inactive-gb", f"{require_free_inactive_gb:g}"])
    if timeout_sec is not None:
        parts.extend(["--timeout-sec", str(max(1, int(timeout_sec)))])
    parts.extend(["--", command])
    return " ".join(parts)


def token_refresh_cron_command(
    repo_root: Path,
    python_bin: Path,
    command: str,
    *,
    required_checks: tuple[str, ...] = (),
) -> str:
    """Refresh Google OAuth tokens immediately before a Google-dependent cron job."""
    wrapper = repo_root / "scripts" / "ops" / "run_after_token_refresh.py"
    gate_args: list[str] = []
    for name in required_checks:
        gate_args.extend(["--require", str(name)])
    return qcmd(python_bin, wrapper, *gate_args, "--") + " " + command


def worldmonitor_job(repo_root: Path = REPO_ROOT, python_path: Path | None = None) -> dict[str, Any]:
    python_bin = python_path or default_python_path(repo_root)
    action_path = repo_root / "skills" / "worldmonitor-intel" / "action.py"
    command = qcmd(python_bin, action_path, "--task", "collect", "--no-reasoning", "--plain-output")
    return {
        "id": "job_worldmonitor_intel",
        "cron": "0 8 * * *",
        "command": guarded_cron_command(repo_root, python_bin, "job_worldmonitor_intel", command),
        "desc": "每日全球新聞網收集摘要（worldmonitor-intel）",
        "channel_id": None,
        "last_run": None,
        "last_run_minute": None,
        "enabled": True,
    }


def deterministic_legacy_replacements(repo_root: Path = REPO_ROOT, python_path: Path | None = None) -> list[dict[str, Any]]:
    """Replace ambiguous natural-language cron macros with verifiable CLIs."""
    python_bin = python_path or default_python_path(repo_root)
    run_with_env = repo_root / "scripts" / "ops" / "run_with_env.py"
    no_delete_prefix = (python_bin, run_with_env, "MAGI_NO_DELETE=1", "--")
    return [
        {
            "id": "job_optimize_report",
            "cron": "0 16 * * *",
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "system_diagnostic_report.py",
                "--json-out",
                repo_root / ".runtime" / "system_diagnostic_report_latest.json",
            ),
            "desc": "16:00 輕量系統診斷報告（deterministic localhost/read-only CLI）",
            "channel_id": None,
            "enabled": True,
            "timeout_sec": 60,
            "no_catchup": True,
        },
        {
            "id": "job_1770705679",
            "cron": "0 4,10,16,22 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                "MAGI_NO_DELETE=1",
                "JUDGMENT_DAILY_TIME_BUDGET_SEC=1200",
                "JUDGMENT_DAILY_COLLECT_TIMEOUT_SEC=180",
                "JUDGMENT_DAILY_MAX_REASONS=5",
                "JUDGMENT_MCP_GAP_FILL_ENABLE=1",
                "JUDGMENT_MCP_GAP_MAX_RESULTS_PER=5",
                "JUDGMENT_MCP_GAP_TIME_BUDGET_SEC=480",
                # Summary retry has its own bounded 22:30 job.  Keeping it out
                # of this 04:00 crawl preserves >5 minutes for cleanup before
                # the scheduler's 1800-second outer deadline.
                "JUDGMENT_SUMMARY_RETRY_ENABLE=0",
                "--",
                python_bin,
                repo_root / "skills" / "judgment-collector" / "action.py",
                "--task",
                "daily_crawl",
            ),
            "desc": "法律資料爬取（04:00 主跑；10:00、16:00、22:00 有界補跑）",
            "channel_id": None,
            "enabled": True,
            "timeout_sec": 1800,
            "no_catchup": True,
        },
        {
            "id": "job_1770948489644_0726cf",
            "cron": "30 4 * * *",
            "command": qcmd(*no_delete_prefix, python_bin, repo_root / "skills" / "memory" / "cortex_sync.py"),
            "desc": "Daily Cortex Vector Sync（deterministic CLI）",
            "channel_id": None,
            "enabled": True,
            "timeout_sec": 900,
            "no_catchup": True,
        },
        {
            "id": "job_1770948489644_c5a469",
            "cron": "0 */2 * * *",
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "resource_guarded_run.py",
                "--job-id",
                "job_1770948489644_c5a469",
                "--block-at",
                "throttle",
                "--timeout-sec",
                "900",
                "--",
                python_bin,
                repo_root / "skills" / "magi-autopilot" / "action.py",
                "--task",
                "tick",
            ),
            "desc": "Bi-hourly CODE Auto Cycle（deterministic CLI）",
            "channel_id": None,
            "enabled": True,
            # 工作本體仍由 resource_guarded_run 在 900 秒內中止；排程層
            # 保留 60 分鐘尖峰排隊窗，避免把資源保護等待誤報為失敗。
            "timeout_sec": 3600,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        {
            "id": "job_1770949442096_9e8adf",
            "cron": "15 1 * * *",
            "command": qcmd(python_bin, repo_root / "scripts" / "ops" / "run_auto_skill_import.py"),
            "desc": "Daily Auto-Skill Import + DC Summary（deterministic CLI）",
            "channel_id": None,
            "enabled": True,
            "timeout_sec": 900,
            "no_catchup": True,
        },
        {
            "id": "job_judgment_retry_evening",
            "cron": "30 22 * * *",
            "command": qcmd(
                *no_delete_prefix,
                python_bin,
                repo_root / "skills" / "judgment-collector" / "action.py",
                "--task",
                'retry_summary_queue_auto {"notify":false}',
            ),
            "desc": "Evening Judgment Summary Retry（deterministic CLI）",
            "channel_id": None,
            "enabled": True,
            "timeout_sec": 900,
            "no_catchup": True,
        },
        {
            "id": "job_osc_index_cases",
            "cron": "7,22,37,52 * * * *",
            "command": qcmd(
                *no_delete_prefix,
                python_bin,
                repo_root / "skills" / "osc-orchestrator" / "action.py",
                "--task",
                'index_cases {"max_cases":220,"max_files_per_case":120}',
            ),
            "desc": "案件證據事件增量對帳（每15分鐘；硬體壓力守門；每日全量保底）",
            "channel_id": None,
            "enabled": True,
            "timeout_sec": 1200,
            "no_catchup": True,
        },
        {
            "id": "job_weekly_legal_crawl",
            "cron": "0 3 * * 0",
            "command": qcmd(
                *no_delete_prefix,
                python_bin,
                repo_root / "skills" / "judgment-collector" / "action.py",
                "--task",
                "daily_crawl",
            ),
            "desc": "每週法律資料爬蟲（deterministic CLI）",
            "channel_id": None,
            "enabled": True,
            "timeout_sec": 7200,
            "no_catchup": True,
        },
    ]


def business_jobs(repo_root: Path = REPO_ROOT, python_path: Path | None = None) -> list[dict[str, Any]]:
    """Core single-machine business jobs that must exist on fresh installs."""
    python_bin = python_path or default_python_path(repo_root)
    run_with_env = repo_root / "scripts" / "ops" / "run_with_env.py"
    obsidian_agent_dir = repo_root / ".agent"
    return [
        {
            "id": "job_laf_pending_scan",
            "cron": "30 8 * * *",
            "command": qcmd(python_bin, repo_root / "skills" / "osc-orchestrator" / "action.py", "--task", "laf_pending_scan"),
            "desc": "法扶未開辦/待報結案件提醒（08:30）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
        },
        {
            "id": "job_laf_gmail_dispatch_scan",
            "cron": "2,17,32,47 * * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                "--",
                python_bin,
                repo_root / "scripts" / "ops" / "laf_gmail_dispatch_scan.py",
                "--max-results",
                "80",
                "--apply",
                "--json-out",
                repo_root / "static" / "laf_gmail_monitor_state.json",
            ),
            "desc": "法扶 Gmail 派案/回報隔離正式掃描（每 15 分鐘；成功才標記已處理）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 420,
        },
        {
            "id": "job_laf_portal_retry_once",
            "cron": "25 * * * *",
            "command": qcmd(
                python_bin,
                repo_root / "casper_ecosystem" / "law_firm_orchestrators" / "laf_orchestrator.py",
                "--mode",
                "portal-retry-once",
                "--max-items",
                "2",
                "--timeout-sec",
                "600",
            ),
            "desc": "法扶附件獨立補抓（每小時；與 Gmail 常駐監控解耦、限時且有心跳）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 750,
            "long_job": False,
            "resource_guarded": False,
            "resource_block_at": "none",
        },
        {
            "id": "job_laf_nightly_audit",
            "cron": "55 2 * * *",
            "command": qcmd(python_bin, repo_root / "scripts" / "laf_nightly_audit.py"),
            "desc": "法扶夜間審計（02:55；避開 02:15 PDF 書籤增量高峰）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            # The authenticated portal audit has exceeded the historical
            # 960-second value when the site is slow.  Keep this explicit so
            # seed merging overwrites stale persisted values instead of
            # silently inheriting them from cron_jobs.json.
            "timeout_sec": 1800,
        },
        {
            "id": "job_laf_portal_new_files_scan",
            "cron": "15 */6 * * *",
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "laf_portal_new_files_scan.py",
                "--apply",
                "--json-out",
                repo_root / "static" / "laf_portal_new_files_latest.json",
            ),
            "desc": "法扶官網附件補抓（每 6 小時；成功下載歸檔才更新狀態）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 3900,
        },
        {
            "id": "job_business_readiness_snapshot",
            "cron": "4,14,24,34,44,54 * * * *",
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "business_readiness_snapshot.py",
                "--json-out",
                repo_root / "static" / "business_readiness_latest.json",
            ),
            "desc": "業務待辦與 AI 引擎就緒快照（每 10 分鐘）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 120,
        },
        {
            "id": "job_laf_condition_dedup_scan",
            "cron": "35 8 * * *",
            "command": qcmd(
                python_bin,
                repo_root / "casper_ecosystem" / "law_firm_orchestrators" / "laf_orchestrator.py",
                "--mode",
                "condition-mark-by-mediation",
            ),
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
            "command": qcmd(
                python_bin,
                repo_root / "casper_ecosystem" / "law_firm_orchestrators" / "laf_orchestrator.py",
                "--mode",
                "condition-draft",
                "--max-cases",
                "3",
            ),
            "desc": "法扶二階段批次暫存（每日 08:40；永久去重，不重報）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            # Three official-site drafts can exceed the generic ten-minute
            # scheduler limit even while each browser step is still making
            # progress.  The old limit killed a healthy Chromium at ~607s and
            # then surfaced a SafeProcess cleanup error.  Keep the operation
            # bounded, but give the declared three-case batch enough room.
            "timeout_sec": 1200,
        },
        {
            "id": "job_file_review_check",
            "cron": "0 9-18 * * 1-5",
            "command": qcmd(python_bin, repo_root / "skills" / "file-review-orchestrator" / "action.py", "--task", "scheduled_check"),
            "desc": "閱卷完整檢查（平日 09:00-18:00 每小時；信箱/入口/繳費單/卷宗下載）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 900,
        },
        {
            "id": "job_file_review_downloadable_probe_dense",
            "cron": "20,40 8-20 * * 1-5",
            "command": qcmd(
                python_bin,
                run_with_env,
                "MAGI_FILE_REVIEW_PROBE_WITH_GMAIL=0",
                "--",
                python_bin,
                repo_root / "skills" / "file-review-orchestrator" / "action.py",
                "--task",
                'downloadable_probe {"days":30,"notify":false}',
            ),
            "desc": "閱卷可下載狀態探測（平日 08:00-20:59 每 20 分鐘；只查入口不下載）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 420,
        },
        {
            "id": "job_file_review_staging_cleanup",
            "cron": "47 3 * * *",
            "command": qcmd(
                python_bin,
                repo_root / "skills" / "file-review-orchestrator" / "action.py",
                "--task",
                'cleanup_downloads {"max_days":7,"pending_max_days":14,"quarantine_max_days":14}',
            ),
            "desc": "閱卷下載暫存清理（每日 03:45；日期暫存保留 7 天、待歸檔/隔離保留 14 天，避免 Mac 容量累積）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 600,
        },
        {
            "id": "job_transcript_sync",
            "cron": "8 */2 * * *",
            "command": qcmd(python_bin, repo_root / "skills" / "transcript-downloader" / "action.py", "--task", "sync"),
            "desc": "筆錄同步（每 2 小時；入口忙碌會持久化重試，下載後以文件內法院案號驗證後歸檔）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 7200,
            "auto_retry_max_attempts": 4,
            "auto_retry_delays_seconds": [120, 600, 1800, 3600],
        },
        {
            "id": "job_transcript_indexer",
            "cron": "30 6,21 * * *",
            "command": qcmd(python_bin, repo_root / "skills" / "transcript-indexer" / "action.py", "--task", "index"),
            "desc": "筆錄索引更新（筆錄同步後 30 分鐘；供搜尋與待辦抽取使用）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 900,
        },
        {
            "id": "job_transcript_self_test",
            "cron": "5 3 * * *",
            "command": qcmd(python_bin, repo_root / "skills" / "transcript-downloader" / "action.py", "--task", "self_test"),
            "desc": "筆錄系統健康檢查（daily 03:00，驗證 import/credentials/DB/網站可達）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
        },
        {
            "id": "job_business_module_live_check",
            "cron": "7 0,6,12,18 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                "MAGI_BUSINESS_LIVE_CHECK_NOTIFY=1",
                "--",
                python_bin,
                repo_root / "scripts" / "ops" / "business_module_live_check.py",
                "--json-out",
                repo_root / ".runtime" / "business_module_live_check_latest.json",
            ),
            "desc": "業務三模組 LIVE/健康檢查（法扶/閱卷/筆錄；每 6 小時產生當前版本證據）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            # File-review's strict portal probe may use its full 900-second
            # allowance; keep the scheduler envelope above that inner limit.
            "timeout_sec": 960,
        },
        {
            "id": "job_commercial_readiness_live",
            "cron": "37 */6 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                "--",
                python_bin,
                repo_root / "scripts" / "ops" / "commercial_readiness_live.py",
                "--skip-backup",
                "--json-out",
                repo_root / ".runtime" / "commercial_readiness_live_latest.json",
            ),
            "desc": "商用就緒 LIVE 閘門（每 6 小時；綁定目前 release，唯讀驗證）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 300,
        },
        {
            "id": "job_accounting_sheet_import",
            "cron": "20 9 * * 1,5",
            "command": token_refresh_cron_command(
                repo_root,
                python_bin,
                qcmd(
                    python_bin,
                    repo_root / "scripts" / "import_accounting_sheet.py",
                    "--commit",
                    "--include-previous",
                ),
                required_checks=("google_accounting_sheets",),
            ),
            "desc": "同事帳務 Google Sheet 匯入（每週一、五 09:20；跳過標識俊儒，檢查本月與前月並去重）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 300,
        },
        {
            "id": "job_accounting_monthly_bonus",
            "cron": "0 12 * * *",
            "command": token_refresh_cron_command(
                repo_root,
                python_bin,
                qcmd(
                    python_bin,
                    repo_root / "scripts" / "accounting_monthly_bonus.py",
                    "--commit",
                    "--refresh-import",
                    "--catch-up",
                    "--export-xlsx",
                ),
                required_checks=("google_accounting_sheets",),
            ),
            "desc": "每月帳務獎金結算（24 日中午起自動重新匯入帳務、計算法扶消債酬金獎金與案件獎金，月初可重算前月）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 300,
        },
        {
            "id": "job_case_index_sync",
            "cron": "0 0 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                f"MAGI_OBSIDIAN_AGENT_DIR={obsidian_agent_dir}",
                "--",
                python_bin,
                repo_root / "skills" / "obsidian" / "action.py",
                "--task",
                "sync_case_notes",
            ),
            "desc": "Obsidian 案件索引卡同步（每日 00:00，從 DB 生成 30_Index/ 結構化卡片，無 LLM）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 600,
        },
        {
            "id": "job_obsidian_ingest",
            "cron": "35 2 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                f"MAGI_OBSIDIAN_AGENT_DIR={obsidian_agent_dir}",
                "MAGI_USE_MARKITDOWN=0",
                "MAGI_OBSIDIAN_OCR_MAX_PAGES=8",
                "--",
                python_bin,
                repo_root / "skills" / "obsidian" / "action.py",
                "--task",
                "ingest_source",
                "--source",
                "案件",
                "--include-folders",
                "high-value",
                "--limit",
                "4",
            ),
            "desc": "Obsidian 來源文件匯入（每日 02:35；高價值資料夾，4 份可續跑批次，原生 PDF/OCR 優先）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 2700,
        },
        {
            "id": "job_obsidian_repair_notes",
            "cron": "27 3 * * *",
            "command": guarded_cron_command(
                repo_root,
                python_bin,
                "job_obsidian_repair_notes",
                qcmd(
                    python_bin,
                    run_with_env,
                    f"MAGI_OBSIDIAN_AGENT_DIR={obsidian_agent_dir}",
                    "MAGI_USE_MARKITDOWN=0",
                    "MAGI_OBSIDIAN_PDF_EXTRACTOR_TIMEOUT_SEC=45",
                    "--",
                    python_bin,
                    repo_root / "skills" / "obsidian" / "action.py",
                    "--task",
                    "repair_notes",
                    "--limit",
                    "20",
                    "--reextract",
                ),
                block_at="throttle",
                require_disk_free_gb=30,
                require_free_inactive_gb=3,
                timeout_sec=2400,
            ),
            "desc": "Obsidian 摘要修復（每日 03:20；小批次重建 v2 摘要，弱抽取可重抽來源；PDF parser timeout 防卡死）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 2700,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        {
            "id": "job_obsidian_duplicate_cleanup",
            "cron": "10 4 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                f"MAGI_OBSIDIAN_AGENT_DIR={obsidian_agent_dir}",
                "--",
                python_bin,
                repo_root / "skills" / "obsidian" / "action.py",
                "--task",
                "cleanup_duplicate_notes",
            ),
            "desc": "Obsidian summary 重複筆記隔離（每日 04:10；同案同 hash 僅保留 canonical note）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 600,
        },
        {
            "id": "job_wiki_synthesizer",
            "cron": "40 4 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                f"MAGI_OBSIDIAN_AGENT_DIR={obsidian_agent_dir}",
                "MAGI_WIKI_FALLBACK_RETRY_HOURS=72",
                "--",
                python_bin,
                repo_root / "scripts" / "wiki_synthesizer.py",
                "--quiet",
                "--limit",
                "12",
            ),
            "desc": "Obsidian Wiki 合成（每日 04:40；每輪最多 12 案，模型備援 72 小時後輪替重試）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 3600,
        },
        {
            "id": "job_obsidian_vector_reindex_notes",
            "cron": "50 4 * * *",
            "command": guarded_cron_command(
                repo_root,
                python_bin,
                "job_obsidian_vector_reindex_notes",
                qcmd(
                    python_bin,
                    run_with_env,
                    f"MAGI_OBSIDIAN_AGENT_DIR={obsidian_agent_dir}",
                    "MAGI_OBSIDIAN_INGEST_ZERO_CHUNKS_FIRST=1",
                    "MAGI_OBSIDIAN_INGEST_NOTE_LIMIT=120",
                    "MAGI_OBSIDIAN_CHECKPOINT_EVERY=5",
                    "--",
                    python_bin,
                    repo_root / "skills" / "obsidian" / "action.py",
                    "--task",
                    "ingest",
                    "--folder",
                    "20_Notes",
                ),
                block_at="throttle",
                require_disk_free_gb=30,
                require_free_inactive_gb=3,
                timeout_sec=1800,
            ),
            "desc": "Obsidian 向量索引補齊（每日 04:50；20_Notes 零 chunks 優先、小批次 checkpoint）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 3600,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        {
            "id": "job_obsidian_vector_reindex_wiki",
            "cron": "20 5 * * *",
            "command": guarded_cron_command(
                repo_root,
                python_bin,
                "job_obsidian_vector_reindex_wiki",
                qcmd(
                    python_bin,
                    run_with_env,
                    f"MAGI_OBSIDIAN_AGENT_DIR={obsidian_agent_dir}",
                    "MAGI_OBSIDIAN_INGEST_ZERO_CHUNKS_FIRST=1",
                    "MAGI_OBSIDIAN_INGEST_NOTE_LIMIT=120",
                    "MAGI_OBSIDIAN_CHECKPOINT_EVERY=5",
                    "--",
                    python_bin,
                    repo_root / "skills" / "obsidian" / "action.py",
                    "--task",
                    "ingest",
                    "--folder",
                    "30_Wiki",
                ),
                block_at="throttle",
                require_disk_free_gb=30,
                require_free_inactive_gb=3,
                timeout_sec=1800,
            ),
            "desc": "Obsidian Wiki 向量索引補齊（每日 05:20；30_Wiki 零 chunks 優先、小批次 checkpoint）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 3600,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        {
            "id": "job_knowledge_lint",
            "cron": "47 5 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                f"MAGI_OBSIDIAN_AGENT_DIR={obsidian_agent_dir}",
                "--",
                python_bin,
                repo_root / "scripts" / "knowledge_lint.py",
                "--quick",
                "--write-to-vault",
                "--quiet",
            ),
            "desc": "知識品質掃描（每日 05:00；含 Obsidian 摘要品質/Wiki 完整度/索引漂移）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 1200,
        },
        {
            "id": "job_obsidian_acceptance_gate",
            "cron": "0 6 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                f"MAGI_OBSIDIAN_AGENT_DIR={obsidian_agent_dir}",
                "--",
                python_bin,
                repo_root / "scripts" / "ops" / "obsidian_acceptance_gate.py",
                "--json-out",
                repo_root / ".runtime" / "obsidian_acceptance_latest.json",
            ),
            "desc": "Obsidian 知識庫出廠檢查線（每日 05:20；摘要/重複/wiki/索引）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 900,
        },
        {
            "id": "job_drive_case_sync_bidirectional",
            "cron": "1 */6 * * *",
            "command": guarded_cron_command(
                repo_root,
                python_bin,
                "job_drive_case_sync_bidirectional",
                token_refresh_cron_command(
                    repo_root,
                    python_bin,
                    qcmd(
                        python_bin,
                        run_with_env,
                        "MAGI_DRIVE_SYNC_HTTP_TIMEOUT=60",
                        "MAGI_DRIVE_SYNC_DRIVE_LIST_TIMEOUT_SEC=60",
                        "MAGI_DRIVE_SYNC_API_RETRIES=2",
                        "--",
                        python_bin,
                        repo_root / "scripts" / "drive_case_sync_worker.py",
                        "--matched-case-limit",
                        "8",
                        "--download-limit",
                        "24",
                        "--upload-limit",
                        "24",
                        "--max-download-bytes",
                        "1500000000",
                        "--max-upload-bytes",
                        "1500000000",
                        "--max-case-depth",
                        "20",
                        "--max-case-items",
                        "1000",
                        "--create-drive-folder-limit",
                        "10",
                        "--create-drive-folder-max-age-hours",
                        "168",
                        "--priority-upcoming-days",
                        "21",
                        "--priority-case-limit",
                        "24",
                        "--no-downloads",
                        "--no-uploads",
                        "--no-create-drive-folders",
                        "--inventory-timeout-sec",
                        "1200",
                    ),
                    required_checks=("google_drive_sync_readonly",),
                ),
                block_at="core_only",
                require_disk_free_gb=30,
                require_free_inactive_gb=3,
                timeout_sec=900,
            ),
            "desc": "Google Drive/NAS 案件辦理 priority preflight（每 6 小時；讀取近期待辦案件映射，實際補檔由 all-files bounded sync 執行）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "long_job": True,
            "timeout_sec": 1200,
            "resource_guarded": True,
            "resource_block_at": "core_only",
        },
        {
            "id": "job_drive_case_sync_all_files",
            "cron": "12,32,52 * * * *",
            "command": guarded_cron_command(
                repo_root,
                python_bin,
                "job_drive_case_sync_all_files",
                token_refresh_cron_command(
                    repo_root,
                    python_bin,
                    qcmd(
                        python_bin,
                        run_with_env,
                        "MAGI_DRIVE_SYNC_LOCAL_SCAN_TIMEOUT_SEC=300",
                        "MAGI_DRIVE_SYNC_LOCAL_HASH_TIMEOUT_SEC=900",
                        "MAGI_DRIVE_SYNC_HTTP_TIMEOUT=90",
                        "MAGI_DRIVE_SYNC_DRIVE_LIST_TIMEOUT_SEC=90",
                        "MAGI_DRIVE_SYNC_API_RETRIES=3",
                        # Hashing is streamed in 1 MiB chunks, so the former
                        # 5 MB ceiling did not protect memory; it only left
                        # ordinary PDFs/media permanently "unverified" and
                        # retried the same DB slice forever.  Match the
                        # worker's bounded transfer envelope instead.  Keep
                        # hashing streamed while allowing one ordinary large
                        # archive/PDF to complete through the resumable 8 MiB
                        # uploader rather than retrying the same cursor.
                        "MAGI_DRIVE_SYNC_LOCAL_HASH_MAX_BYTES=3000000000",
                        "MAGI_DRIVE_SYNC_MAX_SINGLE_DOWNLOAD_BYTES=1500000000",
                        "MAGI_DRIVE_SYNC_MAX_SINGLE_UPLOAD_BYTES=3000000000",
                        # SMB reads must stay bounded too.  Eight MiB
                        # resumable chunks prevent one 100+ MB PDF from
                        # wedging the whole worker in an uninterruptible read.
                        "MAGI_DRIVE_SYNC_RESUMABLE_UPLOAD_MIN_BYTES=8388608",
                        "MAGI_DRIVE_SYNC_UPLOAD_CHUNK_BYTES=8388608",
                        "MAGI_DRIVE_SYNC_DEFERRED_DOWNLOADS_ARE_OK=1",
                        "MAGI_DRIVE_SYNC_DEFERRED_UPLOADS_ARE_OK=1",
                        "MAGI_DRIVE_SYNC_UNVERIFIED_EXISTING_ARE_OK=0",
                        "--",
                        python_bin,
                        repo_root / "scripts" / "drive_case_sync_worker.py",
                        "--direct-all-cases",
                        "--direct-all-case-limit",
                        "1",
                        "--all-case-chunk-size",
                        "1",
                        "--execute-downloads",
                        "--execute-uploads",
                        "--no-create-drive-folders",
                        "--download-limit",
                        "24",
                        "--upload-limit",
                        "24",
                        "--max-download-bytes",
                        "1500000000",
                        "--max-upload-bytes",
                        "3000000000",
                        "--max-case-depth",
                        "24",
                        "--max-case-items",
                        "20000",
                        "--create-drive-folder-limit",
                        "24",
                        "--create-drive-folder-max-age-hours",
                        "168",
                        "--priority-upcoming-days",
                        "0",
                        "--inventory-timeout-sec",
                        "5400",
                        "--terminal-headroom-sec",
                        "300",
                    ),
                    required_checks=(
                        "google_drive_sync_readonly",
                        "google_drive_sync_write",
                    ),
                ),
                block_at="core_only",
                require_disk_free_gb=30,
                require_free_inactive_gb=3,
                # Scan one canonical case chunk completely and checkpoint its
                # DB cursor before moving on.  A multi-case transaction could
                # lose the entire slice when one slow SMB directory exhausted
                # the outer deadline; one-case commits retain full depth and
                # file coverage without turning a timeout into lost progress.
                # Leave ten minutes for the guarded wrapper to persist the
                # worker's checkpoint/deferred receipt, then another five
                # minutes for the cron owner to persist its terminal state.
                timeout_sec=6000,
            ),
            "desc": "Google Drive/NAS 全案件輪巡雙向同步（每 20 分鐘單案 chunk 完整掃描並保留公平 cursor；僅整個 cycle 完成才標記完成；依 DB folder_path 輪巡，不改兩邊命名規則；缺檔才補，不覆蓋、不刪除）；單輪最長 90 分鐘並保留分層收尾時間",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "long_job": True,
            "timeout_sec": 6300,
            "resource_guarded": True,
            "resource_block_at": "core_only",
        },
        {
            "id": "job_osc_events_refresh",
            "cron": "35 */2 * * *",
            "command": token_refresh_cron_command(
                repo_root,
                python_bin,
                qcmd(
                    "MAGI_GCAL_DEDUP_ENABLED=1",
                    "MAGI_GCAL_DEDUP_DRY_RUN=0",
                    "MAGI_GCAL_INCREMENTAL_IMPORT=1",
                    "MAGI_GCAL_REPAIR_EXISTING=1",
                    "OSC_EVENTS_REFRESH_CALENDAR_LIMIT=500",
                    "OSC_EVENTS_REFRESH_GCAL_PUSH_LIMIT=200",
                    "OSC_PDF_CALENDAR_FULL_FILENAME_SWEEP=1",
                    "OSC_PDF_CALENDAR_FILENAME_SWEEP_LIMIT=50000",
                    "OSC_PDF_CALENDAR_FINAL_DOC_PRIORITY_CASE_LIMIT=2000",
                    "OSC_PDF_CALENDAR_TARGET_TIMEOUT_SEC=180",
                    "OSC_PDF_CALENDAR_BULK_TEXT_ENABLE=1",
                    "OSC_PDF_CALENDAR_FULL_TEXT_SCAN=1",
                    # Calendar omissions are a high-impact failure: every
                    # ordinary refresh must cover the complete case corpus,
                    # not merely rotate through a bounded slice.  The worker
                    # remains time-bounded and serialised by the shared case
                    # file lock, but its *coverage* is no longer capped.
                    "OSC_PDF_CALENDAR_FULL_TEXT_ALL_CASES=1",
                    "OSC_PDF_CALENDAR_FULL_TEXT_SCAN_LIMIT=10000",
                    "OSC_EVENTS_REFRESH_PDF_LIMIT=10000",
                    "OSC_EVENTS_REFRESH_PDF_CASE_BATCH=500",
                    "OSC_EVENTS_REFRESH_SCAN_BUDGET_SEC=3600",
                    "OSC_EVENTS_REFRESH_TRANSCRIPT_LIMIT=120",
                    "TRANSCRIPT_TODO_PDF_TIMEOUT_SEC=60",
                    "OSC_TRANSCRIPT_TODO_TIMEOUT_SEC=600",
                    "OSC_EVENTS_REFRESH_SOURCE_AUDIT_DRIVE_REMEDIATE_ENABLE=1",
                    python_bin,
                    repo_root / "scripts" / "ops" / "osc_events_refresh.py",
                    "--skip-drive-sync",
                    "--skip-share-repair",
                ),
                required_checks=("google_calendar",),
            ),
            "desc": "OSC/PDF 待辦與行事曆全案快刷（每 2 小時；完整掃描全案檔名與內文、筆錄待辦並修復既有 Google 行程）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 3600,
        },
        {
            "id": "job_osc_todo_governance",
            "cron": "35 3 * * *",
            "command": token_refresh_cron_command(
                repo_root,
                python_bin,
                qcmd(
                    "MAGI_GCAL_DEDUP_ENABLED=1",
                    "MAGI_GCAL_DEDUP_DRY_RUN=0",
                    "MAGI_GCAL_INCREMENTAL_IMPORT=1",
                    "MAGI_GCAL_REPAIR_EXISTING=1",
                    "OSC_PDF_CALENDAR_FULL_FILENAME_SWEEP=1",
                    "OSC_PDF_CALENDAR_FILENAME_SWEEP_LIMIT=50000",
                    "OSC_PDF_CALENDAR_FINAL_DOC_PRIORITY_CASE_LIMIT=2000",
                    "OSC_PDF_CALENDAR_FULL_TEXT_SCAN=1",
                    "OSC_PDF_CALENDAR_FULL_TEXT_ALL_CASES=1",
                    "OSC_PDF_CALENDAR_FULL_TEXT_SCAN_LIMIT=10000",
                    "OSC_PDF_CALENDAR_BUDGET_SEC=7200",
                    "OSC_PDF_CALENDAR_TARGET_TIMEOUT_SEC=180",
                    "OSC_PDF_CALENDAR_FILE_TIMEOUT_SEC=8",
                    "OSC_EVENTS_REFRESH_PDF_LIMIT=10000",
                    "OSC_EVENTS_REFRESH_PDF_CASE_BATCH=500",
                    "OSC_EVENTS_REFRESH_SCAN_BUDGET_SEC=7200",
                    "OSC_EVENTS_REFRESH_SOURCE_AUDIT_DRIVE_REMEDIATE_ENABLE=1",
                    python_bin,
                    repo_root / "scripts" / "ops" / "osc_events_refresh.py",
                    "--force-rebuild",
                ),
                required_checks=("google_calendar",),
            ),
            "desc": "OSC 待辦治理每日深掃（全案件檔名與正文、終結文件、筆錄、補漏及 Google 行程修復）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 10800,
        },
        {
            "id": "job_osc_overdue_todo_reconcile",
            "cron": "15 4 * * *",
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "reconcile_overdue_todos.py",
                "--apply",
                "--json-out",
                repo_root / ".runtime" / "overdue_todo_reconcile_latest.json",
            ),
            "desc": "OSC 逾期待辦治理（每日 04:15；歷史行程與已完成證據自動結清，無證據者保守升級）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 900,
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
            "cron": "3,18,33,48 * * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                "--",
                "/bin/bash",
                omlx_switch,
                "auto",
            ),
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
            "command": qcmd(python_bin, repo_root / "scripts" / "ops" / "resource_governor.py", "--json", "status"),
            "desc": "MAGI 資源治理守門（磁碟/swap/記憶體分級，重型任務降級依據）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 120,
            "no_catchup": True,
        },
        {
            "id": "job_function_health_index",
            "cron": "2,12,22,32,42,52 * * * *",
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "function_health_index.py",
                "--compact",
                "--fail-on-health",
                "--json-out",
                repo_root / ".runtime" / "function_health_index_latest.json",
            ),
            "desc": "功能健康索引（每 10 分鐘同步排程與健康證據，唯讀）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 180,
            "no_catchup": True,
        },
        {
            "id": "job_magi_self_repair_guardian",
            "cron": "15 6 * * *",
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "magi_self_repair_guardian.py",
                "--mode",
                "repair-safe",
                "--fail-on-issues",
                "--json-out",
                repo_root / ".runtime" / "magi_self_repair_guardian_latest.json",
            ),
            "desc": "MAGI 自我修復守護（每日 06:15；僅清理具所有權標記的低風險殘留）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 300,
            "no_catchup": True,
        },
        {
            "id": "job_self_repair_reporter",
            "cron": "20 6 * * *",
            "command": qcmd(python_bin, repo_root / "skills" / "ops" / "self_repair_reporter.py", "--force"),
            "desc": "MAGI 自我修復週報（每日 06:20；彙整 guardian 後的失敗、恢復與待處理問題）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 120,
            "no_catchup": True,
        },
        {
            "id": "job_legacy_judgment_resummary_quality",
            "cron": "*/15 * * * *",
            "command": guarded_cron_command(
                repo_root,
                python_bin,
                "job_legacy_judgment_resummary_quality",
                qcmd(
                    python_bin,
                    repo_root / "scripts" / "ops" / "judgment_summary_staged_backfill.py",
                    "--scan-limit",
                    "480",
                    "--nvidia-limit",
                    "32",
                    "--nvidia-timeout",
                    "180",
                    "--local-min-score",
                    "80",
                    "--json-out",
                    repo_root / ".runtime" / "legacy_judgment_resummary_latest.json",
                ),
                block_at="throttle",
                timeout_sec=900,
            ),
            "desc": "判決摘要兩階段持續清理（每 15 分鐘先做來源逐字高信心抽取，再由 NVIDIA 120B 有界複核）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            # 實作仍由 resource_guarded_run 在 780 秒中止；排程層允許
            # 在資源繁忙時等待 30 分鐘再開始，避免把可恢復的排隊誤報失敗。
            "timeout_sec": 1800,
            "no_catchup": True,
            "long_job": True,
            "resource_guarded": True,
            "resource_block_at": "throttle",
        },
        {
            "id": "job_api_token_health_check",
            "cron": "10 */6 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                "--",
                python_bin,
                repo_root / "scripts" / "ops" / "token_health_check.py",
                "--refresh",
                "--threshold-days",
                "7",
                "--notify",
                "--json-out",
                repo_root / ".runtime" / "token_health" / "token_health_latest.json",
            ),
            "desc": "API/OAuth token 預防性刷新與失效告警（每 6 小時；Google Sheets/Drive/Calendar + 外部 API key 設定）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 180,
            "no_catchup": True,
        },
        {
            "id": "job_heavy_translation_quality_live",
            "cron": "25 4 * * *",
            "command": guarded_cron_command(
                repo_root,
                python_bin,
                "job_heavy_translation_quality_live",
                qcmd(
                    python_bin,
                    run_with_env,
                    "--",
                    python_bin,
                    repo_root / "scripts" / "ops" / "heavy_translation_quality_live.py",
                    "--json-out",
                    repo_root / ".runtime" / "heavy_translation_quality_latest.json",
                ),
                block_at="core_only",
                timeout_sec=420,
            ),
            "desc": "@heavy 翻譯品質 live gate（NVIDIA route、DOI 降噪、臺灣法律術語、DOCX 讀回）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 480,
            "no_catchup": True,
        },
        {
            "id": "job_reboot_before_day_model_switch",
            "cron": "35 6 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                "MAGI_ALLOW_SCHEDULED_REBOOT=1",
                "--",
                python_bin,
                repo_root / "scripts" / "ops" / "scheduled_reboot_guard.py",
                "--mode",
                "day",
                "--apply",
                "--json",
            ),
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
            "command": qcmd(
                python_bin,
                run_with_env,
                "MAGI_ALLOW_SCHEDULED_REBOOT=1",
                "--",
                python_bin,
                repo_root / "scripts" / "ops" / "scheduled_reboot_guard.py",
                "--mode",
                "night",
                "--apply",
                "--json",
            ),
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
            "command": qcmd(python_bin, repo_root / "scripts" / "ops" / "disk_low_water_alarm.py"),
            "desc": "磁碟低水位守門（每小時；低於門檻自動執行保守回收並通知）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 600,
            "no_catchup": True,
        },
        {
            "id": "job_empty_case_shell_cleanup",
            "cron": "8,23,38,53 * * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                "MAGI_CLEAN_EMPTY_CASE_SHELL_INCLUDE_LOCAL=1",
                "--",
                python_bin,
                repo_root / "scripts" / "ops" / "cleanup_synology_empty_case_shells.py",
                "--apply",
                "--limit",
                "0",
                "--max-seconds",
                "240",
                "--json-out",
                repo_root / ".runtime" / "empty_case_shell_cleanup_latest.json",
            ),
            "desc": "已結案案件空資料夾清理（每 15 分鐘；只刪 DB 已結案/已封存且無真實檔案的進行中空殼）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 300,
            "no_catchup": True,
        },
        {
            "id": "job_slow_archive_closed_cases",
            "cron": "40 5 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                "MAGI_SLOW_ARCHIVE_BWLIMIT_MBPS=80",
                "MAGI_SLOW_ARCHIVE_MAX_RUNTIME_SEC=7200",
                "MAGI_SLOW_ARCHIVE_RSYNC_TIMEOUT_SEC=600",
                "--",
                python_bin,
                repo_root / "scripts" / "ops" / "start_slow_archive_closed_cases.py",
                "--apply",
                "--limit",
                "3",
                "--min-size-mb",
                "0",
                "--bwlimit-mbps",
                "80",
                "--max-runtime-sec",
                "7200",
                "--rsync-timeout-sec",
                "600",
                "--json-out",
                repo_root / ".runtime" / "slow_archive_closed_cases_latest.json",
            ),
            "desc": "已結案案件離峰慢搬（05:40 背景啟動；80MB/s 升級 NAS 限速可續傳；清掉仍留在進行中根目錄的殘留資料夾，游秀鈴 2025-0002 仍優先）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 7500,
            "no_catchup": True,
        },
        {
            "id": "job_disk_cleanup_healthcheck",
            "cron": "55 3 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                "MAGI_DISK_CLEANUP_DRY_RUN=0",
                "MAGI_DISK_NAS_RECYCLE_ENABLE=1",
                "--",
                python_bin,
                repo_root / "scripts" / "ops" / "disk_cleanup_healthcheck.py",
                "--apply",
            ),
            "desc": "磁碟自動清理與壓縮（每日 03:55；快取上限、舊報告 gzip、備份保留、NAS 回收筒保留 14 天）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 1800,
            "no_catchup": True,
        },
        {
            "id": "job_nas_recycle_heavy_cleanup",
            "cron": "20 4 * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                "MAGI_DISK_CLEANUP_DRY_RUN=0",
                "MAGI_DISK_NAS_RECYCLE_ENABLE=1",
                "MAGI_DISK_NAS_RECYCLE_HEAVY_ENABLE=1",
                "MAGI_DISK_NAS_RECYCLE_MAX_DELETE_ITEMS=20",
                "MAGI_DISK_NAS_RECYCLE_HEAVY_MAX_RUNTIME_SEC=900",
                "MAGI_DISK_NAS_RECYCLE_HEAVY_MAX_FILES=5000",
                "--",
                python_bin,
                repo_root / "scripts" / "ops" / "disk_cleanup_healthcheck.py",
                "--apply",
            ),
            "desc": "NAS 回收筒重型舊備份離峰分批清理（每日 04:20；只處理回收筒內 Backup/Drive/SteamLibrary/.app）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 1200,
            "no_catchup": True,
        },
        {
            "id": "job_weekly_cache_cleanup",
            "cron": "10 4 * * 0",
            "command": qcmd(python_bin, repo_root / "scripts" / "ops" / "weekly_cache_cleanup.py"),
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
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "model_live_gate.py",
                "--expect",
                "auto",
                "--json",
                "--json-out",
                repo_root / ".runtime" / "model_live_gate_latest.json",
            ),
            "desc": "MAGI 日夜模型拓撲守門（確認 8080/8081/8082/8083 與日夜 profile 一致）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 120,
            "no_catchup": True,
        },
        {
            "id": "job_nightly_bookmark_regex",
            "cron": "15 2 * * *",
            "command": guarded_cron_command(
                repo_root,
                python_bin,
                "job_nightly_bookmark_regex",
                qcmd(
                    python_bin,
                    repo_root / "scripts" / "weekend_bookmark_batch.py",
                    "--stage",
                    "regex",
                    "--max-minutes",
                    "30",
                    "--single-doc-fastpath",
                    "--skip-large-non-ocr",
                    "--defer-large-ocr-pages",
                    "350",
                    "--file-timeout-sec",
                    "45",
                    "--write-followup-plan",
                    "--enqueue-ocr-followups",
                ),
                block_at="throttle",
            ),
            "desc": "卷宗自動書籤夜間增量（regex-only + OCR 佇列，每日 02:15，30 分鐘上限）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 2100,
            "no_catchup": True,
        },
        {
            "id": "job_pdf_bookmark_label_repair",
            "cron": "35 4 * * *",
            "command": guarded_cron_command(
                repo_root,
                python_bin,
                "job_pdf_bookmark_label_repair",
                qcmd(
                    python_bin,
                    repo_root / "scripts" / "ops" / "repair_pdf_bookmark_labels.py",
                    "--apply",
                    "--limit",
                    "12",
                    "--max-files",
                    "120",
                    "--max-dirs",
                    "2500",
                    "--max-seconds",
                    "1800",
                    "--per-file-timeout",
                    "90",
                    "--max-file-mb",
                    "80",
                    "--json-out",
                    repo_root / ".runtime" / "pdf_bookmark_label_repair_latest.json",
                ),
                block_at="throttle",
            ),
            "desc": "PDF 既有書籤污染稽核與重標（每日 04:35；80MB 以下快修，避開 02:15 增量書籤）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 2100,
            "no_catchup": True,
        },
        {
            "id": "job_pdf_bookmark_large_volume_repair",
            "cron": "55 4 * * *",
            "command": guarded_cron_command(
                repo_root,
                python_bin,
                "job_pdf_bookmark_large_volume_repair",
                qcmd(
                    python_bin,
                    repo_root / "scripts" / "ops" / "repair_pdf_bookmark_labels.py",
                    "--apply",
                    "--limit",
                    "1",
                    "--max-files",
                    "40",
                    "--max-dirs",
                    "2500",
                    "--max-seconds",
                    "3600",
                    "--per-file-timeout",
                    "900",
                    "--min-file-mb",
                    "80",
                    "--max-file-mb",
                    "1600",
                    "--target-hint",
                    "閱卷資料",
                    "--target-hint",
                    "判決書",
                    "--target-hint",
                    "法院通知",
                    "--target-hint",
                    "程序裁定",
                    "--json-out",
                    repo_root / ".runtime" / "pdf_bookmark_large_volume_repair_latest.json",
                ),
                block_at="core_only",
            ),
            "desc": "大型卷宗 PDF 書籤污染慢修（每日 04:55；80MB 以上、單次 1 份、負載過高即跳過）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 3900,
            "no_catchup": True,
        },
        {
            "id": "job_weekend_bookmark",
            "cron": "15 3 * * 6,0",
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "resource_guarded_run.py",
                "--job-id",
                "job_weekend_bookmark",
                "--block-at",
                "throttle",
                "--require-free-inactive-gb",
                "6",
                "--timeout-sec",
                "19800",
                "--termination-grace-sec",
                "60",
                "--",
                python_bin,
                repo_root / "scripts" / "weekend_bookmark_batch.py",
                "--stage",
                "all",
                "--max-minutes",
                "330",
                "--max-runtime-minutes",
                "300",
                "--vision-max-pages",
                "350",
                "--single-doc-fastpath",
                "--skip-large-non-ocr",
                "--defer-large-ocr-pages",
                "350",
                "--file-timeout-sec",
                "45",
                "--write-followup-plan",
                "--enqueue-ocr-followups",
            ),
            "desc": "卷宗自動書籤週末完整回合（週六主跑、週日依 checkpoint 自動補跑；6GB admission、5h checkpoint、6h outer timeout）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 21600,
            "no_catchup": True,
        },
        {
            "id": "job_nas_pdf_ocr_worker_offpeak",
            "cron": "45 0-6,22-23 * * *",
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "resource_guarded_run.py",
                "--job-id",
                "job_nas_pdf_ocr_worker_offpeak",
                "--block-at",
                "throttle",
                "--require-disk-free-gb",
                "30",
                "--require-free-inactive-gb",
                "3",
                "--",
                python_bin,
                repo_root / "skills" / "documents" / "nas_pdf_ocr_worker.py",
                "work",
                "--batch",
                "2",
            ),
            "desc": "NAS PDF OCR 離峰佇列處理（22:45–06:45 每小時 2 份；依 16GB 主機容量放寬處理，仍保留防止磁碟耗盡的安全底線）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 3600,
            "no_catchup": True,
        },
        {
            "id": "job_benchmark_pdf_bookmarker",
            "cron": "40 14 * * *",
            "command": guarded_cron_command(
                repo_root,
                python_bin,
                "job_benchmark_pdf_bookmarker",
                qcmd(python_bin, repo_root / "scripts" / "ops" / "benchmark_pdf_bookmarker.py"),
            ),
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
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "tailscale_funnel_healthcheck.py",
                "--apply",
                "--json-out",
                repo_root / ".runtime" / "tailscale_funnel_health_latest.json",
            ),
            "desc": "Tailscale Funnel 外網入口巡檢（每 10 分鐘；核對公共 DNS、登入邊界與唯一 443→5002 規則，僅允許受限重申）",
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
            "command": qcmd(python_bin, repo_root / "scripts" / "nightly_distill_gemma.py"),
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
            "command": qcmd(
                python_bin,
                run_with_env,
                "MAGI_PDF_NAMER_DOCLING_ENABLED=1",
                "--",
                python_bin,
                repo_root / "skills" / "pdf-namer" / "nightly_layout.py",
            ),
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


def canonicalize_job_command(job: dict[str, Any], repo_root: Path) -> tuple[dict[str, Any], bool]:
    command = job.get("command")
    if not isinstance(command, str) or not command:
        return job, False
    root_text = str(repo_root)
    new_command = command
    for old_root in KNOWN_MAGI_ROOTS:
        if old_root != root_text:
            new_command = new_command.replace(old_root, root_text)
    new_command = quote_repo_root_paths(new_command, repo_root)
    if new_command == command:
        return job, False
    return {**job, "command": new_command}, True


def seed_jobs(repo_root: Path = REPO_ROOT, *, python_path: Path | None = None) -> dict[str, Any]:
    cron_path = repo_root / "cron_jobs.json"
    jobs = load_jobs(cron_path)
    desired_jobs = [
        worldmonitor_job(repo_root, python_path),
        *deterministic_legacy_replacements(repo_root, python_path),
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
        if job_id in DEPRECATED_JOB_IDS and (
            job.get("enabled") is not False
            or job.get("no_catchup") is not True
            or not str(job.get("desc") or "").startswith("已停用：")
        ):
            job = {
                **job,
                "enabled": False,
                "no_catchup": True,
                "desc": f"已停用：{job.get('desc') or job_id}",
            }
            changed = True
        job, path_changed = canonicalize_job_command(job, repo_root)
        if path_changed:
            changed = True
        if job.get("enabled") is not False and not job.get("timeout_sec"):
            job = {**job, "timeout_sec": cron_job_timeout(job)}
            changed = True
        deduped.append(job)
    if len(deduped) != len(jobs):
        changed = True
    jobs = deduped

    if changed:
        cron_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {"ok": True, "path": str(cron_path), "changed": changed, "jobs": len(jobs)}


def main() -> int:
    result = seed_jobs()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
