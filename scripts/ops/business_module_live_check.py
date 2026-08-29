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
from magi_v3.file_review_receipts import (
    PORTAL_DOWNLOAD_RECEIPT_SCHEMA,
    normalize_signature_hashes,
    portal_observed_epoch,
    portal_snapshot_fingerprint,
    signature_set_hash,
)
DEFAULT_LIVE_RUNTIME_ROOT = (
    Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v3"
)
_RUNTIME_OVERRIDE = os.environ.get("MAGI_RUNTIME_DIR", "").strip()
RUNTIME_DIR = Path(_RUNTIME_OVERRIDE or (REPO_ROOT / ".runtime")).expanduser()
PYTHON = os.environ.get("MAGI_SKILL_PYTHON") or str(REPO_ROOT / "venv" / "bin" / "python3")
if not Path(PYTHON).exists():
    PYTHON = sys.executable
DRIVE_SYNC_STATUS_SLA_HOURS = 24.0
CALENDAR_TODO_STATUS_SLA_HOURS = 24.0
FILE_REVIEW_STATUS_SLA_HOURS = 0.5
LAF_GMAIL_STATUS_SLA_HOURS = 0.75
LAF_PORTAL_STATUS_SLA_HOURS = 8.0
TRANSCRIPT_SYNC_STATUS_SLA_HOURS = 18.0
TRANSCRIPT_FULL_CYCLE_SLA_HOURS = 36.0
NOTIFICATION_STATUS_SLA_HOURS = 0.5
DEFAULT_LIVE_REPORT = (
    RUNTIME_DIR / "business_module_live_check_latest.json"
    if _RUNTIME_OVERRIDE
    else Path(".runtime/business_module_live_check_latest.json")
)
LIVE_ENV_WHITELIST_PREFIXES = (
    "MAGI_",
    "JUDICIAL_",
    "OSC_",
    "NVIDIA_",
    "MYSQL_",
    "DB_",
    "GOOGLE_",
    "GEMINI_",
    "OPENAI_",
    "FINNHUB_",
    "HF_",
    "HUGGINGFACE_",
    "PATH",
    "HOME",
    "USER",
    "PYTHONPATH",
    "PYTHONPYCACHEPREFIX",
    "LANG",
    "LC_",
    "TZ",
)

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
    "config/business_recovery_contracts.json",
    "magi_v3/business_recovery.py",
    "magi_v3/cron_service.py",
    "skills/ops/cron_scheduler.py",
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
_RELEASE_BOUND_ENV = {
    "MAGI_ROOT": "",
    "MAGI_ROOT_DIR": "",
    "MAGI_ORCH_DIR": "casper_ecosystem/law_firm_orchestrators",
    "MAGI_CODE_DIR": "casper_ecosystem/law_firm_orchestrators",
}
_HOST_SINGLETON_LABELS = (
    "com.magi.input-method-watchdog",
    "com.magi.omlx",
    "com.magi.omlx-watchdog",
    "com.magi.rpc",
)


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


