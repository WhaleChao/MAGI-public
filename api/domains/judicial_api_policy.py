"""Central load policy for Judicial Yuan API jobs.

MAGI treats the official Judicial Yuan API as an incremental cache source while
semantic retrieval is handled by Taiwan Legal RAG/TLR first.  The default
TLR-smart mode keeps summaries extractive and skips attachments/vectors, but it
may drain backlog aggressively when a NAS-backed cache is available.
"""
from __future__ import annotations

import os
from typing import MutableMapping

LOAD_MODE_ENV = "MAGI_JUDICIAL_API_LOAD_MODE"
DEFAULT_LOAD_MODE = "tlr_smart"


_MODE_DEFAULTS: dict[str, dict[str, str]] = {
    "tlr_smart": {
        "enable_day_process": "1",
        "enable_night_pull": "1",
        "enable_nightly_process": "1",
        "night_max_jdocs": "600",
        "night_max_days": "3",
        "night_timeout_sec": "2400",
        "day_max_docs": "240",
        "day_summary_max": "48",
        "day_summary_mode": "extractive",
        "day_skip_assets": "1",
        "day_vector_ingest": "0",
        "day_timeout_sec": "2400",
        "day_retry_on_backlog": "1",
        "day_retry_max_docs": "480",
        "day_retry_timeout_sec": "1800",
        "nightly_process_max_docs": "240",
        "nightly_summary_max": "48",
        "nightly_summary_mode": "extractive",
        "nightly_skip_assets": "1",
        "nightly_vector_ingest": "0",
        "nightly_process_timeout_sec": "3600",
        "tlr_cache_hits": "1",
    },
    "balanced": {
        "enable_day_process": "1",
        "enable_night_pull": "1",
        "enable_nightly_process": "1",
        "night_max_jdocs": "1500",
        "night_max_days": "7",
        "night_timeout_sec": "3600",
        "day_max_docs": "150",
        "day_summary_max": "30",
        "day_summary_mode": "extractive",
        "day_skip_assets": "1",
        "day_vector_ingest": "0",
        "day_timeout_sec": "1800",
        "day_retry_on_backlog": "0",
        "day_retry_max_docs": "300",
        "day_retry_timeout_sec": "900",
        "nightly_process_max_docs": "200",
        "nightly_summary_max": "60",
        "nightly_summary_mode": "extractive",
        "nightly_skip_assets": "1",
        "nightly_vector_ingest": "0",
        "nightly_process_timeout_sec": "3600",
        "tlr_cache_hits": "1",
    },
    "legacy": {
        "enable_day_process": "1",
        "enable_night_pull": "1",
        "enable_nightly_process": "1",
        "night_max_jdocs": "25000",
        "night_max_days": "0",
        "night_timeout_sec": "5400",
        "day_max_docs": "200",
        "day_summary_max": "80",
        "day_summary_mode": "llm",
        "day_skip_assets": "0",
        "day_vector_ingest": "1",
        "day_timeout_sec": "3600",
        "day_retry_on_backlog": "1",
        "day_retry_max_docs": "400",
        "day_retry_timeout_sec": "1200",
        "nightly_process_max_docs": "400",
        "nightly_summary_max": "200",
        "nightly_summary_mode": "llm",
        "nightly_skip_assets": "0",
        "nightly_vector_ingest": "1",
        "nightly_process_timeout_sec": "7200",
        "tlr_cache_hits": "0",
    },
}


