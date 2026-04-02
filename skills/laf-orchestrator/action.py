#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
laf-orchestrator/action.py  v2.0

法扶案件自動化 MAGI 技能入口。
支援直接執行 closing / go_live / inquiry / withdrawal / fee / condition，
或 preview_counts 僅查看次數不操作 portal。

Usage:
  python action.py --task closing --laf-case-no "1140806-J-002" --client "陳賜聰"
  python action.py --task preview_counts --client "莊依稜"
  python action.py --task self_test
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

MAGI_ROOT = Path(os.environ.get("MAGI_ROOT_DIR", str(Path(__file__).resolve().parents[2]))).expanduser()
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

from api.runtime_paths import get_laf_script, get_orch_dir, get_skill_python
from api.product_runtime import apply_product_runtime_env, product_profile_report

SOURCE_FILE = str(get_laf_script())
CODE_ROOT = str(get_orch_dir())
LAF_RUNTIME = apply_product_runtime_env("laf", env=os.environ)

PORTAL_ACTIONS = {"closing", "go_live", "inquiry", "withdrawal", "fee", "condition"}

# ── helpers ──────────────────────────────────────────────────────────────

def _candidate_pythons():
    candidates = [str(get_skill_python()), sys.executable]
    sys_py = "/usr/bin/python3"
    if os.path.exists(sys_py) and sys_py not in candidates:
        candidates.append(sys_py)
    extra = os.environ.get("MAGI_CODE_SKILL_PYTHONS", "")
    for item in (extra or "").split(","):
        item = item.strip()
        if item and item not in candidates and os.path.exists(item):
            candidates.append(item)
    seen = set()
    out = []
    for p in candidates:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out[:4]


