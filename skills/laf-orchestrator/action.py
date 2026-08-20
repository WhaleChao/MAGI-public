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
import logging
import os
import secrets
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

PORTAL_ACTIONS = {"closing", "go_live", "inquiry", "withdrawal", "fee", "condition", "progress"}

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
        # Try to extract JSON from stdout.  The orchestrator wraps portal
        # results in sentinel markers because verbose browser logs may appear
        # around a multi-line JSON object.
        result = None
        start = "===MAGI_RESULT_JSON_START==="
        end = "===MAGI_RESULT_JSON_END==="
        if start in stdout and end in stdout:
            try:
                block = stdout.split(start, 1)[1].split(end, 1)[0].strip()
                result = json.loads(block)
            except Exception:
                result = None
        for line in reversed(stdout.splitlines()):
            if result is not None:
                break
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                    break
                except Exception:
                    continue
        parse_failed = False
        if result is None and stdout:
            # Try the whole stdout as JSON
            try:
                result = json.loads(stdout)
            except Exception:
                parse_failed = True
                result = {"raw_stdout": stdout[-3000:]}
        payload_ok = True
        if isinstance(result, dict):
            nested = result.get("result")
            if isinstance(nested, dict) and (nested.get("success") is False or nested.get("ok") is False):
                payload_ok = False
            if result.get("success") is False or result.get("ok") is False:
                payload_ok = False
        return {
            "success": r.returncode == 0 and payload_ok and not parse_failed,
            "returncode": r.returncode,
            "result": result or {},
            "error": "json_parse_failed" if parse_failed else (str(result.get("error") or "") if isinstance(result, dict) else ""),
            "stderr_tail": stderr[-1000:] if stderr else "",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout", "timeout_seconds": timeout}
    except Exception as e:
        return {"success": False, "error": str(e)[:500]}


def _probe_orchestrator_db(timeout: int = 20) -> dict:
    """Probe the LAF database with a bounded, strictly read-only query.

    Instantiating ``LAFOrchestrator`` also constructs the legacy
    ``DatabaseManager``, whose constructor performs schema DDL. That made this
    health check contend with live work and time out even when the database was
    healthy. Use the same bound OSC profile resolver and issue only ``SELECT
    1`` instead.
    """

    py = _choose_runtime_python()
    code = f"""
import json
import logging
import sys
logging.disable(logging.CRITICAL)
sys.path.insert(0, {str(MAGI_ROOT / 'skills' / 'osc-orchestrator')!r})
from osc_headless.db import DBConfig, connect_mysql, db_config_from_env
base = db_config_from_env(prefix='OSC_DB_')
cfg = DBConfig(
    host=base.host,
    port=int(base.port),
    user=base.user,
    password=base.password,
    database=base.database,
    connection_timeout=max(1, min(5, int(base.connection_timeout or 5))),
)
conn = connect_mysql(cfg)
try:
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT 1 AS healthcheck')
        row = cursor.fetchone()
    finally:
        cursor.close()
    conn.rollback()
finally:
    conn.close()
print(json.dumps({{'success': bool(row), 'db': bool(row), 'read_only_probe': True}}, ensure_ascii=False))
"""
    try:
        result = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=timeout, cwd=CODE_ROOT)
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "timeout"}
    except Exception as exc:
        return {"success": False, "error": str(exc)[:500]}
    stdout = (result.stdout or "").strip()
    payload = None
    decoder = json.JSONDecoder()
    for start in reversed([idx for idx, char in enumerate(stdout) if char == "{"]):
        try:
            candidate, end = decoder.raw_decode(stdout[start:])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(candidate, dict) and not stdout[start + end :].strip():
            payload = candidate
            break
    if payload is None:
        payload = {}
    return {
        "success": result.returncode == 0 and isinstance(payload, dict) and payload.get("success") is True,
        "returncode": result.returncode,
        "result": payload if isinstance(payload, dict) else {},
        "error": "" if payload else "json_parse_failed",
        "stderr_tail": (result.stderr or "")[-1000:],
    }


