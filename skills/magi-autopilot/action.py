#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
magi-autopilot/action.py

流程型自動巡檢：
- 預設靜默：只有卡住或需要人工才 LINE 通知
- 所有動作落地 report.json/report.txt 供追溯
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import hmac
import hashlib
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import socket

logger = logging.getLogger("magi-autopilot")
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib import request as urlreq
from urllib import error as urlerr

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.case_path_mapper import default_scan_roots
from api.autopilot_artifacts import (
    cleanup_stale_kill_reason_files,
    get_kill_reason_path,
    read_kill_reason,
    write_kill_reason,
)

from api.runtime_paths import (
    ensure_orch_on_sys_path,
    get_autopilot_runs_dir,
    get_config_path,
    get_magi_root_dir,
    get_orch_dir,
    get_skill_python,
)

# --- Load .env for subprocess/cron credential access ---
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except Exception:
    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 56, exc_info=True)

MAGI_ROOT_DIR = os.environ.get("MAGI_ROOT_DIR", str(get_magi_root_dir()))
CODE_DIR = str(get_orch_dir())
SKILLS_DIR = os.path.join(MAGI_ROOT_DIR, "skills")
RUNS_DIR = str(get_autopilot_runs_dir())
VENV_PY = str(get_skill_python())
STATE_PATH = os.environ.get("MAGI_AUTOPILOT_STATE_PATH", os.path.join(MAGI_ROOT_DIR, "_autopilot_state.json"))
GCAL_OAUTH_DEFER_PATH = os.path.join(RUNS_DIR, "_pending_gcal_oauth.jsonl")
TRANSCRIPT_CAPTCHA_DEFER_PATH = os.path.join(RUNS_DIR, "_pending_transcript_captcha.jsonl")
MAGI_ENV_PATH = os.environ.get("MAGI_ENV_PATH", os.path.join(MAGI_ROOT_DIR, ".env"))
MAGI_RUNTIME_OVERRIDES_PATH = os.environ.get(
    "MAGI_RUNTIME_OVERRIDES_PATH",
    os.path.join(MAGI_ROOT_DIR, ".agent", "runtime_overrides.json"),
)
OPENCLAW_AUTH_GUARD_SCRIPT = os.path.join(
    MAGI_ROOT_DIR,
    "scripts",
    "ops",
    "check_openclaw_auth_mode.py",
)
OPENCLAW_RUNTIME_MODE_SCRIPT = os.path.join(
    MAGI_ROOT_DIR,
    "scripts",
    "ops",
    "check_openclaw_runtime_mode.py",
)
JUDICIAL_API_PIPELINE_SCRIPT = os.path.join(
    MAGI_ROOT_DIR,
    "scripts",
    "ops",
    "check_judicial_api_pipeline.py",
)
LOCAL_OPENCLAW_PROVIDERS = {"omlx", "ollama"}
SLO_GUARD_SCRIPT_CANDIDATES = [
    "/Users/ai/.openclaw/skills/magi-debug-ops/scripts/slo_guard.py",
    os.path.join(MAGI_ROOT_DIR, "skills", "magi-debug-ops", "scripts", "slo_guard.py"),
]


def _load_runtime_env() -> None:
    """
    Best-effort load MAGI .env so scheduled invocations have channel/db credentials.
    Only fills missing variables.
    """
    p = Path(MAGI_ENV_PATH)
    if not p.exists():
        return
    try:
        for raw in p.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            k, v = s.split("=", 1)
            k = k.strip()
            if k and k not in os.environ:
                os.environ[k] = v.strip()
    except Exception:
        return

    # Normalize LINE var names across modules.
    if not os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("MAGI_LINE_CHANNEL_ACCESS_TOKEN"):
        os.environ["LINE_CHANNEL_ACCESS_TOKEN"] = os.environ["MAGI_LINE_CHANNEL_ACCESS_TOKEN"]
    if not os.environ.get("LINE_CHANNEL_SECRET") and os.environ.get("MAGI_LINE_CHANNEL_SECRET"):
        os.environ["LINE_CHANNEL_SECRET"] = os.environ["MAGI_LINE_CHANNEL_SECRET"]
    if not os.environ.get("OPENCLAW_LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_CHANNEL_ACCESS_TOKEN"):
        os.environ["OPENCLAW_LINE_CHANNEL_ACCESS_TOKEN"] = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
    if not os.environ.get("OPENCLAW_LINE_CHANNEL_SECRET") and os.environ.get("LINE_CHANNEL_SECRET"):
        os.environ["OPENCLAW_LINE_CHANNEL_SECRET"] = os.environ["LINE_CHANNEL_SECRET"]

    # Optional runtime overrides (degrade profile / emergency knobs).
    # Format:
    # {
    #   "enabled": true,
    #   "force": false,
    #   "env": {"MAGI_NIGHTLY_TRANSCRIPT_BUDGET_SEC":"900", ...},
    #   "meta": {...}
    # }
    try:
        rp = Path(MAGI_RUNTIME_OVERRIDES_PATH)
        if rp.exists():
            data = json.loads(rp.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict) and bool(data.get("enabled", True)):
                force = bool(data.get("force", False))
                env_map = data.get("env") or {}
                if isinstance(env_map, dict):
                    for k, v in env_map.items():
                        ks = str(k or "").strip()
                        if not ks:
                            continue
                        vs = str(v if v is not None else "").strip()
                        if force or ks not in os.environ:
                            os.environ[ks] = vs
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 150, exc_info=True)

def _resolve_remote_db_endpoint() -> Tuple[str, int]:
    """
    Resolve remote DB endpoint with this order:
    1) MAGI_REMOTE_DB_HOST/PORT
    2) code/json/config.json profile Studio_VPN_Remote
    3) fallback 100.121.61.74:3306
    """
    host = (os.environ.get("MAGI_REMOTE_DB_HOST", "") or "").strip()
    port_raw = (os.environ.get("MAGI_REMOTE_DB_PORT", "") or "").strip()

    if not host:
        for cp in (
            os.path.join(CODE_DIR, "json", "config.json"),
            os.path.join(CODE_DIR, "config.json"),
        ):
            try:
                if not os.path.exists(cp):
                    continue
                cfg = json.loads(Path(cp).read_text(encoding="utf-8")) or {}
                for p in (cfg.get("mariadb_profiles") or []):
                    if not isinstance(p, dict):
                        continue
                    if str(p.get("profile_name") or "").strip() != "Studio_VPN_Remote":
                        continue
                    c = p.get("config") if isinstance(p.get("config"), dict) else {}
                    host = str(c.get("host") or "").strip()
                    if not port_raw:
                        port_raw = str(c.get("port") or "").strip()
                    break
                if host:
                    break
            except Exception:
                continue

    if not host:
        try:
            from api.routing.node_registry import get_node_ip as _get_node_ip
            host = _get_node_ip("nas") or "100.121.61.74"
        except Exception:
            host = "100.121.61.74"
    try:
        port = int(port_raw or "3306")
    except Exception:
        port = 3306
    return host, port


def _set_db_preference_by_reachability(timeout_sec: float = 1.6) -> Dict[str, Any]:
    """
    Enforce DB preference for this run:
    - remote reachable -> MAGI_PREFER_LOCAL_DB=0
    - remote unreachable -> MAGI_PREFER_LOCAL_DB=1
    """
    host, port = _resolve_remote_db_endpoint()
    reachable = False
    err = ""
    try:
        with socket.create_connection((host, int(port)), timeout=max(0.6, float(timeout_sec))):
            reachable = True
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        reachable = False

    os.environ["MAGI_PREFER_LOCAL_DB"] = "0" if reachable else "1"
    return {
        "ok": True,
        "remote_host": host,
        "remote_port": int(port),
        "remote_reachable": bool(reachable),
        "prefer_local_db": os.environ.get("MAGI_PREFER_LOCAL_DB", "0"),
        "message": "remote-first" if reachable else "fallback-local",
        "error": err,
    }


def _load_mariadb_profiles() -> list:
    """
    Load DB profiles from code config (best-effort).
    """
    profiles = []
    for cp in (
        os.path.join(CODE_DIR, "json", "config.json"),
        os.path.join(CODE_DIR, "config.json"),
    ):
        try:
            if not os.path.exists(cp):
                continue
            cfg = json.loads(Path(cp).read_text(encoding="utf-8")) or {}
            p = cfg.get("mariadb_profiles") or []
            if isinstance(p, list):
                profiles.extend([x for x in p if isinstance(x, dict)])
            if profiles:
                break
        except Exception:
            continue
    return profiles


