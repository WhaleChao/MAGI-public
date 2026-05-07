"""
Administrative / runtime status routes extracted from server.py.

This module keeps Web dashboard support, NERV APIs, system health probes,
and audio transcription wiring, while receiving runtime dependencies from the
main server bootstrap.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import socket
import subprocess
import time
from html import escape
from api.thread_pools import io_pool
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, Response, current_app, jsonify, request
from flask_login import current_user, login_required


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
    audit = checks.get("operational_audit") if isinstance(checks.get("operational_audit"), dict) else {}
    op = checks.get("operational_health") if isinstance(checks.get("operational_health"), dict) else {}

    services = [
        ("主狀態", status == "operational", status_text),
        ("資料庫", db.get("ok"), db.get("detail") or "MariaDB"),
        ("推論服務", omlx.get("ok"), ", ".join(omlx.get("models") or []) or "模型狀態"),
        ("OCR", (checks.get("ocr") or {}).get("ok") if isinstance(checks.get("ocr"), dict) else None, (checks.get("ocr") or {}).get("engine", "")),
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
    try:
        return float(value)
    except Exception:
        pass
    txt = str(value or "").strip()
    if not txt:
        return 0.0
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


def _classify_cron_issue(
    row: dict[str, Any],
    *,
    active_cutoff: float,
    latest_cron_issue_ts_by_job: dict[str, float],
    cron_last_run_ts: dict[str, float],
) -> str:
    if _is_false_positive_cron_issue(row):
        return "false_positive"

    ts = float(row.get("_ts") or 0.0)
    job_id = _cron_job_from_issue_command(row.get("command"))
    if not job_id:
        return "stale" if ts < active_cutoff else "active_unresolved"

    latest_issue_ts = latest_cron_issue_ts_by_job.get(job_id, ts)
    last_run_ts = cron_last_run_ts.get(job_id, 0.0)
    if latest_issue_ts > ts:
        return "superseded"
    if last_run_ts > ts:
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


def _load_cron_last_run_ts(root: Path) -> dict[str, float]:
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
        ts = _safe_epoch(data.get("last_run"))
        if ts > 0:
            out[str(job_id)] = ts
    return out


def _compute_operational_issue_health(root: Path, now_ts: float) -> dict[str, Any]:
    cutoff_24h = now_ts - 86400
    active_window_sec = int(os.environ.get("MAGI_OPERATIONAL_ACTIVE_ISSUE_WINDOW_SEC", "21600") or "21600")
    active_cutoff = now_ts - active_window_sec
    rows = _load_recent_issue_rows(root / ".runtime" / "issue_agenda.jsonl", cutoff_24h)
    cron_last_run_ts = _load_cron_last_run_ts(root)

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

    for row in rows:
        ts = float(row.get("_ts") or 0.0)
        source = str(row.get("source", ""))
        is_cron = source.startswith("discord_bot.cron_scheduler")
        is_high = str(row.get("severity", "")) in ("High", "Critical")
        if is_high:
            raw_high_severity += 1

        if not is_cron:
            if is_high and ts >= active_cutoff:
                active_high_severity += 1
            continue

        raw_cron_failures += 1
        state = _classify_cron_issue(
            row,
            active_cutoff=active_cutoff,
            latest_cron_issue_ts_by_job=latest_cron_issue_ts_by_job,
            cron_last_run_ts=cron_last_run_ts,
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
                pass
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

    @bp.route("/health", methods=["GET"])
    def health():
        import time as _time
        import subprocess as _sp
        from urllib.parse import urlparse as _urlparse
        from skills.bridge.http_pool import get_session as _get_session

        sess = _get_session()
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
            checks["omlx"] = {
                "ok": bool(primary.get("reachable")) and not unmanaged_alive,
                "models": primary.get("models", []),
                "services": service_map,
                "unmanaged_alive": unmanaged_alive,
            }
            if unmanaged_alive:
                checks["omlx"]["degraded_reasons"] = [f"unmanaged_service:{sid}" for sid in unmanaged_alive]
        except Exception:
            checks["omlx"] = {"ok": False}

        # GLM-OCR retired — check macOS Vision OCR availability instead
        try:
            from skills.apple.apple_intelligence import VISION_AVAILABLE
            checks["ocr"] = {"ok": VISION_AVAILABLE, "engine": "macOS Vision", "note": "GLM-OCR retired"}
        except Exception:
            checks["ocr"] = {"ok": False, "engine": "macOS Vision", "note": "import failed"}

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
            for _b, _age in bench_freshness.items():
                if _age is not None and _age > 48:
                    _op_health["degraded_reasons"].append(f"{_b}_stale_{_age}h")
            _op_health["ok"] = len(_op_health["degraded_reasons"]) == 0
            checks["operational_health"] = _op_health
        except Exception as exc:
            checks["operational_health"] = {"ok": False, "detail": str(exc)[:120]}

        try:
            from api.nas_mount_guard import _SHARES, get_share_available_path

            def _nas_check(share_name, vol):
                return bool(get_share_available_path(share_name, vol))

            checks["nas"] = {vol.split("/")[-1]: _nas_check(name, vol) for name, vol in _SHARES}
        except Exception:
            logger.debug("silent-catch in health nas", exc_info=True)

        try:
            checks["uptime_seconds"] = round(_time.time() - server_start_time, 0)
        except Exception:
            logger.debug("silent-catch in health uptime", exc_info=True)

        degraded = not checks.get("omlx", {}).get("ok")
        if checks.get("operational_audit", {}).get("ok") is False:
            degraded = True
        # 2026-04-25 P2-7: operational_health degradation also marks degraded
        if checks.get("operational_health", {}).get("ok") is False:
            degraded = True
        checks["status"] = "degraded" if degraded else "operational"
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
            if os.path.exists(filepath):
                safe_remove_tmp(filepath)
            return jsonify(result)
        except Exception as exc:
            logger.error("❌ Transcription endpoint error: %s", exc)
            return jsonify({"error": str(exc)}), 500

    return bp
