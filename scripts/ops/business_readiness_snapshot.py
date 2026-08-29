#!/usr/bin/env python3
"""Build a public-safe snapshot of work that can still block MAGI operations."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable


ROOT = Path(os.environ.get("MAGI_ROOT_DIR") or Path(__file__).resolve().parents[2]).expanduser()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from magi_v3.file_review_receipts import normalize_signature_hashes


def _load_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return int(pid) > 0


def _mutable_static_dir(root: Path, env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get("MAGI_MUTABLE_STATIC_DIR", "").strip() or root / "static").expanduser()


def _runtime_dir(root: Path, env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get("MAGI_RUNTIME_DIR", "").strip() or root / ".runtime").expanduser()


def _agent_dir(root: Path, env: dict[str, str] | None = None) -> Path:
    source = os.environ if env is None else env
    return Path(source.get("MAGI_AGENT_DIR", "").strip() or root / ".agent").expanduser()


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _latest_successful_nvidia_usage(path: Path) -> dict:
    """Return the newest successful API call without trusting a stale gate."""
    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return {}
    for raw in reversed(lines[-2000:]):
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if isinstance(item, dict) and item.get("ok") is True and _parse_time(item.get("ts")):
            return item
    return {}


def _recent_report_failures(root: Path, *, now: datetime, days: int = 7, env: dict[str, str] | None = None) -> dict:
    path = _runtime_dir(root, env) / "laf_report_jobs.jsonl"
    if not path.exists():
        return {"count": 0, "reasons": {}}
    latest_by_case: dict[str, dict] = {}
    cutoff = now - timedelta(days=max(1, days))
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-3000:]:
        try:
            item = json.loads(raw)
        except Exception:
            continue
        if not isinstance(item, dict) or item.get("status") not in {"ok", "failed"}:
            continue
        ts = _parse_time(item.get("ts"))
        if ts is None or ts < cutoff:
            continue
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        identity = result.get("identity") if isinstance(result.get("identity"), dict) else {}
        case_key = str(
            identity.get("case_number")
            or identity.get("laf_case_number")
            or item.get("job_id")
            or ""
        ).strip()
        if case_key:
            latest_by_case[case_key] = item
    failures = [
        item
        for item in latest_by_case.values()
        if item.get("status") == "failed" and not _report_failure_is_now_resolved(item)
    ]
    reasons = Counter()
    for item in failures:
        result = item.get("result") if isinstance(item.get("result"), dict) else {}
        reason = str(result.get("error") or "unknown").strip()
        reasons["missing_required_docs" if reason == "missing_required_docs" else "other"] += 1
    return {"count": len(failures), "reasons": dict(reasons)}


def _report_failure_is_now_resolved(item: dict) -> bool:
    """Re-check document failures so an old log cannot keep the UI red."""
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    if str(result.get("error") or "") != "missing_required_docs":
        return False
    identity = result.get("identity") if isinstance(result.get("identity"), dict) else {}
    case_folder = str(identity.get("case_folder") or "").strip()
    if not case_folder or not os.path.isdir(case_folder):
        return False
    try:
        from casper_ecosystem.law_firm_orchestrators.laf_orchestrator_docmixins import (
            LAFOrchestratorDocumentMixin,
        )

        docs = LAFOrchestratorDocumentMixin()._scan_case_folder_docs(case_folder, action="closing")
        return bool(docs.get("closing_basis_files") or docs.get("mediation_success_files"))
    except Exception:
        return False


def _latest_file_review_job(root: Path, env: dict[str, str] | None = None) -> dict:
    source = os.environ if env is None else env
    configured = source.get("MAGI_FILE_REVIEW_BG_JOB_DIR", "").strip()
    state_root = source.get("MAGI_FILE_REVIEW_STATE_DIR", "").strip()
    runtime_root = source.get("MAGI_RUNTIME_DIR", "").strip()
    if configured:
        jobs_dir = Path(configured).expanduser()
    elif state_root:
        jobs_dir = Path(state_root).expanduser() / "bg-jobs"
    elif runtime_root:
        jobs_dir = Path(runtime_root).expanduser() / "file-review" / "bg-jobs"
    else:
        jobs_dir = root / "skills" / "file-review-orchestrator" / "_bg_jobs"
    try:
        latest = max(jobs_dir.glob("download_*.json"), key=lambda path: path.stat().st_mtime)
    except (OSError, ValueError):
        return {}
    return _load_json(latest)


def _scheduled_file_review_download_enabled(root: Path) -> bool:
    """Return whether the hourly production download pipeline is enabled.

    The always-on FileReviewAuto worker is intentionally scan-only on machines
    that already have ``scheduled_check`` in cron.  Treating that safe setup as
    a business outage kept the menu yellow and encouraged enabling a duplicate
    15-minute full-portal download loop.
    """
    from magi_v3.external_inputs import load_bound_cron_jobs

    jobs = list(load_bound_cron_jobs(root).jobs)
    if not isinstance(jobs, list):
        return False
    return any(
        isinstance(job, dict)
        and job.get("enabled", True) is not False
        and str(job.get("id") or "") == "job_file_review_check"
        and "scheduled_check" in str(job.get("command") or "")
        for job in jobs
    )


def _operations(exec_fn: Callable | None) -> dict:
    if exec_fn is None:
        try:
            from api.blueprints.osc_cases import _osc_exec
            from api.osc.saas_workbench import build_operations_report

            return build_operations_report(_osc_exec)
        except Exception:
            return {}
    try:
        from api.osc.saas_workbench import build_operations_report

        return build_operations_report(exec_fn)
    except Exception:
        return {}


def _business_item_text(value: Any, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _closing_pending_items(operations: dict) -> list[dict]:
    items = []
    for row in operations.get("closing_pending_items") or []:
        if not isinstance(row, dict):
            continue
        items.append(
            {
                "case_number": _business_item_text(row.get("case_number"), 40),
                "client_name": _business_item_text(row.get("client_name"), 80),
                "status": _business_item_text(row.get("legal_aid_status") or row.get("status"), 80),
                "approval_status": _business_item_text(row.get("legal_aid_approval_status"), 40),
            }
        )
    return items


def _review_pending_items(operations: dict) -> list[dict]:
    items = []
    for row in operations.get("pending_review_items") or []:
        if not isinstance(row, dict):
            continue
        description = str(row.get("description") or "")
        match = re.search(r"原期限：([^／\n]+)／原類型：([^\n]+)", description)
        original_due = _business_item_text(match.group(1), 20) if match else ""
        original_type = _business_item_text(match.group(2), 40) if match else ""
        summary = ""
        for raw_line in description.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("【MAGI", "原期限：", "尚無可驗證", "MAGI分享連結：", "連結有效至：")):
                continue
            summary = _business_item_text(line, 160)
            break
        items.append(
            {
                "case_number": _business_item_text(row.get("case_number"), 40),
                "client_name": _business_item_text(row.get("client_name"), 80),
                "review_date": _business_item_text(row.get("todo_date"), 20),
                "original_due_date": original_due,
                "original_type": original_type,
                "summary": summary,
            }
        )
    return items


_LAF_RETRY_REASON_LABELS = {
    "portal_not_listed": "法扶網站目前尚未列出可下載附件",
    "portal_check_failed": "法扶網站檢查失敗，等待下一輪重試",
    "login_failed": "法扶網站登入失敗，等待下一輪重試",
    "missing_local_case_folder": "找不到本機案件資料夾",
    "identity_ambiguous": "案件資料無法唯一比對",
    "portal_attachment_retention_expired": "官網附件下載期限已屆滿，需人工確認是否已另行取得",
    "review_result_download": "已收到明確的官網下載通知，等待附件可下載",
    "startup_backfill_missing_closing_docs": "結案附件尚未歸檔，下載期限內自動補抓",
    "startup_backfill_missing_opening_docs": "開辦附件尚未歸檔，下載期限內自動補抓",
}


def _laf_retry_details(items: list[dict]) -> list[dict]:
    details = []
    for row in items:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status") or "pending_retry").strip().lower()
        # expired/archived 是已終止的歷史證據，不是現在可執行
        # 的待辦，也不應在每次健康快照中反覆警示。
        if status not in {"", "pending_retry", "manual_review", "exhausted"}:
            continue
        reason_key = str(row.get("last_error") or row.get("reason") or "").strip()
        reason = _LAF_RETRY_REASON_LABELS.get(reason_key) or _business_item_text(reason_key.replace("_", " "), 120)
        if status == "exhausted":
            reason = "已達自動重試上限，需要人工確認" + (f"；{reason}" if reason else "")
        elif status == "manual_review" and reason:
            reason = "需要人工確認；" + reason
        details.append(
            {
                "case_number": _business_item_text(row.get("case_number"), 40),
                "laf_case_number": _business_item_text(row.get("laf_case_number"), 40),
                "client_name": _business_item_text(row.get("client_name"), 80),
                "case_type": _business_item_text(row.get("case_type"), 60),
                "case_reason": _business_item_text(row.get("case_reason"), 100),
                "status": "需人工確認" if status in {"manual_review", "exhausted"} else "自動重試中",
                "reason": reason or "附件尚未取得，等待下一輪重試",
                "tries": int(row.get("tries") or 0),
                "last_try_at": _business_item_text(str(row.get("last_try_at") or "").replace("T", " "), 30),
                "first_observed_at": _business_item_text(str(row.get("first_observed_at") or "").replace("T", " "), 30),
                "expires_at": _business_item_text(str(row.get("expires_at") or "").replace("T", " "), 30),
            }
        )
    return details


def _laf_missing_details(portal: dict) -> list[dict]:
    details = []
    for row in portal.get("portal_new_files") or []:
        if not isinstance(row, dict) or int(row.get("new_count") or 0) <= 0:
            continue
        details.append(
            {
                "laf_case_number": _business_item_text(row.get("laf_no") or row.get("case_number"), 40),
                "client_name": _business_item_text(row.get("client_name"), 80),
                "missing_files": [
                    _business_item_text(name, 120)
                    for name in (row.get("missing_files") or [])
                    if str(name or "").strip()
                ],
            }
        )
    return details


def _laf_mapping_details(portal: dict) -> list[dict]:
    """Expose NAS authority uncertainty without calling it a missing file."""
    details = []
    for row in portal.get("portal_new_files") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("reason_code") or "").strip() != "nas_mapping_unverified":
            continue
        details.append(
            {
                "laf_case_number": _business_item_text(
                    row.get("laf_no") or row.get("case_number"), 40
                ),
                "client_name": _business_item_text(row.get("client_name"), 80),
                "file_count": int(
                    row.get("mapping_unverified_count") or row.get("file_count") or 0
                ),
                "storage_status": "NAS映射待驗證",
            }
        )
    return details


def _review_ready_items(review_result: dict) -> list[dict]:
    parsed = (review_result.get("check") or {}).get("parsed") or {}
    rows = parsed.get("ready_to_download_items") if isinstance(parsed, dict) else []
    details = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        details.append(
            {
                "case_number": _business_item_text(row.get("court_case_no") or row.get("case_number"), 80),
                "laf_case_number": _business_item_text(row.get("laf_case_no"), 40),
                "application_no": _business_item_text(row.get("application_no"), 60),
                "client_name": _business_item_text(row.get("client_name"), 80),
                "court": _business_item_text(row.get("court"), 80),
            }
        )
    return details


_FILE_REVIEW_FAILURE_REASON_ALLOWLIST = frozenset(
    {
        "bulk_download_blocked_by_env",
        "court_payload_identity_mismatch",
        "download_retry_pending",
        "file_review_portal_busy",
        "owner_pid_missing",
        "owner_state_path_unavailable",
        "owner_state_unverified",
        "payment_proof_queue_needs_attention",
        "payment_proof_registry_write_failed",
        "portal_downloadable_not_reconciled",
        "portal_probe_failed",
        "portal_probe_transient_retry",
    }
)


def _file_review_failure_fields(review_result: dict, review_job: dict) -> dict:
    """Expose only safe aggregate evidence for a failed review-download cycle."""
    review_result = review_result if isinstance(review_result, dict) else {}
    review_job = review_job if isinstance(review_job, dict) else {}
    scheduled = (
        review_result.get("scheduled_check")
        if isinstance(review_result.get("scheduled_check"), dict)
        else {}
    )
    scheduled_parsed = (
        scheduled.get("parsed") if isinstance(scheduled.get("parsed"), dict) else {}
    )
    scheduled_steps = (
        scheduled_parsed.get("steps")
        if isinstance(scheduled_parsed.get("steps"), dict)
        else {}
    )
    scheduled_download = (
        scheduled_steps.get("download")
        if isinstance(scheduled_steps.get("download"), dict)
        else {}
    )
    review_download = (
        review_result.get("download")
        if isinstance(review_result.get("download"), dict)
        else {}
    )
    job_result = (
        review_job.get("result") if isinstance(review_job.get("result"), dict) else {}
    )
    job_download = (
        job_result.get("download")
        if isinstance(job_result.get("download"), dict)
        else {}
    )
    candidates = (
        review_download,
        scheduled_download,
        job_download,
        job_result,
        review_result,
        review_job,
    )
    failure_reason = next(
        (
            reason
            for candidate in candidates
            for reason in [str(candidate.get("reason") or "").strip().lower()]
            if reason in _FILE_REVIEW_FAILURE_REASON_ALLOWLIST
        ),
        "file_review_download_failed",
    )
    evidence = next(
        (
            candidate
            for candidate in candidates
            if any(
                key in candidate
                for key in (
                    "expected_portal_downloadable_count",
                    "accounted_portal_downloadable_count",
                    "download_reconciliation_verified",
                )
            )
        ),
        {},
    )

    def safe_count(key: str) -> int:
        try:
            return max(0, int(evidence.get(key) or 0))
        except (TypeError, ValueError):
            return 0

    return {
        "failure_reason": failure_reason,
        "expected_portal_downloadable_count": safe_count(
            "expected_portal_downloadable_count"
        ),
        "accounted_portal_downloadable_count": safe_count(
            "accounted_portal_downloadable_count"
        ),
        "download_reconciliation_verified": bool(
            evidence.get("download_reconciliation_verified") is True
        ),
    }


def build_snapshot(
    *,
    root: Path = ROOT,
    env: dict[str, str] | None = None,
    exec_fn: Callable | None = None,
    now: datetime | None = None,
    mlx_available: bool | None = None,
    whisper_cli: str | None = None,
) -> dict:
    root = Path(root)
    env = dict(os.environ if env is None else env)
    now = now or datetime.now()
    operations = _operations(exec_fn)
    report_failures = _recent_report_failures(root, now=now, env=env)
    closing_pending = int(operations.get("closing_pending_cases") or 0) if operations else 0
    review_pending = int(operations.get("pending_review_todos") or 0) if operations else 0
    closing_items = _closing_pending_items(operations) if operations else []
    review_items = _review_pending_items(operations) if operations else []
    attention_items = _closing_pending_items(
        {"closing_pending_items": operations.get("laf_attention_items") or []}
    ) if operations else []
    attention_count = int(operations.get("laf_attention_cases") or len(attention_items)) if operations else 0
    branch_pending_items = _closing_pending_items(
        {"closing_pending_items": operations.get("laf_branch_pending_items") or []}
    ) if operations else []
    branch_pending = int(operations.get("laf_branch_pending_cases") or len(branch_pending_items)) if operations else 0
    action_pending = max(0, closing_pending - attention_count)

    if report_failures["count"] or attention_count:
        missing_docs = int(report_failures["reasons"].get("missing_required_docs") or 0)
        label_parts = []
        if attention_count:
            label_parts.append(f"{attention_count}案需補正/退件")
        if report_failures["count"]:
            label_parts.append(f"{report_failures['count']}案受阻")
        label = "／".join(label_parts)
        if report_failures["count"] and missing_docs == report_failures["count"]:
            label = f"{missing_docs}案欠件"
            if attention_count:
                label = f"{attention_count}案需補正/退件／{label}"
        if action_pending:
            label += f"／{action_pending}案待回報"
        if branch_pending:
            label += f"／{branch_pending}案分會審核"
        if review_pending:
            label += f"／{review_pending}項確認"
        report_item = {
            "state": "attention",
            "label": label or "需處理",
            "count": report_failures["count"] + attention_count,
            "pending": action_pending,
            "review_pending": review_pending,
            "pending_items": [
                item for item in closing_items
                if item.get("approval_status") not in {"待補件", "退件", "補件中暫存"}
            ],
            "review_items": review_items,
            "attention": attention_count,
            "attention_items": attention_items,
            "branch_pending": branch_pending,
            "branch_pending_items": branch_pending_items,
        }
    elif operations:
        labels = []
        if action_pending:
            labels.append(f"{action_pending}案回報")
        if branch_pending:
            labels.append(f"{branch_pending}案分會審核")
        if review_pending:
            labels.append(f"{review_pending}項確認")
        report_item = {
            "state": "waiting" if action_pending or review_pending else "ok",
            "label": "／".join(labels) if labels else "無待處理",
            "count": action_pending + review_pending,
            "pending": action_pending,
            "review_pending": review_pending,
            "pending_items": closing_items,
            "review_items": review_items,
            "attention": 0,
            "attention_items": [],
            "branch_pending": branch_pending,
            "branch_pending_items": branch_pending_items,
        }
    else:
        report_item = {"state": "waiting", "label": "資料庫待確認", "count": 0}

    portal = _load_json(_mutable_static_dir(root, env) / "laf_portal_new_files_latest.json")
    missing_files = int(portal.get("portal_still_missing") or 0)
    portal_scan_failed = (
        portal.get("ok") is False
        or str(portal.get("status") or "") == "portal_scan_failed"
    )
    portal_scan_deferred = (
        portal.get("deferred") is True
        and portal.get("retryable") is True
        and portal.get("action_required") is False
        and str(portal.get("reason") or "").strip()
        in {"case_inventory_unavailable", "portal_listing_unavailable"}
        and str(portal.get("last_successful_status") or "").strip().lower()
        in {"", "ok", "idle", "downloaded", "mapping_unverified"}
        and missing_files == 0
    )
    retry = _load_json(_agent_dir(root, env) / "laf_pending_portal_downloads.json")
    retry_items = retry.get("items") if isinstance(retry.get("items"), list) else []
    pending_retry = sum(1 for item in retry_items if str(item.get("status") or "pending_retry") in {"", "pending_retry"})
    manual_retry = sum(
        1
        for item in retry_items
        if str(item.get("status") or "") in {"manual_review", "exhausted"}
    )
    laf_retry_details = _laf_retry_details(retry_items)
    laf_missing_details = _laf_missing_details(portal)
    laf_mapping_details = _laf_mapping_details(portal)
    mapping_unverified_cases = int(
        portal.get("portal_mapping_unverified_cases") or len(laf_mapping_details)
    )
    mapping_unverified_files = int(
        portal.get("portal_mapping_unverified_files")
        or sum(int(item.get("file_count") or 0) for item in laf_mapping_details)
    )
    if portal_scan_deferred:
        laf_item = {
            "state": "waiting",
            "label": "附件巡檢待重試（案件清單來源暫不可用）",
            "missing": 0,
            "pending_retry": pending_retry,
            "manual_review": 0,
            "retry_items": laf_retry_details,
            "missing_items": [],
            "mapping_unverified": mapping_unverified_cases,
            "mapping_unverified_files": mapping_unverified_files,
            "mapping_items": laf_mapping_details,
            "scan_deferred": True,
            "scan_reason": _business_item_text(
                portal.get("message") or portal.get("reason") or "deferred",
                160,
            ),
        }
    elif portal_scan_failed:
        laf_item = {
            "state": "attention",
            "label": "入口檢查失敗",
            "missing": missing_files,
            "pending_retry": pending_retry,
            "manual_review": manual_retry,
            "scan_error": _business_item_text(
                portal.get("error") or portal.get("message") or "portal_scan_failed",
                160,
            ),
            "retry_items": laf_retry_details,
            "missing_items": laf_missing_details,
            "mapping_unverified": mapping_unverified_cases,
            "mapping_unverified_files": mapping_unverified_files,
            "mapping_items": laf_mapping_details,
        }
    elif missing_files or manual_retry:
        laf_item = {
            "state": "attention",
            "label": f"{missing_files}份欠檔" if missing_files else f"{manual_retry}案人工確認",
            "missing": missing_files,
            "pending_retry": pending_retry,
            "manual_review": manual_retry,
            "retry_items": laf_retry_details,
            "missing_items": laf_missing_details,
            "mapping_unverified": mapping_unverified_cases,
            "mapping_unverified_files": mapping_unverified_files,
            "mapping_items": laf_mapping_details,
        }
    elif mapping_unverified_cases:
        labels = [f"{mapping_unverified_cases}案 NAS映射待驗證"]
        if pending_retry:
            labels.append(f"{pending_retry}案重試中")
        laf_item = {
            "state": "waiting",
            "label": "／".join(labels),
            "missing": 0,
            "pending_retry": pending_retry,
            "manual_review": 0,
            "mapping_unverified": mapping_unverified_cases,
            "mapping_unverified_files": mapping_unverified_files,
            "retry_items": laf_retry_details,
            "missing_items": [],
            "mapping_items": laf_mapping_details,
        }
    elif pending_retry:
        laf_item = {
            "state": "waiting",
            "label": f"{pending_retry}案重試中",
            "missing": 0,
            "pending_retry": pending_retry,
            "manual_review": 0,
            "retry_items": laf_retry_details,
            "missing_items": [],
            "mapping_unverified": 0,
            "mapping_unverified_files": 0,
            "mapping_items": [],
        }
    else:
        laf_item = {"state": "ok", "label": "附件齊全", "missing": 0, "pending_retry": 0, "manual_review": 0, "mapping_unverified": 0, "mapping_unverified_files": 0, "retry_items": [], "missing_items": [], "mapping_items": []}

    review = _load_json(_mutable_static_dir(root, env) / "file_review_auto_state.json")
    review_result = review.get("result") if isinstance(review.get("result"), dict) else {}
    review_job = _latest_file_review_job(root, env)
    review_ready_items = _review_ready_items(review_result)
    review_parsed = ((review_result.get("check") or {}).get("parsed") or {})
    unattended_mode = _truthy(env.get("MAGI_FILE_REVIEW_UNATTENDED_MODE", "1"))
    auto_download = unattended_mode or _truthy(env.get("MAGI_FILE_REVIEW_AUTO_DOWNLOAD"))
    try:
        review_interval_sec = max(120, int(env.get("MAGI_FILE_REVIEW_AUTO_INTERVAL_SEC") or 600))
    except (TypeError, ValueError):
        review_interval_sec = 600
    if unattended_mode:
        review_interval_sec = min(review_interval_sec, 600)
    review_phase = str(review.get("phase") or "").strip().lower()
    review_pid = int(review.get("pid") or 0)
    review_updated_at = _parse_time(review.get("updated_at"))
    review_cycle_active = bool(
        review_phase
        in {
            "cycle_started",
            "draining_payment_proof_queue",
            "running_check_emails",
            "running_scheduled_check",
        }
        and review_pid > 0
        and _pid_alive(review_pid)
        and review_updated_at is not None
        and 0 <= (now - review_updated_at).total_seconds() <= max(300, review_interval_sec * 2)
    )
    scheduled_download = _scheduled_file_review_download_enabled(root)
    review_job_status = str(review_job.get("status") or "").lower()
    review_job_failed = bool(review_job) and (
        review_job_status in {"failed", "error"}
        or (review_job.get("success") is False and review_job_status not in {"stopped", "cancelled"})
    )
    review_failure_fields = _file_review_failure_fields(review_result, review_job)
    review_download = (
        review_result.get("download")
        if isinstance(review_result.get("download"), dict)
        else {}
    )
    mismatch_deferred_raw = review_download.get(
        "mismatch_deferred_portal_signature_hashes"
    )
    mismatch_deferred = (
        len(mismatch_deferred_raw)
        if (
            type(mismatch_deferred_raw) is list
            and mismatch_deferred_raw
            == normalize_signature_hashes(mismatch_deferred_raw)
            and review_download.get("deferred") is True
            and str(review_download.get("reason") or "").strip()
            == "court_payload_identity_mismatch"
            and review_download.get("download_reconciliation_verified") is True
        )
        else 0
    )
    if review_job_failed:
        review_item = {
            "state": "attention",
            "label": "下載工作失敗",
            "auto_download": auto_download,
            "ready_items": review_ready_items,
            **review_failure_fields,
        }
    elif review_result and not bool(review_result.get("ok", True)):
        review_item = {
            "state": "attention",
            "label": "上輪失敗",
            "auto_download": auto_download,
            "ready_items": review_ready_items,
            **review_failure_fields,
        }
    elif (
        review_cycle_active
        and str(review_parsed.get("portal_status_semantics") or "")
        != "ola-current-state-v2"
    ):
        # A controlled release handoff can leave a terminal observation in
        # ``result`` while the replacement worker is already producing its
        # first authoritative portal snapshot.  That bounded, live cycle is
        # an operational wait, not evidence that portal semantics regressed.
        review_item = {
            "state": "waiting",
            "label": "法院狀態更新中",
            "auto_download": auto_download,
            "scan_phase": review_phase,
            "ready_items": review_ready_items,
        }
    elif review_result and str(review_parsed.get("portal_status_semantics") or "") != "ola-current-state-v2":
        review_item = {"state": "attention", "label": "法院狀態判讀未驗證", "auto_download": auto_download, "ready_items": review_ready_items}
    elif not auto_download and scheduled_download:
        review_item = {
            "state": "ok",
            "label": "每小時排程下載",
            "auto_download": False,
            "scheduled_download": True,
            "ready_items": review_ready_items,
        }
    elif not auto_download:
        review_item = {"state": "attention", "label": "僅掃描未下載", "auto_download": False, "ready_items": review_ready_items}
    elif str(review_result.get("reason") or "") == "auto_download_disabled":
        review_item = {"state": "waiting", "label": "已啟用待首輪", "auto_download": True, "ready_items": review_ready_items}
    else:
        ready = int(review_parsed.get("ready_to_download_count") or 0)
        pending_payment = int(review_parsed.get("portal_pending_payment_count") or 0)
        review_item = {
            "state": "waiting" if ready or pending_payment else "ok",
            "label": (
                f"{mismatch_deferred}件法院資料待更新"
                if mismatch_deferred
                else f"{pending_payment}件繳費待處理"
                if pending_payment
                else f"{ready}件待下載"
                if ready
                else f"自動下載正常・每{max(1, review_interval_sec // 60)}分鐘"
            ),
            "auto_download": True,
            "scan_interval_sec": review_interval_sec,
            "portal_verified": bool(review_result.get("portal_verified")),
            "ready_to_download": ready,
            "pending_payment": pending_payment,
            "court_payload_waiting": mismatch_deferred,
            "ready_items": review_ready_items,
        }

    # This status describes the Judicial Yuan transcript-download portal, not
    # audio-to-text inference.  Older snapshots coupled it to MLX Whisper and
    # therefore made an ordinary portal sweep look like a user-requested audio
    # transcription job (and even failed the portal status when no ASR engine
    # was installed).  Keep the legacy arguments in the public function
    # signature for callers, but deliberately do not use them here.
    transcript_sync_path = _runtime_dir(root, env) / "transcript_sync" / "transcript_sync_latest.json"
    transcript_sync = _load_json(transcript_sync_path)
    transcript_status = transcript_sync.get("sync_status") if isinstance(transcript_sync.get("sync_status"), dict) else {}
    transcript_summary = transcript_sync.get("summary") if isinstance(transcript_sync.get("summary"), dict) else {}
    transcript_created = _parse_time(transcript_sync.get("created_at"))
    transcript_full = _parse_time(transcript_status.get("last_cycle_completed_at"))
    transcript_eligible = int(transcript_status.get("eligible_cases") or transcript_sync.get("eligible_cases") or 0)
    transcript_scanned = int(transcript_status.get("cycle_scanned_cases") or 0)
    transcript_remaining = max(0, transcript_eligible - transcript_scanned)
    transcript_retry = int(transcript_summary.get("retry_pending_cases_count") or 0)
    transcript_failed = int(transcript_summary.get("failed_cases_count") or 0)
    transcript_stale = transcript_created is None or now - transcript_created > timedelta(hours=18)
    transcript_cycle_stale = transcript_full is None or now - transcript_full > timedelta(hours=36)
    transcript_base = {
        "kind": "judicial_transcript_download",
        "eligible_cases": transcript_eligible,
        "cycle_scanned_cases": transcript_scanned,
        "remaining_cases": transcript_remaining,
        "retry_pending_cases": transcript_retry,
        "failed_cases": transcript_failed,
    }
    if transcript_sync.get("ok") is not True or transcript_status.get("success") is not True or transcript_failed:
        transcript_item = {**transcript_base, "state": "attention", "label": f"同步失敗 {transcript_failed}案"}
    elif transcript_stale or transcript_cycle_stale:
        transcript_item = {**transcript_base, "state": "attention", "label": "完整輪巡逾時"}
    elif transcript_remaining or transcript_retry:
        suffix = f"・{transcript_retry}案自動復核" if transcript_retry else ""
        transcript_item = {
            **transcript_base,
            "state": "ok",
            "label": f"背景輪巡中 {transcript_scanned}/{transcript_eligible}{suffix}",
        }
    else:
        transcript_item = {**transcript_base, "state": "ok", "label": "筆錄下載正常"}

    runtime_root = _runtime_dir(root, env)
    heavy_live_path = runtime_root / "heavy_fallback_live_latest.json"
    heavy_live = _load_json(heavy_live_path)
    heavy_usage = _latest_successful_nvidia_usage(runtime_root / "nvidia_nim_usage.jsonl")
    heavy_enabled = _truthy(env.get("NVIDIA_NIM_ENABLE"))
    heavy_model = str(env.get("NVIDIA_NIM_MODEL") or "").strip()
    heavy_checked = _parse_time(heavy_usage.get("ts"))
    if heavy_checked is None and heavy_live:
        heavy_checked = datetime.fromtimestamp(heavy_live_path.stat().st_mtime)
    heavy_recent = bool(heavy_checked and now - heavy_checked <= timedelta(hours=24))
    if not heavy_enabled:
        # NVIDIA NIM is an opt-in cloud-heavy accelerator.  Disabling that
        # optional route must not turn an otherwise healthy, locally served
        # MAGI installation red.  Once explicitly enabled, however, a missing
        # model remains a real configuration failure and is surfaced below.
        heavy_item = {
            "state": "ok",
            "label": "選配未啟用",
            "model": heavy_model,
            "enabled": False,
        }
    elif not heavy_model:
        heavy_item = {
            "state": "attention",
            "label": "設定不完整",
            "model": heavy_model,
            "enabled": True,
        }
    elif (heavy_usage.get("ok") is True or heavy_live.get("success") is True) and heavy_recent:
        heavy_item = {
            "state": "ok",
            "label": "NVIDIA 120B",
            "model": heavy_model,
            "enabled": True,
        }
    else:
        heavy_item = {
            "state": "waiting",
            "label": "等待LIVE驗證",
            "model": heavy_model,
            "enabled": True,
        }

    items = {
        "案件回報": report_item,
        "法扶附件": laf_item,
        "閱卷下載": review_item,
        "筆錄下載": transcript_item,
        "NVIDIA重型": heavy_item,
    }
    attention = sum(1 for item in items.values() if item.get("state") == "attention")
    waiting = sum(1 for item in items.values() if item.get("state") == "waiting")
    state = "attention" if attention else ("waiting" if waiting else "ok")
    return {
        "ok": attention == 0,
        "state": state,
        "generated_at": now.isoformat(timespec="seconds"),
        "summary": {"attention": attention, "waiting": waiting, "ok": len(items) - attention - waiting},
        "items": items,
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="建立 MAGI 業務就緒快照")
    parser.add_argument(
        "--json-out",
        default=str(_mutable_static_dir(ROOT) / "business_readiness_latest.json"),
    )
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except Exception:
        pass
    payload = build_snapshot(root=ROOT)
    _write_json(Path(args.json_out), payload)
    # The file is a readiness snapshot and may legitimately contain business
    # attention items.  Stdout is the cron execution contract: producing that
    # snapshot succeeded even when the snapshot itself reports pending work.
    print(json.dumps({
        "ok": True,
        "snapshot_state": payload.get("state"),
        "summary": payload.get("summary", {}),
        "json_out": str(Path(args.json_out)),
    }, ensure_ascii=False, indent=2))
    # Business blockers are carried in the payload; generating the snapshot is
    # still a successful cron run and must not masquerade as scheduler failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
