#!/usr/bin/env python3
"""Beginner-friendly MAGI detection wizard."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import plistlib
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LIVE_RUNTIME_ROOT = (
    Path.home() / "Library" / "Application Support" / "MAGI" / "runtime" / "MAGI_v3"
)
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
_RUNTIME_ROOT_FINGERPRINT_FILES = (
    "api/server.py",
    "api/discord_bot.py",
    "api/tools_api.py",
    "scripts/magi_doctor.py",
    "scripts/ops/magi_acceptance_gate.py",
    "scripts/ops/business_module_live_check.py",
    "scripts/ops/run_after_token_refresh.py",
    "gui/magi_menubar.py",
    "casper_ecosystem/law_firm_orchestrators/laf_automation_v2.py",
    "casper_ecosystem/law_firm_orchestrators/file_review_automation.py",
    "skills/laf-orchestrator/action.py",
    "skills/file-review-orchestrator/action.py",
    "skills/transcript-downloader/action.py",
    "config/test_matrix.json",
)
_RUNTIME_ROOT_GOOGLE_CRON_JOBS = {
    "job_accounting_sheet_import",
    "job_accounting_monthly_bonus",
    "job_drive_case_sync_bidirectional",
    "job_drive_case_sync_all_files",
    "job_osc_events_refresh",
    "job_osc_todo_governance",
    "job_api_token_health_check",
}
_V3_RETIRED_LAUNCHD_LABELS = {
    "com.magi.daemon",
    "com.magi.log-rotate",
    "com.magi.menubar",
    "com.magi.omlx-restore",
}


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _project_python() -> Path | None:
    bound_runtime = os.environ.get("MAGI_V3_PYTHON_RUNTIME", "").strip()
    if bound_runtime:
        candidate = Path(bound_runtime).expanduser()
        expected_sha = os.environ.get(
            "MAGI_V3_PYTHON_RUNTIME_SHA256", ""
        ).strip().lower()
        try:
            actual_sha = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            actual_sha = ""
        if (
            candidate.exists()
            and len(expected_sha) == 64
            and actual_sha == expected_sha
        ):
            return candidate
    candidates = [
        REPO_ROOT / ".venv" / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python"),
        REPO_ROOT / "venv" / ("Scripts/python.exe" if platform.system() == "Windows" else "bin/python"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _package_available(module_name: str) -> bool:
    if importlib.util.find_spec(module_name) is not None:
        return True

    project_python = _project_python()
    if not project_python or Path(sys.executable).absolute() == project_python.absolute():
        return False

    probe = (
        "import importlib.util, sys; "
        f"sys.exit(0 if importlib.util.find_spec({module_name!r}) else 1)"
    )
    try:
        return subprocess.run(
            [str(project_python), "-c", probe],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).returncode == 0
    except Exception:
        return False


def _disk_free_gb(path: Path) -> float:
    usage = shutil.disk_usage(path)
    return round(usage.free / (1024 ** 3), 1)


def _ram_gb() -> float:
    try:
        import psutil

        return round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        if platform.system() == "Darwin":
            try:
                import subprocess

                raw = subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True).strip()
                return round(int(raw) / (1024 ** 3), 1)
            except Exception:
                return 0.0
        return 0.0


def _http_json(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read(4096).decode("utf-8", errors="replace")
            return 200 <= resp.status < 300, body
    except urllib.error.URLError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, str(exc)


def _mtp_sidecar_check() -> tuple[bool, str]:
    """Confirm the local MTP sidecar across transient restart/warm-up delays."""

    last_detail = ""
    for attempt in range(4):
        ok, detail = _http_json("http://127.0.0.1:8090/health", timeout=3.0)
        if ok:
            suffix = "" if attempt == 0 else f"; retry={attempt}"
            return True, detail + suffix
        last_detail = detail
        if attempt < 3:
            time.sleep(1)
    return False, last_detail


def _mtp_sidecar_required() -> bool:
    """MTP is optional unless draft acceleration or a strict probe is enabled."""
    truthy = {"1", "true", "yes", "on"}
    return (
        os.environ.get("MAGI_ENABLE_MTP_DRAFT", "0").strip().lower() in truthy
        or os.environ.get("MAGI_REQUIRE_MLX_MTP", "0").strip().lower() in truthy
    )


def _runtime_dir() -> Path:
    raw = os.environ.get("MAGI_RUNTIME_DIR", "").strip()
    return Path(raw).expanduser() if raw else REPO_ROOT / ".runtime"


def _live_runtime_root() -> Path:
    # V3 release launch agents expose the immutable release through MAGI_ROOT
    # / MAGI_ROOT_DIR.  These hash-bound values describe the process that is
    # actually running, so they must take precedence over a legacy
    # MAGI_LIVE_RUNTIME_ROOT left in the mutable external environment.  The
    # latter can otherwise survive several upgrades and make Doctor compare a
    # current release with an old release, producing false drift and Menubar
    # warnings even though every LaunchAgent is correctly rebound.
    release_id = os.environ.get("MAGI_V3_RELEASE_ID", "").strip()
    if release_id.startswith("v3-"):
        for key in ("MAGI_ROOT_DIR", "MAGI_ROOT"):
            candidate = os.environ.get(key, "").strip()
            path = Path(candidate).expanduser() if candidate else None
            if (
                path is not None
                and "/releases/v3-" in path.as_posix()
                and path.name == release_id
            ):
                return path

    raw = os.environ.get("MAGI_LIVE_RUNTIME_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_LIVE_RUNTIME_ROOT


def _v3_release_active(live_root: Path | None = None) -> bool:
    release_id = os.environ.get("MAGI_V3_RELEASE_ID", "").strip()
    live = _safe_absolute(live_root or _live_runtime_root())
    return release_id.startswith("v3-") and "/releases/v3-" in live.as_posix()


def _safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except Exception:
        return path.expanduser().absolute()


def _safe_absolute(path: Path) -> Path:
    try:
        return path.expanduser().absolute()
    except Exception:
        return path.expanduser()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        return path.is_relative_to(root)
    except AttributeError:
        try:
            path.relative_to(root)
            return True
        except Exception:
            return False
    except Exception:
        return False


def _path_under(path: Path, root: Path) -> bool:
    # Check both the literal path and the resolved target.  Virtualenv
    # interpreters are often symlinks to Homebrew/Python.org binaries; the
    # launchagent should be allowed when the symlink entry itself lives inside
    # the active MAGI root.
    return _is_relative_to(_safe_absolute(path), _safe_absolute(root)) or _is_relative_to(
        _safe_resolve(path),
        _safe_resolve(root),
    )


def _install_support_root_for(root: Path) -> Path | None:
    raw = _safe_absolute(root)
    if raw.name == "MAGI_v3" and raw.parent.name == "runtime":
        return raw.parent.parent
    return None


def _launchagent_allowed_roots(repo_root: Path, live_root: Path) -> list[Path]:
    roots = [_safe_absolute(repo_root), _safe_absolute(live_root)]
    for root in list(roots):
        install_root = _install_support_root_for(root)
        if install_root and install_root.exists():
            roots.append(install_root)
    env_root = os.environ.get("MAGI_INSTALL_ROOT", "").strip()
    if env_root:
        roots.append(Path(env_root).expanduser())
    default_install = DEFAULT_LIVE_RUNTIME_ROOT.parent.parent
    if default_install.exists():
        roots.append(default_install)

    out: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(_safe_absolute(root))
        if key and key not in seen:
            seen.add(key)
            out.append(root)
    return out


def _launchagent_disabled(payload: dict[str, Any]) -> bool:
    return bool(payload.get("Disabled") is True)


def _launchagent_expects_launchctl(payload: dict[str, Any]) -> bool:
    if _launchagent_disabled(payload):
        return False
    return bool(
        payload.get("KeepAlive")
        or payload.get("RunAtLoad")
        or payload.get("StartInterval")
        or payload.get("StartCalendarInterval")
    )


def _split_path_entries(value: str) -> list[str]:
    return [item for item in str(value or "").split(os.pathsep) if item.strip()]


def _launchagent_env_root_warnings(payload: dict[str, Any], *, repo_root: Path, live_root: Path) -> list[str]:
    env = payload.get("EnvironmentVariables") or {}
    if not isinstance(env, dict):
        return []
    repo = _safe_resolve(repo_root)
    live = _safe_resolve(live_root)
    if not live.exists() or live == repo:
        return []

    warnings: list[str] = []
    for key in ("MAGI_ROOT", "MAGI_ROOT_DIR", "MAGI_RUNTIME_DIR"):
        raw = str(env.get(key) or "").strip()
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if _path_under(candidate, repo) and not _path_under(candidate, live):
            warnings.append(f"{key} points to source root: {raw}")

    for entry in _split_path_entries(str(env.get("PYTHONPATH") or "")):
        if "magi" not in entry.lower():
            continue
        candidate = Path(entry).expanduser()
        if _path_under(candidate, repo) and not _path_under(candidate, live):
            warnings.append(f"PYTHONPATH points to source root: {entry}")
    return warnings


def _launchctl_print_status(label: str, *, uid: int | None = None) -> dict[str, Any]:
    if platform.system() != "Darwin" or not shutil.which("launchctl"):
        return {"checked": False, "reason": "launchctl unavailable"}
    target_uid = uid if uid is not None else os.getuid()
    target = f"gui/{target_uid}/{label}"
    try:
        result = subprocess.run(
            ["launchctl", "print", target],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except Exception as exc:
        return {"checked": True, "loaded": False, "error": str(exc)}
    output = result.stdout or ""
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().replace("\n", " ")
        return {"checked": True, "loaded": False, "detail": detail[:240], "returncode": result.returncode}

    def match(pattern: str) -> str:
        found = re.search(pattern, output, flags=re.MULTILINE)
        return found.group(1).strip() if found else ""

    return {
        "checked": True,
        "loaded": True,
        "state": match(r"^\s*state = ([^\n]+)$"),
        "pid": match(r"^\s*pid = ([^\n]+)$"),
        "runs": match(r"^\s*runs = ([^\n]+)$"),
        "last_exit_code": match(r"^\s*last exit code = ([^\n]+)$"),
        "last_terminating_signal": match(r"^\s*last terminating signal = ([^\n]+)$"),
    }


def _direct_menubar_process(payload: dict[str, Any]) -> dict[str, Any] | None:
    working_dir = str(payload.get("WorkingDirectory") or "").strip()
    if not working_dir:
        return None
    expected_script = (Path(working_dir).expanduser() / "gui" / "magi_menubar.py").resolve()
    if not expected_script.exists():
        return None
    try:
        import psutil

        matches = []
        for proc in psutil.process_iter(["pid", "cmdline"]):
            try:
                cmdline = [str(arg) for arg in (proc.info.get("cmdline") or [])]
                if any(
                    Path(arg).expanduser().resolve() == expected_script
                    for arg in cmdline
                    if arg.endswith("magi_menubar.py")
                ):
                    matches.append(int(proc.info["pid"]))
            except (OSError, psutil.Error):
                continue
        if matches:
            return {"pid": min(matches), "count": len(matches), "script": str(expected_script)}
    except Exception:
        # psutil is optional in the sealed runtime used by the acceptance
        # process.  Fall back to macOS' read-only process inventory instead of
        # turning a visibly running menu bar into a Doctor warning.
        pass
    try:
        completed = subprocess.run(
            ["/bin/ps", "-axo", "pid=,command="],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        matches = _menubar_pids_from_ps_output(completed.stdout, expected_script)
        if completed.returncode == 0 and matches:
            return {"pid": min(matches), "count": len(matches), "script": str(expected_script)}
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _menubar_pids_from_ps_output(output: str, expected_script: Path) -> list[int]:
    expected = str(expected_script)
    matches: list[int] = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, separator, command = stripped.partition(" ")
        if not separator or not pid_text.isdigit():
            continue
        if expected in command and "magi_menubar.py" in command:
            matches.append(int(pid_text))
    return matches


def _launchctl_check(label: str, payload: dict[str, Any]) -> Check | None:
    if not _launchagent_expects_launchctl(payload):
        return None
    status = _launchctl_print_status(label)
    if not status.get("checked"):
        return None
    name = f"launchctl:{label}"
    if not status.get("loaded"):
        if label == "com.magi.menubar":
            direct = _direct_menubar_process(payload)
            if direct:
                return Check(
                    name,
                    "pass",
                    f"direct GUI fallback running; pid={direct['pid']}; count={direct['count']}",
                )
        detail = str(status.get("detail") or status.get("error") or "not loaded")
        return Check(name, "warn", f"not loaded: {detail}", "載入或停用該 LaunchAgent，避免 plist 殘留造成監控誤判。")

    state = str(status.get("state") or "unknown")
    pid = str(status.get("pid") or "-")
    bits = [f"state={state}", f"pid={pid}"]
    if status.get("runs"):
        bits.append(f"runs={status['runs']}")
    if status.get("last_exit_code"):
        bits.append(f"last_exit={status['last_exit_code']}")
    if status.get("last_terminating_signal"):
        bits.append(f"last_signal={status['last_terminating_signal']}")

    if payload.get("KeepAlive") and state != "running":
        return Check(name, "warn", "; ".join(bits), "KeepAlive 服務應維持 running；確認 launchctl 狀態或停用舊 plist。")
    return Check(name, "pass", "; ".join(bits))


def _launchagent_workdir_candidate(path: str, allowed_roots: list[Path]) -> bool:
    text = str(path or "").strip()
    if not text:
        return False
    p = Path(text).expanduser()
    if any(_path_under(p, root) for root in allowed_roots):
        return True
    lower = text.lower()
    return "magi" in lower or "magi_v2" in lower


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _runtime_root_fingerprint(repo_root: Path, live_root: Path) -> dict[str, Any]:
    file_mismatches: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for rel in _RUNTIME_ROOT_FINGERPRINT_FILES:
        src = repo_root / rel
        live = live_root / rel
        if not src.exists() or not live.exists():
            missing.append({"file": rel, "source_exists": src.exists(), "runtime_exists": live.exists()})
            continue
        try:
            src_hash = _sha256_file(src)
            live_hash = _sha256_file(live)
        except OSError:
            missing.append({"file": rel, "source_exists": src.exists(), "runtime_exists": live.exists()})
            continue
        if src_hash != live_hash:
            file_mismatches.append({"file": rel, "source": src_hash[:12], "runtime": live_hash[:12]})

    cron_mismatches: list[dict[str, Any]] = []
    try:
        from scripts.ops.business_module_live_check import _cron_semantic_map

        source_cron = _cron_semantic_map(repo_root)
        runtime_cron = _cron_semantic_map(live_root)
        for job_id in sorted(_RUNTIME_ROOT_GOOGLE_CRON_JOBS):
            source_job = source_cron.get(job_id)
            runtime_job = runtime_cron.get(job_id)
            if source_job != runtime_job:
                cron_mismatches.append({"id": job_id, "source": source_job or {}, "runtime": runtime_job or {}})
    except Exception as exc:
        cron_mismatches.append({"id": "_cron_fingerprint_error", "error": str(exc)[:200]})

    return {"file_mismatches": file_mismatches, "missing": missing, "cron_mismatches": cron_mismatches}


def _parse_dt(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            # cron_state.json is written by the local scheduler without an
            # offset.  Treat those values as host-local time before comparing
            # them with UTC artifact mtimes.
            local_now = datetime.now()
            local_tz = local_now.tzinfo or local_now.astimezone().tzinfo or timezone.utc
            dt = dt.replace(tzinfo=local_tz)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _age_hours(dt: datetime | None, now: datetime) -> float | None:
    if dt is None:
        return None
    return round(max(0.0, (now - dt.astimezone(timezone.utc)).total_seconds() / 3600.0), 3)


def _runtime_root_checks(*, repo_root: Path = REPO_ROOT, live_root: Path | None = None) -> list[Check]:
    live = live_root or _live_runtime_root()
    checks: list[Check] = []
    repo_resolved = _safe_resolve(repo_root)
    live_resolved = _safe_resolve(live)
    if live.exists() and live_resolved != repo_resolved:
        fingerprint = _runtime_root_fingerprint(repo_root, live)
        problem_bits = [
            f"file_mismatches={len(fingerprint['file_mismatches'])}",
            f"missing={len(fingerprint['missing'])}",
            f"cron_mismatches={len(fingerprint['cron_mismatches'])}",
        ]
        if not any(fingerprint.values()):
            checks.append(
                Check(
                    "runtime_root_drift",
                    "pass",
                    f"source={repo_resolved}; live_runtime_root={live_resolved}; fingerprint=matched",
                )
            )
            return checks
        checks.append(
            Check(
                "runtime_root_drift",
                "warn",
                f"source={repo_resolved}; live_runtime_root={live_resolved}; {'; '.join(problem_bits)}",
                "確認目前修改的是 daemon 實際載入的 runtime copy，或同步部署 source→runtime。",
            )
        )
    else:
        checks.append(Check("runtime_root_drift", "pass", f"source={repo_resolved}"))
    return checks


def _launchagent_checks(
    *,
    home: Path | None = None,
    repo_root: Path = REPO_ROOT,
    live_root: Path | None = None,
    check_launchctl: bool | None = None,
) -> list[Check]:
    effective_home = home or Path.home()
    launch_dir = effective_home / "Library" / "LaunchAgents"
    if not launch_dir.exists():
        return [Check("launchagents", "warn", f"{launch_dir} missing", "若使用 launchd，確認 LaunchAgent 已安裝。")]

    live = live_root or _live_runtime_root()
    allowed_roots = _launchagent_allowed_roots(repo_root, live)

    checks: list[Check] = []
    found = 0
    for plist_path in sorted(launch_dir.glob("*.plist")):
        try:
            payload = plistlib.loads(plist_path.read_bytes())
        except Exception as exc:
            if "magi" in plist_path.name.lower():
                checks.append(Check(f"launchagent:{plist_path.name}", "fail", f"plist parse failed: {exc}", "修正 plist XML。"))
            continue
        label = str(payload.get("Label") or plist_path.stem)
        args = payload.get("ProgramArguments") or []
        if not isinstance(args, list):
            args = []
        wd = str(payload.get("WorkingDirectory") or "").strip()
        joined = " ".join(str(x) for x in args)
        if "magi" not in f"{plist_path.name} {label} {joined} {wd}".lower():
            continue
        found += 1
        candidate_paths = []
        if wd and _launchagent_workdir_candidate(wd, allowed_roots):
            candidate_paths.append(Path(wd).expanduser())
        for arg in args:
            text = str(arg).strip()
            tokens = [text]
            is_direct_path_arg = text.startswith(("/", "~")) and not any(op in text for op in ("&&", "||", ";"))
            if not is_direct_path_arg and any(sep in text for sep in (" ", "\t", "\"", "'")):
                try:
                    tokens = shlex.split(text)
                except ValueError:
                    tokens = [text]
            for token in tokens:
                if "/" in token and (
                    token.endswith((".py", ".sh"))
                    or "MAGI" in token
                    or "MAGI_v2" in token
                ):
                    candidate_paths.append(Path(token).expanduser())
        missing = [str(p) for p in candidate_paths if not p.exists()]
        outside = [
            str(p)
            for p in candidate_paths
            if p.exists() and not any(_path_under(p, root) for root in allowed_roots)
        ]
        name = f"launchagent:{label}"
        if missing:
            checks.append(Check(name, "fail", f"missing path(s): {', '.join(missing[:3])}", "更新 ProgramArguments/WorkingDirectory 指向現行 MAGI root。"))
            continue
        elif outside:
            checks.append(Check(name, "warn", f"path outside active roots: {', '.join(outside[:3])}", "確認 launchd 沒有指向舊 source/runtime root。"))
            continue
        elif _launchagent_disabled(payload):
            checks.append(Check(name, "pass", "disabled plist"))
        else:
            checks.append(Check(name, "pass", wd or joined[:180] or str(plist_path)))

        env_warnings = _launchagent_env_root_warnings(payload, repo_root=repo_root, live_root=live)
        if env_warnings:
            checks.append(Check(f"{name}:env", "warn", "; ".join(env_warnings[:3]), "更新 LaunchAgent EnvironmentVariables 指向 live runtime root。"))

        should_check_launchctl = check_launchctl if check_launchctl is not None else home is None
        if _v3_release_active(live) and label in _V3_RETIRED_LAUNCHD_LABELS:
            should_check_launchctl = False
        if should_check_launchctl:
            launchctl_check = _launchctl_check(label, payload)
            if launchctl_check:
                checks.append(launchctl_check)
    if _v3_release_active(live) and (check_launchctl is True or (check_launchctl is None and home is None)):
        direct = _direct_menubar_process({"WorkingDirectory": str(live)})
        if direct:
            checks.append(
                Check(
                    "launchctl:com.magi.menubar",
                    "pass",
                    f"V3 supervisor child running; pid={direct['pid']}; count={direct['count']}",
                )
            )
        else:
            checks.append(
                Check(
                    "launchctl:com.magi.menubar",
                    "warn",
                    "V3 supervisor child not found",
                    "確認 com.magi.v3.supervisor 與 MAGI 選單列程序。",
                )
            )
    if found == 0:
        checks.append(Check("launchagents", "warn", "no MAGI LaunchAgent plist found", "若由 launchd 管理，確認 plist label/路徑含 MAGI。"))
    return checks


def _cron_job_enabled_map(cron_jobs_path: Path) -> dict[str, bool] | None:
    jobs = _read_json(cron_jobs_path, None)
    if not isinstance(jobs, list):
        return None
    return {
        str(job.get("id") or "").strip(): bool(job.get("enabled", True))
        for job in jobs
        if isinstance(job, dict) and str(job.get("id") or "").strip()
    }


def _cron_job_timeout_map(cron_jobs_path: Path) -> dict[str, int] | None:
    """Return the bound execution timeout for each cron owner.

    Doctor used to classify a previous non-zero result as the current state
    while a newly dispatched long-running owner was still active.  Keep the
    timeout alongside the enabled map so the same in-progress contract used
    by function_health_index can be applied here as well.
    """

    jobs = _read_json(cron_jobs_path, None)
    if not isinstance(jobs, list):
        return None
    timeouts: dict[str, int] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            continue
        try:
            timeouts[job_id] = max(0, int(job.get("timeout_sec") or 0))
        except (TypeError, ValueError):
            timeouts[job_id] = 0
    return timeouts


def _cron_job_command_sha_map(jobs: list[dict[str, Any]]) -> dict[str, str]:
    """Return canonical command identities for the currently bound schedule."""

    from skills.ops.cron_command_identity import command_definition_sha256

    identities: dict[str, str] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            continue
        try:
            identities[job_id] = command_definition_sha256(job)
        except Exception:
            continue
    return identities


def _cron_run_within_timeout(
    state_item: dict[str, Any], *, now: datetime, timeout_sec: int
) -> bool:
    """Recognize a fresh dispatch whose completion has not been recorded."""

    if timeout_sec <= 0:
        return False
    started = _parse_dt(
        state_item.get("last_start_at")
        or state_item.get("last_started_at")
        or state_item.get("last_dispatch_at")
        or state_item.get("last_run")
    )
    completed = _parse_dt(
        state_item.get("last_complete_at") or state_item.get("last_result_at")
    )
    if started is None or started > now:
        return False
    if completed is not None and completed >= started:
        return False
    return (now - started).total_seconds() <= max(60, timeout_sec)


def _operational_audit_recovered_after_failure(runtime_dir: Path, state_item: dict) -> bool:
    failed_at = _parse_dt(state_item.get("last_failure_at") or state_item.get("last_result_at"))
    artifact = runtime_dir / "operational_hardening_audit_latest.json"
    if failed_at is None or not artifact.exists():
        return False
    try:
        artifact_at = datetime.fromtimestamp(artifact.stat().st_mtime, tz=timezone.utc)
    except Exception:
        return False
    if artifact_at <= failed_at:
        return False
    payload = _read_json(artifact, {})
    cron = payload.get("cron") if isinstance(payload, dict) else {}
    gmail = payload.get("gmail_monitor") if isinstance(payload, dict) else {}
    return (
        isinstance(cron, dict)
        and int(cron.get("parse_failure_count") or 0) == 0
        and int(cron.get("collision_count") or 0) == 0
        and (not isinstance(gmail, dict) or gmail.get("ok") is not False)
    )


def _omlx_switch_recovered_after_failure(
    runtime_dir: Path,
    job_id: str,
    state_item: dict,
) -> bool:
    from magi_v3.model_recovery import assess_omlx_recovery

    failed_at = (
        state_item.get("last_failure_at")
        or state_item.get("last_result_at")
        or state_item.get("last_complete_at")
    )
    return bool(
        assess_omlx_recovery(
            runtime_dir,
            job_id=job_id,
            failed_at=failed_at,
        ).get("recovered")
    )


def _omlx_superseded_release_recovered_after_failure(
    runtime_dir: Path,
    job_id: str,
    state_item: dict,
    *,
    current_command_sha256: str,
) -> bool:
    """Apply the same release-bound recovery contract as function health."""

    from scripts.ops.function_health_index import (
        _cron_omlx_superseded_failure_recovered,
    )

    return _cron_omlx_superseded_failure_recovered(
        runtime_dir,
        job_id,
        state_item,
        current_command_sha256=current_command_sha256,
    )


def _cron_resource_protection_active(
    job_id: str,
    state_item: dict,
    *,
    returncode: int | None,
    timed_out: bool,
) -> bool:
    """Treat the governor's structured critical result as safe degradation."""
    if job_id != "job_resource_governor" or timed_out or returncode != 2:
        return False
    result_text = "\n".join(
        str(state_item.get(key) or "")
        for key in (
            "last_error",
            "last_stdout_tail",
            "last_stderr_tail",
            "stdout",
            "stderr",
        )
    ).lower()
    return (
        ('"ok": false' in result_text or "'ok': false" in result_text)
        and (
            '"level": "critical"' in result_text
            or "'level': 'critical'" in result_text
        )
        and any(
            marker in result_text
            for marker in (
                "free_plus_inactive",
                "disk_free_gb",
                "swap_used_gb",
                "pause_heavy_backlog_jobs",
            )
        )
    )


