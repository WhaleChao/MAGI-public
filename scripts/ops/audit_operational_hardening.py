#!/usr/bin/env python3
"""Operational hardening audit for MAGI.

Checks the items that basic /health cannot see: cron fallback compatibility,
cron time collisions, dirty worktree categories, and recent issue agenda
failures.
"""

from __future__ import annotations

import argparse
import errno
from magi_v3 import fcntl_compat as fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _FixtureExternalProvider:
    """Raw external boundaries for the formal audit suite, fixture-only."""

    def __init__(self, jobs: list[dict[str, Any]]):
        self.jobs = [dict(job) for job in jobs]
        self.calls: list[dict[str, Any]] = []
        self.profile, self.keyword = expected_omlx_profile_now(datetime.now())

    def cron_jobs(self) -> list[dict[str, Any]]:
        self.calls.append({"provider": "cron", "terminal": True})
        jobs = [dict(job) for job in self.jobs]
        jobs.append(
            {
                "id": "job_laf_gmail_dispatch_scan",
                "cron": "15 * * * *",
                "enabled": True,
                "command": shlex.join(
                    [
                        str(ROOT / "venv/bin/python3"),
                        str(ROOT / "scripts/ops/laf_gmail_dispatch_scan.py"),
                        "--apply",
                        "--json-out",
                        "fixture.json",
                    ]
                ),
            }
        )
        return jobs

    def models(self, port: int) -> list[str]:
        self.calls.append({"provider": "omlx", "port": port, "terminal": True})
        if port == 8080:
            return [f"fixture-{self.keyword}"]
        if self.profile == "day" and port == 8082:
            return ["fixture-phi4"]
        if self.profile == "day" and port == 8083:
            return ["fixture-smollm"]
        return []

    def omlx_state(self) -> tuple[str, str]:
        self.calls.append({"provider": "omlx_state", "terminal": True})
        return self.profile, f"fixture-{self.keyword}"

    def run(self, command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        argv = [str(value) for value in command]
        stdout = ""
        returncode = 0
        if argv[:2] == ["git", "status"]:
            provider = "git"
        elif argv and argv[0] == "pgrep":
            provider = "process_table"
        elif any(value.endswith("audit_osc_buttons.py") for value in argv):
            provider = "osc_routes"
            stdout = "audit summary missing_route=0\n"
        else:
            provider = "forbidden_external_command"
            returncode = 127
        self.calls.append(
            {
                "provider": provider,
                "argv": argv,
                "returncode": returncode,
                "terminal": True,
            }
        )
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    def evidence(self) -> dict[str, Any]:
        return {
            "calls": list(self.calls),
            "call_count": len(self.calls),
            "all_terminal": bool(self.calls)
            and all(call.get("terminal") is True for call in self.calls),
            "forbidden_calls": [
                call
                for call in self.calls
                if call.get("provider") == "forbidden_external_command"
            ],
        }


_ACTIVE_FIXTURE_EXTERNAL_PROVIDER: _FixtureExternalProvider | None = None


def _cron_jobs() -> list[dict[str, Any]]:
    if _ACTIVE_FIXTURE_EXTERNAL_PROVIDER is not None:
        return _ACTIVE_FIXTURE_EXTERNAL_PROVIDER.cron_jobs()
    from magi_v3.external_inputs import load_bound_cron_jobs

    return list(load_bound_cron_jobs(ROOT).jobs)


def _external_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    if _ACTIVE_FIXTURE_EXTERNAL_PROVIDER is not None:
        return _ACTIVE_FIXTURE_EXTERNAL_PROVIDER.run(command, **kwargs)
    return subprocess.run(command, **kwargs)


def _runtime_override() -> str:
    """Resolve the override at call time so tests and operators can retarget safely."""

    return os.environ.get("MAGI_RUNTIME_DIR", "").strip()


def _runtime_dir() -> Path:
    return Path(_runtime_override() or ROOT / ".runtime").expanduser()

from api.platforms.safe_process import parse_cron_command, _validate_argv  # noqa: E402
from scripts.ops.background_task_locks import cleanup_stale_lock_metadata  # noqa: E402
from scripts.ops.omlx_profile_policy import expected_profile_now as expected_omlx_profile_now  # noqa: E402


_SILENT_EXCEPT_LINE_RE = re.compile(
    r"^\s*except\s*(?:\(\s*)?(?:Exception|BaseException)?(?:\s+as\s+\w+)?(?:\s*\))?\s*:\s*(?:pass\s*(?:#.*)?$)?"
)
_EXCEPT_WITH_SAME_LINE_PASS_RE = re.compile(
    r"^\s*except\s*(?:\(\s*)?(?:Exception|BaseException)?(?:\s+as\s+\w+)?(?:\s*\))?\s*:\s*pass(?:\s*#.*)?$"
)

_ACTIVE_SOURCE_DIRS = (
    "api",
    "casper_ecosystem",
    "scripts",
    "skills",
)
_SILENT_SCAN_SKIP_PARTS = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "venv",
    "node_modules",
}
_SILENT_SCAN_SKIP_FILES = {
    "scripts/fix_silent_except.py",
}
_CRITICAL_SILENT_PREFIXES = (
    "api/blueprints/",
    "api/discord_bot.py",
    "api/handlers/",
    "api/pipelines/",
    "api/tools_api.py",
    "casper_ecosystem/law_firm_orchestrators/",
    "skills/bridge/inference_gateway.py",
)
_LEGACY_RUNTIME_PID_FILES = (
    (".runtime/_autopilot.lock", "legacy_autopilot"),
    (".runtime/laf_portal.lock", "laf_portal"),
    (".runtime/slow_archive_closed_cases_worker.pid", "slow_archive_closed_cases"),
)


