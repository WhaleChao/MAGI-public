"""Shared execution limits for every MAGI cron dispatcher."""

from __future__ import annotations

from typing import Any, Mapping


DEFAULT_TIMEOUT_SEC = 600
LONG_TIMEOUT_SEC = 7200

LONG_JOB_IDS = frozenset(
    {
        "job_nightly_regression",
        "job_distill_train",
        "job_weekend_resummary",
        "job_pdf_namer_nightly",
        "job_reprocess_insights",
        "job_obsidian_ingest",
        "job_laf_nightly_audit",
        "job_nightly_autopilot",
        "job_judicial_api_night_pull",
        "job_judicial_api_morning",
        "job_weekly_legal_crawl",
        "job_transcript_sync",
        "job_file_review_check",
        "job_weekend_bookmark",
        "job_nightly_bookmark_regex",
        "job_market_briefing_script",
        "job_wiki_synthesizer",
        "job_knowledge_lint",
        "job_smoke_external_chat",
        "job_translator_ape_regression",
        "job_omlx_switch_night",
        "job_omlx_switch_day",
        "job_benchmark_pdf_bookmarker",
        "job_research_brief_daily",
        "job_disk_cleanup_healthcheck",
        "job_1772867062892_6cef0b",
        "job_1776221713533_0a5366",
        "job_weekly_cache_cleanup",
        "job_benchmark_pdf_namer",
        "job_case_index_sync",
        "job_self_repair_reporter",
    }
)

JOB_TIMEOUT_OVERRIDES = {
    "job_nightly_autopilot": 28800,
    "job_weekend_bookmark": 21600,
}


def cron_job_timeout(job: Mapping[str, Any]) -> int:
    """Return the effective positive timeout for a cron job."""
    custom_timeout = job.get("timeout_sec")
    if isinstance(custom_timeout, (int, float)) and not isinstance(custom_timeout, bool) and custom_timeout > 0:
        return int(custom_timeout)

    job_id = str(job.get("id") or "")
    if job_id in JOB_TIMEOUT_OVERRIDES:
        return JOB_TIMEOUT_OVERRIDES[job_id]
    if job.get("long_job") is True or job_id in LONG_JOB_IDS:
        return LONG_TIMEOUT_SEC
    return DEFAULT_TIMEOUT_SEC