def _retry_error_label(result: dict) -> str:
    """Map internal failures to a small, non-sensitive notification label."""
    details = [
        str(result.get("error") or ""),
        str(result.get("stderr_tail") or ""),
        str((result.get("result") or {}).get("error") or "") if isinstance(result.get("result"), dict) else "",
    ]
    text = "\n".join(details).lower()
    if "captcha" in text:
        return "captcha_required"
    if "csrf" in text or "session" in text:
        return "portal_session_invalid"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "credential" in text or "unauthorized" in text or "forbidden" in text or "login" in text:
        return "authentication_failed"
    if "attachment" in text or "upload" in text:
        return "attachment_failed"
    if "connection" in text or "network" in text or "dns" in text:
        return "connection_failed"
    if "json_parse_failed" in text or "json" in text:
        return "result_parse_failed"
    return "portal_draft_failed"


def _retry_user_reason(error_label: str) -> str:
    """Translate an internal retry category into plain Taiwanese usage."""
    return {
        "captcha_required": "法扶網站要求重新完成驗證",
        "portal_session_invalid": "法扶網站登入狀態已失效",
        "timeout": "法扶網站回應時間過長",
        "authentication_failed": "法扶網站登入未完成",
        "attachment_failed": "附件暫時未能完整上傳",
        "connection_failed": "目前無法穩定連上法扶網站",
        "result_parse_failed": "法扶網站回傳內容暫時無法確認",
        "portal_draft_failed": "法扶網站暫存作業尚未完成",
    }.get(error_label, "法扶網站暫存作業尚未完成")


def _notify_retrying_after_failure(action: str, _target: str, result: dict) -> None:
    """Tell business channels MAGI is retrying after a failed LAF draft run."""
    if str(os.environ.get("MAGI_LAF_NOTIFY_RETRY_ON_FAILURE", "1")).strip().lower() not in {
        "1", "true", "yes", "on"
    }:
        return
    act = (action or "").strip().lower()
    if act != "closing":
        return
    trace_id = f"laf-retry-{secrets.token_hex(6)}"
    error_label = _retry_error_label(result)
    result["retry_trace_id"] = trace_id
    msg = (
        "法扶結案暫存尚未完成，MAGI 正在自動重試。\n"
        f"原因：{_retry_user_reason(error_label)}。\n"
        "這次沒有正式送出；完成後會再回覆結果。"
    )
    logging.getLogger(__name__).warning(
        "LAF retry notification queued: action=%s error_label=%s trace_id=%s",
        act,
        error_label,
        trace_id,
    )
    try:
        from skills.ops.red_phone import send_telegram_push_with_status, _send_discord_bot_message
        send_telegram_push_with_status(
            msg,
            severity="info",
            source="laf_closing_retry",
            topic_key="laf_closing",
            queue_on_fail=True,
        )
        _send_discord_bot_message(msg, "info", topic_key="laf_closing", source="laf_closing_retry")
    except Exception:
        try:
            from api.discord_channel_router import send as _dc_send
            _dc_send("laf_general", msg, level="info")
        except Exception:
            pass


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
        runtime_dir = Path(
            os.environ.get("MAGI_RUNTIME_DIR", "").strip()
            or Path(MAGI_ROOT) / ".runtime"
        ).expanduser()
        cache_dir = runtime_dir / "pycache" / "laf"
        cache_dir.mkdir(parents=True, exist_ok=True)
        py_compile.compile(
            SOURCE_FILE,
            cfile=str(cache_dir / "laf_orchestrator.pyc"),
            doraise=True,
        )
        report["compile"] = {"ok": True}
    except Exception as e:
        report["compile"] = {"ok": False, "error": str(e)[:500]}
        report["success"] = False
        return report

    # 2) Import + one read-only DB query. Do not use --mode dry-run here: its
    # CLI deliberately scans all pending closing cases.
    r = _probe_orchestrator_db(timeout=20)
    report["orchestrator_reachable"] = bool(r.get("success", False))
    report["orchestrator_db_probe"] = r.get("result") or {}
    if not report["orchestrator_reachable"]:
        report["orchestrator_error"] = str(r.get("error") or "db_probe_failed")[:120]
    report["success"] = bool(report["compile"]["ok"] and report["orchestrator_reachable"])
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
                       reason="", fields_json="", suppress_notify=False):
    """Execute a portal action via laf_orchestrator.py --mode portal-draft."""
    # CLI portal-draft timeout: default 900s (15 min) to accommodate LAF CSRF delays,
    # NAS attachment scan, form fill, and screenshot. Discord path uses 2400s.
    portal_timeout = int(os.environ.get("MAGI_LAF_PORTAL_DRAFT_TIMEOUT_SEC", "900"))
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
    if suppress_notify:
        args_list.append("--no-notify")
    args_list.append("-v")

    result = _run_orchestrator(args_list, timeout=portal_timeout)
    result["product_profile"] = product_profile_report("laf")
    if not result.get("success") and not suppress_notify:
        _notify_retrying_after_failure(
            action,
            laf_case_no or case_number or client_name,
            result,
        )
    return result


