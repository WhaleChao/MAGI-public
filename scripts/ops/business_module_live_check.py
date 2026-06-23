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
import subprocess
import sys
import argparse
import re
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
PYTHON = os.environ.get("MAGI_SKILL_PYTHON") or str(REPO_ROOT / "venv" / "bin" / "python3")
if not Path(PYTHON).exists():
    PYTHON = sys.executable


_REDACT_KEYS = {
    "applicant",
    "case_number",
    "client_name",
    "court_case_number",
    "email",
    "folder_path",
    "local_path",
    "name",
    "path",
    "phone",
    "recipient",
    "sample",
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


def _redact_obj(value: Any, *, key: str = "") -> Any:
    key_lower = str(key or "").lower()
    if any(marker in key_lower for marker in _REDACT_KEYS):
        if isinstance(value, (int, float, bool)) or value is None:
            return value
        if key_lower == "sample" and isinstance(value, list):
            return f"<REDACTED:{len(value)} item(s)>"
        return "<REDACTED>"
    if isinstance(value, dict):
        return {k: _redact_obj(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_obj(item, key=key) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _run(name: str, argv: list[str], timeout: int = 600) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("MAGI_NO_DELETE", "1")
    env.setdefault("MAGI_PREFER_LOCAL_DB", "1")
    try:
        proc = subprocess.run(
            argv,
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        return {"name": name, "ok": False, "error": f"timeout_{timeout}s", "stdout_tail": _redact_text(e.stdout or "")[-1200:]}
    except Exception as e:
        return {"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"}

    parsed = _redact_obj(_parse_last_json(proc.stdout or ""))
    ok = proc.returncode == 0
    if isinstance(parsed, dict):
        ok = ok and bool(parsed.get("success", parsed.get("ok", True)))
    return {
        "name": name,
        "ok": bool(ok),
        "returncode": proc.returncode,
        "parsed": parsed,
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


def _laf_portal_live() -> dict[str, Any]:
    sys.path.insert(0, str(REPO_ROOT))
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import scripts.laf_nightly_audit as audit

        result = audit.scan_portal_pending_drafts(db=None)
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
            elif "case_status_drafts" in parsed:
                detail = (
                    f"案件狀態暫存 {parsed.get('case_status_drafts')} / "
                    f"二階段 {parsed.get('condition_pending')} / 開辦 {parsed.get('go_live_pending')}"
                )
            elif parsed.get("errors"):
                detail = str(parsed.get("errors"))[:120]
        if not detail and r.get("error"):
            detail = str(r.get("error"))[:120]
        lines.append(f"{mark} {r.get('name')}: {detail}".rstrip())
    return "\n".join(lines)


def _notify(text: str) -> None:
    if str(os.environ.get("MAGI_BUSINESS_LIVE_CHECK_NOTIFY", "0")).lower() not in {"1", "true", "yes", "on"}:
        return
    try:
        from skills.ops.red_phone import send_telegram_push_with_status

        send_telegram_push_with_status(
            text,
            severity="warning",
            source="business_module_live_check",
            topic_key="check",
        )
    except Exception:
        pass


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run non-destructive MAGI business module LIVE/health checks.")
    parser.add_argument("--json", action="store_true", help="Compatibility flag; output is JSON by default.")
    parser.add_argument("--skip-laf-live", action="store_true", help="Skip live LAF portal login/scan.")
    parser.add_argument("--notify", action="store_true", help="Send the summary through the internal check topic.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.notify:
        os.environ["MAGI_BUSINESS_LIVE_CHECK_NOTIFY"] = "1"

    results = [
        _run("laf_self_test", [PYTHON, str(REPO_ROOT / "skills" / "laf-orchestrator" / "action.py"), "--task", "self_test"], timeout=120),
        _run("file_review_self_test", [PYTHON, str(REPO_ROOT / "skills" / "file-review-orchestrator" / "action.py"), "--task", "self_test"], timeout=120),
        _run(
            "file_review_downloadable_probe",
            [PYTHON, str(REPO_ROOT / "skills" / "file-review-orchestrator" / "action.py"), "--task", 'downloadable_probe {"days":30,"notify":false}'],
            timeout=900,
        ),
        _run("transcript_self_test", [PYTHON, str(REPO_ROOT / "skills" / "transcript-downloader" / "action.py"), "--task", "self_test"], timeout=120),
        _run("transcript_db_probe", [PYTHON, str(REPO_ROOT / "skills" / "transcript-downloader" / "action.py"), "--task", "db_probe"], timeout=180),
    ]
    if args.skip_laf_live:
        results.insert(1, {"name": "laf_portal_live", "ok": True, "skipped": True, "parsed": {"error": None}})
    else:
        results.insert(1, _laf_portal_live())
    ok = all(bool(r.get("ok")) for r in results)
    out = {"ok": ok, "success": ok, "results": results, "message": _summarize(results)}
    _notify(out["message"])
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