def _db_schema_chk_nb_guard() -> Dict[str, Any]:
    """
    Detect regressions: any CHECK constraint named chk_nb_* in law_firm_data.
    This catches accidental re-hardening that breaks OSC/GUI empty-string workflows.
    """
    out: Dict[str, Any] = {
        "ok": True,
        "has_chk_nb": False,
        "targets": [],
        "message": "",
    }
    try:
        import mysql.connector  # type: ignore
    except Exception as e:
        out["ok"] = False
        out["message"] = f"mysql_connector_unavailable: {type(e).__name__}: {e}"
        return out

    wanted = []
    wanted_names = set(
        x.strip()
        for x in str(os.environ.get("MAGI_SCHEMA_GUARD_PROFILES", "Studio_VPN_Remote,Home_Local_Test")).split(",")
        if x.strip()
    )
    for p in _load_mariadb_profiles():
        name = str(p.get("profile_name") or "").strip()
        if name and name in wanted_names:
            c = p.get("config") if isinstance(p.get("config"), dict) else {}
            wanted.append((name, c))

    # fallback: still probe remote endpoint if profile missing
    if not wanted:
        host, port = _resolve_remote_db_endpoint()
        wanted.append(
            (
                "Studio_VPN_Remote",
                {
                    "host": host,
                    "port": int(port),
                    "user": str(os.environ.get("MAGI_DB_USER", "python_user") or "python_user"),
                    "password": str(os.environ.get("MAGI_DB_PASSWORD", "") or ""),
                    "database": "law_firm_data",
                },
            )
        )

    any_success = False
    for name, c in wanted:
        item: Dict[str, Any] = {
            "profile": name,
            "host": str(c.get("host") or ""),
            "port": int(c.get("port") or 3306),
            "database": str(c.get("database") or "law_firm_data"),
            "reachable": False,
            "chk_nb_count": None,
            "total_check_count": None,
            "error": "",
        }
        try:
            conn = mysql.connector.connect(
                host=item["host"],
                port=item["port"],
                user=str(c.get("user") or os.environ.get("OSC_DB_USER", "python_user")),
                password=str(c.get("password") or os.environ.get("OSC_DB_PASSWORD", "")),
                database="information_schema",
                connection_timeout=4,
            )
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*)
                FROM TABLE_CONSTRAINTS
                WHERE CONSTRAINT_SCHEMA=%s
                  AND CONSTRAINT_TYPE='CHECK'
                  AND CONSTRAINT_NAME REGEXP '^chk_nb_'
                """,
                (item["database"],),
            )
            item["chk_nb_count"] = int(cur.fetchone()[0] or 0)
            cur.execute(
                """
                SELECT COUNT(*)
                FROM TABLE_CONSTRAINTS
                WHERE CONSTRAINT_SCHEMA=%s
                  AND CONSTRAINT_TYPE='CHECK'
                """,
                (item["database"],),
            )
            item["total_check_count"] = int(cur.fetchone()[0] or 0)
            cur.close()
            conn.close()
            item["reachable"] = True
            any_success = True
        except Exception as e:
            item["error"] = f"{type(e).__name__}: {e}"
        out["targets"].append(item)

    if not any_success:
        out["ok"] = False
        out["message"] = "schema_guard_no_db_reachable"
        return out

    total_chk = 0
    for t in out["targets"]:
        try:
            total_chk += int(t.get("chk_nb_count") or 0)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 353, exc_info=True)
    out["has_chk_nb"] = total_chk > 0
    out["ok"] = not out["has_chk_nb"]
    if out["has_chk_nb"]:
        out["message"] = f"chk_nb_constraints_detected={total_chk}"
    else:
        out["message"] = "schema_ok_no_chk_nb"
    return out

def _remember_run_event(report: Dict[str, Any]) -> None:
    """
    寫入向量記憶：讓使用者可用對話回溯「夜間/巡檢做了什麼、結果如何」。
    Best-effort：失敗不影響主流程。
    """
    try:
        ensure_orch_on_sys_path()
        import magi_eventlog  # type: ignore
    except Exception:
        return

    try:
        details = report.get("details") or {}
        steps = (details.get("steps") or {}) if isinstance(details, dict) else {}
        step_status: Dict[str, Any] = {}
        for k, v in (steps or {}).items():
            if not isinstance(v, dict):
                continue
            # Keep it compact for embeddings.
            st: Dict[str, Any] = {"ok": bool(v.get("ok", True))}
            if "returncode" in v:
                st["returncode"] = v.get("returncode")
            parsed = v.get("parsed")
            if isinstance(parsed, dict):
                for hint in ("count", "processed", "inserted", "queued", "skipped", "message"):
                    if hint in parsed:
                        st[hint] = parsed.get(hint)
                if "errors" in parsed and isinstance(parsed.get("errors"), list):
                    st["errors"] = (parsed.get("errors") or [])[:5]
            step_status[str(k)] = st

        payload = {
            "task": report.get("task"),
            "ts": report.get("ts"),
            "run_dir": report.get("run_dir"),
            "ok": bool(report.get("ok")),
            "summary": report.get("summary"),
            "blocked": bool((details or {}).get("blocked")) if isinstance(details, dict) else False,
            "blockers": (details.get("blockers") or [])[:12] if isinstance(details, dict) else [],
            "steps": step_status,
        }
        magi_eventlog.remember_event(
            f"autopilot:{str(report.get('task') or '').strip() or 'run'}",
            ok=bool(report.get("ok")),
            source="magi_autopilot",
            payload=payload,
            tags={"task": str(report.get("task") or ""), "ok": "1" if report.get("ok") else "0"},
        )
    except Exception:
        return


def _remember_step_events(task: str, run_dir: str, steps: Dict[str, Any]) -> None:
    """
    將每一步驟結果寫入向量記憶，方便事後精準查詢「哪一步失敗、錯在哪」。
    Best-effort：失敗不影響主流程。
    """
    try:
        ensure_orch_on_sys_path()
        import magi_eventlog  # type: ignore
    except Exception:
        return

    if not isinstance(steps, dict):
        return
    for step_name, step_data in steps.items():
        if not isinstance(step_data, dict):
            continue
        parsed = step_data.get("parsed")
        payload: Dict[str, Any] = {
            "task": str(task or ""),
            "step": str(step_name or ""),
            "run_dir": str(run_dir or ""),
            "ok": bool(step_data.get("ok", True)),
            "returncode": step_data.get("returncode"),
        }
        if isinstance(parsed, dict):
            payload["parsed"] = {
                k: parsed.get(k)
                for k in (
                    "ok",
                    "success",
                    "count",
                    "processed",
                    "inserted",
                    "skipped",
                    "queued",
                    "downloaded_count",
                    "message",
                    "error",
                    "target_gb",
                    "tolerance_gb",
                    "initial_ngl",
                    "seed_hint_ngl",
                    "recommended_ngl",
                    "best_delta_gb",
                    "note",
                )
                if k in parsed
            }
        try:
            magi_eventlog.remember_event(
                f"autopilot:{str(task or '').strip() or 'run'}:{str(step_name)}",
                ok=bool(step_data.get("ok", True)),
                source="magi_autopilot_step",
                payload=payload,
                tags={"task": str(task or ""), "step": str(step_name), "ok": "1" if step_data.get("ok", True) else "0"},
            )
        except Exception:
            continue


def _remember_ngl_calibration_event(run_dir: str, parsed: Dict[str, Any], trigger_reason: str) -> None:
    """
    將 NGL 校準結果寫入 CASPER 記憶（向量資料庫），供後續對話與自動化沿用。
    """
    try:
        ensure_orch_on_sys_path()
        import magi_eventlog  # type: ignore
    except Exception:
        return

    if not isinstance(parsed, dict):
        return
    payload = {
        "run_dir": str(run_dir or ""),
        "trigger_reason": str(trigger_reason or ""),
        "success": bool(parsed.get("success")),
        "target_gb": parsed.get("target_gb"),
        "tolerance_gb": parsed.get("tolerance_gb"),
        "initial_ngl": parsed.get("initial_ngl"),
        "seed_hint_ngl": parsed.get("seed_hint_ngl"),
        "recommended_ngl": parsed.get("recommended_ngl"),
        "best_delta_gb": parsed.get("best_delta_gb"),
        "note": parsed.get("note"),
        "hint_path": parsed.get("hint_path"),
    }
    try:
        magi_eventlog.remember_event(
            "autopilot:nightly_ngl_calibration",
            ok=bool(parsed.get("success")),
            source="magi_autopilot_ngl",
            payload=payload,
            tags={
                "task": "nightly",
                "step": "nightly_ngl_calibrate",
                "ok": "1" if parsed.get("success") else "0",
                "trigger": str(trigger_reason or "unknown"),
            },
        )
    except Exception:
        return


def _maybe_reexec_venv() -> None:
    if os.environ.get("MAGI_AUTOPILOT_NO_VENV", "").strip() == "1":
        return
    if os.path.exists(VENV_PY) and os.path.realpath(sys.executable) != os.path.realpath(VENV_PY):
        os.execv(VENV_PY, [VENV_PY, __file__, *sys.argv[1:]])


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _ensure_dirs() -> None:
    os.makedirs(RUNS_DIR, exist_ok=True)
    try:
        os.makedirs(os.path.dirname(GCAL_OAUTH_DEFER_PATH) or RUNS_DIR, exist_ok=True)
        os.makedirs(os.path.dirname(TRANSCRIPT_CAPTCHA_DEFER_PATH) or RUNS_DIR, exist_ok=True)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 533, exc_info=True)


def _load_json(path: Any) -> dict:
    try:
        with open(os.fspath(path), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_json(path: Any, data: dict) -> None:
    try:
        path_str = os.fspath(path)
        os.makedirs(os.path.dirname(path_str) or ".", exist_ok=True)
        tmp = path_str + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path_str)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 552, exc_info=True)


def _append_jsonl(path: str, obj: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, default=str) + "\n")
    except Exception:
        return


def _queue_gcal_oauth_defer(run_dir: str, *, limit: int, parsed: Optional[dict]) -> str:
    """
    OAuth 未就緒時，將 gcal_sync 延後記錄到佇列，避免 nightly 報錯中斷判讀。
    """
    item = {
        "ts": datetime.now().isoformat(),
        "run_dir": run_dir,
        "reason": "need_interactive_oauth",
        "limit": int(limit),
        "parsed": parsed if isinstance(parsed, dict) else {},
    }
    _append_jsonl(GCAL_OAUTH_DEFER_PATH, item)
    return GCAL_OAUTH_DEFER_PATH


def _looks_like_captcha_error(text: str) -> bool:
    s = str(text or "").lower()
    if not s:
        return False
    return ("captcha" in s) or ("驗證碼" in str(text or ""))


def _queue_transcript_captcha_defer(
    run_dir: str,
    *,
    step: str,
    eligible_cases: int,
    parsed: Optional[dict],
    error: str,
    retry_attempted: bool,
    retry_ok: bool,
) -> str:
    """
    Record transcript captcha deferrals so ops can retry manually / in next tick.
    """
    item = {
        "ts": datetime.now().isoformat(),
        "run_dir": run_dir,
        "step": str(step or "transcript_sync"),
        "reason": "captcha",
        "eligible_cases": int(max(0, int(eligible_cases or 0))),
        "retry_attempted": bool(retry_attempted),
        "retry_ok": bool(retry_ok),
        "error": str(error or "")[:500],
        "parsed": parsed if isinstance(parsed, dict) else {},
    }
    _append_jsonl(TRANSCRIPT_CAPTCHA_DEFER_PATH, item)
    return TRANSCRIPT_CAPTCHA_DEFER_PATH


def _transcript_captcha_cooldown_state() -> Dict[str, Any]:
    """
    If recent captcha deferrals happened, skip transcript sync temporarily.
    This avoids repeatedly hitting the same captcha wall every tick/nightly cycle.
    """
    enabled = os.environ.get("MAGI_TRANSCRIPT_CAPTCHA_COOLDOWN_ENABLE", "1").strip().lower() in {"1", "true", "yes", "on"}
    try:
        cooldown_min = int(os.environ.get("MAGI_TRANSCRIPT_CAPTCHA_COOLDOWN_MINUTES", "180") or "180")
    except Exception:
        cooldown_min = 180
    cooldown_min = max(5, cooldown_min)
    out: Dict[str, Any] = {
        "enabled": enabled,
        "active": False,
        "cooldown_minutes": cooldown_min,
        "remaining_minutes": 0,
        "recent_count": 0,
        "last_ts": "",
        "path": TRANSCRIPT_CAPTCHA_DEFER_PATH,
    }
    if (not enabled) or (not os.path.exists(TRANSCRIPT_CAPTCHA_DEFER_PATH)):
        return out

    now = datetime.now()
    cutoff = now.timestamp() - (cooldown_min * 60)
    last_dt: Optional[datetime] = None
    count = 0
    try:
        with open(TRANSCRIPT_CAPTCHA_DEFER_PATH, "r", encoding="utf-8") as f:
            for ln in f:
                s = (ln or "").strip()
                if not s:
                    continue
                try:
                    obj = json.loads(s)
                except Exception:
                    continue
                ts_raw = str(obj.get("ts") or "").strip()
                if not ts_raw:
                    continue
                try:
                    dt = datetime.fromisoformat(ts_raw)
                except Exception:
                    continue
                if dt.timestamp() >= cutoff:
                    count += 1
                    if (last_dt is None) or (dt > last_dt):
                        last_dt = dt
    except Exception:
        return out

    out["recent_count"] = int(count)
    if last_dt is None:
        return out
    out["last_ts"] = last_dt.isoformat()
    delta_sec = int((now - last_dt).total_seconds())
    remaining = max(0, cooldown_min - int(delta_sec // 60))
    out["remaining_minutes"] = remaining
    out["active"] = remaining > 0
    return out


def _blocker_sig(blockers: list) -> str:
    """
    產生「穩定」的 blockers 簽章，避免因為 stderr/細節字串變動導致每 2 小時都被視為不同事件而洗版。
    規則：
    - 盡量只保留「步驟: 類型」(例如 file_review_preview: reauth_required)
    - 對 OSC 佇列類訊息，去除數字差異（只保留 pending 狀態）
    - 對 OAuth/權限/驗證碼類，抽出固定 kind
    """
    items = []
    for raw in (blockers or []):
        s = str(raw or "").strip()
        if not s:
            continue
        sl = s.lower()

        # OSC queue: remove variable counts
        if "osc" in sl and ("佇列" in s or "pending" in sl):
            items.append("osc_queue: pending")
            continue

        # Keep "step: kind" format if present and short enough
        if ":" in s:
            left, _, right = s.partition(":")
            left = left.strip()
            right = right.strip()
            # If right is a known kind already, keep it.
            rk = right.lower()
            known = [
                "reauth_required",
                "invalid_grant",
                "insufficientpermissions",
                "captcha",
                "login_failed",
                "http_401",
                "http_403",
                "needs_human",
            ]
            if any(k == rk for k in known):
                items.append(f"{left}: {rk}")
                continue

        # Fallback: derive kind from content
        if "need_interactive_oauth" in sl or "reauth_gmail" in sl or "reauth_required" in sl:
            items.append("oauth: reauth_required")
        elif "invalid_grant" in sl:
            items.append("oauth: invalid_grant")
        elif "insufficientpermissions" in sl or "insufficient authentication scopes" in sl:
            items.append("oauth: insufficientPermissions")
        elif "captcha" in sl or "驗證碼" in s:
            items.append("auth: captcha")
        elif "login_failed" in sl or "登入失敗" in s:
            items.append("auth: login_failed")
        elif "403" in sl or "forbidden" in sl:
            items.append("http: 403")
        elif "401" in sl or "unauthorized" in sl:
            items.append("http: 401")
        else:
            # Last resort: bucket to a generic type, but avoid embedding raw stderr.
            items.append("other: blocked")

    norm = "\n".join(sorted(set(items)))
    if not norm:
        norm = "none"
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _should_notify(blockers: list, *, cooldown_sec: int = 6 * 3600) -> bool:
    """
    避免排程重複洗版：同一組 blockers 在 cooldown 期間內只通知一次。
    """
    sig = _blocker_sig(blockers)
    if not sig:
        return False
    st = _load_json(STATE_PATH) or {}
    last = (st.get("last_notified") or {}).get(sig) or {}
    last_ts = last.get("ts") or ""
    if last_ts:
        try:
            dt = datetime.fromisoformat(last_ts)
            if (datetime.now() - dt).total_seconds() < cooldown_sec:
                return False
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 758, exc_info=True)
    return True


def _cooldown_for_blockers(blockers: list) -> int:
    """
    某些 blocker（OAuth/權限）通常需要人工一次性處理；
    這類型不要每 2 小時就提醒一次，避免 LINE 洗版。
    """
    s = " ".join(str(b) for b in (blockers or [])).lower()
    if any(k in s for k in ["reauth_required", "invalid_grant", "insufficientpermissions", "insufficient authentication scopes"]):
        return 24 * 3600
    return 6 * 3600


def _mark_notified(blockers: list, *, task: str, report_json: str = "") -> None:
    sig = _blocker_sig(blockers)
    if not sig:
        return
    st = _load_json(STATE_PATH) or {}
    st.setdefault("last_notified", {})
    st["last_notified"][sig] = {
        "ts": datetime.now().isoformat(),
        "task": task,
        "report_json": report_json,
        "blockers": list(blockers or [])[:20],
    }
    _save_json(STATE_PATH, st)


def _should_notify_success(task: str) -> bool:
    """
    Nightly success notification (once per day).
    """
    if (task or "").lower() != "nightly":
        return False
    st = _load_json(STATE_PATH) or {}
    today = datetime.now().strftime("%Y-%m-%d")
    if st.get("last_success_notified_date") == today:
        return False
    return True


def _mark_success_notified(task: str, report_json: str = "") -> None:
    st = _load_json(STATE_PATH) or {}
    st["last_success_notified_date"] = datetime.now().strftime("%Y-%m-%d")
    st["last_success_notified_task"] = task
    st["last_success_notified_report"] = report_json
    _save_json(STATE_PATH, st)


def _last_big_brain_success_age_sec() -> Optional[int]:
    st = _load_json(STATE_PATH) or {}
    bb = st.get("big_brain") if isinstance(st.get("big_brain"), dict) else {}
    ts = str((bb or {}).get("last_distributed_success_ts") or "").strip()
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        return max(0, int((datetime.now() - dt).total_seconds()))
    except Exception:
        return None


def _mark_big_brain_success(payload: Dict[str, Any]) -> None:
    st = _load_json(STATE_PATH) or {}
    bb = st.get("big_brain") if isinstance(st.get("big_brain"), dict) else {}
    probe = payload.get("inference_probe_retry") if isinstance(payload.get("inference_probe_retry"), dict) else payload.get("inference_probe")
    probe = probe if isinstance(probe, dict) else {}
    bb["last_distributed_success_ts"] = datetime.now().isoformat()
    bb["last_status"] = str(payload.get("status") or "")
    bb["last_mode_after"] = str(payload.get("mode_after") or "")
    bb["last_route"] = str(probe.get("route") or "")
    bb["last_model"] = str(probe.get("model") or "")
    try:
        bb["last_latency_ms"] = int(probe.get("latency_ms") or 0)
    except Exception:
        bb["last_latency_ms"] = 0
    st["big_brain"] = bb
    _save_json(STATE_PATH, st)


def _extract_last_json(stdout: str) -> Optional[dict]:
    s = (stdout or "").strip()
    if not s:
        return None
    # 1) common case: stdout is pure JSON
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 850, exc_info=True)

    # 2) common mixed-output case: one JSON object on the last line
    for line in reversed(s.splitlines()):
        t = line.strip()
        if not t or not t.startswith("{"):
            continue
        try:
            obj = json.loads(t)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue

    # 3) robust fallback: scan all '{' positions and JSON-decode from there.
    # Only accept candidates that consume the rest of string (ignoring whitespace),
    # so nested dicts inside arrays won't be mistaken for top-level output.
    dec = json.JSONDecoder()
    last: Optional[dict] = None
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        seg = s[i:]
        try:
            obj, _end = dec.raw_decode(seg)
            tail = seg[_end:].strip()
            if isinstance(obj, dict) and not tail:
                last = obj
        except Exception:
            continue
    if last is not None:
        return last

    # 4) very last resort: keep previous behavior for odd mixed stdout.
    # (May capture nested dict in malformed logs, but better than None.)
    for i, ch in enumerate(s):
        if ch != "{":
            continue
        seg = s[i:]
        try:
            obj, _end = dec.raw_decode(seg)
            if isinstance(obj, dict):
                last = obj
        except Exception:
            continue
    return last


@dataclass
class CmdResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    parsed: Optional[dict] = None


def _run_cmd(cmd: list, timeout_sec: int = 900, env: Optional[dict] = None) -> CmdResult:
    p = None
    try:
        p = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=(env or None),
            start_new_session=True,
        )
        out, err = p.communicate(timeout=timeout_sec)
        parsed = _extract_last_json(out)
        combined = (out or "") + "\n" + (err or "")
        combined_l = combined.lower()

        # Some legacy skills log errors but still exit 0; treat common blockers as failure.
        error_markers = [
            "invalid_grant",
            "insufficientpermissions",
            "request had insufficient authentication scopes",
            "httperror 403",
            "captcha",
        ]

        # If the command returns structured JSON indicating success, trust it.
        # Many skills print "❌ 連線失敗" during fallback probing but still succeed.
        if p.returncode == 0 and isinstance(parsed, dict):
            ok = (parsed.get("ok", True) is not False) and (parsed.get("success", True) is not False)
        else:
            # Only check error markers when there's no structured success JSON.
            has_marker = any(m in combined_l for m in error_markers)
            ok = (
                (p.returncode == 0)
                and (not has_marker)
                and (parsed is None or parsed.get("ok", True) is not False)
                and (parsed is None or parsed.get("success", True) is not False)
            )
        return CmdResult(ok=ok, returncode=p.returncode, stdout=out, stderr=err, parsed=parsed)
    except subprocess.TimeoutExpired as e:
        # Ensure timeout does not leave orphan child processes that keep locks.
        try:
            if p and p.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(p.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                    )
                else:
                    os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 960, exc_info=True)
        out = ""
        err = ""
        try:
            if p:
                out, err = p.communicate(timeout=2)
        except Exception:
            out = out or ""
            err = err or ""
        if not out and getattr(e, "stdout", None):
            out = e.stdout or ""
        if not err:
            err = f"timeout after {timeout_sec}s"
        return CmdResult(ok=False, returncode=124, stdout=out, stderr=err)
    except Exception as e:
        return CmdResult(ok=False, returncode=1, stdout="", stderr=f"{type(e).__name__}: {e}")


def _cmd_timed_out(res: Optional[CmdResult]) -> bool:
    if not isinstance(res, CmdResult):
        return False
    if int(res.returncode or 0) == 124:
        return True
    msg = (str(res.stderr or "") + "\n" + str(res.stdout or "")).lower()
    timeout_hints = (
        "timeout after ",
        "timed out",
        "read timed out",
    )
    return any(h in msg for h in timeout_hints)


def _skill_action(skill: str) -> str:
    return os.path.join(SKILLS_DIR, skill, "action.py")


def _stash_cmd_output(run_dir: str, step: str, cmd: list, res: CmdResult) -> None:
    try:
        base = re.sub(r"[^a-zA-Z0-9._-]+", "_", step).strip("_") or "step"
        with open(os.path.join(run_dir, f"{base}.cmd.txt"), "w", encoding="utf-8") as f:
            f.write(" ".join(cmd) + "\n")
        with open(os.path.join(run_dir, f"{base}.stdout.txt"), "w", encoding="utf-8") as f:
            f.write(res.stdout or "")
        with open(os.path.join(run_dir, f"{base}.stderr.txt"), "w", encoding="utf-8") as f:
            f.write(res.stderr or "")
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1006, exc_info=True)


def _notify_line(text: str, *, topic_key: str = "check") -> bool:
    try:
        ensure_orch_on_sys_path()
        from line_notifier import LAFNotifier
        return bool(LAFNotifier().notify_admin(text, topic_key=topic_key, source="magi_autopilot"))
    except Exception:
        return False


def _load_telegram_targets() -> Tuple[str, list]:
    """
    Resolve TG bot token + notify chat IDs from env/OpenClaw config.
    """
    token = (
        os.environ.get("OPENCLAW_TELEGRAM_BOT_TOKEN")
        or os.environ.get("MAGI_TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    notify_ids = [
        x.strip()
        for x in (os.environ.get("MAGI_NOTIFY_TELEGRAM_IDS") or "").split(",")
        if x.strip()
    ]
    if token and notify_ids:
        return token, notify_ids
    try:
        _magi_cfg_path = str(get_config_path("config.json"))
        if os.path.exists(_magi_cfg_path):
            _magi_cfg = json.loads(Path(_magi_cfg_path).read_text(encoding="utf-8")) or {}
            _magi_tg = _magi_cfg.get("telegram") or {}
            _magi_notify = _magi_tg.get("notifyTo") or []
            if isinstance(_magi_notify, list):
                notify_ids.extend([str(x).strip() for x in _magi_notify if str(x).strip()])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1043, exc_info=True)
    try:
        oc_path = os.path.join(os.path.expanduser("~"), ".openclaw", "openclaw.json")
        if os.path.exists(oc_path):
            cfg = json.loads(Path(oc_path).read_text(encoding="utf-8")) or {}
            tg = (cfg.get("channels") or {}).get("telegram") or {}
            if not token:
                token = str(tg.get("botToken") or "").strip()
            notify_to = tg.get("notifyTo") or []
            if isinstance(notify_to, list):
                notify_ids.extend([str(x).strip() for x in notify_to if str(x).strip()])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1055, exc_info=True)
    dedup: list[str] = []
    seen: set[str] = set()
    for x in notify_ids:
        if x and x not in seen:
            seen.add(x)
            dedup.append(x)
    return token, dedup


def _notify_tg(text: str, *, topic_key: str = "check") -> bool:
    """
    Send notification to admin TG chats.
    """
    msg = str(text or "")
    try:
        from skills.ops.red_phone import send_telegram_push_with_status  # type: ignore

        st = send_telegram_push_with_status(
            msg,
            severity="info",
            source="magi_autopilot",
            topic_key=topic_key,
            queue_on_fail=True,
        ) or {}
        if bool(st.get("telegram")) or bool(st.get("queued")):
            return True
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1083, exc_info=True)

    token, notify_ids = _load_telegram_targets()
    if not token or not notify_ids:
        return False
    payload = json.dumps({"text": msg}, ensure_ascii=False).encode("utf-8")
    ok_any = False
    for chat_id in notify_ids:
        try:
            req = urlreq.Request(
                f"https://api.telegram.org/bot{token}/sendMessage?chat_id={chat_id}",
                data=payload,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urlreq.urlopen(req, timeout=10):
                pass
            ok_any = True
        except Exception:
            continue
    return ok_any


def _notify_system(text: str, *, topic_key: str = "check") -> bool:
    """
    System-check notifications: TG-only.
    """
    return _notify_tg(text, topic_key=topic_key)


def _line_local_webhook_selftest(timeout_sec: int = 8) -> Dict[str, Any]:
    """
    本機 LINE webhook 煙霧測試（不依賴外網）：
    - 產生合法 X-Line-Signature
    - POST 到 localhost:5002/callback
    """
    out: Dict[str, Any] = {"ok": False, "status": 0, "error": "", "endpoint": "http://127.0.0.1:5002/callback"}
    secret = (os.environ.get("MAGI_LINE_CHANNEL_SECRET") or os.environ.get("LINE_CHANNEL_SECRET") or "").strip()
    if not secret:
        out["error"] = "line_secret_missing"
        return out
    try:
        body = json.dumps({"destination": "U_LOCAL_SELFTEST", "events": []}, separators=(",", ":")).encode("utf-8")
        sig = base64.b64encode(hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()).decode("utf-8")
        req = urlreq.Request(
            out["endpoint"],
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "magi-autopilot-selftest",
                "X-Line-Signature": sig,
            },
        )
        with urlreq.urlopen(req, timeout=max(2, int(timeout_sec))) as resp:
            out["status"] = int(getattr(resp, "status", 0) or 0)
            _ = resp.read()
        out["ok"] = out["status"] == 200
        if not out["ok"]:
            out["error"] = f"http_{out['status']}"
        return out
    except urlerr.HTTPError as e:
        out["status"] = int(getattr(e, "code", 0) or 0)
        out["error"] = f"http_{out['status']}"
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out


def _check_local_process(pattern: str) -> Tuple[bool, str]:
    try:
        r = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and (r.stdout or "").strip():
            pids = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
            return True, ",".join(pids[:5])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1165, exc_info=True)
    return False, ""


def _resolve_discord_bot_token() -> str:
    for name in ("DISCORD_BOT_TOKEN", "MAGI_DISCORD_BOT_TOKEN"):
        raw = (os.environ.get(name) or "").strip()
        if raw:
            return raw
    try:
        cfg = _load_json(str(get_config_path("config.json"))) or {}
        return str(cfg.get("discord_bot_token") or "").strip()
    except Exception:
        return ""


def _official_discord_selftest(timeout_sec: int = 8) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": False,
        "process_running": False,
        "process_pids": "",
        "bot_id": "",
        "bot_tag": "",
        "error": "",
    }
    running, pid_text = _check_local_process("api/discord_bot.py")
    out["process_running"] = running
    out["process_pids"] = pid_text

    token = _resolve_discord_bot_token()
    if not token:
        out["error"] = "discord_bot_token_missing"
        return out
    try:
        req = urlreq.Request(
            "https://discord.com/api/v10/users/@me",
            headers={
                "Authorization": f"Bot {token}",
                "User-Agent": "magi-autopilot-selftest",
            },
        )
        with urlreq.urlopen(req, timeout=max(3, int(timeout_sec))) as resp:  # nosec B310
            raw = resp.read().decode("utf-8", errors="ignore")
        data = json.loads(raw) if raw else {}
        bot_id = str(data.get("id") or "").strip()
        username = str(data.get("username") or "").strip()
        disc = str(data.get("discriminator") or "").strip()
        tag = f"{username}#{disc}" if disc and disc != "0" else username
        out["bot_id"] = bot_id
        out["bot_tag"] = tag
        expected_id = str(os.environ.get("MAGI_DISCORD_EXPECTED_BOT_ID") or "").strip()
        if expected_id and bot_id and bot_id != expected_id:
            out["error"] = f"unexpected_bot_id:{bot_id}"
            return out
        out["ok"] = bool(running and bot_id)
        if not out["ok"]:
            out["error"] = "discord_bot_process_not_running"
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out


def _official_channel_smoke_selftest(timeout_sec: int = 45) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": False,
        "discord_ok": False,
        "line_ok": False,
        "telegram_ok": False,
        "report_path": "",
        "error": "",
    }
    smoke_script = _MAGI_ROOT / "scripts" / "ops" / "smoke_three_channels.py"
    if not smoke_script.exists():
        out["error"] = "smoke_three_channels_missing"
        return out

    report_path = Path(tempfile.gettempdir()) / "magi_three_channel_smoke_selftest.json"
    out["report_path"] = str(report_path)
    cmd = [VENV_PY, str(smoke_script), "--json-out", str(report_path)]
    result = _run_cmd(cmd, timeout_sec=max(15, int(timeout_sec)), env=os.environ.copy())
    if not result.ok and not report_path.exists():
        out["error"] = f"smoke_script_failed: rc={result.returncode}"
        return out

    try:
        payload = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
    except Exception as e:
        out["error"] = f"smoke_report_parse_failed: {type(e).__name__}: {e}"
        return out

    checks = payload.get("checks") if isinstance(payload, dict) else None
    if not isinstance(checks, list):
        out["error"] = "smoke_report_missing_checks"
        return out

    per_channel: Dict[str, Dict[str, int]] = {}
    for item in checks:
        if not isinstance(item, dict):
            continue
        channel = str(item.get("channel") or "").strip().upper()
        status = str(item.get("status") or "").strip().upper()
        if not channel or status not in {"PASS", "WARN", "FAIL", "SKIP"}:
            continue
        slot = per_channel.setdefault(channel, {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0})
        slot[status] += 1

    def _channel_ok(channel: str) -> bool:
        slot = per_channel.get(channel.upper(), {})
        return slot.get("FAIL", 0) == 0 and slot.get("PASS", 0) > 0

    out["discord_ok"] = _channel_ok("DISCORD")
    out["line_ok"] = _channel_ok("LINE")
    out["telegram_ok"] = _channel_ok("TELEGRAM")
    out["ok"] = bool(out["discord_ok"] and out["line_ok"] and out["telegram_ok"])
    if not out["ok"] and not out["error"]:
        out["error"] = "official_channel_smoke_not_green"
    return out


def _load_last_line_callback_age_sec() -> Optional[int]:
    """
    回傳最近 LINE callback 的秒數（age）。若不存在則回傳 None。
    """
    p = os.environ.get("MAGI_LINE_LAST_CALLBACK_FILE", os.path.join(MAGI_ROOT_DIR, ".agent", "line_last_callback.json"))
    try:
        if not os.path.exists(p):
            return None
        data = _load_json(p) or {}
        ts = float(data.get("updated_at") or 0)
        if ts <= 0:
            return None
        return max(0, int(time.time() - ts))
    except Exception:
        return None


def _comm_health_self_test() -> Dict[str, Any]:
    """
    通訊介面自檢：
    1) OpenClaw channel probe
    2) LINE 本機 webhook 簽章 smoke test
    3) 最近 callback 新鮮度
    """
    out: Dict[str, Any] = {
        "ok": True,
        "openclaw_probe": {},
        "line_local_webhook": {},
        "line_last_callback_age_sec": None,
        "warnings": [],
        "errors": [],
    }

    # 1) OpenClaw probe (retry to avoid false negative right after restart)
    try:
        probe_retries = max(1, int(os.environ.get("MAGI_OPENCLAW_PROBE_RETRIES", "3") or "3"))
    except Exception:
        probe_retries = 3
    try:
        probe_retry_wait = max(1, int(os.environ.get("MAGI_OPENCLAW_PROBE_RETRY_WAIT_SEC", "3") or "3"))
    except Exception:
        probe_retry_wait = 3

    probe = _run_cmd(["openclaw", "channels", "status", "--probe"], timeout_sec=40)
    probe_text = (probe.stdout or "") + "\n" + (probe.stderr or "")
    discord_line = ""
    for ln in probe_text.splitlines():
        s = ln.strip()
        if re.search(r"Discord\s+default\b", s, re.IGNORECASE):
            discord_line = s
            break
    discord_ok = bool(re.search(r"Discord\s+default\b.*\bworks\b", probe_text, re.IGNORECASE))
    line_ok = bool(re.search(r"LINE\s+default\b.*\bworks\b", probe_text, re.IGNORECASE))
    tg_ok = bool(re.search(r"Telegram\s+default\b.*\bworks\b", probe_text, re.IGNORECASE))

    attempt = 1
    while attempt < probe_retries and not (discord_ok and line_ok and tg_ok):
        time.sleep(probe_retry_wait)
        probe = _run_cmd(["openclaw", "channels", "status", "--probe"], timeout_sec=40)
        probe_text = (probe.stdout or "") + "\n" + (probe.stderr or "")
        for ln in probe_text.splitlines():
            s = ln.strip()
            if re.search(r"Discord\s+default\b", s, re.IGNORECASE):
                discord_line = s
                break
        discord_ok = bool(re.search(r"Discord\s+default\b.*\bworks\b", probe_text, re.IGNORECASE))
        line_ok = bool(re.search(r"LINE\s+default\b.*\bworks\b", probe_text, re.IGNORECASE))
        tg_ok = bool(re.search(r"Telegram\s+default\b.*\bworks\b", probe_text, re.IGNORECASE))
        attempt += 1
    warn_lines = []
    for ln in probe_text.splitlines():
        s = ln.strip()
        if s.startswith("- ") and "not configured" in s.lower():
            warn_lines.append(s)
    out["openclaw_probe"] = {
        "ok": bool(probe.ok and discord_ok and line_ok and tg_ok),
        "returncode": probe.returncode,
        "discord_ok": discord_ok,
        "line_ok": line_ok,
        "telegram_ok": tg_ok,
        "attempts": attempt,
        "warnings": warn_lines[:8],
    }
    discord_disabled = (": disabled" in discord_line.lower()) or ("error:disabled" in discord_line.lower())
    if not discord_ok and discord_disabled:
        official_discord = _official_discord_selftest(timeout_sec=8)
        out["official_discord"] = official_discord
        if official_discord.get("ok"):
            discord_ok = True
            out["openclaw_probe"]["discord_ok"] = True
            out["warnings"].append("openclaw_discord_disabled_using_official_bot")
    if not (discord_ok and line_ok and tg_ok):
        official_channels = _official_channel_smoke_selftest(timeout_sec=45)
        out["official_channel_smoke"] = official_channels
        recovered = []
        if not discord_ok and official_channels.get("discord_ok"):
            discord_ok = True
            out["openclaw_probe"]["discord_ok"] = True
            recovered.append("discord")
        if not line_ok and official_channels.get("line_ok"):
            line_ok = True
            out["openclaw_probe"]["line_ok"] = True
            recovered.append("line")
        if not tg_ok and official_channels.get("telegram_ok"):
            tg_ok = True
            out["openclaw_probe"]["telegram_ok"] = True
            recovered.append("telegram")
        if recovered:
            out["warnings"].append("openclaw_probe_recovered_by_official_channel_smoke:" + ",".join(recovered))
    out["openclaw_probe"]["ok"] = bool(probe.ok and discord_ok and line_ok and tg_ok)
    if not discord_ok:
        out["errors"].append("discord_channel_probe_failed")
    if not line_ok:
        out["errors"].append("line_channel_probe_failed")
    if not tg_ok:
        out["errors"].append("telegram_channel_probe_failed")
    if warn_lines:
        if line_ok:
            # OpenClaw doctor occasionally reports LINE token/secret missing even when probe is healthy.
            out["warnings"].append("openclaw_line_warning_false_positive")
        else:
            out["warnings"].append("openclaw_line_config_warning_present")

    # 2) Local LINE callback smoke
    local_cb = _line_local_webhook_selftest(timeout_sec=8)
    out["line_local_webhook"] = local_cb
    if not local_cb.get("ok"):
        out["errors"].append(f"line_local_webhook_failed:{local_cb.get('error')}")

    # 3) Callback freshness
    age = _load_last_line_callback_age_sec()
    out["line_last_callback_age_sec"] = age
    fresh_threshold = int(os.environ.get("MAGI_LINE_HEALTH_RECENT_CALLBACK_SEC", "1200") or "1200")
    if age is None:
        out["warnings"].append("line_last_callback_missing")
    elif age > max(60, fresh_threshold * 2):
        out["warnings"].append(f"line_callback_stale:{age}s")

    out["ok"] = len(out["errors"]) == 0
    return out


def _env_on(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _csv_values(raw: str) -> list:
    return [item.strip() for item in str(raw or "").split(",") if item.strip()]


def _openclaw_auth_mode_guard() -> Dict[str, Any]:
    allowed_statuses = _csv_values(
        os.environ.get(
            "MAGI_OPENCLAW_AUTH_GUARD_ALLOW_STATUSES",
            "SAFE_OAUTH_ONLY,SAFE_LOCAL_ONLY",
        )
    )
    out: Dict[str, Any] = {
        "ok": False,
        "status": "unknown",
        "allowed_statuses": allowed_statuses,
        "script_path": OPENCLAW_AUTH_GUARD_SCRIPT,
        "summary": {},
        "reasons": [],
        "error": "",
    }
    if not os.path.exists(OPENCLAW_AUTH_GUARD_SCRIPT):
        out["status"] = "script_missing"
        out["error"] = f"script_missing:{OPENCLAW_AUTH_GUARD_SCRIPT}"
        return out

    try:
        timeout_sec = int(os.environ.get("MAGI_OPENCLAW_AUTH_GUARD_TIMEOUT_SEC", "20") or "20")
    except Exception:
        timeout_sec = 20

    res = _run_cmd(
        [VENV_PY, OPENCLAW_AUTH_GUARD_SCRIPT, "--json"],
        timeout_sec=max(5, timeout_sec),
        env=os.environ.copy(),
    )
    parsed = res.parsed if isinstance(res.parsed, dict) else {}
    out["returncode"] = int(res.returncode or 0)
    out["stdout_tail"] = (res.stdout or "")[-1200:]
    out["stderr_tail"] = (res.stderr or "")[-600:]
    if not isinstance(parsed, dict) or not parsed:
        out["status"] = "parse_failed"
        out["error"] = "auth_guard_parse_failed"
        return out

    status = str(parsed.get("status") or "").strip() or "missing_status"
    out["status"] = status
    out["summary"] = parsed.get("summary") or {}
    out["reasons"] = list(parsed.get("reasons") or [])[:8]
    out["risky_env_vars"] = list(parsed.get("risky_env_vars") or [])[:8]
    out["risky_provider_keys"] = list(parsed.get("risky_provider_keys") or [])[:8]
    out["risky_profile_keys"] = list(parsed.get("risky_profile_keys") or [])[:8]
    out["matching_profiles"] = list(parsed.get("matching_profiles") or [])[:4]
    out["ok"] = status in set(allowed_statuses)
    if not out["ok"]:
        reason_hint = "; ".join(out["reasons"]) or f"status={status}"
        out["error"] = f"auth_mode_not_allowed:{status}:{reason_hint}"
    return out


def _openclaw_runtime_mode_report() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": False,
        "status_code": "UNKNOWN",
        "mode": "未知",
        "summary": "",
        "next_action": "",
        "current_primary_model": "",
        "auth_mode_status": "",
        "quota_reason": "",
        "hold_until_iso": "",
        "script_path": OPENCLAW_RUNTIME_MODE_SCRIPT,
        "error": "",
    }
    if not os.path.exists(OPENCLAW_RUNTIME_MODE_SCRIPT):
        out["status_code"] = "SCRIPT_MISSING"
        out["error"] = f"script_missing:{OPENCLAW_RUNTIME_MODE_SCRIPT}"
        return out

    try:
        timeout_sec = int(os.environ.get("MAGI_OPENCLAW_RUNTIME_MODE_TIMEOUT_SEC", "20") or "20")
    except Exception:
        timeout_sec = 20

    res = _run_cmd(
        [VENV_PY, OPENCLAW_RUNTIME_MODE_SCRIPT, "--json"],
        timeout_sec=max(5, timeout_sec),
        env=os.environ.copy(),
    )
    parsed = res.parsed if isinstance(res.parsed, dict) else {}
    out["returncode"] = int(res.returncode or 0)
    out["stdout_tail"] = (res.stdout or "")[-1200:]
    out["stderr_tail"] = (res.stderr or "")[-600:]
    if not isinstance(parsed, dict) or not parsed:
        out["status_code"] = "PARSE_FAILED"
        out["error"] = "runtime_mode_parse_failed"
        return out

    for key in [
        "status_code",
        "mode",
        "summary",
        "next_action",
        "current_primary_model",
        "auth_mode_status",
        "quota_reason",
        "hold_until_iso",
        "usage_active_reason",
        "usage_active_until_iso",
    ]:
        if key in parsed:
            out[key] = parsed.get(key)
    out["ok"] = True
    return out


def _format_openclaw_runtime_lines(runtime: Any, *, include_next_action: bool = True) -> list[str]:
    if not isinstance(runtime, dict) or not runtime:
        return []

    mode = str(runtime.get("mode") or "未知").strip() or "未知"
    status_code = str(runtime.get("status_code") or "UNKNOWN").strip() or "UNKNOWN"
    primary_model = str(runtime.get("current_primary_model") or "").strip()
    auth_mode = str(runtime.get("auth_mode_status") or "").strip()
    summary = str(runtime.get("summary") or "").strip()
    quota_reason = str(runtime.get("quota_reason") or runtime.get("usage_active_reason") or "").strip()
    hold_until = str(runtime.get("hold_until_iso") or runtime.get("usage_active_until_iso") or "").strip()
    next_action = str(runtime.get("next_action") or "").strip()
    error = str(runtime.get("error") or "").strip()

    lines = ["OpenClaw 狀態：", f"- 模式：{mode}（{status_code}）"]
    if primary_model:
        lines.append(f"- 主模型：{primary_model}")
    if auth_mode:
        lines.append(f"- Auth：{auth_mode}")
    if summary:
        lines.append(f"- 摘要：{summary}")
    if quota_reason:
        lines.append(f"- 原因：{quota_reason}")
    if hold_until:
        lines.append(f"- 恢復檢查時間：{hold_until}")
    if include_next_action and next_action:
        lines.append(f"- 下一步：{next_action}")
    if error:
        lines.append(f"- 附註：{error}")
    return lines


def _judicial_api_pipeline_report() -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": False,
        "status": "UNKNOWN",
        "summary": {},
        "credentials": {},
        "pull": {},
        "process": {},
        "backlog": {},
        "normalized": {},
        "reasons": [],
        "script_path": JUDICIAL_API_PIPELINE_SCRIPT,
        "error": "",
    }
    if not os.path.exists(JUDICIAL_API_PIPELINE_SCRIPT):
        out["status"] = "SCRIPT_MISSING"
        out["error"] = f"script_missing:{JUDICIAL_API_PIPELINE_SCRIPT}"
        return out

    try:
        timeout_sec = int(os.environ.get("MAGI_JUDICIAL_API_PIPELINE_TIMEOUT_SEC", "20") or "20")
    except Exception:
        timeout_sec = 20

    res = _run_cmd(
        [VENV_PY, JUDICIAL_API_PIPELINE_SCRIPT, "--json"],
        timeout_sec=max(5, timeout_sec),
        env=os.environ.copy(),
    )
    parsed = res.parsed if isinstance(res.parsed, dict) else {}
    out["returncode"] = int(res.returncode or 0)
    out["stdout_tail"] = (res.stdout or "")[-1200:]
    out["stderr_tail"] = (res.stderr or "")[-600:]
    if not isinstance(parsed, dict) or not parsed:
        out["status"] = "PARSE_FAILED"
        out["error"] = "judicial_api_pipeline_parse_failed"
        return out

    for key in [
        "status",
        "summary",
        "credentials",
        "pull",
        "process",
        "backlog",
        "normalized",
        "reasons",
    ]:
        if key in parsed:
            out[key] = parsed.get(key)
    out["ok"] = str(out.get("status") or "").strip() == "PIPELINE_HEALTHY"
    return out


def _format_judicial_api_pipeline_lines(pipeline: Any) -> list[str]:
    if not isinstance(pipeline, dict) or not pipeline:
        return []

    status = str(pipeline.get("status") or "UNKNOWN").strip() or "UNKNOWN"
    credentials = pipeline.get("credentials") if isinstance(pipeline.get("credentials"), dict) else {}
    pull = pipeline.get("pull") if isinstance(pipeline.get("pull"), dict) else {}
    latest_pull = pull.get("latest") if isinstance(pull.get("latest"), dict) else {}
    process = pipeline.get("process") if isinstance(pipeline.get("process"), dict) else {}
    backlog = pipeline.get("backlog") if isinstance(pipeline.get("backlog"), dict) else {}
    normalized = pipeline.get("normalized") if isinstance(pipeline.get("normalized"), dict) else {}
    reasons = list(pipeline.get("reasons") or [])[:3]
    error = str(pipeline.get("error") or "").strip()

    lines = ["司法院 API 狀態：", f"- 狀態：{status}"]

    cred_sources = [str(x).strip() for x in (credentials.get("sources") or []) if str(x).strip()]
    if credentials:
        lines.append(
            f"- 帳密：{'已就緒' if credentials.get('present') else '缺少'}"
            + (f"（來源：{', '.join(cred_sources)}）" if cred_sources else "")
        )

    pull_ts = str(pull.get("latest_ts") or "").strip()
    pull_age = pull.get("latest_age_hours")
    if pull_ts or latest_pull:
        pull_line = f"- 夜拉：{pull_ts or '無成功紀錄'}"
        if pull_age is not None:
            pull_line += f"（{pull_age}h 前）"
        if latest_pull:
            pull_line += (
                f" / fetched={latest_pull.get('fetched', '-')}"
                f" / failed={latest_pull.get('failed', '-')}"
                f" / skipped={latest_pull.get('skipped', '-')}"
            )
        cred_source = str(pull.get("credentials_source") or "").strip()
        if cred_source:
            pull_line += f" / creds={cred_source}"
        lines.append(pull_line)

    proc_ts = str(process.get("updated_at") or "").strip()
    proc_age = process.get("updated_age_hours")
    if proc_ts or process:
        proc_line = f"- 晨整：{proc_ts or '無狀態檔'}"
        if proc_age is not None:
            proc_line += f"（{proc_age}h 前）"
        if process.get("processed_entries") is not None:
            proc_line += f" / processed={process.get('processed_entries', '-')}"
        lines.append(proc_line)

    raw_total = backlog.get("raw_total")
    pending = backlog.get("backlog_count")
    oldest_pending = backlog.get("oldest_backlog_age_hours")
    if raw_total is not None or pending is not None:
        backlog_line = f"- Raw backlog：pending={pending if pending is not None else '-'} / raw_total={raw_total if raw_total is not None else '-'}"
        if oldest_pending is not None:
            backlog_line += f" / oldest={oldest_pending}h"
        lines.append(backlog_line)

    normalized_count = normalized.get("count")
    normalized_ts = str(normalized.get("latest_at") or "").strip()
    if normalized_count is not None:
        norm_line = f"- Normalized：{normalized_count} 份"
        if normalized_ts:
            norm_line += f" / latest={normalized_ts}"
        lines.append(norm_line)

    for item in reasons:
        lines.append(f"- 摘要：{item}")

    pending_examples = [str(x).strip() for x in (backlog.get("pending_examples") or []) if str(x).strip()]
    if pending_examples:
        lines.append(f"- 待處理示例：{'; '.join(pending_examples[:3])}")
    if error:
        lines.append(f"- 附註：{error}")
    return lines


def _big_brain_health_probe() -> Dict[str, Any]:
    """
    Big Brain 健康探測器：
    - 檢查 Melchior 遠端健康
    - 進行一次「實際推理回覆」探測（避免只活著但空回覆）
    - 失敗時嘗試對 Melchior 發切換訊號自修復（force distributed）
    - 仍失敗則降級 local（可由 require_distributed 決定是否視為 block）
    """
    out: Dict[str, Any] = {
        "ok": False,
        "status": "unknown",
        "mode_before": "",
        "mode_after": "",
        "require_distributed": _env_on("MAGI_BIG_BRAIN_REQUIRE_DISTRIBUTED", False),
        "require_main_model": _env_on("MAGI_BIG_BRAIN_REQUIRE_MAIN_MODEL", False),
        "auto_heal": _env_on("MAGI_BIG_BRAIN_AUTO_HEAL", True),
        "auto_fallback_local": _env_on("MAGI_BIG_BRAIN_AUTO_FALLBACK_LOCAL", True),
        "switch_events": [],
        "remote_health": {},
        "inference_probe": {},
        "error": "",
    }
    try:
        if MAGI_ROOT_DIR not in sys.path:
            sys.path.insert(0, MAGI_ROOT_DIR)
        from skills.brain_manager import action as brain_manager  # type: ignore
        from skills.bridge import melchior_client  # type: ignore
    except Exception as e:
        out["status"] = "probe_import_failed"
        out["error"] = f"import_failed:{type(e).__name__}: {e}"
        return out

    def _switch(mode: str, force: bool, reason: str) -> Dict[str, Any]:
        ev: Dict[str, Any] = {"mode": mode, "force": bool(force), "reason": reason, "success": False, "api_url": ""}
        try:
            ok, api = brain_manager.restart_inference_engine(mode, force=force)
            ev["success"] = bool(ok)
            ev["api_url"] = str(api or "")
            if not ok:
                ev["error"] = f"restart_inference_engine({mode}) failed"
        except Exception as e:
            ev["success"] = False
            ev["error"] = f"{type(e).__name__}: {e}"
        out["switch_events"].append(ev)
        return ev

    def _model_family(name: str) -> str:
        s = str(name or "").strip().lower()
        if not s:
            return ""
        s = s.split(":", 1)[0]
        # e.g. qwen3-30b-instruct.Q4_K_M.gguf -> qwen3
        m = re.match(r"^([a-z]+[0-9]*(?:\.[0-9]+)?)", s)
        if m:
            return m.group(1)
        return s

    def _same_model_family(a: str, b: str) -> bool:
        fa = _model_family(a)
        fb = _model_family(b)
        return bool(fa and fb and fa == fb)

    def _probe_once(timeout_sec: int = 70) -> Dict[str, Any]:
        probe_prompt = (os.environ.get("MAGI_BIG_BRAIN_PROBE_PROMPT", "") or "").strip()
        if not probe_prompt:
            probe_prompt = "健康探測，請只回覆：OK"
        model_hint = (os.environ.get("MAGI_MAIN_MODEL", "") or "").strip()
        t0 = time.monotonic()
        use_timeout = max(20, int(timeout_sec))
        errors = []
        tried_models = []
        # Remote-only probe: do not accept local fallback, otherwise Big Brain health is misreported.
        try:
            available = melchior_client._list_ollama_models(  # type: ignore[attr-defined]
                melchior_client.MELCHIOR_HOST,  # type: ignore[attr-defined]
                melchior_client.MELCHIOR_OLLAMA_PORT,  # type: ignore[attr-defined]
                force_refresh=True,
            )
        except Exception:
            available = []

        candidates = [model_hint]
        try:
            candidates += melchior_client._fallback_remote_models(model_hint, available)  # type: ignore[attr-defined]
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 1798, exc_info=True)
        # de-dup
        uniq = []
        seen = set()
        for m in candidates:
            ms = str(m or "").strip()
            if not ms:
                continue
            key = ms.lower()
            if key in seen:
                continue
            seen.add(key)
            uniq.append(ms)
        candidates = uniq[:3]
        if not candidates:
            candidates = [model_hint]
        try:
            probe_num_predict = int(os.environ.get("MAGI_BIG_BRAIN_PROBE_NUM_PREDICT", "256") or "256")
        except Exception:
            probe_num_predict = 256
        probe_num_predict = max(64, min(probe_num_predict, 512))

        # Prefer llama-server /v1 probe first (force API port), because remote Ollama
        # /api/generate can stall under load even when /v1 remains healthy.
        try:
            v1_host = str(melchior_client.MELCHIOR_HOST)  # type: ignore[attr-defined]
            v1_port = int(getattr(melchior_client, "MELCHIOR_API_PORT", 8080))  # type: ignore[attr-defined]
            v1_base = f"http://{v1_host}:{v1_port}/v1"
            v1_timeout = max(10, min(use_timeout, 60))

            req_models = urlreq.Request(
                f"{v1_base}/models",
                headers={"User-Agent": "magi-autopilot/1.0"},
            )
            with urlreq.urlopen(req_models, timeout=max(6, min(v1_timeout, 12))) as resp:  # nosec B310
                models_raw = resp.read().decode("utf-8", errors="ignore")
            models_data = json.loads(models_raw) if models_raw else {}
            model_ids = []
            for it in (models_data.get("data") or []):
                if isinstance(it, dict) and it.get("id"):
                    model_ids.append(str(it.get("id")).strip())
            # Prefer qwen model on /v1 if available.
            v1_model = ""
            for m in model_ids:
                if m.lower().startswith("qwen3"):
                    v1_model = m
                    break
            if not v1_model and model_ids:
                v1_model = model_ids[0]
            if v1_model:
                tried_models.append(v1_model)
                payload = {
                    "model": v1_model,
                    "messages": [{"role": "user", "content": probe_prompt}],
                    "temperature": 0.0,
                    "max_tokens": int(probe_num_predict),
                    "stream": False,
                }
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req_chat = urlreq.Request(
                    f"{v1_base}/chat/completions",
                    data=body,
                    headers={"Content-Type": "application/json", "User-Agent": "magi-autopilot/1.0"},
                )
                with urlreq.urlopen(req_chat, timeout=v1_timeout) as resp:  # nosec B310
                    raw = resp.read().decode("utf-8", errors="ignore")
                data = json.loads(raw) if raw else {}
                choices = data.get("choices") or []
                text = ""
                if choices and isinstance(choices, list) and isinstance(choices[0], dict):
                    msg = choices[0].get("message")
                    if isinstance(msg, dict):
                        text = str(msg.get("content") or "").strip()
                        # qwen3 may occasionally output only reasoning tokens first.
                        if not text:
                            text = str(msg.get("reasoning_content") or "").strip()
                if text:
                    return {
                        "success": True,
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                        "error": "",
                        "model": v1_model,
                        "route": "melchior_openai_v1",
                        "response_len": len(text),
                        "response_preview": text[:80],
                        "degraded": (not _same_model_family(v1_model, model_hint)),
                        "tried_models": tried_models,
                    }
                errors.append("empty_v1_response")
        except Exception as e:
            errors.append(f"openai_v1_probe_failed:{type(e).__name__}: {e}")

        remain = use_timeout
        for idx, cand in enumerate(candidates):
            tried_models.append(cand)
            slots_left = max(1, len(candidates) - idx)
            per_try = max(8, int(remain / slots_left))
            try:
                resp = melchior_client._chat_ollama(  # type: ignore[attr-defined]
                    probe_prompt,
                    cand,
                    per_try,
                    host=melchior_client.MELCHIOR_HOST,  # type: ignore[attr-defined]
                    port=melchior_client.MELCHIOR_OLLAMA_PORT,  # type: ignore[attr-defined]
                    num_predict=probe_num_predict,
                    num_ctx_override=4096,
                    temperature_override=0.0,
                )
                text = str(resp.get("response") or "").strip()
                if resp.get("success") and text:
                    return {
                        "success": True,
                        "latency_ms": int((time.monotonic() - t0) * 1000),
                        "error": "",
                        "model": str(resp.get("model") or cand),
                        "route": "melchior_ollama",
                        "response_len": len(text),
                        "response_preview": text[:80],
                        "degraded": (not _same_model_family(cand, model_hint)),
                        "tried_models": tried_models,
                    }
                errors.append(str(resp.get("error") or "empty_response"))
            except Exception as e:
                errors.append(f"{type(e).__name__}: {e}")
            remain = max(8, use_timeout - int(time.monotonic() - t0))
            if remain <= 8:
                break

        return {
            "success": False,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": "remote_probe_failed: " + " | ".join(errors[:4]),
            "model": model_hint,
            "route": "",
            "response_len": 0,
            "response_preview": "",
            "degraded": False,
            "tried_models": tried_models,
        }

    def _is_remote_probe_success(probe_obj: Dict[str, Any]) -> bool:
        if not isinstance(probe_obj, dict):
            return False
        if not bool(probe_obj.get("success")):
            return False
        if out["require_main_model"] and probe_obj.get("degraded", False):
            return False
        route = str(probe_obj.get("route") or "").strip().lower()
        return route.startswith("melchior_")

    def _fetch_melchior_mode() -> str:
        try:
            hurl = f"{brain_manager.MELCHIOR_AGENT_ENDPOINT}/health"
            req = urlreq.Request(hurl, headers={"User-Agent": "magi-autopilot/1.0"})
            with urlreq.urlopen(req, timeout=5) as resp:  # nosec B310
                raw = resp.read().decode("utf-8", errors="ignore")
            data = json.loads(raw) if raw else {}
            mode = str((data or {}).get("mode") or "").strip().lower()
            return mode
        except Exception:
            return ""

    try:
        out["mode_before"] = str(brain_manager.get_brain_mode() or "")
    except Exception:
        out["mode_before"] = ""
    out["melchior_mode_before"] = _fetch_melchior_mode()

    # 優先嘗試進入 distributed（會對 Melchior 發 mode switch 訊號）。
    if out["mode_before"] != "distributed":
        _switch("distributed", force=False, reason="ensure_distributed_before_probe")
    if out.get("melchior_mode_before") != "distributed":
        try:
            ev_mode = {"mode": "distributed", "force": False, "reason": "ensure_melchior_distributed_before_probe"}
            ev_mode["success"] = bool(brain_manager.set_melchior_mode("distributed"))
            out["switch_events"].append(ev_mode)
            time.sleep(1.5)
        except Exception as e:
            out["switch_events"].append(
                {
                    "mode": "distributed",
                    "force": False,
                    "reason": "ensure_melchior_distributed_before_probe",
                    "success": False,
                    "error": f"{type(e).__name__}: {e}",
                }
            )

    remote_ok, remote_msg = brain_manager.check_remote_health()
    out["remote_health"] = {"ok": bool(remote_ok), "message": str(remote_msg)}
    loading_detected = "loading model" in str(remote_msg or "").lower()
    out["melchior_mode_after_switch"] = _fetch_melchior_mode()

    model_hint = (os.environ.get("MAGI_MAIN_MODEL", "") or "").strip()
    # 先做一次 warmup，降低主模型冷啟動 timeout 機率。
    try:
        warmup_timeout = int(os.environ.get("MAGI_BIG_BRAIN_WARMUP_TIMEOUT_SEC", "45") or "45")
    except Exception:
        warmup_timeout = 45
    try:
        out["warmup"] = melchior_client.warmup(model=model_hint, timeout=max(10, warmup_timeout))
    except Exception as e:
        out["warmup"] = {"success": False, "error": f"{type(e).__name__}: {e}"}
    # Older Melchior agents may not expose /api/warmup (404). Fallback to direct Ollama warmup.
    warmup_ok = bool((out.get("warmup") or {}).get("success"))
    warmup_err = str((out.get("warmup") or {}).get("error") or "").lower()
    if (not warmup_ok) and out["require_main_model"] and ("404" in warmup_err or "not found" in warmup_err):
        try:
            direct_timeout = int(os.environ.get("MAGI_BIG_BRAIN_DIRECT_WARMUP_TIMEOUT_SEC", "220") or "220")
        except Exception:
            direct_timeout = 220
        try:
            payload = {
                "model": model_hint,
                "prompt": "健康探測預熱，請只回覆OK",
                "stream": False,
                "keep_alive": os.environ.get("MELCHIOR_KEEP_ALIVE", "15m"),
                "options": {"num_predict": 16, "temperature": 0.0},
            }
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            o_url = f"http://{brain_manager.MELCHIOR_IP}:{brain_manager.MELCHIOR_OLLAMA_PORT}/api/generate"
            req = urlreq.Request(o_url, data=body, headers={"Content-Type": "application/json", "User-Agent": "magi-autopilot/1.0"})
            t1 = time.monotonic()
            with urlreq.urlopen(req, timeout=max(20, int(direct_timeout))) as resp:  # nosec B310
                txt = resp.read().decode("utf-8", errors="ignore")
            out["warmup_direct"] = {
                "success": True,
                "latency_ms": int((time.monotonic() - t1) * 1000),
                "response_preview": txt[:120],
            }
        except Exception as e:
            out["warmup_direct"] = {"success": False, "error": f"{type(e).__name__}: {e}"}

    try:
        fresh_window_sec = int(os.environ.get("MAGI_BIG_BRAIN_SUCCESS_FRESH_SEC", "5400") or "5400")
    except Exception:
        fresh_window_sec = 5400
    try:
        quick_timeout_sec = int(os.environ.get("MAGI_BIG_BRAIN_PROBE_TIMEOUT_QUICK_SEC", "45") or "45")
    except Exception:
        quick_timeout_sec = 45
    try:
        full_timeout_sec = int(os.environ.get("MAGI_BIG_BRAIN_PROBE_TIMEOUT_SEC", "140") or "140")
    except Exception:
        full_timeout_sec = 140
    last_success_age = _last_big_brain_success_age_sec()
    out["last_distributed_success_age_sec"] = last_success_age
    try:
        loading_grace_sec = int(os.environ.get("MAGI_BIG_BRAIN_LOADING_GRACE_SEC", "900") or "900")
    except Exception:
        loading_grace_sec = 900
    loading_grace_sec = max(120, min(loading_grace_sec, 7200))
    loading_stale = bool(
        loading_detected
        and (
            last_success_age is None
            or last_success_age > loading_grace_sec
        )
    )
    out["loading_grace_sec"] = loading_grace_sec
    out["loading_stale"] = loading_stale
    prefer_quick = (not out["require_main_model"]) and (last_success_age is not None) and (last_success_age <= max(300, fresh_window_sec))
    probe_timeout = quick_timeout_sec if prefer_quick else full_timeout_sec
    out["probe_timeout_sec"] = int(max(20, probe_timeout))

    probe = _probe_once(timeout_sec=int(max(20, probe_timeout)))
    out["inference_probe"] = probe
    melchior_mode = str(out.get("melchior_mode_after_switch") or "").strip().lower()
    melchior_distributed = melchior_mode in {"distributed", "big_brain", "big-brain", "big"}
    distributed_healthy = bool(remote_ok and _is_remote_probe_success(probe) and melchior_distributed)

    # 第一次失敗時，做一次 force distributed 自修復再測一次。
    if (not distributed_healthy) and out["auto_heal"]:
        _switch("distributed", force=True, reason="self_heal_distributed_after_probe_fail")
        remote_ok2, remote_msg2 = brain_manager.check_remote_health()
        out["remote_health_retry"] = {"ok": bool(remote_ok2), "message": str(remote_msg2)}
        loading_detected = loading_detected or ("loading model" in str(remote_msg2 or "").lower())
        probe2 = _probe_once(timeout_sec=int(max(20, full_timeout_sec)))
        out["inference_probe_retry"] = probe2
        melchior_mode = _fetch_melchior_mode()
        out["melchior_mode_after_retry_switch"] = melchior_mode
        melchior_distributed = str(melchior_mode or "").strip().lower() in {"distributed", "big_brain", "big-brain", "big"}
        distributed_healthy = bool(remote_ok2 and _is_remote_probe_success(probe2) and melchior_distributed)

    try:
        long_timeout = int(os.environ.get("MAGI_BIG_BRAIN_PROBE_TIMEOUT_LONG_SEC", "120") or "120")
    except Exception:
        long_timeout = 120
    long_timeout = max(60, min(long_timeout, 240))

    def _hard_reset_and_reprobe(probe_timeout_sec: int) -> bool:
        nonlocal melchior_mode, melchior_distributed, distributed_healthy
        if not out["auto_heal"]:
            return False
        reset_info: Dict[str, Any] = {"triggered": True, "ready": False}
        try:
            reset_info["to_engineer"] = bool(brain_manager.set_melchior_mode("engineer"))
            time.sleep(2.0)
            reset_info["to_distributed"] = bool(brain_manager.set_melchior_mode("distributed"))
        except Exception as e:
            reset_info["error"] = f"melchior_hard_reset_failed:{type(e).__name__}: {e}"
            out["melchior_loading_hard_reset"] = reset_info
            return False

        try:
            wait_sec = int(os.environ.get("MAGI_BIG_BRAIN_LOADING_RESET_WAIT_SEC", "45") or "45")
        except Exception:
            wait_sec = 45
        wait_sec = max(20, min(wait_sec, 120))
        deadline = time.time() + wait_sec
        last_status = ""
        models_url = f"{brain_manager.MELCHIOR_API_ENDPOINT}/models"
        while time.time() < deadline:
            try:
                req = urlreq.Request(models_url, headers={"User-Agent": "magi-autopilot/1.0"})
                with urlreq.urlopen(req, timeout=5) as resp:  # nosec B310
                    code = int(getattr(resp, "status", 0) or 0)
                    if code == 200:
                        reset_info["ready"] = True
                        last_status = "200"
                        break
                    last_status = str(code)
            except Exception as e:
                last_status = str(e)
            time.sleep(2.0)
        reset_info["last_status"] = last_status
        out["melchior_loading_hard_reset"] = reset_info
        if not reset_info.get("ready"):
            return False

        probe4 = _probe_once(timeout_sec=max(60, int(probe_timeout_sec)))
        out["inference_probe_after_hard_reset"] = probe4
        melchior_mode = _fetch_melchior_mode()
        out["melchior_mode_after_hard_reset"] = melchior_mode
        melchior_distributed = str(melchior_mode or "").strip().lower() in {"distributed", "big_brain", "big-brain", "big"}
        distributed_healthy = bool(_is_remote_probe_success(probe4) and melchior_distributed)
        return bool(distributed_healthy)

    # 若仍失敗但遠端處於 model loading，給一次較長 timeout 的探測機會。
    if not distributed_healthy:
        rmsg = str((out.get("remote_health_retry") or out.get("remote_health") or {}).get("message") or "").lower()
        is_loading = "loading model" in rmsg
        if is_loading and loading_stale:
            out["loading_stale_reset"] = True
            is_loading = False
        if is_loading:
            probe3 = _probe_once(timeout_sec=max(20, long_timeout))
            out["inference_probe_loading_retry"] = probe3
            melchior_mode = _fetch_melchior_mode()
            out["melchior_mode_after_loading_retry"] = melchior_mode
            melchior_distributed = str(melchior_mode or "").strip().lower() in {"distributed", "big_brain", "big-brain", "big"}
            distributed_healthy = bool(_is_remote_probe_success(probe3) and melchior_distributed)
            loading_detected = True
            # Loading 階段預設不做硬重置，避免把正常載入中的分散式反覆打斷。
            if (not distributed_healthy) and _env_on("MAGI_BIG_BRAIN_LOADING_HARD_RESET", False):
                _hard_reset_and_reprobe(probe_timeout_sec=int(long_timeout))
        elif out["auto_heal"]:
            # 非 loading 失敗（常見為連線 timeout）也只做一次硬重置，避免重試過久。
                _hard_reset_and_reprobe(probe_timeout_sec=max(20, int(full_timeout_sec)))

    # 若仍未恢復，交給 Melchior 執行遠端自修復（CASPER 觸發）。
    if (not distributed_healthy) and out["auto_heal"] and (not loading_detected or loading_stale) and _env_on("MAGI_BIG_BRAIN_REMOTE_REPAIR", True):
        rr: Dict[str, Any] = {"triggered": True, "ok": False}
        try:
            rr_timeout = int(os.environ.get("MAGI_BIG_BRAIN_REMOTE_REPAIR_TIMEOUT_SEC", "150") or "150")
        except Exception:
            rr_timeout = 300
        rr_timeout = max(60, min(rr_timeout, 900))
        try:
            ok_rr, rr_payload = brain_manager.remote_repair_distributed(
                model=model_hint,
                timeout_sec=rr_timeout,
                force_cycle=True,
            )
            rr["ok"] = bool(ok_rr)
            rr["payload"] = rr_payload
        except Exception as e:
            rr["ok"] = False
            rr["error"] = f"remote_repair_exception:{type(e).__name__}: {e}"
        out["remote_repair"] = rr

        if rr.get("ok"):
            remote_ok_rr, remote_msg_rr = brain_manager.check_remote_health()
            out["remote_health_after_remote_repair"] = {"ok": bool(remote_ok_rr), "message": str(remote_msg_rr)}
            probe_rr = _probe_once(timeout_sec=max(30, int(full_timeout_sec)))
            out["inference_probe_after_remote_repair"] = probe_rr
            melchior_mode = _fetch_melchior_mode()
            out["melchior_mode_after_remote_repair"] = melchior_mode
            melchior_distributed = str(melchior_mode or "").strip().lower() in {"distributed", "big_brain", "big-brain", "big"}
            distributed_healthy = bool(remote_ok_rr and _is_remote_probe_success(probe_rr) and melchior_distributed)

    if distributed_healthy:
        final_probe = (
            out.get("inference_probe_after_remote_repair")
            if isinstance(out.get("inference_probe_after_remote_repair"), dict)
            else out.get("inference_probe_after_hard_reset")
            if isinstance(out.get("inference_probe_after_hard_reset"), dict)
            else out.get("inference_probe_loading_retry")
            if isinstance(out.get("inference_probe_loading_retry"), dict)
            else out.get("inference_probe_retry")
            if isinstance(out.get("inference_probe_retry"), dict)
            else out.get("inference_probe")
        )
        final_probe = final_probe if isinstance(final_probe, dict) else {}
        probe_model = str(final_probe.get("model") or "").strip()
        main_model_ready = _same_model_family(probe_model, model_hint)
        out["main_model_target"] = model_hint
        out["main_model_ready"] = main_model_ready
        if main_model_ready:
            out["status"] = "distributed_healthy"
            out["ok"] = True
        else:
            out["status"] = "distributed_degraded_model"
            out["ok"] = (not out["require_main_model"])
            if out["require_main_model"]:
                out["error"] = "distributed_reachable_but_main_model_not_ready"
        if out.get("ok"):
            _mark_big_brain_success(out)
    else:
        fallback_ok = False
        # If Melchior is in distributed mode and reports model loading, keep distributed
        # and mark as warming_up instead of forcing local fallback.
        if loading_detected and melchior_distributed and not loading_stale:
            out["status"] = "warming_up"
            out["ok"] = True
            out["error"] = "distributed_loading_model"
            out["warming_up"] = True
        else:
            if out["auto_fallback_local"]:
                ev = _switch("local", force=True, reason="distributed_unhealthy_fallback_local")
                fallback_ok = bool(ev.get("success"))
                out["fallback_local"] = {"ok": fallback_ok}
            if fallback_ok:
                out["status"] = "degraded_local"
                if out["require_distributed"]:
                    out["ok"] = False
                    out["error"] = "distributed_unavailable_fallback_local"
                else:
                    out["ok"] = True
            else:
                out["status"] = "inference_offline"
                out["ok"] = False
                out["error"] = out.get("error") or "distributed_unavailable_and_local_fallback_failed"

    try:
        out["mode_after"] = str(brain_manager.get_brain_mode() or "")
    except Exception:
        out["mode_after"] = ""
    out["melchior_mode_final"] = _fetch_melchior_mode()
    return out


def _openclaw_session_selfheal(max_files: int = 80) -> Dict[str, Any]:
    """
    修補 OpenClaw session 中缺失的 modelApi/modelProvider/model 欄位。
    目的：避免 `No API provider registered for api: undefined` 導致通訊偶發不回。
    """
    out: Dict[str, Any] = {
        "ok": True,
        "changed_files": 0,
        "changed_rows": 0,
        "session_entries_patched": 0,
        "backup_dir": "",
        "errors": [],
    }
    base = Path("/Users/ai/.openclaw/agents/main/sessions")
    if not base.exists():
        out["ok"] = False
        out["errors"].append("sessions_dir_missing")
        return out
    fallback_model = "ollama/taide-12b"
    try:
        cfg_path = Path.home() / ".openclaw" / "openclaw.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        primary = (((cfg or {}).get("agents") or {}).get("defaults") or {}).get("model") or {}
        primary_model = str(primary.get("primary") or "").strip()
        if primary_model:
            fallback_model = primary_model if "/" in primary_model else f"ollama/{primary_model}"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 2277, exc_info=True)
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = base / "_repair_backups" / f"autopilot_{ts}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        out["backup_dir"] = str(backup_dir)

        jsonl_files = sorted(base.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[: max(1, int(max_files))]
        for p in jsonl_files:
            try:
                raw_lines = p.read_text(encoding="utf-8").splitlines()
            except Exception as e:
                out["errors"].append(f"read_failed:{p.name}:{type(e).__name__}")
                continue
            new_lines = []
            changed = False
            for ln in raw_lines:
                s = ln.strip()
                if not s:
                    new_lines.append(ln)
                    continue
                try:
                    obj = json.loads(s)
                except Exception:
                    new_lines.append(ln)
                    continue

                if obj.get("type") == "model_change":
                    if not obj.get("modelApi"):
                        obj["modelApi"] = "openai-responses"
                        out["changed_rows"] += 1
                        changed = True
                    if not obj.get("modelProvider"):
                        obj["modelProvider"] = "ollama"
                        out["changed_rows"] += 1
                        changed = True
                    if not obj.get("model"):
                        obj["model"] = fallback_model
                        out["changed_rows"] += 1
                        changed = True

                if obj.get("type") == "custom":
                    payload = obj.get("payload") or {}
                    if isinstance(payload, dict) and payload.get("type") in {"model-snapshot", "model_change"}:
                        data = payload.get("data")
                        if isinstance(data, dict):
                            if not data.get("modelApi"):
                                data["modelApi"] = "openai-responses"
                                out["changed_rows"] += 1
                                changed = True
                            if not data.get("modelProvider"):
                                data["modelProvider"] = "ollama"
                                out["changed_rows"] += 1
                                changed = True
                            if not data.get("model"):
                                data["model"] = fallback_model
                                out["changed_rows"] += 1
                                changed = True

                new_lines.append(json.dumps(obj, ensure_ascii=False))

            if changed:
                try:
                    (backup_dir / p.name).write_text("\n".join(raw_lines) + ("\n" if raw_lines else ""), encoding="utf-8")
                    p.write_text("\n".join(new_lines) + ("\n" if new_lines else ""), encoding="utf-8")
                    out["changed_files"] += 1
                except Exception as e:
                    out["errors"].append(f"write_failed:{p.name}:{type(e).__name__}")

        sess_file = base / "sessions.json"
        try:
            sess_raw = sess_file.read_text(encoding="utf-8")
            sess = json.loads(sess_raw)
            if isinstance(sess, dict):
                changed = False
                for k, v in sess.items():
                    if not isinstance(v, dict):
                        continue
                    if not v.get("modelApi"):
                        v["modelApi"] = "openai-responses"
                        changed = True
                        out["session_entries_patched"] += 1
                    if not v.get("modelProvider"):
                        v["modelProvider"] = "ollama"
                        changed = True
                        out["session_entries_patched"] += 1
                    if not v.get("model"):
                        v["model"] = fallback_model
                        changed = True
                        out["session_entries_patched"] += 1
                if changed:
                    (backup_dir / "sessions.json").write_text(sess_raw, encoding="utf-8")
                    sess_file.write_text(json.dumps(sess, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            out["errors"].append(f"sessions_json_failed:{type(e).__name__}")
    except Exception as e:
        out["ok"] = False
        out["errors"].append(f"{type(e).__name__}: {e}")

    if out["errors"]:
        out["ok"] = False
    return out


def _ensure_ollama_model_meta(models_node: Any, model_id: str) -> Tuple[Any, bool]:
    """
    Ensure OpenClaw ollama provider has metadata entry for model_id.
    Supports both list-style and dict-style config formats.
    """
    mid = str(model_id or "").strip()
    if not mid:
        return models_node, False

    default_meta = {
        "id": mid,
        "name": mid,
        "input": ["text"],
        "reasoning": False,
        "maxTokens": 4096,
        "contextWindow": 32768,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }

    if isinstance(models_node, list):
        for it in models_node:
            if not isinstance(it, dict):
                continue
            if str(it.get("id") or "").strip() == mid or str(it.get("name") or "").strip() == mid:
                if not it.get("id"):
                    it["id"] = mid
                    return models_node, True
                return models_node, False
        models_node.append(default_meta)
        return models_node, True

    if isinstance(models_node, dict):
        if mid not in models_node:
            models_node[mid] = default_meta
            return models_node, True
        cur = models_node.get(mid)
        if isinstance(cur, dict):
            changed = False
            if not cur.get("id"):
                cur["id"] = mid
                changed = True
            if not cur.get("name"):
                cur["name"] = mid
                changed = True
            if changed:
                models_node[mid] = cur
            return models_node, changed
        return models_node, False

    return [default_meta], True


def _openclaw_config_path() -> Path:
    return Path(
        os.environ.get(
            "MAGI_OPENCLAW_CONFIG_PATH",
            str(Path.home() / ".openclaw" / "openclaw.json"),
        )
    )


def _openclaw_auth_profiles_path() -> Path:
    return Path(
        os.environ.get(
            "MAGI_OPENCLAW_AUTH_PROFILES_PATH",
            str(Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"),
        )
    )


def _openclaw_codex_quota_state_path() -> Path:
    return Path(
        os.environ.get(
            "MAGI_OPENCLAW_CODEX_QUOTA_STATE_PATH",
            os.path.join(MAGI_ROOT_DIR, ".agent", "openclaw_codex_quota_state.json"),
        )
    )


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _ms_to_iso(ts_ms: int) -> str:
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(ts_ms / 1000.0).isoformat()
    except Exception:
        return ""


def _model_chain_from_node(model_node: Any) -> Tuple[str, list[str]]:
    if isinstance(model_node, str):
        primary = str(model_node or "").strip()
        if primary and "/" not in primary:
            primary = f"ollama/{primary}"
        return primary, []
    if isinstance(model_node, dict):
        primary = str(model_node.get("primary") or "").strip()
        if primary and "/" not in primary:
            primary = f"ollama/{primary}"
        fallbacks = []
        for raw in (model_node.get("fallbacks") or []):
            item = str(raw or "").strip()
            if not item:
                continue
            fallbacks.append(item if "/" in item else f"ollama/{item}")
        return primary, fallbacks
    return "", []


def _model_list_from_env(name: str, default_csv: str) -> list[str]:
    out = []
    raw = str(os.environ.get(name, default_csv) or default_csv)
    for item in raw.split(","):
        token = str(item or "").strip()
        if not token:
            continue
        out.append(token if "/" in token else f"ollama/{token}")
    return out


def _pick_profile_failure_reason(stats: dict) -> str:
    reason = str((stats or {}).get("disabledReason") or "").strip()
    if reason in {"billing", "rate_limit"}:
        return reason
    failure_counts = (stats or {}).get("failureCounts") or {}
    if not isinstance(failure_counts, dict):
        return ""
    if _safe_int(failure_counts.get("billing"), 0) > 0:
        return "billing"
    if _safe_int(failure_counts.get("rate_limit"), 0) > 0:
        return "rate_limit"
    return ""


def _load_openclaw_codex_usage_snapshot(now_ms: Optional[int] = None) -> Dict[str, Any]:
    now_ms = int(now_ms or int(time.time() * 1000))
    auth_path = _openclaw_auth_profiles_path()
    data = _load_json(auth_path) or {}
    profiles = data.get("profiles") if isinstance(data.get("profiles"), dict) else {}
    usage_stats = data.get("usageStats") if isinstance(data.get("usageStats"), dict) else {}

    entries = []
    for profile_id, profile in profiles.items():
        if not isinstance(profile, dict):
            continue
        if str(profile.get("provider") or "").strip() != "openai-codex":
            continue
        stats = usage_stats.get(profile_id) if isinstance(usage_stats.get(profile_id), dict) else {}
        cooldown_until = _safe_int(stats.get("cooldownUntil"), 0)
        disabled_until = _safe_int(stats.get("disabledUntil"), 0)
        unusable_until = max(cooldown_until, disabled_until)
        reason = _pick_profile_failure_reason(stats)
        entries.append(
            {
                "profile_id": profile_id,
                "reason": reason,
                "active": bool(unusable_until > now_ms and reason in {"billing", "rate_limit"}),
                "cooldown_until_ms": cooldown_until,
                "disabled_until_ms": disabled_until,
                "unusable_until_ms": unusable_until,
                "unusable_until_iso": _ms_to_iso(unusable_until),
                "last_failure_at_ms": _safe_int(stats.get("lastFailureAt"), 0),
                "last_failure_at_iso": _ms_to_iso(_safe_int(stats.get("lastFailureAt"), 0)),
                "last_used_ms": _safe_int(stats.get("lastUsed"), 0),
                "failure_counts": stats.get("failureCounts") if isinstance(stats.get("failureCounts"), dict) else {},
                "disabled_reason": str(stats.get("disabledReason") or "").strip(),
            }
        )

    def _reason_rank(item: dict) -> Tuple[int, int]:
        reason = str(item.get("reason") or "")
        severity = 2 if reason == "billing" else 1 if reason == "rate_limit" else 0
        return severity, _safe_int(item.get("unusable_until_ms"), 0)

    active_entries = [it for it in entries if it.get("active")]
    selected = max(active_entries, key=_reason_rank) if active_entries else None
    return {
        "ok": True,
        "auth_profiles_path": str(auth_path),
        "profiles": entries,
        "active": bool(selected),
        "active_reason": str((selected or {}).get("reason") or "").strip(),
        "active_until_ms": _safe_int((selected or {}).get("unusable_until_ms"), 0),
        "active_until_iso": str((selected or {}).get("unusable_until_iso") or ""),
        "last_failure_at_ms": max((_safe_int(it.get("last_failure_at_ms"), 0) for it in entries), default=0),
        "last_failure_at_iso": _ms_to_iso(max((_safe_int(it.get("last_failure_at_ms"), 0) for it in entries), default=0)),
    }


def _quota_hold_ms(reason: str) -> int:
    if reason == "billing":
        hours = _safe_int(os.environ.get("MAGI_OPENCLAW_CODEX_BILLING_HOLD_HOURS"), 72)
    else:
        hours = _safe_int(os.environ.get("MAGI_OPENCLAW_CODEX_RATE_LIMIT_HOLD_HOURS"), 24)
    return max(1, hours) * 3600 * 1000


def _openclaw_codex_quota_guard(current_primary: str, current_fallbacks: list[str]) -> Dict[str, Any]:
    state_path = _openclaw_codex_quota_state_path()
    now_ms = int(time.time() * 1000)
    out: Dict[str, Any] = {
        "ok": True,
        "state_path": str(state_path),
        "mode": "pass_through",
        "desired_primary": current_primary,
        "desired_fallbacks": list(current_fallbacks or []),
        "active_reason": "",
        "changed_state": False,
        "restored": False,
        "usage": {},
    }
    if not _env_on("MAGI_OPENCLAW_CODEX_QUOTA_FALLBACK_ENABLE", True):
        out["disabled"] = True
        return out

    local_primary = str(
        os.environ.get("MAGI_OPENCLAW_LOCAL_PRIMARY_MODEL", "omlx/TAIDE-12b-Chat-mlx-4bit")
        or "omlx/TAIDE-12b-Chat-mlx-4bit"
    ).strip()
    local_fallbacks = _model_list_from_env(
        "MAGI_OPENCLAW_LOCAL_FALLBACK_MODELS",
        "omlx/TAIDE-12b-Chat-mlx-4bit",
    )
    remote_primary_default = str(
        os.environ.get("MAGI_OPENCLAW_REMOTE_PRIMARY_MODEL", "openai-codex/gpt-5.4")
        or "openai-codex/gpt-5.4"
    ).strip()
    remote_fallbacks_default = _model_list_from_env(
        "MAGI_OPENCLAW_REMOTE_FALLBACK_MODELS",
        ",".join(local_fallbacks),
    )

    usage = _load_openclaw_codex_usage_snapshot(now_ms=now_ms)
    out["usage"] = usage
    state = _load_json(state_path) or {}
    state_before = json.dumps(state, ensure_ascii=False, sort_keys=True)

    remembered_remote_primary = str(state.get("remote_primary") or "").strip()
    if not remembered_remote_primary:
        remembered_remote_primary = current_primary if str(current_primary or "").startswith("openai-codex/") else remote_primary_default
    remembered_remote_fallbacks = [
        str(item or "").strip()
        for item in (state.get("remote_fallbacks") or remote_fallbacks_default)
        if str(item or "").strip()
    ]
    if not remembered_remote_fallbacks:
        remembered_remote_fallbacks = list(remote_fallbacks_default)

    auto_switched = bool(state.get("auto_switched"))
    active_reason = str(usage.get("active_reason") or "").strip()

    if usage.get("active") and active_reason in {"rate_limit", "billing"}:
        hold_until_ms = max(
            _safe_int(usage.get("active_until_ms"), 0),
            now_ms + _quota_hold_ms(active_reason),
            _safe_int(state.get("hold_until_ms"), 0) if auto_switched and str(state.get("reason") or "") == active_reason else 0,
        )
        if not auto_switched:
            state["entered_at_ms"] = now_ms
        state["mode"] = "local_fallback"
        state["auto_switched"] = True
        state["reason"] = active_reason
        state["last_seen_at_ms"] = now_ms
        state["hold_until_ms"] = hold_until_ms
        state["provider_unusable_until_ms"] = _safe_int(usage.get("active_until_ms"), 0)
        state["remote_primary"] = remembered_remote_primary
        state["remote_fallbacks"] = remembered_remote_fallbacks
        state["local_primary"] = local_primary
        state["local_fallbacks"] = local_fallbacks
        out["mode"] = "local_fallback"
        out["active_reason"] = active_reason
        out["desired_primary"] = local_primary
        out["desired_fallbacks"] = list(local_fallbacks)
        out["hold_until_ms"] = hold_until_ms
        out["hold_until_iso"] = _ms_to_iso(hold_until_ms)
    elif auto_switched and str(state.get("mode") or "") == "local_fallback":
        hold_until_ms = _safe_int(state.get("hold_until_ms"), 0)
        if hold_until_ms > now_ms:
            out["mode"] = "local_fallback"
            out["active_reason"] = str(state.get("reason") or "quota_hold")
            out["desired_primary"] = str(state.get("local_primary") or local_primary).strip() or local_primary
            saved_local_fallbacks = [
                str(item or "").strip()
                for item in (state.get("local_fallbacks") or local_fallbacks)
                if str(item or "").strip()
            ]
            out["desired_fallbacks"] = saved_local_fallbacks or list(local_fallbacks)
            out["hold_until_ms"] = hold_until_ms
            out["hold_until_iso"] = _ms_to_iso(hold_until_ms)
        else:
            state["mode"] = "remote"
            state["auto_switched"] = False
            state["reason"] = ""
            state["last_recovered_at_ms"] = now_ms
            state["hold_until_ms"] = 0
            state["provider_unusable_until_ms"] = 0
            out["mode"] = "remote"
            out["desired_primary"] = remembered_remote_primary or remote_primary_default
            out["desired_fallbacks"] = remembered_remote_fallbacks or list(remote_fallbacks_default)
            out["restored"] = True
    else:
        out["desired_primary"] = current_primary or remote_primary_default
        out["desired_fallbacks"] = list(current_fallbacks or (remote_fallbacks_default if out["desired_primary"].startswith("openai-codex/") else local_fallbacks))

    state_after = json.dumps(state, ensure_ascii=False, sort_keys=True)
    if state_after != state_before:
        _save_json(state_path, state)
        out["changed_state"] = True
    return out


def _openclaw_model_guard(auto_restart: bool = True) -> Dict[str, Any]:
    """
    Guard OpenClaw model runtime knobs to reduce "request aborted / all models failed":
    - timeout/context/concurrency sane defaults
    - ensure primary/fallback model metadata exists
    - restart gateway only when config changed
    """
    out: Dict[str, Any] = {
        "ok": True,
        "changed": False,
        "restarted": False,
        "checks": {},
        "errors": [],
    }

    cfg_path = _openclaw_config_path()
    if not cfg_path.exists():
        out["ok"] = False
        out["errors"].append("openclaw_config_missing")
        return out

    try:
        cfg_raw = cfg_path.read_text(encoding="utf-8")
        cfg = json.loads(cfg_raw)
    except Exception as e:
        out["ok"] = False
        out["errors"].append(f"openclaw_config_parse_failed:{type(e).__name__}: {e}")
        return out

    changed = False
    agents = cfg.setdefault("agents", {})
    defaults = agents.setdefault("defaults", {})

    def _set_if_diff(key: str, new_val: Any, old_val: Any) -> None:
        nonlocal changed
        if old_val != new_val:
            defaults[key] = new_val
            changed = True

    # 1) timeout
    min_timeout = max(180, int(os.environ.get("MAGI_OPENCLAW_MIN_TIMEOUT_SEC", "900") or "900"))
    old_timeout = defaults.get("timeoutSeconds")
    try:
        cur_timeout = int(old_timeout)
    except Exception:
        cur_timeout = 0
    _set_if_diff("timeoutSeconds", max(min_timeout, cur_timeout if cur_timeout > 0 else min_timeout), old_timeout)

    # 2) context tokens (keep practical value to avoid model mismatch)
    target_ctx = max(8000, int(os.environ.get("MAGI_OPENCLAW_CONTEXT_TOKENS", "16000") or "16000"))
    old_ctx = defaults.get("contextTokens")
    try:
        cur_ctx = int(old_ctx)
    except Exception:
        cur_ctx = 0
    if cur_ctx != target_ctx:
        defaults["contextTokens"] = target_ctx
        changed = True

    # 3) max concurrency (reduce head-of-line blocking / model thrash)
    old_mc = defaults.get("maxConcurrent")
    try:
        cur_mc = int(old_mc)
    except Exception:
        cur_mc = 0
    if cur_mc != 1:
        defaults["maxConcurrent"] = 1
        changed = True

    sub = defaults.setdefault("subagents", {})
    old_sub_mc = sub.get("maxConcurrent")
    try:
        cur_sub_mc = int(old_sub_mc)
    except Exception:
        cur_sub_mc = 0
    if cur_sub_mc != 2:
        sub["maxConcurrent"] = 2
        changed = True

    # 4) primary/fallback model chain with Codex quota-aware local fallback
    current_primary, current_fallbacks = _model_chain_from_node(defaults.get("model"))
    quota_guard = _openclaw_codex_quota_guard(current_primary=current_primary, current_fallbacks=current_fallbacks)
    out["quota_guard"] = quota_guard
    desired_primary = str(quota_guard.get("desired_primary") or current_primary or os.environ.get("MAGI_OPENCLAW_REMOTE_PRIMARY_MODEL", "openai-codex/gpt-5.4")).strip()
    desired_fallbacks = [
        str(item or "").strip()
        for item in (quota_guard.get("desired_fallbacks") or [])
        if str(item or "").strip()
    ]
    desired_model = {"primary": desired_primary, "fallbacks": desired_fallbacks}
    if defaults.get("model") != desired_model:
        defaults["model"] = desired_model
        changed = True

    needed_model_ids_by_provider: Dict[str, list[str]] = {}
    for full_model in [desired_primary] + desired_fallbacks:
        model_ref = str(full_model or "").strip()
        if not model_ref or "/" not in model_ref:
            continue
        provider_name, model_id = model_ref.split("/", 1)
        provider_name = provider_name.strip()
        model_id = model_id.strip()
        if provider_name not in LOCAL_OPENCLAW_PROVIDERS or not model_id:
            continue
        bucket = needed_model_ids_by_provider.setdefault(provider_name, [])
        if model_id not in bucket:
            bucket.append(model_id)

    # 5) ensure local-provider model metadata exists
    models = cfg.setdefault("models", {})
    providers = models.setdefault("providers", {})
    for provider_name, model_ids in needed_model_ids_by_provider.items():
        provider_cfg = providers.setdefault(provider_name, {})
        p_models = provider_cfg.get("models")
        if p_models is None:
            p_models = []
            provider_cfg["models"] = p_models
            changed = True
        for mid in model_ids:
            p_models, c = _ensure_ollama_model_meta(p_models, mid)
            if c:
                changed = True
        provider_cfg["models"] = p_models
        providers[provider_name] = provider_cfg
    models["providers"] = providers
    cfg["models"] = models

    out["checks"] = {
        "timeoutSeconds": defaults.get("timeoutSeconds"),
        "contextTokens": defaults.get("contextTokens"),
        "maxConcurrent": defaults.get("maxConcurrent"),
        "subagents.maxConcurrent": (defaults.get("subagents") or {}).get("maxConcurrent"),
        "model": defaults.get("model"),
        "needed_model_ids_by_provider": needed_model_ids_by_provider,
    }

    if changed:
        try:
            backup = cfg_path.with_suffix(f".json.bak.model_guard_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            backup.write_text(cfg_raw, encoding="utf-8")
            cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            out["changed"] = True
            out["backup"] = str(backup)
        except Exception as e:
            out["ok"] = False
            out["errors"].append(f"openclaw_config_write_failed:{type(e).__name__}: {e}")
            return out

        if auto_restart:
            r = _run_cmd(["openclaw", "gateway", "restart"], timeout_sec=90)
            out["restarted"] = bool(r.ok)
            out["restart_returncode"] = int(r.returncode)
            if not r.ok:
                out["ok"] = False
                out["errors"].append((r.stderr or r.stdout or "gateway_restart_failed").strip()[:300])

    return out


def _summarize_file_review_download(parsed: Optional[dict], max_groups: Optional[int] = None) -> Dict[str, Any]:
    if max_groups is None:
        try:
            max_groups = int(os.environ.get("MAGI_FILE_REVIEW_NOTIFY_MAX_GROUPS", "10") or "10")
        except Exception:
            max_groups = 10
    max_groups = max(1, min(int(max_groups), 30))
    """
    將 file-review-orchestrator download 的回傳轉成可讀摘要。
    優先顯示：當事人 + 法院案號；缺資料時 fallback 到檔案路徑推斷。
    """
    out: Dict[str, Any] = {"count": 0, "groups": [], "raw_message": ""}
    if not isinstance(parsed, dict):
        return out
    try:
        out["count"] = int(parsed.get("downloaded_count") or 0)
    except Exception:
        out["count"] = 0
    out["raw_message"] = str(parsed.get("message") or "").strip()
    if isinstance(parsed.get("archive_summary"), dict):
        out["archive_summary"] = parsed.get("archive_summary")

    def _norm(x: Any) -> str:
        return str(x or "").strip()

    groups: Dict[str, Dict[str, Any]] = {}
    items = parsed.get("items")
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            party = _norm(it.get("party"))
            court_case_no = _norm(it.get("court_case_no"))
            folder = _norm(it.get("folder"))
            file_name = _norm(it.get("file") or it.get("filename"))
            label_parts = []
            if party:
                label_parts.append(party)
            if court_case_no:
                label_parts.append(court_case_no)
            if not label_parts and folder:
                label_parts.append(Path(folder).name)
            label = "｜".join(label_parts) if label_parts else "（未能判斷案件）"
            g = groups.setdefault(label, {"label": label, "files": [], "folder": folder})
            if file_name:
                g["files"].append(file_name)

    if not groups:
        files = parsed.get("files")
        if isinstance(files, list):
            for f in files:
                fp = _norm(f)
                if not fp:
                    continue
                p = Path(fp)
                case_label = ""
                for part in reversed(p.parts):
                    if re.search(r"\d{4}-\d{4}", part):
                        case_label = part
                        break
                if not case_label:
                    case_label = p.parent.name or "（未能判斷案件）"
                g = groups.setdefault(case_label, {"label": case_label, "files": [], "folder": str(p.parent)})
                g["files"].append(p.name)

    ordered = sorted(groups.values(), key=lambda x: x.get("label", ""))[: max_groups]
    out["groups"] = ordered
    return out


def _resolve_slo_guard_script() -> str:
    for p in SLO_GUARD_SCRIPT_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _top_unstable_steps(step_stats: Any, top_n: int = 3) -> list:
    rows = []
    if not isinstance(step_stats, list):
        return rows
    for r in step_stats:
        if not isinstance(r, dict):
            continue
        try:
            runs = int(r.get("runs") or 0)
        except Exception:
            runs = 0
        try:
            fails = int(r.get("fails") or 0)
        except Exception:
            fails = 0
        try:
            timeouts = int(r.get("timeouts") or 0)
        except Exception:
            timeouts = 0
        try:
            success_rate = float(r.get("success_rate") or 0.0)
        except Exception:
            success_rate = 0.0
        if runs < 1:
            continue
        if fails <= 0 and timeouts <= 0:
            continue
        rows.append(
            {
                "step": str(r.get("step") or ""),
                "runs": runs,
                "fails": fails,
                "timeouts": timeouts,
                "success_rate": success_rate,
            }
        )
    rows.sort(key=lambda x: (x.get("fails", 0), x.get("timeouts", 0), -x.get("success_rate", 0.0)), reverse=True)
    return rows[: max(1, min(int(top_n), 6))]


def _format_slo_notify_lines(parsed: Any, *, max_steps: int = 3) -> list:
    if not isinstance(parsed, dict):
        return []
    lines = []
    try:
        hours = int(parsed.get("hours") or 72)
    except Exception:
        hours = 72
    try:
        blocked_runs = int(parsed.get("blocked_runs") or 0)
    except Exception:
        blocked_runs = 0
    try:
        rec_count = len(parsed.get("recommended_env") or {})
    except Exception:
        rec_count = 0
    lines.append(f"- 穩定度（{hours}h）：blocked={blocked_runs}，建議調整={rec_count}")
    unstable = _top_unstable_steps(parsed.get("step_stats"), top_n=max_steps)
    if unstable:
        for u in unstable:
            sr = int(round(float(u.get("success_rate", 0.0)) * 100))
            lines.append(
                f"  • {u.get('step')}（成功率 {sr}% / fail {u.get('fails', 0)} / timeout {u.get('timeouts', 0)}）"
            )
    reasons = parsed.get("reasons") if isinstance(parsed.get("reasons"), list) else []
    if reasons:
        lines.append(f"  • 建議：{str(reasons[0])[:160]}")
    return lines


def _format_comm_notify_lines(parsed: Any) -> list:
    if not isinstance(parsed, dict):
        return []
    lines = []
    probe = parsed.get("openclaw_probe") if isinstance(parsed.get("openclaw_probe"), dict) else {}
    discord_ok = bool(probe.get("discord_ok"))
    line_ok = bool(probe.get("line_ok"))
    webhook = parsed.get("line_local_webhook") if isinstance(parsed.get("line_local_webhook"), dict) else {}
    line_local_ok = bool(webhook.get("ok"))
    age = parsed.get("line_last_callback_age_sec")
    age_text = "unknown"
    try:
        age_text = f"{int(age)}s"
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3018, exc_info=True)
    lines.append(
        "- 通訊健康："
        f"Discord={'OK' if discord_ok else 'FAIL'} / "
        f"LINE-probe={'OK' if line_ok else 'FAIL'} / "
        f"LINE-webhook={'OK' if line_local_ok else 'FAIL'} / "
        f"LINE-callback-age={age_text}"
    )
    return lines


def _format_big_brain_notify_lines(parsed: Any) -> list:
    if not isinstance(parsed, dict):
        return []
    lines = []
    status = str(parsed.get("status") or "unknown")
    mode_before = str(parsed.get("mode_before") or "unknown")
    mode_after = str(parsed.get("mode_after") or "unknown")
    probe = parsed.get("inference_probe_retry") if isinstance(parsed.get("inference_probe_retry"), dict) else parsed.get("inference_probe")
    probe = probe if isinstance(probe, dict) else {}
    route = str(probe.get("route") or "n/a")
    model = str(probe.get("model") or "n/a")
    try:
        latency_ms = int(probe.get("latency_ms") or 0)
    except Exception:
        latency_ms = 0
    lines.append(
        f"- Big Brain：{status}（mode {mode_before} -> {mode_after} / route={route} / model={model} / latency={latency_ms}ms）"
    )
    if status != "distributed_healthy":
        err = str(parsed.get("error") or probe.get("error") or "").strip()
        if err:
            lines.append(f"  • 原因：{err[:180]}")
    return lines


def _slo_non_baseline_env_count(parsed: Any) -> int:
    if not isinstance(parsed, dict):
        return 0
    env_map = parsed.get("recommended_env") if isinstance(parsed.get("recommended_env"), dict) else {}
    baseline = {"MAGI_NO_DELETE", "MAGI_PREFER_LOCAL_DB"}
    n = 0
    for k in env_map.keys():
        if str(k or "").strip() and str(k or "").strip() not in baseline:
            n += 1
    return n


def _slo_has_critical_unstable(parsed: Any) -> bool:
    if not isinstance(parsed, dict):
        return False
    for u in _top_unstable_steps(parsed.get("step_stats"), top_n=6):
        try:
            runs = int(u.get("runs") or 0)
            fails = int(u.get("fails") or 0)
            timeouts = int(u.get("timeouts") or 0)
            success_rate = float(u.get("success_rate") or 0.0)
        except Exception:
            continue
        if runs >= 3 and (fails >= 2 or timeouts >= 2 or success_rate < 0.70):
            return True
    return False


def _should_auto_apply_slo_degrade(parsed_24h: Any, parsed_72h: Any, *, min_blocked_24h: int = 1) -> bool:
    p24 = parsed_24h if isinstance(parsed_24h, dict) else {}
    p72 = parsed_72h if isinstance(parsed_72h, dict) else {}
    nb24 = _slo_non_baseline_env_count(p24)
    nb72 = _slo_non_baseline_env_count(p72)
    if max(nb24, nb72) <= 0:
        return False
    try:
        blocked24 = int(p24.get("blocked_runs") or 0)
    except Exception:
        blocked24 = 0
    if blocked24 >= max(1, int(min_blocked_24h)):
        return True
    if _slo_has_critical_unstable(p24):
        return True
    return False


def _format_slo_apply_notify_lines(parsed: Any) -> list:
    if not isinstance(parsed, dict):
        return []
    lines = []
    applied = bool(parsed.get("applied"))
    lines.append(f"- 自動降級套用：{'已套用' if applied else '未套用'}")
    env_map = parsed.get("recommended_env") if isinstance(parsed.get("recommended_env"), dict) else {}
    baseline = {"MAGI_NO_DELETE", "MAGI_PREFER_LOCAL_DB"}
    tuned = [str(k) for k in env_map.keys() if str(k) not in baseline]
    if tuned:
        lines.append(f"  • 調整參數：{', '.join(tuned[:6])}")
    return lines


def _write_report(run_dir: str, report: dict) -> Tuple[str, str]:
    json_path = os.path.join(run_dir, "report.json")
    txt_path = os.path.join(run_dir, "report.txt")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    with open(txt_path, "w", encoding="utf-8") as f:
        # User-facing text report: keep it human-readable; DO NOT dump JSON here.
        summary = (report.get("summary") or "").strip()
        task = (report.get("task") or "").strip()
        ts = (report.get("ts") or "").strip()
        ok = bool(report.get("ok"))

        lines = []
        if summary:
            lines.append(summary)
        else:
            lines.append("完成" if ok else "完成但有錯誤")
        if ts:
            lines.append(f"時間：{ts}")
        if task:
            lines.append(f"任務：{task}")

        details = report.get("details") or {}
        steps = (details.get("steps") or {}) if isinstance(details, dict) else {}
        blockers = (details.get("blockers") or []) if isinstance(details, dict) else []
        runtime_lines = _format_openclaw_runtime_lines(
            (details.get("openclaw_runtime_mode") or {}) if isinstance(details, dict) else {}
        )
        judicial_api_lines = _format_judicial_api_pipeline_lines(
            (details.get("judicial_api_pipeline") or {}) if isinstance(details, dict) else {}
        )
        fr_dl_detail: Dict[str, Any] = {}

        if runtime_lines:
            lines.append("")
            lines.extend(runtime_lines)
        if judicial_api_lines:
            lines.append("")
            lines.extend(judicial_api_lines)

        if steps:
            lines.append("")
            lines.append("步驟摘要：")
            for name, step in steps.items():
                if not isinstance(step, dict):
                    lines.append(f"- {name}: (no details)")
                    continue
                step_ok = bool(step.get("ok"))
                line = f"- {name}: {'OK' if step_ok else 'FAIL'}"
                # Best-effort add metrics
                parsed = step.get("parsed") if isinstance(step.get("parsed"), dict) else {}
                if name == "laf" and isinstance(step, dict):
                    line += f" (cases={step.get('cases', 0)}, processed={step.get('processed', 0)}, errors={len(step.get('errors') or [])})"
                elif name == "laf_condition_draft" and isinstance(step, dict):
                    line += f" (scanned={step.get('scanned', 0)}, processed={step.get('processed', 0)}, errors={len(step.get('errors') or [])})"
                elif name == "big_brain_health" and isinstance(parsed, dict):
                    p = parsed if isinstance(parsed, dict) else {}
                    status = str(p.get("status") or "unknown")
                    mb = str(p.get("mode_before") or "?")
                    ma = str(p.get("mode_after") or "?")
                    probe = p.get("inference_probe_retry") if isinstance(p.get("inference_probe_retry"), dict) else p.get("inference_probe")
                    probe = probe if isinstance(probe, dict) else {}
                    route = str(probe.get("route") or "n/a")
                    model = str(probe.get("model") or "n/a")
                    line += f" (status={status}, mode={mb}->{ma}, route={route}, model={model})"
                elif name in {"file_review_preview", "file_review_check", "file_review_email_scan"} and isinstance(parsed, dict):
                    line += f" (count={parsed.get('count', 0)})"
                elif name == "file_review_downloadable_probe" and isinstance(parsed, dict):
                    line += f" (notifications={parsed.get('count', 0)}, downloadable={parsed.get('downloadable_count', 0)})"
                elif name == "osc_scan_cases" and isinstance(parsed, dict):
                    total = parsed.get("total") or {}
                    if isinstance(total, dict):
                        line += f" (scanned={total.get('scanned', 0)}, queued={total.get('queued', 0)})"
                elif name == "file_review_download" and isinstance(parsed, dict):
                    fr_dl_detail = _summarize_file_review_download(parsed)
                    line += f" (downloaded={fr_dl_detail.get('count', 0)})"
                elif str(name).startswith("slo_guard_snapshot") and isinstance(parsed, dict):
                    line += (
                        f" (hours={parsed.get('hours', 72)}, "
                        f"blocked_runs={parsed.get('blocked_runs', 0)}, "
                        f"recommended_env={len(parsed.get('recommended_env') or {})})"
                    )
                elif name == "transcript_sync" and isinstance(step, dict):
                    # Keep compact; details in report.json
                    pass
                lines.append(line)

        if fr_dl_detail.get("groups"):
            lines.append("")
            lines.append("閱卷下載明細：")
            for g in fr_dl_detail.get("groups", []):
                if not isinstance(g, dict):
                    continue
                label = str(g.get("label") or "（未能判斷案件）")
                files = g.get("files") if isinstance(g.get("files"), list) else []
                lines.append(f"- {label}（{len(files)} 份）")
                if files:
                    lines.append(f"  例：{files[0]}")
            a = fr_dl_detail.get("archive_summary") if isinstance(fr_dl_detail.get("archive_summary"), dict) else {}
            if a:
                unresolved = int(a.get("unresolved_count") or 0)
                if unresolved > 0:
                    lines.append(f"- ⚠️ 待歸檔：{unresolved} 份（請檢查閱卷下載 _待歸檔）")
        elif fr_dl_detail.get("count", 0) <= 0:
            lines.append("")
            lines.append("閱卷下載明細：本輪無可下載檔案")

        if blockers:
            lines.append("")
            lines.append("卡住項目：")
            for b in blockers:
                lines.append(f"- {b}")

        lines.append("")
        lines.append(f"詳細報告（JSON）：{json_path}")

        f.write("\n".join(lines).rstrip() + "\n")
    return json_path, txt_path


def _load_primary_config_path() -> str:
    return str(get_config_path("config.json"))


def _laf_one_shot(max_results: int = 15, general_max: int = 15) -> Dict[str, Any]:
    """
    一次性法扶流程：
    - 掃描未讀法扶信件（解析主旨）
    - 掃描一般信件規則（專員來信等）
    - 需要下載者：嘗試登入法扶系統下載 + 建案/歸檔
    """
    out: Dict[str, Any] = {"ok": True, "cases": 0, "processed": 0, "general": 0, "errors": []}
    try:
        max_results = int(os.environ.get("MAGI_LAF_EMAIL_MAX_RESULTS", str(max_results)) or str(max_results))
    except Exception:
        max_results = int(max_results)
    try:
        general_max = int(os.environ.get("MAGI_LAF_GENERAL_EMAIL_MAX_RESULTS", str(general_max)) or str(general_max))
    except Exception:
        general_max = int(general_max)
    max_results = max(10, min(max_results, 200))
    general_max = max(10, min(general_max, 200))
    try:
        ensure_orch_on_sys_path()
        import laf_automation_v2 as laf

        cfg_path = _load_primary_config_path()
        cfg = _load_json(cfg_path)

        # Try to use OSC DatabaseManager if available (for case creation / folder resolution)
        db_manager = None
        try:
            import laf_orchestrator as lo
            db_manager = lo._get_db_manager()
        except Exception:
            db_manager = None

        manager = laf.LAFAutomationManager(config=cfg, db_manager=db_manager, discord_notifier=None, log_callback=lambda m: None)
        manager.setup()

        if not manager.gmail_monitor:
            return {"ok": False, "error": "gmail_monitor_not_ready"}

        # Authenticate (may require interactive OAuth if token is invalid)
        try:
            ok = manager.gmail_monitor.authenticate()
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            out["ok"] = False
            out["error"] = msg
            return out

        if not ok:
            return {"ok": False, "error": "gmail_auth_failed"}

        cases = manager.gmail_monitor.check_emails(max_results=max_results)
        out["cases"] = len(cases or [])
        for ci in (cases or []):
            try:
                # Queue decision logic + attachment handling
                manager._on_new_case(ci)
            except Exception as e:
                out["errors"].append(f"on_new_case: {e}")

        # Process queued download tasks immediately (one-shot)
        processed = 0
        try:
            while True:
                try:
                    ci = manager.task_queue.get_nowait()
                except Exception:
                    break
                try:
                    res = manager._auto_process(ci)
                    if isinstance(res, dict) and not res.get("success", True):
                        out["errors"].append(f"auto_process: {res.get('error','')}")
                except Exception as e:
                    out["errors"].append(f"auto_process_exc: {e}")
                finally:
                    try:
                        manager.task_queue.task_done()
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3316, exc_info=True)
                processed += 1
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3319, exc_info=True)
        out["processed"] = processed

        # General email rules (staff emails etc)
        # 若法扶啟用，強制啟用一般信件監控，避免「法扶專員額外附件」漏抓。
        laf_cfg = cfg.get("laf", {}) or {}
        general_cfg = cfg.get("general_email_monitor", {}) or {}
        general_enabled = bool(general_cfg.get("enabled", False))
        if bool(laf_cfg.get("enabled", True)):
            general_enabled = True

        if general_enabled:
            rules = list(general_cfg.get("rules", []) or [])
            # 內建補抓規則：不依賴 is:unread，補抓近期法扶專員附件。
            catchup_query = "from:laf.org.tw has:attachment -from:laf.server newer_than:180d"
            rules.append(
                {
                    "name": "法扶專員附件補抓",
                    "query": catchup_query,
                    "target_subfolder": "法扶專員附件",
                }
            )
            # 去重（同 query 保留首個）
            uniq_rules = []
            seen_q = set()
            for r in rules:
                q = (r.get("query", "") if isinstance(r, dict) else "").strip()
                if not q:
                    continue
                if q in seen_q:
                    continue
                seen_q.add(q)
                uniq_rules.append(r)
            try:
                effective_general_max = max(int(general_max or 0), int(os.environ.get("MAGI_LAF_GENERAL_MAX", "30") or "30"))
                general = manager.gmail_monitor.check_general_emails(uniq_rules, max_results=effective_general_max)
            except Exception as e:
                out["errors"].append(f"general_scan: {e}")
                general = []
            out["general"] = len(general or [])
            # LAFAutomationManager.setup() 已把 general_callback 綁到 _on_general_email；
            # check_general_emails() 內部會觸發，不在此重複處理以避免重複下載。

        # Dedupe folders by laf marker (best-effort; non-blocking)
        try:
            if getattr(manager, "case_creator", None):
                dd = manager.case_creator.dedupe_case_folders_by_laf_marker(max_scan_per_type=2500)
                out["dedupe"] = dd
        except Exception as e:
            out["errors"].append(f"dedupe: {e}")

        out["ok"] = (len(out["errors"]) == 0)
        return out

    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _laf_condition_draft_one_shot(max_cases: int = 0) -> Dict[str, Any]:
    """
    自動觸發法扶 WF5（二階段）暫存：
    - 由 laf_orchestrator 依 DB + 檔案條件挑選候選案件
    - 僅暫存，不送出
    - max_cases=0 表示不限（有幾件做幾件）
    """
    out: Dict[str, Any] = {"ok": True, "processed": 0, "scanned": 0, "items": [], "errors": []}
    try:
        ensure_orch_on_sys_path()
        import laf_orchestrator as lo  # type: ignore

        orchestrator = lo.LAFOrchestrator(dry_run=False)
        result = orchestrator.run_condition_drafts(max_cases=int(max_cases or 2))
        if isinstance(result, dict):
            out.update(result)
            out["ok"] = bool(result.get("ok", True))
        else:
            out["ok"] = False
            out["errors"].append("invalid_return_from_run_condition_drafts")
    except Exception as e:
        out["ok"] = False
        out["errors"].append(f"{type(e).__name__}: {e}")
    return out


def _is_judicial_api_service_window(now_dt: Optional[datetime] = None) -> bool:
    """
    True when local hour is within official Judicial API service window.
    Controlled by env:
      - JUDICIAL_API_WINDOW_START_HOUR (default 0)
      - JUDICIAL_API_WINDOW_END_HOUR   (default 6)
    """
    if now_dt is None:
        now_dt = datetime.now()
    try:
        s = int((os.environ.get("JUDICIAL_API_WINDOW_START_HOUR", "0") or "0").strip() or "0") % 24
    except Exception:
        s = 0
    try:
        e = int((os.environ.get("JUDICIAL_API_WINDOW_END_HOUR", "6") or "6").strip() or "6") % 24
    except Exception:
        e = 6
    h = int(now_dt.hour) % 24
    if s == e:
        return True
    if s < e:
        return s <= h < e
    return (h >= s) or (h < e)


def run_tick(run_dir: str, *, emit_step_events: bool = True) -> Dict[str, Any]:
    # Default safety: do not delete any data (especially Synology Drive).
    os.environ.setdefault("MAGI_NO_DELETE", "1")
    # DB safety: block destructive SQL in headless/automation paths.
    os.environ.setdefault("MAGI_DB_NO_DELETE", "1")
    # DB strategy: prefer Keeper/main DB by default; fallback logic lives in each module.
    os.environ.setdefault("MAGI_PREFER_LOCAL_DB", "0")
    # Schema guard: detect accidental re-hardening (chk_nb_*) that can break OSC/GUI flows.
    os.environ.setdefault("MAGI_DB_SCHEMA_GUARD_ENABLE", "1")
    # User asked to pause Apple AI usage; prefer non-Apple PDF text extraction.
    os.environ.setdefault("MAGI_PDF_TEXT_ENGINE", "pymupdf")
    os.environ.setdefault("MAGI_DISABLE_APPLE_AI", "1")
    # 系統告警預設走 TG，避免 LINE 頻率上限干擾日常通知。
    os.environ.setdefault("MAGI_SYSTEM_NOTIFY_CHANNEL", "telegram")
    os.environ.setdefault("MAGI_SYSTEM_NOTIFY_LINE_FALLBACK", "0")
    # 筆錄調閱站（ezlawyer）夜間/排程不應要求驗證碼人工輸入
    os.environ.setdefault("MAGI_EZLAWYER_SOLVE_CAPTCHA", "0")
    os.environ.setdefault("MAGI_EZLAWYER_ASSUME_CAPTCHA_REQUIRED", "0")
    os.environ.setdefault("MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK", "0")
    # 夜間/排程避免呼叫 CASPER Tools 解析筆錄（/collab/chat 可能在忙或未啟動，會拖慢整輪）。
    os.environ.setdefault("MAGI_RECORD_PARSE_CASPER_ASSIST", "0")
    # Big Brain health probe & self-heal defaults
    os.environ.setdefault("MAGI_BIG_BRAIN_HEALTH_ENABLE", "1")
    os.environ.setdefault("MAGI_BIG_BRAIN_AUTO_HEAL", "1")
    os.environ.setdefault("MAGI_BIG_BRAIN_AUTO_FALLBACK_LOCAL", "1")
    # Tick 優先快速可用，不做超長修復循環，避免整輪卡住。
    os.environ.setdefault("MAGI_BIG_BRAIN_PROBE_TIMEOUT_SEC", "70")
    os.environ.setdefault("MAGI_BIG_BRAIN_PROBE_TIMEOUT_QUICK_SEC", "28")
    os.environ.setdefault("MAGI_BIG_BRAIN_PROBE_TIMEOUT_LONG_SEC", "80")
    os.environ.setdefault("MAGI_BIG_BRAIN_LOADING_RESET_WAIT_SEC", "25")
    os.environ.setdefault("MAGI_BIG_BRAIN_LOADING_GRACE_SEC", "600")
    os.environ.setdefault("MAGI_BIG_BRAIN_REMOTE_REPAIR", "1")
    # 預設不把分散式視為硬依賴，避免阻擋任務完成。
    os.environ.setdefault("MAGI_BIG_BRAIN_REQUIRE_DISTRIBUTED", "0")
    # 預設允許非主模型降級，重點是任務可完成與回覆不中斷。
    os.environ.setdefault("MAGI_BIG_BRAIN_REQUIRE_MAIN_MODEL", "0")
    # 司法院官方 API：夜間拉取、白天整理（由 tick/ nighty 分工）
    os.environ.setdefault("MAGI_ENABLE_JUDICIAL_API_DAY_PROCESS", "1")
    os.environ.setdefault("MAGI_ENABLE_JUDICIAL_API_NIGHT_PULL", "1")
    # Tick 補強：除了信箱掃描，也要實際到閱卷網站檢查可下載並自動下載。
    os.environ.setdefault("MAGI_ENABLE_FILE_REVIEW_SITE_CHECK_TICK", "1")
    os.environ.setdefault("MAGI_TICK_FILE_REVIEW_SITE_TIMEOUT_SEC", "900")
    # Tick 補強：每日筆錄檢查（db_probe -> sync）。
    os.environ.setdefault("MAGI_ENABLE_TRANSCRIPT_CHECK_TICK", "1")
    os.environ.setdefault("MAGI_TICK_TRANSCRIPT_DB_PROBE_TIMEOUT_SEC", "120")
    os.environ.setdefault("MAGI_TICK_TRANSCRIPT_SYNC_TIMEOUT_SEC", "1800")
    os.environ.setdefault("MAGI_TRANSCRIPT_CAPTCHA_COOLDOWN_ENABLE", "1")
    os.environ.setdefault("MAGI_TRANSCRIPT_CAPTCHA_COOLDOWN_MINUTES", "180")
    os.environ.setdefault("MAGI_TICK_OSC_QUEUE_FLUSH_TIMEOUT_SEC", "120")
    os.environ.setdefault("MAGI_TICK_SCAN_FOLDER_ASYNC", "1")
    # OpenClaw 配置守門：卡住時自動校正 timeout/context/concurrency 並重啟 gateway
    os.environ.setdefault("MAGI_OPENCLAW_MODEL_GUARD_RESTART", "1")
    # LAF 深度二次抽取（舊案件文件/信件附件/報表文字）
    os.environ.setdefault("MAGI_LAF_DEEP_EXTRACT_ENABLE", "1")
    os.environ.setdefault("MAGI_LAF_DEEP_EXTRACT_MAX_CLIENTS", "8")
    os.environ.setdefault("MAGI_LAF_DEEP_EXTRACT_TIMEOUT_SEC", "240")
    # Light mode: bounded smoke tick for fast health verification.
    tick_light_mode = _env_on("MAGI_TICK_LIGHT_MODE", False)
    if tick_light_mode:
        # Explicitly override tick defaults to keep runtime bounded.
        os.environ["MAGI_TICK_OPENCLAW_SESSION_SELFHEAL_ENABLE"] = "0"
        os.environ["MAGI_TICK_OPENCLAW_MODEL_GUARD_ENABLE"] = "0"
        os.environ["MAGI_TICK_COMM_HEALTH_ENABLE"] = "0"
        os.environ["MAGI_ENABLE_TRANSCRIPT_CHECK_TICK"] = "0"
        os.environ["MAGI_ENABLE_SCAN_FOLDER"] = "0"
        os.environ["MAGI_TICK_OSC_QUEUE_FLUSH_TIMEOUT_SEC"] = "45"

    def maybe_block(step: str, err: str) -> None:
        t = (err or "").lower()
        # transcript sync captcha is recoverable and should not fail the whole tick/nightly loop.
        if step in {"transcript_sync", "transcript_sync_retry"} and ("captcha" in t or "驗證碼" in (err or "")):
            results.setdefault("warnings", []).append(f"{step}: captcha")
            return
        # 會需要人工互動的常見關鍵字（OAuth/驗證碼/登入）
        human_keywords = [
            "invalid_grant",
            "captcha",
            "驗證碼",
            "login_failed",
            "登入失敗",
            "authorization",
            "oauth",
            "insufficientpermissions",
            "insufficient authentication scopes",
            "forbidden",
            "unauthorized",
            "403",
            "401",
        ]
        if any(k in t for k in human_keywords):
            results["blocked"] = True
            kind = ""
            if "invalid_grant" in t:
                kind = "invalid_grant"
            elif "need_interactive_oauth" in t:
                kind = "reauth_required"
            elif "insufficientpermissions" in t or "insufficient authentication scopes" in t:
                kind = "insufficientPermissions"
            elif "captcha" in t or "驗證碼" in err:
                kind = "captcha"
            elif "login_failed" in t or "登入失敗" in err:
                kind = "login_failed"
            elif "403" in t or "forbidden" in t:
                kind = "http_403"
            elif "401" in t or "unauthorized" in t:
                kind = "http_401"
            else:
                kind = "needs_human"
            results["blockers"].append(f"{step}: {kind}")

    results: Dict[str, Any] = {"ok": True, "steps": {}, "blocked": False, "blockers": []}
    if _env_on("MAGI_TICK_OPENCLAW_AUTH_GUARD_ENABLE", True):
        try:
            auth_guard = _openclaw_auth_mode_guard()
        except Exception as e:
            auth_guard = {"ok": False, "status": "guard_exception", "error": f"{type(e).__name__}: {e}"}
        results["steps"]["openclaw_auth_guard"] = {
            "ok": bool(auth_guard.get("ok", False)),
            "parsed": auth_guard,
        }
        if not auth_guard.get("ok", False):
            status = str(auth_guard.get("status") or "unknown").strip()
            results["blocked"] = True
            results["blockers"].append(
                f"OpenClaw auth guard：目前為 {status}，夜間任務已停止以避免走到可能計費的 API 路徑"
            )
            results.setdefault("notes", []).append(
                "允許狀態只有 SAFE_OAUTH_ONLY 或 SAFE_LOCAL_ONLY；其他狀態一律先停下。"
            )
            results["ok"] = False
            if emit_step_events and (not tick_light_mode):
                _remember_step_events("tick", run_dir, results.get("steps") or {})
            return results
    else:
        results["steps"]["openclaw_auth_guard"] = {
            "ok": True,
            "skipped": True,
            "reason": "MAGI_TICK_OPENCLAW_AUTH_GUARD_ENABLE=0",
        }

    # DB strategy guardrail: remote DB first when reachable, local fallback only on real outage.
    db_pref = _set_db_preference_by_reachability()
    results["steps"]["db_preference_probe"] = {
        "ok": True,
        "parsed": db_pref,
    }
    if _env_on("MAGI_DB_SCHEMA_GUARD_ENABLE", True):
        try:
            schema_guard = _db_schema_chk_nb_guard()
        except Exception as e:
            schema_guard = {"ok": False, "message": f"{type(e).__name__}: {e}", "targets": []}
        results["steps"]["db_schema_guard"] = {
            "ok": bool(schema_guard.get("ok", False)),
            "parsed": schema_guard,
        }
        if schema_guard.get("has_chk_nb", False):
            results["blocked"] = True
            results["blockers"].append(
                f"DB schema guard：偵測到 chk_nb_* 約束回灌（{schema_guard.get('message','')}）"
            )
        elif not schema_guard.get("ok", True):
            maybe_block("db_schema_guard", str(schema_guard.get("message", "")))

    # -1) OpenClaw session self-heal（修補 modelApi 缺漏，避免 TG/LINE 偶發不回）
    if _env_on("MAGI_TICK_OPENCLAW_SESSION_SELFHEAL_ENABLE", True):
        try:
            sh = _openclaw_session_selfheal(max_files=int(os.environ.get("MAGI_OPENCLAW_SESSION_HEAL_MAX_FILES", "80") or "80"))
        except Exception as e:
            sh = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        results["steps"]["openclaw_session_selfheal"] = sh
        if not sh.get("ok", True):
            maybe_block("openclaw_session_selfheal", str(sh.get("error", "")) or "selfheal_failed")
    else:
        results["steps"]["openclaw_session_selfheal"] = {
            "ok": True,
            "skipped": True,
            "reason": "MAGI_TICK_OPENCLAW_SESSION_SELFHEAL_ENABLE=0",
        }

    # -0.5) OpenClaw model guard（避免 context/timeout/並發配置導致任務卡住）
    if _env_on("MAGI_TICK_OPENCLAW_MODEL_GUARD_ENABLE", True):
        try:
            mg = _openclaw_model_guard(auto_restart=_env_on("MAGI_OPENCLAW_MODEL_GUARD_RESTART", True))
        except Exception as e:
            mg = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        results["steps"]["openclaw_model_guard"] = mg
        if not mg.get("ok", True):
            maybe_block("openclaw_model_guard", str(mg.get("error", "")) or "model_guard_failed")
    else:
        results["steps"]["openclaw_model_guard"] = {
            "ok": True,
            "skipped": True,
            "reason": "MAGI_TICK_OPENCLAW_MODEL_GUARD_ENABLE=0",
        }

    # 0) 通訊健康自檢（LINE/Discord）
    if _env_on("MAGI_TICK_COMM_HEALTH_ENABLE", True):
        try:
            comm = _comm_health_self_test()
        except Exception as e:
            comm = {"ok": False, "errors": [f"{type(e).__name__}: {e}"], "warnings": []}
        results["steps"]["comm_health"] = {
            "ok": bool(comm.get("ok", False)),
            "parsed": comm,
        }
        if not comm.get("ok", False):
            for e in (comm.get("errors") or []):
                maybe_block("comm_health", str(e))
    else:
        results["steps"]["comm_health"] = {
            "ok": True,
            "skipped": True,
            "reason": "MAGI_TICK_COMM_HEALTH_ENABLE=0",
        }

    # 0.5) Big Brain health probe（實際推理探測 + 自修復 + 必要時切換）
    if _env_on("MAGI_BIG_BRAIN_HEALTH_ENABLE", True):
        try:
            bb = _big_brain_health_probe()
        except Exception as e:
            bb = {
                "ok": False,
                "status": "probe_exception",
                "error": f"{type(e).__name__}: {e}",
                "mode_before": "",
                "mode_after": "",
            }
        results["steps"]["big_brain_health"] = {
            "ok": bool(bb.get("ok", False)),
            "parsed": bb,
        }
        if not bb.get("ok", False):
            status = str(bb.get("status") or "").strip()
            if status == "degraded_local":
                results.setdefault("warnings", []).append("big_brain: degraded_local")
                results.setdefault("notes", []).append("Big Brain 分散式推理不可用，已自動降級 local（不阻斷）")
            elif status == "distributed_degraded_model":
                target_model = str(bb.get("main_model_target") or os.environ.get("MAGI_MAIN_MODEL", "")).strip()
                results.setdefault("warnings", []).append("big_brain: distributed_degraded_model")
                results.setdefault("notes", []).append(
                    f"Big Brain 主模型尚未就緒（目前非 {target_model}），已使用降級路徑（不阻斷）"
                )
            else:
                results["blocked"] = True
                results["blockers"].append("Big Brain 健康探測失敗（分散式與備援皆不穩）")

    # 0.8) 司法院 API 夜間拉取 — 已移至 run_nightly() 中以獨立執行緒在 00:00 準時啟動，不佔 tick 流程

    # 1) LAF one-shot (email + download + archive)
    # Changed to async background to avoid Head-of-Line Blocking
    try:
        laf_proc = subprocess.Popen(
            [VENV_PY, "-c", "import sys; sys.path.insert(0, str(_MAGI_ROOT)); from skills.magi_autopilot.action import _laf_one_shot; _laf_one_shot()"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ
        )
        threading.Thread(target=laf_proc.wait, daemon=True).start()
        results["steps"]["laf"] = {"ok": True, "async_bg": True, "pid": laf_proc.pid}
    except Exception as e:
        results["steps"]["laf"] = {"ok": False, "error": f"async_launch_failed: {e}"}

    # 1.3) LAF pending case active scan (待開辦/待報結 主動通知)
    try:
        laf_pending_payload = json.dumps({
            "open_grace_days": int(os.environ.get("MAGI_LAF_OPEN_GRACE_DAYS", "3") or "3"),
            "report_grace_days": int(os.environ.get("MAGI_LAF_REPORT_GRACE_DAYS", "1") or "1"),
            "notify": True,
            "limit": 50,
        }, ensure_ascii=False)
        laf_pending_cmd = [VENV_PY, _skill_action("osc-orchestrator"), "--task",
                           f"laf_pending_scan {laf_pending_payload}"]
        _run_budgeted_step(
            "laf_pending_scan",
            laf_pending_cmd,
            _tb("MAGI_NIGHTLY_LAF_PENDING_BUDGET_SEC", "MAGI_NIGHTLY_LAF_PENDING_TIMEOUT_SEC", 60),
            min_start_sec=10,
            reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
        )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3706, exc_info=True)

    # 1.5) LAF condition draft one-shot (WF5)
    if os.environ.get("MAGI_LAF_CONDITION_ENABLE", "1").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            max_cases = int((os.environ.get("MAGI_LAF_CONDITION_MAX_CASES", "0") or "0").strip() or "0")
        except Exception:
            max_cases = 0
        try:
            cond_res = _laf_condition_draft_one_shot(max_cases=max_cases)
        except Exception as e:
            cond_res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        results["steps"]["laf_condition_draft"] = cond_res
        if not cond_res.get("ok", True):
            maybe_block("laf_condition_draft", str(cond_res.get("error", "")))

    # 1.7) LAF missing-address deep extraction (old case docs/email attachments/report text)
    if os.environ.get("MAGI_LAF_DEEP_EXTRACT_ENABLE", "1").strip().lower() in {"1", "true", "yes", "on"}:
        try:
            de_limit = int((os.environ.get("MAGI_LAF_DEEP_EXTRACT_MAX_CLIENTS", "8") or "8").strip() or "8")
        except Exception:
            de_limit = 8
        try:
            de_timeout = int((os.environ.get("MAGI_LAF_DEEP_EXTRACT_TIMEOUT_SEC", "240") or "240").strip() or "240")
        except Exception:
            de_timeout = 240
        deep_script = os.path.join(MAGI_ROOT_DIR, "scripts", "ops", "laf_deep_extract_backfill.py")
        if os.path.exists(deep_script):
            deep_cmd = [
                VENV_PY,
                deep_script,
                "--limit",
                str(max(1, de_limit)),
            ]
            deep_res = _run_cmd(deep_cmd, timeout_sec=max(60, de_timeout))
            _stash_cmd_output(run_dir, "laf_deep_extract", deep_cmd, deep_res)
            results["steps"]["laf_deep_extract"] = {
                "ok": deep_res.ok,
                "returncode": deep_res.returncode,
                "parsed": deep_res.parsed,
                "stderr_tail": (deep_res.stderr or "")[-800:],
            }
        else:
            results["steps"]["laf_deep_extract"] = {
                "ok": True,
                "skipped": True,
                "reason": f"missing_script:{deep_script}",
            }
        # Non-blocking: this is enrichment; never stall the whole tick.

    # 2) File review email check — 已由 file_review_auto_worker.py 每小時執行，tick 不再重複
    results["steps"]["file_review_check"] = {
        "ok": True,
        "skipped": True,
        "reason": "delegated_to_file_review_auto_worker (interval=3600s)",
    }

    # 2.2) File review site check + auto download — 已由 file_review_auto_worker.py 每小時執行
    results["steps"]["file_review_download"] = {
        "ok": True,
        "skipped": True,
        "reason": "delegated_to_file_review_auto_worker (interval=3600s)",
    }

    # 2.3) Transcript daily check on tick (db_probe -> sync)
    tr_tick_enabled = os.environ.get("MAGI_ENABLE_TRANSCRIPT_CHECK_TICK", "1").strip().lower() in {"1", "true", "yes", "on"}
    if tr_tick_enabled:
        tr_probe_cmd = [VENV_PY, _skill_action("transcript-downloader"), "--task", "db_probe"]
        tr_probe = _run_cmd(
            tr_probe_cmd,
            timeout_sec=int(os.environ.get("MAGI_TICK_TRANSCRIPT_DB_PROBE_TIMEOUT_SEC", "120") or "120"),
        )
        _stash_cmd_output(run_dir, "transcript_db_probe", tr_probe_cmd, tr_probe)
        results["steps"]["transcript_db_probe"] = {
            "ok": tr_probe.ok,
            "returncode": tr_probe.returncode,
            "parsed": tr_probe.parsed,
            "stderr_tail": (tr_probe.stderr or "")[-800:],
        }
        if not tr_probe.ok:
            maybe_block("transcript_db_probe", str((tr_probe.parsed or {}).get("error") or tr_probe.stderr or ""))
        else:
            p = (tr_probe.parsed or {}) if isinstance(tr_probe.parsed, dict) else {}
            try:
                eligible = int((p.get("eligible_cases") or 0))
            except Exception:
                eligible = 0
            # 兼容舊/混合輸出：若解析結果是單一案件樣本物件，也視為有可同步案件。
            if eligible <= 0:
                try:
                    if isinstance(p, dict) and (p.get("case_number") or p.get("court_case_number")):
                        eligible = 1
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 3799, exc_info=True)
            if eligible > 0:
                tr_cd = _transcript_captcha_cooldown_state()
                if bool(tr_cd.get("active")):
                    results["steps"]["transcript_sync"] = {
                        "ok": True,
                        "skipped": True,
                        "reason": f"略過：captcha cooldown 中（剩餘約 {int(tr_cd.get('remaining_minutes', 0))} 分鐘）",
                        "eligible_cases": eligible,
                        "captcha_cooldown": tr_cd,
                    }
                    results.setdefault("warnings", []).append("transcript_sync: captcha_cooldown_skip")
                else:
                    tr_sync_cmd = [VENV_PY, _skill_action("transcript-downloader"), "--task", "sync"]
                    tr_sync_env = dict(os.environ)
                    tr_sync_env["MAGI_EZLAWYER_SOLVE_CAPTCHA"] = "0"
                    tr_sync_env["MAGI_EZLAWYER_ASSUME_CAPTCHA_REQUIRED"] = "0"
                    tr_sync_env["MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK"] = "0"
                    tr_sync_first = _run_cmd(
                        tr_sync_cmd,
                        timeout_sec=int(os.environ.get("MAGI_TICK_TRANSCRIPT_SYNC_TIMEOUT_SEC", "900") or "900"),
                        env=tr_sync_env,
                    )
                    _stash_cmd_output(run_dir, "transcript_sync", tr_sync_cmd, tr_sync_first)
                    tr_sync = tr_sync_first
                    tr_retry_detail: Optional[Dict[str, Any]] = None
                    tr_retry_enabled = os.environ.get("MAGI_TICK_TRANSCRIPT_RETRY_ON_TIMEOUT", "1").strip().lower() in {"1", "true", "yes", "on"}
                    if tr_retry_enabled and (not tr_sync_first.ok) and _cmd_timed_out(tr_sync_first):
                        # 低負載重試：改用 download_all（略過前置掃描/更名），避免第二次又卡死。
                        tr_retry_cmd = [VENV_PY, _skill_action("transcript-downloader"), "--task", "download_all"]
                        tr_retry_env = dict(tr_sync_env)
                        tr_retry_env["MAGI_SELENIUM_PAGELOAD_TIMEOUT_SEC"] = str(
                            os.environ.get("MAGI_TICK_TRANSCRIPT_RETRY_PAGELOAD_TIMEOUT_SEC", "30") or "30"
                        )
                        tr_retry = _run_cmd(
                            tr_retry_cmd,
                            timeout_sec=int(os.environ.get("MAGI_TICK_TRANSCRIPT_RETRY_TIMEOUT_SEC", "420") or "420"),
                            env=tr_retry_env,
                        )
                        _stash_cmd_output(run_dir, "transcript_sync_retry", tr_retry_cmd, tr_retry)
                        tr_retry_detail = {
                            "attempted": True,
                            "reason": "timeout",
                            "mode": "low_load_download_all",
                            "ok": tr_retry.ok,
                            "returncode": tr_retry.returncode,
                            "parsed": tr_retry.parsed if isinstance(tr_retry.parsed, dict) else {},
                            "stderr_tail": (tr_retry.stderr or "")[-500:],
                        }
                        tr_sync = tr_retry
                    results["steps"]["transcript_sync"] = {
                        "ok": tr_sync.ok,
                        "returncode": tr_sync.returncode,
                        "parsed": tr_sync.parsed,
                        "stderr_tail": (tr_sync.stderr or "")[-800:],
                    }
                    if tr_retry_detail:
                        results["steps"]["transcript_sync"]["retry"] = tr_retry_detail
                    if not tr_sync.ok:
                        tr_err = str((tr_sync.parsed or {}).get("error") or tr_sync.stderr or "")
                        if _looks_like_captcha_error(tr_err):
                            qpath = _queue_transcript_captcha_defer(
                                run_dir,
                                step="transcript_sync",
                                eligible_cases=eligible,
                                parsed=(tr_sync.parsed if isinstance(tr_sync.parsed, dict) else {}),
                                error=tr_err,
                                retry_attempted=bool(tr_retry_detail and tr_retry_detail.get("attempted")),
                                retry_ok=bool(tr_retry_detail and tr_retry_detail.get("ok")),
                            )
                            results["steps"]["transcript_sync"]["captcha_deferred"] = True
                            results["steps"]["transcript_sync"]["defer_queue_path"] = qpath
                        maybe_block("transcript_sync", tr_err)
            else:
                results["steps"]["transcript_sync"] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "db_probe 無可同步案件",
                    "eligible_cases": 0,
                }
    else:
        results["steps"]["transcript_db_probe"] = {
            "ok": True,
            "skipped": True,
            "reason": "MAGI_ENABLE_TRANSCRIPT_CHECK_TICK=0",
        }
        results["steps"]["transcript_sync"] = {
            "ok": True,
            "skipped": True,
            "reason": "MAGI_ENABLE_TRANSCRIPT_CHECK_TICK=0",
        }

    # 2.5) 官方裁判 API 白天整理（夜間服務時段內直接略過，不啟動子程序）
    jc_path = _skill_action("judgment-collector")
    day_proc_enabled = os.environ.get("MAGI_ENABLE_JUDICIAL_API_DAY_PROCESS", "1").strip().lower() in {"1", "true", "yes", "on"}
    in_jdg_window = _is_judicial_api_service_window()
    if os.path.exists(jc_path) and day_proc_enabled:
        if in_jdg_window:
            results["steps"]["judicial_api_day_process"] = {
                "ok": True,
                "skipped": True,
                "reason": "夜間官方 API 服務時段內，白天整理略過",
            }
        else:
            day_payload = {
                "max_docs": int(os.environ.get("MAGI_JUDICIAL_API_DAY_MAX_DOCS", "200") or "200"),
                "summarize_max": int(os.environ.get("MAGI_JUDICIAL_API_DAY_SUMMARY_MAX", "80") or "80"),
                "force": False,
                "notify": False,
            }
            jc_day_cmd = [VENV_PY, jc_path, "--task", "official_api_day_process " + json.dumps(day_payload, ensure_ascii=False)]
            jc_day = _run_cmd(
                jc_day_cmd,
                timeout_sec=int(os.environ.get("MAGI_JUDICIAL_API_DAY_TIMEOUT_SEC", "3600") or "3600"),
            )
            _stash_cmd_output(run_dir, "judicial_api_day_process", jc_day_cmd, jc_day)
            results["steps"]["judicial_api_day_process"] = {
                "ok": jc_day.ok,
                "returncode": jc_day.returncode,
                "parsed": jc_day.parsed,
                "stderr_tail": (jc_day.stderr or "")[-800:],
            }
            retry_day_process = (
                os.environ.get("MAGI_JUDICIAL_API_DAY_RETRY_ON_BACKLOG", "1").strip().lower()
                in {"1", "true", "yes", "on"}
            )
            if jc_day.ok and retry_day_process and isinstance(jc_day.parsed, dict):
                try:
                    backlog_remaining = int(jc_day.parsed.get("backlog_remaining") or 0)
                except Exception:
                    backlog_remaining = 0
                try:
                    handled_first = int(jc_day.parsed.get("handled") or 0)
                except Exception:
                    handled_first = 0
                if backlog_remaining > 0 and handled_first > 0:
                    retry_payload = dict(day_payload)
                    retry_payload["force"] = True
                    retry_payload["max_docs"] = min(
                        max(backlog_remaining, int(day_payload.get("max_docs") or 0)),
                        int(os.environ.get("MAGI_JUDICIAL_API_DAY_RETRY_MAX_DOCS", "400") or "400"),
                    )
                    jc_day_retry_cmd = [
                        VENV_PY,
                        jc_path,
                        "--task",
                        "official_api_day_process " + json.dumps(retry_payload, ensure_ascii=False),
                    ]
                    jc_day_retry = _run_cmd(
                        jc_day_retry_cmd,
                        timeout_sec=int(os.environ.get("MAGI_JUDICIAL_API_DAY_RETRY_TIMEOUT_SEC", "1200") or "1200"),
                    )
                    _stash_cmd_output(run_dir, "judicial_api_day_process_retry", jc_day_retry_cmd, jc_day_retry)
                    results["steps"]["judicial_api_day_process"]["retry"] = {
                        "ok": jc_day_retry.ok,
                        "returncode": jc_day_retry.returncode,
                        "parsed": jc_day_retry.parsed,
                        "stderr_tail": (jc_day_retry.stderr or "")[-800:],
                    }
            if not jc_day.ok:
                maybe_block("judicial_api_day_process", str((jc_day.parsed or {}).get("error") or jc_day.stderr or ""))

    # 3) PDF namer file pipeline (notify=0, execute=1)
    # Changed to async background
    pn_cmd = [VENV_PY, _skill_action("pdf-namer"), "--task", "file", "--execute", "1", "--notify", "0"]
    try:
        pn_proc = subprocess.Popen(pn_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ)
        threading.Thread(target=pn_proc.wait, daemon=True).start()
        results["steps"]["pdf_namer"] = {"ok": True, "async_bg": True, "pid": pn_proc.pid}
    except Exception as e:
        results["steps"]["pdf_namer"] = {"ok": False, "error": f"async_launch_failed: {e}"}

    # 3.5) OSC Scan Folder (Synology)
    sf_enabled = os.environ.get("MAGI_ENABLE_SCAN_FOLDER", "1").strip().lower() in {"1", "true", "yes", "on"}
    if sf_enabled:
        sf_payload = {"max_files": 80, "dry_run": False}
        sf_async = os.environ.get("MAGI_TICK_SCAN_FOLDER_ASYNC", "1").strip().lower() in {"1", "true", "yes", "on"}
        scan_root = (
            str(os.environ.get("MAGI_SCAN_FOLDER_ROOT") or os.environ.get("MAGI_PIGEONHOLE_STAGING") or "").strip()
        )
        if not scan_root:
            for c in default_scan_roots():
                if os.path.isdir(c):
                    scan_root = c
                    break
        if not scan_root:
            results["steps"]["osc_scan_folder"] = {
                "ok": True,
                "skipped": True,
                "reason": "missing_scan_root",
            }
        else:
            sf_payload["root"] = scan_root
            sf_cmd = [VENV_PY, _skill_action("osc-scan-folder"), "--task", "run " + json.dumps(sf_payload, ensure_ascii=False)]
            if sf_async:
                try:
                    sf_proc = subprocess.Popen(sf_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ)
                    threading.Thread(target=sf_proc.wait, daemon=True).start()
                    results["steps"]["osc_scan_folder"] = {
                        "ok": True,
                        "async_bg": True,
                        "pid": sf_proc.pid,
                        "scan_root": scan_root,
                    }
                except Exception as e:
                    results["steps"]["osc_scan_folder"] = {"ok": False, "error": f"async_launch_failed: {e}"}
                    maybe_block("osc_scan_folder", str(e))
            else:
                try:
                    sf_timeout = int(os.environ.get("MAGI_TICK_SCAN_FOLDER_TIMEOUT_SEC", "300") or "300")
                except Exception:
                    sf_timeout = 300
                sf_res = _run_cmd(sf_cmd, timeout_sec=max(30, sf_timeout))
                _stash_cmd_output(run_dir, "osc_scan_folder", sf_cmd, sf_res)
                sf_parsed = sf_res.parsed if isinstance(sf_res.parsed, dict) else {}
                sf_error = str((sf_parsed or {}).get("error") or "")
                sf_missing_root = ("找不到可用掃描根目錄" in sf_error) or ("需要 root/path" in sf_error)
                results["steps"]["osc_scan_folder"] = {
                    "ok": (sf_res.ok or sf_missing_root),
                    "returncode": sf_res.returncode,
                    "parsed": sf_parsed,
                    "stderr_tail": (sf_res.stderr or "")[-800:],
                }
                if sf_missing_root:
                    results["steps"]["osc_scan_folder"]["skipped"] = True
                    results["steps"]["osc_scan_folder"]["reason"] = sf_error or "missing_scan_root"
                elif not sf_res.ok:
                    maybe_block("osc_scan_folder", str((sf_res.parsed or {}).get("error") or sf_res.stderr or ""))

    # 4) OSC todo sync scan (best-effort) + flush queue
    # Changed to async background
    osc_scan_payload = {"max_cases": 10, "max_files_per_case": 30, "time_budget_sec": 240}
    osc_scan_cmd = [VENV_PY, _skill_action("osc-orchestrator"), "--task", "scan_cases " + json.dumps(osc_scan_payload, ensure_ascii=False)]
    try:
        osc_scan_proc = subprocess.Popen(osc_scan_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=os.environ)
        threading.Thread(target=osc_scan_proc.wait, daemon=True).start()
        results["steps"]["osc_scan_cases"] = {"ok": True, "async_bg": True, "pid": osc_scan_proc.pid}
    except Exception as e:
        results["steps"]["osc_scan_cases"] = {"ok": False, "error": f"async_launch_failed: {e}"}

    osc_flush_cmd = [VENV_PY, _skill_action("osc-orchestrator"), "--task", "queue_flush {}"]
    try:
        osc_flush_timeout = int(os.environ.get("MAGI_TICK_OSC_QUEUE_FLUSH_TIMEOUT_SEC", "120") or "120")
    except Exception:
        osc_flush_timeout = 120
    osc_flush = _run_cmd(osc_flush_cmd, timeout_sec=max(20, osc_flush_timeout))
    _stash_cmd_output(run_dir, "osc_queue_flush", osc_flush_cmd, osc_flush)
    results["steps"]["osc_queue_flush"] = {
        "ok": osc_flush.ok,
        "returncode": osc_flush.returncode,
        "parsed": osc_flush.parsed,
        "stderr_tail": (osc_flush.stderr or "")[-800:],
    }
    if not osc_flush.ok:
        maybe_block("osc_queue_flush", str((osc_flush.parsed or {}).get("error") or osc_flush.stderr or ""))
    else:
        # Prefer "blocking" count: db_error is usually transient (DB down) and should not wake you up.
        try:
            remaining_blocking = int(((osc_flush.parsed or {}).get("remaining_blocking") or 0))
        except Exception:
            remaining_blocking = 0
        try:
            remaining_total = int(((osc_flush.parsed or {}).get("remaining") or 0))
        except Exception:
            remaining_total = 0
        if remaining_blocking > 0:
            results["blocked"] = True
            results["blockers"].append(
                f"OSC 待辦佇列尚有 {remaining_blocking} 筆需人工判斷（共 {remaining_total} 筆未入庫；可能是案號歧義）"
            )

    # "ok" means: no human intervention needed (blockers empty).
    # Some steps can fail transiently (site down / network hiccup) and should not wake you up.
    results["ok"] = (not results["blocked"])
    if emit_step_events and (not tick_light_mode):
        _remember_step_events("tick", run_dir, results.get("steps") or {})
    return results


def run_nightly(run_dir: str) -> Dict[str, Any]:
    os.environ.setdefault("MAGI_NO_DELETE", "1")
    os.environ.setdefault("MAGI_DB_NO_DELETE", "1")
    os.environ.setdefault("MAGI_PREFER_LOCAL_DB", "0")
    os.environ.setdefault("MAGI_PDF_TEXT_ENGINE", "pymupdf")
    os.environ.setdefault("MAGI_DISABLE_APPLE_AI", "1")
    os.environ.setdefault("MAGI_SYSTEM_NOTIFY_CHANNEL", "telegram")
    os.environ.setdefault("MAGI_SYSTEM_NOTIFY_LINE_FALLBACK", "0")
    # 筆錄調閱站（ezlawyer）夜間/排程不應要求驗證碼人工輸入
    os.environ.setdefault("MAGI_EZLAWYER_SOLVE_CAPTCHA", "0")
    os.environ.setdefault("MAGI_EZLAWYER_ASSUME_CAPTCHA_REQUIRED", "0")
    os.environ.setdefault("MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK", "0")
    os.environ.setdefault("MAGI_RECORD_PARSE_CASPER_ASSIST", "0")
    os.environ.setdefault("MAGI_LAF_CONDITION_ENABLE", "1")
    os.environ.setdefault("MAGI_LAF_CONDITION_MAX_CASES", "6")
    os.environ.setdefault("MAGI_ENABLE_FILE_REVIEW_SCAN", "1")
    os.environ.setdefault("MAGI_ENABLE_FILE_REVIEW_DOWNLOAD", "1")
    os.environ.setdefault("MAGI_ENABLE_TRANSCRIPT_SYNC", "1")
    os.environ.setdefault("MAGI_TRANSCRIPT_CAPTCHA_COOLDOWN_ENABLE", "1")
    os.environ.setdefault("MAGI_TRANSCRIPT_CAPTCHA_COOLDOWN_MINUTES", "180")
    os.environ.setdefault("MAGI_ENABLE_JUDGMENT_CRAWL", "1")
    os.environ.setdefault("MAGI_ENABLE_JUDICIAL_API_NIGHT_PULL", "1")
    os.environ.setdefault("MAGI_ENABLE_SCAN_FOLDER", "1")
    os.environ.setdefault("MAGI_ENABLE_DB_DAILY_BACKUP", "1")
    os.environ.setdefault("MAGI_DB_BACKUP_TARGET", "both")
    os.environ.setdefault("MAGI_DB_BACKUP_KEEP_DAYS", "30")
    # Nightly 可以做較完整的大腦修復循環。
    os.environ.setdefault("MAGI_BIG_BRAIN_REMOTE_REPAIR", "1")
    os.environ.setdefault("MAGI_BIG_BRAIN_REMOTE_REPAIR_TIMEOUT_SEC", "150")
    os.environ.setdefault("MAGI_LAF_DEEP_EXTRACT_ENABLE", "1")
    os.environ.setdefault("MAGI_LAF_DEEP_EXTRACT_MAX_CLIENTS", "30")
    os.environ.setdefault("MAGI_LAF_DEEP_EXTRACT_TIMEOUT_SEC", "720")
    os.environ.setdefault("MAGI_ENABLE_SLO_GUARD", "1")
    os.environ.setdefault("MAGI_ENABLE_SLO_GUARD_24H", "1")
    os.environ.setdefault("MAGI_AUTO_APPLY_SLO_DEGRADE", "1")
    os.environ.setdefault("MAGI_AUTO_APPLY_SLO_MIN_BLOCKED_24H", "1")
    os.environ.setdefault("MAGI_SLO_GUARD_HOURS", "72")
    os.environ.setdefault("OSC_GCAL_RETRY_MAX_ATTEMPTS", "3")
    os.environ.setdefault("OSC_GCAL_RETRY_SLEEP_SEC", "1.0")
    # 判決爬取避免拖死整輪：讓 judgment-collector 內部 time_budget 更保守一點。
    os.environ.setdefault("JUDGMENT_DAILY_TIME_BUDGET_SEC", "900")

    # ── Phase 0: PDF 視覺訓練（22:00 起跑，使用 oMLX/PyMuPDF，不影響後續 API 拉取）──
    import threading as _thr
    _nightly_train_script = os.path.join(MAGI_ROOT_DIR, "skills", "pdf-namer", "nightly_train.py")
    _pdf_train_result: Dict[str, Any] = {"ok": False, "skipped": True}
    pdf_train_enabled = os.environ.get("MAGI_ENABLE_PDF_NIGHTLY_TRAIN", "1").strip().lower() in {"1", "true", "yes", "on"}
    if pdf_train_enabled and os.path.exists(_nightly_train_script):
        _pdf_train_max = int(os.environ.get("MAGI_PDF_TRAIN_MAX_FILES", "200") or "200")
        _pdf_train_timeout = int(os.environ.get("MAGI_PDF_TRAIN_TIMEOUT_SEC", "3600") or "3600")
        _pdf_train_cmd = [VENV_PY, _nightly_train_script, "--max-files", str(_pdf_train_max)]
        logger.info("nightly: PDF 視覺訓練開始（max_files=%d）", _pdf_train_max)
        _pdf_train_res = _run_cmd(_pdf_train_cmd, timeout_sec=_pdf_train_timeout)
        _stash_cmd_output(run_dir, "pdf_nightly_train", _pdf_train_cmd, _pdf_train_res)
        _pdf_train_result = {
            "ok": _pdf_train_res.ok,
            "returncode": _pdf_train_res.returncode,
            "parsed": _pdf_train_res.parsed,
            "stderr_tail": (_pdf_train_res.stderr or "")[-800:],
        }
        logger.info("nightly: PDF 視覺訓練完成 ok=%s", _pdf_train_res.ok)

    # ── Phase 1: 司法院 API 夜間拉取 — 獨立執行緒，等到 00:00 準時啟動 ──
    # 司法院 API 是純 HTTP（data.judicial.gov.tw），完全不用 oMLX，可與其他任務並行。
    _jdg_thread_result: Dict[str, Any] = {"ok": False, "skipped": True}
    _jdg_thread_done = _thr.Event()
    jc_path = _skill_action("judgment-collector")
    jdg_night_enabled = os.environ.get("MAGI_ENABLE_JUDICIAL_API_NIGHT_PULL", "1").strip().lower() in {"1", "true", "yes", "on"}

    def _judicial_api_night_thread():
        """等到 00:00 服務時段再開始拉取，不阻塞主流程。"""
        nonlocal _jdg_thread_result
        try:
            # 等到 00:00（若已在 00:00-06:00 區間則立即開始）
            while True:
                now_h = datetime.now().hour
                if 0 <= now_h < 6:
                    break
                # 計算距離午夜的秒數
                now_dt = datetime.now()
                midnight = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
                if now_dt >= midnight:
                    from datetime import timedelta
                    midnight += timedelta(days=1)
                wait_sec = (midnight - now_dt).total_seconds()
                logger.info("judicial_api_night_thread: 等待 %.0f 秒到 00:00 服務時段", wait_sec)
                time.sleep(min(wait_sec + 1, 60))  # 每 60 秒最多重新檢查一次
            logger.info("judicial_api_night_thread: 00:00 服務時段到，開始拉取")
            jdg_payload = {
                "max_jdocs": int(os.environ.get("MAGI_JUDICIAL_API_NIGHT_MAX_JDOCS", "25000") or "25000"),
                "max_days": int(os.environ.get("MAGI_JUDICIAL_API_NIGHT_MAX_DAYS", "0") or "0"),
                "force": False,
                "notify": True,
            }
            jdg_cmd = [VENV_PY, jc_path, "--task", "official_api_night_pull " + json.dumps(jdg_payload, ensure_ascii=False)]
            jdg_env = os.environ.copy()
            # P0-12: 不再預設開啟 insecure SSL。若需要請在 .env 中明確設定。
            # jdg_env["JUDICIAL_API_ALLOW_INSECURE_SSL"] = "1"
            jdg_env.setdefault("JUDICIAL_API_ALLOW_INSECURE_SSL", os.environ.get("JUDICIAL_API_ALLOW_INSECURE_SSL", "0"))
            jdg_timeout = int(os.environ.get("MAGI_JUDICIAL_API_NIGHT_TIMEOUT_SEC", "5400") or "5400")
            res = _run_cmd(jdg_cmd, timeout_sec=jdg_timeout, env=jdg_env)
            _stash_cmd_output(run_dir, "judicial_api_night_pull", jdg_cmd, res)
            _jdg_thread_result = {
                "ok": res.ok,
                "returncode": res.returncode,
                "parsed": res.parsed,
                "stderr_tail": (res.stderr or "")[-800:],
                "thread": True,
            }
            logger.info("judicial_api_night_thread: 完成 ok=%s", res.ok)
        except Exception as e:
            _jdg_thread_result = {"ok": False, "error": f"{type(e).__name__}: {e}", "thread": True}
            logger.error("judicial_api_night_thread: 失敗 %s", e)
        finally:
            _jdg_thread_done.set()

    _jdg_thread = None
    if os.path.exists(jc_path) and jdg_night_enabled:
        _jdg_thread = _thr.Thread(target=_judicial_api_night_thread, name="judicial_api_night", daemon=True)
        _jdg_thread.start()
        logger.info("nightly: 司法院 API 執行緒已啟動，將在 00:00 準時開始拉取")
    else:
        _jdg_thread_result = {"ok": True, "skipped": True, "reason": "disabled or script not found"}
        _jdg_thread_done.set()

    # ── Phase 2: 主流程（run_tick 中的各項任務照常執行）──
    # nightly 階段只做夜間拉取，不做白天整理，避免同輪混跑。
    _prev_day_process = os.environ.get("MAGI_ENABLE_JUDICIAL_API_DAY_PROCESS")
    os.environ["MAGI_ENABLE_JUDICIAL_API_DAY_PROCESS"] = "0"
    try:
        results = run_tick(run_dir, emit_step_events=False)
    finally:
        if _prev_day_process is None:
            os.environ.pop("MAGI_ENABLE_JUDICIAL_API_DAY_PROCESS", None)
        else:
            os.environ["MAGI_ENABLE_JUDICIAL_API_DAY_PROCESS"] = _prev_day_process

    # 寫入 PDF 視覺訓練結果
    results["steps"]["pdf_nightly_train"] = _pdf_train_result

    # 等待司法院 API 執行緒完成（最多再等 30 分鐘）
    if _jdg_thread is not None and _jdg_thread.is_alive():
        logger.info("nightly: 等待司法院 API 執行緒完成...")
        _jdg_thread_done.wait(timeout=1800)
    results["steps"]["judicial_api_night_pull"] = _jdg_thread_result

    # ── Phase 2.5: 拉取完成後立即整理摘要（夜間有充足 budget）──
    jc_path_post = _skill_action("judgment-collector")
    _post_day_enabled = os.environ.get("MAGI_ENABLE_JUDICIAL_API_NIGHTLY_PROCESS", "1").strip().lower() in {"1", "true", "yes", "on"}
    if _user_active_defer("judicial_api_nightly_process"):
        pass  # 使用者活躍中，摘要整理自動延後
    elif os.path.exists(jc_path_post) and _post_day_enabled:
        _post_payload = {
            "max_docs": int(os.environ.get("MAGI_JUDICIAL_API_NIGHTLY_PROCESS_MAX_DOCS", "400") or "400"),
            "summarize_max": int(os.environ.get("MAGI_JUDICIAL_API_NIGHTLY_SUMMARY_MAX", "200") or "200"),
            "force": False,
            "notify": False,
        }
        _post_timeout = int(os.environ.get("MAGI_JUDICIAL_API_NIGHTLY_PROCESS_TIMEOUT_SEC", "7200") or "7200")
        _post_cmd = [VENV_PY, jc_path_post, "--task", "official_api_day_process " + json.dumps(_post_payload, ensure_ascii=False)]
        logger.info("nightly: 開始整理摘要（max_docs=%d, timeout=%ds）", _post_payload["max_docs"], _post_timeout)
        _post_res = _run_cmd(_post_cmd, timeout_sec=_post_timeout)
        _stash_cmd_output(run_dir, "judicial_api_nightly_process", _post_cmd, _post_res)
        results["steps"]["judicial_api_nightly_process"] = {
            "ok": _post_res.ok,
            "returncode": _post_res.returncode,
            "parsed": _post_res.parsed,
            "stderr_tail": (_post_res.stderr or "")[-800:],
        }
        logger.info("nightly: 整理摘要完成 ok=%s", _post_res.ok)
    else:
        results["steps"]["judicial_api_nightly_process"] = {"ok": True, "skipped": True, "reason": "disabled or script not found"}

    results["openclaw_runtime_mode"] = _openclaw_runtime_mode_report()
    results["judicial_api_pipeline"] = _judicial_api_pipeline_report()
    auth_step = (results.get("steps") or {}).get("openclaw_auth_guard") or {}
    if isinstance(auth_step, dict) and not bool(auth_step.get("ok", True)):
        results["ok"] = False
        return results

    # ── 使用者優先：LLM 重度步驟開始前檢查使用者是否活躍 ──
    _USER_DEFER_THRESHOLD = int(os.environ.get("MAGI_USER_ACTIVE_THRESHOLD_SEC", "300"))

    def _user_active_defer(step_name: str) -> bool:
        """若使用者近期活躍，跳過此 LLM 步驟並記錄。回傳 True 表示已延後。"""
        try:
            from skills.ops.user_activity_beacon import is_user_active, seconds_since_last_activity
            if is_user_active(_USER_DEFER_THRESHOLD):
                elapsed = int(seconds_since_last_activity())
                reason = f"略過：使用者 {elapsed} 秒前仍在使用中，LLM 任務自動延後"
                logger.info("nightly: %s — %s", step_name, reason)
                results["steps"][step_name] = {
                    "ok": True, "skipped": True,
                    "reason": reason,
                    "user_active_defer": True,
                }
                return True
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4273, exc_info=True)
        return False

    def _t(env_name: str, default: int) -> int:
        try:
            return int((os.environ.get(env_name, str(default)) or str(default)).strip() or str(default))
        except Exception:
            return int(default)

    def _tb(env_name_budget: str, env_name_legacy_timeout: str, default: int) -> int:
        """
        取每步驟 budget（秒），並向後相容舊的 *_TIMEOUT_SEC 設定。
        優先順序：*_BUDGET_SEC > *_TIMEOUT_SEC > default
        """
        raw = os.environ.get(env_name_budget, "")
        if str(raw or "").strip():
            return _t(env_name_budget, default)
        return _t(env_name_legacy_timeout, default)

    nightly_total_budget_sec = _t("MAGI_NIGHTLY_TOTAL_BUDGET_SEC", 28800)  # 8h (22:00-06:00)
    nightly_guard_sec = _t("MAGI_NIGHTLY_BUDGET_GUARD_SEC", 45)
    reserve_iron_dome_sec = _t("MAGI_NIGHTLY_RESERVE_IRON_DOME_SEC", 25)
    reserve_final_flush_sec = _t("MAGI_NIGHTLY_RESERVE_FINAL_FLUSH_SEC", 20)
    nightly_started = time.monotonic()

    def _remaining_budget_sec() -> int:
        if nightly_total_budget_sec <= 0:
            return 0
        elapsed = int(time.monotonic() - nightly_started)
        remain = nightly_total_budget_sec - elapsed
        return remain if remain > 0 else 0

    def _allocate_step_budget(requested_sec: int, *, min_start_sec: int = 20) -> Dict[str, int]:
        remain_before = _remaining_budget_sec()
        usable = max(0, remain_before - max(0, nightly_guard_sec))
        allocated = min(max(0, int(requested_sec)), usable)
        return {
            "requested_sec": max(0, int(requested_sec)),
            "allocated_sec": max(0, int(allocated)),
            "min_start_sec": max(1, int(min_start_sec)),
            "remaining_before_sec": max(0, int(remain_before)),
            "guard_sec": max(0, int(nightly_guard_sec)),
        }

    def _record_skip(step_name: str, reason: str, budget: Dict[str, int]) -> None:
        parsed = {
            "success": True,
            "skipped": True,
            "skipped_due_to_budget": True,
            "message": reason,
            "budget": {
                **budget,
                "remaining_after_sec": _remaining_budget_sec(),
            },
        }
        results["steps"][step_name] = {
            "ok": True,
            "returncode": 0,
            "parsed": parsed,
            "stderr_tail": "",
        }

    # ── LLM 密集步驟白名單：只有這些步驟在使用者活躍時自動延後 ──
    # 注意：judicial_api_night_pull（司法院API夜拉）是最高優先夜間任務，
    #       走獨立 thread + _run_cmd，不經此處，絕不延後。
    #       judicial_api_nightly_process（摘要整理）已有手動 _user_active_defer，不需列入。
    _LLM_HEAVY_STEPS = {
        "judgment_daily_crawl",         # LLM 摘要（Lawsnote 爬取+摘要）
        "weekend_insight_repair",       # LLM 摘要修復
        "writing_style_learning",       # LLM 風格分析
        "statutes_vdb_update",          # oMLX embedding
        "daily_reflection",             # LLM 生成
    }

    def _run_budgeted_step(
        step_name: str,
        cmd: list,
        requested_sec: int,
        *,
        env: Optional[dict] = None,
        min_start_sec: int = 20,
        reserve_after_sec: int = 0,
    ) -> Optional[CmdResult]:
        # ── 使用者優先：LLM 密集步驟在使用者活躍時自動延後 ──
        if step_name in _LLM_HEAVY_STEPS and _user_active_defer(step_name):
            return None
        budget = _allocate_step_budget(requested_sec, min_start_sec=min_start_sec)
        if reserve_after_sec > 0 and budget.get("allocated_sec", 0) > 0:
            allocated_after_reserve = max(0, int(budget["allocated_sec"]) - int(reserve_after_sec))
            budget["allocated_sec"] = allocated_after_reserve
            budget["reserved_for_later_sec"] = int(reserve_after_sec)
        if budget["allocated_sec"] < budget["min_start_sec"]:
            _record_skip(step_name, "略過：nightly 剩餘 time budget 不足", budget)
            return None
        res = _run_cmd(cmd, timeout_sec=budget["allocated_sec"], env=env)
        _stash_cmd_output(run_dir, step_name, cmd, res)
        parsed = res.parsed if isinstance(res.parsed, dict) else {}
        if not isinstance(parsed, dict):
            parsed = {}
        parsed.setdefault("budget", {})
        if isinstance(parsed.get("budget"), dict):
            parsed["budget"].update(
                {
                    **budget,
                    "remaining_after_sec": _remaining_budget_sec(),
                }
            )
        else:
            parsed["budget"] = {
                **budget,
                "remaining_after_sec": _remaining_budget_sec(),
            }
        results["steps"][step_name] = {
            "ok": res.ok,
            "returncode": res.returncode,
            "parsed": parsed,
            "stderr_tail": (res.stderr or "")[-800:],
        }
        return res

    # Parallel execution helper for independent nightly steps
    import threading
    _budget_lock = threading.Lock()

    def _run_budgeted_step_ts(
        step_name: str,
        cmd: list,
        requested_sec: int,
        *,
        env: Optional[dict] = None,
        min_start_sec: int = 20,
        reserve_after_sec: int = 0,
    ) -> Optional[CmdResult]:
        """Thread-safe version of _run_budgeted_step for parallel execution."""
        # ── 使用者優先：LLM 密集步驟在使用者活躍時自動延後 ──
        if step_name in _LLM_HEAVY_STEPS and _user_active_defer(step_name):
            return None
        with _budget_lock:
            budget = _allocate_step_budget(requested_sec, min_start_sec=min_start_sec)
            if reserve_after_sec > 0 and budget.get("allocated_sec", 0) > 0:
                allocated_after_reserve = max(0, int(budget["allocated_sec"]) - int(reserve_after_sec))
                budget["allocated_sec"] = allocated_after_reserve
                budget["reserved_for_later_sec"] = int(reserve_after_sec)
            if budget["allocated_sec"] < budget["min_start_sec"]:
                _record_skip(step_name, "略過：nightly 剩餘 time budget 不足", budget)
                return None
        # Run outside lock — subprocess can take a long time
        res = _run_cmd(cmd, timeout_sec=budget["allocated_sec"], env=env)
        _stash_cmd_output(run_dir, step_name, cmd, res)
        parsed = res.parsed if isinstance(res.parsed, dict) else {}
        if not isinstance(parsed, dict):
            parsed = {}
        parsed.setdefault("budget", {})
        with _budget_lock:
            remain_after = _remaining_budget_sec()
            if isinstance(parsed.get("budget"), dict):
                parsed["budget"].update({**budget, "remaining_after_sec": remain_after})
            else:
                parsed["budget"] = {**budget, "remaining_after_sec": remain_after}
            results["steps"][step_name] = {
                "ok": res.ok,
                "returncode": res.returncode,
                "parsed": parsed,
                "stderr_tail": (res.stderr or "")[-800:],
            }
        return res

    def _run_parallel_steps(tasks: list) -> dict:
        """
        Run multiple independent steps in parallel.
        tasks: list of (callable, ) where each callable takes no args
               and returns (step_name, result).
        Returns dict of {step_name: result}.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        parallel_results = {}
        max_parallel = int(os.environ.get("MAGI_NIGHTLY_MAX_PARALLEL", "3") or "3")
        with ThreadPoolExecutor(max_workers=max_parallel) as pool:
            futures = {pool.submit(fn): fn for fn in tasks}
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception:
                    logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4456, exc_info=True)
        return parallel_results

    # 3.75) DB bidirectional sync (remote <-> local)
    # Purpose:
    # - Remote DB is source-of-truth when online.
    # - Local DB rows created during remote outage are pushed back automatically.
    try:
        # Keep remote/local DBs converged by default (upsert-only, no delete).
        db_sync_enabled = os.environ.get("MAGI_ENABLE_DB_BIDIR_SYNC", "1").strip().lower() in {"1", "true", "yes", "on"}
        db_sync_script = f"{_MAGI_ROOT}/skills/ops/database/sync_bidirectional.py"
        if db_sync_enabled and os.path.exists(db_sync_script):
            cmd = [
                VENV_PY,
                db_sync_script,
                "--chunk-size",
                str(max(100, _t("MAGI_DB_BIDIR_SYNC_CHUNK_SIZE", 800))),
                "--update-window-days",
                str(max(1, _t("MAGI_DB_BIDIR_SYNC_UPDATE_WINDOW_DAYS", 21))),
                "--recent-limit",
                str(max(200, _t("MAGI_DB_BIDIR_SYNC_RECENT_LIMIT", 5000))),
            ]
            tables_env = str(os.environ.get("MAGI_DB_BIDIR_SYNC_TABLES", "") or "").strip()
            if tables_env:
                cmd.extend(["--tables", tables_env])
            _run_budgeted_step(
                "db_bidirectional_sync",
                cmd,
                _tb("MAGI_NIGHTLY_DB_BIDIR_SYNC_BUDGET_SEC", "MAGI_NIGHTLY_DB_BIDIR_SYNC_TIMEOUT_SEC", 900),
                min_start_sec=20,
                reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
            )
        elif not db_sync_enabled:
            _record_skip(
                "db_bidirectional_sync",
                "略過：MAGI_ENABLE_DB_BIDIR_SYNC=0",
                {
                    "requested_sec": 0,
                    "allocated_sec": 0,
                    "min_start_sec": 1,
                    "remaining_before_sec": _remaining_budget_sec(),
                    "guard_sec": max(0, int(nightly_guard_sec)),
                },
            )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4501, exc_info=True)

    # 3.77) Daily DB backup (remote/local) with rotation
    try:
        db_backup_enabled = os.environ.get("MAGI_ENABLE_DB_DAILY_BACKUP", "1").strip().lower() in {"1", "true", "yes", "on"}
        db_backup_script = f"{_MAGI_ROOT}/skills/ops/database/backup_restore.py"
        if db_backup_enabled and os.path.exists(db_backup_script):
            backup_target = (os.environ.get("MAGI_DB_BACKUP_TARGET", "both") or "both").strip().lower()
            if backup_target not in {"remote", "local", "both"}:
                backup_target = "both"
            try:
                keep_days = int(os.environ.get("MAGI_DB_BACKUP_KEEP_DAYS", "30") or "30")
            except Exception:
                keep_days = 30
            keep_days = max(7, min(3650, keep_days))

            cmd = [
                VENV_PY,
                db_backup_script,
                "--task",
                "backup",
                "--target",
                backup_target,
                "--keep-days",
                str(keep_days),
            ]
            _run_budgeted_step(
                "db_daily_backup",
                cmd,
                _tb("MAGI_NIGHTLY_DB_BACKUP_BUDGET_SEC", "MAGI_NIGHTLY_DB_BACKUP_TIMEOUT_SEC", 900),
                min_start_sec=20,
                reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
            )
        elif not db_backup_enabled:
            _record_skip(
                "db_daily_backup",
                "略過：MAGI_ENABLE_DB_DAILY_BACKUP=0",
                {
                    "requested_sec": 0,
                    "allocated_sec": 0,
                    "min_start_sec": 1,
                    "remaining_before_sec": _remaining_budget_sec(),
                    "guard_sec": max(0, int(nightly_guard_sec)),
                },
            )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4547, exc_info=True)

    # 3.8) NGL auto-calibration for nightly:
    # 先看 big_brain_health，只有不健康時才校準。
    try:
        cal_enabled = os.environ.get("MAGI_NIGHTLY_NGL_CALIBRATE_ENABLE", "1").strip().lower() in {"1", "true", "yes", "on"}
        bb_step = (results.get("steps") or {}).get("big_brain_health") or {}
        has_health_probe = isinstance(bb_step, dict) and bool(bb_step)
        bb_parsed = bb_step.get("parsed") if isinstance(bb_step, dict) else {}
        bb_ok = bool(bb_step.get("ok", False)) and bool((bb_parsed or {}).get("ok", False))

        if cal_enabled and has_health_probe and (not bb_ok):
            budget = _allocate_step_budget(
                _tb("MAGI_NIGHTLY_NGL_CALIBRATE_BUDGET_SEC", "MAGI_NIGHTLY_NGL_CALIBRATE_TIMEOUT_SEC", 420),
                min_start_sec=25,
            )
            if budget["allocated_sec"] < budget["min_start_sec"]:
                _record_skip("nightly_ngl_calibrate", "略過：nightly 剩餘 time budget 不足", budget)
            else:
                parsed: Dict[str, Any] = {}
                ok = True
                stderr_tail = ""
                try:
                    if MAGI_ROOT_DIR not in sys.path:
                        sys.path.insert(0, MAGI_ROOT_DIR)
                    from skills.brain_manager import action as brain_manager  # type: ignore

                    target = float(os.environ.get("MAGI_NGL_TARGET_GB", "8.0") or "8.0")
                    tol = float(os.environ.get("MAGI_NGL_TARGET_TOL_GB", "0.5") or "0.5")
                    rounds_cfg = int(os.environ.get("MAGI_NIGHTLY_NGL_CALIBRATE_MAX_ROUNDS", "4") or "4")
                    # rough per-round estimate: 80 sec
                    dyn_rounds = max(1, min(rounds_cfg, max(1, int(budget["allocated_sec"]) // 80)))
                    init_ngl = int(os.environ.get("MAGI_NIGHTLY_NGL_INITIAL", "-1") or "-1")
                    init_val = None if init_ngl < 0 else init_ngl

                    parsed = brain_manager.calibrate_distributed_ngl(
                        target_gb=target,
                        tolerance_gb=tol,
                        max_rounds=dyn_rounds,
                        min_ngl=int(os.environ.get("MAGI_NGL_MIN", "8") or "8"),
                        max_ngl=int(os.environ.get("MAGI_NGL_MAX", "80") or "80"),
                        initial_ngl=init_val,
                        hard_cycle=True,
                    )
                    parsed["trigger_reason"] = "big_brain_unhealthy"
                    parsed["max_rounds_runtime"] = dyn_rounds
                    _remember_ngl_calibration_event(run_dir, parsed, "big_brain_unhealthy")
                except Exception as e:
                    ok = False
                    stderr_tail = f"{type(e).__name__}: {e}"
                    parsed = {
                        "success": False,
                        "error": stderr_tail,
                        "message": "nightly NGL calibration failed",
                    }

                parsed.setdefault("budget", {})
                if isinstance(parsed.get("budget"), dict):
                    parsed["budget"].update(
                        {
                            **budget,
                            "remaining_after_sec": _remaining_budget_sec(),
                        }
                    )
                else:
                    parsed["budget"] = {**budget, "remaining_after_sec": _remaining_budget_sec()}

                results["steps"]["nightly_ngl_calibrate"] = {
                    "ok": bool(ok),
                    "returncode": 0 if ok else 1,
                    "parsed": parsed,
                    "stderr_tail": stderr_tail[-800:],
                }
        else:
            skip_msg = "略過：big_brain_health 健康，無需校準"
            if not cal_enabled:
                skip_msg = "略過：MAGI_NIGHTLY_NGL_CALIBRATE_ENABLE=0"
            elif not has_health_probe:
                skip_msg = "略過：缺少 big_brain_health 探測結果"
            _record_skip(
                "nightly_ngl_calibrate",
                skip_msg,
                {
                    "requested_sec": 0,
                    "allocated_sec": 0,
                    "min_start_sec": 1,
                    "remaining_before_sec": _remaining_budget_sec(),
                    "guard_sec": max(0, int(nightly_guard_sec)),
                },
            )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4638, exc_info=True)

    # Nightly 補做較完整的案件待辦掃描（仍有 time budget 以免卡死）
    try:
        osc_full_payload = {"max_cases": 80, "max_files_per_case": 80, "time_budget_sec": 1200}
        osc_full_cmd = [VENV_PY, _skill_action("osc-orchestrator"), "--task", "scan_cases " + json.dumps(osc_full_payload, ensure_ascii=False)]
        _run_budgeted_step(
            "osc_scan_cases_full",
            osc_full_cmd,
            _tb("MAGI_NIGHTLY_OSC_FULL_BUDGET_SEC", "MAGI_NIGHTLY_OSC_FULL_TIMEOUT_SEC", 1200),
            min_start_sec=30,
            reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
        )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4652, exc_info=True)

    # 4.5) Index cases from Synology Drive into local DB (enables transcript downloader, queries)
    try:
        idx_payload = {
            "max_cases": int(os.environ.get("MAGI_NIGHTLY_CASE_INDEX_MAX_CASES", "220") or "220"),
            "max_files_per_case": int(os.environ.get("MAGI_NIGHTLY_CASE_INDEX_MAX_FILES", "250") or "250"),
            "dry_run": False,
        }
        idx_cmd = [VENV_PY, _skill_action("osc-orchestrator"), "--task", "index_cases " + json.dumps(idx_payload, ensure_ascii=False)]
        _run_budgeted_step(
            "osc_index_cases",
            idx_cmd,
            _tb("MAGI_NIGHTLY_CASE_INDEX_BUDGET_SEC", "MAGI_NIGHTLY_CASE_INDEX_TIMEOUT_SEC", 900),
            min_start_sec=30,
            reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
        )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4670, exc_info=True)

    # 5) File review pipeline（三段：信件檢查 -> 可下載判定 -> 下載）
    fr_scan_enabled = os.environ.get("MAGI_ENABLE_FILE_REVIEW_SCAN", "1").strip().lower() in {"1", "true", "yes", "on"}
    if fr_scan_enabled:
        fr_scan_cmd = [VENV_PY, _skill_action("file-review-orchestrator"), "--task", "check_emails"]
        _run_budgeted_step(
            "file_review_email_scan",
            fr_scan_cmd,
            _tb("MAGI_NIGHTLY_FILE_REVIEW_SCAN_BUDGET_SEC", "MAGI_NIGHTLY_FILE_REVIEW_EMAIL_TIMEOUT_SEC", 600),
            min_start_sec=20,
            reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
        )
    else:
        _record_skip(
            "file_review_email_scan",
            "略過：MAGI_ENABLE_FILE_REVIEW_SCAN=0（SLO 降級）",
            {
                "requested_sec": 0,
                "allocated_sec": 0,
                "min_start_sec": 1,
                "remaining_before_sec": _remaining_budget_sec(),
                "guard_sec": max(0, int(nightly_guard_sec)),
            },
        )

    fr_probe_cmd = [VENV_PY, _skill_action("file-review-orchestrator"), "--task", "downloadable_probe"]
    fr_probe = _run_budgeted_step(
        "file_review_downloadable_probe",
        fr_probe_cmd,
        _tb("MAGI_NIGHTLY_FILE_REVIEW_PROBE_BUDGET_SEC", "MAGI_NIGHTLY_FILE_REVIEW_PREVIEW_TIMEOUT_SEC", 480),
        min_start_sec=20,
        reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
    )

    fr_dl_cmd = [VENV_PY, _skill_action("file-review-orchestrator"), "--task", "download"]
    fr_env = dict(os.environ)
    # 夜間任務避免卡住：不允許人工驗證碼回傳；OCR 失敗則直接回報失敗即可。
    fr_env.setdefault("MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK", "0")
    fr_env.setdefault("MAGI_CAPTCHA_DOUBLE_CHECK", "1")
    run_fr_download = True
    if isinstance(fr_probe, CmdResult) and isinstance(fr_probe.parsed, dict):
        try:
            probe_count = int(fr_probe.parsed.get("count") or 0)
        except Exception:
            probe_count = -1
        if probe_count == 0:
            run_fr_download = False
            _record_skip(
                "file_review_download",
                "略過：downloadable_probe 判定目前無可下載通知",
                {
                    "requested_sec": 0,
                    "allocated_sec": 0,
                    "min_start_sec": 1,
                    "remaining_before_sec": _remaining_budget_sec(),
                    "guard_sec": max(0, int(nightly_guard_sec)),
                    "depends_on": "file_review_downloadable_probe",
                },
            )
    fr_download_enabled = os.environ.get("MAGI_ENABLE_FILE_REVIEW_DOWNLOAD", "1").strip().lower() in {"1", "true", "yes", "on"}
    if run_fr_download and fr_download_enabled:
        _run_budgeted_step(
            "file_review_download",
            fr_dl_cmd,
            _tb("MAGI_NIGHTLY_FILE_REVIEW_DOWNLOAD_BUDGET_SEC", "MAGI_NIGHTLY_FILE_REVIEW_DOWNLOAD_TIMEOUT_SEC", 1200),
            env=fr_env,
            min_start_sec=30,
            reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
        )
    elif not fr_download_enabled:
        _record_skip(
            "file_review_download",
            "略過：MAGI_ENABLE_FILE_REVIEW_DOWNLOAD=0（SLO 降級）",
            {
                "requested_sec": 0,
                "allocated_sec": 0,
                "min_start_sec": 1,
                "remaining_before_sec": _remaining_budget_sec(),
                "guard_sec": max(0, int(nightly_guard_sec)),
            },
        )

    # 6) Transcript flow（db_probe + sync，分段 budget）
    tr_probe_cmd = [VENV_PY, _skill_action("transcript-downloader"), "--task", "db_probe"]
    tr_probe = _run_budgeted_step(
        "transcript_db_probe",
        tr_probe_cmd,
        _tb("MAGI_NIGHTLY_TRANSCRIPT_DB_PROBE_BUDGET_SEC", "MAGI_NIGHTLY_TRANSCRIPT_DB_PROBE_TIMEOUT_SEC", 120),
        min_start_sec=15,
        reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
    )

    tr_should_sync = True
    if isinstance(tr_probe, CmdResult) and isinstance(tr_probe.parsed, dict):
        try:
            eligible = int(tr_probe.parsed.get("eligible_cases") or 0)
            if eligible <= 0:
                tr_should_sync = False
                _record_skip(
                    "transcript_sync",
                    "略過：筆錄 db_probe 顯示目前無可同步案件",
                    {
                        "requested_sec": 0,
                        "allocated_sec": 0,
                        "min_start_sec": 1,
                        "remaining_before_sec": _remaining_budget_sec(),
                        "guard_sec": max(0, int(nightly_guard_sec)),
                    },
                )
        except Exception:
            tr_should_sync = True

    tr_cmd = [VENV_PY, _skill_action("transcript-downloader"), "--task", "sync"]
    tr_env = dict(os.environ)
    tr_env["MAGI_EZLAWYER_SOLVE_CAPTCHA"] = "0"
    tr_env["MAGI_EZLAWYER_ASSUME_CAPTCHA_REQUIRED"] = "0"
    tr_env["MAGI_ALLOW_HUMAN_CAPTCHA_FALLBACK"] = "0"
    tr_enabled = os.environ.get("MAGI_ENABLE_TRANSCRIPT_SYNC", "1").strip().lower() in {"1", "true", "yes", "on"}
    if tr_should_sync and tr_enabled:
        tr_cd = _transcript_captcha_cooldown_state()
        if bool(tr_cd.get("active")):
            _record_skip(
                "transcript_sync",
                f"略過：captcha cooldown 中（剩餘約 {int(tr_cd.get('remaining_minutes', 0))} 分鐘）",
                {
                    "requested_sec": 0,
                    "allocated_sec": 0,
                    "min_start_sec": 1,
                    "remaining_before_sec": _remaining_budget_sec(),
                    "guard_sec": max(0, int(nightly_guard_sec)),
                    "captcha_cooldown": tr_cd,
                },
            )
            results.setdefault("warnings", []).append("transcript_sync: captcha_cooldown_skip")
        else:
            _run_budgeted_step(
                "transcript_sync",
                tr_cmd,
                _tb("MAGI_NIGHTLY_TRANSCRIPT_BUDGET_SEC", "MAGI_NIGHTLY_TRANSCRIPT_TIMEOUT_SEC", 900),
                env=tr_env,
                min_start_sec=45,
                reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
            )
    elif not tr_enabled:
        _record_skip(
            "transcript_sync",
            "略過：MAGI_ENABLE_TRANSCRIPT_SYNC=0（SLO 降級）",
            {
                "requested_sec": 0,
                "allocated_sec": 0,
                "min_start_sec": 1,
                "remaining_before_sec": _remaining_budget_sec(),
                "guard_sec": max(0, int(nightly_guard_sec)),
            },
        )

    # 7) 司法院 API 夜拉 — 已移至 run_nightly() 獨立執行緒，00:00 準時啟動，不排隊

    # 7.2) Judgment crawl（既有案由模式，與官方 API 並行）
    jc_path = _skill_action("judgment-collector")
    judgment_enabled = os.environ.get("MAGI_ENABLE_JUDGMENT_CRAWL", "1").strip().lower() in {"1", "true", "yes", "on"}
    if _user_active_defer("judgment_daily_crawl"):
        pass  # 使用者活躍中，已自動延後
    elif os.path.exists(jc_path) and judgment_enabled:
        jc_cmd = [VENV_PY, jc_path, "--task", "daily_crawl"]
        _run_budgeted_step(
            "judgment_daily_crawl",
            jc_cmd,
            _tb("MAGI_NIGHTLY_JUDGMENT_BUDGET_SEC", "MAGI_NIGHTLY_JUDGMENT_TIMEOUT_SEC", 1200),
            min_start_sec=30,
            reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
        )
    elif not judgment_enabled:
        _record_skip(
            "judgment_daily_crawl",
            "略過：MAGI_ENABLE_JUDGMENT_CRAWL=0（SLO 降級）",
            {
                "requested_sec": 0,
                "allocated_sec": 0,
                "min_start_sec": 1,
                "remaining_before_sec": _remaining_budget_sec(),
                "guard_sec": max(0, int(nightly_guard_sec)),
            },
        )

    # 7.25) 週末重型判決摘要修復（修 legal_insights 中遺留的 broken summaries）
    weekend_insight_repair_enabled = os.environ.get(
        "MAGI_ENABLE_WEEKEND_INSIGHT_REPAIR", "1"
    ).strip().lower() in {"1", "true", "yes", "on"}
    now_dt = datetime.now()
    is_weekend = now_dt.weekday() >= 5
    repair_script = os.path.join(MAGI_ROOT_DIR, "scripts", "ops", "repair_insight_summaries.py")
    if _user_active_defer("weekend_insight_repair"):
        pass  # 使用者活躍中，已自動延後
    elif weekend_insight_repair_enabled and is_weekend and os.path.exists(repair_script):
        repair_limit = _t("MAGI_WEEKEND_INSIGHT_REPAIR_LIMIT", 24)
        repair_timeout = _t("MAGI_WEEKEND_INSIGHT_REPAIR_TIMEOUT_SEC", 540)
        repair_cmd = [
            VENV_PY,
            repair_script,
            "--limit",
            str(max(1, repair_limit)),
            "--timeout",
            str(max(120, repair_timeout)),
        ]
        repair_env = os.environ.copy()
        repair_env.setdefault("MAGI_PREFER_LOCAL_DB", "0")
        _run_budgeted_step(
            "weekend_insight_repair",
            repair_cmd,
            _tb(
                "MAGI_NIGHTLY_WEEKEND_INSIGHT_REPAIR_BUDGET_SEC",
                "MAGI_NIGHTLY_WEEKEND_INSIGHT_REPAIR_TIMEOUT_SEC",
                max(600, repair_timeout + 60),
            ),
            env=repair_env,
            min_start_sec=30,
            reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
        )
    elif weekend_insight_repair_enabled and (not is_weekend):
        _record_skip(
            "weekend_insight_repair",
            "略過：非週末時段",
            {
                "requested_sec": 0,
                "allocated_sec": 0,
                "min_start_sec": 1,
                "remaining_before_sec": _remaining_budget_sec(),
                "guard_sec": max(0, int(nightly_guard_sec)),
            },
        )

    # ========================================================================
    # 7.3–8.5) PARALLEL GROUP: Independent maintenance steps
    # These steps are fully independent and can run concurrently to save time.
    # ========================================================================
    nightly_parallel_enabled = os.environ.get(
        "MAGI_NIGHTLY_PARALLEL", "1"
    ).strip().lower() in {"1", "true", "yes", "on"}

    # Pre-compute statutes VDB payload (depends on earlier osc_scan results, read before parallel launch)
    _svdb_case_list = []
    try:
        svdb_path = _skill_action("statutes-vdb")
        if os.path.exists(svdb_path):
            osc_parsed = (results.get("steps", {}).get("osc_scan_cases_full") or results.get("steps", {}).get("osc_scan_cases") or {}).get("parsed") or {}
            for r in (osc_parsed.get("results") or []):
                cn = (r.get("case_number") or "").strip()
                cp = (r.get("case_path") or "").strip()
                if cn and cp:
                    _svdb_case_list.append({"case_number": cn, "case_path": cp})
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4923, exc_info=True)

    def _step_style_learning():
        style_learn_path = os.path.join(SCRIPT_DIR, "..", "osc-draft", "learn_style.py")
        style_enabled = os.environ.get("MAGI_ENABLE_STYLE_LEARNING", "1").strip().lower() in {"1", "true", "yes", "on"}
        if os.path.exists(style_learn_path) and style_enabled:
            style_cmd = [VENV_PY, style_learn_path, "--limit", "10"]
            _run_budgeted_step_ts("writing_style_learning", style_cmd,
                _tb("MAGI_NIGHTLY_STYLE_LEARN_BUDGET_SEC", "MAGI_NIGHTLY_STYLE_LEARN_TIMEOUT_SEC", 300),
                min_start_sec=10)
        elif not style_enabled:
            with _budget_lock:
                _record_skip("writing_style_learning", "略過：MAGI_ENABLE_STYLE_LEARNING=0",
                    {"requested_sec": 0, "allocated_sec": 0, "min_start_sec": 1,
                     "remaining_before_sec": _remaining_budget_sec(), "guard_sec": max(0, int(nightly_guard_sec))})

    def _step_iron_dome():
        try:
            id_cmd = [VENV_PY, _skill_action("iron-dome"), "self_test"]
            _run_budgeted_step_ts("iron_dome_self_test", id_cmd,
                _tb("MAGI_NIGHTLY_IRON_DOME_BUDGET_SEC", "MAGI_NIGHTLY_IRON_DOME_TIMEOUT_SEC", 120),
                min_start_sec=10)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4946, exc_info=True)

    def _step_image_smoke():
        try:
            if os.environ.get("MAGI_ENABLE_IMAGE_SMOKE", "1").strip().lower() in {"1", "true", "yes", "on"}:
                smoke_prompt = os.environ.get("MAGI_IMAGE_SMOKE_PROMPT", "簡約黑白測試圖示，一個法槌與天平").strip()
                smoke_timeout = _t("MAGI_NIGHTLY_IMAGE_SMOKE_TIMEOUT_SEC", 240)
                smoke_code = (
                    "import json,sys;"
                    "sys.path.insert(0,str(_MAGI_ROOT));"
                    "from skills.bridge.tri_sage_collab import generate_image;"
                    f"r=generate_image({json.dumps(smoke_prompt, ensure_ascii=False)});"
                    "print(json.dumps(r, ensure_ascii=False))"
                )
                img_cmd = [VENV_PY, "-c", smoke_code]
                _run_budgeted_step_ts("image_generation_smoke", img_cmd,
                    _tb("MAGI_NIGHTLY_IMAGE_SMOKE_BUDGET_SEC", "MAGI_NIGHTLY_IMAGE_SMOKE_TIMEOUT_SEC", smoke_timeout),
                    min_start_sec=20)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4965, exc_info=True)

    def _step_statutes_vdb():
        try:
            if _svdb_case_list:
                svdb_payload = json.dumps({"cases": _svdb_case_list}, ensure_ascii=False)
                svdb_cmd = [VENV_PY, _skill_action("statutes-vdb"), "--task", "update_cases " + svdb_payload]
                _run_budgeted_step_ts("statutes_vdb_update", svdb_cmd,
                    _tb("MAGI_NIGHTLY_STATUTES_BUDGET_SEC", "MAGI_NIGHTLY_STATUTES_TIMEOUT_SEC", 600),
                    min_start_sec=25)
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 4976, exc_info=True)

    def _step_faiss_evict():
        try:
            faiss_enabled = os.environ.get("MAGI_ENABLE_FAISS_EVICT", "1").strip().lower() in {"1", "true", "yes", "on"}
            if faiss_enabled:
                faiss_evict_code = "\n".join([
                    "import json", "import sys",
                    "sys.path.insert(0, str(_MAGI_ROOT))",
                    "out = {'ok': True, 'skipped': False}",
                    "try:",
                    "    from skills.memory import mem_bridge",
                    "    idx = mem_bridge._get_faiss_index()",
                    "    db_cfg = mem_bridge.DB_CONFIG",
                    "    idx.rebuild_if_needed(db_cfg, hours_threshold=23.0)",
                    "    out['message'] = 'faiss_evict_done'",
                    "except ModuleNotFoundError as e:",
                    "    out = {'ok': True, 'skipped': True, 'reason': 'faiss_not_installed', 'error': str(e)}",
                    "except Exception as e:",
                    "    out = {'ok': True, 'skipped': True, 'reason': 'faiss_evict_error', 'error': str(e)}",
                    "print(json.dumps(out, ensure_ascii=False))",
                ])
                faiss_evict_cmd = ["python3", "-c", faiss_evict_code]
                _run_budgeted_step_ts("faiss_phantom_memory_eviction", faiss_evict_cmd,
                    _tb("MAGI_NIGHTLY_FAISS_EVICT_BUDGET_SEC", "MAGI_NIGHTLY_FAISS_EVICT_TIMEOUT_SEC", 60),
                    min_start_sec=10)
            else:
                with _budget_lock:
                    _record_skip("faiss_phantom_memory_eviction", "略過：MAGI_ENABLE_FAISS_EVICT=0",
                        {"requested_sec": 0, "allocated_sec": 0, "min_start_sec": 10,
                         "remaining_before_sec": _remaining_budget_sec(), "guard_sec": max(0, int(nightly_guard_sec))})
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5008, exc_info=True)

    parallel_tasks = [_step_style_learning, _step_iron_dome, _step_image_smoke,
                      _step_statutes_vdb, _step_faiss_evict]

    if nightly_parallel_enabled:
        _run_parallel_steps(parallel_tasks)
    else:
        # Fallback: sequential execution
        for fn in parallel_tasks:
            try:
                fn()
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5021, exc_info=True)

    # 8.7) Daily Reflection (Heartbeat + daily_reflection.py)
    try:
        dr_res = _step_daily_reflection(run_dir)
        results["steps"]["daily_reflection"] = {
            "ok": dr_res.ok,
            "returncode": dr_res.returncode,
            "parsed": dr_res.parsed,
            "stderr_tail": (dr_res.stderr or "")[-800:],
        }
    except Exception as e:
        results["steps"]["daily_reflection"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}


    # Final flush
    osc_flush2_cmd = [VENV_PY, _skill_action("osc-orchestrator"), "--task", "queue_flush {}"]
    _run_budgeted_step(
        "osc_queue_flush_final",
        osc_flush2_cmd,
        _tb("MAGI_NIGHTLY_OSC_FLUSH_FINAL_BUDGET_SEC", "MAGI_NIGHTLY_OSC_FLUSH_FINAL_TIMEOUT_SEC", 120),
        min_start_sec=10,
    )

    # 9) Google Calendar sync (best-effort, non-blocking)
    # Do not wake you up at night if OAuth isn't ready yet; just record the failure.
    try:
        if os.environ.get("MAGI_ENABLE_GCAL_SYNC", "1").strip() in {"1", "true", "yes", "on"}:
            gcal_limit = int(os.environ.get("MAGI_GCAL_SYNC_LIMIT", "60") or "60")
            gcal_retry_max_attempts = int(os.environ.get("OSC_GCAL_RETRY_MAX_ATTEMPTS", "3") or "3")
            gcal_retry_sleep_sec = float(os.environ.get("OSC_GCAL_RETRY_SLEEP_SEC", "1.0") or "1.0")
            gcal_payload = {
                "limit": gcal_limit,
                "retry_max_attempts": max(1, min(gcal_retry_max_attempts, 4)),
                "retry_sleep_sec": max(0.0, min(gcal_retry_sleep_sec, 5.0)),
            }
            gcal_cmd = [VENV_PY, _skill_action("osc-orchestrator"), "--task", "gcal_sync " + json.dumps(gcal_payload, ensure_ascii=False)]
            _run_budgeted_step(
                "osc_gcal_sync",
                gcal_cmd,
                _tb("MAGI_NIGHTLY_GCAL_BUDGET_SEC", "MAGI_NIGHTLY_GCAL_TIMEOUT_SEC", 600),
                min_start_sec=20,
                reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
            )
            try:
                step = (results.get("steps") or {}).get("osc_gcal_sync") or {}
                parsed = step.get("parsed") if isinstance(step, dict) else {}
                if isinstance(parsed, dict) and (
                    bool(parsed.get("need_interactive_oauth"))
                    or str(parsed.get("error") or "").strip().lower() == "need_interactive_oauth"
                ):
                    qpath = _queue_gcal_oauth_defer(run_dir, limit=gcal_limit, parsed=parsed)
                    parsed["deferred"] = True
                    parsed["defer_reason"] = "need_interactive_oauth"
                    parsed["defer_queue_path"] = qpath
                    parsed["message"] = "Google Calendar OAuth 未就緒，已寫入降級佇列，待授權後補同步。"
                    step["ok"] = True
                    step["returncode"] = 0
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5080, exc_info=True)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5082, exc_info=True)

    # 9.5) Google Calendar import（手動事件→DB，best-effort，不阻塞）
    try:
        if os.environ.get("MAGI_ENABLE_GCAL_SYNC", "1").strip() in {"1", "true", "yes", "on"}:
            gcal_import_payload = json.dumps({"lookback_days": 30, "lookahead_days": 180, "limit": 250}, ensure_ascii=False)
            gcal_import_cmd = [VENV_PY, _skill_action("osc-orchestrator"), "--task", f"gcal_import {gcal_import_payload}"]
            _run_budgeted_step(
                "osc_gcal_import",
                gcal_import_cmd,
                _tb("MAGI_NIGHTLY_GCAL_BUDGET_SEC", "MAGI_NIGHTLY_GCAL_TIMEOUT_SEC", 300),
                min_start_sec=5,
                reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
            )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5097, exc_info=True)

    # 9.8) 開庭提醒（掃描 case_todos，發送前一天/當天提醒）
    try:
        hearing_enabled = os.environ.get("MAGI_ENABLE_HEARING_REMINDER", "1").strip().lower() in {"1", "true", "yes", "on"}
        hr_path = _skill_action("court-hearing-reminder")
        if hearing_enabled and os.path.exists(hr_path):
            hr_cmd = [VENV_PY, hr_path, "--task", "remind", "--notify", "1"]
            _run_budgeted_step(
                "court_hearing_reminder",
                hr_cmd,
                _tb("MAGI_NIGHTLY_HEARING_REMIND_BUDGET_SEC", "MAGI_NIGHTLY_HEARING_REMIND_TIMEOUT_SEC", 120),
                min_start_sec=10,
                reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
            )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5113, exc_info=True)

    # 10.5) SLO guard snapshot（每日穩定度快照，僅摘要通知，不阻塞）
    try:
        slo_enabled = os.environ.get("MAGI_ENABLE_SLO_GUARD", "1").strip().lower() in {"1", "true", "yes", "on"}
        if slo_enabled:
            slo_script = _resolve_slo_guard_script()
            if slo_script:
                slo_hours = int(os.environ.get("MAGI_SLO_GUARD_HOURS", "72") or "72")
                slo_cmd = [VENV_PY, slo_script, "--hours", str(max(24, min(slo_hours, 336)))]
                _run_budgeted_step(
                    "slo_guard_snapshot",
                    slo_cmd,
                    _tb("MAGI_NIGHTLY_SLO_GUARD_BUDGET_SEC", "MAGI_NIGHTLY_SLO_GUARD_TIMEOUT_SEC", 120),
                    min_start_sec=8,
                )
            else:
                _record_skip(
                    "slo_guard_snapshot",
                    "略過：未找到 slo_guard.py",
                    {
                        "requested_sec": 0,
                        "allocated_sec": 0,
                        "min_start_sec": 1,
                        "remaining_before_sec": _remaining_budget_sec(),
                        "guard_sec": max(0, int(nightly_guard_sec)),
                    },
                )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5142, exc_info=True)

    # 10.6) SLO guard snapshot 24h（短視窗）
    try:
        slo_24_enabled = os.environ.get("MAGI_ENABLE_SLO_GUARD_24H", "1").strip().lower() in {"1", "true", "yes", "on"}
        if slo_24_enabled:
            slo_script = _resolve_slo_guard_script()
            if slo_script:
                slo_cmd_24 = [VENV_PY, slo_script, "--hours", "24"]
                _run_budgeted_step(
                    "slo_guard_snapshot_24h",
                    slo_cmd_24,
                    _tb("MAGI_NIGHTLY_SLO_GUARD_24H_BUDGET_SEC", "MAGI_NIGHTLY_SLO_GUARD_24H_TIMEOUT_SEC", 90),
                    min_start_sec=8,
                )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5158, exc_info=True)

    # 10.7) Optional: auto-apply SLO degrade profile (only when signals indicate real instability)
    try:
        auto_apply = os.environ.get("MAGI_AUTO_APPLY_SLO_DEGRADE", "1").strip().lower() in {"1", "true", "yes", "on"}
        if auto_apply:
            steps_ref = results.get("steps") or {}
            p24 = ((steps_ref.get("slo_guard_snapshot_24h") or {}).get("parsed") if isinstance(steps_ref.get("slo_guard_snapshot_24h"), dict) else {}) or {}
            p72 = ((steps_ref.get("slo_guard_snapshot") or {}).get("parsed") if isinstance(steps_ref.get("slo_guard_snapshot"), dict) else {}) or {}
            try:
                min_blocked_24h = int(os.environ.get("MAGI_AUTO_APPLY_SLO_MIN_BLOCKED_24H", "1") or "1")
            except Exception:
                min_blocked_24h = 1
            if _should_auto_apply_slo_degrade(p24, p72, min_blocked_24h=min_blocked_24h):
                slo_script = _resolve_slo_guard_script()
                if slo_script:
                    apply_hours = int(os.environ.get("MAGI_SLO_GUARD_HOURS", "72") or "72")
                    apply_cmd = [VENV_PY, slo_script, "--hours", str(max(24, min(apply_hours, 336))), "--apply"]
                    _run_budgeted_step(
                        "slo_guard_apply",
                        apply_cmd,
                        _tb("MAGI_NIGHTLY_SLO_GUARD_APPLY_BUDGET_SEC", "MAGI_NIGHTLY_SLO_GUARD_APPLY_TIMEOUT_SEC", 120),
                        min_start_sec=8,
                    )
            else:
                _record_skip(
                    "slo_guard_apply",
                    "略過：SLO 未達自動降級門檻",
                    {
                        "requested_sec": 0,
                        "allocated_sec": 0,
                        "min_start_sec": 1,
                        "remaining_before_sec": _remaining_budget_sec(),
                        "guard_sec": max(0, int(nightly_guard_sec)),
                    },
                )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5195, exc_info=True)

    # 10) User crawl targets (best-effort, non-blocking)
    try:
        ct_path = _skill_action("crawler-targets")
        if os.path.exists(ct_path):
            ct_payload = {"max_targets": int(os.environ.get("MAGI_CRAWL_TARGETS_MAX", "20") or "20"), "max_sections": 10}
            ct_cmd = [VENV_PY, ct_path, "--task", "run_daily " + json.dumps(ct_payload, ensure_ascii=False)]
            _run_budgeted_step(
                "crawl_targets_daily",
                ct_cmd,
                _tb("MAGI_NIGHTLY_CRAWL_TARGETS_BUDGET_SEC", "MAGI_NIGHTLY_CRAWL_TARGETS_TIMEOUT_SEC", 900),
                min_start_sec=20,
                reserve_after_sec=(reserve_iron_dome_sec + reserve_final_flush_sec),
            )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5211, exc_info=True)

    # 10.5) Iron Dome upstream rule fetch (best-effort, non-blocking)
    # Requires IRON_DOME_UPSTREAM_URL set in .env; silently skips if not configured.
    try:
        upstream_url = os.environ.get("IRON_DOME_UPSTREAM_URL", "").strip()
        if upstream_url:
            iron_dome_fetch_cmd = [
                VENV_PY, _skill_action("iron-dome"),
                "sync", "fetch_upstream",
            ]
            _run_budgeted_step(
                "iron_dome_fetch_upstream",
                iron_dome_fetch_cmd,
                _tb("MAGI_NIGHTLY_IRON_DOME_FETCH_BUDGET_SEC", "MAGI_NIGHTLY_IRON_DOME_FETCH_TIMEOUT_SEC", 60),
                min_start_sec=10,
                reserve_after_sec=reserve_final_flush_sec,
            )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5230, exc_info=True)

    # 10.6) OpenClaw self-update (best-effort, non-blocking)
    # Controlled by MAGI_OPENCLAW_AUTO_UPDATE=1 (default) in .env
    try:
        oc_auto = os.environ.get("MAGI_OPENCLAW_AUTO_UPDATE", "1").strip().lower() in {"1", "true", "yes", "on"}
        if oc_auto:
            oc_update_cmd = [
                VENV_PY, "-c",
                (
                    "import sys; sys.path.insert(0, str(_MAGI_ROOT)); "
                    "from skills.ops.openclaw_updater import update_openclaw; "
                    "import json; print(json.dumps(update_openclaw(auto=True), ensure_ascii=False))"
                ),
            ]
            _run_budgeted_step(
                "openclaw_self_update",
                oc_update_cmd,
                _tb("MAGI_NIGHTLY_OPENCLAW_UPDATE_BUDGET_SEC", "MAGI_NIGHTLY_OPENCLAW_UPDATE_TIMEOUT_SEC", 180),
                min_start_sec=10,
                reserve_after_sec=reserve_final_flush_sec,
            )
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5253, exc_info=True)

    # --- Night Talk (三哲人議會) ---
    # 整合到 nightly 流程尾段，僅在三哲人可用時召開。
    try:
        night_talk_enabled = os.environ.get("MAGI_ENABLE_NIGHT_TALK", "1").strip().lower() in {"1", "true", "yes", "on"}
        if night_talk_enabled:
            budget_nt = _allocate_step_budget(
                _t("MAGI_NIGHTLY_NIGHT_TALK_BUDGET_SEC", 600), min_start_sec=60
            )
            if budget_nt["allocated_sec"] >= budget_nt["min_start_sec"]:
                try:
                    from skills.magi.night_talk import start_night_talk
                    minutes = start_night_talk()
                    results["steps"]["night_talk"] = {
                        "ok": True,
                        "parsed": {
                            "success": True,
                            "minutes_length": len(minutes or ""),
                            "budget": {**budget_nt, "remaining_after_sec": _remaining_budget_sec()},
                        },
                    }
                    # 寫入議事錄
                    try:
                        minutes_path = os.path.join(run_dir, "night_talk_minutes.md")
                        with open(minutes_path, "w", encoding="utf-8") as f:
                            f.write(minutes or "(empty)")
                    except Exception:
                        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5281, exc_info=True)
                except Exception as e:
                    results["steps"]["night_talk"] = {
                        "ok": False,
                        "parsed": {"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"},
                    }
            else:
                _record_skip("night_talk", "略過：nightly 剩餘 time budget 不足", budget_nt)
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5290, exc_info=True)

    # "ok" means: no human intervention needed (blockers empty).
    results["ok"] = (not results.get("blocked"))
    _remember_step_events("nightly", run_dir, results.get("steps") or {})
    return results