def _run(
    name: str,
    argv: list[str],
    timeout: int = 600,
    *,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("MAGI_NO_DELETE", "1")
    env.setdefault("MAGI_PREFER_LOCAL_DB", "1")
    # Launching a file below scripts/ or skills/ makes Python use that file's
    # directory as sys.path[0].  Bind every nested probe to this immutable V3
    # release explicitly so imports cannot depend on an interactive shell's
    # incidental PYTHONPATH.
    inherited_pythonpath = str(env.get("PYTHONPATH") or "").strip()
    pythonpath_parts = [str(REPO_ROOT)]
    if inherited_pythonpath:
        pythonpath_parts.extend(
            part for part in inherited_pythonpath.split(os.pathsep) if part
        )
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(pythonpath_parts))
    env.update(env_overrides or {})
    try:
        proc = safe_process.run(
            argv,
            cwd=str(REPO_ROOT),
            env_extra=env,
            env_whitelist_prefixes=LIVE_ENV_WHITELIST_PREFIXES,
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


def _load_live_environment() -> str:
    """Bind the live check to the same dotenv used by the supervised service."""
    if os.environ.get("MAGI_V3_SCHEDULE_FIXTURE_ROOT"):
        return "bounded_fixture"

    explicit = os.environ.get("MAGI_ENV_FILE", "").strip()
    candidates = [
        ("magi_env_file", Path(explicit).expanduser() if explicit else None),
        ("repo_env", REPO_ROOT / ".env"),
        (
            "live_runtime_env",
            Path(
                os.environ.get("MAGI_LIVE_RUNTIME_ROOT", "").strip()
                or DEFAULT_LIVE_RUNTIME_ROOT
            ).expanduser()
            / ".env",
        ),
    ]
    source = "inherited_only"
    for label, path in candidates:
        if path is None or not path.is_file():
            continue
        try:
            from dotenv import load_dotenv

            load_dotenv(dotenv_path=path, override=False)
            source = label
            break
        except Exception:
            continue
    _bind_release_local_environment()
    return source


def _bind_release_local_environment() -> dict[str, str]:
    """Keep immutable-release checks on their own release after dotenv loading.

    Shared credential files intentionally survive releases.  Historical copies
    may therefore contain compatibility path keys.  Those keys must never make
    a V3 release execute another release's business modules.
    """

    root = REPO_ROOT.resolve()
    if not root.name.startswith("v3-") or not (root / "release-manifest.json").is_file():
        return {}
    bound: dict[str, str] = {}
    for key, relative in _RELEASE_BOUND_ENV.items():
        value = str((root / relative).resolve()) if relative else str(root)
        os.environ[key] = value
        bound[key] = value
    return bound


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
    from magi_v3.external_inputs import load_bound_cron_jobs

    jobs = list(load_bound_cron_jobs(root).jobs)
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


def _recovery_owner_job_ids(root: Path) -> set[str]:
    path = root / "config" / "business_recovery_contracts.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    domains = payload.get("domains") if isinstance(payload, dict) else None
    if not isinstance(domains, dict):
        return set()
    return {
        str(job_id).strip()
        for contract in domains.values()
        if isinstance(contract, dict)
        for job_id in (contract.get("owner_job_ids") or [])
        if str(job_id).strip()
    }


def _business_recovery_contract_live() -> dict[str, Any]:
    try:
        from magi_v3.business_recovery import audit_recovery_catalog
        from magi_v3.external_inputs import load_bound_cron_jobs

        jobs = list(load_bound_cron_jobs(REPO_ROOT).jobs)
        audit = audit_recovery_catalog(
            jobs,
            catalog_path=str(REPO_ROOT / "config" / "business_recovery_contracts.json"),
        )
    except Exception as exc:
        return {
            "name": "business_recovery_contract",
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "name": "business_recovery_contract",
        "ok": bool(audit.get("ok")),
        "parsed": {
            "domain_count": int(audit.get("domain_count") or 0),
            "owner_count": int(audit.get("owner_count") or 0),
            "verifier_count": int(audit.get("verifier_count") or 0),
            "errors": list(audit.get("issues") or []),
        },
    }


def _live_runtime_root_live() -> dict[str, Any]:
    configured_root = os.environ.get("MAGI_LIVE_RUNTIME_ROOT", "").strip()
    if not configured_root and os.environ.get("MAGI_V3_RELEASE_ID", "").strip().startswith("v3-"):
        for key in ("MAGI_ROOT_DIR", "MAGI_ROOT"):
            candidate = os.environ.get(key, "").strip()
            if candidate and "/releases/v3-" in Path(candidate).expanduser().as_posix():
                configured_root = candidate
                break
    runtime_root = Path(configured_root or DEFAULT_LIVE_RUNTIME_ROOT).expanduser()
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
    recovery_jobs = _recovery_owner_job_ids(REPO_ROOT) | _recovery_owner_job_ids(runtime_root)
    for job_id in sorted(_LIVE_ROOT_CRON_JOBS | recovery_jobs):
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


def _host_singleton_release_bindings_live() -> dict[str, Any]:
    """Reject LaunchAgents that still execute an older immutable release."""

    raw_root = os.environ.get("MAGI_LAUNCHAGENTS_DIR", "").strip()
    launchagents = Path(raw_root).expanduser() if raw_root else Path.home() / "Library" / "LaunchAgents"
    active_root = REPO_ROOT.resolve()
    if not active_root.name.startswith("v3-") or not (active_root / "release-manifest.json").is_file():
        return {
            "name": "host_singleton_release_bindings",
            "ok": True,
            "skipped": True,
            "parsed": {"reason": "not_versioned_release"},
        }
    if not launchagents.is_dir():
        return {
            "name": "host_singleton_release_bindings",
            "ok": True,
            "skipped": True,
            "parsed": {"reason": "launchagents_unavailable"},
        }

    active_root_text = active_root.as_posix().rstrip("/")
    expected_prefix = active_root_text + "/"
    drift: list[dict[str, str]] = []

    def iter_strings(value: Any):
        if isinstance(value, dict):
            for child in value.values():
                yield from iter_strings(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield from iter_strings(child)
        elif isinstance(value, str):
            yield value

    inspected = 0
    for label in _HOST_SINGLETON_LABELS:
        path = launchagents / f"{label}.plist"
        if not path.is_file() or path.is_symlink():
            continue
        inspected += 1
        try:
            payload = plistlib.loads(path.read_bytes())
        except Exception as exc:
            drift.append({"label": label, "reason": f"invalid_plist:{type(exc).__name__}"})
            continue
        for value in iter_strings(payload):
            normalized = value.replace("\\", "/")
            marker = "/releases/v3-"
            if marker in normalized and not (
                normalized == active_root_text or normalized.startswith(expected_prefix)
            ):
                release_part = "v3-" + normalized.split(marker, 1)[1].split("/", 1)[0]
                drift.append({"label": label, "reason": "older_release_reference", "release": release_part})
                break
    return {
        "name": "host_singleton_release_bindings",
        "ok": not drift,
        "parsed": {
            "active_release": active_root.name,
            "inspected": inspected,
            "drift": drift,
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
            str(RUNTIME_DIR / "token_health" / "business_module_token_health_latest.json"),
        ],
        timeout=240,
    )


def _nas_mounts_live(provider: Any | None = None) -> dict[str, Any]:
    try:
        if provider is not None:
            detail = provider.nas_share_statuses()
        else:
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
    if _RUNTIME_OVERRIDE:
        return RUNTIME_DIR.joinpath(*parts)
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


def _mutable_static_status_file(*parts: str) -> Path:
    """Resolve mutable public state beside the bound runtime directory."""
    configured = os.environ.get("MAGI_MUTABLE_STATIC_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().joinpath(*parts)
    if _RUNTIME_OVERRIDE:
        return RUNTIME_DIR.parent.joinpath("static", *parts)
    return REPO_ROOT.joinpath("static", *parts)


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
    if any(
        reason in reasons
        for reason in (
            "calendar_audit_failed",
            "calendar_import_failed",
            "calendar_push_failed",
            "calendar_pdf_scan_failed",
            "calendar_source_audit_failed",
        )
    ):
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


def _artifact_age_seconds(path: Path, payload: dict[str, Any], *time_keys: str) -> float | None:
    for key in time_keys:
        age = _iso_age_seconds(payload.get(key))
        if age is not None:
            return age
    return _age_seconds(path)


def _file_review_ingestion_coverage_live(
    max_age_hours: float = FILE_REVIEW_STATUS_SLA_HOURS,
    *,
    portal_probe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = _mutable_static_status_file("file_review_auto_state.json")
    data = _load_json_file(path, {})
    if not isinstance(data, dict) or not data:
        return {
            "name": "file_review_ingestion_coverage_live",
            "ok": False,
            "error": "missing_file_review_auto_state",
            "parsed": {"sla_hours": max_age_hours, "reason": "missing_file_review_auto_state"},
        }
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    check = result.get("check") if isinstance(result.get("check"), dict) else {}
    parsed = check.get("parsed") if isinstance(check.get("parsed"), dict) else {}
    payment = result.get("payment_slips") if isinstance(result.get("payment_slips"), dict) else {}
    download = result.get("download") if isinstance(result.get("download"), dict) else {}
    live_probe = portal_probe if isinstance(portal_probe, dict) else {}
    live_probe_parsed = (
        live_probe.get("parsed") if isinstance(live_probe.get("parsed"), dict) else {}
    )
    live_probe_portal = (
        live_probe_parsed.get("portal")
        if isinstance(live_probe_parsed.get("portal"), dict)
        else {}
    )
    live_portal_verified = bool(
        live_probe.get("ok") is True
        and live_probe_parsed.get("success") is True
        and live_probe_portal.get("success") is True
        and str(live_probe_parsed.get("source") or "").strip().lower() == "portal"
    )
    live_portal_active_pid = int(live_probe_parsed.get("active_pid") or 0)
    live_portal_busy = bool(
        live_probe.get("ok") is True
        and live_probe_parsed.get("success") is True
        and live_probe_parsed.get("deferred") is True
        and str(live_probe_parsed.get("status") or "").strip().lower() == "deferred"
        and str(live_probe_parsed.get("reason") or "").strip().lower()
        == "file_review_portal_busy"
        and live_portal_active_pid > 0
        and _pid_alive(live_portal_active_pid)
    )
    age = _artifact_age_seconds(path, data, "updated_at")
    phase = str(data.get("phase") or "").strip().lower()
    pid = int(data.get("pid") or 0)
    active_phases = {
        "cycle_started",
        "draining_payment_proof_queue",
        "running_check_emails",
        "running_scheduled_check",
    }
    active_running = (
        phase in active_phases
        and pid > 0
        and _pid_alive(pid)
        and age is not None
        and age <= max_age_hours * 3600
    )
    pending_payment = int(parsed.get("portal_pending_payment_count") or 0)
    ready_download = int(parsed.get("ready_to_download_count") or 0)
    live_downloadable = int(live_probe_portal.get("downloadable_count") or 0)
    def _exact_signature_list(value: Any) -> tuple[list[str], bool]:
        normalized = normalize_signature_hashes(value if type(value) is list else [])
        return normalized, bool(type(value) is list and value == normalized)

    live_signatures, live_signatures_exact = _exact_signature_list(
        live_probe_portal.get("portal_download_signature_hashes")
    )
    live_signature_set_hash = str(
        live_probe_portal.get("portal_download_signature_set_hash") or ""
    ).strip().lower()
    live_snapshot_fingerprint = str(
        live_probe_portal.get("portal_probe_snapshot_fingerprint") or ""
    ).strip().lower()
    live_probe_observed_at = str(
        live_probe_portal.get("portal_probe_observed_at") or ""
    ).strip()
    reconciled_expected = int(
        download.get("expected_portal_downloadable_count") or 0
    )
    reconciled_accounted = int(
        download.get("accounted_portal_downloadable_count") or 0
    )
    receipt_expected_signatures, receipt_expected_signatures_exact = _exact_signature_list(
        download.get("expected_portal_signature_hashes")
    )
    receipt_handled_signatures, receipt_handled_signatures_exact = _exact_signature_list(
        download.get("handled_portal_signature_hashes")
    )
    receipt_mismatch_deferred_signatures, receipt_mismatch_deferred_signatures_exact = _exact_signature_list(
        download.get("mismatch_deferred_portal_signature_hashes")
    )
    receipt_accounted_signatures = normalize_signature_hashes(
        [
            *receipt_handled_signatures,
            *receipt_mismatch_deferred_signatures,
        ]
    )
    receipt_expected_set_hash = str(
        download.get("expected_portal_signature_set_hash") or ""
    ).strip().lower()
    receipt_handled_set_hash = str(
        download.get("handled_portal_signature_set_hash") or ""
    ).strip().lower()
    receipt_mismatch_deferred_set_hash = str(
        download.get("mismatch_deferred_portal_signature_set_hash") or ""
    ).strip().lower()
    receipt_accounted_set_hash = str(
        download.get("accounted_portal_signature_set_hash") or ""
    ).strip().lower()
    receipt_snapshot_fingerprint = str(
        download.get("reconciled_probe_snapshot_fingerprint") or ""
    ).strip().lower()
    receipt_probe_observed_at = str(
        download.get("reconciled_probe_observed_at") or ""
    ).strip()
    live_probe_epoch = portal_observed_epoch(live_probe_observed_at)
    receipt_probe_epoch = portal_observed_epoch(receipt_probe_observed_at)
    observation_order_valid = bool(
        live_probe_epoch is not None
        and receipt_probe_epoch is not None
        and receipt_probe_epoch <= live_probe_epoch + 300
    )
    live_signature_contract = bool(
        live_signatures_exact
        and live_downloadable == len(live_signatures)
        and live_signature_set_hash == signature_set_hash(live_signatures)
        and live_snapshot_fingerprint == portal_snapshot_fingerprint(live_signatures)
        and live_probe_epoch is not None
        and str(live_probe_portal.get("portal_download_receipt_schema") or "")
        == PORTAL_DOWNLOAD_RECEIPT_SCHEMA
    )
    receipt_signature_contract = bool(
        receipt_expected_signatures_exact
        and receipt_handled_signatures_exact
        and receipt_mismatch_deferred_signatures_exact
        and type(download.get("accounted_portal_signature_hashes")) is list
        and download.get("accounted_portal_signature_hashes")
        == receipt_accounted_signatures
        and reconciled_expected == len(receipt_expected_signatures)
        and receipt_expected_set_hash == signature_set_hash(receipt_expected_signatures)
        and receipt_handled_set_hash == signature_set_hash(receipt_handled_signatures)
        and receipt_mismatch_deferred_set_hash
        == signature_set_hash(receipt_mismatch_deferred_signatures)
        and receipt_accounted_set_hash == signature_set_hash(receipt_accounted_signatures)
        and receipt_snapshot_fingerprint
        == portal_snapshot_fingerprint(receipt_expected_signatures)
        and receipt_probe_epoch is not None
        and str(download.get("portal_download_receipt_schema") or "")
        == PORTAL_DOWNLOAD_RECEIPT_SCHEMA
        and set(receipt_mismatch_deferred_signatures).issubset(
            receipt_expected_signatures
        )
        and set(receipt_expected_signatures).issubset(receipt_accounted_signatures)
    )
    mismatch_deferred_verified = bool(
        download.get("deferred") is True
        and str(download.get("reason") or "").strip()
        == "court_payload_identity_mismatch"
        and bool(receipt_mismatch_deferred_signatures)
        and set(receipt_mismatch_deferred_signatures).isdisjoint(
            receipt_handled_signatures
        )
    )
    same_snapshot = bool(
        live_signature_contract
        and receipt_signature_contract
        and observation_order_valid
        and live_snapshot_fingerprint == receipt_snapshot_fingerprint
        and live_signatures == receipt_expected_signatures
    )
    live_subset_accounted = bool(
        live_signature_contract
        and receipt_signature_contract
        and observation_order_valid
        and set(live_signatures).issubset(receipt_accounted_signatures)
    )
    download_verified = (
        bool(result.get("portal_verified"))
        and bool(download.get("success") is True)
        and (
            not bool(download.get("deferred"))
            or mismatch_deferred_verified
        )
        and (
            live_downloadable <= 0
            or (
                bool(download.get("download_reconciliation_verified"))
                and (same_snapshot or live_subset_accounted)
            )
        )
    )
    reasons: list[str] = []
    if age is None or age > max_age_hours * 3600:
        reasons.append("stale_file_review_state")
    if not active_running and not bool(result.get("ok")):
        reasons.append("file_review_worker_failed")
    portal_verified = bool(result.get("portal_verified")) or live_portal_verified
    portal_probe_ok = bool(parsed.get("portal_probe_ok")) or live_portal_verified
    if not active_running and not live_portal_busy and (not portal_verified or not portal_probe_ok):
        reasons.append("portal_not_verified")
    if int(parsed.get("scan_errors") or 0) > 0:
        reasons.append("source_scan_errors")
    if int(parsed.get("recent_unnotified_count") or 0) > 0:
        reasons.append("processed_without_notification")
    if not active_running and parsed.get("portal_status_semantics") != "ola-current-state-v2":
        reasons.append("unverified_portal_status_semantics")
    if pending_payment > 0 and str(payment.get("reason") or "") == "verified_no_pending_payment":
        reasons.append("payment_pipeline_contradiction")
    if ready_download > 0 and str(download.get("reason") or "") == "verified_no_download_signal":
        reasons.append("download_pipeline_contradiction")
    # A read-only LIVE probe can discover files after the worker's preceding
    # cycle was deferred by the same single-owner portal lock.  Do not merge
    # that fresh portal evidence with the older worker snapshot into a false
    # green result: the next worker cycle must actually account for the files
    # (download or verified duplicate) before coverage is healthy.
    if live_downloadable > 0 and not active_running and not download_verified:
        reasons.append("portal_downloads_waiting_worker")
    return {
        "name": "file_review_ingestion_coverage_live",
        "ok": not reasons,
        "parsed": {
            "age_hours": round((age or 0) / 3600, 2) if age is not None else None,
            "sla_hours": max_age_hours,
            "phase": phase,
            "pid": pid,
            "active_running": active_running,
            "portal_verified": portal_verified,
            "portal_verified_by_current_live_probe": live_portal_verified,
            "portal_waiting_on_active_owner": live_portal_busy,
            "portal_active_owner_pid": live_portal_active_pid if live_portal_busy else 0,
            "portal_raw_rows": int(
                result.get("portal_raw_row_count")
                or parsed.get("portal_raw_row_count")
                or live_probe_portal.get("raw_count")
                or 0
            ),
            "portal_cases": int(
                result.get("portal_case_count")
                or parsed.get("portal_case_count")
                or live_probe_portal.get("case_count")
                or 0
            ),
            "pending_payment": pending_payment,
            "ready_download": ready_download,
            "portal_downloadable_current": live_downloadable,
            "download_verified": download_verified,
            "download_reconciled_expected": reconciled_expected,
            "download_reconciled_accounted": reconciled_accounted,
            "download_mismatch_deferred": len(
                receipt_mismatch_deferred_signatures
            ),
            "download_mismatch_deferred_verified": mismatch_deferred_verified,
            "download_signature_contract": receipt_signature_contract,
            "live_signature_contract": live_signature_contract,
            "download_same_snapshot": same_snapshot,
            "live_signature_subset_accounted": live_subset_accounted,
            "download_observation_order_valid": observation_order_valid,
            "recent_unnotified": int(parsed.get("recent_unnotified_count") or 0),
            "status_semantics": str(parsed.get("portal_status_semantics") or ""),
            "reason": ",".join(reasons),
        },
    }


def _laf_ingestion_coverage_live() -> dict[str, Any]:
    monitor_path = _mutable_static_status_file("laf_gmail_monitor_state.json")
    pending_path = _runtime_status_file("laf_gmail_dispatch_pending.json")
    portal_path = _mutable_static_status_file("laf_portal_new_files_latest.json")
    monitor = _load_json_file(monitor_path, {})
    pending = _load_json_file(pending_path, {})
    portal = _load_json_file(portal_path, {})
    reasons: list[str] = []
    portal_deferred = False
    monitor_age = _artifact_age_seconds(monitor_path, monitor, "updated_at") if monitor else None
    pending_age = _artifact_age_seconds(pending_path, pending, "updated_at") if pending else None
    portal_age = _artifact_age_seconds(portal_path, portal, "checked_at", "updated_at") if portal else None
    if not monitor:
        reasons.append("missing_laf_gmail_monitor")
    elif monitor_age is None or monitor_age > LAF_GMAIL_STATUS_SLA_HOURS * 3600:
        reasons.append("stale_laf_gmail_monitor")
    else:
        monitor_status = str(monitor.get("status") or "").lower()
        monitor_starting = monitor_status == "started" and monitor.get("running") is True
        if (
            monitor_status not in {"ok", "running"}
            and not monitor_starting
        ) or int(monitor.get("consecutive_errors") or 0) > 0:
            reasons.append("laf_gmail_monitor_failed")
    if not pending:
        reasons.append("missing_laf_gmail_dispatch_evidence")
    elif pending_age is None or pending_age > LAF_GMAIL_STATUS_SLA_HOURS * 3600:
        reasons.append("stale_laf_gmail_dispatch_evidence")
    elif pending.get("ok") is not True or int(pending.get("failure_count") or 0) > 0:
        reasons.append("laf_gmail_dispatch_failed")
    if not portal:
        reasons.append("missing_laf_portal_attachment_evidence")
    elif portal_age is None or portal_age > LAF_PORTAL_STATUS_SLA_HOURS * 3600:
        reasons.append("stale_laf_portal_attachment_evidence")
    else:
        portal_status = str(portal.get("status") or "").lower()
        portal_deferred = (
            portal.get("deferred") is True
            and portal.get("retryable") is True
            and portal.get("action_required") is False
            and str(portal.get("reason") or "").strip()
            in {"case_inventory_unavailable", "portal_listing_unavailable"}
            and str(portal.get("last_successful_status") or "").strip().lower()
            in {"", "ok", "idle", "downloaded", "mapping_unverified"}
            and int(portal.get("portal_still_missing") or 0) == 0
        )
        portal_mapping_only = (
            portal_status == "mapping_unverified"
            or (
                int(portal.get("portal_still_missing") or 0) == 0
                and int(portal.get("portal_mapping_unverified_cases") or 0) > 0
                and all(
                    str(item.get("reason_code") or "").strip()
                    == "nas_mapping_unverified"
                    for item in (portal.get("portal_new_files") or [])
                    if isinstance(item, dict)
                )
            )
        )
        if (portal.get("ok") is not True and not portal_deferred) or (
            portal_status not in {"ok", "idle", "downloaded"}
            and not portal_mapping_only
            and not portal_deferred
        ):
            reasons.append("laf_portal_attachment_scan_failed")
    return {
        "name": "laf_ingestion_coverage_live",
        "ok": not reasons,
        "parsed": {
            "gmail_monitor_age_minutes": round((monitor_age or 0) / 60, 1) if monitor_age is not None else None,
            "gmail_dispatch_age_minutes": round((pending_age or 0) / 60, 1) if pending_age is not None else None,
            "portal_attachment_age_hours": round((portal_age or 0) / 3600, 2) if portal_age is not None else None,
            "dispatch_pending": int(pending.get("pending_count") or 0),
            "dispatch_failures": int(pending.get("failure_count") or 0),
            "portal_cases_scanned": int(portal.get("scanned_cases") or 0),
            "portal_still_missing": int(portal.get("portal_still_missing") or 0),
            "portal_mapping_unverified_cases": int(
                portal.get("portal_mapping_unverified_cases") or 0
            ),
            "portal_mapping_unverified_files": int(
                portal.get("portal_mapping_unverified_files") or 0
            ),
            "portal_scan_deferred": portal_deferred,
            "portal_last_successful_status": str(
                portal.get("last_successful_status") or ""
            ),
            "reason": ",".join(reasons),
        },
    }


def _transcript_sync_coverage_live(
    max_age_hours: float = TRANSCRIPT_SYNC_STATUS_SLA_HOURS,
    full_cycle_sla_hours: float = TRANSCRIPT_FULL_CYCLE_SLA_HOURS,
) -> dict[str, Any]:
    path = _runtime_status_file("transcript_sync", "transcript_sync_latest.json")
    data = _load_json_file(path, {})
    if not isinstance(data, dict) or not data:
        return {
            "name": "transcript_sync_coverage_live",
            "ok": False,
            "error": "missing_transcript_sync_evidence",
            "parsed": {"reason": "missing_transcript_sync_evidence"},
        }
    sync = data.get("sync_status") if isinstance(data.get("sync_status"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    age = _artifact_age_seconds(path, data, "created_at", "finished_at")
    full_age = _iso_age_seconds(sync.get("last_cycle_completed_at"))
    eligible = int(sync.get("eligible_cases") or data.get("eligible_cases") or 0)
    scanned = int(sync.get("cycle_scanned_cases") or 0)
    remaining = max(0, eligible - scanned)
    configured_agent_dir = str(os.environ.get("MAGI_AGENT_DIR") or "").strip()
    if configured_agent_dir:
        lock_path = Path(configured_agent_dir).expanduser() / "transcript_sync.lock"
    elif _RUNTIME_OVERRIDE:
        lock_path = RUNTIME_DIR.parent / "agent" / "transcript_sync.lock"
    else:
        # Production V3 always supplies MAGI_AGENT_DIR.  Keeping the fallback
        # release-local prevents a developer/test checkout from accidentally
        # reading a real host worker lock and reporting a false running state.
        lock_path = REPO_ROOT / ".agent" / "transcript_sync.lock"
    lock = _load_json_file(lock_path, {})
    lock_pid = int(lock.get("pid") or 0) if isinstance(lock, dict) else 0
    active_running = bool(lock_pid and _pid_alive(lock_pid))
    lock_age = (
        _iso_age_seconds(lock.get("started_at"))
        if active_running and isinstance(lock, dict)
        else None
    )
    reasons: list[str] = []
    if data.get("ok") is not True or sync.get("success") is not True:
        reasons.append("transcript_sync_failed")
    # A long full sweep updates its durable report only after a batch exits.
    # A verified live lock therefore proves freshness while the next batch is
    # still running.  It may suppress age-only warnings, but never a recorded
    # case failure or an unsuccessful previous result.
    if not active_running and (age is None or age > max_age_hours * 3600):
        reasons.append("stale_transcript_sync")
    if int(summary.get("failed_cases_count") or 0) > 0:
        reasons.append("transcript_case_failures")
    if not active_running and (full_age is None or full_age > full_cycle_sla_hours * 3600):
        reasons.append("stale_full_transcript_cycle")
    return {
        "name": "transcript_sync_coverage_live",
        "ok": not reasons,
        "parsed": {
            "age_hours": round((age or 0) / 3600, 2) if age is not None else None,
            "full_cycle_age_hours": round((full_age or 0) / 3600, 2) if full_age is not None else None,
            "full_cycle_sla_hours": full_cycle_sla_hours,
            "eligible_cases": eligible,
            "cycle_scanned_cases": scanned,
            "remaining_cases": remaining,
            "retry_pending_cases": int(summary.get("retry_pending_cases_count") or 0),
            "failed_cases": int(summary.get("failed_cases_count") or 0),
            "active_running": active_running,
            "active_pid": lock_pid if active_running else None,
            "active_lock_age_minutes": (
                round(lock_age / 60, 1) if lock_age is not None else None
            ),
            "coverage_state": (
                "running"
                if active_running
                else ("complete" if remaining == 0 else "in_progress")
            ),
            "reason": ",".join(reasons),
        },
    }


def _notification_delivery_status_live(
    max_age_hours: float = NOTIFICATION_STATUS_SLA_HOURS,
) -> dict[str, Any]:
    path = _runtime_status_file("notification_delivery_health_latest.json")
    data = _load_json_file(path, {})
    if not isinstance(data, dict) or not data:
        return {
            "name": "notification_delivery_status_live",
            "ok": False,
            "error": "missing_notification_delivery_evidence",
            "parsed": {"reason": "missing_notification_delivery_evidence"},
        }
    age = _artifact_age_seconds(path, data, "generated_at", "updated_at")
    try:
        remaining = int(data.get("remaining") or 0)
        manual_hold = int(data.get("manual_hold_pending") or 0)
        auto_retry_pending = int(data.get("auto_retry_pending") or 0)
        oldest = float(data.get("oldest_pending_age_seconds") or 0)
    except (TypeError, ValueError):
        return {
            "name": "notification_delivery_status_live",
            "ok": False,
            "error": "invalid_notification_delivery_evidence",
            "parsed": {"reason": "invalid_notification_delivery_evidence"},
        }
    if min(remaining, manual_hold, auto_retry_pending, oldest) < 0:
        return {
            "name": "notification_delivery_status_live",
            "ok": False,
            "error": "invalid_notification_delivery_evidence",
            "parsed": {"reason": "invalid_notification_delivery_evidence"},
        }
    reasons: list[str] = []
    if data.get("ok") is not True or str(data.get("status") or "").lower() not in {"ok", "idle"}:
        reasons.append("notification_delivery_failed")
    # Delivery evidence is event driven.  A quiet queue does not become a
    # fault merely because no new notification has needed delivery recently;
    # the ingestion coverage checks independently prove the producers are
    # alive.  Freshness is actionable only while something is still pending.
    if (age is None or age > max_age_hours * 3600) and (remaining > 0 or manual_hold > 0):
        reasons.append("stale_notification_delivery_evidence")
    if manual_hold > 0 or (remaining > 0 and oldest > max_age_hours * 3600):
        reasons.append("notification_backlog_requires_attention")
    return {
        "name": "notification_delivery_status_live",
        "ok": not reasons,
        "parsed": {
            "age_minutes": round((age or 0) / 60, 1) if age is not None else None,
            "remaining": remaining,
            "auto_retry_pending": auto_retry_pending,
            "manual_hold_pending": manual_hold,
            "oldest_pending_minutes": round(oldest / 60, 1),
            "reason": ",".join(reasons),
        },
    }


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
    normalized_status = status.strip().lower()
    reason = str(payload.get("reason") or payload.get("defer_reason") or "").strip()
    reporter_pid = int(payload.get("pid") or 0)
    active_worker_pid = int(payload.get("active_worker_pid") or payload.get("active_pid") or 0)
    reporter_pid_alive = _pid_alive(reporter_pid) if reporter_pid else False
    active_worker_pid_alive = _pid_alive(active_worker_pid) if active_worker_pid else False
    # Legacy overlap receipts contain the contender PID in ``pid`` and the
    # real lock owner in ``active_worker_pid``.  Judge the run by the owner;
    # otherwise a contender that exits normally turns a healthy sync into a
    # false red light (running_without_live_pid).
    use_active_owner = normalized_status in {
        "already_running",
        "case_file_operation_already_running",
    } and active_worker_pid_alive
    pid = active_worker_pid if use_active_owner else reporter_pid
    pid_alive = active_worker_pid_alive if use_active_owner else reporter_pid_alive
    age = candidate.get("age_seconds")
    is_running = "running" in status and normalized_status != "stale_running_cleared"
    safe_overlap = (
        normalized_status
        in {"already_running", "case_file_operation_already_running"}
        and not bool(payload.get("action_required"))
    )
    active_running = is_running and pid_alive
    stale_age = age is not None and age > max_age_hours * 3600 and not active_running
    # An overlap contender exits after observing the lock owner.  Once the
    # owner finishes, its historic ``already_running`` receipt naturally has
    # no live PID; that is proof the singleton guard worked, not a crashed
    # worker.  Ordinary running states still require a live PID.
    running_without_pid = is_running and not safe_overlap and (not pid or not pid_alive)
    file_sync = payload.get("file_sync_summary") if isinstance(payload.get("file_sync_summary"), dict) else {}
    execution = payload.get("execution_summary") if isinstance(payload.get("execution_summary"), dict) else {}
    drive_folders = payload.get("drive_folder_summary") if isinstance(payload.get("drive_folder_summary"), dict) else {}
    folder_repair = (
        payload.get("drive_imported_folder_repair")
        if isinstance(payload.get("drive_imported_folder_repair"), dict)
        else {}
    )
    semantic_collisions = int(file_sync.get("semantic_collision_files") or 0)
    actual_failure_count = sum(
        int(value or 0)
        for value in (
            execution.get("download_failed"),
            execution.get("upload_failed"),
            drive_folders.get("failed"),
            folder_repair.get("errors"),
            file_sync.get("incomplete_case_scans"),
        )
    )
    # ``pending_unverified_files`` and ``unverified_existing_files`` describe
    # the same file-plan set, so count that set once rather than turning a
    # single record into two fictitious verification failures.
    pending_sources = (
        execution.get("download_pending_unverified"),
        execution.get("upload_pending_unverified"),
        max(
            int(file_sync.get("pending_unverified_files") or 0),
            int(file_sync.get("unverified_existing_files") or 0),
        ),
    )
    integrity_pending_count = sum(int(value or 0) for value in pending_sources)
    upload_existing_checksum_missing_conflicts = int(
        execution.get("upload_pending_existing_checksum_missing_conflict") or 0
    )
    file_plan_existing_checksum_missing_conflicts = int(
        file_sync.get("pending_existing_checksum_missing_conflict") or 0
    )
    existing_checksum_missing_conflicts = (
        upload_existing_checksum_missing_conflicts
        + file_plan_existing_checksum_missing_conflicts
    )
    # This is a deliberately fail-closed exception: every pending item must be
    # represented by an upload-manifest collision where the existing Drive
    # object omitted its MD5.  We neither overwrite nor delete it; the user
    # still has a data-integrity review to perform.  Any extra pending item is
    # a real blocking verification gap.
    data_integrity_review_wait = (
        normalized_status in {"partial_failure", "deferred"}
        and semantic_collisions > 0
        and existing_checksum_missing_conflicts > 0
        and integrity_pending_count == existing_checksum_missing_conflicts
        and actual_failure_count == 0
    )
    hard_failure_count = actual_failure_count + (
        0 if data_integrity_review_wait else integrity_pending_count
    )
    semantic_collision_wait = (
        normalized_status in {"partial_failure", "deferred"}
        and (
            normalized_status == "partial_failure"
            or reason == "semantic_path_collision_requires_human_review"
        )
        and semantic_collisions > 0
        and hard_failure_count == 0
        and not data_integrity_review_wait
    )
    storage_unavailable_wait = (
        normalized_status == "deferred"
        and reason == "storage_unavailable"
        # Items that could not yet be verified are expected while storage is
        # disconnected.  They remain visible and resumable, while genuine I/O
        # failures or incomplete scans still block health.
        and actual_failure_count == 0
    )
    safe_interruption_wait = (
        normalized_status == "interrupted"
        and payload.get("action_required") is False
        and bool(payload.get("finished_at"))
        and hard_failure_count == 0
    )
    safe_timeout_wait = (
        normalized_status == "timeout"
        and payload.get("action_required") is False
        and bool(payload.get("finished_at"))
        and hard_failure_count == 0
        and payload.get("_scheduler_retry_pending") is True
    )
    safe_chunk_deadline_wait = (
        normalized_status == "chunk_deadline_deferred"
        and payload.get("deferred") is True
        and payload.get("action_required") is False
        and bool(payload.get("finished_at"))
        and payload.get("all_case_offset_before") == payload.get("all_case_offset_after")
        and payload.get("cycle_completed") is False
        and hard_failure_count == 0
        and payload.get("_scheduler_retry_pending") is True
    )
    stale_running_cleared_wait = (
        normalized_status == "stale_running_cleared"
        and payload.get("action_required") is False
        and bool(payload.get("finished_at"))
        and hard_failure_count == 0
    )
    contract_ok = (
        bool(payload.get("ok"))
        or bool(payload.get("success"))
        or active_running
        or data_integrity_review_wait
        or semantic_collision_wait
        or safe_overlap
        or storage_unavailable_wait
        or safe_interruption_wait
        or safe_timeout_wait
        or safe_chunk_deadline_wait
        or stale_running_cleared_wait
    )
    # ``data_integrity_review`` is green for service availability but remains
    # explicitly actionable: an existing object without a checksum was never
    # assumed equal to the local upload.
    action_required = bool(payload.get("action_required")) and not semantic_collision_wait
    blocking_status = (
        (
            normalized_status in {"auth_required", "partial_failure", "timeout", "interrupted", "failed", "error"}
            or (
                hard_failure_count > 0
                and not storage_unavailable_wait
                and not data_integrity_review_wait
            )
        )
        and not semantic_collision_wait
        and not data_integrity_review_wait
        and not safe_interruption_wait
        and not safe_timeout_wait
        and not safe_chunk_deadline_wait
    )
    return {
        "status": status,
        "worker_kind": payload.get("worker_kind") or "",
        "pid": pid,
        "pid_alive": pid_alive,
        "reporter_pid": reporter_pid,
        "reporter_pid_alive": reporter_pid_alive,
        "active_worker_pid": active_worker_pid,
        "active_worker_pid_alive": active_worker_pid_alive,
        "age_seconds": age,
        "stale_age": stale_age,
        "active_running": active_running,
        "running_without_pid": running_without_pid,
        "action_required": action_required,
        "blocking_status": blocking_status,
        "contract_ok": contract_ok,
        "waiting": (
            data_integrity_review_wait
            or semantic_collision_wait
            or safe_overlap
            or storage_unavailable_wait
            or safe_interruption_wait
            or safe_timeout_wait
            or safe_chunk_deadline_wait
            or stale_running_cleared_wait
        ),
        "waiting_reason": (
            "data_integrity_review"
            if data_integrity_review_wait
            else "semantic_path_collision_requires_human_review"
            if semantic_collision_wait
            else "storage_unavailable"
            if storage_unavailable_wait
            else "controlled_interruption_retry_scheduled"
            if safe_interruption_wait
            else "bounded_timeout_retry_scheduled"
            if safe_timeout_wait
            else "chunk_deadline_retry_scheduled"
            if safe_chunk_deadline_wait
            else "stale_running_marker_safely_cleared"
            if stale_running_cleared_wait
            else "singleton_overlap_safely_deferred"
            if safe_overlap
            else ""
        ),
        "semantic_collision_files": semantic_collisions,
        "integrity_pending_count": integrity_pending_count,
        "existing_checksum_missing_conflicts": existing_checksum_missing_conflicts,
        "upload_pending_existing_checksum_missing_conflict": upload_existing_checksum_missing_conflicts,
        "file_plan_pending_existing_checksum_missing_conflict": file_plan_existing_checksum_missing_conflicts,
        "data_integrity_review": data_integrity_review_wait,
        "hard_failure_count": hard_failure_count,
        "healthy": (
            contract_ok
            and (not action_required or data_integrity_review_wait)
            and not blocking_status
            and (not stale_age or stale_running_cleared_wait)
            and not running_without_pid
        ),
    }


def _drive_scheduler_retry_states() -> dict[str, bool]:
    """Return persisted, bounded retry state for each Drive worker kind."""

    state = _load_json_file(RUNTIME_DIR / "cron_state.json", {})
    if not isinstance(state, dict):
        return {}
    job_by_kind = {
        "all_files": "job_drive_case_sync_all_files",
        "priority": "job_drive_case_sync_bidirectional",
        "inventory": "job_drive_case_sync_nightly",
    }
    result: dict[str, bool] = {}
    for kind, job_id in job_by_kind.items():
        row = state.get(job_id) if isinstance(state.get(job_id), dict) else {}
        retry = row.get("v3_retry") if isinstance(row.get("v3_retry"), dict) else {}
        pending = (
            row.get("v3_pending_occurrence")
            if isinstance(row.get("v3_pending_occurrence"), dict)
            else {}
        )
        result[kind] = (
            str(retry.get("status") or "").lower() in {"queued", "running"}
            or str(pending.get("status") or "").lower() in {"queued", "running"}
        )
    return result


def _drive_status_kind_map(evaluated: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for candidate, evald in evaluated:
        payload = candidate.get("payload") if isinstance(candidate.get("payload"), dict) else {}
        kind = str(evald.get("worker_kind") or payload.get("worker_kind") or "latest").strip() or "latest"
        current = out.get(kind)
        age = evald.get("age_seconds")
        if current is not None:
            current_age = current.get("age_seconds")
            # Several status mirrors represent the exact same worker run. Their
            # age measurements can differ by fractions of a second, while the
            # public value is rounded. Keep the first (most detailed) candidate
            # for the same run instead of letting a summary-only worker_state
            # mirror overwrite it because of rounding jitter.
            if (
                current_age is not None
                and age is not None
                and float(current_age) <= float(age) + 1.0
            ):
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
            "waiting": bool(evald.get("waiting")),
            "waiting_reason": str(evald.get("waiting_reason") or ""),
            "semantic_collision_files": int(evald.get("semantic_collision_files") or 0),
            "integrity_pending_count": int(evald.get("integrity_pending_count") or 0),
            "upload_pending_existing_checksum_missing_conflict": int(
                evald.get("upload_pending_existing_checksum_missing_conflict") or 0
            ),
            "existing_checksum_missing_conflicts": int(
                evald.get("existing_checksum_missing_conflicts") or 0
            ),
            "file_plan_pending_existing_checksum_missing_conflict": int(
                evald.get("file_plan_pending_existing_checksum_missing_conflict") or 0
            ),
            "data_integrity_review": bool(evald.get("data_integrity_review")),
            "hard_failure_count": int(evald.get("hard_failure_count") or 0),
            "source": candidate.get("source") or "",
        }
    return out


def _enabled_drive_sync_worker_kinds(root: Path = REPO_ROOT) -> set[str]:
    from magi_v3.external_inputs import load_bound_cron_jobs

    jobs = list(load_bound_cron_jobs(root).jobs)
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
            or (
                bool(payload.get("action_required"))
                and not bool(payload.get("data_integrity_review"))
            )
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
    scheduler_retry = _drive_scheduler_retry_states()
    enriched_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        enriched = dict(candidate)
        payload = dict(candidate.get("payload") or {})
        kind = str(payload.get("worker_kind") or "").strip()
        payload["_scheduler_retry_pending"] = scheduler_retry.get(kind, False)
        enriched["payload"] = payload
        enriched_candidates.append(enriched)
    candidates = enriched_candidates
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
    waiting_kinds = sorted(
        str(kind)
        for kind, payload in status_by_kind.items()
        if isinstance(payload, dict)
        and str(kind) in active_kinds
        and bool(payload.get("waiting"))
    )
    all_file_pairs = [
        pair
        for pair in evaluated
        if str((pair[0].get("payload") or {}).get("worker_kind") or "") == "all_files"
    ]
    all_file_pairs.sort(
        key=lambda pair: pair[1]["age_seconds"] if pair[1]["age_seconds"] is not None else 10**12
    )
    all_files_payload = (
        all_file_pairs[0][0].get("payload")
        if all_file_pairs and isinstance(all_file_pairs[0][0].get("payload"), dict)
        else {}
    )
    sweep_total = int(all_files_payload.get("all_case_total") or 0)
    sweep_after_present = "all_case_offset_after" in all_files_payload
    sweep_before = int(all_files_payload.get("all_case_offset_before") or 0)
    sweep_after = int(all_files_payload.get("all_case_offset_after") or 0)
    sweep_batch = len(all_files_payload.get("all_case_numbers") or [])
    if not sweep_batch and sweep_total:
        sweep_batch = (sweep_after - sweep_before) % sweep_total
    sweep_running = bool(selected_eval["active_running"])
    # Only the worker's terminal receipt can assert a complete global sweep.
    # A zero cursor on a running/timeout/chunk receipt is merely a position,
    # never evidence that all cases were processed.
    sweep_wrapped_complete = bool(
        sweep_total
        and all_files_payload.get("cycle_completed") is True
        and sweep_after_present
        and sweep_after == 0
        and not sweep_running
    )
    sweep_position = sweep_before if sweep_running and not sweep_after_present else sweep_after
    sweep_remaining = (
        0
        if sweep_wrapped_complete
        else max(0, sweep_total - sweep_position) if sweep_total else 0
    )
    estimated_cycles = (
        (sweep_remaining + max(1, sweep_batch) - 1) // max(1, sweep_batch)
        if sweep_total and sweep_remaining
        else 0
    )
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
            "waiting_kinds": waiting_kinds,
            "active_kinds": sorted(active_kinds),
            "inactive_kinds": inactive_kinds,
            "all_case_sweep_total": sweep_total,
            "all_case_sweep_position": sweep_position,
            "all_case_sweep_batch": sweep_batch,
            "all_case_sweep_remaining": sweep_remaining,
            "all_case_sweep_estimated_cycles": estimated_cycles,
            "all_case_sweep_state": (
                "running"
                if sweep_running
                else "complete"
                if sweep_wrapped_complete
                else "in_progress"
            ),
            "all_case_sweep_full_cycle_completed": sweep_wrapped_complete,
            "all_case_sweep_throughput_warning": bool(sweep_total and sweep_batch < 1),
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
    pushed = data.get("calendar_push") if isinstance(data.get("calendar_push"), dict) else {}
    pdf_scan = data.get("pdf_calendar_scan") if isinstance(data.get("pdf_calendar_scan"), dict) else {}
    source_audit = data.get("calendar_source_audit") if isinstance(data.get("calendar_source_audit"), dict) else {}
    audit_ok = bool(audit.get("ok", True))
    import_ok = bool(imported.get("ok", True))
    push_ok = bool(pushed.get("ok", True))
    pdf_scan_ok = bool(pdf_scan.get("ok", True))
    source_audit_ok = bool(source_audit.get("ok", True))
    stale_age = age is not None and age > max_age_hours * 3600
    reasons = []
    if not audit_ok:
        reasons.append("calendar_audit_failed")
    if not import_ok:
        reasons.append("calendar_import_failed")
    if not push_ok or int(pushed.get("failed") or 0) > 0:
        reasons.append("calendar_push_failed")
    if not pdf_scan_ok or int(pdf_scan.get("error_count") or 0) > 0 or int(pdf_scan.get("timeout_count") or 0) > 0:
        reasons.append("calendar_pdf_scan_failed")
    if not source_audit_ok:
        reasons.append("calendar_source_audit_failed")
    if stale_age:
        reasons.append("stale_status")
    ok = audit_ok and import_ok and push_ok and pdf_scan_ok and source_audit_ok and not stale_age and not reasons
    pdf_targets = int(pdf_scan.get("targets") or 0)
    pdf_scanned = int(pdf_scan.get("scanned") or 0)
    pdf_cache_verified = int(pdf_scan.get("cache_skipped") or 0)
    pdf_verified = min(pdf_targets, pdf_scanned + pdf_cache_verified) if pdf_targets else 0
    if pdf_targets and pdf_verified < pdf_targets:
        reasons.append("calendar_pdf_coverage_incomplete")
        ok = False
    return {
        "name": "calendar_todo_status_live",
        "ok": ok,
        "parsed": {
            "age_hours": round((age or 0) / 3600, 2) if age is not None else None,
            "sla_hours": max_age_hours,
            "calendar_audit_ok": audit_ok,
            "calendar_import_ok": import_ok,
            "calendar_push_ok": push_ok,
            "calendar_pdf_scan_ok": pdf_scan_ok,
            "calendar_source_audit_ok": source_audit_ok,
            "checked_primary_events": ((audit.get("summary") or {}).get("checked_primary_events")),
            "checked_source_events": ((audit.get("summary") or {}).get("checked_source_events")),
            "imported": imported.get("imported"),
            "skipped": imported.get("skipped"),
            "calendar_pushed_inserted": int(pushed.get("inserted") or 0),
            "calendar_pushed_patched": int(pushed.get("patched") or 0),
            "pdf_targets": pdf_targets,
            "pdf_scanned": pdf_scanned,
            "pdf_cache_verified": pdf_cache_verified,
            "pdf_verified": pdf_verified,
            "pdf_coverage_percent": round((pdf_verified / pdf_targets) * 100, 1) if pdf_targets else 100.0,
            "pdf_event_candidates": int(pdf_scan.get("event_count") or 0),
            "pdf_scan_errors": int(pdf_scan.get("error_count") or 0),
            "pdf_scan_timeouts": int(pdf_scan.get("timeout_count") or 0),
            "quarantined_implausible_todos": int(pdf_scan.get("quarantine_todo_count") or 0),
            "calendar_import_only_without_pdf_source": int(source_audit.get("calendar_import_only_count") or 0),
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
    from magi_v3.external_inputs import load_bound_cron_jobs

    cron_jobs = list(load_bound_cron_jobs(root).jobs)
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


def _playwright_resource_cleanup_live() -> dict[str, Any]:
    """Close browser drivers owned by this bounded LIVE-check process.

    Portal adapters normally close their own driver.  Exceptional shutdown
    paths can nevertheless leave Playwright's driver process registered and
    keep this otherwise-completed health probe alive.  The shared registry is
    process-local, so this cannot close a production browser owned by another
    worker.
    """
    try:
        from skills.engine.playwright_wrapper import shutdown_all_playwright_drivers

        cleanup = shutdown_all_playwright_drivers()
        attempted = int(cleanup.get("attempted") or 0)
        failures = int(cleanup.get("failures") or 0)
        remaining = int(cleanup.get("remaining") or 0)
        return {
            "name": "playwright_resource_cleanup",
            "ok": failures == 0 and remaining == 0,
            "parsed": {
                "attempted": attempted,
                "failures": failures,
                "remaining": remaining,
            },
            "error": "browser_resource_cleanup_incomplete" if failures or remaining else "",
        }
    except Exception as exc:
        return {
            "name": "playwright_resource_cleanup",
            "ok": False,
            "error": _redact_text(f"{type(exc).__name__}: {exc}"),
        }


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
            elif "domain_count" in parsed and "owner_count" in parsed:
                detail = (
                    f"業務領域 {parsed.get('domain_count')} / "
                    f"owner {parsed.get('owner_count')} / 驗證器 {parsed.get('verifier_count')}"
                )
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
            delivery_value = response.get(
                "delivered",
                response.get("telegram", response.get("ok", response.get("sent", queued))),
            )
            delivered = delivery_value if type(delivery_value) is bool else False
            delivery = str(response.get("delivery") or ("queued" if queued else ("sent" if delivered else "failed")))
            result = {
                "requested": True,
                "ok": delivered or queued,
                "delivery": delivery,
                "queued": queued,
            }
            if response.get("error"):
                result["error"] = _redact_text(str(response.get("error")))
            if response.get("outbox_id"):
                result["outbox_id"] = _redact_text(str(response.get("outbox_id")))
            return result
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
    parser.add_argument(
        "--schedule-fixture-root",
        help="Run the bounded product fixture; requires the explicit fixture environment binding.",
    )
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


def _refresh_volatile_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Re-certify state that can change during the slower browser probes.

    A complete business check can spend several minutes in the read-only LAF
    and file-review probes.  Keeping the Drive result captured at the start can
    therefore publish a false failure after a replacement worker has already
    acquired the singleton lock.  Replace only the named volatile observation;
    every durable probe result remains untouched.
    """

    refreshed = _drive_sync_status_live()
    name = str(refreshed.get("name") or "")
    output = list(results)
    for index, current in enumerate(output):
        if str(current.get("name") or "") == name:
            output[index] = refreshed
            break
    else:
        output.append(refreshed)
    return output


def _run_schedule_fixture(raw_root: str, raw_output: str | None) -> int:
    # Import the production LAF probe implementation before the fixture safety
    # observer starts.  Its module initialization discovers host mount
    # candidates, while the actual probe below is bound to the disposable
    # provider.  The observed fixture phase must contain only the latter.
    import scripts.laf_nightly_audit  # noqa: F401

    from scripts.ops.schedule_fixture_contract import (
        load_schedule_fixture,
        safety_receipt,
        write_fixture_report,
    )

    fixture = load_schedule_fixture(
        raw_root, job_id="job_business_module_live_check"
    )
    product_input = fixture.manifest["product_input"]
    provider = _BoundedBusinessProbeProvider.load(fixture, product_input)
    required = {
        "laf_portal_live",
        "nas_mounts_live",
        "drive_sync_status_live",
        "calendar_todo_status_live",
        "file_review_scheduled_probe",
        "transcript_self_test_probe",
    }
    common_child_env = {
        # The outer schedule runner has already selected the hash-bound V3
        # runtime.  Source trees may expose a convenience ``venv/bin/python3``
        # symlink without their own pyvenv.cfg; letting a child re-exec through
        # that alias makes CPython lose the runtime prefix and re-exec forever.
        "MAGI_DISABLE_SKILL_VENV_REEXEC": "1",
        "MAGI_V3_SCHEDULE_FIXTURE_ROOT": str(fixture.root),
        "MAGI_V3_SCHEDULE_DRY_RUN": "1",
        "MAGI_V3_SCHEDULE_NO_NETWORK": "1",
        "MAGI_V3_SCHEDULE_NO_NOTIFY": "1",
        "MAGI_RUNTIME_DIR": str(fixture.workspace / "runtime"),
        "MAGI_AGENT_DIR": str(fixture.workspace / "agent"),
    }
    old_env = {
        key: os.environ.get(key)
        for key in (
            "MAGI_V3_REALISM_SANDBOX",
            "MAGI_LAF_PORTAL_PROVIDER_FIXTURE",
            "MAGI_RUNTIME_DIR",
        )
    }
    os.environ["MAGI_V3_REALISM_SANDBOX"] = "1"
    os.environ["MAGI_LAF_PORTAL_PROVIDER_FIXTURE"] = str(
        fixture.input_path(str(product_input["laf_provider"]))
    )
    os.environ["MAGI_RUNTIME_DIR"] = str(fixture.workspace / "runtime")
    try:
        results = [
            _laf_portal_live(),
            _nas_mounts_live(provider=provider),
            _drive_sync_status_live(),
            _calendar_todo_status_live(),
            _run(
                "file_review_scheduled_probe",
                [
                    PYTHON,
                    str(REPO_ROOT / "skills" / "file-review-orchestrator" / "action.py"),
                    "--task",
                    'scheduled_check {"notify":false}',
                ],
                timeout=60,
                env_overrides={
                    **common_child_env,
                    "MAGI_V3_SCHEDULE_ADAPTER": "real_entrypoint_fixture_v1",
                    "MAGI_FILE_REVIEW_SCHEDULE_FIXTURE_PATH": str(
                        fixture.input_path(str(product_input["file_review_provider"]))
                    ),
                },
            ),
            _run(
                "transcript_self_test_probe",
                [
                    PYTHON,
                    str(REPO_ROOT / "skills" / "transcript-downloader" / "action.py"),
                    "--task",
                    "self_test",
                ],
                timeout=60,
                env_overrides={
                    **common_child_env,
                    "MAGI_V3_SCHEDULE_ADAPTER": "real_entrypoint_dry_run_v1",
                },
            ),
        ]
    finally:
        provider.close()
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    valid_rows = all(
        isinstance(row, dict)
        and isinstance(row.get("name"), str)
        and type(row.get("ok")) is bool
        for row in results
    )
    observed_names = {str(row.get("name")) for row in results}
    required_present = valid_rows and required == observed_names
    all_healthy = valid_rows and all(row["ok"] is True for row in results)
    safe_results = _redact_obj({"results": results or []})["results"]
    summary = _summarize(safe_results if isinstance(safe_results, list) else [])
    serialized = json.dumps(
        {"results": safe_results, "summary": summary}, ensure_ascii=False
    )
    redaction_ok = not any(
        marker in serialized
        for marker in (
            "2026-9999",
            "fixture.person@example.com",
            "FIXTURE_PRIVATE_PATH_MARKER",
        )
    )
    checks = {
        "fixture_sample_bound": 1 <= fixture.sample_id <= 3,
        "formal_probe_results_typed": valid_rows,
        "all_required_probes_executed": required_present,
        "all_probes_reached_terminal_success": all_healthy,
        "summary_contains_each_probe": valid_rows
        and all(str(row["name"]) in summary for row in results),
        "external_providers_fixture_bound": all(
            path.is_relative_to(fixture.root)
            for path in (
                fixture.input_path(str(product_input["business_provider"])),
                fixture.input_path(str(product_input["laf_provider"])),
                fixture.input_path(str(product_input["file_review_provider"])),
                fixture.input_path(str(product_input["transcript_config"])),
            )
        ),
        "provider_terminal_close": provider.actions == ["nas_share_statuses", "close"]
        and [
            row.get("action")
            for row in json.loads(
                (fixture.root / "portal_provider_transcript.json").read_text(
                    encoding="utf-8"
                )
            )
        ]
        == ["login", "query_pending_drafts_all", "close"],
        "sensitive_fields_redacted": redaction_ok,
    }
    success = all(checks.values())
    report = {
        "schema": "magi.schedule-product-result/v1",
        "job_id": fixture.job_id,
        "fixture_sample_id": fixture.sample_id,
        "success": success,
        "status": "passed" if success else "failed",
        "checks": checks,
        "result_count": len(results),
        "summary": summary,
        "results": safe_results,
        "safety": safety_receipt(fixture),
    }
    output = write_fixture_report(
        fixture, raw_output or "business_module_live_check.json", report
    )
    report["json_out"] = str(output)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if success else 1


class _BoundedBusinessProbeProvider:
    def __init__(self, root: Path, payload: dict[str, Any]):
        self.root = root
        self.payload = payload
        self.actions: list[str] = []

    @classmethod
    def load(cls, fixture: Any, product_input: dict[str, Any]):
        path = fixture.input_path(str(product_input.get("business_provider") or "business-provider.json"))
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("bounded business provider is unreadable") from exc
        shares = payload.get("nas_shares") if isinstance(payload, dict) else None
        if payload.get("schema") != "magi.business-probe-provider/v1" or not isinstance(shares, dict) or not shares:
            raise RuntimeError("bounded business provider schema is invalid")
        return cls(fixture.root, payload)

    def _record(self, action: str) -> None:
        self.actions.append(action)
        target = self.root / "business_probe_provider_transcript.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps([{"action": item} for item in self.actions], indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(target)

    def nas_share_statuses(self) -> dict[str, dict[str, Any]]:
        self._record("nas_share_statuses")
        return {
            str(name): {
                "available": bool(row.get("available")),
                "mounted": bool(row.get("mounted")),
                "mode": str(row.get("mode") or "fixture"),
            }
            for name, row in self.payload["nas_shares"].items()
            if isinstance(row, dict)
        }

    def close(self) -> None:
        self._record("close")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.schedule_fixture_root:
        return _run_schedule_fixture(args.schedule_fixture_root, args.json_out)
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

    environment_source = _load_live_environment()
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
    results.append(_host_singleton_release_bindings_live())
    results.append(_business_recovery_contract_live())
    results.extend([
        _token_health_live(),
        _nas_mounts_live(),
        _drive_sync_status_live(),
        _calendar_todo_status_live(),
        _laf_ingestion_coverage_live(),
        _transcript_sync_coverage_live(),
        _notification_delivery_status_live(),
        _laf_closing_transfer_notice_live(),
    ])
    file_review_probe = _run(
        "file_review_downloadable_probe",
        [
            PYTHON,
            str(REPO_ROOT / "skills" / "file-review-orchestrator" / "action.py"),
            "--task",
            'downloadable_probe {"days":30,"notify":false,"require_portal":true,"read_only":true}',
        ],
        timeout=900,
    )
    results.extend([
        _run("laf_self_test", [PYTHON, str(REPO_ROOT / "skills" / "laf-orchestrator" / "action.py"), "--task", "self_test"], timeout=120),
        _run("file_review_self_test", [PYTHON, str(REPO_ROOT / "skills" / "file-review-orchestrator" / "action.py"), "--task", "self_test"], timeout=120),
        file_review_probe,
        _file_review_ingestion_coverage_live(portal_probe=file_review_probe),
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
    results = _refresh_volatile_results(results)
    results.append(_playwright_resource_cleanup_live())
    ok = all(bool(r.get("ok")) for r in results)
    message = _summarize(results)
    notification = _notify(message)
    if notification.get("requested") and not notification.get("ok"):
        ok = False
        results.append(
            {
                "name": "notification_delivery",
                "ok": False,
                "severity": "warning",
                "business_impact": False,
                "error": str(notification.get("error") or "notification_delivery_failed"),
                "parsed": {
                    "delivery": notification.get("delivery"),
                    "queued": bool(notification.get("queued")),
                },
            }
        )
        message = _summarize(results)
    out = {
        "ok": ok,
        "success": ok,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "release_id": str(os.environ.get("MAGI_V3_RELEASE_ID") or REPO_ROOT.name),
        "release_root": str(REPO_ROOT),
        "notification_ok": bool(notification.get("ok")),
        "results": results,
        "message": message,
        "environment_source": environment_source,
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