_ENV_TO_KEY: dict[str, str] = {
    "MAGI_ENABLE_JUDICIAL_API_DAY_PROCESS": "enable_day_process",
    "MAGI_ENABLE_JUDICIAL_API_NIGHT_PULL": "enable_night_pull",
    "MAGI_ENABLE_JUDICIAL_API_NIGHTLY_PROCESS": "enable_nightly_process",
    "MAGI_JUDICIAL_API_NIGHT_MAX_JDOCS": "night_max_jdocs",
    "MAGI_JUDICIAL_API_NIGHT_MAX_DAYS": "night_max_days",
    "MAGI_JUDICIAL_API_NIGHT_TIMEOUT_SEC": "night_timeout_sec",
    "MAGI_JUDICIAL_API_DAY_MAX_DOCS": "day_max_docs",
    "MAGI_JUDICIAL_API_DAY_SUMMARY_MAX": "day_summary_max",
    "MAGI_JUDICIAL_API_DAY_SUMMARY_MODE": "day_summary_mode",
    "MAGI_JUDICIAL_API_DAY_SKIP_ASSETS": "day_skip_assets",
    "MAGI_JUDICIAL_API_DAY_VECTOR_INGEST": "day_vector_ingest",
    "MAGI_JUDICIAL_API_DAY_TIMEOUT_SEC": "day_timeout_sec",
    "MAGI_JUDICIAL_API_DAY_RETRY_ON_BACKLOG": "day_retry_on_backlog",
    "MAGI_JUDICIAL_API_DAY_RETRY_MAX_DOCS": "day_retry_max_docs",
    "MAGI_JUDICIAL_API_DAY_RETRY_TIMEOUT_SEC": "day_retry_timeout_sec",
    "MAGI_JUDICIAL_API_NIGHTLY_PROCESS_MAX_DOCS": "nightly_process_max_docs",
    "MAGI_JUDICIAL_API_NIGHTLY_SUMMARY_MAX": "nightly_summary_max",
    "MAGI_JUDICIAL_API_NIGHTLY_SUMMARY_MODE": "nightly_summary_mode",
    "MAGI_JUDICIAL_API_NIGHTLY_SKIP_ASSETS": "nightly_skip_assets",
    "MAGI_JUDICIAL_API_NIGHTLY_VECTOR_INGEST": "nightly_vector_ingest",
    "MAGI_JUDICIAL_API_NIGHTLY_PROCESS_TIMEOUT_SEC": "nightly_process_timeout_sec",
    "MAGI_TWLEGALRAG_CACHE_HITS": "tlr_cache_hits",
    "JUDICIAL_API_NIGHT_MAX_JDOCS": "night_max_jdocs",
    "JUDICIAL_API_NIGHT_MAX_DAYS": "night_max_days",
    "JUDICIAL_API_DAY_MAX_PROCESS": "day_max_docs",
    "JUDICIAL_API_DAY_SUMMARY_MAX": "day_summary_max",
    "JUDICIAL_API_DAY_SUMMARY_MODE": "day_summary_mode",
    "JUDICIAL_API_DAY_SKIP_ASSETS": "day_skip_assets",
}


def judicial_api_load_mode() -> str:
    mode = str(os.environ.get(LOAD_MODE_ENV) or DEFAULT_LOAD_MODE).strip().lower()
    return mode if mode in _MODE_DEFAULTS else DEFAULT_LOAD_MODE


def judicial_api_default(key: str, fallback: str = "") -> str:
    mode = judicial_api_load_mode()
    defaults = _MODE_DEFAULTS.get(mode) or _MODE_DEFAULTS[DEFAULT_LOAD_MODE]
    return str(defaults.get(key, fallback))


def judicial_api_env_default(env_name: str, fallback: str = "") -> str:
    key = _ENV_TO_KEY.get(str(env_name or "").strip())
    if not key:
        return fallback
    return judicial_api_default(key, fallback)


def apply_judicial_api_env_defaults(env: MutableMapping[str, str] | None = None) -> None:
    target = env if env is not None else os.environ
    target.setdefault(LOAD_MODE_ENV, judicial_api_load_mode())
    for env_name in sorted(_ENV_TO_KEY):
        target.setdefault(env_name, judicial_api_env_default(env_name, ""))


def judicial_api_policy_report() -> dict[str, str]:
    mode = judicial_api_load_mode()
    report = {"mode": mode}
    report.update(_MODE_DEFAULTS.get(mode) or _MODE_DEFAULTS[DEFAULT_LOAD_MODE])
    return report
