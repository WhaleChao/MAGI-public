#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from api.runtime_paths import get_config_path, get_json_dir, get_laf_script, get_orch_dir


TOOLS_API = os.environ.get("MAGI_TOOLS_API", "http://127.0.0.1:5003").rstrip("/")
MAIN_API = os.environ.get("MAGI_MAIN_API", "http://127.0.0.1:5002").rstrip("/")
OMLX_EMBED_BASE = os.environ.get("MAGI_OMLX_EMBED_URL", "http://127.0.0.1:8081").rstrip("/")
REPORT_DIR = ROOT / "static"
JSON_REPORT = REPORT_DIR / "integration_smoke_latest.json"
MD_REPORT = REPORT_DIR / "integration_smoke_latest.md"


def _now() -> str:
    return datetime.now().isoformat()


def _read_json_from_text(text: str) -> dict[str, Any]:
    s = (text or "").strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {"value": obj}
    except Exception:
        m = re.search(r"(\{[\s\S]*\})\s*$", s)
        if m:
            try:
                obj = json.loads(m.group(1))
                return obj if isinstance(obj, dict) else {"value": obj}
            except Exception:
                return {}
        return {}


def _http_get_json(url: str, timeout: int = 20) -> tuple[bool, int | None, dict[str, Any], str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            body = resp.read().decode("utf-8", errors="replace")
        return True, status, _read_json_from_text(body), ""
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return False, int(getattr(e, "code", 0) or 0), _read_json_from_text(body), body[:500]
    except Exception as e:
        return False, None, {}, f"{type(e).__name__}: {e}"


def _http_post_json(url: str, payload: dict[str, Any], timeout: int = 60) -> tuple[bool, int | None, dict[str, Any], str]:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = int(getattr(resp, "status", 200))
            body = resp.read().decode("utf-8", errors="replace")
        return True, status, _read_json_from_text(body), ""
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return False, int(getattr(e, "code", 0) or 0), _read_json_from_text(body), body[:800]
    except Exception as e:
        return False, None, {}, f"{type(e).__name__}: {e}"


def _extract_bool(payload: Any, *keys: str) -> bool | None:
    cur = payload
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    if isinstance(cur, bool):
        return cur
    return None


def _capture_import(module_name: str) -> dict[str, Any]:
    buf_out = io.StringIO()
    buf_err = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            mod = importlib.import_module(module_name)
        return {
            "ok": True,
            "module": module_name,
            "file": getattr(mod, "__file__", ""),
            "stdout": buf_out.getvalue()[:400],
            "stderr": buf_err.getvalue()[:400],
        }
    except Exception as e:
        return {
            "ok": False,
            "module": module_name,
            "error": f"{type(e).__name__}: {e}",
            "stdout": buf_out.getvalue()[:400],
            "stderr": buf_err.getvalue()[:400],
        }


def _run_skill_test(skill: str, task: str, timeout_sec: int) -> dict[str, Any]:
    ok, status, body, err = _http_post_json(
        f"{TOOLS_API}/skills/run",
        {
            "skill": skill,
            "task": task,
            "timeout_sec": timeout_sec,
            "auto_repair": False,
            "rollback_on_fail": True,
            "auto_install_deps": False,
        },
        timeout=timeout_sec + 30,
    )
    nested = _read_json_from_text(str(body.get("output") or ""))
    return {
        "http_ok": ok,
        "http_status": status,
        "dispatch_success": bool(body.get("success")),
        "skill": skill,
        "task": task,
        "stderr": str(body.get("stderr") or "")[:500],
        "body": body,
        "nested": nested,
        "error": err,
    }


def _assess_skill_test(result: dict[str, Any]) -> tuple[bool, str]:
    skill = result.get("skill", "")
    nested = result.get("nested") or {}
    if not result.get("http_ok"):
        return False, result.get("error", "http_error")
    if not result.get("dispatch_success"):
        return False, "dispatch_failed"

    if skill == "laf-orchestrator":
        return bool(nested.get("success")), "self_test"
    if skill == "file-review-orchestrator":
        return bool(nested.get("success")), "self_test/db_smoke"
    if skill == "transcript-downloader":
        return bool(nested.get("success")), "self_test/db_probe"
    if skill == "osc-orchestrator":
        return bool(nested.get("ok")), "self_test"
    if skill == "osc-scan-folder":
        return bool(nested.get("success")), "self_test"
    if skill == "db-dual-sync":
        return bool(nested.get("success")), "self_test"
    if skill == "laf-withdrawal-report":
        return bool(nested.get("success")), "wrapper_self_test"
    if skill == "crawler-targets":
        return bool(nested.get("success")), "self_test"
    if skill == "magi-autopilot":
        if nested.get("ok") is True and nested.get("skipped") is True:
            return True, f"self_test_skipped:{nested.get('reason', 'skip')}"
        return bool(nested.get("ok")), "self_test"
    if skill == "statutes-vdb":
        return bool(nested.get("ok")), "help"
    if skill == "gmail-drafts":
        return bool(nested.get("success")), "help"
    return bool(result.get("dispatch_success")), "dispatch_only"


def _write_reports(report: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    JSON_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# MAGI Integration Smoke",
        "",
        f"- Generated: {report.get('generated_at', '')}",
        f"- Overall OK: {report.get('overall_ok', False)}",
        f"- Main API: {report.get('main_api', '')}",
        f"- Tools API: {report.get('tools_api', '')}",
        "",
        "## Summary",
    ]
    for item in report.get("checks", []):
        status = "PASS" if item.get("ok") else "FAIL"
        lines.append(f"- [{status}] {item.get('name')}: {item.get('summary', '')}")
    MD_REPORT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main() -> int:
    checks: list[dict[str, Any]] = []
    orch_dir = str(get_orch_dir())
    if orch_dir not in sys.path:
        sys.path.insert(0, orch_dir)

    ok, status, health_body, err = _http_get_json(f"{MAIN_API}/health", timeout=20)
    checks.append(
        {
            "name": "main_health",
            "ok": bool(ok and health_body.get("status") in {"operational", "ok"}),
            "summary": f"status={health_body.get('status', '')} http={status}",
            "details": health_body if health_body else {"error": err},
        }
    )

    ok, status, sages_body, err = _http_get_json(f"{TOOLS_API}/sages", timeout=20)
    casper_ok = _extract_bool(sages_body, "casper", "online")
    melchior_ok = _extract_bool(sages_body, "melchior", "online")
    checks.append(
        {
            "name": "tools_sages",
            "ok": bool(ok and casper_ok is True and melchior_ok is True),
            "summary": f"casper_online={casper_ok} melchior_online={melchior_ok} http={status}",
            "details": sages_body if sages_body else {"error": err},
        }
    )

    imports = []
    for name in [
        "laf_orchestrator",
        "judicial_automation_v2",
        "file_review_automation",
        "legalbridge_core",
        "laf_automation_v2",
        "magi_eventlog",
        "line_notifier",
    ]:
        imports.append(_capture_import(name))
    import_ok = all(x.get("ok") and str(x.get("file") or "").startswith(str(ROOT)) for x in imports)
    checks.append(
        {
            "name": "module_provenance",
            "ok": import_ok,
            "summary": "all core modules import from MAGI",
            "details": {
                "imports": imports,
                "json_dir": str(get_json_dir()),
                "config_path": str(get_config_path("config.json")),
                "laf_script": str(get_laf_script()),
            },
        }
    )

    ok, status, embed_health_body, err = _http_get_json(f"{OMLX_EMBED_BASE}/health", timeout=20)
    checks.append(
        {
            "name": "embed_service_health",
            "ok": bool(ok and embed_health_body.get("status") == "healthy"),
            "summary": f"status={embed_health_body.get('status', '')} http={status}",
            "details": embed_health_body if embed_health_body else {"error": err, "base": OMLX_EMBED_BASE},
        }
    )

    ok, status, embed_body, err = _http_post_json(
        f"{OMLX_EMBED_BASE}/v1/embeddings",
        {"model": os.environ.get("MAGI_OMLX_EMBED_MODEL", os.environ.get("MAGI_OMLX_EMBED_MODEL", "")), "input": "MAGI embedding smoke"},
        timeout=45,
    )
    embed_data = (embed_body or {}).get("data", [])
    embed_dims = 0
    if embed_data and isinstance(embed_data, list):
        embed_dims = len((embed_data[0] or {}).get("embedding", []) or [])
    checks.append(
        {
            "name": "embed_roundtrip",
            "ok": bool(ok and embed_dims >= 256),
            "summary": f"dims={embed_dims} http={status}",
            "details": embed_body if embed_body else {"error": err, "base": OMLX_EMBED_BASE},
        }
    )

    try:
        from skills.ops import red_phone  # type: ignore

        tg_token, tg_ids = red_phone._get_telegram_config()
        checks.append(
            {
                "name": "notification_config",
                "ok": bool(tg_token and tg_ids),
                "summary": f"telegram_targets={len(tg_ids)}",
                "details": {
                    "has_token": bool(tg_token),
                    "target_count": len(tg_ids),
                },
            }
        )
    except Exception as e:
        checks.append(
            {
                "name": "notification_config",
                "ok": False,
                "summary": "telegram config import failed",
                "details": {"error": f"{type(e).__name__}: {e}"},
            }
        )

    skill_matrix = [
        ("laf-orchestrator", "self_test", 90),
        ("file-review-orchestrator", "self_test", 90),
        ("file-review-orchestrator", "db_smoke {}", 120),
        ("transcript-downloader", "self_test", 90),
        ("transcript-downloader", "db_probe", 120),
        ("osc-orchestrator", "self_test", 180),
        ("osc-scan-folder", "self_test", 120),
        ("db-dual-sync", "self_test", 180),
        ("laf-withdrawal-report", "self_test", 60),
        ("crawler-targets", "self_test", 60),
        ("magi-autopilot", "self_test", 240),
        ("statutes-vdb", "help", 60),
        ("gmail-drafts", "help", 60),
    ]

    for skill, task, timeout_sec in skill_matrix:
        res = _run_skill_test(skill, task, timeout_sec)
        test_ok, note = _assess_skill_test(res)
        checks.append(
            {
                "name": f"skill:{skill}:{task}",
                "ok": test_ok,
                "summary": note,
                "details": {
                    "http_ok": res.get("http_ok"),
                    "http_status": res.get("http_status"),
                    "dispatch_success": res.get("dispatch_success"),
                    "stderr": res.get("stderr"),
                    "nested": res.get("nested"),
                    "error": res.get("error"),
                },
            }
        )

    launch = subprocess.run(
        ["launchctl", "list"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    launch_text = (launch.stdout or "") + "\n" + (launch.stderr or "")
    checks.append(
        {
            "name": "launch_agent",
            "ok": "com.magi.casper" in launch_text,
            "summary": "com.magi.casper present in launchctl list",
            "details": {
                "matches": [ln for ln in launch_text.splitlines() if "com.magi.casper" in ln][:3],
            },
        }
    )

    report = {
        "generated_at": _now(),
        "main_api": MAIN_API,
        "tools_api": TOOLS_API,
        "overall_ok": all(bool(item.get("ok")) for item in checks),
        "checks": checks,
    }
    _write_reports(report)
    print(json.dumps({"overall_ok": report["overall_ok"], "json_report": str(JSON_REPORT), "md_report": str(MD_REPORT)}, ensure_ascii=False))
    return 0 if report["overall_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
