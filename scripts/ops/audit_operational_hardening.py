#!/usr/bin/env python3
"""Operational hardening audit for MAGI.

Checks the items that basic /health cannot see: cron fallback compatibility,
cron time collisions, dirty worktree categories, and recent issue agenda
failures.
"""

from __future__ import annotations

import argparse
import json
import os
import re
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

from api.platforms.safe_process import parse_cron_command, _validate_argv  # noqa: E402


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


def _load_cron_last_run_ts() -> dict[str, float]:
    state_path = ROOT / ".runtime" / "cron_state.json"
    if not state_path.exists():
        return {}
    raw = _load_json(state_path, {})
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for job_id, data in raw.items():
        if not isinstance(data, dict):
            continue
        ts = _safe_epoch(data.get("last_run"))
        if ts > 0:
            out[str(job_id)] = ts
    return out


def _current_omlx_models(port: int = 8080) -> list[str]:
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
    minutes = now.hour * 60 + now.minute
    expected_profile = "day" if 415 <= minutes < 1310 else "night"
    expected_keyword = "e4b" if expected_profile == "day" else "26b"
    models = _current_omlx_models(8080)
    phi4_models = _current_omlx_models(8082)
    smol_models = _current_omlx_models(8083)
    active_profile = ""
    try:
        active_profile = (Path.home() / ".omlx" / "active_profile").read_text(encoding="utf-8").strip()
    except Exception:
        active_profile = ""
    model_dir_hint = ""
    try:
        model_dir = Path.home() / ".omlx" / "models-text"
        model_dir_hint = " ".join(sorted(p.name.lower() for p in model_dir.iterdir()))
    except Exception:
        model_dir_hint = ""
    live_text = " ".join(models).lower()
    phi4_text = " ".join(phi4_models).lower()
    smol_text = " ".join(smol_models).lower()
    ok = (
        expected_keyword in live_text
        and expected_keyword in model_dir_hint.lower()
        and active_profile == expected_profile
    )
    sidecars_ok = True
    if expected_profile == "day":
        sidecars_ok = ("phi" in phi4_text and "smol" in smol_text)
        ok = ok and sidecars_ok
    else:
        sidecars_ok = (not phi4_models and not smol_models)
        ok = ok and sidecars_ok
    return {
        "ok": ok,
        "expected_profile": expected_profile,
        "expected_keyword": expected_keyword,
        "active_profile": active_profile,
        "models": models,
        "phi4_models": phi4_models,
        "smol_models": smol_models,
        "sidecars_ok": sidecars_ok,
        "model_dir_hint": model_dir_hint,
        "time": now.strftime("%Y-%m-%d %H:%M"),
        "remediation": "Run config/bin/omlx_switch_model.sh auto; cron job_omlx_profile_guard should keep this idempotently repaired.",
    }


def _latest_operational_audit_is_green(issue_ts: float) -> bool:
    path = ROOT / ".runtime" / "operational_hardening_audit_latest.json"
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
    path = ROOT / ".runtime" / "tailscale_funnel_health_latest.json"
    if not path.exists() or path.stat().st_mtime <= issue_ts:
        return False
    data = _load_json(path, {})
    return str((data or {}).get("status") or "").lower() in {"ok", "recovered", "skipped"}


def _classify_issue_row(
    row: dict[str, Any],
    *,
    active_cutoff: float,
    latest_cron_issue_ts_by_job: dict[str, float],
    cron_last_run_ts: dict[str, float],
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
    if cron_last_run_ts.get(job_id, 0.0) > ts:
        return "recovered"
    if ts < active_cutoff:
        return "stale"
    return "active_unresolved"


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def audit_cron() -> dict[str, Any]:
    jobs = _load_json(ROOT / "cron_jobs.json", [])
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


def audit_git() -> dict[str, Any]:
    proc = subprocess.run(
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
    path = ROOT / ".runtime" / "issue_agenda.jsonl"
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
    cron_last_run_ts = _load_cron_last_run_ts()
    class_counts: dict[str, int] = defaultdict(int)
    recent = []
    for row in rows:
        state = _classify_issue_row(
            row,
            active_cutoff=active_cutoff,
            latest_cron_issue_ts_by_job=latest_cron_issue_ts_by_job,
            cron_last_run_ts=cron_last_run_ts,
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

    return {
        "exists": True,
        "recent_count": len(rows),
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
    proc = subprocess.run(
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
    proc = subprocess.run(
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json-out", default=str(ROOT / ".runtime" / "operational_hardening_audit_latest.json"))
    parser.add_argument("--fail-on-red", action="store_true")
    args = parser.parse_args()

    report = {
        "cron": audit_cron(),
        "git": audit_git(),
        "issue_agenda": audit_issue_agenda(),
        "gmail_monitor": audit_gmail_monitor_mode(),
        "omlx_profile": audit_omlx_profile(),
        "silent_exception_handlers": audit_silent_exception_handlers(),
        "retired_feature_residue": audit_retired_feature_residue(),
        "osc_route_integrity": audit_osc_route_integrity(),
    }
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({
        "cron_parse_failures": report["cron"]["parse_failure_count"],
        "cron_collisions": report["cron"]["collision_count"],
        "dirty_count": report["git"]["dirty_count"],
        "recent_issues": int(report["issue_agenda"].get("recent_count") or 0),
        "gmail_monitor_mode": report["gmail_monitor"]["mode"],
        "omlx_profile_ok": report["omlx_profile"]["ok"],
        "omlx_expected": report["omlx_profile"]["expected_profile"],
        "omlx_models": report["omlx_profile"]["models"],
        "silent_exception_critical_count": report["silent_exception_handlers"]["critical_count"],
        "retired_feature_residue_ok": report["retired_feature_residue"]["ok"],
        "osc_route_integrity_ok": report["osc_route_integrity"]["ok"],
        "json_out": str(out),
    }, ensure_ascii=False))

    if args.fail_on_red and (
        report["cron"]["parse_failure_count"] > 0
        or report["cron"]["collision_count"] > 0
        or not report["gmail_monitor"]["ok"]
        or not report["omlx_profile"]["ok"]
        or not report["retired_feature_residue"]["ok"]
        or not report["osc_route_integrity"]["ok"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
