#!/usr/bin/env python3
"""Health/LIVE checks for MAGI business modules.

The checks are intentionally non-destructive:
- LAF logs in and scans portal draft/list state without submitting forms.
- File review runs self_test and the portal downloadable probe.
- Transcript runs self_test and DB probe; full sync remains on its own cron.
"""

from __future__ import annotations

import json
import os
import sys
import argparse
import re
import plistlib
import html
import hashlib
from datetime import datetime
from pathlib import Path
from collections import defaultdict
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_REPO_ROOT = REPO_ROOT
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.platforms import safe_process
DEFAULT_LIVE_RUNTIME_ROOT = Path("/Users/ai/Library/Application Support/MAGI/runtime/MAGI_v2")
PYTHON = os.environ.get("MAGI_SKILL_PYTHON") or str(REPO_ROOT / "venv" / "bin" / "python3")
if not Path(PYTHON).exists():
    PYTHON = sys.executable
DRIVE_SYNC_STATUS_SLA_HOURS = 24.0
CALENDAR_TODO_STATUS_SLA_HOURS = 24.0
DEFAULT_LIVE_REPORT = Path(".runtime/business_module_live_check_latest.json")

_ACTIVE_SCAN_DIRS = ("api", "casper_ecosystem", "scripts", "skills")
_SOURCE_SKIP_PARTS = {".git", ".pytest_cache", "__pycache__", "venv", "node_modules", "_bg_jobs"}
_HIGH_RISK_ROUTES = {
    "/line/webhook",
    "/telegram/webhook",
    "/webhook/external",
    "/skills/run",
    "/jobs/<job_id>",
    "/api/osc/files/upload",
    "/api/osc/files/upload-multi",
    "/api/osc/files/upload-chunked",
    "/api/osc/files/share",
}
_DEPRECATED_AUTO_DISPATCH_ALIASES = {
    "pdf-annotator": {"pdf_annotate", "pdf_annotator", "run_pdf_annotator"},
}
_AUTO_DISPATCH_FILES = (
    "api/pipelines/skill_dispatch.py",
    "api/pipelines/message_pipeline.py",
    "api/pipelines/message_router.py",
    "skills/bridge/semantic_router.py",
    "skills/bridge/embedding_router.py",
    "skills/definitions.json",
)
_LIVE_ROOT_FINGERPRINT_FILES = (
    "api/server.py",
    "api/discord_bot.py",
    "api/tools_api.py",
    "api/blueprints/admin_runtime.py",
    "api/pipelines/command_dispatch.py",
    "scripts/ops/magi_acceptance_gate.py",
    "scripts/ops/business_module_live_check.py",
    "scripts/ops/run_after_token_refresh.py",
    "scripts/laf_nightly_audit.py",
    "scripts/ops/laf_gmail_dispatch_scan.py",
    "scripts/ops/laf_portal_new_files_scan.py",
    "casper_ecosystem/law_firm_orchestrators/laf_automation_v2.py",
    "casper_ecosystem/law_firm_orchestrators/laf_orchestrator.py",
    "casper_ecosystem/law_firm_orchestrators/laf_orchestrator_docmixins.py",
    "casper_ecosystem/law_firm_orchestrators/file_review_automation.py",
    "skills/laf-orchestrator/action.py",
    "skills/file-review-orchestrator/action.py",
    "skills/ops/file_review_auto_worker.py",
    "skills/transcript-downloader/action.py",
    "skills/transcript-indexer/action.py",
    "config/test_matrix.json",
)
_LIVE_ROOT_GOOGLE_CRON_JOBS = {
    "job_accounting_sheet_import",
    "job_accounting_monthly_bonus",
    "job_drive_case_sync_bidirectional",
    "job_drive_case_sync_all_files",
    "job_osc_events_refresh",
    "job_osc_todo_governance",
    "job_api_token_health_check",
}
_LIVE_ROOT_BUSINESS_CRON_JOBS = {
    "job_laf_pending_scan",
    "job_laf_gmail_dispatch_scan",
    "job_laf_nightly_audit",
    "job_laf_portal_new_files_scan",
    "job_laf_condition_dedup_scan",
    "job_laf_condition_draft",
    "job_file_review_check",
    "job_file_review_downloadable_probe_dense",
    "job_file_review_staging_cleanup",
    "job_transcript_sync",
    "job_transcript_indexer",
    "job_transcript_self_test",
    "job_business_module_live_check",
}
_LIVE_ROOT_CRON_JOBS = _LIVE_ROOT_GOOGLE_CRON_JOBS | _LIVE_ROOT_BUSINESS_CRON_JOBS


_REDACT_KEYS = {
    "applicant",
    "case_number",
    "client_name",
    "court_case_no",
    "court_case_number",
    "email",
    "folder_path",
    "items",
    "local_path",
    "party",
    "path",
    "phone",
    "recipient",
    "row_text",
    "sample",
    "name",
    "token",
}
_REDACT_PATTERNS = (
    (re.compile(r"\b20\d{2}-\d{4,}\b"), "<CASE_ID>"),
    (re.compile(r"\b1\d{2}年度[^\\s,，。；;\"']{1,28}?字第\d{1,8}號"), "<COURT_CASE_NO>"),
    (re.compile(r"\b09\d{2}[- ]?\d{3}[- ]?\d{3}\b"), "<PHONE>"),
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "<EMAIL>"),
    (re.compile(r"(?i)(token|password|secret|api[_-]?key)[\"':= ]+[^\\s,，。；;\"']+"), r"\1=<REDACTED>"),
    (re.compile(r"(/Users/[^\\s,，。；;\"']+|/Volumes/[^\\s,，。；;\"']+)"), "<PATH>"),
)