def _step_daily_reflection(run_dir: str) -> CmdResult:
    log_file = os.path.join(run_dir, "daily_reflection.log")
    with open(log_file, "w") as f:
        f.write("Starting daily reflection and updating HEARTBEAT.md...\n")

    # Update MAGI System Heartbeat
    try:
        from skills.bridge import melchior_client
        def get_balthasar_status(): return "🟢 Active"
        def get_casper_status(): return "🟢 Active"
        def get_melchior_status():
            try:
                r = melchior_client.get_capabilities()
                if r:
                    m = getattr(r, "get", lambda x, y: y)("model", "Unknown")
                    return f"🟢 Active (Model: {m})"
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5315, exc_info=True)
            return "🔴 Offline"

        status_content = f"""# MAGI System Heartbeat

## System Philosophers
* **Balthasar (Planning/Coding)**: {get_balthasar_status()}
* **Casper (Memory/Context)**: {get_casper_status()}
* **Melchior (Evaluation/Chat)**: {get_melchior_status()}
* **Last Updated**: {_now_tag()}
"""
        heartbeat_path = "/Users/ai/.openclaw/workspace/HEARTBEAT.md"
        with open(heartbeat_path, "w") as f:
            f.write(status_content)
        with open(log_file, "a") as f:
            f.write(f"Updated {heartbeat_path}\n")
    except Exception as e:
        with open(log_file, "a") as f:
            f.write(f"Failed to update HEARTBEAT.md: {e}\n")

    # Run daily reflection script
    script_path = os.path.join(MAGI_ROOT_DIR, "skills", "ops", "daily_reflection.py")
    if os.path.exists(script_path):
        res = _run_cmd(
            [sys.executable, script_path],
            timeout_sec=900
        )
        with open(log_file, "a") as f:
            f.write(f"\n[STDOUT]\n{res.stdout}\n[STDERR]\n{res.stderr}\n")
        return res
    else:
        return CmdResult(ok=False, returncode=1, stdout="", stderr=f"Script not found: {script_path}")


