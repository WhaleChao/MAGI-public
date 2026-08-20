#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/ops/nightly_regression.py
===================================
MAGI Nightly Regression Runner

Orchestrates the production test suites and sends a consolidated report:
  1. system_test.py       — subsystem health (Ollama, DB, memory, …)
  2. smoke_three_channels.py — TG / Discord / LINE credential + API checks
  3. smoke_core_routes.py — core text-routing capability checks

The retired V2 mock-skill fixture remains available only when ``mock`` is
explicitly requested.  It is not part of the production nightly verdict.

Usage:
  python scripts/ops/nightly_regression.py
  python scripts/ops/nightly_regression.py --no-notify
  python scripts/ops/nightly_regression.py --suites system,channels,coreroutes

LaunchAgent / crontab example:
  0 2 * * * <_MAGI_ROOT>/venv/bin/python3 \
      <_MAGI_ROOT>/scripts/ops/nightly_regression.py
"""
from __future__ import annotations

import argparse
import json
import os
_MAGI_ROOT = os.path.abspath(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MAGI_DIR = Path(_MAGI_ROOT)
VENV_PY  = Path(f"{_MAGI_ROOT}/venv/bin/python3")
PYTHON   = str(VENV_PY) if VENV_PY.exists() else sys.executable

if str(MAGI_DIR) not in sys.path:
    sys.path.insert(0, str(MAGI_DIR))

from skills.ops import health_probes as _health_probes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], timeout: int = 300) -> tuple[int, str, str]:
    """Run subprocess, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout ({timeout}s)"
    except Exception as e:
        return -2, "", str(e)


def _suite_report_path(filename: str) -> Path:
    runtime = Path(os.environ.get("MAGI_RUNTIME_DIR") or "/tmp").expanduser()
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / filename
    # Never let a failed child process inherit a stale successful report.
    path.unlink(missing_ok=True)
    return path


def _discord_bot_process() -> tuple[bool, str]:
    """Return whether the Discord bot process is visible to local health probes."""
    return _health_probes.python_script_process_running("api/discord_bot.py", timeout_sec=5)


def _pid_list(pid_text: str) -> list[str]:
    return [pid.strip() for pid in str(pid_text or "").split(",") if pid.strip()]


