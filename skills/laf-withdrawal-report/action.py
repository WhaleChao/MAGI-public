#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
laf-withdrawal-report/action.py

Wrapper around:
  MAGI laf_orchestrator.py --mode portal-draft --action withdrawal
"""

from __future__ import annotations
import logging

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import get_laf_script

ORCH = str(get_laf_script())


def _print(payload: Dict[str, Any]) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("success") else 1


def _parse_task(task: str) -> Dict[str, str]:
    text = (task or "").strip()
    out = {"client_name": "", "laf_case_no": "", "case_number": "", "reason": ""}
    if not text:
        return out

    # JSON mode
    if text.startswith("{"):
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for k in out.keys():
                    v = data.get(k)
                    if v is not None:
                        out[k] = str(v).strip()
                return out
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 51, exc_info=True)

    # reason
    m_reason = re.search(r"(?:原因|理由|說明)\s*(?:是|為|:|：)?\s*(.+)$", text)
    if m_reason:
        out["reason"] = (m_reason.group(1) or "").strip()

    # LAF case no (ex: 1140728-K-002)
    m_laf = re.search(r"(\d{6,8}-[A-Za-z]-\d{3})", text)
    if m_laf:
        out["laf_case_no"] = (m_laf.group(1) or "").strip()

    # OSC case no (ex: 2026-0013)
    m_case = re.search(r"\b(\d{4}-\d{4})\b", text)
    if m_case:
        out["case_number"] = (m_case.group(1) or "").strip()

    # name fallback
    if not out["client_name"]:
        m_name = re.search(
            r"(?:幫我(?:做)?|請(?:幫)?|幫)?\s*([一-龥A-Za-z][一-龥A-Za-z0-9_.\- ]{1,60}?)\s*(?:的)?(?:受扶助人撤回|撤回)(?:回報)?",
            text,
        )
        if m_name:
            out["client_name"] = (m_name.group(1) or "").strip()

    return out


def _run(payload: Dict[str, str], timeout_sec: int = 1500) -> Dict[str, Any]:
    if not (payload.get("client_name") or payload.get("laf_case_no") or payload.get("case_number")):
        return {"success": False, "error": "missing_target", "hint": "請提供姓名或案號"}
    if not payload.get("reason"):
        return {"success": False, "error": "missing_reason", "hint": "請提供撤回原因，例如：原因 申請人撤回"}

    env = os.environ.copy()
    env.setdefault("MAGI_NO_DELETE", "1")

    cmd = [sys.executable, ORCH, "--mode", "portal-draft", "--action", "withdrawal"]
    if payload.get("client_name"):
        cmd.extend(["--client", payload["client_name"]])
    if payload.get("laf_case_no"):
        cmd.extend(["--laf-case-no", payload["laf_case_no"]])
    if payload.get("case_number"):
        cmd.extend(["--case", payload["case_number"]])
    cmd.extend(["--reason", payload["reason"]])

    try:
        r = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(30, int(timeout_sec)),
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout"}
    except Exception as e:
        return {"success": False, "error": f"spawn_failed: {e}"}

    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    parsed: Dict[str, Any] = {}
    try:
        parsed = json.loads(out) if out else {}
    except Exception:
        m = re.search(r"(\{[\s\S]*\})\s*$", out)
        if m:
            try:
                parsed = json.loads(m.group(1))
            except Exception:
                parsed = {"raw_stdout": out[:1200]}
        else:
            parsed = {"raw_stdout": out[:1200]}

    ok = (r.returncode == 0) and bool(parsed.get("ok"))
    return {
        "success": bool(ok),
        "returncode": r.returncode,
        "result": parsed,
        "stderr_tail": err[-1200:],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="LAF withdrawal report skill")
    ap.add_argument("--task", default="help", help="help | self_test | run <json/nl>")
    args = ap.parse_args()
    task = (args.task or "").strip()
    lower = task.lower()

    if lower in {"help", "summary", "list"}:
        return _print(
            {
                "success": True,
                "commands": [
                    "help",
                    "self_test",
                    'run {"client_name":"[當事人F]","reason":"申請人撤回"}',
                    "run 幫我做[當事人F]受扶助人撤回 原因 申請人撤回",
                ],
            }
        )

    if lower == "self_test":
        payload = {"client_name": "[當事人F]", "reason": "申請人撤回（自測）"}
        res = _run(payload, timeout_sec=30)
        # self_test 不要求成功，只檢查 wrapper 可執行與錯誤結構。
        return _print({"success": True, "self_test": res})

    if lower.startswith("run"):
        body = task[3:].strip()
        payload = _parse_task(body)
        return _print(_run(payload))

    return _print({"success": False, "error": f"unknown task: {task}"})


if __name__ == "__main__":
    raise SystemExit(main())
