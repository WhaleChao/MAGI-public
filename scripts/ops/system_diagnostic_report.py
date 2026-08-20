#!/usr/bin/env python3
"""Emit a bounded, deterministic MAGI system diagnostic report.

This replaces the legacy natural-language ``@MAGI`` diagnostic cron macro.
It performs only localhost/read-only probes and returns a machine-readable
success contract so the scheduler can distinguish a completed report from a
model clarification response.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
DEFAULT_HEALTH_URLS = (
    "http://127.0.0.1:5002/health",
    "http://127.0.0.1:5003/health",
    "http://127.0.0.1:8088/health",
    "http://127.0.0.1:5014/health",
)


def _health_urls() -> tuple[str, ...]:
    raw = os.environ.get("MAGI_DIAGNOSTIC_HEALTH_URLS", "").strip()
    if not raw:
        return DEFAULT_HEALTH_URLS
    urls = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not urls or any(not item.startswith("http://127.0.0.1:") for item in urls):
        raise ValueError("diagnostic health URLs must be localhost HTTP endpoints")
    return urls


def _probe_url(
    url: str,
    *,
    timeout: float = 3.0,
    attempts: int = 3,
) -> dict[str, Any]:
    """Probe a localhost service with bounded transient retries.

    The 8088 control endpoint performs a fail-closed ownership audit.  A
    single slow macOS process/listener observation must not turn a healthy
    service into a daily red light, while repeated failures still fail closed.
    """

    last_error: BaseException | None = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            request = urllib.request.Request(
                url,
                method="GET",
                headers={"User-Agent": "MAGI-diagnostic/1"},
            )
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response.read(4096)
                status = int(response.status)
            return {
                "url": url,
                "ok": 200 <= status < 300,
                "http_status": status,
                "attempts": attempt,
            }
        except (OSError, urllib.error.URLError, ValueError) as exc:
            last_error = exc
    return {
        "url": url,
        "ok": False,
        "error_type": type(last_error).__name__ if last_error is not None else "UnknownError",
        "attempts": max(1, int(attempts)),
    }


def _memory_free_percent() -> float | None:
    try:
        result = subprocess.run(
            ["/usr/bin/memory_pressure"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (result.stdout or "").splitlines():
        if "System-wide memory free percentage:" not in line:
            continue
        try:
            return float(line.rsplit(":", 1)[1].strip().rstrip("%"))
        except ValueError:
            return None
    return None


def _command_references_root(command: str, root: Path) -> bool:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return False
    normalized_root = os.path.normpath(str(root))
    prefix = normalized_root + os.sep
    for token in tokens:
        candidate = token.split("=", 1)[1] if "=" in token else token
        if not os.path.isabs(candidate):
            continue
        normalized = os.path.normpath(candidate)
        if normalized == normalized_root or normalized.startswith(prefix):
            return True
    return False


def _rss_summary() -> dict[str, Any]:
    manifest = os.environ.get("MAGI_V3_RELEASE_MANIFEST", "").strip()
    active_root = (
        Path(manifest).expanduser().parent
        if manifest
        else REPO_ROOT
    )
    active_root_text = str(active_root)
    active_group = (
        "magi_v3"
        if (
            os.environ.get("MAGI_V3_RELEASE_ID", "").strip()
            or "MAGI_v3" in active_root.parts
            or "MAGI_v3_candidates" in active_root.parts
            or active_root.name.startswith("v3-")
        )
        else "magi_v2"
    )
    groups = {"magi_v2": 0, "magi_v3": 0, "omlx": 0, "input_method": 0}
    counts = {key: 0 for key in groups}
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "rss=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "groups": {}}
    if result.returncode != 0:
        return {"available": False, "groups": {}}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        try:
            rss_kb = int(parts[0])
        except ValueError:
            continue
        command = parts[1]
        key = None
        if active_root_text and _command_references_root(command, active_root):
            key = active_group
        elif "omlx serve" in command:
            key = "omlx"
        elif command.endswith("/McBopomofo.app/Contents/MacOS/McBopomofo"):
            key = "input_method"
        if key:
            groups[key] += rss_kb
            counts[key] += 1
    return {
        "available": True,
        "active_release_group": active_group,
        "active_release_root_sha256": hashlib.sha256(
            active_root_text.encode("utf-8")
        ).hexdigest(),
        "groups": {
            key: {"processes": counts[key], "rss_mb": round(groups[key] / 1024, 1)}
            for key in groups
        },
    }


def _load_json(path: Path, expected: type) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return expected()
    return value if isinstance(value, expected) else expected()


def _parse_state_time(value: Any) -> datetime | None:
    """Return a comparable aware datetime for mixed legacy cron timestamps.

    Older scheduler state used local naive ISO strings while newer state uses
    explicit UTC offsets.  Treating both formats as valid input is required
    during a rolling upgrade, but Python deliberately refuses to compare them
    directly.  Naive values represent the host's local wall clock, so attach
    the host timezone before comparing them with offset-aware values.
    """

    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def _schedule_summary(
    cron_jobs_path: Path,
    cron_state_path: Path,
    *,
    verified_jobs: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    jobs = list(verified_jobs) if verified_jobs is not None else _load_json(cron_jobs_path, list)
    state = _load_json(cron_state_path, dict)
    enabled = [job for job in jobs if isinstance(job, dict) and job.get("enabled") is True]
    failures: list[str] = []
    deferred: list[str] = []
    auto_repair: list[str] = []
    for job in enabled:
        job_id = str(job.get("id") or "")
        latest = state.get(job_id)
        if not job_id or not isinstance(latest, dict) or latest.get("last_run") is None:
            continue
        last_start = _parse_state_time(latest.get("last_start_at"))
        last_complete = _parse_state_time(latest.get("last_complete_at"))
        if last_start is not None and (
            last_complete is None or last_start > last_complete
        ):
            # The diagnostic job sees its own state after the scheduler has
            # marked it started.  Do not inherit a previous failed result while
            # the current, hash-bound invocation is still in progress.
            continue
        state_text = "\n".join(
            str(latest.get(key) or "")
            for key in ("last_status", "last_error", "status", "error")
        ).lower()
        retry = latest.get("v3_retry")
        retry_status = (
            str(retry.get("status") or "").strip().lower()
            if isinstance(retry, dict)
            else ""
        )
        if retry_status in {"queued", "running"}:
            auto_repair.append(job_id)
            continue
        if "deferred" in state_text or "resource_guard_skipped" in state_text:
            deferred.append(job_id)
            continue
        if "definition_drift" in state_text:
            failures.append(job_id)
            continue
        # A dispatch may have ``last_run`` before a result exists.  Only an
        # explicit false result is a failure; missing legacy/in-flight state is
        # not evidence of failure.
        if latest.get("last_success") is False:
            failures.append(job_id)
    return {
        "definitions": len(jobs),
        "enabled": len(enabled),
        "failed_job_ids": sorted(failures),
        "failed": len(failures),
        "deferred_job_ids": sorted(deferred),
        "deferred": len(deferred),
        "auto_repair_job_ids": sorted(auto_repair),
        "auto_repair": len(auto_repair),
    }


def collect_report(*, repo_root: Path = REPO_ROOT) -> dict[str, Any]:
    from magi_v3.external_inputs import load_bound_cron_jobs

    probes = [_probe_url(url) for url in _health_urls()]
    memory_free = _memory_free_percent()
    usage = shutil.disk_usage(repo_root)
    cron_binding = load_bound_cron_jobs(repo_root)
    schedule = _schedule_summary(
        cron_binding.path,
        Path(
            os.environ.get("MAGI_CRON_STATE_FILE", "").strip()
            or Path(os.environ.get("MAGI_RUNTIME_DIR", "").strip() or repo_root / ".runtime") / "cron_state.json"
        ),
        verified_jobs=cron_binding.jobs,
    )
    core_ok = bool(probes) and all(item["ok"] for item in probes)
    warnings: list[str] = []
    if memory_free is not None and memory_free < 15:
        warnings.append("memory_free_below_15_percent")
    if usage.free < 10 * 1024**3:
        warnings.append("disk_free_below_10_gib")
    if schedule["failed"]:
        warnings.append("enabled_schedule_failures_present")
    if schedule["deferred"]:
        warnings.append("enabled_schedule_deferred_present")
    return {
        "schema_version": 1,
        "success": core_ok,
        "status": "healthy" if core_ok and not warnings else ("warning" if core_ok else "failed"),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "localhost_health": probes,
        "resources": {
            "memory_free_percent": memory_free,
            "disk_free_gib": round(usage.free / 1024**3, 2),
            "rss": _rss_summary(),
        },
        "schedule": schedule,
        "warnings": warnings,
        "network_scope": "localhost_only",
        "production_write_performed": False,
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args(argv)
    try:
        report = collect_report()
    except ValueError as exc:
        report = {"schema_version": 1, "success": False, "status": "failed", "error": str(exc)}
    if args.json_out:
        _write_atomic(args.json_out, report)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("success") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