def _safe_epoch(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        pass
    txt = str(value or "").strip()
    if not txt:
        return 0.0
    try:
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        return datetime.fromisoformat(txt).timestamp()
    except Exception:
        return 0.0


def _cron_job_from_issue_command(command: Any) -> str:
    cmd = str(command or "").strip()
    if not cmd.startswith("cron:"):
        return ""
    return cmd.split(":", 1)[1].strip()


def _is_false_positive_cron_issue(row: dict[str, Any]) -> bool:
    source = str(row.get("source", ""))
    if not source.startswith("discord_bot.cron_scheduler"):
        return False
    err = str(row.get("error", ""))
    err_lower = err.lower()
    if "stdout_tail=" not in err_lower:
        return False
    return ("\"success\": true" in err_lower) or ("✅" in err)


def _load_cron_success_ts() -> dict[str, float]:
    state_path = _runtime_dir() / "cron_state.json"
    if not state_path.exists():
        return {}
    raw = _load_json(state_path, {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for job_id, data in raw.items():
        if not isinstance(data, dict):
            continue
        ts = _safe_epoch(data.get("last_success_at"))
        if ts > 0:
            out[str(job_id)] = ts
    return out


def _current_omlx_models(port: int = 8080) -> list[str]:
    if _ACTIVE_FIXTURE_EXTERNAL_PROVIDER is not None:
        return _ACTIVE_FIXTURE_EXTERNAL_PROVIDER.models(port)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return [
            str(item.get("id") or "").strip()
            for item in (data.get("data") or [])
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]
    except Exception:
        return []


def audit_omlx_profile() -> dict[str, Any]:
    """Verify that the live oMLX model matches the current day/night policy."""
    now = datetime.now()
    expected_profile, expected_keyword = expected_omlx_profile_now(now)
    models = _current_omlx_models(8080)
    phi4_models = _current_omlx_models(8082)
    smol_models = _current_omlx_models(8083)
    active_profile = ""
    model_dir_hint = ""
    if _ACTIVE_FIXTURE_EXTERNAL_PROVIDER is not None:
        active_profile, model_dir_hint = _ACTIVE_FIXTURE_EXTERNAL_PROVIDER.omlx_state()
    else:
        try:
            active_profile = (Path.home() / ".omlx" / "active_profile").read_text(encoding="utf-8").strip()
        except Exception:
            active_profile = ""
        try:
            model_dir = Path.home() / ".omlx" / "models-text"
            model_dir_hint = " ".join(sorted(p.name.lower() for p in model_dir.iterdir()))
        except Exception:
            model_dir_hint = ""
    live_text = " ".join(models).lower()
    phi4_text = " ".join(phi4_models).lower()
    smol_text = " ".join(smol_models).lower()
    exact_profile_ok = (
        expected_keyword in live_text
        and expected_keyword in model_dir_hint.lower()
        and active_profile == expected_profile
    )
    sidecars_ok = True
    if expected_profile == "day":
        sidecars_ok = ("phi" in phi4_text and "smol" in smol_text)
    else:
        sidecars_ok = (not phi4_models and not smol_models)
    exact_profile_ok = exact_profile_ok and sidecars_ok

    fallback_specs = {
        ("day", "day-e4b-degraded"): ("e4b", "day_fallback_stamp", 3600),
        ("night", "night-12b-degraded"): ("12b", "night_fallback_stamp", 21600),
        ("night", "night-e4b-degraded"): ("e4b", "night_fallback_stamp", 21600),
    }
    fallback_keyword = ""
    fallback_age_seconds: int | None = None
    fallback_retry_seconds: int | None = None
    fallback_spec = fallback_specs.get((expected_profile, active_profile))
    controlled_degraded = False
    if fallback_spec is not None and _ACTIVE_FIXTURE_EXTERNAL_PROVIDER is None:
        fallback_keyword, stamp_name, fallback_retry_seconds = fallback_spec
        stamp = Path.home() / ".omlx" / stamp_name
        try:
            stamp_age = int(time.time() - stamp.stat().st_mtime)
            stamp_is_bound = (
                stamp.is_file()
                and not stamp.is_symlink()
                and stamp.resolve(strict=True) == stamp
                and 0 <= stamp_age < fallback_retry_seconds
            )
        except (OSError, ValueError):
            stamp_age = -1
            stamp_is_bound = False
        fallback_age_seconds = stamp_age if stamp_age >= 0 else None
        controlled_degraded = (
            stamp_is_bound
            and fallback_keyword in live_text
            and fallback_keyword in model_dir_hint.lower()
            and sidecars_ok
        )
    ok = exact_profile_ok or controlled_degraded
    return {
        "ok": ok,
        "degraded": controlled_degraded,
        "expected_profile": expected_profile,
        "expected_keyword": expected_keyword,
        "active_profile": active_profile,
        "fallback_keyword": fallback_keyword,
        "fallback_age_seconds": fallback_age_seconds,
        "fallback_retry_seconds": fallback_retry_seconds,
        "models": models,
        "phi4_models": phi4_models,
        "smol_models": smol_models,
        "sidecars_ok": sidecars_ok,
        "model_dir_hint": model_dir_hint,
        "time": now.strftime("%Y-%m-%d %H:%M"),
        "remediation": (
            "Controlled local fallback is healthy; cron job_omlx_profile_guard will retry the preferred model after cooldown."
            if controlled_degraded
            else "Run config/bin/omlx_switch_model.sh auto; cron job_omlx_profile_guard should keep this idempotently repaired."
        ),
    }


def _latest_operational_audit_is_green(issue_ts: float) -> bool:
    path = _runtime_dir() / "operational_hardening_audit_latest.json"
    if not path.exists() or path.stat().st_mtime <= issue_ts:
        return False
    data = _load_json(path, {})
    cron = data.get("cron") if isinstance(data, dict) else {}
    gmail = data.get("gmail_monitor") if isinstance(data, dict) else {}
    return (
        int((cron or {}).get("parse_failure_count") or 0) == 0
        and int((cron or {}).get("collision_count") or 0) == 0
        and bool((gmail or {}).get("ok", True))
    )


def _latest_tailscale_funnel_is_green(issue_ts: float) -> bool:
    path = _runtime_dir() / "tailscale_funnel_health_latest.json"
    if not path.exists() or path.stat().st_mtime <= issue_ts:
        return False
    data = _load_json(path, {})
    return str((data or {}).get("status") or "").lower() in {"ok", "recovered", "skipped"}


def _classify_issue_row(
    row: dict[str, Any],
    *,
    active_cutoff: float,
    latest_cron_issue_ts_by_job: dict[str, float],
    cron_success_ts: dict[str, float],
) -> str:
    source = str(row.get("source", ""))
    if not source.startswith("discord_bot.cron_scheduler"):
        return "non_cron"
    if _is_false_positive_cron_issue(row):
        return "false_positive"

    ts = _safe_epoch(row.get("ts") or row.get("iso"))
    job_id = _cron_job_from_issue_command(row.get("command"))
    if not job_id:
        return "stale" if ts < active_cutoff else "active_unresolved"
    err = str(row.get("error") or "")
    if job_id in {"job_omlx_switch_day", "job_omlx_switch_night", "job_omlx_profile_guard"} and "8080" in err:
        if any("gemma-4-e4b" in model.lower() for model in _current_omlx_models()):
            return "recovered"
    if job_id == "job_operational_hardening_audit" and _latest_operational_audit_is_green(ts):
        return "recovered"
    if job_id == "job_tailscale_funnel_healthcheck" and _latest_tailscale_funnel_is_green(ts):
        return "recovered"
    if latest_cron_issue_ts_by_job.get(job_id, ts) > ts:
        return "superseded"
    if cron_success_ts.get(job_id, 0.0) > ts:
        return "recovered"
    if ts < active_cutoff:
        return "stale"
    return "active_unresolved"


def _load_json(path: Path, default: Any) -> Any:
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


def _load_dotenv_value(key: str, default: str = "") -> str:
    env_value = os.environ.get(key)
    if env_value is not None:
        return env_value
    env_path = ROOT / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == key:
                return v.strip().strip("'\"")
    except Exception:
        return default
    return default


def _truthy_config(name: str, default: str = "0") -> bool:
    return str(_load_dotenv_value(name, default) or default).strip().lower() in {"1", "true", "yes", "on"}


def _active_cloudflared_tunnels() -> list[dict[str, Any]]:
    proc = _external_run(
        ["pgrep", "-fl", "cloudflared tunnel --url"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    tunnels: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        if not line.strip():
            continue
        if "pgrep -fl" in line or "audit_operational_hardening.py" in line or "pytest" in line:
            continue
        match = re.search(r"cloudflared tunnel --url\s+https?://127\.0\.0\.1:(\d+)", line)
        if not match:
            continue
        tunnels.append({"line": line.strip(), "port": match.group(1)})
    return tunnels


def audit_cron() -> dict[str, Any]:
    jobs = _cron_jobs()
    enabled = [j for j in jobs if j.get("enabled", True)]
    parse_failures = []
    collisions = []
    by_cron: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for job in enabled:
        by_cron[job.get("cron", "")].append(job)
        command = (job.get("command") or "").strip()
        if not command or command.startswith("@MAGI"):
            continue
        try:
            argv = parse_cron_command(command)
            _validate_argv(argv)
        except Exception as exc:
            parse_failures.append({
                "id": job.get("id"),
                "cron": job.get("cron"),
                "desc": job.get("desc"),
                "error": f"{type(exc).__name__}: {exc}",
                "command": command,
            })

    for cron, grouped in sorted(by_cron.items()):
        if len(grouped) <= 1:
            continue
        heavy = [
            j for j in grouped
            if not (j.get("command") or "").strip().startswith("@MAGI")
        ]
        if len(heavy) > 1:
            collisions.append({
                "cron": cron,
                "jobs": [
                    {"id": j.get("id"), "desc": j.get("desc"), "command": j.get("command")}
                    for j in grouped
                ],
            })

    return {
        "enabled_count": len(enabled),
        "parse_failure_count": len(parse_failures),
        "parse_failures": parse_failures,
        "collision_count": len(collisions),
        "collisions": collisions,
    }


def audit_runtime_root_consistency() -> dict[str, Any]:
    """Find active cron commands that point at a different MAGI checkout."""
    jobs = _cron_jobs()
    enabled = [j for j in jobs if j.get("enabled", True)]
    root_text = str(ROOT)
    known_roots = {
        root_text,
        str(Path.home() / "Desktop" / "MAGI_v2"),
        str(
            Path.home()
            / "Library"
            / "Application Support"
            / "MAGI"
            / "runtime"
            / "MAGI_v2"
        ),
    }
    runtime_env = str(os.environ.get("MAGI_RUNTIME_DIR") or "").strip()
    mismatches: list[dict[str, Any]] = []
    for job in enabled:
        command = str(job.get("command") or "")
        hits = sorted(root for root in known_roots if root and root in command)
        bad_hits = [root for root in hits if root != root_text]
        if bad_hits:
            mismatches.append(
                {
                    "id": job.get("id"),
                    "cron": job.get("cron"),
                    "desc": job.get("desc"),
                    "unexpected_roots": bad_hits,
                    "command": command[:320],
                }
            )
    return {
        "ok": not mismatches,
        "root": root_text,
        "runtime_dir": runtime_env,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches[:30],
        "requirement": "Enabled cron jobs should execute inside the same MAGI root as the running daemon checkout.",
    }


def _legacy_pid_file_paths() -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    resolved_runtime_dir = _runtime_dir()
    runtime_dir = str(resolved_runtime_dir)
    runtime_root = resolved_runtime_dir.parent if _runtime_override() else None
    for rel, domain in _LEGACY_RUNTIME_PID_FILES:
        path = resolved_runtime_dir / rel.split("/", 1)[1] if rel.startswith(".runtime/") else ROOT / rel
        candidates.append((path, domain))
        if runtime_root is not None:
            if rel.startswith(".runtime/"):
                alt = Path(runtime_dir) / rel.split("/", 1)[1]
            else:
                alt = runtime_root / rel
            candidates.append((alt, domain))

    out: list[tuple[Path, str]] = []
    seen: set[str] = set()
    for path, domain in candidates:
        key = str(path.resolve()) if path.exists() else str(path)
        if key in seen:
            continue
        seen.add(key)
        out.append((path, domain))
    return out


def _read_legacy_pid_file(path: Path) -> tuple[int, str]:
    raw = path.read_text(encoding="utf-8", errors="replace").strip()
    match = re.search(r"\b(\d{2,})\b", raw)
    if not match:
        return 0, raw[:200]
    return int(match.group(1)), raw[:200]


def _legacy_lock_is_held(path: Path) -> bool:
    """Check the kernel lock, not stale PID text left in a reusable lock anchor."""

    try:
        with path.open("r+", encoding="utf-8") as handle:
            try:
                fcntl.lockf(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN}:
                    return True
                raise
            finally:
                try:
                    fcntl.lockf(handle, fcntl.LOCK_UN)
                except OSError:
                    pass
    except OSError:
        return True
    return False


def _audit_legacy_runtime_pid_files(*, cleanup: bool = False) -> dict[str, Any]:
    active: list[dict[str, Any]] = []
    stale: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    inactive_anchors: list[dict[str, Any]] = []
    cleaned: list[str] = []
    for path, domain in _legacy_pid_file_paths():
        if not path.exists():
            continue
        if domain == "legacy_autopilot" and not _legacy_lock_is_held(path):
            item = {
                "path": str(path),
                "domain": domain,
                "reason": "kernel_lock_not_held",
            }
            if cleanup:
                try:
                    path.unlink(missing_ok=True)
                    cleaned.append(str(path))
                    continue
                except Exception:
                    pass
            inactive_anchors.append(item)
            continue
        try:
            pid, sample = _read_legacy_pid_file(path)
        except Exception as exc:
            item = {"path": str(path), "domain": domain, "reason": f"read_error:{exc}"}
            if cleanup:
                try:
                    path.unlink(missing_ok=True)
                    cleaned.append(str(path))
                    continue
                except Exception:
                    pass
            malformed.append(item)
            continue
        item = {"path": str(path), "domain": domain, "pid": pid, "sample": sample}
        if not pid:
            if cleanup:
                try:
                    path.unlink(missing_ok=True)
                    cleaned.append(str(path))
                    continue
                except Exception:
                    pass
            malformed.append({**item, "reason": "missing_pid"})
        elif _pid_alive(pid):
            active.append(item)
        else:
            if cleanup:
                try:
                    path.unlink(missing_ok=True)
                    cleaned.append(str(path))
                    continue
                except Exception:
                    pass
            stale.append(item)
    return {
        "ok": not stale and not malformed,
        "apply": bool(cleanup),
        "active_count": len(active),
        "stale_count": len(stale),
        "malformed_count": len(malformed),
        "cleaned_count": len(cleaned),
        "inactive_anchor_count": len(inactive_anchors),
        "active": active[:20],
        "stale": stale[:20],
        "malformed": malformed[:10],
        "cleaned": cleaned[:20],
        "inactive_anchors": inactive_anchors[:20],
    }


def audit_stale_runtime_locks(*, cleanup: bool = False) -> dict[str, Any]:
    """Report lock files whose owner PID is gone.

    By default this is read-only.  With cleanup=True it first clears stale
    owner metadata only after acquiring the same flock, then audits the result.
    """
    roots = [_runtime_dir() / "locks"]
    cleanup_report = cleanup_stale_lock_metadata(roots, apply=True) if cleanup else {"apply": False}
    legacy_report = _audit_legacy_runtime_pid_files(cleanup=cleanup)
    seen: set[str] = set()
    stale: list[dict[str, Any]] = []
    active: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    orphaned_anchors: list[dict[str, Any]] = []
    for lock_dir in roots:
        if not lock_dir.exists():
            continue
        for path in sorted(lock_dir.glob("*.lock")):
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            meta_path = Path(str(path) + ".json")
            raw = path.read_text(encoding="utf-8", errors="replace")
            if not meta_path.exists():
                # BackgroundLock removes the sidecar metadata on clean release
                # but intentionally may leave the flock file itself behind.
                # Without sidecar metadata, the file is only a reusable lock
                # anchor.  Older BackgroundLock releases left the previous JSON
                # owner in the lock body; surface that as cleanup noise without
                # failing the hardening gate.
                body = _load_json(path, {})
                if isinstance(body, dict) and body.get("pid"):
                    orphaned_anchors.append(
                        {
                            "path": str(path),
                            "domain": body.get("domain") or path.stem,
                            "owner": body.get("owner") or "",
                            "pid": int(body.get("pid") or 0),
                            "started_at": body.get("started_at") or "",
                            "reason": "owner_json_without_sidecar",
                        }
                    )
                continue
            data = _load_json(meta_path, {}) or _load_json(path, {})
            if not isinstance(data, dict):
                malformed.append({"path": str(path), "sample": raw[:200]})
                continue
            pid = int(data.get("pid") or 0)
            item = {
                "path": str(path),
                "domain": data.get("domain") or path.stem,
                "owner": data.get("owner") or "",
                "pid": pid,
                "started_at": data.get("started_at") or "",
            }
            if pid and _pid_alive(pid):
                active.append(item)
            else:
                stale.append(item)
    legacy_active = legacy_report.get("active", []) if isinstance(legacy_report, dict) else []
    legacy_stale = legacy_report.get("stale", []) if isinstance(legacy_report, dict) else []
    legacy_malformed = legacy_report.get("malformed", []) if isinstance(legacy_report, dict) else []
    return {
        "ok": not stale and not malformed and not legacy_stale and not legacy_malformed,
        "active_count": len(active) + len(legacy_active),
        "stale_count": len(stale) + len(legacy_stale),
        "malformed_count": len(malformed) + len(legacy_malformed),
        "legacy_stale_count": len(legacy_stale),
        "legacy_malformed_count": len(legacy_malformed),
        "orphaned_anchor_count": len(orphaned_anchors),
        "active": (active + legacy_active)[:30],
        "stale": (stale + legacy_stale)[:30],
        "malformed": (malformed + legacy_malformed)[:10],
        "orphaned_anchors": orphaned_anchors[:30],
        "cleanup": cleanup_report,
        "legacy_pid_files": legacy_report,
        "requirement": "Runtime lock files must point to a live PID or be cleaned by the next owner.",
    }


def audit_laf_gmail_fallback_job() -> dict[str, Any]:
    jobs = _cron_jobs()
    matches = [
        j
        for j in jobs
        if j.get("enabled", True)
        and (
            str(j.get("id") or "") == "job_laf_gmail_dispatch_scan"
            or "laf_gmail_dispatch_scan.py" in str(j.get("command") or "")
        )
    ]
    ok = bool(matches) and any(
        "--json-out" in str(j.get("command") or "")
        and "--apply" in str(j.get("command") or "")
        for j in matches
    )
    return {
        "ok": ok,
        "match_count": len(matches),
        "jobs": [
            {
                "id": j.get("id"),
                "cron": j.get("cron"),
                "desc": j.get("desc"),
                "has_json_out": "--json-out" in str(j.get("command") or ""),
                "has_apply": "--apply" in str(j.get("command") or ""),
            }
            for j in matches
        ],
        "requirement": "LAF Gmail dispatch needs a bounded supervised scanner with durable JSON output and explicit --apply processing.",
    }


def audit_domain_interference() -> dict[str, Any]:
    """Find enabled cron jobs that can fight over the same business domain.

    Time-slot collision checks catch jobs that run at the exact same minute, but
    user-facing interference often comes from two enabled entrypoints that touch
    the same state at different times.  Keep these rules narrow and explainable:
    each violation points to a known canonical job.
    """
    jobs = _cron_jobs()
    enabled = [j for j in jobs if j.get("enabled", True)]
    issues: list[dict[str, Any]] = []

    def _matching_jobs(needle: str) -> list[dict[str, Any]]:
        return [j for j in enabled if needle in str(j.get("command") or "")]

    transcript_indexers = [
        j
        for j in _matching_jobs("skills/transcript-indexer/action.py")
        if "--task index" in str(j.get("command") or "")
    ]
    if len(transcript_indexers) > 1:
        issues.append(
            {
                "domain": "transcript_indexer",
                "canonical_job": "job_transcript_indexer",
                "issue": "多個啟用排程會執行 transcript-indexer --task index，可能在筆錄同步前後重複處理同一批資料。",
                "jobs": [
                    {"id": j.get("id"), "cron": j.get("cron"), "desc": j.get("desc")}
                    for j in transcript_indexers
                ],
            }
        )

    osc_events_refresh = _matching_jobs("scripts/ops/osc_events_refresh.py")
    legacy_gcal_sync = _matching_jobs("scripts/ops/osc_gcal_sync.py")
    if osc_events_refresh and legacy_gcal_sync:
        issues.append(
            {
                "domain": "osc_calendar_todos",
                "canonical_job": "job_osc_events_refresh",
                "issue": "osc_events_refresh 與舊 osc_gcal_sync 同時啟用，可能讓 PDF/筆錄待辦與 Google 日曆匯入互相覆寫。",
                "jobs": [
                    {"id": j.get("id"), "cron": j.get("cron"), "desc": j.get("desc")}
                    for j in (osc_events_refresh + legacy_gcal_sync)
                ],
            }
        )

    share_tunnel_jobs = [
        j
        for j in enabled
        if "start_paperclip_share_tunnel.sh" in str(j.get("command") or "")
        or "cloudflared tunnel" in str(j.get("command") or "")
    ]
    tailscale_funnel_jobs = _matching_jobs("tailscale_funnel_healthcheck.py")
    if share_tunnel_jobs and tailscale_funnel_jobs:
        issues.append(
            {
                "domain": "external_share_tunnel",
                "canonical_job": "job_tailscale_funnel_healthcheck",
                "issue": "Tailscale Funnel 與 cloudflared trycloudflare 分享通道同時由 cron 維持，可能產生過期分享連結或殭屍 tunnel。",
                "jobs": [
                    {"id": j.get("id"), "cron": j.get("cron"), "desc": j.get("desc")}
                    for j in (tailscale_funnel_jobs + share_tunnel_jobs)
                ],
            }
        )

    allowed_cloudflared_ports = {
        str(_load_dotenv_value("PAPERCLIP_SHARE_GATEWAY_PORT", "5014") or "5014").strip(),
    }
    if _truthy_config("MAGI_ENABLE_CLOUDFLARE_WEBHOOK", "0"):
        allowed_cloudflared_ports.add(
            str(
                _load_dotenv_value(
                    "MAGI_WEBHOOK_PROXY_PORT",
                    _load_dotenv_value("MAGI_TAILSCALE_PORT", "18790"),
                )
                or "18790"
            ).strip()
        )
    active_cloudflared = _active_cloudflared_tunnels()
    unexpected_cloudflared = [
        item for item in active_cloudflared if str(item.get("port") or "") not in allowed_cloudflared_ports
    ]
    if unexpected_cloudflared:
        issues.append(
            {
                "domain": "cloudflare_quick_tunnel",
                "canonical_job": "job_tailscale_funnel_healthcheck",
                "issue": "偵測到非授權 cloudflared Quick Tunnel。固定外網應走 Tailscale Funnel；分享檔案只允許 Paperclip gateway port。",
                "allowed_ports": sorted(allowed_cloudflared_ports),
                "processes": unexpected_cloudflared,
            }
        )

    return {
        "ok": not issues,
        "issue_count": len(issues),
        "issues": issues,
        "requirement": "Each business domain should have one canonical scheduled entrypoint; companion audits may exist but must not mutate the same state.",
    }


def audit_background_task_locks() -> dict[str, Any]:
    """Verify canonical background task mutex contracts are wired in source."""
    checks = []

    def _contains(rel: str, *needles: str) -> bool:
        text = (ROOT / rel).read_text(encoding="utf-8", errors="replace")
        return all(needle in text for needle in needles)

    checks.append({
        "name": "discord_daemon_scheduler_owner_lock",
        "ok": _contains("api/discord_bot.py", "SCHEDULER_LOCK_NAME", "discord_internal_cron")
        and _contains("daemon.py", "SCHEDULER_LOCK_NAME", "daemon_cron_fallback"),
        "requirement": "Discord internal cron and daemon fallback must compete for one scheduler owner lock.",
    })
    checks.append({
        "name": "osc_refresh_single_writer_lock",
        "ok": _contains("scripts/ops/osc_events_refresh.py", "OSC_REFRESH_LOCK_NAME", "osc_refresh_already_running")
        and _contains("skills/pdf-namer/smart_filer.py", "OSC_REFRESH_LOCK_NAME", "pdf_namer_best_effort_osc_sync"),
        "requirement": "OSC/GCal/todo refresh and pdf-namer best-effort sync must share one nonblocking mutex.",
    })
    checks.append({
        "name": "drive_worker_kind_state",
        "ok": _contains(
            "scripts/drive_case_sync_worker.py",
            "status_by_kind",
            "worker_state_",
            "worker_status_path(kind)",
        ),
        "requirement": "Drive bounded/all-files runs must preserve per-kind status/state instead of overwriting each other.",
    })
    checks.append({
        "name": "case_folder_domain_guard",
        "ok": _contains("api/domains/case_file_operation_lock.py", "acquire_lock", "write_pid_file=True")
        and _contains("scripts/drive_case_sync_worker.py", "acquire_case_file_operation_lock")
        and _contains("scripts/ops/slow_archive_closed_cases.py", "acquire_case_file_operation_lock")
        and _contains("scripts/ops/cleanup_synology_empty_case_shells.py", "acquire_case_file_operation_lock"),
        "requirement": "Drive sync, slow archive, and empty-shell cleanup must share one case-folder mutation guard.",
    })
    checks.append({
        "name": "pdf_in_place_mutation_guard",
        "ok": _contains("scripts/ops/pdf_mutation_lock.py", "PDF_IN_PLACE_MUTATION_LOCK_NAME", "pdf_in_place_mutation")
        and _contains("skills/pdf-bookmarker/action.py", "pdf_in_place_mutation_lock")
        and _contains("scripts/weekend_bookmark_batch.py", "pdf_in_place_mutation_lock")
        and _contains("scripts/ops/repair_pdf_bookmark_labels.py", "scan_and_bookmark(str(pdf_path), dry_run=False"),
        "requirement": "PDF bookmark writers and repair jobs must share one in-place PDF mutation guard through pdf-bookmarker.",
    })
    checks.append({
        "name": "nas_ocr_queue_worker_lock",
        "ok": _contains("skills/documents/nas_pdf_ocr_worker.py", "NAS_OCR_QUEUE_LOCK_NAME", "already_running_status")
        and _contains("tests/test_nas_pdf_ocr_worker_lock.py", "worker body should not run"),
        "requirement": "NAS OCR queue worker must skip before touching SQLite/PDFs when another OCR worker is active.",
    })
    failures = [check for check in checks if not check.get("ok")]
    return {
        "ok": not failures,
        "failure_count": len(failures),
        "checks": checks,
    }


def audit_git() -> dict[str, Any]:
    proc = _external_run(
        ["git", "status", "--short"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    generated_prefixes = (
        "?? static/worldmonitor_reports/",
        " D static/worldmonitor_reports/",
        " M static/translator_ape_latest.json",
        "?? static/translator_ape_latest.json",
        " M static/knowledge_lint_latest.json",
        "?? static/knowledge_lint_latest.json",
        " M json/processed_laf_emails.json",
        "?? json/processed_laf_emails.json",
        " M skills/pdf-namer/db_rules_cache.json",
        "?? skills/pdf-namer/db_rules_cache.json",
        "?? cron_jobs.json.bak.",
        "?? .claude/worktrees/",
    )
    generated = [
        line for line in lines
        if line.startswith(generated_prefixes)
        or "__pycache__/" in line
        or line.endswith(".pyc")
    ]
    source = [line for line in lines if line not in generated]
    return {
        "dirty_count": len(source),
        "raw_dirty_count": len(lines),
        "source_or_review_count": len(source),
        "generated_or_runtime_count": len(generated),
        "source_or_review": source,
        "generated_or_runtime": generated[:80],
    }


def audit_issue_agenda(limit: int = 20) -> dict[str, Any]:
    path = _runtime_dir() / "issue_agenda.jsonl"
    if not path.exists():
        return {"exists": False, "recent": []}

    all_rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
            row["_ts"] = _safe_epoch(row.get("ts") or row.get("iso"))
            all_rows.append(row)
        except Exception:
            continue
    rows = all_rows[-limit:]

    latest_cron_issue_ts_by_job: dict[str, float] = {}
    for row in all_rows:
        source = str(row.get("source", ""))
        if not source.startswith("discord_bot.cron_scheduler"):
            continue
        if _is_false_positive_cron_issue(row):
            continue
        job_id = _cron_job_from_issue_command(row.get("command"))
        if not job_id:
            continue
        ts = float(row.get("_ts") or 0.0)
        prev = latest_cron_issue_ts_by_job.get(job_id, 0.0)
        if ts > prev:
            latest_cron_issue_ts_by_job[job_id] = ts

    active_window_sec = int(os.environ.get("MAGI_OPERATIONAL_ACTIVE_ISSUE_WINDOW_SEC", "21600") or "21600")
    active_cutoff = time.time() - active_window_sec
    cron_success_ts = _load_cron_success_ts()
    class_counts: dict[str, int] = defaultdict(int)
    recent = []
    for row in rows:
        state = _classify_issue_row(
            row,
            active_cutoff=active_cutoff,
            latest_cron_issue_ts_by_job=latest_cron_issue_ts_by_job,
            cron_success_ts=cron_success_ts,
        )
        class_counts[state] += 1
        recent.append(
            {
                "iso": row.get("iso"),
                "command": row.get("command"),
                "severity": row.get("severity"),
                "state": state,
                "error": (row.get("error") or "")[:500],
            }
        )

    active_count = int(class_counts.get("active_unresolved") or 0)
    return {
        "exists": True,
        "recent_count": len(rows),
        "active_count": active_count,
        "inactive_or_context_count": max(0, len(rows) - active_count),
        "recent_state_counts": dict(class_counts),
        "recent": recent,
    }


def audit_gmail_monitor_mode() -> dict[str, Any]:
    """Check whether Gmail monitoring uses polling or push-watch semantics.

    Push mode needs daily watch renewal and historyId 404 full-sync handling.
    MAGI's stable LAF monitor is currently polling; this audit makes that
    explicit so a future push implementation cannot be added silently.
    """
    files = [
        ROOT / "skills" / "legal" / "laf.py",
        ROOT / "skills" / "gmail-drafts" / "action.py",
        ROOT / "api" / "startup.py",
    ]
    hits = []
    for path in files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if ".watch(" in text or "users().watch" in text or "history().list" in text:
            hits.append(str(path.relative_to(ROOT)))
    return {
        "ok": len(hits) == 0,
        "mode": "polling" if not hits else "push_or_history_detected",
        "push_watch_files": hits,
        "requirement": "If Gmail push/history is introduced, add daily watch renewal and HTTP 404 full-sync backstop.",
    }


def _iter_source_files() -> list[Path]:
    files: list[Path] = []
    for dirname in _ACTIVE_SOURCE_DIRS:
        base = ROOT / dirname
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            rel = path.relative_to(ROOT)
            rel_text = rel.as_posix()
            if rel_text in _SILENT_SCAN_SKIP_FILES:
                continue
            if any(part in _SILENT_SCAN_SKIP_PARTS for part in rel.parts):
                continue
            files.append(path)
    return files


def audit_silent_exception_handlers(max_samples: int = 30) -> dict[str, Any]:
    """Find exception handlers that fully disappear errors.

    The goal is not to ban every defensive ``except`` block. MAGI should,
    however, know where an exception is swallowed with only ``pass`` so that
    production failures become visible and can be reduced over time.
    """
    findings: list[dict[str, Any]] = []
    by_area: dict[str, int] = defaultdict(int)
    critical_count = 0
    for path in _iter_source_files():
        rel = path.relative_to(ROOT).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        for idx, line in enumerate(lines):
            same_line = bool(_EXCEPT_WITH_SAME_LINE_PASS_RE.match(line))
            block_pass = False
            if (not same_line) and _SILENT_EXCEPT_LINE_RE.match(line):
                for nxt in lines[idx + 1 : idx + 8]:
                    stripped = nxt.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    block_pass = stripped == "pass"
                    break
            if not same_line and not block_pass:
                continue
            area = rel.split("/", 1)[0]
            by_area[area] += 1
            is_critical = rel.startswith(_CRITICAL_SILENT_PREFIXES)
            if is_critical:
                critical_count += 1
            if len(findings) < max_samples:
                findings.append(
                    {
                        "file": rel,
                        "line": idx + 1,
                        "critical": is_critical,
                        "kind": "same_line_pass" if same_line else "block_pass",
                    }
                )
    return {
        "ok": critical_count == 0,
        "total_count": sum(by_area.values()),
        "critical_count": critical_count,
        "by_area": dict(sorted(by_area.items())),
        "samples": findings,
        "requirement": "Replace pass-only handlers with logging, explicit degraded state, retry, or user-visible failure.",
    }


def audit_retired_feature_residue() -> dict[str, Any]:
    """Surface retired runtime paths that can still confuse routing or alerts."""
    proc = _external_run(
        ["pgrep", "-fl", "openclaw|WFGY|wfgy"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    process_lines = [
        line
        for line in proc.stdout.splitlines()
        if line.strip()
        and "rg -n" not in line
        and "pytest" not in line
        and "test_wfgy_retired.py" not in line
        and "test_rule_source_of_truth.py" not in line
        and "audit_operational_hardening.py" not in line
        and "pgrep -fl" not in line
    ]
    route_residue: list[dict[str, Any]] = []
    for rel in (
        "api/handlers/translation_handler.py",
        "skills/bridge/inference_gateway.py",
        "skills/bridge/tri_sage_collab.py",
    ):
        path = ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "route=\"openclaw_codex\"" in text or "\"route\": \"openclaw_codex\"" in text:
            route_residue.append(
                {
                    "file": rel,
                    "issue": "active response metadata still says openclaw_codex",
                    "recommendation": "Rename to codex_direct or remove the retired fallback route.",
                }
            )
    env_flags = {
        name: os.environ.get(name, "")
        for name in (
            "MAGI_OPENCLAW_SKILL_ROOTS",
            "MAGI_CODEX_CHAT_FALLBACK",
            "MAGI_CODEX_DIRECT_VISION_ENABLE",
            "MAGI_JUDGMENT_WFGY",
        )
        if os.environ.get(name)
    }
    ok = not process_lines and not route_residue and not env_flags
    return {
        "ok": ok,
        "process_count": len(process_lines),
        "processes": process_lines[:20],
        "route_residue_count": len(route_residue),
        "route_residue": route_residue,
        "env_flags": env_flags,
        "requirement": "Retired OpenClaw/WFGY names must not be active runtime routes, processes, or opt-in defaults.",
    }


def audit_osc_route_integrity() -> dict[str, Any]:
    """Run the OSC frontend/backend route audit without making network calls."""
    script = ROOT / "scripts" / "ops" / "audit_osc_buttons.py"
    if not script.exists():
        return {"ok": False, "error": "scripts/ops/audit_osc_buttons.py missing"}
    proc = _external_run(
        [sys.executable, str(script), "--summary"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=90,
        check=False,
    )
    match = re.search(r"missing_route=(\d+)", proc.stdout or "")
    missing = int(match.group(1)) if match else -1
    return {
        "ok": proc.returncode == 0 and missing == 0,
        "returncode": proc.returncode,
        "missing_route": missing,
        "summary": next((line for line in (proc.stdout or "").splitlines() if "summary" in line and "missing_route=" in line), ""),
        "stdout_tail": proc.stdout[-1200:],
        "stderr_tail": proc.stderr[-1200:],
    }


def run_operational_audit(*, cleanup_stale_locks: bool) -> dict[str, Any]:
    """Execute the same formal audit suite for CLI and bounded providers."""

    return {
        "cron": audit_cron(),
        "runtime_root_consistency": audit_runtime_root_consistency(),
        "stale_runtime_locks": audit_stale_runtime_locks(
            cleanup=cleanup_stale_locks
        ),
        "domain_interference": audit_domain_interference(),
        "background_task_locks": audit_background_task_locks(),
        "git": audit_git(),
        "issue_agenda": audit_issue_agenda(),
        "gmail_monitor": audit_gmail_monitor_mode(),
        "laf_gmail_fallback_job": audit_laf_gmail_fallback_job(),
        "omlx_profile": audit_omlx_profile(),
        "silent_exception_handlers": audit_silent_exception_handlers(),
        "retired_feature_residue": audit_retired_feature_residue(),
        "osc_route_integrity": audit_osc_route_integrity(),
    }


def _formal_red_reasons(report: dict[str, Any]) -> list[str]:
    reasons = []
    if report["cron"]["parse_failure_count"] > 0:
        reasons.append("cron_parse")
    if report["cron"]["collision_count"] > 0:
        reasons.append("cron_collision")
    if not report["runtime_root_consistency"]["ok"]:
        reasons.append("runtime_root")
    if not report["stale_runtime_locks"]["ok"]:
        reasons.append("stale_lock")
    if report["domain_interference"]["issue_count"] > 0:
        reasons.append("domain_interference")
    if report["silent_exception_handlers"]["critical_count"] > 0:
        reasons.append("critical_silent_exception")
    for key in (
        "background_task_locks",
        "gmail_monitor",
        "laf_gmail_fallback_job",
        "omlx_profile",
        "retired_feature_residue",
        "osc_route_integrity",
    ):
        if not report[key]["ok"]:
            reasons.append(key)
    return reasons


def _formal_red_count(report: dict[str, Any]) -> int:
    return (
        int(report["cron"]["parse_failure_count"])
        + int(report["cron"]["collision_count"])
        + int(report["runtime_root_consistency"]["mismatch_count"])
        + int(report["stale_runtime_locks"]["stale_count"])
        + int(report["stale_runtime_locks"]["malformed_count"])
        + int(report["domain_interference"]["issue_count"])
        + int(report["silent_exception_handlers"]["critical_count"])
        + sum(
            report[key]["ok"] is not True
            for key in (
                "background_task_locks",
                "gmail_monitor",
                "laf_gmail_fallback_job",
                "omlx_profile",
                "retired_feature_residue",
                "osc_route_integrity",
            )
        )
    )


def _run_schedule_fixture(
    raw_root: str,
    raw_output: str,
    *,
    cleanup_stale_locks: bool,
    fail_on_red: bool,
) -> int:
    from scripts.ops.schedule_fixture_contract import (
        load_schedule_fixture,
        safety_receipt,
        write_fixture_report,
    )

    fixture = load_schedule_fixture(
        raw_root, job_id="job_operational_hardening_audit"
    )
    product_input = fixture.manifest["product_input"]
    jobs = product_input.get("cron_jobs")
    locks = product_input.get("locks")
    findings = product_input.get("findings")
    expected = product_input.get("expected")
    typed = bool(
        isinstance(jobs, list)
        and all(
            isinstance(job, dict)
            and isinstance(job.get("id"), str)
            and isinstance(job.get("cron"), str)
            and isinstance(job.get("command"), str)
            for job in jobs
        )
        and isinstance(locks, list)
        and all(
            isinstance(lock, dict)
            and isinstance(lock.get("name"), str)
            and lock.get("state") in {"active", "stale", "clean"}
            for lock in locks
        )
        and isinstance(findings, list)
        and all(
            isinstance(finding, dict)
            and isinstance(finding.get("name"), str)
            and type(finding.get("ok")) is bool
            for finding in findings
        )
        and isinstance(expected, dict)
    )

    lock_dir = _runtime_dir() / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    for lock in locks if isinstance(locks, list) else []:
        if lock["state"] == "clean":
            continue
        if Path(lock["name"]).name != lock["name"] or lock["name"].startswith("."):
            raise RuntimeError("bounded hardening lock name escaped its domain")
        lock_path = lock_dir / f"{lock['name']}.lock"
        lock_path.write_text("", encoding="utf-8")
        Path(str(lock_path) + ".json").write_text(
            json.dumps(
                {
                    "domain": lock["name"],
                    "owner": "bounded-fixture",
                    "pid": os.getpid() if lock["state"] == "active" else 99999999,
                    "started_at": "2026-07-17T00:00:00+00:00",
                },
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    provider = _FixtureExternalProvider(jobs if isinstance(jobs, list) else [])
    global _ACTIVE_FIXTURE_EXTERNAL_PROVIDER
    if _ACTIVE_FIXTURE_EXTERNAL_PROVIDER is not None:
        raise RuntimeError("hardening fixture provider is already bound")
    _ACTIVE_FIXTURE_EXTERNAL_PROVIDER = provider
    try:
        initial_report = run_operational_audit(cleanup_stale_locks=False)
        final_report = run_operational_audit(
            cleanup_stale_locks=cleanup_stale_locks
        )
    finally:
        _ACTIVE_FIXTURE_EXTERNAL_PROVIDER = None
    initial_lock_audit = initial_report["stale_runtime_locks"]
    final_lock_audit = final_report["stale_runtime_locks"]
    initial_red = _formal_red_count(initial_report)
    final_red = _formal_red_count(final_report)
    repaired_locks = sorted(
        str(item.get("domain") or "")
        for item in (final_lock_audit.get("cleanup") or {}).get("cleaned", [])
    )
    provider_evidence = provider.evidence()
    terminal_state = "green" if final_red == 0 else "red"
    audit_state = {
        "initial_red_count": initial_red,
        "final_red_count": final_red,
        "repaired_locks": repaired_locks,
        "terminal_state": terminal_state,
    }
    state_path = fixture.workspace / "operational_audit_state.json"
    state_path.write_text(
        json.dumps(audit_state, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checks = {
        "fixture_sample_bound": 1 <= fixture.sample_id <= 3,
        "typed_audit_fixture": typed,
        "cron_commands_parsed_by_product_policy": initial_report["cron"][
            "parse_failure_count"
        ]
        == int(expected.get("parse_failure_count", -1)),
        "cron_collisions_match_expected": initial_report["cron"]["collision_count"]
        == int(expected.get("collision_count", -1)),
        "initial_red_state_audited": initial_red
        == int(expected.get("initial_red_count", -1)),
        "stale_lock_repair_matches_expected": repaired_locks
        == expected.get("repaired_locks"),
        "audit_reached_expected_terminal_state": terminal_state
        == expected.get("terminal_state"),
        "fail_on_red_semantics_preserved": (not fail_on_red) or terminal_state == "green",
        "formal_audit_suite_executed": set(final_report)
        == {
            "cron",
            "runtime_root_consistency",
            "stale_runtime_locks",
            "domain_interference",
            "background_task_locks",
            "git",
            "issue_agenda",
            "gmail_monitor",
            "laf_gmail_fallback_job",
            "omlx_profile",
            "silent_exception_handlers",
            "retired_feature_residue",
            "osc_route_integrity",
        },
        "external_dependencies_isolated": provider_evidence["forbidden_calls"] == []
        and {
            "cron",
            "git",
            "process_table",
            "omlx",
            "osc_routes",
        }
        <= {str(call.get("provider")) for call in provider_evidence["calls"]},
        "external_provider_calls_terminal": provider_evidence["all_terminal"] is True,
    }
    success = all(checks.values())
    safety = safety_receipt(fixture, include_process=True)
    safety.update(
        {
            "model_provider": "fixture_model_probe",
            "database_provider": "fixture_database",
            "notification_provider": "fixture_notification",
            "dispatcher_invoked": False,
        }
    )
    report = {
        "schema": "magi.schedule-nonstorage-result/v1",
        "job_id": fixture.job_id,
        "fixture_sample_id": fixture.sample_id,
        "success": success,
        "status": "passed" if success else "failed",
        "terminal_state": terminal_state,
        "checks": checks,
        "audit": {
            "initial": {
                "report": initial_report,
                "red_count": initial_red,
            },
            "final": {
                "report": final_report,
                "red_count": final_red,
            },
            "repairs": {
                "locks": repaired_locks,
            },
        },
        "provider_observation": provider_evidence,
        "safety": safety,
    }
    output = write_fixture_report(fixture, raw_output, report)
    report["json_out"] = str(output)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if success else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", default=str(_runtime_dir() / "operational_hardening_audit_latest.json"))
    parser.add_argument("--cleanup-stale-locks", action="store_true")
    parser.add_argument("--fail-on-red", action="store_true")
    parser.add_argument("--schedule-fixture-root")
    args = parser.parse_args()

    if args.schedule_fixture_root:
        return _run_schedule_fixture(
            args.schedule_fixture_root,
            args.json_out,
            cleanup_stale_locks=args.cleanup_stale_locks,
            fail_on_red=args.fail_on_red,
        )

    report = run_operational_audit(cleanup_stale_locks=args.cleanup_stale_locks)
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "cron_parse_failures": report["cron"]["parse_failure_count"],
        "cron_collisions": report["cron"]["collision_count"],
        "runtime_root_mismatch_count": report["runtime_root_consistency"]["mismatch_count"],
        "stale_runtime_lock_count": report["stale_runtime_locks"]["stale_count"],
        "domain_interference_count": report["domain_interference"]["issue_count"],
        "background_task_locks_ok": report["background_task_locks"]["ok"],
        "dirty_count": report["git"]["dirty_count"],
        "recent_issues": int(report["issue_agenda"].get("active_count") or 0),
        "historical_recent_issues": int(report["issue_agenda"].get("recent_count") or 0),
        "inactive_or_context_issues": int(report["issue_agenda"].get("inactive_or_context_count") or 0),
        "gmail_monitor_mode": report["gmail_monitor"]["mode"],
        "laf_gmail_fallback_job_ok": report["laf_gmail_fallback_job"]["ok"],
        "omlx_profile_ok": report["omlx_profile"]["ok"],
        "omlx_expected": report["omlx_profile"]["expected_profile"],
        "omlx_models": report["omlx_profile"]["models"],
        "silent_exception_critical_count": report["silent_exception_handlers"]["critical_count"],
        "retired_feature_residue_ok": report["retired_feature_residue"]["ok"],
        "osc_route_integrity_ok": report["osc_route_integrity"]["ok"],
        "json_out": str(out),
    }, ensure_ascii=False))

    if args.fail_on_red and _formal_red_reasons(report):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