def _cron_state_checks(
    *,
    runtime_dir: Path | None = None,
    cron_jobs_path: Path | None = None,
    now: datetime | None = None,
    max_age_hours: float = 36.0,
) -> list[Check]:
    runtime = runtime_dir or _runtime_dir()
    if cron_jobs_path is not None:
        explicit_jobs = _read_json(cron_jobs_path, [])
        if not isinstance(explicit_jobs, list):
            explicit_jobs = []
        job_enabled = _cron_job_enabled_map(cron_jobs_path)
        job_timeouts = _cron_job_timeout_map(cron_jobs_path)
        job_command_sha256 = _cron_job_command_sha_map(explicit_jobs)
        job_descriptions = {
            str(job.get("id") or "").strip(): str(job.get("desc") or job.get("id") or "").strip()
            for job in explicit_jobs
            if isinstance(job, dict) and str(job.get("id") or "").strip()
        }
    else:
        try:
            from magi_v3.external_inputs import load_bound_cron_jobs

            bound_jobs = list(load_bound_cron_jobs(REPO_ROOT).jobs)
            job_enabled = {
                str(job.get("id") or "").strip(): bool(job.get("enabled", True))
                for job in bound_jobs
                if str(job.get("id") or "").strip()
            }
            job_timeouts = {
                str(job.get("id") or "").strip(): max(
                    0, int(job.get("timeout_sec") or 0)
                )
                for job in bound_jobs
                if str(job.get("id") or "").strip()
            }
            job_command_sha256 = _cron_job_command_sha_map(bound_jobs)
            job_descriptions = {
                str(job.get("id") or "").strip(): str(job.get("desc") or job.get("id") or "").strip()
                for job in bound_jobs
                if str(job.get("id") or "").strip()
            }
        except Exception:
            job_enabled = None
            job_timeouts = None
            job_command_sha256 = {}
            job_descriptions = {}
    now = now or datetime.now(timezone.utc)
    path = runtime / "cron_state.json"
    if not path.exists():
        return [Check("cron_state", "warn", f"{path} missing", "讓 cron scheduler 寫入 dispatch/start/complete state，或確認 MAGI_RUNTIME_DIR。")]

    payload = _read_json(path, {})
    if not isinstance(payload, dict):
        return [Check("cron_state", "fail", f"{path} is not an object", "刪除或修復損壞的 cron_state.json。")]

    failures = []
    validation_gated = []
    disabled_jobs = []
    recovered_jobs = []
    waiting_jobs = []
    latest_success: datetime | None = None
    latest_dispatch: datetime | None = None
    for job_id, item in payload.items():
        if not isinstance(item, dict):
            continue
        success_at = _parse_dt(item.get("last_success_at"))
        dispatch_at = _parse_dt(item.get("last_dispatch_at") or item.get("last_run"))
        if success_at and (latest_success is None or success_at > latest_success):
            latest_success = success_at
        if dispatch_at and (latest_dispatch is None or dispatch_at > latest_dispatch):
            latest_dispatch = dispatch_at
        returncode = item.get("returncode", item.get("last_returncode"))
        try:
            returncode_i = int(returncode) if returncode is not None and returncode != "" else None
        except Exception:
            returncode_i = None
        timed_out = bool(item.get("timed_out") or item.get("last_timed_out"))
        last_success = item.get("last_success")
        last_status = str(item.get("last_status") or "").strip().lower()
        last_error = str(item.get("last_error") or "").strip().lower()
        resource_deferred = (
            last_status == "deferred" or "resource_guard_skipped" in last_error
        ) and not timed_out and (returncode_i is None or returncode_i == 0)
        occurrence = item.get("v3_pending_occurrence")
        occurrence_status = (
            str(occurrence.get("status") or "").strip().lower()
            if isinstance(occurrence, dict)
            else ""
        )
        occurrence_pending = occurrence_status in {"queued", "running"}
        retry = item.get("v3_retry")
        retry_status = (
            str(retry.get("status") or "").strip().lower()
            if isinstance(retry, dict)
            else ""
        )
        automatic_retry_pending = retry_status in {"queued", "running"}
        running_within_timeout = _cron_run_within_timeout(
            item,
            now=now,
            timeout_sec=(job_timeouts or {}).get(str(job_id), 0),
        )
        result_text = "\n".join(
            str(item.get(key) or "")
            for key in (
                "last_status",
                "last_error",
                "last_stdout_tail",
                "last_stderr_tail",
            )
        ).lower()
        semantic_waiting = (
            not timed_out
            and (
                automatic_retry_pending
                or _cron_resource_protection_active(
                    str(job_id),
                    item,
                    returncode=returncode_i,
                    timed_out=timed_out,
                )
                or returncode_i == 75
                or last_status == "partial"
                or '"status": "partial"' in result_text
                or "'status': 'partial'" in result_text
                or (
                    str(job_id) == "job_accounting_monthly_bonus"
                    and "permissionerror" in result_text
                )
                or str(job_id) == "job_heavy_translation_quality_live"
                or (
                    str(job_id) == "job_1770705679"
                    and "all_judgment_reason_searches_failed" in result_text
                )
                or any(
                    marker in result_text
                    for marker in (
                        "upstream",
                        "external service",
                        "external_service",
                        "temporarily unavailable",
                        "service unavailable",
                    )
                )
            )
        )
        if timed_out or (returncode_i is not None and returncode_i != 0) or last_success is False:
            # The scheduler records dispatch/claim before it records the new
            # result.  While that occurrence is queued or running, the
            # previous non-zero result is historical evidence, not the state
            # of the active run.  Mirror function_health_index semantics so
            # Doctor and the self-repair guardian do not create a false red
            # light during every legitimate retry.
            if occurrence_pending or automatic_retry_pending or running_within_timeout:
                waiting_jobs.append(str(job_id))
                continue
            if resource_deferred or semantic_waiting:
                waiting_jobs.append(str(job_id))
                continue
            if job_enabled is not None and job_enabled.get(str(job_id)) is False:
                disabled_jobs.append(str(job_id))
                continue
            if _cron_failure_is_validation_gate_blocked(str(job_id), item):
                validation_gated.append(str(job_id))
                continue
            if str(job_id) == "job_operational_hardening_audit" and _operational_audit_recovered_after_failure(runtime, item):
                recovered_jobs.append(str(job_id))
                continue
            if _omlx_switch_recovered_after_failure(runtime, str(job_id), item):
                recovered_jobs.append(str(job_id))
                continue
            if _omlx_superseded_release_recovered_after_failure(
                runtime,
                str(job_id),
                item,
                current_command_sha256=job_command_sha256.get(str(job_id), ""),
            ):
                recovered_jobs.append(str(job_id))
                continue
            title = job_descriptions.get(str(job_id), str(job_id))
            if "507" in result_text and "embedding" in result_text:
                reason = "向量嵌入服務曾因可用空間不足拒絕請求，等待自動重試"
            elif timed_out:
                reason = "超過預計執行時間"
            elif returncode_i not in (None, 0):
                reason = f"執行未完成（結束碼 {returncode_i}）"
            else:
                reason = "未取得成功證據"
            failures.append(f"{title}：{reason}")

    checks: list[Check] = []
    if failures:
        checks.append(Check("cron_state_failures", "fail", "; ".join(failures[:4]), "MAGI 會依對應作業的自動重試規則重跑；若連續失敗再列為需人工處理。"))
    else:
        detail = f"{len(payload)} tracked job(s)"
        if validation_gated:
            detail += "; validation-gated=" + ",".join(validation_gated[:4])
        if disabled_jobs:
            detail += "; disabled=" + ",".join(disabled_jobs[:4])
        if recovered_jobs:
            detail += "; recovered=" + ",".join(recovered_jobs[:4])
        if waiting_jobs:
            detail += "; waiting=" + ",".join(waiting_jobs[:4])
        checks.append(Check("cron_state_failures", "pass", detail))

    freshness_dt = latest_success or latest_dispatch
    age = _age_hours(freshness_dt, now)
    if age is None:
        checks.append(Check("cron_state_freshness", "warn", "no dispatch/success timestamps", "確認 scheduler 已啟動且 state 使用新版欄位。"))
    elif age > max_age_hours:
        checks.append(Check("cron_state_freshness", "warn", f"latest evidence age={age}h", "確認 cron scheduler 沒有停擺，或調整 runtime root。"))
    else:
        checks.append(Check("cron_state_freshness", "pass", f"latest evidence age={age}h"))
    return checks


