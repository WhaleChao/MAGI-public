"""
LAF (Legal Aid Foundation) submission workflow extracted from Orchestrator.

All functions accept an `orch` parameter (the Orchestrator instance)
instead of `self`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import sys
import threading
import time

logger = logging.getLogger("Orchestrator")

_MAGI_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Subprocess result parser (sentinel-aware)
# ---------------------------------------------------------------------------
_MAGI_RESULT_SENTINEL_START = "===MAGI_RESULT_JSON_START==="
_MAGI_RESULT_SENTINEL_END = "===MAGI_RESULT_JSON_END==="


def _parse_subprocess_result(stdout_text: str):
    """Parse JSON result from a laf_orchestrator.py subprocess.

    Priority:
      1. Sentinel-delimited block (new — robust against logger noise / Playwright
         warnings printed before the JSON output).
      2. Whole-string json.loads (back-compat for older subprocess builds).
      3. Greedy regex (last-resort fallback).

    Returns (data_dict_or_None, parse_method_str).
    """
    if not stdout_text:
        return None, "empty_stdout"
    # 1) Sentinel-delimited block — preferred
    s_idx = stdout_text.rfind(_MAGI_RESULT_SENTINEL_START)
    if s_idx >= 0:
        body_start = s_idx + len(_MAGI_RESULT_SENTINEL_START)
        e_idx = stdout_text.find(_MAGI_RESULT_SENTINEL_END, body_start)
        if e_idx > body_start:
            block = stdout_text[body_start:e_idx].strip()
            try:
                return json.loads(block), "sentinel"
            except Exception as _e:
                logger.warning("sentinel block JSON parse failed: %s; block_head=%r", _e, block[:200])
    # 2) Whole stdout
    try:
        return json.loads(stdout_text), "whole_stdout"
    except Exception:
        pass
    # 3) Last-resort greedy regex (legacy)
    m = re.search(r"(\{[\s\S]*\})\s*$", stdout_text)
    if m:
        try:
            return json.loads(m.group(1)), "regex_fallback"
        except Exception:
            pass
    return None, "parse_failed"


# ---------------------------------------------------------------------------
# LAF submit pending persistence
# ---------------------------------------------------------------------------

def load_laf_submit_pending(orch) -> dict:
    try:
        if os.path.exists(orch._laf_submit_pending_file):
            with open(orch._laf_submit_pending_file, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def save_laf_submit_pending(orch, data: dict) -> None:
    try:
        tmp = orch._laf_submit_pending_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data if isinstance(data, dict) else {}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, orch._laf_submit_pending_file)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "save_laf_submit_pending", exc_info=True)


# ---------------------------------------------------------------------------
# DB status update after LAF action
# ---------------------------------------------------------------------------

def update_laf_status_after_action(orch, *, case_number: str = "", client_name: str = "",
                                   laf_case_no: str = "",
                                   case_reason_hint: str = "",
                                   new_status: str, action_label: str = "") -> bool:
    """Update DB legal_aid_status after a successful LAF operation."""
    try:
        from api.runtime_paths import get_config_path
        from osc import DatabaseManager
        config_path = get_config_path("config.json")
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        db = None
        for profile in config.get("mariadb_profiles", []):
            try:
                db = DatabaseManager(profile["config"])
                break
            except Exception:
                continue
        if not db:
            return False
        rows: list = []
        if case_number:
            rows = db.fetch_all(
                "SELECT id, case_number, client_name, legal_aid_status, case_reason FROM cases WHERE case_number = %s",
                (case_number,), as_dict=True,
            ) or []
        if not rows and laf_case_no:
            rows = db.fetch_all(
                "SELECT id, case_number, client_name, legal_aid_status, case_reason FROM cases "
                "WHERE (legal_aid_number = %s OR laf_case_no = %s OR application_no = %s) LIMIT 1",
                (laf_case_no, laf_case_no, laf_case_no), as_dict=True,
            ) or []
        if not rows and client_name:
            rows = db.fetch_all(
                "SELECT id, case_number, client_name, legal_aid_status, legal_aid_number, case_reason FROM cases "
                "WHERE client_name LIKE %s AND (case_category='\u6cd5\u5f8b\u6276\u52a9\u6848\u4ef6' OR case_reason LIKE '%%\u6cd5\u6276%%') "
                "ORDER BY case_number DESC",
                (f"%{client_name}%",), as_dict=True,
            ) or []
        if not rows:
            return False
        if len(rows) > 1 and case_reason_hint:
            from api.handlers.laf_handler import _expand_reason_keywords
            keywords = _expand_reason_keywords(case_reason_hint)
            if keywords:
                filtered = [
                    r for r in rows
                    if any(kw in (r.get("case_reason") or "") for kw in keywords)
                ]
                if len(filtered) == 1:
                    rows = filtered
                elif filtered:
                    rows = filtered
        if len(rows) > 1 and action_label.startswith("\u624b\u52d5"):
            lines = [f"\u26a0\ufe0f \u627e\u5230 {len(rows)} \u4ef6\u300c{client_name}\u300d\u7684\u6cd5\u6276\u6848\u4ef6\uff0c\u8acb\u6307\u5b9a\u6848\u865f\u6216\u52a0\u4e0a\u6848\u7531\uff1a"]
            for r in rows:
                laf = r.get("legal_aid_number") or ""
                reason = r.get("case_reason") or ""
                _status = r.get("legal_aid_status") or "(\u7a7a)"
                lines.append(f"  \u2022 {r['case_number']} {r['client_name']} {reason} ({laf}) \u2014 {_status}")
            status_word = new_status.replace("\u9032\u884c\u4e2d", "\u958b\u8fa6").replace("\u5df2\u5831\u7d50", "\u5831\u7d50")
            _reason_hint = (rows[0].get("case_reason") or "\u6848\u7531")[:6]
            lines.append(f"\n\u7bc4\u4f8b\uff1a`{rows[0]['case_number']} \u5df2{status_word}` \u6216 `{client_name} {_reason_hint} \u5df2{status_word}`")
            orch._ambiguous_laf_status_hint = "\n".join(lines)
            return False
        row = rows[0]
        old = row.get("legal_aid_status") or "(\u7a7a)"
        db.execute_write(
            "UPDATE cases SET legal_aid_status = %s WHERE id = %s",
            (new_status, row["id"]),
        )
        logger.info("\U0001f4dd %s \u2192 DB legal_aid_status\u300c%s\u300d\u2192\u300c%s\u300d\uff08%s %s\uff09",
                    action_label, old, new_status, row.get("case_number"), row.get("client_name"))
        return True
    except Exception as e:
        logger.warning("_update_laf_status_after_action failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Go-live submit pending registration & resolution
# ---------------------------------------------------------------------------

def register_laf_go_live_submit_pending(orch, *, platform: str, requester_user_id: str, payload: dict, result_data: dict) -> dict:
    pending = load_laf_submit_pending(orch)
    token = secrets.token_hex(3).upper()
    expires_sec = int(os.environ.get("MAGI_LAF_GO_LIVE_CONFIRM_TTL_SEC", "1800") or "1800")
    now = time.time()
    entry = {
        "kind": "laf_go_live_submit",
        "token": token,
        "platform": str(platform or "").strip(),
        "requester_user_id": str(requester_user_id or "").strip(),
        "created_at": now,
        "expires_at": now + float(expires_sec),
        "status": "pending",
        "payload": payload or {},
        "result_data": result_data or {},
    }
    pending[token] = entry
    save_laf_submit_pending(orch, pending)
    return entry


def resolve_laf_go_live_pending_token(orch, platform: str, message: str) -> tuple[str, dict]:
    pending = load_laf_submit_pending(orch)
    msg = (message or "").strip()
    platform_norm = str(platform or "").strip().lower()

    now = time.time()
    removed = []
    for tk, e in list(pending.items()):
        if not isinstance(e, dict):
            removed.append(tk)
            continue
        exp = float(e.get("expires_at", 0.0) or 0.0)
        if exp and now > exp:
            removed.append(tk)
    if removed:
        for tk in removed:
            pending.pop(tk, None)
        save_laf_submit_pending(orch, pending)

    m = re.search(r"\b([A-F0-9]{6,12})\b", msg.upper())
    if m:
        tk = m.group(1)
        e = pending.get(tk)
        if isinstance(e, dict):
            if str(e.get("kind")) != "laf_go_live_submit":
                return "", {}
            if str(e.get("status")) != "pending":
                return "", {}
            if str(e.get("platform", "")).strip().lower() != platform_norm:
                return "", {}
            return tk, e
        # Message explicitly carried a token-like string. Do not fall back to the
        # platform-unique pending item, or a stale/foreign token could cancel the
        # wrong go-live submission.
        return "", {}

    cands = []
    for tk, e in pending.items():
        if not isinstance(e, dict):
            continue
        if str(e.get("kind")) != "laf_go_live_submit":
            continue
        if str(e.get("status")) != "pending":
            continue
        if str(e.get("platform", "")).strip().lower() != platform_norm:
            continue
        cands.append((tk, e))
    if len(cands) == 1:
        return cands[0]

    return "", {}


def handle_laf_submit_confirmation_if_any(orch, user_id: str, platform: str, role: str, message: str) -> tuple[bool, str]:
    msg = (message or "").strip()
    if not msg:
        return False, ""

    progress_result = handle_laf_progress_submit_confirmation_if_any(
        orch,
        platform=str(platform or ""),
        user_id=str(user_id or ""),
        text=msg,
    )
    if isinstance(progress_result, dict) and progress_result.get("handled"):
        return True, str(progress_result.get("message") or "")

    low = msg.lower()
    has_confirm_kw = any(k in low for k in ["\u6b63\u78ba", "\u78ba\u8a8d", "ok", "\u53ef\u4ee5\u9001\u51fa", "\u9001\u51fa"])
    has_cancel_kw = any(k in low for k in ["\u53d6\u6d88", "\u4e0d\u8981\u9001\u51fa", "\u5148\u4e0d\u8981", "\u66ab\u505c\u9001\u51fa"])
    has_laf_kw = any(k in msg for k in ["\u958b\u8fa6", "\u6cd5\u6276", "\u56de\u5831"])
    has_token = bool(re.search(r"\b([A-F0-9]{6,12})\b", msg.upper()))

    if not (has_confirm_kw or has_cancel_kw or has_token):
        return False, ""
    if not (has_laf_kw or has_token):
        tk_probe, _e_probe = resolve_laf_go_live_pending_token(orch, platform, "")
        if not tk_probe:
            return False, ""

    token, entry = resolve_laf_go_live_pending_token(orch, platform, msg)
    if not token or not isinstance(entry, dict):
        m = re.search(r"\b([A-F0-9]{6,12})\b", msg.upper())
        if m:
            tk = m.group(1)
            pending0 = load_laf_submit_pending(orch)
            e0 = pending0.get(tk) if isinstance(pending0, dict) else None
            if isinstance(e0, dict) and str(e0.get("kind")) == "laf_go_live_submit":
                platform_ok = str(e0.get("platform", "")).strip().lower() == str(platform or "").strip().lower()
                if not platform_ok:
                    return True, f"\u26a0\ufe0f \u78ba\u8a8d\u78bc {tk} \u5c6c\u65bc\u5176\u4ed6\u901a\u8a0a\u5e73\u53f0\uff0c\u8acb\u5728\u539f\u5e73\u53f0\u56de\u8986\u3002"
                return True, f"\u26a0\ufe0f \u9019\u7b46\u958b\u8fa6\u9001\u51fa\u78ba\u8a8d\u76ee\u524d\u72c0\u614b\u70ba\u300c{e0.get('status')}\u300d\uff0c\u4e0d\u80fd\u518d\u6b21\u9001\u51fa\u3002"
        return False, ""

    pending = load_laf_submit_pending(orch)
    ent = pending.get(token) if isinstance(pending, dict) else None
    if not isinstance(ent, dict):
        return True, "\u26a0\ufe0f \u9019\u7b46\u958b\u8fa6\u9001\u51fa\u78ba\u8a8d\u5df2\u4e0d\u5b58\u5728\u6216\u5df2\u904e\u671f\uff0c\u8acb\u91cd\u65b0\u57f7\u884c\u958b\u8fa6\u586b\u5beb\u6d41\u7a0b\u3002"
    if str(ent.get("status")) != "pending":
        return True, f"\u26a0\ufe0f \u9019\u7b46\u958b\u8fa6\u9001\u51fa\u78ba\u8a8d\u76ee\u524d\u72c0\u614b\u70ba\u300c{ent.get('status')}\u300d\uff0c\u4e0d\u80fd\u91cd\u8907\u9001\u51fa\u3002"

    allow_colleague = str(os.environ.get("MAGI_LAF_ALLOW_COLLEAGUE_CONFIRM", "1")).strip().lower() in {"1", "true", "yes", "on"}
    if role != "admin" and not allow_colleague:
        return True, "\u26d4 \u76ee\u524d\u53ea\u5141\u8a31\u7ba1\u7406\u54e1\u78ba\u8a8d\u9001\u51fa\u3002"

    if has_cancel_kw:
        ent["status"] = "cancelled"
        ent["cancelled_by"] = str(user_id or "")
        ent["cancelled_at"] = time.time()
        pending[token] = ent
        save_laf_submit_pending(orch, pending)
        return True, f"\U0001f6d1 \u5df2\u53d6\u6d88\u958b\u8fa6\u9001\u51fa\uff08\u78ba\u8a8d\u78bc {token}\uff09\u3002"

    # confirm -> submit in background
    ent["status"] = "submitting"
    ent["confirmed_by"] = str(user_id or "")
    ent["confirmed_at"] = time.time()
    pending[token] = ent
    save_laf_submit_pending(orch, pending)

    from api.runtime_paths import get_laf_script

    skill_python = (os.environ.get("MAGI_SKILL_PYTHON") or "").strip()
    if not skill_python:
        skill_python = f"{_MAGI_ROOT}/venv/bin/python"
    if not os.path.exists(skill_python):
        skill_python = sys.executable or "python3"

    laf_script = str(get_laf_script())
    payload = ent.get("payload") if isinstance(ent.get("payload"), dict) else {}
    result_data = ent.get("result_data") if isinstance(ent.get("result_data"), dict) else {}

    def _run_submit(uid: str, platform_name: str, token_id: str, payload_obj: dict, result_obj: dict):
        cmd = [skill_python, laf_script, "--mode", "portal-submit", "--action", "go_live"]
        if payload_obj.get("laf_case_no"):
            cmd.extend(["--laf-case-no", str(payload_obj.get("laf_case_no"))])
        if payload_obj.get("case_number"):
            cmd.extend(["--case", str(payload_obj.get("case_number"))])
        if payload_obj.get("client_name"):
            cmd.extend(["--client", str(payload_obj.get("client_name"))])
        fields = payload_obj.get("fields") if isinstance(payload_obj.get("fields"), dict) else {}
        if fields:
            cmd.extend(["--fields-json", json.dumps(fields, ensure_ascii=False)])

        env = os.environ.copy()
        env["MAGI_LAF_ALLOW_GO_LIVE_SUBMIT"] = "1"
        timeout_sec = int(os.environ.get("MAGI_LAF_REPORT_TIMEOUT_SEC", "2400") or "2400")
        text = ""
        success = False
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, env=env)
            stdout_text = (proc.stdout or "").strip()
            stderr_text = (proc.stderr or "").strip()
            data, _parse_method = _parse_subprocess_result(stdout_text)
            if proc.returncode != 0 and not (isinstance(data, dict) and data.get("ok")):
                # \u771f\u6b63\u5931\u6557\uff1areturncode \u975e\u96f6\u4e14 result \u4e5f\u4e0d\u662f ok=True
                text = f"\u274c \u958b\u8fa6\u9001\u51fa\u5931\u6557\uff08\u78ba\u8a8d\u78bc {token_id}\uff0ccode={proc.returncode}\uff09\n{(stderr_text or stdout_text)[:1200]}"
            else:
                if isinstance(data, dict) and data.get("ok"):
                    identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
                    cname = str(identity.get("client_name") or payload_obj.get("client_name") or "").strip()
                    laf_no = str(identity.get("laf_case_number") or payload_obj.get("laf_case_no") or "").strip()
                    osc_no = str(identity.get("case_number") or payload_obj.get("case_number") or "").strip()
                    parts = [x for x in [cname, laf_no, osc_no] if x]
                    preview = data.get("preview") if isinstance(data.get("preview"), dict) else {}
                    shot_url = ""
                    if isinstance(preview.get("png_export"), dict):
                        shot_url = str(preview.get("png_export", {}).get("url") or "").strip()
                    shot_path = str(preview.get("png") or "").strip()
                    lines = [f"\u2705 \u958b\u8fa6\u56de\u5831\u5df2\u9001\u51fa\uff08\u78ba\u8a8d\u78bc {token_id}\uff09"]
                    if parts:
                        lines.append("\u76ee\u6a19\uff1a" + "\uff5c".join(parts))
                    if shot_url:
                        lines.append(f"\u9001\u51fa\u5f8c\u756b\u9762\uff1a{shot_url}")
                    elif shot_path:
                        lines.append(f"\u9001\u51fa\u5f8c\u622a\u5716\uff1a{shot_path}")
                    text = "\n".join(lines)
                    success = True
                    try:
                        _update_case_no = osc_no or str(payload_obj.get("case_number") or "").strip()
                        _update_client = cname or str(payload_obj.get("client_name") or "").strip()
                        if _update_case_no or _update_client:
                            update_laf_status_after_action(
                                orch,
                                case_number=_update_case_no,
                                client_name=_update_client,
                                new_status="\u9032\u884c\u4e2d",
                                action_label="\u958b\u8fa6\u9001\u51fa",
                            )
                    except Exception as _db_err:
                        logger.warning("go_live DB status update failed: %s", _db_err)
                else:
                    err = ""
                    if isinstance(data, dict):
                        err = str(data.get("error") or "").strip()
                    text = f"\u274c \u958b\u8fa6\u9001\u51fa\u5931\u6557\uff08\u78ba\u8a8d\u78bc {token_id}\uff09\uff1a{err or (stdout_text[:500] if stdout_text else 'unknown')}"
        except subprocess.TimeoutExpired:
            text = f"\u23f3 \u958b\u8fa6\u9001\u51fa\u903e\u6642\uff08\u78ba\u8a8d\u78bc {token_id}\uff09\uff0c\u8acb\u7a0d\u5f8c\u6aa2\u67e5\u5e73\u53f0\u7d50\u679c\u3002"
        except Exception as e:
            text = f"\u274c \u958b\u8fa6\u9001\u51fa\u6d41\u7a0b\u7570\u5e38\uff08\u78ba\u8a8d\u78bc {token_id}\uff09\uff1a{e}"

        try:
            pending2 = load_laf_submit_pending(orch)
            e2 = pending2.get(token_id) if isinstance(pending2, dict) else None
            if isinstance(e2, dict):
                e2["status"] = "submitted" if success else "failed"
                e2["finished_at"] = time.time()
                pending2[token_id] = e2
                save_laf_submit_pending(orch, pending2)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, "_run_submit", exc_info=True)

        try:
            if getattr(orch, "notification_callback", None):
                orch.notification_callback(uid, text, platform_name, topic_key="laf_go_live")
        except Exception as notify_err:
            logger.warning(f"LAF submit callback failed: {notify_err}")

    threading.Thread(
        target=_run_submit,
        args=(str(user_id or ""), str(platform or ""), token, payload, result_data),
        daemon=True,
    ).start()

    return True, f"\u23f3 \u5df2\u6536\u5230\u78ba\u8a8d\uff0c\u958b\u59cb\u9001\u51fa\u958b\u8fa6\u56de\u5831\uff08\u78ba\u8a8d\u78bc {token}\uff09\u3002\u5b8c\u6210\u5f8c\u6211\u6703\u4e3b\u52d5\u56de\u5831\u3002"


# ---------------------------------------------------------------------------
# Progress submit pending (T3: 未結案件進度回報 confirm_token)
# ---------------------------------------------------------------------------

def _progress_pending_file(orch) -> str:
    """Return path to laf_progress_submit_pending.json (runtime dir)."""
    if hasattr(orch, "_laf_progress_submit_pending_file"):
        return str(orch._laf_progress_submit_pending_file)
    runtime_dir = os.path.join(_MAGI_ROOT, ".runtime")
    os.makedirs(runtime_dir, exist_ok=True)
    return os.path.join(runtime_dir, "laf_progress_submit_pending.json")


def _load_progress_pending(path: str) -> dict:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


def _save_progress_pending(path: str, data: dict) -> None:
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data if isinstance(data, dict) else {}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch _save_progress_pending", exc_info=True)


def register_laf_progress_submit_pending(
    orch, *, platform: str, requester_user_id: str, payload: dict, result_data: dict
) -> str:
    """Register a pending progress submit.  Returns the 6-hex confirm_token string."""
    pending_file = _progress_pending_file(orch)
    pending = _load_progress_pending(pending_file)
    token = secrets.token_hex(3).upper()
    expires_sec = int(os.environ.get("MAGI_LAF_PROGRESS_CONFIRM_TTL_SEC", "1800") or "1800")
    now = time.time()
    entry = {
        "kind": "laf_progress_submit",
        "token": token,
        "platform": str(platform or "").strip(),
        "requester_user_id": str(requester_user_id or "").strip(),
        "created_at": now,
        "expires_at": now + float(expires_sec),
        "status": "pending",
        "payload": payload or {},
        "result_data": result_data or {},
    }
    pending[token] = entry
    _save_progress_pending(pending_file, pending)
    return token


def find_pending_progress_token_for_case(orch, laf_case_no: str):
    """Return (token, entry) if an unexpired pending token exists for laf_case_no, else (None, None)."""
    pending_file = _progress_pending_file(orch)
    pending = _load_progress_pending(pending_file)
    now = time.time()
    for tk, entry in list(pending.items()):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("kind")) != "laf_progress_submit":
            continue
        if str(entry.get("status")) != "pending":
            continue
        exp = float(entry.get("expires_at", 0) or 0)
        if exp and now > exp:
            continue
        p = entry.get("payload") or {}
        if str(p.get("laf_case_no") or "").strip() == str(laf_case_no or "").strip():
            return tk, entry
    return None, None


def resolve_laf_progress_pending_token(orch, token: str):
    """Return the pending entry if token is valid + unexpired, else None."""
    if not token:
        return None
    pending_file = _progress_pending_file(orch)
    pending = _load_progress_pending(pending_file)
    now = time.time()
    # prune expired
    removed = [
        tk for tk, e in list(pending.items())
        if not isinstance(e, dict) or (float(e.get("expires_at", 0) or 0) and now > float(e.get("expires_at", 0) or 0))
    ]
    if removed:
        for tk in removed:
            pending.pop(tk, None)
        _save_progress_pending(pending_file, pending)

    e = pending.get(token)
    if not isinstance(e, dict):
        return None
    if str(e.get("kind")) != "laf_progress_submit":
        return None
    if str(e.get("status")) != "pending":
        return None
    return e


def handle_laf_progress_submit_confirmation_if_any(orch, *, platform: str, user_id: str, text: str):
    """
    Check if text contains a valid progress confirm_token.
    Returns dict with 'handled', 'message' keys, or None if not a progress token.
    """
    msg = (text or "").strip()
    if not msg:
        return None
    m = re.search(r"\b([A-F0-9]{6,12})\b", msg.upper())
    if not m:
        return None
    token = m.group(1)
    entry = resolve_laf_progress_pending_token(orch, token)
    if not isinstance(entry, dict):
        return None

    platform_norm = str(platform or "").strip().lower()
    if str(entry.get("platform", "")).strip().lower() != platform_norm:
        return {"handled": True, "message": f"\u26a0\ufe0f \u78ba\u8a8d\u78bc {token} \u5c6c\u65bc\u5176\u4ed6\u901a\u8a0a\u5e73\u53f0\uff0c\u8acb\u5728\u539f\u5e73\u53f0\u56de\u8986\u3002"}

    low = msg.lower()
    has_cancel = any(k in low for k in ["\u53d6\u6d88", "\u4e0d\u8981\u9001\u51fa", "\u5148\u4e0d\u8981", "\u66ab\u505c"])
    if has_cancel:
        pending_file = _progress_pending_file(orch)
        pending = _load_progress_pending(pending_file)
        ent = pending.get(token)
        if isinstance(ent, dict):
            ent["status"] = "cancelled"
            ent["cancelled_by"] = str(user_id or "")
            ent["cancelled_at"] = time.time()
            pending[token] = ent
            _save_progress_pending(pending_file, pending)
        return {"handled": True, "message": f"\U0001f6d1 \u5df2\u53d6\u6d88\u9032\u5ea6\u56de\u5831\u9001\u51fa\uff08\u78ba\u8a8d\u78bc {token}\uff09\u3002"}

    # Confirm
    pending_file = _progress_pending_file(orch)
    pending = _load_progress_pending(pending_file)
    ent = pending.get(token)
    if not isinstance(ent, dict):
        return {"handled": True, "message": "\u26a0\ufe0f \u9019\u7b46\u9032\u5ea6\u56de\u5831\u78ba\u8a8d\u5df2\u4e0d\u5b58\u5728\u6216\u5df2\u904e\u671f\u3002"}
    if str(ent.get("status")) != "pending":
        return {"handled": True, "message": f"\u26a0\ufe0f \u9019\u7b46\u9032\u5ea6\u56de\u5831\u78ba\u8a8d\u76ee\u524d\u72c0\u614b\u70ba\u300c{ent.get('status')}\u300d\uff0c\u4e0d\u80fd\u91cd\u8907\u9001\u51fa\u3002"}

    ent["status"] = "submitting"
    ent["confirmed_by"] = str(user_id or "")
    ent["confirmed_at"] = time.time()
    pending[token] = ent
    _save_progress_pending(pending_file, pending)

    skill_python = (os.environ.get("MAGI_SKILL_PYTHON") or "").strip()
    if not skill_python:
        skill_python = os.path.join(_MAGI_ROOT, "venv", "bin", "python")
    if not os.path.exists(skill_python):
        skill_python = sys.executable or "python3"
    laf_skill = os.path.join(_MAGI_ROOT, "skills", "laf-orchestrator", "action.py")
    payload = ent.get("payload") if isinstance(ent.get("payload"), dict) else {}
    laf_case_no = str(payload.get("laf_case_no") or "").strip()
    client_name = str(payload.get("client_name") or "").strip()
    # P0-2: pass remark and PDF paths so the submit subprocess can re-fill the form
    _remark_str = str(payload.get("remark") or "").strip()
    _court_pdf_str = str(payload.get("court_pdf") or "").strip()
    _doc_pdf_str = str(payload.get("doc_pdf") or "").strip()

    def _run_progress_submit(uid, plat, tok):
        cmd = [
            skill_python, laf_skill,
            "--task", "progress_report",
            "--case_no", laf_case_no,
            "--client_name", client_name,
            "--mode", "submit",
            "--no-notify",
        ]
        if _remark_str:
            cmd += ["--reason", _remark_str]
        _upload_files = [p for p in [_court_pdf_str, _doc_pdf_str] if p and os.path.exists(p)]
        if _upload_files:
            cmd += ["--fields-json", json.dumps({"upload_files": _upload_files})]
        env = os.environ.copy()
        env["MAGI_LAF_ALLOW_PROGRESS_SUBMIT"] = "1"
        timeout_sec = int(os.environ.get("MAGI_LAF_REPORT_TIMEOUT_SEC", "2400") or "2400")
        success = False
        text_out = ""
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec, env=env)
            stdout_text = (proc.stdout or "").strip()
            data = None
            for line in reversed(stdout_text.splitlines()):
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        break
                except Exception:
                    pass
            if proc.returncode == 0 and isinstance(data, dict) and data.get("success"):
                text_out = f"\u2705 \u9032\u5ea6\u56de\u5831\u5df2\u9001\u51fa\uff08\u78ba\u8a8d\u78bc {tok}\uff09"
                success = True
            else:
                err = str((data or {}).get("error") or stdout_text[:300]) if data else stdout_text[:300]
                text_out = f"\u274c \u9032\u5ea6\u56de\u5831\u9001\u51fa\u5931\u6557\uff08\u78ba\u8a8d\u78bc {tok}\uff09\uff1a{err}"
        except subprocess.TimeoutExpired:
            text_out = f"\u23f3 \u9032\u5ea6\u56de\u5831\u9001\u51fa\u903e\u6642\uff08\u78ba\u8a8d\u78bc {tok}\uff09\u3002"
        except Exception as exc:
            text_out = f"\u274c \u9032\u5ea6\u56de\u5831\u9001\u51fa\u7570\u5e38\uff08\u78ba\u8a8d\u78bc {tok}\uff09\uff1a{exc}"
        try:
            pending2 = _load_progress_pending(pending_file)
            e2 = pending2.get(tok)
            if isinstance(e2, dict):
                e2["status"] = "submitted" if success else "failed"
                e2["finished_at"] = time.time()
                pending2[tok] = e2
                _save_progress_pending(pending_file, pending2)
        except Exception:
            pass
        try:
            if getattr(orch, "notification_callback", None):
                orch.notification_callback(uid, text_out, plat, topic_key="laf_progress")
        except Exception as ne:
            logger.warning("progress submit notify failed: %s", ne)

    threading.Thread(
        target=_run_progress_submit,
        args=(str(user_id or ""), str(platform or ""), token),
        daemon=True,
    ).start()
    return {"handled": True, "message": f"\u23f3 \u5df2\u6536\u5230\u78ba\u8a8d\uff0c\u958b\u59cb\u9001\u51fa\u9032\u5ea6\u56de\u5831\uff08\u78ba\u8a8d\u78bc {token}\uff09\u3002\u5b8c\u6210\u5f8c\u6211\u6703\u4e3b\u52d5\u56de\u5831\u3002"}
