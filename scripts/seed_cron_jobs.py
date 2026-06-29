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
DEPRECATED_JOB_IDS = {
    # Replaced by pdf-bookmarker/pdf-namer. Keeping it enabled caused an extra
    # 02:10 NAS scan and duplicated PDF tagging logic.
    "job_1772867062892_e33b6a",
}

KNOWN_MAGI_ROOTS = (
    "/Users/ai/Desktop/MAGI_v2",
    "/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2",
)


def qcmd(*parts: object) -> str:
    return " ".join(shlex.quote(str(part)) for part in parts)


def quote_repo_root_paths(command: str, repo_root: Path) -> str:
    root_text = str(repo_root)
    if " " not in root_text or root_text not in command:
        return command
    pattern = re.compile(rf"(?<!['\"]){re.escape(root_text)}(?:/[^'\"\s]+)*")
    return pattern.sub(lambda match: shlex.quote(match.group(0)), command)


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


def token_refresh_cron_command(repo_root: Path, python_bin: Path, command: str) -> str:
    """Refresh Google OAuth tokens immediately before a Google-dependent cron job."""
    wrapper = repo_root / "scripts" / "ops" / "run_after_token_refresh.py"
    return qcmd(python_bin, wrapper, "--") + " " + command


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


def business_jobs(repo_root: Path = REPO_ROOT, python_path: Path | None = None) -> list[dict[str, Any]]:
    """Core single-machine business jobs that must exist on fresh installs."""
    python_bin = python_path or default_python_path(repo_root)
    run_with_env = repo_root / "scripts" / "ops" / "run_with_env.py"
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
            "cron": "*/5 * * * *",
            "command": qcmd(
                python_bin,
                run_with_env,
                "--",
                python_bin,
                repo_root / "scripts" / "ops" / "laf_gmail_dispatch_scan.py",
                "--max-results",
                "80",
                "--json-out",
                repo_root / "static" / "laf_gmail_monitor_state.json",
            ),
            "desc": "法扶 Gmail 派案/回報 one-shot 補掃（每 5 分鐘；常駐 monitor 掛住時仍可處理新信）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 420,
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
        },
        {
            "id": "job_laf_portal_new_files_scan",
            "cron": "15 */6 * * *",
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "laf_portal_new_files_scan.py",
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
        },
        {
            "id": "job_file_review_check",
            "cron": "0 10,15 * * 1-5",
            "command": qcmd(python_bin, repo_root / "skills" / "file-review-orchestrator" / "action.py", "--task", "download"),
            "desc": "閱卷通知與下載檢查（平日 10:00, 15:00；下載前去重）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
        },
        {
            "id": "job_transcript_sync",
            "cron": "0 6,21 * * *",
            "command": qcmd(python_bin, repo_root / "skills" / "transcript-downloader" / "action.py", "--task", "sync"),
            "desc": "筆錄同步（每日 06:00, 21:00）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
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
            "cron": "10 3 * * *",
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
            "command": token_refresh_cron_command(
                repo_root,
                python_bin,
                qcmd(
                    python_bin,
                    repo_root / "scripts" / "import_accounting_sheet.py",
                    "--commit",
                    "--include-previous",
                ),
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
                        repo_root / "scripts" / "drive_case_sync_worker.py",
                        "--matched-case-limit",
                        "24",
                        "--download-limit",
                        "80",
                        "--upload-limit",
                        "80",
                        "--max-download-bytes",
                        "1500000000",
                        "--max-upload-bytes",
                        "1500000000",
                        "--max-case-depth",
                        "5",
                        "--max-case-items",
                        "240",
                        "--create-drive-folder-limit",
                        "10",
                        "--create-drive-folder-max-age-hours",
                        "168",
                        "--priority-upcoming-days",
                        "21",
                        "--priority-case-limit",
                        "80",
                        "--inventory-timeout-sec",
                        "1200",
                    ),
                ),
                block_at="core_only",
                require_disk_free_gb=30,
                require_free_inactive_gb=3,
                timeout_sec=2400,
            ),
            "desc": "Google Drive/NAS 案件辦理 bounded 雙向同步（每 6 小時；先同步近期待辦案件；新案補建 Drive 資料夾；缺檔才補，不覆蓋、不刪除）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "long_job": True,
            "timeout_sec": 3600,
            "resource_guarded": True,
            "resource_block_at": "core_only",
        },
        {
            "id": "job_drive_case_sync_all_files",
            "cron": "12 1,7,13,19 * * *",
            "command": guarded_cron_command(
                repo_root,
                python_bin,
                "job_drive_case_sync_all_files",
                token_refresh_cron_command(
                    repo_root,
                    python_bin,
                    qcmd(
                        "MAGI_DRIVE_SYNC_LOCAL_SCAN_TIMEOUT_SEC=8",
                        "MAGI_DRIVE_SYNC_DRIVE_LIST_TIMEOUT_SEC=20",
                        python_bin,
                        repo_root / "scripts" / "drive_case_sync_worker.py",
                        "--direct-all-cases",
                        "--direct-all-case-limit",
                        "96",
                        "--download-limit",
                        "240",
                        "--upload-limit",
                        "240",
                        "--max-download-bytes",
                        "3000000000",
                        "--max-upload-bytes",
                        "3000000000",
                        "--max-case-depth",
                        "6",
                        "--max-case-items",
                        "360",
                        "--create-drive-folder-limit",
                        "24",
                        "--create-drive-folder-max-age-hours",
                        "168",
                        "--priority-upcoming-days",
                        "0",
                        "--inventory-timeout-sec",
                        "2400",
                    ),
                ),
                block_at="core_only",
                require_disk_free_gb=30,
                require_free_inactive_gb=3,
                timeout_sec=4800,
            ),
            "desc": "Google Drive/NAS 全案件輪巡雙向同步（每 6 小時、錯開近期待辦同步避免鎖定互相飢餓；依 DB folder_path 輪巡，不改兩邊命名規則；缺檔才補，不覆蓋、不刪除）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "long_job": True,
            "timeout_sec": 3600,
            "resource_guarded": True,
            "resource_block_at": "core_only",
        },
        {
            "id": "job_osc_events_refresh",
            "cron": "35 */6 * * *",
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
                    "OSC_PDF_CALENDAR_FULL_TEXT_SCAN=0",
                    "OSC_EVENTS_REFRESH_PDF_LIMIT=500",
                    python_bin,
                    repo_root / "scripts" / "ops" / "osc_events_refresh.py",
                ),
            ),
            "desc": "OSC/PDF/筆錄待辦與行事曆事件更新（每 6 小時；先補 Drive/NAS 缺檔，再全量掃描法院 PDF 檔名並登檔；PDF 正文/OCR 深掃留給半夜治理；GCal 去重/修復）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 1500,
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
                    "OSC_PDF_CALENDAR_FULL_TEXT_SCAN=1",
                    "OSC_PDF_CALENDAR_FULL_TEXT_SCAN_LIMIT=5000",
                    "OSC_EVENTS_REFRESH_PDF_LIMIT=5000",
                    "OSC_EVENTS_REFRESH_SCAN_BUDGET_SEC=1800",
                    "OSC_EVENTS_REFRESH_DRIVE_SYNC_ALL_CASES=1",
                    "OSC_EVENTS_REFRESH_DRIVE_SYNC_ALL_CASE_LIMIT=64",
                    python_bin,
                    repo_root / "scripts" / "ops" / "osc_events_refresh.py",
                    "--force-rebuild",
                ),
            ),
            "desc": "OSC 待辦治理巡檢（每日 03:35；先做 Drive/NAS 補檔，再全量掃描法院 PDF 檔名與正文、補漏、重複/殘影日曆清理，不依賴使用者回報）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "no_catchup": True,
            "timeout_sec": 2100,
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
                    "90",
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
            "cron": "15 3 * * 6",
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "weekend_bookmark_batch.py",
                "--stage",
                "all",
                "--single-doc-fastpath",
                "--skip-large-non-ocr",
                "--defer-large-ocr-pages",
                "350",
                "--file-timeout-sec",
                "90",
                "--write-followup-plan",
                "--enqueue-ocr-followups",
            ),
            "desc": "卷宗自動書籤週末完整回合（regex + OCR 佇列 + vision 補漏，6h timeout）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 21600,
            "no_catchup": True,
        },
        {
            "id": "job_nas_pdf_ocr_worker_offpeak",
            "cron": "45 1,3,5,22 * * *",
            "command": qcmd(
                python_bin,
                repo_root / "scripts" / "ops" / "resource_guarded_run.py",
                "--job-id",
                "job_nas_pdf_ocr_worker_offpeak",
                "--block-at",
                "throttle",
                "--require-disk-free-gb",
                "80",
                "--require-free-inactive-gb",
                "4",
                "--",
                python_bin,
                repo_root / "skills" / "documents" / "nas_pdf_ocr_worker.py",
                "work",
                "--batch",
                "1",
            ),
            "desc": "NAS PDF OCR 離峰佇列處理（每次 1 份；資源不足自動略過，避免拖垮 NAS/Mac）",
            "channel_id": None,
            "last_run": None,
            "last_run_minute": None,
            "enabled": True,
            "timeout_sec": 1500,
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