def _cron_failure_is_validation_gate_blocked(job_id: str, state_item: dict) -> bool:
    """Validation-gated distill rejection is a safe block, not a broken cron."""

    if "distill_train_gemma" not in job_id and "nightly_distill_gemma" not in str(state_item.get("command") or ""):
        return False
    state_text = "\n".join(
        str(state_item.get(key) or "")
        for key in (
            "last_error",
            "last_stderr_tail",
            "last_stdout_tail",
            "error",
            "stderr",
            "stdout",
            "status",
        )
    ).lower()
    markers = (
        "validation gate",
        "candidate_rejected",
        "candidate rejected",
        "channel_marker_leak",
        "insufficient_traditional_chinese",
        "too_much_english",
        "deploy_allowed=false",
        "blocked from deploy",
        "refusing to deploy rejected",
    )
    return any(marker in state_text for marker in markers) or (
        "v011" in state_text and "reclaim" in state_text
    )


def collect_report(*, live: bool = True, runtime_dir: Path | None = None) -> dict[str, Any]:
    ram = _ram_gb()
    system = platform.system()
    machine = platform.machine()
    checks: list[Check] = []

    checks.append(Check("python", "pass" if sys.version_info >= (3, 10) else "fail", platform.python_version(), "Install Python 3.10 or newer."))
    bound = _project_python()
    if bound:
        from magi_v3.shapely_runtime import verify_shapely_runtime
        ok, detail = verify_shapely_runtime(bound)
        checks.append(Check("shapely_sealed_runtime", "pass" if ok else "fail", detail, "Install Shapely >=2,<3 into the bound MAGI runtime."))
    checks.append(Check("disk", "pass" if _disk_free_gb(REPO_ROOT) >= 20 else "warn", f"{_disk_free_gb(REPO_ROOT)} GB free", "Free at least 20 GB for models and logs."))
    checks.append(Check("memory", "pass" if ram >= 16 else "warn", f"{ram} GB RAM", "16 GB+ is recommended; 32 GB+ is better for local models."))
    checks.append(Check("git", "pass" if shutil.which("git") else "fail", shutil.which("git") or "missing", "Install Git."))

    project_python = _project_python()
    venv_path = REPO_ROOT / ".venv"
    legacy_venv = REPO_ROOT / "venv"
    checks.append(Check("virtualenv", "pass" if project_python else "warn", str(project_python or venv_path if venv_path.exists() else legacy_venv), "Run scripts/install_magi.py --yes."))

    for module, pip_name in (
        ("flask", "flask"),
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn"),
        ("pytest", "pytest"),
        ("requests", "requests"),
    ):
        checks.append(Check(f"python:{module}", "pass" if _package_available(module) else "warn", pip_name, f"Install with pip install {pip_name}."))

    apple_silicon = system == "Darwin" and machine == "arm64"
    checks.append(Check("apple_silicon", "pass" if apple_silicon else "warn", f"{system} {machine}", "MLX acceleration is best on Apple Silicon."))
    checks.append(Check("mlx", "pass" if _package_available("mlx") else "warn", "installed" if _package_available("mlx") else "missing", "Install optional MLX dependencies."))
    checks.append(Check("mlx_vlm", "pass" if _package_available("mlx_vlm") else "warn", "installed" if _package_available("mlx_vlm") else "missing", "pip install mlx-vlm"))

    model_dir = Path.home() / ".omlx" / "models" / "gemma-4-E4B-it-assistant-bf16"
    checks.append(Check("gemma4_e4b_model", "pass" if model_dir.exists() else "warn", str(model_dir), "Download the Gemma 4 E4B assistant draft model."))

    checks.extend(_runtime_root_checks())
    checks.extend(_launchagent_checks())
    checks.extend(_cron_state_checks(runtime_dir=runtime_dir))

    if live and _mtp_sidecar_required():
        ok, detail = _mtp_sidecar_check()
        checks.append(Check("mlx_mtp_sidecar", "pass" if ok else "warn", detail[:240], "Start com.magi.mlx-mtp or run scripts/serve_mlx_mtp.py."))
    elif live:
        checks.append(Check("mlx_mtp_sidecar", "pass", "disabled; available on demand"))

    failed = sum(1 for c in checks if c.status == "fail")
    warned = sum(1 for c in checks if c.status == "warn")
    return {
        "ok": failed == 0,
        "status": "pass" if failed == 0 and warned == 0 else ("warn" if failed == 0 else "fail"),
        "system": {
            "os": system,
            "release": platform.release(),
            "machine": machine,
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "project_python": str(project_python) if project_python else None,
            "repo": str(REPO_ROOT),
        },
        "summary": {"pass": sum(1 for c in checks if c.status == "pass"), "warn": warned, "fail": failed},
        "checks": [asdict(c) for c in checks],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect whether this computer can run MAGI.")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--no-live", action="store_true", help="skip localhost service probes")
    parser.add_argument("--output", type=Path, help="write JSON report to a file")
    args = parser.parse_args(argv)

    report = collect_report(live=not args.no_live)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"MAGI Doctor: {report['status'].upper()} {report['summary']}")
        for item in report["checks"]:
            print(f"{item['status'].upper():4} {item['name']}: {item['detail']}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