def _choose_runtime_python():
    for py in _candidate_pythons():
        try:
            r = subprocess.run(
                [py, "-c", "import mysql.connector; print('ok')"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                return py
        except Exception:
            continue
    return _candidate_pythons()[0] if _candidate_pythons() else sys.executable


def _run_orchestrator(args_list, timeout=300, extra_env=None):
    """Run laf_orchestrator.py as subprocess with given args."""
    py = _choose_runtime_python()
    cmd = [py, SOURCE_FILE] + args_list
    run_env = os.environ.copy()
    if isinstance(extra_env, dict):
        run_env.update({str(k): str(v) for k, v in extra_env.items() if v is not None})
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=CODE_ROOT,
            env=run_env,
        )
        stdout = (r.stdout or "").strip()
        stderr = (r.stderr or "").strip()
        # Try to extract JSON from stdout (last valid JSON block)
        result = None
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                    break
                except Exception:
                    continue
        if result is None and stdout:
            # Try the whole stdout as JSON
            try:
                result = json.loads(stdout)
            except Exception:
                result = {"raw_stdout": stdout[-3000:]}
        return {
            "success": r.returncode == 0,
            "returncode": r.returncode,
            "result": result or {},
            "stderr_tail": stderr[-1000:] if stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout", "timeout_seconds": timeout}
    except Exception as e:
        return {"success": False, "error": str(e)[:500]}


# ── task handlers ────────────────────────────────────────────────────────

def task_self_test():
    """Compile check + quick DB connectivity test."""
    report = {
        "mode": "self_test",
        "source_file": SOURCE_FILE,
        "product_profile": product_profile_report("laf"),
    }

    # 1) Compile check
    try:
        import py_compile
        py_compile.compile(SOURCE_FILE, doraise=True)
        report["compile"] = {"ok": True}
    except Exception as e:
        report["compile"] = {"ok": False, "error": str(e)[:500]}
        report["success"] = False
        return report

    # 2) Quick DB test via orchestrator
    r = _run_orchestrator(["--mode", "dry-run", "--help"], timeout=15)
    report["orchestrator_reachable"] = r.get("success", False)
    report["success"] = report["compile"]["ok"]
    return report


def task_preview_counts(client_name, case_number="", laf_case_no=""):
    """Query counts without touching the portal."""
    py = _choose_runtime_python()
    code = f"""
import json, sys, logging
logging.disable(logging.CRITICAL)  # suppress INFO noise
sys.path.insert(0, {CODE_ROOT!r})
from laf_orchestrator import LAFOrchestrator
o = LAFOrchestrator(dry_run=True)
ident = o._lookup_case_identity(
    laf_case_number={laf_case_no!r},
    case_number={case_number!r},
    client_name={client_name!r},
)
osc_no = ident.get("case_number") or {case_number!r}
cname = ident.get("client_name") or {client_name!r}
logging.disable(logging.NOTSET)
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    counts = o._gather_case_counts(osc_no, cname)
log_lines = buf.getvalue().strip().splitlines()[-20:]
print(json.dumps({{"identity": ident, "counts": counts, "log": log_lines}}, ensure_ascii=False, indent=2, default=str))
"""
    try:
        r = subprocess.run(
            [py, "-c", code],
            capture_output=True, text=True, timeout=60, cwd=CODE_ROOT,
        )
        stdout = (r.stdout or "").strip()
        # Extract the last JSON object from stdout (skip non-JSON INFO lines)
        json_start = stdout.rfind("\n{")
        if json_start >= 0:
            stdout = stdout[json_start + 1:]
        elif stdout.startswith("{"):
            pass  # already clean
        try:
            data = json.loads(stdout)
            if isinstance(data, dict):
                data["product_profile"] = product_profile_report("laf")
            return data
        except Exception:
            return {"raw": stdout[-2000:], "stderr": (r.stderr or "")[-500:]}
    except Exception as e:
        return {"success": False, "error": str(e)[:500]}


def task_portal_action(action, laf_case_no="", case_number="", client_name="",
                       reason="", fields_json=""):
    """Execute a portal action via laf_orchestrator.py --mode portal-draft."""
    args_list = [
        "--mode", "portal-draft",
        "--action", action,
    ]
    if laf_case_no:
        args_list += ["--laf-case-no", laf_case_no]
    if case_number:
        args_list += ["--case", case_number]
    if client_name:
        args_list += ["--client", client_name]
    if reason:
        args_list += ["--reason", reason]
    if fields_json:
        args_list += ["--fields-json", fields_json]
    args_list.append("-v")

    result = _run_orchestrator(args_list, timeout=300)
    result["product_profile"] = product_profile_report("laf")
    return result


# ── main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LAF Orchestrator MAGI Skill (v2.0)"
    )
    parser.add_argument("--task", default="summary",
                        help="closing|go_live|inquiry|withdrawal|fee|condition|"
                             "preview_counts|self_test|summary")
    parser.add_argument("--laf-case-no", default="", help="法扶案號 e.g. 1140806-J-002")
    parser.add_argument("--case", default="", help="OSC 案號 e.g. 2025-0022")
    parser.add_argument("--client", default="", help="當事人姓名")
    parser.add_argument("--reason", default="", help="理由/說明文字")
    parser.add_argument("--fields-json", default="", help="附加欄位 JSON")

    args = parser.parse_args()
    task = (args.task or "").strip().lower()

    if not os.path.exists(SOURCE_FILE):
        print(json.dumps({"success": False, "error": f"source missing: {SOURCE_FILE}"},
                         ensure_ascii=False))
        return 1

    # ── summary / help ──
    if task in {"summary", "help", "list"}:
        print(json.dumps({
            "success": True,
            "mode": "metadata",
            "version": "2.0",
            "source_file": SOURCE_FILE,
            "product_profile": product_profile_report("laf"),
            "available_tasks": sorted(PORTAL_ACTIONS | {"preview_counts", "self_test", "summary"}),
            "usage": {
                "closing": 'python action.py --task closing --laf-case-no "..." --client "..."',
                "preview_counts": 'python action.py --task preview_counts --client "..."',
                "self_test": "python action.py --task self_test",
            },
        }, ensure_ascii=False, indent=2))
        return 0

    # ── self_test ──
    if task in {"self_test", "selftest", "self test"}:
        result = task_self_test()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("success") else 1

    # ── preview_counts ──
    if task in {"preview_counts", "preview", "counts", "查看次數"}:
        if not args.client and not args.laf_case_no and not args.case:
            print(json.dumps({"success": False, "error": "需要 --client 或 --laf-case-no"},
                             ensure_ascii=False))
            return 1
        result = task_preview_counts(
            client_name=args.client,
            case_number=args.case,
            laf_case_no=args.laf_case_no,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    # ── portal actions ──
    if task in PORTAL_ACTIONS:
        if not args.client and not args.laf_case_no and not args.case:
            print(json.dumps({"success": False, "error": "需要 --client 或 --laf-case-no"},
                             ensure_ascii=False))
            return 1
        result = task_portal_action(
            action=task,
            laf_case_no=args.laf_case_no,
            case_number=args.case,
            client_name=args.client,
            reason=args.reason,
            fields_json=args.fields_json,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("success") else 1

    # ── fallback: pass to orchestrator directly ──
    print(json.dumps({
        "success": False,
        "error": f"unknown task: {task}",
        "available_tasks": sorted(PORTAL_ACTIONS | {"preview_counts", "self_test", "summary"}),
    }, ensure_ascii=False, indent=2))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
