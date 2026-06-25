"""
Administrative / runtime status routes extracted from server.py.

This module keeps Web dashboard support, NERV APIs, system health probes,
and audio transcription wiring, while receiving runtime dependencies from the
main server bootstrap.
"""

from __future__ import annotations

import logging
import copy
import importlib
import importlib.util
import json
from collections import deque
import os
import re
import shutil
import shlex
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from html import escape
from api.thread_pools import io_pool
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, Response, current_app, jsonify, request
from flask_login import current_user, login_required


ROOT = Path(__file__).resolve().parents[2]
_BROWSER_CORE_HEALTH_CACHE: dict[str, Any] = {"ts": 0.0, "result": None}
_BROWSER_CORE_HEALTH_LOCK = threading.Lock()
_PYTHON_STARTUP_HEALTH_CACHE: dict[str, tuple[float, bool]] = {}


def _browser_core_health_hard_timeout(
    timeout_seconds: int = 15,
    cache_ttl_seconds: int = 30,
) -> dict[str, Any]:
    """Run Playwright health in a child process so /health itself never hangs."""
    now = time.time()
    cached = _BROWSER_CORE_HEALTH_CACHE.get("result")
    if (
        isinstance(cached, dict)
        and cache_ttl_seconds > 0
        and now - float(_BROWSER_CORE_HEALTH_CACHE.get("ts") or 0.0) < cache_ttl_seconds
    ):
        result = dict(cached)
        result["cached"] = True
        return result

    sync_probe = str(os.environ.get("MAGI_BROWSER_HEALTH_SYNC_PROBE", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
    if not sync_probe:
        return {
            "ok": True,
            "engine": "playwright-chromium",
            "status": "deferred",
            "detail": "瀏覽器核心由獨立 live 測試驗證；健康頁不同步冷啟動以避免阻塞",
            "cached": False,
        }

    acquired = _BROWSER_CORE_HEALTH_LOCK.acquire(blocking=False)
    if not acquired:
        if isinstance(cached, dict):
            result = dict(cached)
            result["cached"] = True
            result["stale_while_revalidate"] = True
            result["detail"] = "瀏覽器核心檢查正在更新，先回傳上一筆結果以避免健康頁阻塞"
            return result
        return {
            "ok": True,
            "engine": "playwright-chromium",
            "status": "probe_in_progress",
            "detail": "瀏覽器核心檢查正在進行，本次健康頁先不阻塞",
            "cached": False,
        }

    code = (
        "import json;"
        "from skills.engine.playwright_wrapper import playwright_chromium_health;"
        f"print(json.dumps(playwright_chromium_health(timeout_seconds={int(timeout_seconds)}, cache_ttl_seconds=0), ensure_ascii=False))"
    )
    env = os.environ.copy()
    env.setdefault("MAGI_ROOT", str(ROOT))
    env.setdefault("MAGI_ROOT_DIR", str(ROOT))
    try:
        try:
            completed = subprocess.run(
                [sys.executable, "-c", code],
                cwd=str(ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=max(int(timeout_seconds) + 2, 3),
            )
        except subprocess.TimeoutExpired:
            result = {
                "ok": True,
                "engine": "playwright-chromium",
                "reason": "browser_probe_deferred",
                "detail": f"瀏覽器核心冷啟動超過 {int(timeout_seconds) + 2} 秒，已延後檢查且不阻塞 MAGI",
            }
        except Exception as exc:
            result = {
                "ok": False,
                "engine": "playwright-chromium",
                "reason": "browser_probe_failed",
                "detail": str(exc)[:160],
            }
        else:
            stdout_lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
            payload = stdout_lines[-1] if stdout_lines else "{}"
            try:
                result = json.loads(payload)
            except Exception:
                result = {
                    "ok": False,
                    "engine": "playwright-chromium",
                    "reason": "browser_probe_bad_output",
                    "detail": (completed.stderr or completed.stdout or "")[-240:],
                }
            if completed.returncode != 0:
                result = {
                    "ok": False,
                    "engine": "playwright-chromium",
                    "reason": "browser_probe_failed",
                    "detail": (completed.stderr or completed.stdout or "")[-240:],
                }
            elif not isinstance(result, dict):
                result = {
                    "ok": False,
                    "engine": "playwright-chromium",
                    "reason": "browser_probe_bad_output",
                    "detail": repr(result)[:160],
                }
    finally:
        _BROWSER_CORE_HEALTH_LOCK.release()

    _BROWSER_CORE_HEALTH_CACHE["ts"] = now
    _BROWSER_CORE_HEALTH_CACHE["result"] = dict(result)
    return result


def _wants_json_response() -> bool:
    accept = request.headers.get("Accept") or ""
    if not accept:
        return True
    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    if best == "text/html" and request.accept_mimetypes[best] >= request.accept_mimetypes["application/json"]:
        return False
    return True


def _render_health_html(checks: dict[str, Any]) -> Response:
    def ok_badge(ok: Any) -> str:
        if ok is True:
            return '<span class="badge ok">正常</span>'
        if ok is False:
            return '<span class="badge bad">需檢查</span>'
        return '<span class="badge warn">未知</span>'

    status = str(checks.get("status") or "unknown")
    status_text = "正常" if status == "operational" else "需檢查"
    timestamp = datetime.fromtimestamp(float(checks.get("timestamp") or time.time())).strftime("%Y/%m/%d %H:%M:%S")
    system = checks.get("system") if isinstance(checks.get("system"), dict) else {}
    nas = checks.get("nas") if isinstance(checks.get("nas"), dict) else {}
    omlx = checks.get("omlx") if isinstance(checks.get("omlx"), dict) else {}
    db = checks.get("db") if isinstance(checks.get("db"), dict) else {}
    faiss = checks.get("faiss") if isinstance(checks.get("faiss"), dict) else {}
    browser_core = checks.get("browser_core") if isinstance(checks.get("browser_core"), dict) else {}
    drive_sync = checks.get("drive_sync") if isinstance(checks.get("drive_sync"), dict) else {}
    audit = checks.get("operational_audit") if isinstance(checks.get("operational_audit"), dict) else {}
    op = checks.get("operational_health") if isinstance(checks.get("operational_health"), dict) else {}

    services = [
        ("主狀態", status == "operational", status_text),
        ("資料庫", db.get("ok"), db.get("detail") or "MariaDB"),
        ("推論服務", omlx.get("ok"), ", ".join(omlx.get("models") or []) or "模型狀態"),
        ("OCR", (checks.get("ocr") or {}).get("ok") if isinstance(checks.get("ocr"), dict) else None, (checks.get("ocr") or {}).get("engine", "")),
        ("瀏覽器核心", browser_core.get("ok"), browser_core.get("detail") or browser_core.get("reason") or "Playwright Chromium"),
        ("雲端同步", drive_sync.get("ok"), drive_sync.get("detail") or drive_sync.get("message") or "Google Drive"),
        ("向量資料庫", faiss.get("ok"), f"{faiss.get('vectors', '暖機中')} vectors"),
        ("日常稽核", audit.get("ok"), "最近檢查"),
        ("維運健康", op.get("ok"), ", ".join(op.get("degraded_reasons") or []) or "無重大異常"),
    ]
    nas_rows = "".join(
        f"<li><strong>{escape(str(name))}</strong>{ok_badge(bool(ok))}</li>"
        for name, ok in sorted(nas.items())
    ) or "<li>尚未回報</li>"
    service_cards = "".join(
        f"""
        <article class="card">
          <div class="card-title">{escape(name)}{ok_badge(ok)}</div>
          <p>{escape(str(detail or ""))}</p>
        </article>
        """
        for name, ok, detail in services
    )
    html = f"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>系統健康狀態 | MAGI</title>
  <style>
    :root {{ color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
    body {{ margin: 0; background: #f5f7fb; color: #172033; }}
    main {{ max-width: 1080px; margin: 0 auto; padding: 24px; }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 18px; }}
    h1 {{ font-size: 24px; margin: 0; }}
    .time {{ color: #5e6b81; font-size: 14px; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
    .card, .panel {{ background: #fff; border: 1px solid #dbe3ef; border-radius: 8px; padding: 14px; }}
    .card-title {{ display: flex; justify-content: space-between; gap: 12px; font-weight: 700; }}
    p {{ margin: 10px 0 0; color: #536176; line-height: 1.5; word-break: break-word; }}
    .badge {{ border-radius: 999px; padding: 3px 8px; font-size: 12px; white-space: nowrap; }}
    .ok {{ background: #e5f8ed; color: #14743d; }}
    .bad {{ background: #ffe8e8; color: #b42318; }}
    .warn {{ background: #fff4d6; color: #8a5b00; }}
    ul {{ margin: 8px 0 0; padding: 0; list-style: none; display: grid; gap: 8px; }}
    li {{ display: flex; justify-content: space-between; gap: 12px; border-top: 1px solid #eef2f7; padding-top: 8px; }}
    a {{ color: #1264d8; text-decoration: none; }}
    @media (prefers-color-scheme: dark) {{
      body {{ background: #111827; color: #e6edf7; }}
      .card, .panel {{ background: #182235; border-color: #2d3b52; }}
      p, .time {{ color: #b8c3d4; }}
      li {{ border-color: #2d3b52; }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div>
        <h1>MAGI 系統健康狀態</h1>
        <div class="time">更新時間：{escape(timestamp)}｜運行 {escape(str(checks.get("uptime_seconds", "-")))} 秒</div>
      </div>
      <a href="/golem">返回 MAGI</a>
    </header>
    <section class="grid">{service_cards}</section>
    <section class="panel" style="margin-top:12px">
      <strong>NAS 掛載</strong>
      <ul>{nas_rows}</ul>
    </section>
  </main>
</body>
</html>"""
    return Response(html, mimetype="text/html")


def _safe_epoch(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    txt = str(value or "").strip()
    if not txt:
        return 0.0
    try:
        return float(txt)
    except (TypeError, ValueError):
        txt = txt.strip()
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


def _current_omlx_model_ids() -> list[str]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/v1/models", timeout=2) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return [
            str(item.get("id") or "").strip()
            for item in (data.get("data") or [])
            if isinstance(item, dict) and str(item.get("id") or "").strip()
        ]
    except Exception:
        return []


def _expected_omlx_keyword_now() -> str:
    now = datetime.now()
    minutes = now.hour * 60 + now.minute
    return "e4b" if 395 <= minutes < 1310 else "26b"


def _is_omlx_switch_recovered() -> bool:
    expected = _expected_omlx_keyword_now()
    return any(expected in model.lower() for model in _current_omlx_model_ids())


def _is_resource_governor_recovered() -> bool:
    try:
        from scripts.ops import resource_governor

        decision = resource_governor.classify(resource_governor.collect_snapshot())
        return bool(getattr(decision, "ok", False))
    except Exception:
        return False


def _is_tailscale_funnel_recovered(issue_ts: float) -> bool:
    path = ROOT / ".runtime" / "tailscale_funnel_health_latest.json"
    if not path.exists() or path.stat().st_mtime <= issue_ts:
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return False
    return str(data.get("status") or "").lower() in {"ok", "recovered", "skipped"}


def _is_pdf_smoke_progress_callback_recovered(row: dict[str, Any], root: Path) -> bool:
    err = str(row.get("error") or "")
    if "progress_callback" not in err or "_summary_pdf_stub" not in err:
        return False
    issue_ts = float(row.get("_ts") or _safe_epoch(row.get("ts") or row.get("iso")))
    runtime_dir = root / ".runtime"
    try:
        candidates = sorted(
            runtime_dir.glob("allpdf_smoke*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        return False

    for path in candidates[:20]:
        try:
            if path.stat().st_mtime <= issue_ts:
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
            by_kind = data.get("by_kind") if isinstance(data.get("by_kind"), dict) else {}
            summary_kind = by_kind.get("summary") if isinstance(by_kind.get("summary"), dict) else {}
            if int(summary.get("fail") or 0) == 0 and int(summary.get("pass") or 0) > 0:
                if int(summary_kind.get("pass") or 0) > 0:
                    return True
        except Exception:
            continue
    return False


def _python_startup_recovered_after_interrupted_getpath(row: dict[str, Any]) -> bool:
    err = str(row.get("error") or "")
    if "Fatal Python error: error evaluating path" not in err:
        return False
    if "InterruptedError: [Errno 4] Interrupted system call" not in err:
        return False

    context = str(row.get("context") or "")
    match = re.search(r"command=(.+?)(?:\s+[A-Z0-9_]+=|\s+--\s+|$)", context)
    command = match.group(1).strip() if match else ""
    try:
        parts = shlex.split(command)
    except Exception:
        parts = []

    python_exe = ""
    for part in parts[:4]:
        name = Path(part).name
        if name.startswith("python"):
            python_exe = part
            break
    if not python_exe:
        fallback = ROOT / "venv" / "bin" / "python3"
        if fallback.exists():
            python_exe = str(fallback)
    if not python_exe:
        return False

    now = time.time()
    cached = _PYTHON_STARTUP_HEALTH_CACHE.get(python_exe)
    if cached and now - cached[0] < 60:
        return cached[1]

    try:
        proc = subprocess.run(
            [python_exe, "-I", "-c", "print('ok')"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
        ok = proc.returncode == 0 and "ok" in proc.stdout
    except Exception:
        ok = False
    _PYTHON_STARTUP_HEALTH_CACHE[python_exe] = (now, ok)
    return ok


def _classify_cron_issue(
    row: dict[str, Any],
    *,
    active_cutoff: float,
    latest_cron_issue_ts_by_job: dict[str, float],
    cron_success_ts: dict[str, float],
) -> str:
    if _is_false_positive_cron_issue(row):
        return "false_positive"
    if _python_startup_recovered_after_interrupted_getpath(row):
        return "recovered"

    ts = float(row.get("_ts") or 0.0)
    job_id = _cron_job_from_issue_command(row.get("command"))
    if not job_id:
        return "stale" if ts < active_cutoff else "active_unresolved"

    latest_issue_ts = latest_cron_issue_ts_by_job.get(job_id, ts)
    last_success_ts = cron_success_ts.get(job_id, 0.0)
    if job_id in {"job_omlx_switch_day", "job_omlx_switch_night", "job_omlx_profile_guard"}:
        if _is_omlx_switch_recovered():
            return "recovered"
    if job_id == "job_resource_governor" and _is_resource_governor_recovered():
        return "recovered"
    if job_id == "job_tailscale_funnel_healthcheck" and _is_tailscale_funnel_recovered(ts):
        return "recovered"
    if latest_issue_ts > ts:
        return "superseded"
    if last_success_ts > ts:
        return "recovered"
    if ts < active_cutoff:
        return "stale"
    return "active_unresolved"


def _load_recent_issue_rows(issue_path: Path, cutoff_ts: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not issue_path.exists():
        return rows
    with open(issue_path, encoding="utf-8") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = _safe_epoch(row.get("ts") or row.get("iso"))
            if ts < cutoff_ts:
                continue
            row["_ts"] = ts
            rows.append(row)
    return rows


def _load_cron_success_ts(root: Path) -> dict[str, float]:
    state_path = root / ".runtime" / "cron_state.json"
    if not state_path.exists():
        return {}
    try:
        raw = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for job_id, data in raw.items():
        if not isinstance(data, dict):
            continue
        ts = _safe_epoch(data.get("last_success_at"))
        if ts > 0:
            out[str(job_id)] = ts
    return out


def _issue_context_threshold_gb(row: dict[str, Any]) -> float:
    raw = row.get("context")
    if isinstance(raw, dict):
        for key in ("threshold_gb", "threshold_warn_gb", "threshold_critical_gb"):
            try:
                value = float(raw.get(key) or 0)
            except Exception:
                value = 0.0
            if value > 0:
                return value
    text = f"{raw or ''} {row.get('error') or row.get('error_msg') or ''}"
    for pattern in (
        r"threshold_gb['\"]?\s*:\s*([0-9]+(?:\.[0-9]+)?)",
        r"閾值\s*([0-9]+(?:\.[0-9]+)?)\s*GB",
    ):
        m = re.search(pattern, text)
        if m:
            try:
                return float(m.group(1))
            except Exception:
                return 0.0
    return 0.0


def _is_recovered_non_cron_issue(row: dict[str, Any], root: Path) -> bool:
    if _is_pdf_smoke_progress_callback_recovered(row, root):
        return True

    command = str(row.get("command") or "")
    source = str(row.get("source") or "")
    if command != "alarm:disk_low_water" and source != "disk_low_water_alarm":
        return False
    threshold_gb = _issue_context_threshold_gb(row)
    if threshold_gb <= 0:
        return False
    try:
        current_free_gb = shutil.disk_usage(str(root)).free / 1024 / 1024 / 1024
    except Exception:
        return False
    # The hourly disk alarm uses a 50 GB early-warning threshold so MAGI can
    # clean before the machine is under real pressure.  For active incident
    # health, only keep it unresolved while the host is still in core-only risk
    # (<30 GB) or below the original critical threshold.  Otherwise the same
    # warning would keep /health yellow for a full day after cleanup succeeded.
    effective_threshold = min(threshold_gb, 30.0) if threshold_gb > 30 else threshold_gb
    return current_free_gb >= effective_threshold


def _compute_operational_issue_health(root: Path, now_ts: float) -> dict[str, Any]:
    cutoff_24h = now_ts - 86400
    active_window_sec = int(os.environ.get("MAGI_OPERATIONAL_ACTIVE_ISSUE_WINDOW_SEC", "21600") or "21600")
    active_cutoff = now_ts - active_window_sec
    rows = _load_recent_issue_rows(root / ".runtime" / "issue_agenda.jsonl", cutoff_24h)
    cron_success_ts = _load_cron_success_ts(root)

    latest_cron_issue_ts_by_job: dict[str, float] = {}
    for row in rows:
        source = str(row.get("source", ""))
        if not source.startswith("discord_bot.cron_scheduler"):
            continue
        job_id = _cron_job_from_issue_command(row.get("command"))
        if not job_id:
            continue
        ts = float(row.get("_ts") or 0.0)
        prev = latest_cron_issue_ts_by_job.get(job_id, 0.0)
        if ts > prev:
            latest_cron_issue_ts_by_job[job_id] = ts

    raw_cron_failures = 0
    raw_high_severity = 0
    active_cron_failures = 0
    active_high_severity = 0
    active_jobs: set[str] = set()
    inactive_cron_failures = 0
    false_positive_cron_failures = 0
    recovered_cron_failures = 0
    superseded_cron_failures = 0
    stale_cron_failures = 0
    recovered_non_cron_high_severity = 0

    for row in rows:
        ts = float(row.get("_ts") or 0.0)
        source = str(row.get("source", ""))
        is_cron = source.startswith("discord_bot.cron_scheduler")
        is_high = str(row.get("severity", "")) in ("High", "Critical")
        if is_high:
            raw_high_severity += 1

        if not is_cron:
            if is_high and _is_recovered_non_cron_issue(row, root):
                recovered_non_cron_high_severity += 1
                continue
            if is_high and ts >= active_cutoff:
                active_high_severity += 1
            continue

        raw_cron_failures += 1
        state = _classify_cron_issue(
            row,
            active_cutoff=active_cutoff,
            latest_cron_issue_ts_by_job=latest_cron_issue_ts_by_job,
            cron_success_ts=cron_success_ts,
        )
        if state == "false_positive":
            false_positive_cron_failures += 1
            continue
        if state in ("superseded", "recovered", "stale"):
            inactive_cron_failures += 1
            if state == "superseded":
                superseded_cron_failures += 1
            elif state == "recovered":
                recovered_cron_failures += 1
            else:
                stale_cron_failures += 1
            continue

        active_cron_failures += 1
        job_id = _cron_job_from_issue_command(row.get("command"))
        if job_id:
            active_jobs.add(job_id)
        if is_high:
            active_high_severity += 1

    return {
        "active_cron_failures_24h": active_cron_failures,
        "active_high_severity_24h": active_high_severity,
        "active_distinct_jobs_24h": len(active_jobs),
        "raw_cron_failures_24h": raw_cron_failures,
        "raw_high_severity_24h": raw_high_severity,
        "inactive_cron_failures_24h": inactive_cron_failures,
        "false_positive_cron_failures_24h": false_positive_cron_failures,
        "recovered_cron_failures_24h": recovered_cron_failures,
        "superseded_cron_failures_24h": superseded_cron_failures,
        "stale_cron_failures_24h": stale_cron_failures,
        "recovered_non_cron_high_severity_24h": recovered_non_cron_high_severity,
        "inactive_or_noise_cron_failures_24h": (
            inactive_cron_failures + false_positive_cron_failures
        ),
        "active_window_sec": active_window_sec,
    }


def create_admin_runtime_blueprint(
    *,
    logger: Any,
    orchestrator: Any,
    require_json_auth,
    list_skill_docs,
    nerv_skill_interview_user_id,
    extract_interview_skill_name,
    skill_doc_path,
    skill_action_path,
    skill_summary,
    nerv_product_runtime_payload,
    nerv_product_names,
    update_product_runtime,
    cloudflared_alive,
    server_start_time: float,
    attachment_job_queue,
    list_attachment_job_ids,
    read_attachment_job,
    expected_magi_api_key: str,
    db_config: dict[str, Any],
    mysql_connector: Any,
    safe_remove_tmp,
    magi_root: str | Optional[Path] = None,
) -> Blueprint:
    bp = Blueprint("admin_runtime", __name__)
    root = Path(magi_root) if magi_root else Path(__file__).resolve().parents[2]
    static_dir = root / "static"
    agent_dir = root / ".agent"
    env_path = root / ".env"
    status_file = static_dir / "magi_status.json"
    server_log_path = agent_dir / "server.log"
    health_cache: dict[str, Any] = {"ts": 0.0, "checks": None}
    health_cache_ttl_sec = max(5.0, float(os.environ.get("MAGI_HEALTH_CACHE_TTL_SEC", "60") or "60"))

    def _is_current_user_admin() -> bool:
        try:
            checker = getattr(current_user, "is_admin", None)
            if callable(checker):
                return bool(checker())
            return str(getattr(current_user, "role", "") or "").lower() == "admin"
        except Exception:
            return False

    def _parse_env_file(path: Path) -> dict[str, str]:
        values: dict[str, str] = {}
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return values
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
        return values

    def _write_env_values(path: Path, updates: dict[str, str]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            original = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            original = []
        backup = path.with_suffix(path.suffix + f".bak-{datetime.now().strftime('%Y%m%d%H%M%S')}")
        if path.exists():
            shutil.copy2(path, backup)
        else:
            backup.write_text("", encoding="utf-8")

        seen: set[str] = set()
        out: list[str] = []
        for raw in original:
            stripped = raw.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in updates:
                    out.append(f"{key}={updates[key]}")
                    seen.add(key)
                    continue
            out.append(raw)
        missing = [key for key in updates if key not in seen]
        if missing and out and out[-1].strip():
            out.append("")
        for key in missing:
            out.append(f"{key}={updates[key]}")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
        tmp.replace(path)
        return backup

    def _mask_secret(value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) <= 12:
            return "*" * len(text)
        return f"{text[:8]}...{text[-4:]}"

    def _nerv_heavy_runtime_payload() -> dict[str, Any]:
        env_values = _parse_env_file(env_path)
        key_value = os.environ.get("NVIDIA_NIM_API_KEY") or env_values.get("NVIDIA_NIM_API_KEY", "")
        enabled_raw = os.environ.get("NVIDIA_NIM_ENABLE") or env_values.get("NVIDIA_NIM_ENABLE", "0")
        enabled = str(enabled_raw).strip().lower() in {"1", "true", "yes", "on"}
        return {
            "ok": True,
            "can_edit": _is_current_user_admin(),
            "env_path": str(env_path),
            "enabled": enabled,
            "configured": bool(str(key_value or "").strip()),
            "masked": _mask_secret(key_value),
            "env_key": "NVIDIA_NIM_API_KEY",
            "enable_key": "NVIDIA_NIM_ENABLE",
            "command_prefixes": ["@heavy", "@重型"],
            "description": "HEAVY 任務會優先嘗試 NVIDIA NIM API；未啟用或 API 不可用時回到本機 26B。",
        }

    def _load_status_payload() -> dict[str, Any]:
        return json.loads(status_file.read_text(encoding="utf-8"))

    def _run_status_command(args: list[str], *, timeout: int = 4) -> subprocess.CompletedProcess:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)

    def _launchctl_list_contains(label: str) -> bool:
        try:
            result = _run_status_command(["launchctl", "list"], timeout=4)
            text = f"{getattr(result, 'stdout', '')}\n{getattr(result, 'stderr', '')}"
            return label in text
        except Exception:
            return False

    def _cloudflare_tunnel_url() -> str:
        if not cloudflared_alive():
            return ""
        candidates = [
            agent_dir / "cloudflare_tunnel_url.txt",
            root / ".agent" / "cloudflare_tunnel_url.txt",
            root / "logs" / "cloudflared.log",
        ]
        import re as _re
        for path in candidates:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            match = _re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", text)
            if match:
                return match.group(0)
        return ""

    def _tailscale_status() -> dict[str, Any]:
        tailscale_bin = shutil.which("tailscale") or "/opt/homebrew/bin/tailscale"
        installed = bool(tailscale_bin and os.path.exists(tailscale_bin))
        payload: dict[str, Any] = {
            "installed": installed,
            "running": _launchctl_list_contains("tailscale") or _launchctl_list_contains("homebrew.mxcl.tailscale"),
            "ip": "",
            "dns_name": "",
            "status": "offline",
        }
        if not installed:
            return payload
        try:
            result = _run_status_command([tailscale_bin, "status", "--json"], timeout=5)
            raw = getattr(result, "stdout", "") or ""
            if getattr(result, "returncode", 1) == 0 and raw.strip():
                data = json.loads(raw)
                self_node = data.get("Self") or {}
                ips = self_node.get("TailscaleIPs") or []
                payload["ip"] = str(ips[0] if ips else "")
                payload["dns_name"] = str(self_node.get("DNSName") or "").rstrip(".")
                payload["running"] = True
        except Exception:
            logger.debug("silent-catch in _tailscale_status", exc_info=True)
        payload["status"] = "online" if payload.get("running") else "offline"
        return payload

    def _chrome_remote_desktop_status() -> dict[str, Any]:
        host_app = Path("/Library/PrivilegedHelperTools/ChromeRemoteDesktopHost.app")
        config_path = Path("/Library/PrivilegedHelperTools/org.chromium.chromoting.json")
        enabled_flag = Path("/Library/PrivilegedHelperTools/org.chromium.chromoting.me2me_enabled")
        running = _launchctl_list_contains("org.chromium.chromoting")
        installed = host_app.exists()
        return {
            "installed": installed,
            "configured": config_path.exists() or enabled_flag.exists(),
            "running": running,
            "status": "online" if installed and running else ("ready" if installed else "missing"),
            "access_url": "https://remotedesktop.google.com/access",
            "setup_url": "https://remotedesktop.google.com/headless",
        }

    def _tail_file_lines(path: Path, max_lines: int = 120) -> list[str]:
        if max_lines <= 0 or not path.exists():
            return []
        rows: deque[str] = deque(maxlen=max_lines)
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    line = raw_line.rstrip("\n")
                    if line:
                        rows.append(line)
        except Exception:
            return []
        return list(rows)

    def _extract_log_timestamp(raw_line: str) -> float:
        for pattern in (
            r"\[(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?)\]",
            r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
            r"(\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})",
        ):
            match = re.search(pattern, raw_line)
            if not match:
                continue
            return _safe_epoch(match.group(1).replace(",", ""))
        return 0.0

    def _classify_activity_type(message: str, source: str) -> str:
        lowered = (message or "").lower()
        source_lower = (source or "").lower()
        if "error" in lowered or "exception" in lowered or "fail" in lowered or "無法" in lowered:
            return "error"
        if source_lower == "cron" or "cron" in lowered:
            return "cron"
        if "switch" in lowered or "切換" in lowered or "model" in lowered or "reload" in lowered:
            return "model_switch"
        return "log"

    def _collect_macos_memory_pressure() -> dict[str, Any]:
        if sys.platform != "darwin":
            return {"status": "unknown", "free_percent": None}
        try:
            result = subprocess.run(
                ["memory_pressure"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            output = f"{result.stdout or ''}\n{result.stderr or ''}"
        except Exception:
            return {"status": "unknown", "free_percent": None}

        match = re.search(r"System-wide memory free percentage:\s*(\d+)%", output)
        if not match:
            return {"status": "unknown", "free_percent": None}
        free_percent = int(match.group(1))
        if free_percent < 20:
            status = "critical"
        elif free_percent < 30:
            status = "warn"
        else:
            status = "ok"
        return {
            "status": status,
            "free_percent": free_percent,
        }

    def _collect_system_telemetry(now_ts: float) -> dict[str, Any]:
        out: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(now_ts).isoformat(),
            "generated_at": datetime.fromtimestamp(now_ts).isoformat(),
        }
        try:
            out["uptime_seconds"] = max(0.0, now_ts - float(server_start_time))
        except Exception:
            out["uptime_seconds"] = None

        try:
            load_1m, load_5m, load_15m = os.getloadavg()
            out["loadavg"] = {
                "1m": round(float(load_1m), 2),
                "5m": round(float(load_5m), 2),
                "15m": round(float(load_15m), 2),
            }
        except Exception:
            out["loadavg"] = {}

        try:
            import psutil

            vm = psutil.virtual_memory()
            total_bytes = float(vm.total) if isinstance(vm.total, (int, float)) else None
            available_bytes = float(vm.available) if isinstance(vm.available, (int, float)) else None
            used_percent = float(getattr(vm, "percent", 0.0))
            used_gb = None
            free_gb = None
            if total_bytes is not None and available_bytes is not None:
                used_gb = max(0.0, total_bytes - available_bytes) / (1024 ** 3)
                free_gb = available_bytes / (1024 ** 3)
            out["memory"] = {
                "used_gb": round(used_gb, 1) if used_gb is not None else None,
                "free_gb": round(free_gb, 1) if free_gb is not None else None,
                "percent": round(used_percent, 1),
                "pressure": "ok" if used_percent < 85 else ("warn" if used_percent < 92 else "critical"),
            }
            out["swap"] = {
                "percent": round(float(getattr(psutil.swap_memory(), "percent", 0.0)), 1),
            }
            out["cpu_percent"] = psutil.cpu_percent(interval=0.05)
        except Exception:
            out["memory"] = {"used_gb": None, "free_gb": None, "percent": None, "pressure": "unknown"}
            out["swap"] = {"percent": None}
            out["cpu_percent"] = None

        try:
            disk = shutil.disk_usage(str(root))
            disk_total = float(disk.total) if isinstance(disk.total, (int, float)) else None
            disk_used = float(disk.used) if isinstance(disk.used, (int, float)) else None
            disk_free = float(disk.free) if isinstance(disk.free, (int, float)) else None
            used_percent = float(getattr(disk, "percent", 0.0))
            if disk_total is not None and disk_used is not None:
                used_percent = 100.0 * disk_used / disk_total if disk_total else 0.0
            out["disk"] = {
                "free_gb": round(disk_free / (1024 ** 3), 1) if disk_free is not None else None,
                "percent": round(used_percent, 1),
            }
        except Exception:
            out["disk"] = {"free_gb": None, "percent": None}

        out["macos_memory_pressure"] = _collect_macos_memory_pressure()
        swap = out.get("swap") if isinstance(out.get("swap"), dict) else {}
        swap_percent = swap.get("percent")
        mac_pressure = out.get("macos_memory_pressure") if isinstance(out.get("macos_memory_pressure"), dict) else {}
        mac_status = str(mac_pressure.get("status") or "unknown")
        if isinstance(swap_percent, (int, float)):
            if mac_status == "ok" and swap_percent >= 75:
                swap["status"] = "historical"
            elif swap_percent >= 90:
                swap["status"] = "critical"
            elif swap_percent >= 75:
                swap["status"] = "warn"
            else:
                swap["status"] = "ok"
            out["swap"] = swap
        return out

    def _tcp_alive(port: int, timeout_sec: float = 0.4) -> bool:
        if port <= 0:
            return False
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout_sec):
                return True
        except Exception:
            return False

    def _collect_inference_telemetry() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "active_profile": "unknown",
            "expected_profile": "unknown",
            "expected_model_keyword": "unknown",
            "model_8080": {"status": "unknown", "port": 8080, "models": [], "count": 0, "active_model": ""},
            "available_models": [],
            "sidecars": {},
            "summary": {"status": "unknown", "reasons": []},
        }

        try:
            active_profile_path = Path.home() / ".omlx" / "active_profile"
            if active_profile_path.exists():
                payload["active_profile"] = active_profile_path.read_text(encoding="utf-8").strip() or "unknown"
        except Exception:
            logging.getLogger(__name__).debug("active oMLX profile probe failed", exc_info=True)

        try:
            from scripts.ops.omlx_profile_policy import expected_profile_now

            expected_profile, expected_model = expected_profile_now()
            payload["expected_profile"] = str(expected_profile or "unknown")
            payload["expected_model_keyword"] = str(expected_model or "unknown")
            payload["active_profile_expected"] = payload["expected_model_keyword"]
            if payload.get("active_profile") in {"", "unknown", None}:
                payload["active_profile"] = payload["expected_profile"]
        except Exception:
            payload["active_profile_expected"] = payload["expected_model_keyword"]
            pass

        expected_keyword = str(payload.get("expected_model_keyword") or "").lower()
        if not expected_keyword or expected_keyword == "unknown":
            expected_keyword = _expected_omlx_keyword_now() if str(payload.get("active_profile") or "").startswith("day") else "26b"

        base_url = os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:8080").rstrip("/")
        model_payload = payload["model_8080"]
        try:
            with urllib.request.urlopen(f"{base_url}/v1/models", timeout=1.5) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
                models = [
                    str(item.get("id") or "").strip()
                    for item in (body.get("data") or [])
                    if str(item.get("id") or "").strip()
                ]
                model_payload["models"] = models
                model_payload["count"] = len(models)
                model_payload["active_model"] = models[0] if models else ""
                model_payload["status"] = "online"
        except Exception:
            model_payload["status"] = "offline"

        sidecars = {
            "embed": {"port": 8081, "status": "offline", "managed": None},
            "phi4": {"port": 8082, "status": "offline", "managed": None},
            "smol": {"port": 8083, "status": "offline", "managed": None},
        }
        for sid, sid_payload in sidecars.items():
            port = int(sid_payload.get("port") or 0)
            sid_payload["status"] = "online" if _tcp_alive(port) else "offline"
            if sid == "embed":
                sid_payload["managed"] = _launchctl_list_contains("com.magi.omlx-embed")
            else:
                sid_payload["managed"] = _launchctl_list_contains(f"com.magi.omlx-{sid}")
        payload["sidecars"] = sidecars

        summary_reasons: list[str] = []
        active_model = str(model_payload.get("active_model") or "")
        active_profile = str(payload.get("active_profile") or "")
        expected_profile = str(payload.get("expected_profile") or "")
        sidecar_profile = expected_profile if expected_profile not in {"", "unknown", None} else active_profile
        if model_payload.get("status") != "online":
            payload["summary"]["status"] = "critical"
            summary_reasons.append("8080 api unreachable")
        elif expected_keyword and active_model and expected_keyword not in active_model.lower():
            if expected_profile == "night" and active_profile == "night-12b-degraded" and "12b" in active_model.lower():
                summary_reasons.append("night 26B fallback is using 12B")
                payload["summary"]["status"] = "warn"
            else:
                summary_reasons.append(f"active model does not match expected keyword: {expected_keyword}")
                payload["summary"]["status"] = "warn"
        elif summary_reasons:
            payload["summary"]["status"] = "warn"
        elif sidecar_profile == "day" and any(
            sidecars[name].get("status") != "online" for name in ("phi4", "smol")
        ):
            payload["summary"]["status"] = "warn"
            summary_reasons.append("day sidecar not fully online")
        else:
            payload["summary"]["status"] = "ok"
        payload["summary"]["reasons"] = summary_reasons
        payload["available_models"] = payload["model_8080"]["models"]
        return payload

    def _collect_activity_events(now_ts: float) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cron_events: list[dict[str, Any]] = []

        cron_path = root / ".runtime" / "cron_state.json"
        if cron_path.exists():
            try:
                raw = json.loads(cron_path.read_text(encoding="utf-8"))
            except Exception:
                raw = None
            if isinstance(raw, dict):
                for job_id, value in raw.items():
                    if isinstance(value, dict):
                        ts = _safe_epoch(value.get("last_run")) or _safe_epoch(value.get("ts")) or now_ts
                        status = str(value.get("status") or "")
                        detail = str(value.get("detail") or "")
                        message = f"cron {job_id}: {status}" + (f" · {detail}" if detail else "")
                        cron_events.append({
                            "ts": ts,
                            "type": "cron",
                            "source": "cron_state",
                            "message": message.strip(),
                        })
            elif isinstance(raw, list):
                for value in raw:
                    if not isinstance(value, dict):
                        continue
                    ts = _safe_epoch(value.get("ts")) or _safe_epoch(value.get("time")) or now_ts
                    job_id = str(value.get("job") or value.get("job_id") or value.get("source") or "cron")
                    detail = str(value.get("detail") or value.get("message") or "")
                    status = str(value.get("status") or value.get("state") or "")
                    message = f"{job_id}: {status}" + (f" · {detail}" if detail else "")
                    cron_events.append({"ts": ts, "type": "cron", "source": "cron_state", "message": message.strip()})

        for log_path, source_name in (
            (Path("/opt/homebrew/var/log/omlx_switch.log"), "omlx_switch"),
            (root / "casper.log", "casper_log"),
        ):
            for line in _tail_file_lines(log_path, max_lines=80):
                text = line.strip()
                if not text:
                    continue
                event = {
                    "ts": _extract_log_timestamp(text),
                    "type": _classify_activity_type(text, source_name),
                    "source": source_name,
                    "message": text,
                }
                events.append(event)

        cron_events.sort(key=lambda item: item.get("ts", 0.0), reverse=True)
        events.sort(key=lambda item: item.get("ts", 0.0), reverse=True)
        selected = cron_events[:8]
        selected_messages = {(item.get("source"), item.get("message")) for item in selected}
        for item in events:
            key = (item.get("source"), item.get("message"))
            if key in selected_messages:
                continue
            selected.append(item)
            if len(selected) >= 30:
                break
        return selected[:30]

    def _collect_pressure_telemetry(
        system: dict[str, Any],
        inference: dict[str, Any],
        activity: list[dict[str, Any]],
    ) -> dict[str, Any]:
        reasons: list[str] = []
        level = "ok"

        memory = system.get("memory") if isinstance(system, dict) else {}
        mac_pressure = system.get("macos_memory_pressure") if isinstance(system, dict) else {}
        mac_status = str((mac_pressure or {}).get("status") or "unknown")
        mac_free_percent = (mac_pressure or {}).get("free_percent")
        if mac_status == "critical":
            level = "critical"
            reasons.append(f"macOS memory pressure free {mac_free_percent}% < 20")
        elif mac_status == "warn" and level == "ok":
            level = "warn"
            reasons.append(f"macOS memory pressure free {mac_free_percent}% < 30")

        mem_percent = memory.get("percent") if isinstance(memory, dict) else None
        if isinstance(mem_percent, (int, float)):
            if mac_status != "ok" and mem_percent >= 92:
                level = "critical"
                reasons.append(f"memory pressure {mem_percent}% >= 92")
            elif mac_status != "ok" and mem_percent >= 85 and level == "ok":
                level = "warn"
                reasons.append(f"memory pressure {mem_percent}% >= 85")

        swap = system.get("swap") if isinstance(system, dict) else {}
        swap_percent = swap.get("percent") if isinstance(swap, dict) else None
        if isinstance(swap_percent, (int, float)):
            if mac_status == "ok" and swap_percent >= 75:
                reasons.append(f"swap reserved {swap_percent}% but macOS memory pressure is healthy")
            elif swap_percent >= 90:
                level = "critical"
                reasons.append(f"swap pressure {swap_percent}% >= 90")
            elif swap_percent >= 75 and level == "ok":
                level = "warn"
                reasons.append(f"swap pressure {swap_percent}% >= 75")

        cpu_load = system.get("loadavg") if isinstance(system, dict) else {}
        load_1m = cpu_load.get("1m") if isinstance(cpu_load, dict) else None
        if isinstance(load_1m, (int, float)):
            if load_1m >= 6.0:
                level = "critical" if level != "critical" else level
                reasons.append(f"loadavg_1m {load_1m} high")
            elif load_1m >= 3.8 and level == "ok":
                level = "warn"
                reasons.append(f"loadavg_1m {load_1m} elevated")

        inference_summary = inference.get("summary") if isinstance(inference, dict) else {}
        if inference_summary.get("status") == "critical":
            level = "critical"
            reasons.append("inference critical (8080/sidecar)")
        elif inference_summary.get("status") == "warn" and level == "ok":
            level = "warn"
            reasons.append("inference degraded")
            if inference_summary.get("reasons"):
                reasons.extend([str(x) for x in inference_summary.get("reasons")[:3]])

        recent_errors = 0
        for item in activity[:8]:
            if str(item.get("type") or "").lower() in {"error", "model_switch"} and "error" in str(item.get("message") or "").lower():
                recent_errors += 1
        if recent_errors >= 3:
            level = "critical"
            reasons.append(f"recent error events >=3 in latest 8")
        elif recent_errors:
            level = "warn" if level == "ok" else level
            reasons.append("recent inference/runtime error events")

        if not reasons and level == "ok":
            reasons.append("no pressure warning")
        return {"level": level, "reasons": reasons}

    def _collect_nerv_telemetry() -> dict[str, Any]:
        now_ts = time.time()
        system = _collect_system_telemetry(now_ts)
        inference = _collect_inference_telemetry()
        activity_events = _collect_activity_events(now_ts)
        pressure = _collect_pressure_telemetry(
            system=system,
            inference=inference,
            activity=activity_events,
        )

        return {
            "timestamp": datetime.fromtimestamp(now_ts).isoformat(),
            "system": system,
            "inference": inference,
            "activity": {
                "events": activity_events,
                "count": len(activity_events),
            },
            "pressure": pressure,
        }

    def _mac_screen_sharing_status(tailscale: dict[str, Any]) -> dict[str, Any]:
        running = _launchctl_list_contains("com.apple.screensharing") or _launchctl_list_contains("RemoteDesktop")
        host = str(tailscale.get("dns_name") or tailscale.get("ip") or socket.gethostname() or "").strip()
        vnc_url = f"vnc://{host}" if host else ""
        return {
            "running": running,
            "status": "online" if running else "manual",
            "vnc_url": vnc_url,
        }

    def _remote_access_payload() -> dict[str, Any]:
        tailscale = _tailscale_status()
        chrome_remote = _chrome_remote_desktop_status()
        screen_sharing = _mac_screen_sharing_status(tailscale)
        cloudflare_url = _cloudflare_tunnel_url()
        return {
            "ok": True,
            "hostname": socket.gethostname(),
            "google_remote_desktop": chrome_remote,
            "tailscale": tailscale,
            "screen_sharing": screen_sharing,
            "cloudflare": {
                "status": "online" if cloudflared_alive() else "offline",
                "url": cloudflare_url,
            },
            "policy": {
                "public_vnc_exposed": False,
                "message": "只提供已驗證遠端工具入口；不開放裸 VNC 到公網。",
            },
        }

    @bp.route("/dashboard/nerv/api/health")
    @login_required
    def nerv_api_health():
        import requests as _rq

        results: dict[str, Any] = {}

        def _check(name, fn):
            try:
                results[name] = fn()
            except Exception as exc:
                results[name] = {"status": "error", "detail": str(exc)[:120]}

        def _omlx():
            try:
                _omlx_url = os.environ.get("MAGI_OMLX_CHAT_URL", "http://127.0.0.1:11434")
                response = _rq.get(f"{_omlx_url}/v1/models", timeout=3)
                if response.status_code == 200:
                    models = [item.get("id", "?") for item in (response.json().get("data") or [])]
                    return {"status": "online", "models": models, "count": len(models)}
            except Exception:
                logger.debug("silent-catch in nerv_api_health omlx", exc_info=True)
            return {"status": "error", "detail": "unreachable"}

        def _glm_ocr():
            # GLM-OCR retired — report macOS Vision OCR status instead
            try:
                from skills.apple.apple_intelligence import VISION_AVAILABLE
                if VISION_AVAILABLE:
                    return {"status": "online", "engine": "macOS Vision", "models": ["VNRecognizeTextRequest"], "count": 1}
            except Exception:
                logging.getLogger(__name__).warning("nonfatal exception was ignored at %s:%s", __name__, 704, exc_info=True)
            return {"status": "offline", "detail": "macOS Vision OCR unavailable (GLM-OCR retired)"}

        def _ollama():
            try:
                response = _rq.get("http://127.0.0.1:11434/api/tags", timeout=2)
                if response.status_code == 200:
                    models = [item.get("name", "?") for item in (response.json().get("models") or [])]
                    return {"status": "online", "models": models, "count": len(models)}
            except Exception:
                logger.debug("silent-catch in nerv_api_health ollama", exc_info=True)
            return {"status": "retired", "detail": "已退役，推理走 oMLX"}

        def _melchior():
            return {"status": "local", "detail": "oMLX 本地推理"}

        def _balthasar():
            return {"status": "local", "detail": "oMLX 本地摘要"}

        def _watcher():
            return {"status": "retired", "detail": "由 Worldmonitor 取代"}

        def _mysql():
            try:
                conn = mysql_connector.connect(
                    host=os.environ.get("DB_HOST", "127.0.0.1"),
                    port=int(os.environ.get("DB_PORT", "3306")),
                    user=os.environ.get("DB_USER", "casper_service"),
                    password=os.environ.get("DB_PASSWORD") or os.environ.get("MAGI_REMOTE_DB_PASSWORD", ""),
                    connection_timeout=4,
                    use_pure=True,
                )
                conn.close()
                return {"status": "online"}
            except Exception as exc:
                return {"status": "error", "detail": str(exc)[:80]}

        def _cloudflared():
            try:
                if cloudflared_alive():
                    return {"status": "online"}
                return {"status": "offline"}
            except Exception:
                return {"status": "error", "detail": "check failed"}

        def _line_webhook():
            try:
                webhook = os.environ.get("MAGI_LINE_WEBHOOK_ENDPOINT", "")
                if not webhook:
                    return {"status": "offline", "detail": "no endpoint configured"}
                response = _rq.get(webhook.replace("/line/webhook", "/health"), timeout=5)
                return {"status": "online" if response.status_code == 200 else "error"}
            except Exception:
                return {"status": "error", "detail": "unreachable"}

        def _worldmonitor():
            try:
                import subprocess as _sp

                result = _sp.run(["pgrep", "-f", "worldmonitor"], capture_output=True, timeout=3)
                return {"status": "online" if result.returncode == 0 else "offline"}
            except Exception:
                return {"status": "error"}

        def _office_app():
            try:
                response = _rq.get("http://127.0.0.1:4200/office", timeout=4)
                return {"status": "online" if response.status_code == 200 else "error", "detail": f"HTTP {response.status_code}"}
            except Exception:
                return {"status": "skipped", "detail": "disabled (not running)"}

        def _caddy_proxy():
            return {"status": "skipped", "detail": "removed (direct cloudflared→5002)"}

        def _skills():
            docs = list_skill_docs()
            found = [
                item["name"]
                for item in docs
                if not item["name"].startswith(("_", "."))
                and item["name"] not in {"bridge", "ops", "memory", "evolution", "brain_manager"}
            ]
            return {"status": "online", "skills": found, "count": len(found)}

        checks = {
            "omlx": _omlx,
            "glm_ocr": _glm_ocr,
            "ollama": _ollama,
            "melchior": _melchior,
            "balthasar": _balthasar,
            "watcher": _watcher,
            "mysql": _mysql,
            "cloudflared": _cloudflared,
            "line_webhook": _line_webhook,
            "worldmonitor": _worldmonitor,
            "office_app": _office_app,
            "caddy_proxy": _caddy_proxy,
            "skills": _skills,
            "remote_access": _remote_access_payload,
        }
        futures = {name: io_pool.submit(fn) for name, fn in checks.items()}
        for name, future in futures.items():
            try:
                results[name] = future.result(timeout=8)
            except Exception as exc:
                results[name] = {"status": "error", "detail": str(exc)[:80]}

        results["magi_server"] = {"status": "online", "pid": os.getpid()}
        results["timestamp"] = datetime.now().isoformat()
        try:
            results["telemetry"] = _collect_nerv_telemetry()
        except Exception as exc:
            results["telemetry"] = {"status": "error", "detail": str(exc)[:140], "timestamp": datetime.now().isoformat()}

        # FAISS vector DB stats
        try:
            from skills.memory.faiss_index import FAISSMemoryIndex
            idx = FAISSMemoryIndex.get_instance()
            results["faiss"] = {"ok": True, "vectors": getattr(idx, "total", 0), "index_type": getattr(idx, "index_type", "unknown")}
        except Exception:
            _meta_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "..", "skills", "memory", "index_cache", "meta.json")
            try:
                import json as _json
                with open(_meta_path, "r", encoding="utf-8") as _f:
                    _meta = _json.load(_f)
                results["faiss"] = {"ok": True, "vectors": _meta.get("total", 0), "index_type": _meta.get("index_type", "unknown")}
            except Exception:
                results["faiss"] = {"ok": False, "vectors": 0}

        return jsonify(results)

    @bp.route("/api/nerv/remote-access", methods=["GET"])
    def api_nerv_remote_access():
        auth_error = require_json_auth()
        if auth_error:
            return auth_error
        try:
            return jsonify(_remote_access_payload())
        except Exception as exc:
            logger.error("NERV remote access status failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/nerv/remote-access/action", methods=["POST"])
    def api_nerv_remote_access_action():
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        action = str(payload.get("action") or "").strip()
        actions = {
            "open_google_remote_desktop": [
                "open",
                "https://remotedesktop.google.com/access",
            ],
            "open_google_remote_setup": [
                "open",
                "https://remotedesktop.google.com/headless",
            ],
            "open_screen_sharing_settings": [
                "open",
                "x-apple.systempreferences:com.apple.Screen-Sharing-Settings.extension",
            ],
            "open_tailscale": [
                "open",
                "-a",
                "Tailscale",
            ],
        }
        cmd = actions.get(action)
        if not cmd:
            return jsonify({"ok": False, "error": "unsupported_action"}), 400
        try:
            subprocess.Popen(cmd, cwd=str(root))
            return jsonify({"ok": True, "action": action, "remote_access": _remote_access_payload()})
        except Exception as exc:
            logger.error("NERV remote access action failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/system-test", methods=["POST"])
    @login_required
    def api_system_test():
        try:
            from skills.ops.system_test import run_all_tests

            return jsonify(run_all_tests())
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/self-repair", methods=["POST"])
    @login_required
    def api_self_repair():
        try:
            data = request.get_json() or {}
            targets = data.get("targets")
            base_dir = root / "skills"
            candidates = [
                base_dir / "magi-self-repair" / "action.py",
                base_dir / "magi-doctor" / "action.py",
            ]
            repair_mod = None
            for action_path in candidates:
                if not action_path.exists():
                    continue
                spec = importlib.util.spec_from_file_location("magi_self_repair", action_path)
                if spec is None or spec.loader is None:
                    continue
                repair_mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(repair_mod)
                break
            if repair_mod is None:
                raise FileNotFoundError("No self-repair module found. Tried: " + ", ".join(str(item) for item in candidates))
            if not hasattr(repair_mod, "repair_targets"):
                raise AttributeError("self-repair module missing repair_targets()")
            return jsonify(repair_mod.repair_targets(targets))
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @bp.route("/api/nerv/skill-interview", methods=["GET"])
    def api_nerv_skill_interview_status():
        auth_error = require_json_auth()
        if auth_error:
            return auth_error
        try:
            state = orchestrator.get_skill_interview_state(nerv_skill_interview_user_id(), "NERV")
            return jsonify(
                {
                    "ok": True,
                    "can_edit": bool(getattr(current_user, "is_admin", False)),
                    "interview": state,
                }
            )
        except Exception as exc:
            logger.error("NERV skill interview status failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/nerv/skill-interview/start", methods=["POST"])
    def api_nerv_skill_interview_start():
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        initial_request = str(payload.get("request") or "").strip()
        if not initial_request:
            return jsonify({"ok": False, "error": "empty_request"}), 400
        try:
            message = orchestrator.start_skill_interview(
                nerv_skill_interview_user_id(),
                "NERV",
                getattr(current_user, "role", "user"),
                initial_request,
                trigger_reason="manual",
            )
            state = orchestrator.get_skill_interview_state(nerv_skill_interview_user_id(), "NERV")
            return jsonify({"ok": True, "message": message, "interview": state})
        except Exception as exc:
            logger.error("NERV skill interview start failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/nerv/skill-interview/reply", methods=["POST"])
    def api_nerv_skill_interview_reply():
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        payload = request.get_json(silent=True) or {}
        reply_text = str(payload.get("message") or "").strip()
        if not reply_text:
            return jsonify({"ok": False, "error": "empty_message"}), 400
        try:
            handled, message = orchestrator.reply_skill_interview(
                nerv_skill_interview_user_id(),
                "NERV",
                getattr(current_user, "role", "user"),
                reply_text,
            )
            if not handled:
                return jsonify({"ok": False, "error": "no_active_interview"}), 400
            state = orchestrator.get_skill_interview_state(nerv_skill_interview_user_id(), "NERV")
            finalized = (not state.get("active")) and ("新 SKILL 已建立並啟用" in str(message or ""))
            cancelled = (not state.get("active")) and ("已取消這次 SKILL 訪談" in str(message or ""))
            return jsonify(
                {
                    "ok": True,
                    "message": message,
                    "interview": state,
                    "finalized": finalized,
                    "cancelled": cancelled,
                    "skill_name": extract_interview_skill_name(message),
                }
            )
        except Exception as exc:
            logger.error("NERV skill interview reply failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/skills/interview-history", methods=["GET"])
    def api_skill_interview_history():
        auth_error = require_json_auth()
        if auth_error:
            return auth_error
        limit = request.args.get("limit", default=10, type=int) or 10
        limit = max(1, min(limit, 50))
        try:
            from skills.management.skill_interview import list_interview_history

            return jsonify({"ok": True, "history": list_interview_history(limit=limit)})
        except Exception as exc:
            logger.error("Skill interview history failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/skills/<skill_name>/versions", methods=["GET"])
    def api_skill_versions(skill_name):
        auth_error = require_json_auth()
        if auth_error:
            return auth_error
        try:
            skill_doc_path(skill_name)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        try:
            from skills.evolution.skill_genesis import list_skill_versions

            result = list_skill_versions(str(skill_name).strip())
            if not result.get("success"):
                return jsonify({"ok": False, "error": result.get("error") or "versions_unavailable"}), 404
            return jsonify({"ok": True, "versions": result.get("versions") or []})
        except Exception as exc:
            logger.error("Skill versions failed for %s: %s", skill_name, exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/skills/<skill_name>/rollback", methods=["POST"])
    def api_skill_rollback(skill_name):
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        try:
            skill_doc_path(skill_name)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        payload = request.get_json(silent=True) or {}
        version_id = str(payload.get("version_id") or "").strip()
        try:
            from skills.evolution.skill_genesis import rollback_skill_version
            from skills.bridge.embedding_router import get_router
            import skills.bridge.semantic_router as semantic_router

            result = rollback_skill_version(str(skill_name).strip(), version_id=version_id)
            if not result.get("success"):
                return jsonify({"ok": False, "error": result.get("error") or "rollback_failed"}), 400
            try:
                router = get_router()
                if router.is_ready:
                    router.rebuild_cache()
                else:
                    router.initialize()
            except Exception:
                logger.debug("silent-catch in api_skill_rollback router", exc_info=True)
            try:
                semantic_router._SKILLS_CACHE = None
                semantic_router._SKILLS_CACHE_TS = 0.0
            except Exception:
                logger.debug("silent-catch in api_skill_rollback semantic cache", exc_info=True)
            return jsonify({"ok": True, "result": result})
        except Exception as exc:
            logger.error("Skill rollback failed for %s: %s", skill_name, exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/nerv/skills", methods=["GET"])
    def api_nerv_skills():
        auth_error = require_json_auth()
        if auth_error:
            return auth_error
        try:
            return jsonify({"ok": True, "skills": list_skill_docs()})
        except Exception as exc:
            logger.error("NERV skill list failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/nerv/product-runtime", methods=["GET", "POST"])
    def api_nerv_product_runtime():
        auth_error = require_json_auth(admin=request.method == "POST")
        if auth_error:
            return auth_error
        if request.method == "GET":
            try:
                return jsonify(nerv_product_runtime_payload())
            except Exception as exc:
                logger.error("NERV product runtime load failed: %s", exc, exc_info=True)
                return jsonify({"ok": False, "error": str(exc)}), 500

        payload = request.get_json(silent=True) or {}
        product = str(payload.get("product") or "").strip().lower()
        if product not in nerv_product_names:
            return jsonify({"ok": False, "error": "unsupported_product"}), 400
        allowed_keys = {"codex_mode"}
        if product == "laf":
            allowed_keys |= {"portal_env", "prod_base_url", "test_base_url", "compare_base_url"}
        updates = {}
        for key in allowed_keys:
            value = payload.get(key)
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            updates[key] = text
        if not updates:
            return jsonify({"ok": False, "error": "empty_updates"}), 400
        try:
            updated = update_product_runtime(product, **updates)
            response = nerv_product_runtime_payload()
            response["updated_product"] = product
            response["updated_profile"] = updated
            return jsonify(response)
        except Exception as exc:
            logger.error("NERV product runtime save failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/nerv/heavy-runtime", methods=["GET", "POST"])
    def api_nerv_heavy_runtime():
        auth_error = require_json_auth(admin=request.method == "POST")
        if auth_error:
            return auth_error
        if request.method == "GET":
            return jsonify(_nerv_heavy_runtime_payload())

        payload = request.get_json(silent=True) or {}
        updates: dict[str, str] = {}
        if "enabled" in payload:
            updates["NVIDIA_NIM_ENABLE"] = "1" if bool(payload.get("enabled")) else "0"
        api_key = str(payload.get("api_key") or "").strip()
        if api_key:
            if not api_key.startswith("nvapi-"):
                return jsonify({"ok": False, "error": "invalid_prefix:nvapi-"}), 400
            updates["NVIDIA_NIM_API_KEY"] = api_key
        if not updates:
            return jsonify({"ok": False, "error": "empty_updates"}), 400
        try:
            backup = _write_env_values(env_path, updates)
            for key, value in updates.items():
                os.environ[key] = value
            response = _nerv_heavy_runtime_payload()
            response["saved"] = True
            response["backup"] = str(backup)
            response["restart_hint"] = "目前網頁程序已更新環境變數；背景工作或 daemon 若已載入舊環境，建議重啟 MAGI。"
            return jsonify(response)
        except Exception as exc:
            logger.error("NERV heavy runtime save failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/nerv/skills/<skill_name>", methods=["GET", "POST"])
    def api_nerv_skill_detail(skill_name):
        auth_error = require_json_auth(admin=request.method != "GET")
        if auth_error:
            return auth_error
        try:
            skill_doc = skill_doc_path(skill_name)
            action_file = skill_action_path(skill_name)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400

        if request.method == "GET":
            exists = skill_doc.exists()
            content = ""
            if exists:
                try:
                    content = skill_doc.read_text(encoding="utf-8")
                except Exception as exc:
                    return jsonify({"ok": False, "error": f"read_failed: {exc}"}), 500
            updated_at = ""
            stat_target = skill_doc if exists else action_file
            if stat_target.exists():
                try:
                    updated_at = datetime.fromtimestamp(stat_target.stat().st_mtime).isoformat()
                except Exception:
                    updated_at = ""
            return jsonify(
                {
                    "ok": True,
                    "skill": {
                        "name": str(skill_name).strip(),
                        "content": content,
                        "has_skill_doc": exists,
                        "has_action": action_file.exists(),
                        "updated_at": updated_at,
                        "summary": skill_summary(content),
                    },
                }
            )

        payload = request.get_json(silent=True) or {}
        content = str(payload.get("content") or "")
        if not content.strip():
            return jsonify({"ok": False, "error": "empty_skill_content"}), 400
        try:
            skill_doc.parent.mkdir(parents=True, exist_ok=True)
            normalized = content.replace("\r\n", "\n")
            if not normalized.endswith("\n"):
                normalized += "\n"
            skill_doc.write_text(normalized, encoding="utf-8")
        except Exception as exc:
            logger.error("NERV skill save failed for %s: %s", skill_name, exc, exc_info=True)
            return jsonify({"ok": False, "error": f"save_failed: {exc}"}), 500

        return jsonify(
            {
                "ok": True,
                "saved": True,
                "skill": {
                    "name": str(skill_name).strip(),
                    "content": normalized,
                    "has_skill_doc": True,
                    "has_action": action_file.exists(),
                    "updated_at": datetime.now().isoformat(),
                    "summary": skill_summary(normalized),
                },
            }
        )

    @bp.route("/api/codex-distributed/status", methods=["GET"])
    def api_codex_distributed_status():
        auth_error = require_json_auth()
        if auth_error:
            return auth_error
        try:
            from skills.bridge.llm_direct import public_status_report

            return jsonify({"status": public_status_report(), "can_toggle": current_user.is_admin()})
        except Exception as exc:
            logger.error("Codex distributed status failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    @bp.route("/api/codex-distributed/toggle", methods=["POST"])
    def api_codex_distributed_toggle():
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        try:
            from skills.bridge.llm_direct import apply_manual_command, public_status_report

            payload = request.get_json(silent=True) or {}
            command = str(payload.get("command") or "").strip().lower()
            features = payload.get("features")
            apply_manual_command(command, features=features)
            return jsonify({"status": public_status_report(), "can_toggle": True})
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        except Exception as exc:
            logger.error("Codex distributed toggle failed: %s", exc, exc_info=True)
            return jsonify({"ok": False, "error": str(exc)}), 500

    def _collect_drive_sync_status() -> dict[str, Any]:
        drive_dir = root / ".runtime" / "drive_sync"
        auth_required_path = drive_dir / "drive_case_sync_auth_required_latest.json"
        worker_state_path = drive_dir / "worker_state.json"
        try:
            drive_case_sync = importlib.import_module("api.osc.drive_case_sync")
            DriveCaseSyncAuthRequired = drive_case_sync.DriveCaseSyncAuthRequired
            build_drive_service = drive_case_sync.build_drive_service
        except Exception as exc:
            return {"ok": False, "status": "health_probe_failed", "detail": str(exc)[:120]}

        def _probe_drive_sync_auth(write_scope: bool) -> tuple[str, str]:
            try:
                probe_service = build_drive_service(interactive=False, write=write_scope)
                probe_service.files().get(fileId="root", fields="id").execute()
            except DriveCaseSyncAuthRequired as exc:
                return "auth_required", str(exc)
            except Exception as exc:
                return "health_probe_failed", str(exc)
            return "ok", ""

        def _mark_drive_sync_auth_recovered(
            source: dict[str, Any],
            *,
            write_scope: bool,
        ) -> dict[str, Any]:
            recovered_status = {
                "ok": True,
                "status": "ok",
                "action_required": False,
                "message": "Google Drive 授權已恢復；已清除舊授權警示",
                "finished_at": datetime.now().isoformat(),
                "token_path": str(source.get("token_path") or ""),
                "write_scope": write_scope,
            }
            try:
                auth_required_path.unlink(missing_ok=True)
                if worker_state_path.exists():
                    worker_state = json.loads(worker_state_path.read_text(encoding="utf-8"))
                    if isinstance(worker_state, dict):
                        last_summary = worker_state.get("last_summary")
                        if not isinstance(last_summary, dict):
                            last_summary = {}
                        worker_state["last_status"] = recovered_status
                        worker_state["last_summary"] = {
                            **last_summary,
                            "auth_required": False,
                            "auth_recovered": True,
                        }
                        tmp = worker_state_path.with_suffix(".tmp")
                        tmp.write_text(
                            json.dumps(worker_state, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        tmp.replace(worker_state_path)
            except Exception:
                logger.debug("silent-catch updating drive worker auth state", exc_info=True)
            return {
                "ok": True,
                "status": "ok",
                "detail": "Google Drive 授權已恢復；已清除過期授權警示",
                "token_path": recovered_status["token_path"],
                "write_scope": write_scope,
            }

        if not auth_required_path.exists() and not worker_state_path.exists():
            return {"ok": None, "status": "unknown", "detail": "尚未執行同步檢查"}

        try:
            if auth_required_path.exists():
                auth_required = json.loads(auth_required_path.read_text(encoding="utf-8"))
                write_scope = bool(auth_required.get("write_scope"))
                probe_status, probe_detail = _probe_drive_sync_auth(write_scope)
                if probe_status == "ok":
                    return _mark_drive_sync_auth_recovered(auth_required, write_scope=write_scope)
                if probe_status == "auth_required":
                    return {
                        "ok": False,
                        "status": "auth_required",
                        "message": str(probe_detail or auth_required.get("message") or "Google Drive 授權需重新建立")[:160],
                        "token_path": str(auth_required.get("token_path") or ""),
                        "write_scope": write_scope,
                    }
                return {
                    "ok": False,
                    "status": "health_probe_failed",
                    "detail": probe_detail[:160],
                    "token_path": str(auth_required.get("token_path") or ""),
                    "write_scope": write_scope,
                }

            worker_state = json.loads(worker_state_path.read_text(encoding="utf-8"))
            last_status = worker_state.get("last_status") if isinstance(worker_state.get("last_status"), dict) else {}
            last_summary = worker_state.get("last_summary") if isinstance(worker_state.get("last_summary"), dict) else {}
            if last_status.get("action_required"):
                if str(last_status.get("status") or "") == "auth_required":
                    write_scope = bool(last_status.get("write_scope"))
                    probe_status, probe_detail = _probe_drive_sync_auth(write_scope)
                    if probe_status == "ok":
                        return _mark_drive_sync_auth_recovered(last_status, write_scope=write_scope)
                    if probe_status == "auth_required":
                        return {
                            "ok": False,
                            "status": "auth_required",
                            "message": str(probe_detail or last_status.get("message") or "Google Drive 授權需重新建立")[:160],
                            "token_path": str(last_status.get("token_path") or ""),
                            "write_scope": write_scope,
                        }
                    return {
                        "ok": False,
                        "status": "health_probe_failed",
                        "detail": probe_detail[:160],
                        "token_path": str(last_status.get("token_path") or ""),
                        "write_scope": write_scope,
                    }
                return {
                    "ok": False,
                    "status": str(last_status.get("status") or "action_required"),
                    "message": str(last_status.get("message") or "Google Drive 同步需要處理")[:160],
                }
            return {
                "ok": True,
                "status": str(last_status.get("status") or "ok"),
                "detail": "最近同步檢查正常",
                "matched_case_folders": int(last_summary.get("matched_case_folders") or 0),
            }
        except Exception as exc:
            return {"ok": False, "status": "health_probe_failed", "detail": str(exc)[:120]}

    def _collect_api_token_health_status() -> dict[str, Any]:
        candidate_roots: list[Path] = []
        for key in ("MAGI_ROOT", "MAGI_ROOT_DIR"):
            value = os.environ.get(key)
            if value:
                candidate_roots.append(Path(value))
        candidate_roots.append(root)
        seen_paths: set[str] = set()
        report_path = root / ".runtime" / "token_health" / "token_health_latest.json"
        for candidate_root in candidate_roots:
            candidate = candidate_root / ".runtime" / "token_health" / "token_health_latest.json"
            key = str(candidate)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            if candidate.exists():
                report_path = candidate
                break
        if not report_path.exists():
            return {
                "ok": None,
                "status": "unknown",
                "detail": "尚未產生 API/OAuth token 健康檢查報告",
            }
        try:
            data = json.loads(report_path.read_text(encoding="utf-8"))
            age_sec = max(0.0, time.time() - report_path.stat().st_mtime)
            summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
            failures = data.get("failures") if isinstance(data.get("failures"), list) else []
            stale = age_sec > 12 * 3600
            ok = bool(data.get("ok")) and not stale
            return {
                "ok": ok,
                "status": "stale" if stale else ("ok" if data.get("ok") else "action_required"),
                "age_seconds": round(age_sec, 0),
                "summary": {
                    "total": int(summary.get("total") or 0),
                    "failures": int(summary.get("failures") or 0),
                    "refreshed": int(summary.get("refreshed") or 0),
                    "skipped": int(summary.get("skipped") or 0),
                },
                "failures": failures[:5],
            }
        except Exception as exc:
            return {"ok": False, "status": "invalid_report", "detail": str(exc)[:120]}

    def _collect_process_markers() -> dict[str, Any]:
        markers = {
            "daemon_markers": ("daemon.py", "api/discord_bot.py", "rpc-server"),
            "server_markers": ("api/server.py", "api/server", "api_server.py"),
        }
        process_lines: list[str] = []
        try:
            completed = subprocess.run(
                ["ps", "-axo", "command="],
                capture_output=True,
                text=True,
                timeout=2,
            )
            process_lines = [line.strip() for line in (completed.stdout or "").splitlines() if line.strip()]
        except Exception as exc:
            return {
                "ok": False,
                "status": "unknown",
                "error": str(exc)[:120],
                "daemon": {"ok": False, "markers": []},
                "server": {"ok": False, "markers": []},
            }
        daemon_matches = [m for m in markers["daemon_markers"] if any(m in line for line in process_lines)]
        server_matches = [m for m in markers["server_markers"] if any(m in line for line in process_lines)]
        return {
            "ok": bool(daemon_matches and server_matches),
            "status": "online" if (daemon_matches and server_matches) else "partial",
            "daemon": {"ok": bool(daemon_matches), "markers": daemon_matches},
            "server": {"ok": bool(server_matches), "markers": server_matches, "pid": os.getpid()},
            "raw_count": len(process_lines),
        }

    def _collect_db_status() -> dict[str, Any]:
        conn = None
        try:
            conn = mysql_connector.connect(**db_config, connection_timeout=3, use_pure=True)
            return {"ok": conn.is_connected()}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)[:120]}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    logging.getLogger(__name__).debug("DB status connection close failed", exc_info=True)

    def _collect_model_status() -> dict[str, Any]:
        try:
            from urllib.parse import urlparse as _urlparse
            from skills.bridge.http_pool import get_session as _get_session

            url = os.environ.get("MAGI_OMLX_CHAT_URL", os.environ.get("MAGI_OMLX_BASE", "http://127.0.0.1:8080"))
            parsed = _urlparse(str(url or ""))
            port = parsed.port or 8080
            response = _get_session().get(f"{url.rstrip('/')}/v1/models", timeout=2)
            ok = response.status_code == 200
            return {"ok": ok, "status": "ok" if ok else "unreachable", "port": port}
        except Exception as exc:
            return {"ok": False, "status": "probe_failed", "detail": str(exc)[:120]}

    def _collect_tools_api_status() -> dict[str, Any]:
        try:
            docs = list_skill_docs()
            if not isinstance(docs, list):
                raise TypeError("list_skill_docs must return list")
            return {"ok": True, "status": "ok", "count": len(docs)}
        except Exception as exc:
            return {"ok": False, "status": "error", "detail": str(exc)[:120], "count": 0}

    def _collect_nas_mount_status() -> dict[str, Any]:
        shares: dict[str, Any] = {}
        try:
            _nas_guard = importlib.import_module("api.nas_mount_guard")

            for share, vol in _nas_guard.get_configured_shares(refresh=True):
                try:
                    status = _nas_guard.get_share_status(share, vol)
                    shares[share] = bool(status.get("mounted"))
                except Exception:
                    shares[share] = False
            return {"ok": all(shares.values()) if shares else True, "mounts": shares}
        except Exception as exc:
            return {"ok": False, "mounts": {}, "detail": str(exc)[:120]}

    def _attach_runtime_diagnostics(name: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            from api.runtime_diagnostics import classify_model_health, classify_runtime_error
        except Exception:
            return payload
        out = dict(payload)
        if name == "model":
            out["classification"] = classify_model_health(out)
        if out.get("ok") is False:
            out["diagnosis"] = classify_runtime_error(out)
        return out

    @bp.route("/api/status")
    def api_status():
        try:
            return _load_status_payload()
        except Exception as exc:
            return {"error": str(exc)}, 500

    @bp.route("/api/live-log")
    @login_required
    def api_live_log():
        limit = min(int(request.args.get("limit", 40)), 100)
        lines = []
        try:
            with server_log_path.open("rb") as handle:
                handle.seek(0, 2)
                size = handle.tell()
                read_size = min(size, 32768)
                handle.seek(size - read_size)
                raw = handle.read().decode("utf-8", errors="replace")
                lines = raw.strip().splitlines()[-limit:]
        except Exception as exc:
            lines = [f"[LOG READ ERROR] {exc}"]
        return jsonify({"lines": lines})

    @bp.route("/api/live-validation", methods=["GET"])
    def api_live_validation():
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        process_markers = _collect_process_markers()
        checks = {
            "daemon": process_markers.get("daemon", {"ok": False}),
            "server": process_markers.get("server", {"ok": False}),
            "tools_api": _collect_tools_api_status(),
            "nas": _collect_nas_mount_status(),
            "drive": _collect_drive_sync_status(),
            "db": _collect_db_status(),
            "model": _collect_model_status(),
        }
        for check_name in ("daemon", "server", "tools_api", "nas", "drive", "db", "model"):
            checks[check_name] = _attach_runtime_diagnostics(check_name, checks[check_name])
        checks["timestamp"] = datetime.now().isoformat()
        issues = []
        for name, payload in checks.items():
            if name == "timestamp":
                continue
            if not payload.get("ok"):
                issues.append(name)
        checks["summary"] = {
            "ok": not issues,
            "status": "operational" if not issues else "degraded",
            "issues": issues,
        }
        checks["status"] = checks["summary"]["status"]
        return jsonify(checks), 200

    def _request_case_exclusion_paths(payload: dict[str, Any]) -> list[str]:
        raw = payload.get("relative_paths")
        if raw is None:
            raw = payload.get("relative_path")
        if raw is None:
            return []
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, list):
            return [str(item) for item in raw]
        return []

    def _case_exclusion_file_for_runtime() -> Path:
        return root / ".runtime" / "drive_sync" / "case_exclusions.json"

    @bp.route("/api/drive-case-exclusions", methods=["GET"])
    @login_required
    def api_drive_case_exclusions_list():
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        _dcs = importlib.import_module("api.osc.drive_case_sync")

        payload = _dcs.load_case_exclusion_payload(include_env=False, exclusion_path=_case_exclusion_file_for_runtime())
        return jsonify({"ok": True, "count": len(payload.get("relative_paths") or []), **payload}), 200

    @bp.route("/api/drive-case-exclusions", methods=["POST"])
    @login_required
    def api_drive_case_exclusions_add():
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        _dcs = importlib.import_module("api.osc.drive_case_sync")

        data = request.get_json(silent=True) or {}
        paths = _request_case_exclusion_paths(data)
        if not paths:
            return jsonify({"ok": False, "error": "missing_relative_paths"}), 400

        exclusion_path = _case_exclusion_file_for_runtime()
        before = set(_dcs.load_case_exclusion_payload(include_env=False, exclusion_path=exclusion_path).get("relative_paths") or [])
        updated = _dcs.sync_case_exclusions(
            paths,
            reason=str(data.get("reason") or ""),
            exclusion_path=exclusion_path,
        )
        after = set(updated.get("relative_paths") or [])
        return jsonify({"ok": True, "changed": after != before, **updated}), 200

    @bp.route("/api/drive-case-exclusions", methods=["DELETE"])
    @login_required
    def api_drive_case_exclusions_remove():
        auth_error = require_json_auth(admin=True)
        if auth_error:
            return auth_error
        _dcs = importlib.import_module("api.osc.drive_case_sync")

        data = request.get_json(silent=True) or {}
        paths = _request_case_exclusion_paths(data)
        if not paths:
            return jsonify({"ok": False, "error": "missing_relative_paths"}), 400
        updated, removed = _dcs.unsync_case_exclusions(paths, exclusion_path=_case_exclusion_file_for_runtime())
        return jsonify({"ok": True, "changed": removed > 0, "removed": removed, **updated}), 200

    def _uptime_seconds() -> float:
        try:
            return round(max(0.0, time.time() - float(server_start_time)), 3)
        except Exception:
            return 0.0

    @bp.route("/livez", methods=["GET"])
    def livez():
        return jsonify({
            "ok": True,
            "status": "live",
            "timestamp": time.time(),
            "uptime_seconds": _uptime_seconds(),
        }), 200

    @bp.route("/readyz", methods=["GET"])
    def readyz():
        runtime_dir = root / ".runtime"
        root_ok = root.exists() and root.is_dir()
        runtime_ok = (
            (runtime_dir.exists() and runtime_dir.is_dir() and os.access(runtime_dir, os.W_OK))
            or (not runtime_dir.exists() and root_ok and os.access(root, os.W_OK))
        )
        db_ok = bool(
            str(db_config.get("host") or "").strip()
            and str(db_config.get("user") or "").strip()
            and str(db_config.get("password") or "").strip()
        )
        checks = {
            "root": {"ok": bool(root_ok), "path": str(root)},
            "runtime_dir": {"ok": bool(runtime_ok), "path": str(runtime_dir)},
            "db_config": {
                "ok": bool(db_ok),
                "host_configured": bool(str(db_config.get("host") or "").strip()),
                "user_configured": bool(str(db_config.get("user") or "").strip()),
                "password_configured": bool(str(db_config.get("password") or "").strip()),
            },
        }
        ok = all(bool(item.get("ok")) for item in checks.values())
        return jsonify({
            "ok": ok,
            "status": "ready" if ok else "not_ready",
            "timestamp": time.time(),
            "uptime_seconds": _uptime_seconds(),
            "checks": checks,
        }), 200 if ok else 503

    @bp.route("/health", methods=["GET"])
    def health():
        import time as _time
        import subprocess as _sp
        from urllib.parse import urlparse as _urlparse
        from skills.bridge.http_pool import get_session as _get_session

        sess = _get_session()
        cache_now = _time.time()
        bypass_cache = str(request.args.get("fresh") or "").strip().lower() in {"1", "true", "yes", "on"}
        cached_checks = health_cache.get("checks")
        if (
            isinstance(cached_checks, dict)
            and not bypass_cache
            and cache_now - float(health_cache.get("ts") or 0.0) < health_cache_ttl_sec
        ):
            checks = copy.deepcopy(cached_checks)
            checks["cached"] = True
            checks["cache_age_seconds"] = round(cache_now - float(health_cache.get("ts") or 0.0), 3)
            if not _wants_json_response():
                return _render_health_html(checks), 200
            return jsonify(checks), 200

        checks: dict[str, Any] = {"status": "operational", "timestamp": _time.time()}

        def _extract_port(base_url: str, fallback: int) -> int:
            try:
                parsed = _urlparse(str(base_url or ""))
                return int(parsed.port or fallback)
            except Exception:
                return int(fallback)

        def _launchctl_has_label(label: str) -> Optional[bool]:
            if not label:
                return None
            try:
                rc = _sp.run(
                    ["launchctl", "list", label],
                    capture_output=True,
                    text=True,
                    timeout=2,
                ).returncode
                return rc == 0
            except Exception:
                return None

        def _resolve_launchctl_label(label: str, aliases: tuple[str, ...] = ()) -> dict[str, Any]:
            checked: list[dict[str, Any]] = []
            for candidate in (label, *aliases):
                state = _launchctl_has_label(candidate)
                checked.append({"label": candidate, "present": state})
                if state is True:
                    return {
                        "managed": True,
                        "active_label": candidate,
                        "checked": checked,
                    }
            if any(item["present"] is None for item in checked):
                managed: Optional[bool] = None
            else:
                managed = False
            return {"managed": managed, "active_label": "", "checked": checked}

        def _probe_omlx_service(
            *,
            service_id: str,
            name: str,
            base_url: str,
            port: int,
            label: str,
            aliases: tuple[str, ...] = (),
        ) -> dict[str, Any]:
            service: dict[str, Any] = {
                "id": service_id,
                "name": name,
                "base_url": str(base_url).rstrip("/"),
                "port": int(port),
                "label": label,
                "label_aliases": list(aliases),
                "reachable": False,
                "http_status": 0,
                "models": [],
                "managed": None,
                "management_state": "unknown",
            }
            try:
                response = sess.get(f"{service['base_url']}/v1/models", timeout=3)
                service["http_status"] = int(getattr(response, "status_code", 0) or 0)
                if service["http_status"] == 200:
                    models = [item.get("id", "") for item in (response.json() or {}).get("data", [])]
                    service["models"] = [m for m in models if m]
                    service["reachable"] = True
            except Exception as exc:
                service["error"] = str(exc)[:120]

            label_state = _resolve_launchctl_label(label, aliases=aliases)
            service["managed"] = label_state["managed"]
            service["active_label"] = label_state["active_label"]
            service["launchctl_checked"] = label_state["checked"]
            if label_state["managed"] is True:
                service["management_state"] = "managed"
            elif label_state["managed"] is False:
                service["management_state"] = "unmanaged"
            else:
                service["management_state"] = "unknown"
            service["ok"] = bool(service["reachable"]) and service["management_state"] != "unmanaged"
            return service

        try:
            _chat_url = os.environ.get("MAGI_OMLX_CHAT_URL", os.environ.get("MAGI_OMLX_BASE", "http://127.0.0.1:8080"))
            _phi4_url = os.environ.get("MAGI_OMLX_PHI4_URL", f"http://127.0.0.1:{os.environ.get('MAGI_OMLX_PHI4_PORT', '8082')}")
            _smol_url = os.environ.get("MAGI_OMLX_SMOL_URL", f"http://127.0.0.1:{os.environ.get('MAGI_OMLX_SMOL_PORT', '8083')}")
            services = [
                _probe_omlx_service(
                    service_id="text",
                    name="Gemma-4",
                    base_url=_chat_url,
                    port=_extract_port(_chat_url, 8080),
                    label="com.magi.omlx",
                ),
                _probe_omlx_service(
                    service_id="phi4",
                    name="Phi-4",
                    base_url=_phi4_url,
                    port=_extract_port(_phi4_url, 8082),
                    label="com.magi.omlx-phi4",
                ),
                _probe_omlx_service(
                    service_id="smol",
                    name="SmolLM3",
                    base_url=_smol_url,
                    port=_extract_port(_smol_url, 8083),
                    label="com.magi.omlx-smol",
                    aliases=("com.magi.omlx-smollm3",),
                ),
            ]
            service_map = {svc["id"]: svc for svc in services}
            primary = service_map.get("text") or {}
            unmanaged_alive = [svc["id"] for svc in services if svc.get("reachable") and svc.get("management_state") == "unmanaged"]
            active_profile = ""
            try:
                active_profile = (Path.home() / ".omlx" / "active_profile").read_text(encoding="utf-8").strip()
            except Exception:
                active_profile = ""
            day_sidecars_required = active_profile == "day"
            sidecar_failures = []
            if day_sidecars_required:
                for sid in ("phi4", "smol"):
                    svc = service_map.get(sid) or {}
                    if not svc.get("ok"):
                        sidecar_failures.append(sid)
            primary_ok = bool(primary.get("reachable")) and primary.get("management_state") != "unmanaged"
            checks["omlx"] = {
                "ok": primary_ok and not unmanaged_alive and not sidecar_failures,
                "models": primary.get("models", []),
                "services": service_map,
                "unmanaged_alive": unmanaged_alive,
                "active_profile": active_profile,
                "day_sidecars_required": day_sidecars_required,
            }
            degraded_reasons = [f"unmanaged_service:{sid}" for sid in unmanaged_alive]
            degraded_reasons.extend(f"day_sidecar_down:{sid}" for sid in sidecar_failures)
            if degraded_reasons:
                checks["omlx"]["degraded_reasons"] = degraded_reasons
        except Exception:
            checks["omlx"] = {"ok": False}

        # GLM-OCR retired — check macOS Vision OCR availability instead
        try:
            from skills.apple.apple_intelligence import VISION_AVAILABLE
            checks["ocr"] = {"ok": VISION_AVAILABLE, "engine": "macOS Vision", "note": "GLM-OCR retired"}
        except Exception:
            checks["ocr"] = {"ok": False, "engine": "macOS Vision", "note": "import failed"}

        checks["browser_core"] = _browser_core_health_hard_timeout(
            timeout_seconds=max(1, int(os.environ.get("MAGI_BROWSER_HEALTH_TIMEOUT_SEC", "3") or "3")),
            cache_ttl_seconds=max(30, int(os.environ.get("MAGI_BROWSER_HEALTH_CACHE_TTL_SEC", "300") or "300")),
        )

        conn = None
        try:
            conn = mysql_connector.connect(**db_config, connection_timeout=3, use_pure=True)
            checks["db"] = {"ok": conn.is_connected()}
        except Exception as exc:
            checks["db"] = {"ok": False, "detail": str(exc)[:80]}
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    logger.debug("silent-catch in health db close", exc_info=True)

        try:
            import psutil

            vm = psutil.virtual_memory()
            du = psutil.disk_usage("/")
            checks["system"] = {
                "cpu_percent": psutil.cpu_percent(interval=0.05),
                "memory_percent": vm.percent,
                "memory_available_gb": round(vm.available / (1024**3), 1),
                "disk_percent": du.percent,
                "disk_free_gb": round(du.free / (1024**3), 1),
            }
        except Exception:
            logger.debug("silent-catch in health system", exc_info=True)

        uptime = _time.time() - server_start_time
        if uptime < 60:
            checks["faiss"] = {"ok": True, "deferred": True, "reason": "startup_grace_period"}
        else:
            try:
                from skills.memory.faiss_index import FAISSMemoryIndex

                idx = FAISSMemoryIndex.get_instance()
                checks["faiss"] = {"ok": True, "vectors": getattr(idx, "total", getattr(idx, "ntotal", 0))}
            except Exception:
                checks["faiss"] = {"ok": False}

        try:
            if attachment_job_queue:
                checks["attachment_jobs"] = attachment_job_queue.stats()
            else:
                job_ids = list_attachment_job_ids()
                pending = sum(1 for job_id in job_ids if read_attachment_job(job_id).get("status") in ("queued", "running"))
                checks["attachment_jobs"] = {"total": len(job_ids), "active": pending}
        except Exception:
            logger.debug("silent-catch in health attachment_jobs", exc_info=True)

        checks["drive_sync"] = _collect_drive_sync_status()
        checks["api_token_health"] = _collect_api_token_health_status()

        try:
            audit_path = root / ".runtime" / "operational_hardening_audit_latest.json"
            if audit_path.exists():
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                age_sec = max(0.0, time.time() - audit_path.stat().st_mtime)
                cron = audit.get("cron") or {}
                git = audit.get("git") or {}
                checks["operational_audit"] = {
                    "ok": (
                        int(cron.get("parse_failure_count") or 0) == 0
                        and int(cron.get("collision_count") or 0) == 0
                        and age_sec < 36 * 3600
                    ),
                    "age_seconds": round(age_sec, 0),
                    "cron_parse_failures": int(cron.get("parse_failure_count") or 0),
                    "cron_collisions": int(cron.get("collision_count") or 0),
                    "dirty_count": int(git.get("dirty_count") or 0),
                    "generated_or_runtime_count": int(git.get("generated_or_runtime_count") or 0),
                }
            else:
                checks["operational_audit"] = {"ok": False, "missing": True}
        except Exception as exc:
            checks["operational_audit"] = {"ok": False, "detail": str(exc)[:120]}

        # 2026-04-25 P2-7: operational_health — count cron failures + benchmark freshness
        try:
            now_ts = _time.time()
            issue_health = _compute_operational_issue_health(root, now_ts)
            cron_failures_24h = int(issue_health.get("active_cron_failures_24h", 0))
            high_severity_24h = int(issue_health.get("active_high_severity_24h", 0))
            distinct_jobs_24h = int(issue_health.get("active_distinct_jobs_24h", 0))

            # Benchmark freshness (pdf_namer / pdf_bookmarker)
            bench_freshness = {}
            for _bn in ("benchmark_pdf_namer_latest.json", "benchmark_pdf_bookmarker_latest.json"):
                _bp = root / ".runtime" / _bn
                if _bp.exists():
                    _age_h = (_time.time() - _bp.stat().st_mtime) / 3600
                    bench_freshness[_bn.replace("_latest.json", "")] = round(_age_h, 1)
                else:
                    bench_freshness[_bn.replace("_latest.json", "")] = None

            # Watchdog decisions
            wd_path = root / ".runtime" / "metrics" / "memory_watchdog_decisions.jsonl"
            wd_decisions_24h = 0
            if wd_path.exists():
                with open(wd_path, encoding="utf-8") as _fh:
                    for _line in _fh:
                        try:
                            _r = _json_h.loads(_line)
                            if float(_r.get("ts", 0)) >= cutoff_24h:
                                wd_decisions_24h += 1
                        except Exception:
                            continue

            _op_health = {
                "cron_failures_24h": cron_failures_24h,
                "distinct_failing_jobs_24h": distinct_jobs_24h,
                "issue_agenda_high_severity_24h": high_severity_24h,
                "watchdog_decisions_24h": wd_decisions_24h,
                "benchmark_age_hours": bench_freshness,
                "active_unresolved_24h": {
                    "cron_failures": cron_failures_24h,
                    "issue_agenda_high_severity": high_severity_24h,
                    "distinct_failing_jobs": distinct_jobs_24h,
                },
                "raw_counts_24h": {
                    "cron_failures": int(issue_health.get("raw_cron_failures_24h", 0)),
                    "issue_agenda_high_severity": int(issue_health.get("raw_high_severity_24h", 0)),
                    "for_context_only": True,
                },
                "inactive_or_recovered_24h": {
                    "cron_failures": int(issue_health.get("inactive_cron_failures_24h", 0)),
                    "false_positive_cron_failures": int(issue_health.get("false_positive_cron_failures_24h", 0)),
                },
                "inactive_breakdown_24h": {
                    "recovered_cron_failures": int(issue_health.get("recovered_cron_failures_24h", 0)),
                    "superseded_cron_failures": int(issue_health.get("superseded_cron_failures_24h", 0)),
                    "stale_cron_failures": int(issue_health.get("stale_cron_failures_24h", 0)),
                    "false_positive_cron_failures": int(issue_health.get("false_positive_cron_failures_24h", 0)),
                    "recovered_non_cron_high_severity": int(
                        issue_health.get("recovered_non_cron_high_severity_24h", 0)
                    ),
                    "inactive_or_noise_cron_failures": int(
                        issue_health.get("inactive_or_noise_cron_failures_24h", 0)
                    ),
                },
                "active_issue_window_hours": round(
                    float(issue_health.get("active_window_sec", 0)) / 3600.0,
                    1,
                ),
            }
            _op_health["degraded_reasons"] = []
            if cron_failures_24h > 5:
                _op_health["degraded_reasons"].append(f"cron_failures_24h={cron_failures_24h}>5")
            if high_severity_24h > 10:
                _op_health["degraded_reasons"].append(f"issue_agenda_high_severity_24h={high_severity_24h}>10")
            token_health = checks.get("api_token_health") if isinstance(checks.get("api_token_health"), dict) else {}
            if token_health.get("ok") is False:
                _op_health["degraded_reasons"].append(f"api_token_health={token_health.get('status')}")
            for _b, _age in bench_freshness.items():
                if _age is not None and _age > 48:
                    _op_health["degraded_reasons"].append(f"{_b}_stale_{_age}h")
            _op_health["ok"] = len(_op_health["degraded_reasons"]) == 0
            checks["operational_health"] = _op_health
        except Exception as exc:
            checks["operational_health"] = {"ok": False, "detail": str(exc)[:120]}

        try:
            from api import nas_mount_guard as _nas_guard

            shares = _nas_guard.get_configured_shares(refresh=True)
            nas_detail = {vol.split("/")[-1]: _nas_guard.get_share_status(name, vol) for name, vol in shares}
            allow_request_remount = str(request.args.get("remount") or "").strip().lower() in {"1", "true", "yes", "on"}
            if allow_request_remount and any(not bool(detail.get("mounted")) for detail in nas_detail.values()):
                checks["nas_auto_remount_attempted"] = True
                try:
                    _nas_guard.ensure_nas_mounts()
                    shares = _nas_guard.get_configured_shares(refresh=True)
                    nas_detail = {
                        vol.split("/")[-1]: _nas_guard.get_share_status(name, vol)
                        for name, vol in shares
                    }
                except Exception as exc:
                    checks["nas_auto_remount_error"] = str(exc)[:120]
            elif any(not bool(detail.get("mounted")) for detail in nas_detail.values()):
                checks["nas_auto_remount_attempted"] = False
                checks["nas_auto_remount_skipped"] = "background_guard_handles_remount"
            checks["nas"] = {name: bool(detail.get("mounted")) for name, detail in nas_detail.items()}
            checks["nas_detail"] = nas_detail
        except Exception:
            logger.debug("silent-catch in health nas", exc_info=True)

        try:
            checks["uptime_seconds"] = round(_time.time() - server_start_time, 0)
        except Exception:
            logger.debug("silent-catch in health uptime", exc_info=True)

        degraded = not checks.get("omlx", {}).get("ok")
        if checks.get("browser_core", {}).get("ok") is False:
            degraded = True
        if checks.get("operational_audit", {}).get("ok") is False:
            degraded = True
        # 2026-04-25 P2-7: operational_health degradation also marks degraded
        if checks.get("operational_health", {}).get("ok") is False:
            degraded = True
        if checks.get("drive_sync", {}).get("ok") is False:
            degraded = True
        if checks.get("api_token_health", {}).get("ok") is False:
            degraded = True
        if any(ok is False for ok in checks.get("nas", {}).values()):
            degraded = True
        checks["status"] = "degraded" if degraded else "operational"
        checks["cached"] = False
        checks["cache_ttl_seconds"] = health_cache_ttl_sec
        health_cache["ts"] = _time.time()
        health_cache["checks"] = copy.deepcopy(checks)
        if not _wants_json_response():
            return _render_health_html(checks), 200
        return jsonify(checks), 200

    @bp.route("/api/transcribe", methods=["POST"])
    def transcribe_audio():
        import hmac

        api_key = (request.headers.get("X-MAGI-API-KEY") or "").strip()
        api_key_ok = bool(expected_magi_api_key) and hmac.compare_digest(api_key, expected_magi_api_key)
        if not api_key_ok and not current_user.is_authenticated:
            return jsonify({"error": "Unauthorized"}), 401
        if "file" not in request.files:
            return jsonify({"error": "No file part"}), 400
        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No selected file"}), 400
        try:
            safe_filename = "".join([char for char in file.filename if char.isalnum() or char in "._-"]) or "audio.wav"
            filename = f"audio_{int(time.time())}_{safe_filename}"
            filepath = os.path.join("/tmp", filename)
            file.save(filepath)
            logger.info("🎤 Received audio for transcription: %s", filepath)

            from skills.bridge.balthasar_bridge import transcribe

            language = str(request.form.get("language") or "").strip() or None
            taigi_hint_raw = str(request.form.get("taigi_hint") or "").strip().lower()
            taigi_hint = taigi_hint_raw in {"1", "true", "yes", "on"}
            result = transcribe(filepath, language=language, taigi_hint=taigi_hint)
            try:
                from api.handlers.output_quality_handler import estimate_transcript_source_chars_from_audio, run_output_quality_gate

                text = str((result or {}).get("text") or "").strip() if isinstance(result, dict) else ""
                gate = run_output_quality_gate(
                    "transcript",
                    text,
                    source_chars=estimate_transcript_source_chars_from_audio(filepath),
                    source_name=file.filename,
                    instruction="api/transcribe",
                )
                if not gate.get("ok"):
                    result = {"success": False, "error": "transcript_quality_gate:" + str(gate.get("issue") or "failed"), "quality_gate": gate}
            except Exception:
                logger.debug("transcript quality gate skipped", exc_info=True)
            if os.path.exists(filepath):
                safe_remove_tmp(filepath)
            return jsonify(result)
        except Exception as exc:
            logger.error("❌ Transcription endpoint error: %s", exc)
            return jsonify({"error": str(exc)}), 500

    return bp