def _redact_text(text: Any) -> str:
    out = str(text or "")
    for pattern, replacement in _REDACT_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _redact_obj(value: Any, *, key: str = "", preserve_result_names: bool = False) -> Any:
    key_lower = str(key or "").lower()
    if key_lower == "name" and preserve_result_names:
        return str(value or "")
    if any(marker in key_lower for marker in _REDACT_KEYS):
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if key_lower == "sample" and isinstance(value, list):
            return f"<REDACTED:{len(value)} item(s)>"
        return "<REDACTED>"
    if isinstance(value, dict):
        preserve_name_here = bool(preserve_result_names)
        return {
            k: _redact_obj(
                v,
                key=str(k),
                preserve_result_names=preserve_name_here and str(k).lower() == "name",
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        preserve_children = key_lower == "results"
        return [
            _redact_obj(item, key=key, preserve_result_names=preserve_children)
            for item in value
        ]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _run(name: str, argv: list[str], timeout: int = 600) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("MAGI_NO_DELETE", "1")
    env.setdefault("MAGI_PREFER_LOCAL_DB", "1")
    try:
        proc = safe_process.run(
            argv,
            cwd=str(REPO_ROOT),
            env_extra=env,
            timeout_sec=timeout,
        )
    except Exception as e:
        cleanup_failed = bool(getattr(e, "safe_process_cleanup_failed", False))
        return {
            "name": name,
            "ok": False,
            "error": "process_cleanup_failed" if cleanup_failed else f"{type(e).__name__}: {e}",
            "process_cleanup_failed": cleanup_failed,
        }

    if proc.timed_out:
        return {
            "name": name,
            "ok": False,
            "error": f"timeout_{timeout}s",
            "timed_out": True,
            "stdout_tail": _redact_text(proc.stdout or "")[-1200:],
            "stderr_tail": _redact_text(proc.stderr or "")[-1200:],
        }

    parsed = _redact_obj(_parse_last_json(proc.stdout or ""))
    ok = proc.returncode == 0
    contract_error = ""
    if not isinstance(parsed, dict):
        ok = False
        contract_error = "missing_json_object_contract"
    elif "success" not in parsed and "ok" not in parsed:
        ok = False
        contract_error = "missing_success_or_ok_contract"
    else:
        contract_value = parsed.get("success", parsed.get("ok"))
        if type(contract_value) is not bool:
            ok = False
            contract_error = "non_boolean_success_or_ok_contract"
        else:
            ok = ok and contract_value
    return {
        "name": name,
        "ok": bool(ok),
        "returncode": proc.returncode,
        "parsed": parsed,
        "contract_error": contract_error,
        "stdout_tail": _redact_text(proc.stdout or "")[-1600:],
        "stderr_tail": _redact_text(proc.stderr or "")[-1600:],
    }


def _parse_last_json(text: str) -> Any:
    decoder = json.JSONDecoder()
    candidates = [idx for idx, ch in enumerate(text or "") if ch == "{"]
    for idx in reversed(candidates):
        try:
            obj, end = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        if not str(text[idx + end :]).strip():
            return obj
    return None


def _load_json_file(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _pid_alive(pid: int) -> bool:
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _age_seconds(path: Path) -> float | None:
    try:
        return max(0.0, datetime.now().timestamp() - path.stat().st_mtime)
    except Exception:
        return None


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _semantic_cron_job(job: dict[str, Any]) -> dict[str, Any]:
    command = str(job.get("command") or "")
    return {
        "id": str(job.get("id") or ""),
        "enabled": bool(job.get("enabled", True)),
        "cron": str(job.get("cron") or ""),
        "scripts": sorted(_command_script_keys(command)),
        "token_refresh_gate": "scripts/ops/run_after_token_refresh.py" in command,
    }


def _cron_semantic_map(root: Path) -> dict[str, dict[str, Any]]:
    jobs = _load_json_file(root / "cron_jobs.json", [])
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(jobs, list):
        return out
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "")
        if job_id:
            out[job_id] = _semantic_cron_job(job)
    return out


def _live_runtime_root_live() -> dict[str, Any]:
    runtime_root = Path(os.environ.get("MAGI_LIVE_RUNTIME_ROOT") or DEFAULT_LIVE_RUNTIME_ROOT).expanduser()
    if not runtime_root.exists():
        return {
            "name": "live_runtime_root_fingerprint",
            "ok": True,
            "skipped": True,
            "parsed": {"reason": "live_runtime_root_missing", "runtime_root": str(runtime_root)},
        }
    if runtime_root.resolve() == REPO_ROOT.resolve():
        return {
            "name": "live_runtime_root_fingerprint",
            "ok": True,
            "parsed": {"runtime_root": str(runtime_root), "same_root": True},
        }

    file_mismatches = []
    missing = []
    for rel in _LIVE_ROOT_FINGERPRINT_FILES:
        src = REPO_ROOT / rel
        live = runtime_root / rel
        if not src.exists() or not live.exists():
            missing.append({"file": rel, "source_exists": src.exists(), "runtime_exists": live.exists()})
            continue
        src_hash = _sha256_file(src)
        live_hash = _sha256_file(live)
        if src_hash != live_hash:
            file_mismatches.append({"file": rel, "source": src_hash[:12], "runtime": live_hash[:12]})

    source_cron = _cron_semantic_map(REPO_ROOT)
    runtime_cron = _cron_semantic_map(runtime_root)
    cron_mismatches = []
    for job_id in sorted(_LIVE_ROOT_CRON_JOBS):
        source_job = source_cron.get(job_id)
        runtime_job = runtime_cron.get(job_id)
        if source_job != runtime_job:
            cron_mismatches.append({"id": job_id, "source": source_job or {}, "runtime": runtime_job or {}})

    ok = not file_mismatches and not missing and not cron_mismatches
    return {
        "name": "live_runtime_root_fingerprint",
        "ok": ok,
        "parsed": {
            "source_root": str(REPO_ROOT),
            "runtime_root": str(runtime_root),
            "file_mismatches": file_mismatches,
            "missing": missing,
            "cron_mismatches": cron_mismatches,
        },
    }


def _token_health_live() -> dict[str, Any]:
    return _run(
        "token_health_refresh",
        [
            PYTHON,
            str(REPO_ROOT / "scripts" / "ops" / "token_health_check.py"),
            "--refresh",
            "--threshold-days",
            "7",
            "--json-out",
            str(REPO_ROOT / ".runtime" / "token_health" / "business_module_token_health_latest.json"),
        ],
        timeout=240,
    )


def _nas_mounts_live() -> dict[str, Any]:
    try:
        from api import nas_mount_guard

        shares = nas_mount_guard.get_configured_shares(refresh=True)
        detail = {name: nas_mount_guard.get_share_status(name, volume) for name, volume in shares}
        ok = bool(detail) and all(bool(item.get("available") or item.get("mounted")) for item in detail.values())
        return {
            "name": "nas_mounts_live",
            "ok": ok,
            "parsed": {
                "shares": {
                    name: {
                        "available": bool(item.get("available")),
                        "mounted": bool(item.get("mounted")),
                        "mode": item.get("mode") or "",
                    }
                    for name, item in detail.items()
                }
            },
        }
    except Exception as exc:
        return {"name": "nas_mounts_live", "ok": False, "error": _redact_text(f"{type(exc).__name__}: {exc}")}


def _runtime_status_file(*parts: str) -> Path:
    """Return the live-runtime status file when cron writes outside the source checkout."""
    rel = Path(".runtime", *parts)
    live_root = Path(os.environ.get("MAGI_LIVE_RUNTIME_ROOT") or DEFAULT_LIVE_RUNTIME_ROOT).expanduser()
    live_path = live_root / rel
    source_path = REPO_ROOT / rel
    try:
        if REPO_ROOT.resolve() != SOURCE_REPO_ROOT.resolve() and source_path.parent.exists():
            return source_path
        if (
            source_path.exists()
            and live_path.exists()
            and source_path.stat().st_mtime >= live_path.stat().st_mtime
        ):
            return source_path
        if live_root.exists() and live_root.resolve() != REPO_ROOT.resolve() and live_path.exists():
            return live_path
    except Exception:
        if source_path.exists():
            return source_path
        if live_path.exists():
            return live_path
    return source_path


def _drive_sync_next_action(reasons: list[str], *, max_age_hours: float) -> str:
    if "missing_drive_sync_status" in reasons:
        return "Run scripts/drive_case_sync_worker.py once or check cron job_drive_case_sync_bidirectional/all_files."
    if "running_without_live_pid" in reasons:
        return "Inspect the Drive sync worker pid/lock, then rerun scripts/drive_case_sync_worker.py after clearing stale state."
    if "stale_status" in reasons:
        return f"Drive sync status is older than the {max_age_hours:g}h SLA; rerun scripts/drive_case_sync_worker.py and verify cron."
    if "missing_ok_contract" in reasons:
        return "Inspect the latest Drive sync worker status JSON/logs; worker must write ok/success or show an active live pid."
    return ""


def _calendar_todo_next_action(reasons: list[str], *, max_age_hours: float) -> str:
    if "missing_osc_events_refresh_status" in reasons:
        return "Run scripts/ops/osc_events_refresh.py once or check cron job_osc_events_refresh."
    if "stale_status" in reasons:
        return f"Calendar/todo refresh status is older than the {max_age_hours:g}h SLA; rerun scripts/ops/osc_events_refresh.py and verify cron."
    if "calendar_audit_failed" in reasons or "calendar_import_failed" in reasons:
        return "Inspect .runtime/osc_events_refresh_latest.json, then rerun scripts/ops/osc_events_refresh.py after fixing the reported calendar issue."
    return ""


def _iso_age_seconds(value: Any) -> float | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None
    try:
        now = datetime.now(parsed.tzinfo) if parsed.tzinfo else datetime.now()
        return max(0.0, (now - parsed).total_seconds())
    except Exception:
        return None


def _drive_status_age_seconds(path: Path, payload: dict[str, Any]) -> float | None:
    return _iso_age_seconds(payload.get("finished_at") or payload.get("updated_at") or payload.get("heartbeat_at")) or _age_seconds(path)


def _drive_status_candidates(path: Path, data: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    drive_dir = path.parent

    def _add(source: str, payload: Any, payload_path: Path, summary: Any = None) -> None:
        if not isinstance(payload, dict) or not payload:
            return
        key = (
            source,
            str(payload.get("worker_kind") or ""),
            str(payload.get("status") or ""),
            str(payload.get("finished_at") or payload.get("started_at") or payload.get("pid") or ""),
        )
        if key in seen:
            return
        seen.add(key)
        candidates.append(
            {
                "source": source,
                "path": payload_path,
                "payload": payload,
                "summary": summary if isinstance(summary, dict) else payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
                "age_seconds": _drive_status_age_seconds(payload_path, payload),
            }
        )

    _add("latest", data, path, data.get("summary"))
    by_kind = data.get("status_by_kind")
    if isinstance(by_kind, dict):
        for kind, payload in by_kind.items():
            _add(f"latest.status_by_kind.{kind}", payload, path, data.get("summary"))

    for kind in ("priority", "all_files", "inventory"):
        kind_path = drive_dir / f"drive_case_sync_worker_status_{kind}_latest.json"
        kind_data = _load_json_file(kind_path, {}) if kind_path.exists() else {}
        _add(f"kind_file.{kind}", kind_data, kind_path, kind_data.get("summary") if isinstance(kind_data, dict) else {})

    state_path = drive_dir / "worker_state.json"
    state = _load_json_file(state_path, {}) if state_path.exists() else {}
    if isinstance(state, dict):
        _add("worker_state.last_status", state.get("last_status"), state_path, state.get("last_summary"))
        state_by_kind = state.get("status_by_kind")
        if isinstance(state_by_kind, dict):
            for kind, payload in state_by_kind.items():
                _add(f"worker_state.status_by_kind.{kind}", payload, state_path, state.get("last_summary"))
    return candidates


def _drive_status_eval(candidate: dict[str, Any], *, max_age_hours: float) -> dict[str, Any]:
    payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
    status = str(payload.get("status") or "")
    pid = int(payload.get("pid") or 0)
    pid_alive = _pid_alive(pid) if pid else False
    age = candidate.get("age_seconds")
    is_running = "running" in status
    active_running = is_running and pid_alive
    stale_age = age is not None and age > max_age_hours * 3600 and not active_running
    running_without_pid = is_running and (not pid or not pid_alive)
    contract_ok = bool(payload.get("ok")) or bool(payload.get("success")) or active_running
    action_required = bool(payload.get("action_required"))
    blocking_status = status.strip().lower() in {"auth_required", "partial_failure", "timeout", "interrupted", "failed", "error"}
    return {
        "status": status,
        "worker_kind": payload.get("worker_kind") or "",
        "pid": pid,
        "pid_alive": pid_alive,
        "age_seconds": age,
        "stale_age": stale_age,
        "active_running": active_running,
        "running_without_pid": running_without_pid,
        "action_required": action_required,
        "blocking_status": blocking_status,
        "contract_ok": contract_ok,
        "healthy": contract_ok and not action_required and not blocking_status and not stale_age and not running_without_pid,
    }


def _drive_status_kind_map(evaluated: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for candidate, evald in evaluated:
        payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
        kind = str(evald.get("worker_kind") or payload.get("worker_kind") or "latest").strip() or "latest"
        current = out.get(kind)
        age = evald.get("age_seconds")
        if current is not None:
            current_age = current.get("age_seconds")
            if current_age is not None and age is not None and float(current_age) <= float(age):
                continue
        out[kind] = {
            "status": evald.get("status") or "",
            "ok": bool(evald.get("healthy")),
            "pid": evald.get("pid") or 0,
            "pid_alive": bool(evald.get("pid_alive")),
            "age_seconds": round(float(age), 0) if age is not None else None,
            "active_running": bool(evald.get("active_running")),
            "running_without_pid": bool(evald.get("running_without_pid")),
            "action_required": bool(evald.get("action_required")),
            "blocking_status": bool(evald.get("blocking_status")),
            "source": candidate.get("source") or "",
        }
    return out


def _enabled_drive_sync_worker_kinds(root: Path = REPO_ROOT) -> set[str]:
    jobs = _load_json_file(root / "cron_jobs.json", [])
    if not isinstance(jobs, list):
        return {"priority", "all_files", "inventory"}
    kinds: set[str] = set()
    saw_drive_job = False
    for job in jobs:
        if not isinstance(job, dict) or not job.get("enabled", True):
            continue
        command = html.unescape(str(job.get("command") or ""))
        if "scripts/drive_case_sync_inventory.py" in command:
            saw_drive_job = True
            kinds.add("inventory")
        if "scripts/drive_case_sync_worker.py" not in command:
            continue
        saw_drive_job = True
        if "--direct-all-cases" in command:
            kinds.add("all_files")
        elif "--no-direct-priority-sync" in command:
            kinds.add("inventory")
        else:
            kinds.add("priority")
    if not saw_drive_job:
        return {"priority", "all_files", "inventory"}
    return kinds


def _inactive_drive_kinds(status_by_kind: dict[str, Any], active_kinds: set[str]) -> list[str]:
    return sorted(
        str(kind)
        for kind in status_by_kind
        if str(kind) != "latest" and str(kind) not in active_kinds
    )


def _drive_blocking_kinds(status_by_kind: dict[str, Any], *, active_kinds: set[str] | None = None) -> list[str]:
    active = active_kinds or {"priority", "all_files", "inventory"}
    concrete_kinds = {str(kind) for kind in status_by_kind if str(kind) != "latest"}
    return sorted(
        str(kind)
        for kind, payload in status_by_kind.items()
        if isinstance(payload, dict)
        and not (str(kind) == "latest" and concrete_kinds)
        and (str(kind) == "latest" or str(kind) in active)
        and (
            payload.get("ok") is False
            or bool(payload.get("action_required"))
            or bool(payload.get("blocking_status"))
        )
    )


def _drive_sync_status_live(max_age_hours: float = DRIVE_SYNC_STATUS_SLA_HOURS) -> dict[str, Any]:
    path = _runtime_status_file("drive_sync", "drive_case_sync_worker_status_latest.json")
    data = _load_json_file(path, {})
    if not isinstance(data, dict) or not data:
        candidates = _drive_status_candidates(path, {})
        if not candidates:
            reasons = ["missing_drive_sync_status"]
            return {
                "name": "drive_sync_status_live",
                "ok": False,
                "error": "missing_drive_sync_status",
                "parsed": {
                    "sla_hours": max_age_hours,
                    "reason": ",".join(reasons),
                    "next_action": _drive_sync_next_action(reasons, max_age_hours=max_age_hours),
                },
            }
        data = {}
    else:
        candidates = _drive_status_candidates(path, data)
    evaluated = [(candidate, _drive_status_eval(candidate, max_age_hours=max_age_hours)) for candidate in candidates]
    status_by_kind = _drive_status_kind_map(evaluated)
    active_kinds = _enabled_drive_sync_worker_kinds(REPO_ROOT)
    inactive_kinds = _inactive_drive_kinds(status_by_kind, active_kinds)
    latest_eval = _drive_status_eval({"payload": data, "age_seconds": _drive_status_age_seconds(path, data)}, max_age_hours=max_age_hours)
    selected_pair = next((pair for pair in sorted(evaluated, key=lambda pair: pair[1]["age_seconds"] if pair[1]["age_seconds"] is not None else 10**12) if pair[1]["healthy"]), None)
    selected_candidate = selected_pair[0] if selected_pair else {"payload": data, "summary": data.get("summary") or {}, "source": "latest"}
    selected_eval = selected_pair[1] if selected_pair else latest_eval
    reasons = []
    if latest_eval["running_without_pid"]:
        reasons.append("running_without_live_pid")
    elif selected_pair is None and not latest_eval["contract_ok"]:
        reasons.append("missing_ok_contract")
    if selected_pair is None and latest_eval["stale_age"]:
        reasons.append("stale_status")
    blocking_kinds = _drive_blocking_kinds(status_by_kind, active_kinds=active_kinds)
    ok = bool(selected_pair) and not latest_eval["running_without_pid"] and not blocking_kinds
    summary = selected_candidate.get("summary") if isinstance(selected_candidate.get("summary"), dict) else {}
    return {
        "name": "drive_sync_status_live",
        "ok": ok,
        "parsed": {
            "status": selected_eval["status"],
            "worker_kind": selected_eval["worker_kind"],
            "pid": selected_eval["pid"],
            "pid_alive": selected_eval["pid_alive"],
            "age_hours": round((selected_eval["age_seconds"] or 0) / 3600, 2) if selected_eval["age_seconds"] is not None else None,
            "sla_hours": max_age_hours,
            "matched_case_folders": summary.get("matched_case_folders"),
            "active_running": selected_eval["active_running"],
            "running_without_pid": latest_eval["running_without_pid"],
            "selected_source": selected_candidate.get("source") or "",
            "latest_status": latest_eval["status"],
            "latest_worker_kind": latest_eval["worker_kind"],
            "status_by_kind": status_by_kind,
            "blocking_kinds": blocking_kinds,
            "active_kinds": sorted(active_kinds),
            "inactive_kinds": inactive_kinds,
            "reason": ",".join(reasons),
            "next_action": _drive_sync_next_action(reasons, max_age_hours=max_age_hours),
        },
    }


def _calendar_todo_status_live(max_age_hours: float = CALENDAR_TODO_STATUS_SLA_HOURS) -> dict[str, Any]:
    path = _runtime_status_file("osc_events_refresh_latest.json")
    data = _load_json_file(path, {})
    if not isinstance(data, dict) or not data:
        reasons = ["missing_osc_events_refresh_status"]
        return {
            "name": "calendar_todo_status_live",
            "ok": False,
            "error": "missing_osc_events_refresh_status",
            "parsed": {
                "sla_hours": max_age_hours,
                "reason": ",".join(reasons),
                "next_action": _calendar_todo_next_action(reasons, max_age_hours=max_age_hours),
            },
        }
    age = _age_seconds(path)
    audit = data.get("calendar_audit") if isinstance(data.get("calendar_audit"), dict) else {}
    imported = data.get("calendar_import") if isinstance(data.get("calendar_import"), dict) else {}
    audit_ok = bool(audit.get("ok", True))
    import_ok = bool(imported.get("ok", True))
    stale_age = age is not None and age > max_age_hours * 3600
    reasons = []
    if not audit_ok:
        reasons.append("calendar_audit_failed")
    if not import_ok:
        reasons.append("calendar_import_failed")
    if stale_age:
        reasons.append("stale_status")
    ok = audit_ok and import_ok and not stale_age
    return {
        "name": "calendar_todo_status_live",
        "ok": ok,
        "parsed": {
            "age_hours": round((age or 0) / 3600, 2) if age is not None else None,
            "sla_hours": max_age_hours,
            "calendar_audit_ok": audit_ok,
            "calendar_import_ok": import_ok,
            "checked_primary_events": ((audit.get("summary") or {}).get("checked_primary_events")),
            "checked_source_events": ((audit.get("summary") or {}).get("checked_source_events")),
            "imported": imported.get("imported"),
            "skipped": imported.get("skipped"),
            "reason": ",".join(reasons),
            "next_action": _calendar_todo_next_action(reasons, max_age_hours=max_age_hours),
        },
    }


def _iter_source_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirname in _ACTIVE_SCAN_DIRS:
        base = root / dirname
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".json"}:
                continue
            rel = path.relative_to(root)
            if any(part in _SOURCE_SKIP_PARTS for part in rel.parts):
                continue
            out.append(path)
    return out


def _normalize_skill_name(name: str) -> str:
    return re.sub(r"[-_\s]+", "-", str(name or "").strip().lower())


def _parse_skill_frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8", errors="replace")
    meta: dict[str, Any] = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for raw in parts[1].splitlines():
                if ":" not in raw:
                    continue
                key, value = raw.split(":", 1)
                key = key.strip()
                value = value.strip().strip("'\"")
                if value.lower() in {"true", "false"}:
                    meta[key] = value.lower() == "true"
                elif key:
                    meta[key] = value
    if "deprecated: true" in text.lower():
        meta["deprecated"] = True
    if "alias_of:" in text:
        match = re.search(r"alias_of:\s*([A-Za-z0-9_.-]+)", text)
        if match:
            meta["alias_of"] = match.group(1)
    if "type: internal-alias" in text:
        meta["type"] = "internal-alias"
    if "shim" in text.lower() and "alias" in text.lower():
        meta.setdefault("shim_alias", True)
    return meta


def _skill_entries(root: Path) -> list[dict[str, Any]]:
    skills_dir = root / "skills"
    entries: list[dict[str, Any]] = []
    if not skills_dir.exists():
        return entries
    for entry in sorted(skills_dir.iterdir(), key=lambda p: p.name):
        skill_md = entry / "SKILL.md"
        if not entry.is_dir() or not skill_md.exists():
            continue
        meta = _parse_skill_frontmatter(skill_md)
        skill_name = str(meta.get("name") or entry.name)
        rel = entry.relative_to(root).as_posix()
        entries.append(
            {
                "dir": entry.name,
                "name": skill_name,
                "normalized": _normalize_skill_name(skill_name or entry.name),
                "path": rel,
                "deprecated": bool(meta.get("deprecated")) or "[deprecated]" in skill_md.read_text(encoding="utf-8", errors="replace").lower(),
                "alias_of": str(meta.get("alias_of") or ""),
                "type": str(meta.get("type") or ""),
                "shim_alias": bool(meta.get("shim_alias")),
            }
        )
    return entries


def _is_skill_alias(entry: dict[str, Any]) -> bool:
    return bool(entry.get("alias_of")) or entry.get("type") == "internal-alias" or bool(entry.get("shim_alias"))


def _audit_duplicate_skills(root: Path) -> dict[str, Any]:
    by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for entry in _skill_entries(root):
        by_name[str(entry["normalized"])].append(entry)
    duplicates = []
    allowed_aliases = []
    for normalized, grouped in sorted(by_name.items()):
        if len(grouped) <= 1:
            continue
        if any(_is_skill_alias(item) for item in grouped):
            allowed_aliases.append({"normalized": normalized, "skills": grouped})
            continue
        duplicates.append({"normalized": normalized, "skills": grouped})
    return {
        "ok": not duplicates,
        "duplicate_count": len(duplicates),
        "duplicates": duplicates,
        "allowed_alias_count": len(allowed_aliases),
        "allowed_aliases": allowed_aliases,
    }


def _module_to_rel(module: str) -> str:
    return module.replace(".", "/") + ".py"


def _audit_deprecated_auto_dispatch(root: Path) -> dict[str, Any]:
    truth = _load_json_file(root / "config" / "single_source_of_truth.json", {})
    features = truth.get("features") if isinstance(truth, dict) else {}
    legacy_hits: list[dict[str, Any]] = []
    legacy_patterns: list[tuple[str, str, str]] = []
    if isinstance(features, dict):
        for feature, spec in features.items():
            if not isinstance(spec, dict):
                continue
            for legacy in spec.get("legacy_modules") or []:
                legacy = str(legacy)
                legacy_patterns.extend(
                    [
                        (str(feature), legacy, f"import {legacy}"),
                        (str(feature), legacy, f"from {legacy} import"),
                    ]
                )
            for pattern in spec.get("forbidden_imports") or []:
                legacy_patterns.append((str(feature), "forbidden_import", str(pattern)))

    dispatch_scan_files = [
        root / rel
        for rel in _AUTO_DISPATCH_FILES
        if (root / rel).exists()
    ]
    for path in dispatch_scan_files:
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        for feature, legacy, pattern in legacy_patterns:
            if pattern not in text:
                continue
            if legacy != "forbidden_import" and rel == _module_to_rel(legacy):
                continue
            legacy_hits.append(
                {
                    "feature": feature,
                    "legacy_module": legacy,
                    "pattern": pattern,
                    "file": rel,
                }
            )

    deprecated_skills = [entry for entry in _skill_entries(root) if entry.get("deprecated")]
    deprecated_auto_routes: list[dict[str, Any]] = []
    for entry in deprecated_skills:
        aliases = {
            str(entry.get("dir") or "").replace("-", "_"),
            str(entry.get("name") or "").replace("-", "_"),
            f"run_{str(entry.get('dir') or '').replace('-', '_')}",
        }
        aliases.update(_DEPRECATED_AUTO_DISPATCH_ALIASES.get(str(entry.get("dir") or ""), set()))
        aliases = {a for a in aliases if a}
        for rel in _AUTO_DISPATCH_FILES:
            path = root / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for alias in sorted(aliases):
                if re.search(rf"['\"]{re.escape(alias)}['\"]", text):
                    deprecated_auto_routes.append(
                        {
                            "skill": entry.get("dir"),
                            "alias": alias,
                            "file": rel,
                            "severity": "warning",
                            "reason": "deprecated skill is still reachable from semantic/auto dispatch metadata",
                        }
                    )

    return {
        "ok": not legacy_hits,
        "legacy_hit_count": len(legacy_hits),
        "legacy_hits": legacy_hits,
        "deprecated_auto_route_count": len(deprecated_auto_routes),
        "deprecated_auto_routes": deprecated_auto_routes,
    }


_SCRIPT_RE = re.compile(
    r"(?:^|[\s'\"/])"
    r"((?:api|config|scripts|skills)/[^'\"\s]+?\.(?:py|sh))"
)


def _command_script_keys(command: str) -> set[str]:
    text = html.unescape(str(command or ""))
    return {match.group(1) for match in _SCRIPT_RE.finditer(text)}


def _launchd_is_continuous(data: dict[str, Any]) -> bool:
    return bool(data.get("KeepAlive")) or "StartInterval" in data or "StartCalendarInterval" in data


def _audit_cron_dual_executor(root: Path) -> dict[str, Any]:
    cron_jobs = _load_json_file(root / "cron_jobs.json", [])
    cron_scripts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if isinstance(cron_jobs, list):
        for job in cron_jobs:
            if not isinstance(job, dict) or not job.get("enabled", True):
                continue
            for key in _command_script_keys(str(job.get("command") or "")):
                cron_scripts[key].append({"id": job.get("id"), "cron": job.get("cron"), "desc": job.get("desc")})

    launchd_scripts: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for base in (root / "config" / "launchagents", root / "config" / "launchdaemons"):
        if not base.exists():
            continue
        for path in sorted(base.glob("*.plist")):
            try:
                data = plistlib.loads(path.read_bytes())
            except Exception:
                continue
            if not isinstance(data, dict) or not _launchd_is_continuous(data):
                continue
            args = data.get("ProgramArguments") or []
            command = " ".join(str(part) for part in args) if isinstance(args, list) else str(args)
            for key in _command_script_keys(command):
                launchd_scripts[key].append(
                    {
                        "label": data.get("Label") or path.stem,
                        "plist": path.relative_to(root).as_posix(),
                    }
                )

    conflicts = []
    for key in sorted(set(cron_scripts) & set(launchd_scripts)):
        conflicts.append({"script": key, "cron_jobs": cron_scripts[key], "launchd_jobs": launchd_scripts[key]})
    return {
        "ok": not conflicts,
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "cron_script_count": len(cron_scripts),
        "launchd_script_count": len(launchd_scripts),
    }


_ROUTE_RE = re.compile(r"@[\w.]+\.route\(\s*f?[\"']([^\"']+)[\"'](?P<args>[^)]*)\)")
_METHODS_RE = re.compile(r"methods\s*=\s*\[([^\]]+)\]")


def _route_methods(args: str) -> set[str]:
    match = _METHODS_RE.search(args or "")
    if not match:
        return {"GET"}
    methods = re.findall(r"['\"]([A-Z]+)['\"]", match.group(1))
    return set(methods or ["GET"])


def _is_high_risk_route(route: str) -> bool:
    return route in _HIGH_RISK_ROUTES or "webhook" in route.lower()


def _audit_high_risk_endpoint_collisions(root: Path) -> dict[str, Any]:
    routes: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for dirname in ("api", "skills"):
        base = root / dirname
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(root)
            if any(part in _SOURCE_SKIP_PARTS for part in rel.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in _ROUTE_RE.finditer(text):
                route = match.group(1)
                if not _is_high_risk_route(route):
                    continue
                line = text.count("\n", 0, match.start()) + 1
                for method in _route_methods(match.group("args")):
                    routes[(route, method)].append({"file": rel.as_posix(), "line": line})
    collisions = []
    for (route, method), hits in sorted(routes.items()):
        files = sorted({hit["file"] for hit in hits})
        if len(files) > 1:
            collisions.append({"route": route, "method": method, "handlers": hits})
    return {
        "ok": not collisions,
        "collision_count": len(collisions),
        "collisions": collisions,
        "scanned_route_count": len(routes),
    }


def live_validation_commands(py: str | None = None) -> dict[str, list[str]]:
    py = py or PYTHON
    return {
        "production_live": [
            py,
            "scripts/ops/run_test_suite.py",
            "--suite",
            "production-live",
            "--json-out",
            ".runtime/production_live_latest.json",
        ],
        "business_modules": [
            py,
            "scripts/ops/business_module_live_check.py",
            "--json",
            "--json-out",
            str(DEFAULT_LIVE_REPORT),
        ],
        "conflict_audit": [
            py,
            "scripts/ops/business_module_live_check.py",
            "--conflict-audit",
            "--json-out",
            ".runtime/live_conflict_audit_latest.json",
        ],
        "manual_probe": [
            "curl",
            "-fsS",
            "http://127.0.0.1:${MAGI_SERVER_PORT:-5002}/health",
        ],
    }


def audit_live_conflicts(root: Path = REPO_ROOT, *, strict: bool = False) -> dict[str, Any]:
    checks = {
        "duplicate_skills": _audit_duplicate_skills(root),
        "deprecated_auto_dispatch": _audit_deprecated_auto_dispatch(root),
        "cron_dual_executor": _audit_cron_dual_executor(root),
        "high_risk_endpoint_collision": _audit_high_risk_endpoint_collisions(root),
    }
    error_count = sum(
        int(checks[name].get(key) or 0)
        for name, key in (
            ("duplicate_skills", "duplicate_count"),
            ("deprecated_auto_dispatch", "legacy_hit_count"),
            ("cron_dual_executor", "conflict_count"),
            ("high_risk_endpoint_collision", "collision_count"),
        )
    )
    warning_count = int(checks["deprecated_auto_dispatch"].get("deprecated_auto_route_count") or 0)
    ok = error_count == 0 and (warning_count == 0 if strict else True)
    return {
        "ok": ok,
        "success": ok,
        "strict": strict,
        "error_count": error_count,
        "warning_count": warning_count,
        "checks": checks,
        "commands": live_validation_commands(),
    }


def _laf_portal_live() -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        audit = sys.modules.get("scripts.laf_nightly_audit")
        if audit is None:
            import scripts.laf_nightly_audit as audit

        result = audit.scan_portal_pending_drafts(db=None, read_only=True)
        error = _redact_text(result.get("error") or "")
        return {
            "name": "laf_portal_live",
            "ok": not bool(error),
            "parsed": {
                "error": error or None,
                "closing_drafts": len(result.get("closing_drafts") or []),
                "case_status_drafts": len(result.get("case_status_drafts") or []),
                "condition_pending": len(result.get("condition_pending") or []),
                "go_live_pending": len(result.get("go_live_pending") or []),
                "progress_pending": len(result.get("progress_pending") or []),
            },
        }
    except Exception as e:
        return {"name": "laf_portal_live", "ok": False, "error": _redact_text(f"{type(e).__name__}: {e}")}


def _laf_closing_transfer_notice_live() -> dict[str, Any]:
    sample_body = """
    範例律師您好：(本郵件是由系統自動寄出，請勿直接回覆此郵件)
    您自律師線上操作系統回報之下列資料，分會業已轉入本會系統！
    ※律師姓名：範例律師
    ※身分證字號：A123456789
    ※申請編號：1140715-A-024
    ※受扶助人姓名：測試受扶助人
    ※回報類型：問題回報 - 結案
    ※派案分會承辦人：測試承辦人 電話：02-23225151 Email：caseworker@example.test
    請注意！目前您的回報已發生回報效力！
    """

    class _FakeDB:
        def execute(self, sql: str, params: tuple[Any, ...] = (), fetch: str | None = None):
            if "FROM `cases`" in sql and fetch == "one":
                return {
                    "id": 80,
                    "case_number": "2025-0080",
                    "client_name": "測試受扶助人",
                    "status": "結案中",
                    "legal_aid_status": "已結案，待送出",
                    "legal_aid_approval_status": "暫存",
                    "manual_status_lock": 0,
                    "legal_aid_number": "1140715-A-024",
                }
            return None

    try:
        from api.laf_closing_transfer import apply_laf_closing_transfer_notice, parse_laf_closing_transfer_notice

        notice = parse_laf_closing_transfer_notice("法扶結案轉入通知", sample_body)
        if not notice:
            return {"name": "laf_closing_transfer_notice", "ok": False, "error": "sample_notice_not_parsed"}
        result = apply_laf_closing_transfer_notice(_FakeDB(), notice, source_message_id="live-check", dry_run=True)
        ok = (
            notice.laf_case_number == "1140715-A-024"
            and notice.client_name == "測試受扶助人"
            and result.get("status") == "would_update"
            and result.get("case_number") == "2025-0080"
        )
        return {
            "name": "laf_closing_transfer_notice",
            "ok": bool(ok),
            "parsed": {
                "laf_case_number": notice.laf_case_number,
                "client_name": notice.client_name,
                "dry_run_status": result.get("status"),
                "target_case_status": "已結案",
            },
        }
    except Exception as e:
        return {"name": "laf_closing_transfer_notice", "ok": False, "error": _redact_text(f"{type(e).__name__}: {e}")}


def _summarize(results: list[dict[str, Any]]) -> str:
    lines = [f"📋 業務三模組 LIVE/健康檢查 — {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    for r in results:
        mark = "✅" if r.get("ok") else "❌"
        detail = ""
        parsed = r.get("parsed")
        if isinstance(parsed, dict):
            if "downloadable_count" in parsed:
                detail = f"可下載 {parsed.get('downloadable_count')} / 待繳費 {parsed.get('pending_payment_count')}"
            elif "eligible_cases" in parsed:
                detail = f"可同步案件 {parsed.get('eligible_cases')}"
            elif isinstance(parsed.get("summary"), dict) and "failures" in parsed["summary"]:
                summary = parsed["summary"]
                detail = f"checks {summary.get('total')} / failures {summary.get('failures')}"
            elif "failures" in parsed and "total" in parsed:
                detail = f"checks {parsed.get('total')} / failures {parsed.get('failures')}"
            elif "shares" in parsed:
                detail = " / ".join(
                    f"{k}:{'OK' if v.get('available') or v.get('mounted') else 'NG'}"
                    for k, v in (parsed.get("shares") or {}).items()
                )
            elif "matched_case_folders" in parsed:
                detail = (
                    f"{parsed.get('status')} / matched {parsed.get('matched_case_folders')} / "
                    f"age {parsed.get('age_hours')}h"
                )
            elif "calendar_audit_ok" in parsed:
                detail = (
                    f"audit {parsed.get('calendar_audit_ok')} / import {parsed.get('calendar_import_ok')} / "
                    f"age {parsed.get('age_hours')}h"
                )
            elif "case_status_drafts" in parsed:
                detail = (
                    f"案件狀態暫存 {parsed.get('case_status_drafts')} / "
                    f"二階段 {parsed.get('condition_pending')} / 開辦 {parsed.get('go_live_pending')}"
                )
            elif "dry_run_status" in parsed:
                detail = f"{parsed.get('dry_run_status')} -> {parsed.get('target_case_status')}"
            elif parsed.get("errors"):
                detail = str(parsed.get("errors"))[:120]
            if not r.get("ok") and parsed.get("next_action"):
                action = str(parsed.get("next_action"))[:180]
                detail = f"{detail} / next: {action}" if detail else f"next: {action}"
        if not detail and r.get("error"):
            detail = str(r.get("error"))[:120]
        lines.append(f"{mark} {r.get('name')}: {detail}".rstrip())
    return "\n".join(lines)


def _notify(text: str) -> dict[str, Any]:
    enabled = str(os.environ.get("MAGI_BUSINESS_LIVE_CHECK_NOTIFY", "0")).lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return {"requested": False, "ok": True, "delivery": "not_requested", "queued": False}
    try:
        from skills.ops.red_phone import send_telegram_push_with_status

        response = send_telegram_push_with_status(
            text,
            severity="warning",
            source="business_module_live_check",
            topic_key="check",
        )
        if isinstance(response, dict):
            queued_value = response.get("queued", False)
            queued = queued_value if type(queued_value) is bool else False
            delivery_value = response.get("ok", response.get("sent", queued))
            delivered = delivery_value if type(delivery_value) is bool else False
            delivery = str(response.get("delivery") or ("queued" if queued else ("sent" if delivered else "failed")))
            return {
                "requested": True,
                "ok": delivered or queued,
                "delivery": delivery,
                "queued": queued,
            }
        delivered = bool(response)
        return {
            "requested": True,
            "ok": delivered,
            "delivery": "sent" if delivered else "failed",
            "queued": False,
        }
    except Exception as exc:
        return {
            "requested": True,
            "ok": False,
            "delivery": "failed",
            "queued": False,
            "error": _redact_text(f"{type(exc).__name__}: {exc}"),
        }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run non-destructive MAGI business module LIVE/health checks.")
    parser.add_argument("--json", action="store_true", help="Compatibility flag; output is JSON by default.")
    parser.add_argument("--json-out", help="Write JSON report to this path.")
    parser.add_argument("--conflict-audit", action="store_true", help="Run only the fast live conflict audit.")
    parser.add_argument("--strict-conflicts", action="store_true", help="Treat conflict-audit warnings as failures.")
    parser.add_argument("--print-live-commands", action="store_true", help="Print live validation commands and exit.")
    parser.add_argument("--skip-conflict-audit", action="store_true", help="Skip the fast local conflict audit in the live check.")
    parser.add_argument("--skip-laf-live", action="store_true", help="Skip live LAF portal login/scan.")
    parser.add_argument("--notify", action="store_true", help="Send the summary through the internal check topic.")
    return parser.parse_args(argv)


def _resolve_report_path(raw: str | None, *, default: Path | None = None) -> Path | None:
    value = raw.strip() if isinstance(raw, str) else ""
    if value:
        out_path = Path(value)
    elif default is not None:
        out_path = default
    else:
        return None
    if not out_path.is_absolute():
        out_path = REPO_ROOT / out_path
    return out_path


def _write_report(path: Path | None, payload: dict[str, Any]) -> str:
    if path is None:
        return ""
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_payload = _redact_obj(payload)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(safe_payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
    return str(path)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.print_live_commands:
        payload = {"ok": True, "success": True, "commands": live_validation_commands()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.conflict_audit:
        payload = audit_live_conflicts(REPO_ROOT, strict=bool(args.strict_conflicts))
        out_path = _resolve_report_path(args.json_out)
        if out_path:
            _write_report(out_path, payload)
            payload["json_out"] = str(out_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok") else 1

    if args.notify:
        os.environ["MAGI_BUSINESS_LIVE_CHECK_NOTIFY"] = "1"

    results = []
    if not args.skip_conflict_audit:
        conflict = audit_live_conflicts(REPO_ROOT, strict=bool(args.strict_conflicts))
        results.append(
            {
                "name": "live_conflict_audit",
                "ok": bool(conflict.get("ok")),
                "parsed": {
                    "errors": conflict.get("error_count"),
                    "warnings": conflict.get("warning_count"),
                    "commands": conflict.get("commands"),
                },
            }
        )
    results.append(_live_runtime_root_live())
    results.extend([
        _token_health_live(),
        _nas_mounts_live(),
        _drive_sync_status_live(),
        _calendar_todo_status_live(),
        _laf_closing_transfer_notice_live(),
    ])
    results.extend([
        _run("laf_self_test", [PYTHON, str(REPO_ROOT / "skills" / "laf-orchestrator" / "action.py"), "--task", "self_test"], timeout=120),
        _run("file_review_self_test", [PYTHON, str(REPO_ROOT / "skills" / "file-review-orchestrator" / "action.py"), "--task", "self_test"], timeout=120),
        _run(
            "file_review_downloadable_probe",
            [
                PYTHON,
                str(REPO_ROOT / "skills" / "file-review-orchestrator" / "action.py"),
                "--task",
                'downloadable_probe {"days":30,"notify":false,"require_portal":true,"read_only":true}',
            ],
            timeout=900,
        ),
        _run("transcript_self_test", [PYTHON, str(REPO_ROOT / "skills" / "transcript-downloader" / "action.py"), "--task", "self_test"], timeout=120),
        _run("transcript_db_probe", [PYTHON, str(REPO_ROOT / "skills" / "transcript-downloader" / "action.py"), "--task", "db_probe"], timeout=180),
    ])
    if args.skip_laf_live:
        results.insert(
            1,
            {
                "name": "laf_portal_live",
                "ok": False,
                "skipped": True,
                "error": "skipped_live_verification",
                "parsed": {"error": "skipped_live_verification"},
            },
        )
    else:
        results.insert(1, _laf_portal_live())
    ok = all(bool(r.get("ok")) for r in results)
    message = _summarize(results)
    notification = _notify(message)
    if notification.get("requested") and not notification.get("ok"):
        results.append(
            {
                "name": "notification_delivery",
                "ok": False,
                "error": str(notification.get("error") or "notification_delivery_failed"),
                "parsed": {
                    "delivery": notification.get("delivery"),
                    "queued": bool(notification.get("queued")),
                },
            }
        )
        ok = False
        message = _summarize(results)
    out = {
        "ok": ok,
        "success": ok,
        "results": results,
        "message": message,
        "commands": live_validation_commands(),
        "notification": notification,
    }
    out_path = _resolve_report_path(args.json_out, default=DEFAULT_LIVE_REPORT)
    if out_path:
        _write_report(out_path, out)
        out["json_out"] = str(out_path)
    print(json.dumps(_redact_obj(out), ensure_ascii=False, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