def task_portal_submit(action, laf_case_no="", case_number="", client_name="",
                       reason="", fields_json="", suppress_notify=False):
    """Execute a portal action via laf_orchestrator.py --mode portal-submit.

    Used for the second phase of T3 confirm-token flows (e.g. progress submit).
    Requires the caller to have set the appropriate MAGI_LAF_ALLOW_*_SUBMIT
    env variable to "1" (handled by laf_flow._run_progress_submit).
    """
    portal_timeout = int(os.environ.get("MAGI_LAF_REPORT_TIMEOUT_SEC", "2400"))
    args_list = [
        "--mode", "portal-submit",
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
    if suppress_notify:
        args_list.append("--no-notify")
    args_list.append("-v")

    result = _run_orchestrator(args_list, timeout=portal_timeout)
    result["product_profile"] = product_profile_report("laf")
    return result


def cmd_confirm_progress(token: str, *, source: str = "", platform: str = "") -> dict:
    """律師回覆確認碼 → 真送出進度回報（Plan C 兩階段確認碼 Stage 2）。

    安全閘門：source 必須含 user/telegram/discord/line，防止 CLI 直接呼叫。
    可用 MAGI_LAF_ALLOW_PROGRESS_CONFIRM=1 在測試時 bypass。

    此函式是給 Discord / LINE bot 呼叫的 API wrapper。
    實際 confirm 邏輯由 api.domains.laf_flow.handle_laf_progress_submit_confirmation_if_any 執行。
    """
    token = (token or "").strip().upper()
    if not token:
        return {"ok": False, "error": "token 不可為空"}

    # 安全閘門
    _user_sources = {"user", "telegram", "discord", "line"}
    source_lower = (source or "").lower()
    _is_user_src = any(s in source_lower for s in _user_sources)
    if not _is_user_src:
        allow_bypass = str(os.environ.get("MAGI_LAF_ALLOW_PROGRESS_CONFIRM", "0")).strip().lower() in {
            "1", "true", "yes", "on"
        }
        if not allow_bypass:
            return {
                "ok": False,
                "error": "confirm_progress 需從使用者來源觸發（user/telegram/discord/line），"
                         "或設 MAGI_LAF_ALLOW_PROGRESS_CONFIRM=1（測試用）",
            }

    # 呼叫 api.domains.laf_flow.handle_laf_progress_submit_confirmation_if_any
    try:
        from api.domains import laf_flow as _laf_domain
    except ImportError as e:
        return {"ok": False, "error": f"api.domains.laf_flow 無法載入: {e}"}

    # 建立最小 orchestrator stub（只需 pending file 路徑 + notification_callback）
    class _MinimalOrch:
        _laf_progress_submit_pending_file: str = ""
        notification_callback = None

    _orch = _MinimalOrch()

    result = _laf_domain.handle_laf_progress_submit_confirmation_if_any(
        _orch,
        platform=str(platform or "").strip() or "cli",
        user_id="cli_confirm",
        text=token,
    )
    if result is None:
        return {"ok": False, "error": f"token {token} 無效、已使用或已過期"}
    if isinstance(result, dict):
        return {
            "ok": result.get("handled", False),
            "message": result.get("message", ""),
        }
    return {"ok": False, "error": "unexpected result type from handle_laf_progress_submit_confirmation_if_any"}


# ── main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="LAF Orchestrator MAGI Skill (v2.0)"
    )
    parser.add_argument("--task", default="summary",
                        help="closing|go_live|inquiry|withdrawal|fee|condition|progress|"
                             "progress_report|preview_counts|self_test|summary")
    parser.add_argument("--laf-case-no", default="", help="法扶案號 e.g. 1140806-J-002")
    parser.add_argument("--case", default="", help="OSC 案號 e.g. 2025-0022")
    parser.add_argument("--client", default="", help="當事人姓名")
    parser.add_argument("--reason", default="", help="理由/說明文字")
    parser.add_argument("--fields-json", default="", help="附加欄位 JSON")
    # T3: progress_report specific args
    parser.add_argument("--case_no", default="", help="法扶案號（progress_report 專用別名）")
    parser.add_argument("--client_name", default="", help="當事人姓名（progress_report 專用別名）")
    parser.add_argument("--mode", default="draft",
                        help="draft（填寫截圖）或 submit（送出）")
    parser.add_argument("--no-notify", action="store_true",
                        help="suppress Discord notification")

    args = parser.parse_args()
    task = (args.task or "").strip().lower()
    # Normalize aliases: --case_no / --client_name → --laf-case-no / --client
    if args.case_no and not args.laf_case_no:
        args.laf_case_no = args.case_no
    if args.client_name and not args.client:
        args.client = args.client_name

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
        return 1 if result.get("success") is False else 0

    # ── progress_report (T3) ──
    # draft path: fill form + screenshot (portal-draft mode)
    # submit path: re-open form and send for real (portal-submit mode, mode=submit)
    if task in {"progress_report", "progress_draft", "progress_submit"}:
        if not args.laf_case_no and not args.client and not args.case:
            print(json.dumps({"success": False, "error": "需要 --case_no 或 --laf-case-no 或 --client"},
                             ensure_ascii=False))
            return 1
        _mode = (args.mode or "draft").strip().lower()
        if _mode == "submit":
            # P0-2: when the confirm token is verified, submit (not draft) the form
            result = task_portal_submit(
                action="progress",
                laf_case_no=args.laf_case_no,
                case_number=args.case,
                client_name=args.client,
                reason=args.reason or "",
                fields_json=args.fields_json,
                suppress_notify=bool(getattr(args, 'no_notify', False)),
            )
        else:
            result = task_portal_action(
                action="progress",
                laf_case_no=args.laf_case_no,
                case_number=args.case,
                client_name=args.client,
                reason=args.reason or "",
                fields_json=args.fields_json,
                suppress_notify=bool(getattr(args, 'no_notify', False)),
            )
        result["product_profile"] = product_profile_report("laf")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0 if result.get("success") else 1

    # ── reconcile_placeholder：用接案清冊 Excel 修正 placeholder LAF 案件 ──
    if task in {"reconcile_placeholder", "reconcile", "reconcile_placeholders"}:
        try:
            sys.path.insert(0, CODE_ROOT)
            from laf_nightly_audit import reconcile_placeholder_cases, _get_db  # type: ignore
        except Exception as e:
            print(json.dumps({"success": False, "error": f"import failed: {e}"},
                             ensure_ascii=False))
            return 1

        db = _get_db()
        if not db:
            print(json.dumps({"success": False, "error": "db init failed"},
                             ensure_ascii=False))
            return 1

        notifier = None
        if not getattr(args, "no_notify", False):
            try:
                from line_notifier import LAFNotifier  # type: ignore
                notifier = LAFNotifier()
            except Exception:
                notifier = None

        force_flag = bool(args.laf_case_no)  # 單筆觸發即視為 force（跳過節流）
        result = reconcile_placeholder_cases(
            db,
            force=force_flag,
            only_laf_no=(args.laf_case_no or "").strip(),
            notifier=notifier,
        )
        out = {"success": "error" not in result, **result}
        print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
        return 0 if out["success"] else 1

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
            suppress_notify=bool(getattr(args, 'no_notify', False)),
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
