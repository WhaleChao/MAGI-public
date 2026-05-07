# -*- coding: utf-8 -*-
"""Cron result classification helpers.

Cron jobs sometimes print a structured success payload even when a wrapper
returns a non-zero code. Treat those as recovered false positives so the issue
agenda does not hide real failures behind noise.
"""

from __future__ import annotations

import json
from typing import Any, Dict


_SUCCESS_MARKERS = (
    "✅ 未發現角色幻覺污染記憶",
    "✅ 報告已發送",
    "✅ Shell job",
)


def _last_json_object(text: str) -> Dict[str, Any] | None:
    """Best-effort parse of the last JSON object printed by a cron script."""
    if not text:
        return None
    stripped = text.strip()
    candidates = [stripped]
    start = stripped.rfind("{")
    if start > 0:
        candidates.append(stripped[start:])
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def looks_successful_despite_returncode(stdout: str, stderr: str) -> bool:
    """Return True when captured output is strong evidence of success."""
    obj = _last_json_object(stdout)
    if obj:
        success = obj.get("success")
        ok = obj.get("ok")
        if success is True or ok is True:
            severity = str(obj.get("severity") or "").upper()
            alarm_triggered = obj.get("alarm_triggered")
            if severity in {"", "OK", "INFO"} and alarm_triggered in {None, False}:
                return True
    clean_stdout = (stdout or "").strip()
    clean_stderr = (stderr or "").strip()
    if clean_stdout and not clean_stderr:
        if any(marker in clean_stdout for marker in _SUCCESS_MARKERS):
            return "❌" not in clean_stdout and "Traceback" not in clean_stdout
    return False


def should_log_cron_issue(returncode: int, stdout: str, stderr: str) -> bool:
    """Decide whether a non-zero cron result should become an issue agenda item."""
    if int(returncode or 0) == 0:
        return False
    return not looks_successful_despite_returncode(stdout or "", stderr or "")