def main() -> int:
    _load_runtime_env()
    _maybe_reexec_venv()
    _ensure_dirs()

    # Single-instance lock (with stale-lock detection)
    import fcntl
    lock_file = os.path.join(MAGI_ROOT_DIR, "_autopilot.lock")
    fp = open(lock_file, "w+")
    try:
        fcntl.lockf(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        # Check if holder is stale (PID dead or running > 2 hours)
        _skip = True
        try:
            fp_r = open(lock_file, "r")
            holder_info = fp_r.read().strip()
            fp_r.close()
            if holder_info:
                parts = holder_info.split(",")
                holder_pid = int(parts[0]) if parts else 0
                holder_ts = float(parts[1]) if len(parts) > 1 else 0
                pid_alive = True
                if holder_pid:
                    try:
                        os.kill(holder_pid, 0)
                    except OSError:
                        pid_alive = False
                elapsed_h = (time.time() - holder_ts) / 3600 if holder_ts else 999
                if not pid_alive or elapsed_h > 2:
                    # Stale lock — force acquire
                    try:
                        if holder_pid and pid_alive:
                            try:
                                write_kill_reason(
                                    holder_pid,
                                    f"新 autopilot 實例啟動，舊實例鎖定超時（已執行 {elapsed_h:.1f}h > 2h）被強制取代",
                                    root=MAGI_ROOT_DIR,
                                )
                            except Exception:
                                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5387, exc_info=True)
                            os.kill(holder_pid, signal.SIGTERM)
                            time.sleep(2)
                    except OSError:
                        pass
                    fp.close()
                    fp = open(lock_file, "w")
                    fcntl.lockf(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    _skip = False
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5397, exc_info=True)
        if _skip:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "skipped": True,
                        "reason": "another_instance_running",
                        "message": "已有另一個 autopilot 執行中，本次略過避免重疊",
                    },
                    ensure_ascii=False,
                )
            )
            return 0  # Exit silently to avoid launchd spasm

    # Write PID + timestamp into lock file for stale detection
    fp.seek(0)
    fp.truncate()
    fp.write(f"{os.getpid()},{time.time()}")
    fp.flush()

    parser = argparse.ArgumentParser(description="magi-autopilot")
    parser.add_argument("--task", required=True, help="tick|nightly|self_test|help")
    args = parser.parse_args()

    task = (args.task or "").strip()
    if task in ("help", "--help", "-h"):
        print(json.dumps({"ok": True, "tasks": ["help", "self_test", "tick", "nightly"]}, ensure_ascii=False))
        fp.close()
        return 0

    # Overall timeout (env-configurable; nightly needs more time than tick)
    _default_timeout = "21600" if task.strip().lower().startswith("nightly") else "5400"
    _overall_timeout_sec = int(os.environ.get("MAGI_AUTOPILOT_OVERALL_TIMEOUT_SEC", _default_timeout))

    def _timeout_handler(signum, frame):
        raise TimeoutError(f"autopilot {task} 超過整體時限 {_overall_timeout_sec}s，強制中止")

    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(_overall_timeout_sec)

    run_task = task.split()[0].strip().lower()
    run_dir = os.path.join(RUNS_DIR, f"{_now_tag()}_{run_task}")
    os.makedirs(run_dir, exist_ok=True)

    report: Dict[str, Any] = {
        "task": run_task,
        "ts": datetime.now().isoformat(),
        "run_dir": run_dir,
        "ok": True,
        "summary": "",
        "details": {},
    }
    report_written = False

    def _write_partial_report(reason: str, *, error: str = "", traceback_text: str = "") -> tuple[str, str]:
        nonlocal report_written, report
        if report_written:
            return os.path.join(run_dir, "report.json"), os.path.join(run_dir, "report.txt")
        details = report.get("details") if isinstance(report.get("details"), dict) else {}
        details = dict(details or {})
        details.setdefault("partial", True)
        details.setdefault("partial_reason", str(reason or "interrupted"))
        if error:
            details["error"] = error
        if traceback_text:
            details["traceback"] = traceback_text
        report["details"] = details
        report["ok"] = False
        report["summary"] = f"中止（partial report，原因：{reason}）"
        json_path, txt_path = _write_report(run_dir, report)
        report_written = True
        return json_path, txt_path

    def _read_kill_reason() -> str:
        """讀取外部程序在發送 SIGTERM 前寫入的中斷原因檔。"""
        try:
            reason = read_kill_reason(os.getpid(), root=MAGI_ROOT_DIR, delete=True)
            if reason:
                return reason
        except Exception:
            logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5481, exc_info=True)
        # Clean up stale per-PID files (orphans whose target process is long gone)
        try:
            cleanup_stale_kill_reason_files(root=MAGI_ROOT_DIR, max_age_seconds=3600)
        except Exception:
            pass
        return ""

    def _term_handler(signum, frame):
        try:
            signame = signal.Signals(signum).name
        except Exception:
            signame = str(signum)
        kill_reason = _read_kill_reason()
        _write_partial_report(
            f"signal_{signame}",
            error=f"autopilot {run_task} terminated by signal {signame}"
                  + (f" — {kill_reason}" if kill_reason else ""),
        )
        # 即使被 SIGTERM 終止，也嘗試發送通知，避免使用者完全收不到夜間報告
        if run_task in ("nightly", "tick"):
            reason_line = f"- 原因：{kill_reason}\n" if kill_reason else "- 原因：未知（外部 kill 或系統終止）\n"
            try:
                _notify_system(
                    f"⚠️ CASPER 夜間任務被中斷（{signame}）\n"
                    f"- 任務：{run_task}\n"
                    f"{reason_line}"
                    f"- 已完成的步驟可查看報告目錄\n"
                    f"- 報告：{run_dir}",
                    topic_key="nightly",
                )
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5508, exc_info=True)
        raise SystemExit(128 + int(signum))

    try:
        signal.signal(signal.SIGTERM, _term_handler)
        signal.signal(signal.SIGINT, _term_handler)
        if run_task == "self_test":
            # Minimal checks: skills exist + DB smoke
            report["details"]["skills_present"] = {
                k: os.path.exists(_skill_action(k))
                for k in ["pdf-namer", "osc-orchestrator", "file-review-orchestrator", "transcript-downloader"]
            }
            auth_guard = _openclaw_auth_mode_guard()
            report["details"]["openclaw_auth_guard"] = auth_guard
            db_smoke = _run_cmd([VENV_PY, _skill_action("osc-orchestrator"), "--task", "db_smoke {}"], timeout_sec=60)
            report["details"]["osc_db_smoke"] = {"ok": db_smoke.ok, "parsed": db_smoke.parsed, "stderr_tail": (db_smoke.stderr or "")[-400:]}
            schema_guard = _db_schema_chk_nb_guard()
            report["details"]["db_schema_guard"] = schema_guard
            model_guard = _openclaw_model_guard(auto_restart=_env_on("MAGI_OPENCLAW_MODEL_GUARD_RESTART", True))
            report["details"]["openclaw_model_guard"] = model_guard
            comm = _comm_health_self_test()
            report["details"]["comm_health"] = comm
            bb = _big_brain_health_probe() if _env_on("MAGI_BIG_BRAIN_HEALTH_ENABLE", True) else {"ok": True, "status": "disabled"}
            report["details"]["big_brain_health"] = bb
            report["ok"] = bool(auth_guard.get("ok", False) and db_smoke.ok and schema_guard.get("ok", False) and model_guard.get("ok", False) and comm.get("ok", False) and bb.get("ok", False))
            if not auth_guard.get("ok", False):
                report.setdefault("details", {}).setdefault("blockers", []).append(
                    f"openclaw_auth_guard_failed:{auth_guard.get('status') or 'unknown'}"
                )
            if not db_smoke.ok:
                report.setdefault("details", {}).setdefault("blockers", []).append("osc_db_smoke_failed")
            if schema_guard.get("has_chk_nb", False):
                report.setdefault("details", {}).setdefault("blockers", []).append(
                    f"db_schema_guard_failed:{schema_guard.get('message','chk_nb_detected')}"
                )
            if not model_guard.get("ok", False):
                report.setdefault("details", {}).setdefault("blockers", []).append(
                    f"openclaw_model_guard_failed:{model_guard.get('errors') or model_guard.get('error') or 'unknown'}"
                )
            for e in (comm.get("errors") or []):
                report.setdefault("details", {}).setdefault("blockers", []).append(str(e))
            if not bb.get("ok", False):
                report.setdefault("details", {}).setdefault("blockers", []).append(
                    f"big_brain_unhealthy:{bb.get('status') or 'unknown'}"
                )
        elif run_task == "tick":
            report["details"] = run_tick(run_dir)
            report["ok"] = bool(report["details"].get("ok", False))
        elif run_task == "nightly":
            report["details"] = run_nightly(run_dir)
            report["ok"] = bool(report["details"].get("ok", False))
        else:
            report["ok"] = False
            report["details"] = {"error": f"unknown task: {task}"}

        blockers = (report.get("details") or {}).get("blockers") or []
        if blockers:
            report["summary"] = "卡住項目：\n- " + "\n- ".join(blockers)
        else:
            report["summary"] = "完成（無需人工介入）" if report["ok"] else "完成但有錯誤（請看 report.json）"

    except (Exception, TimeoutError) as e:
        report["ok"] = False
        report["details"] = {"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
        report["summary"] = "執行失敗（請看 report.json）"

    json_path, txt_path = _write_report(run_dir, report)
    report_written = True
    if not (run_task == "tick" and _env_on("MAGI_TICK_LIGHT_MODE", False)):
        _remember_run_event(report)

    # Only notify LINE when blocked / needs human.
    details = report.get("details") or {}
    blocked = bool(details.get("blocked"))
    blockers = details.get("blockers") or []
    notified = False
    if (blocked or blockers) and _should_notify(blockers, cooldown_sec=_cooldown_for_blockers(blockers)):
        hint = ""
        if any("reauth_required" in str(b) for b in blockers):
            hint = (
                "\n\n建議處理方式：\n"
                "1) 先跑一次互動式授權（只需做一次）：\n"
                f"   {VENV_PY} {os.path.join(MAGI_ROOT_DIR, 'skills', 'file-review-orchestrator', 'action.py')} --task reauth_gmail\n"
                "2) 授權完成後，下一輪巡檢就不會再卡住。\n"
            )
        # Prefer report.txt for human consumption.
        notified = _notify_system(
            "⛔️ CASPER 自動巡檢卡住，需要你協助\n"
            + ("\n".join([f"- {b}" for b in blockers]) + "\n" if blockers else "")
            + f"報告：{txt_path}"
            + hint
        )
        if notified:
            _mark_notified(blockers, task=run_task, report_json=json_path)

    # Nightly success notification (no human needed).
    if (not blocked) and (not blockers) and report.get("ok") and _should_notify_success(run_task):
        # User-facing LINE notifications should stay human-readable (avoid dumping JSON blobs).
        steps = ((report.get("details") or {}).get("steps") or {}) if isinstance(report.get("details"), dict) else {}
        runtime_lines = _format_openclaw_runtime_lines(
            ((report.get("details") or {}).get("openclaw_runtime_mode") or {}) if isinstance(report.get("details"), dict) else {},
            include_next_action=False,
        )
        fr_dl = (steps.get("file_review_download") or {}).get("parsed") if isinstance(steps.get("file_review_download"), dict) else {}
        fr_summary = _summarize_file_review_download(fr_dl if isinstance(fr_dl, dict) else {})
        fr_lines = []
        if fr_summary.get("count", 0) > 0:
            fr_lines.append(f"- 閱卷下載：{fr_summary.get('count', 0)} 份")
            try:
                notify_groups = int(os.environ.get("MAGI_NIGHTLY_NOTIFY_MAX_GROUPS", "8") or "8")
            except Exception:
                notify_groups = 8
            notify_groups = max(1, min(notify_groups, 30))
            for g in fr_summary.get("groups", [])[:notify_groups]:
                if not isinstance(g, dict):
                    continue
                fr_lines.append(f"  • {g.get('label')}（{len(g.get('files') or [])} 份）")
        else:
            fr_lines.append("- 閱卷下載：0 份")

        slo_lines = []
        slo_step = (steps.get("slo_guard_snapshot") or {}) if isinstance(steps.get("slo_guard_snapshot"), dict) else {}
        slo_parsed = slo_step.get("parsed") if isinstance(slo_step, dict) else {}
        if isinstance(slo_parsed, dict):
            slo_lines = _format_slo_notify_lines(slo_parsed, max_steps=3)
        slo_step_24 = (steps.get("slo_guard_snapshot_24h") or {}) if isinstance(steps.get("slo_guard_snapshot_24h"), dict) else {}
        slo_parsed_24 = slo_step_24.get("parsed") if isinstance(slo_step_24, dict) else {}
        if isinstance(slo_parsed_24, dict):
            slo_lines = _format_slo_notify_lines(slo_parsed_24, max_steps=2) + slo_lines

        slo_apply_lines = []
        slo_apply_step = (steps.get("slo_guard_apply") or {}) if isinstance(steps.get("slo_guard_apply"), dict) else {}
        slo_apply_parsed = slo_apply_step.get("parsed") if isinstance(slo_apply_step, dict) else {}
        if isinstance(slo_apply_parsed, dict):
            slo_apply_lines = _format_slo_apply_notify_lines(slo_apply_parsed)

        comm_lines = []
        comm_step = (steps.get("comm_health") or {}) if isinstance(steps.get("comm_health"), dict) else {}
        comm_parsed = comm_step.get("parsed") if isinstance(comm_step, dict) else {}
        if isinstance(comm_parsed, dict):
            comm_lines = _format_comm_notify_lines(comm_parsed)

        bb_lines = []
        bb_step = (steps.get("big_brain_health") or {}) if isinstance(steps.get("big_brain_health"), dict) else {}
        bb_parsed = bb_step.get("parsed") if isinstance(bb_step, dict) else {}
        if isinstance(bb_parsed, dict):
            bb_lines = _format_big_brain_notify_lines(bb_parsed)

        ok_msg = (
            "✅ CASPER 夜間任務完成（無需人工介入）\n"
            f"- 時間：{report.get('ts')}\n"
            f"- 任務：{report.get('task')}\n"
            + ("\n".join(runtime_lines) + "\n" if runtime_lines else "")
            + ("\n".join(fr_lines) + "\n" if fr_lines else "")
            + ("\n".join(comm_lines) + "\n" if comm_lines else "")
            + ("\n".join(bb_lines) + "\n" if bb_lines else "")
            + ("\n".join(slo_lines) + "\n" if slo_lines else "")
            + ("\n".join(slo_apply_lines) + "\n" if slo_apply_lines else "")
            + f"- 報告：{txt_path}\n"
        )
        notified_ok = _notify_system(
            ok_msg
        )
        if notified_ok:
            _mark_success_notified(run_task, report_json=json_path)

    # 夜間任務失敗（非 blocked）也要通知，避免使用者完全收不到夜間報告
    if (
        run_task in ("nightly", "tick")
        and not notified
        and not report.get("ok")
        and not blocked
        and not blockers
        and _should_notify_success(run_task)  # 同樣每日一次限制
    ):
        fail_summary = (report.get("summary") or "未知原因")[:200]
        _notify_system(
            f"⚠️ CASPER 夜間任務完成但有異常\n"
            f"- 時間：{report.get('ts')}\n"
            f"- 任務：{report.get('task')}\n"
            f"- 狀態：{fail_summary}\n"
            f"- 報告：{txt_path}\n",
            topic_key="nightly",
        )

    print(
        json.dumps(
            {
                "ok": bool(report.get("ok")),
                "task": run_task,
                "report_json": json_path,
                "report_txt": txt_path,
                "blocked": blocked,
                "blockers": blockers,
                "notified": notified,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    rc = 0 if report.get("ok") else 2

    # Cleanup: cancel alarm + release lock
    signal.alarm(0)
    try:
        fp.close()
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 5715, exc_info=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