def ensure_discord_bot_for_regression(wait_sec: int = 25) -> dict:
    """
    Nightly regression is a health check, but the health signal is noisy when
    daemon is between Discord restarts.  Give the bot a short self-heal window
    before running the suites that require it.
    """
    running, pid_text = _discord_bot_process()
    if running:
        pids = _pid_list(pid_text)
        if len(pids) > 1:
            return {"ok": False, "action": "duplicate_running", "pid": pid_text, "count": len(pids)}
        return {"ok": True, "action": "already_running", "pid": pid_text}

    script = MAGI_DIR / "api" / "discord_bot.py"
    if not script.exists():
        return {"ok": False, "action": "missing_script", "path": str(script)}

    from magi_v3.external_inputs import bound_shared_directory

    log_path = bound_shared_directory(
        MAGI_DIR,
        env_name="MAGI_RUNTIME_DIR",
        shared_leaf="runtime",
        source_fallback=".runtime",
    ) / "nightly_discord_preflight.log"
    log_fh = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")
        log_fh.write(f"\n[{datetime.now().isoformat()}] starting discord_bot.py for nightly regression preflight\n")
        log_fh.flush()
        proc = subprocess.Popen(
            [PYTHON, str(script)],
            cwd=str(MAGI_DIR),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        if log_fh is not None:
            try:
                log_fh.close()
            except Exception:
                pass
        return {"ok": False, "action": "start_failed", "error": str(e), "log_path": str(log_path)}
    finally:
        if log_fh is not None and not log_fh.closed:
            try:
                log_fh.close()
            except Exception:
                pass

    deadline = time.time() + max(1, wait_sec)
    while time.time() < deadline:
        running, pid_text = _discord_bot_process()
        if running:
            pids = _pid_list(pid_text)
            if len(pids) > 1:
                return {
                    "ok": False,
                    "action": "duplicate_running",
                    "pid": pid_text,
                    "count": len(pids),
                    "launcher_pid": proc.pid,
                    "log_path": str(log_path),
                }
            return {
                "ok": True,
                "action": "started",
                "pid": pid_text,
                "launcher_pid": proc.pid,
                "log_path": str(log_path),
            }
        if proc.poll() is not None:
            return {
                "ok": False,
                "action": "exited_early",
                "returncode": proc.returncode,
                "launcher_pid": proc.pid,
                "log_path": str(log_path),
            }
        time.sleep(1)

    return {
        "ok": False,
        "action": "start_timeout",
        "launcher_pid": proc.pid,
        "log_path": str(log_path),
    }


def _notify(msg: str) -> None:
    try:
        from skills.ops.red_phone import send_telegram_push_with_status
        send_telegram_push_with_status(msg, topic_key="check", source="nightly_regression")
    except Exception as e:
        print(f"[notify] 發送失敗: {e}")


# ---------------------------------------------------------------------------
# Suite 1 — System health (system_test.py)
# ---------------------------------------------------------------------------

def run_system_test() -> dict:
    print("[Suite 1] System health test …")
    try:
        from skills.ops.system_test import run_all_tests
        report = run_all_tests()
        passed = report.get("passed", 0)
        total  = report.get("total", 0)
        failed = report.get("failed", 0)
        failures = [
            t["label"] for t in report.get("tests", []) if not t.get("pass")
        ]
        return {
            "suite": "system",
            "label": "System Health",
            "passed": passed,
            "failed": failed,
            "total": total,
            "failures": failures,
            "ok": failed == 0,
        }
    except Exception as e:
        return {"suite": "system", "label": "System Health",
                "ok": False, "error": str(e), "passed": 0, "failed": 1, "total": 1, "failures": [str(e)]}


# ---------------------------------------------------------------------------
# Suite 2 — Three-channel smoke (smoke_three_channels.py)
# ---------------------------------------------------------------------------

def run_channel_smoke() -> dict:
    print("[Suite 2] Three-channel smoke test …")
    script = MAGI_DIR / "scripts" / "ops" / "smoke_three_channels.py"
    tmp_json = _suite_report_path("magi_smoke_channels.json")

    rc, stdout, stderr = _run(
        [PYTHON, str(script), "--json-out", str(tmp_json)],
        timeout=60,
    )

    # Parse JSON output
    passed = failed = warned = 0
    failures: list[str] = []
    try:
        if tmp_json.exists():
            data = json.loads(tmp_json.read_text(encoding="utf-8"))
            summary = data.get("summary", {})
            passed  = summary.get("pass", 0)
            warned  = summary.get("warn", 0)
            failed  = summary.get("fail", 0)
            total   = passed + warned + failed
            failures = [
                f"[{c['channel']}] {c['name']}: {c['detail'][:60]}"
                for c in data.get("checks", [])
                if c.get("status") == "FAIL"
            ]
    except Exception:
        # Fallback: parse text output
        for line in stdout.splitlines():
            if "PASS:" in line:
                try: passed = int(line.split(":")[-1].strip())
                except Exception: pass
            elif "FAIL:" in line:
                try: failed = int(line.split(":")[-1].strip())
                except Exception: pass
        total = passed + warned + failed

    total = passed + warned + failed
    return {
        "suite": "channels",
        "label": "TG/DC/LINE Smoke",
        "passed": passed,
        "warned": warned,
        "failed": failed,
        "total": total,
        "failures": failures,
        "ok": failed == 0,
        "rc": rc,
    }


# ---------------------------------------------------------------------------
# Optional retired V2 mock-skill compatibility probe
# ---------------------------------------------------------------------------

def run_mock_skills(skills: str = "all") -> dict:
    print(f"[Suite 3] Mock skill test (skills={skills}) …")
    candidates = [
        MAGI_DIR / "casper_ecosystem" / "law_firm_orchestrators" / "mock_skill_test.py",  # legacy path
        MAGI_DIR / "scripts" / "ops" / "mock_skill_test.py",  # fallback if moved out of LAF tree
    ]
    script = next((p for p in candidates if p.exists()), None)

    if script is None:
        return {
            "suite": "mock",
            "label": "Mock Skills（已退役）",
            "ok": True,
            "passed": 0,
            "failed": 0,
            "skipped": 1,
            "warned": 0,
            "total": 1,
            "failures": [],
            "warnings": [],
            "status": "retired",
        }

    rc, stdout, stderr = _run(
        [PYTHON, str(script), "--skills", skills, "--no-stop"],
        timeout=480,
    )

    # Parse summary line: "結果: X PASS / Y FAIL / Z SKIP / N 共"
    import re
    passed = failed = skipped = total = 0
    for line in stdout.splitlines():
        if "PASS" in line and "FAIL" in line and "共" in line:
            m = re.search(r"(\d+) PASS.*?(\d+) FAIL.*?(\d+) SKIP.*?(\d+) 共", line)
            if m:
                passed, failed, skipped, total = (
                    int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
                )

    failures: list[str] = []
    in_fail = False
    for line in stdout.splitlines():
        if "失敗項目" in line:
            in_fail = True
        elif in_fail and line.strip().startswith("-"):
            failures.append(line.strip()[2:].strip())
        elif in_fail and line.strip() == "=" * 20:
            break

    return {
        "suite": "mock",
        "label": "Mock Skills (eefile+laf)",
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total": total,
        "failures": failures,
        "ok": failed == 0,
        "rc": rc,
    }


# ---------------------------------------------------------------------------
# Suite 4 — Core route smoke (smoke_core_routes.py)
# ---------------------------------------------------------------------------

def run_core_routes() -> dict:
    print("[Suite 4] Core route smoke test …")
    script = MAGI_DIR / "scripts" / "ops" / "smoke_core_routes.py"
    tmp_json = _suite_report_path("magi_smoke_core_routes.json")

    rc, stdout, stderr = _run(
        [PYTHON, str(script), "--json-out", str(tmp_json)],
        timeout=420,
    )

    passed = failed = warned = 0
    failures: list[str] = []

    # Try JSON output first (if smoke_core_routes supports --json-out)
    parsed_json = False
    try:
        if tmp_json.exists():
            data = json.loads(tmp_json.read_text(encoding="utf-8"))
            summary = data.get("summary", {})
            passed = summary.get("pass", summary.get("passed", 0))
            failed = summary.get("fail", summary.get("failed", 0))
            warned = summary.get("warn", summary.get("warned", 0))
            failures = [
                c.get("name", "unknown")
                for c in data.get("cases", data.get("checks", []))
                if c.get("status") == "FAIL"
            ]
            parsed_json = True
    except Exception:
        pass

    # Fallback: parse text output (PASS: N / FAIL: N lines)
    if not parsed_json:
        for line in stdout.splitlines():
            line_s = line.strip()
            if line_s.startswith("PASS:"):
                try:
                    passed = int(line_s.split(":")[-1].strip())
                except ValueError:
                    pass
            elif line_s.startswith("FAIL:"):
                try:
                    failed = int(line_s.split(":")[-1].strip())
                except ValueError:
                    pass
            elif line_s.startswith("WARN:"):
                try:
                    warned = int(line_s.split(":")[-1].strip())
                except ValueError:
                    pass
            elif line_s.startswith("FAIL "):
                # Individual case failure line: "FAIL case_name: preview"
                failures.append(line_s[5:].split(":")[0].strip())

    total = passed + failed + warned
    return {
        "suite": "coreroutes",
        "label": "Core Routes Smoke",
        "passed": passed,
        "failed": failed,
        "warned": warned,
        "total": total,
        "failures": failures,
        "ok": failed == 0 and rc == 0,
        "rc": rc,
    }


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def _suite_icon(s: dict) -> str:
    if not s.get("ok"):
        return "❌"
    if s.get("warned", 0) > 0:
        return "⚠️"
    return "✅"


def build_report(suites: list[dict]) -> tuple[str, bool]:
    """Return (formatted text, overall_ok)."""
    now = datetime.now().strftime("%m/%d %H:%M")
    overall_ok = all(s.get("ok", False) for s in suites)

    header = f"{'✅' if overall_ok else '❌'} MAGI 夜間回歸 {now}"
    lines = [header, ""]

    for s in suites:
        icon = _suite_icon(s)
        label = s.get("label", s.get("suite", "?"))
        p = s.get("passed", 0)
        f = s.get("failed", 0)
        sk = s.get("skipped", 0)
        w = s.get("warned", 0)
        t = s.get("total", p + f + sk)

        detail_parts = [f"{p}✓"]
        if f:  detail_parts.append(f"{f}✗")
        if sk: detail_parts.append(f"{sk}⏭")
        if w:  detail_parts.append(f"{w}⚠")
        detail_parts.append(f"/{t}")

        lines.append(f"{icon} {label}: {' '.join(detail_parts)}")

        if s.get("error"):
            lines.append(f"  錯誤: {s['error'][:80]}")
        if s.get("warnings"):
            for warn in s["warnings"][:3]:
                lines.append(f"  ⚠ {str(warn)[:90]}")
            if len(s["warnings"]) > 3:
                lines.append(f"  … 還有 {len(s['warnings']) - 3} 項 warning")
        elif s.get("failures"):
            for fail in s["failures"][:4]:
                lines.append(f"  ✗ {fail[:70]}")
            if len(s["failures"]) > 4:
                lines.append(f"  … 還有 {len(s['failures']) - 4} 項失敗")

    return "\n".join(lines), overall_ok


def build_failure_summary(suites: list[dict]) -> str:
    """Compact stderr summary so cron issue samples show actionable failures."""
    parts: list[str] = []
    for suite in suites:
        if suite.get("ok"):
            continue
        label = str(suite.get("label") or suite.get("suite") or "?")
        failures = [str(item) for item in suite.get("failures") or [] if str(item).strip()]
        if suite.get("error"):
            failures.insert(0, str(suite.get("error")))
        if failures:
            parts.append(f"{label}: " + "; ".join(failures[:4]))
        else:
            parts.append(f"{label}: failed={suite.get('failed', 0)} rc={suite.get('rc', '')}")
    return " | ".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SUITE_FUNCS = {
    "system":     run_system_test,
    "channels":   run_channel_smoke,
    "mock":       run_mock_skills,
    "coreroutes": run_core_routes,
}


def main() -> int:
    ap = argparse.ArgumentParser(description="MAGI nightly regression runner")
    ap.add_argument("--suites",    default="system,channels,coreroutes",
                    help="Comma-separated suites to run (system,channels,mock,coreroutes)")
    ap.add_argument("--no-notify", action="store_true",
                    help="Skip Telegram notification")
    ap.add_argument("--json-out",  default="",
                    help="Optional path to save JSON report")
    args = ap.parse_args()

    suite_names = [s.strip() for s in args.suites.split(",") if s.strip()]
    results: list[dict] = []

    start = time.time()
    preflight = None
    if any(name in {"system", "channels"} for name in suite_names):
        preflight = ensure_discord_bot_for_regression()
        print(
            "[Preflight] Discord bot: "
            f"{'ok' if preflight.get('ok') else 'fail'} "
            f"action={preflight.get('action')} pid={preflight.get('pid', '')}"
        )
        if not preflight.get("ok"):
            results.append(
                {
                    "suite": "preflight",
                    "label": "Discord Bot Preflight",
                    "ok": False,
                    "passed": 0,
                    "failed": 1,
                    "total": 1,
                    "failures": [
                        f"{preflight.get('action') or 'discord_preflight_failed'}"
                        + (f": {preflight.get('pid')}" if preflight.get("pid") else "")
                    ],
                }
            )

    for name in suite_names:
        fn = SUITE_FUNCS.get(name)
        if fn is None:
            print(f"[warn] Unknown suite '{name}', skipping")
            continue
        try:
            r = fn()
        except Exception as e:
            r = {"suite": name, "label": name, "ok": False,
                 "error": str(e), "passed": 0, "failed": 1, "total": 1, "failures": []}
        results.append(r)
        icon = "✅" if r.get("ok") else "❌"
        print(f"  {icon} {r.get('label', name)}: "
              f"passed={r.get('passed',0)} failed={r.get('failed',0)}")

    elapsed = round(time.time() - start, 1)
    report_text, overall_ok = build_report(results)
    report_text += f"\n\n用時 {elapsed}s"

    print("\n" + "=" * 60)
    print(report_text)
    print("=" * 60)

    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ok": overall_ok,
            "success": overall_ok,
            "generated_at": datetime.now().isoformat(),
            "elapsed_sec": elapsed,
            "overall_ok": overall_ok,
            "preflight": preflight,
            "suites": results,
        }
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON report saved: {out}")

    if not overall_ok:
        print("nightly_regression_failed: " + build_failure_summary(results), file=sys.stderr)

    if not args.no_notify:
        _notify(report_text)

    return 0 if overall_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
