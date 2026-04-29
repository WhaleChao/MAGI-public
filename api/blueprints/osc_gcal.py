"""
OSC Google Calendar OAuth Blueprint
=====================================
Endpoints:
  GET  /api/osc/gcal/status
  POST /api/osc/gcal/auth/start
  GET  /api/osc/gcal/auth/callback
  POST /api/osc/gcal/disconnect
  POST /api/osc/gcal/sync

Token stored at ~/.magi/google/token.json (chmod 0600).
OAuth client_id/secret stored in DB settings table.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from flask import Blueprint, request, jsonify
from flask_login import login_required

logger = logging.getLogger(__name__)

osc_gcal_bp = Blueprint("osc_gcal", __name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_PATH = Path.home() / ".magi" / "google" / "token.json"
MAGI_PORT = int(os.environ.get("MAGI_PORT", "5002"))


def _get_osc_exec():
    """Lazy import to avoid circular imports."""
    from api.osc.utils import _osc_exec
    return _osc_exec


def _get_setting(key: str) -> str | None:
    """Read a single setting from the DB settings table."""
    try:
        _osc_exec = _get_osc_exec()
        row, _ = _osc_exec(
            "SELECT value FROM settings WHERE `key`=%s",
            (key,),
            fetch="one",
        )
        return row[0] if row else None
    except Exception as exc:
        logger.warning("_get_setting(%s) failed: %s", key, exc)
        return None


def _load_creds():
    """Load credentials from token.json, refreshing if expired."""
    if not TOKEN_PATH.exists():
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request

        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
            TOKEN_PATH.chmod(0o600)
        return creds
    except Exception as exc:
        logger.warning("_load_creds failed: %s", exc)
        return None


def _build_redirect_uri() -> str:
    return f"http://127.0.0.1:{MAGI_PORT}/api/osc/gcal/auth/callback"


# ── GET /api/osc/gcal/status ─────────────────────────────────────────────────

@osc_gcal_bp.route("/api/osc/gcal/status", methods=["GET"])
@login_required
def gcal_status():
    creds = _load_creds()
    if creds is None or not creds.valid:
        return jsonify({"ok": True, "connected": False})

    info = {"ok": True, "connected": True}
    try:
        token_data = json.loads(TOKEN_PATH.read_text())
        info["email"] = token_data.get("client_id", "")
        info["expires_at"] = token_data.get("expiry", "")
    except Exception:
        pass
    info["calendar_id"] = _get_setting("gcal_calendar_id") or "primary"
    return jsonify(info)


# ── POST /api/osc/gcal/auth/start ────────────────────────────────────────────

@osc_gcal_bp.route("/api/osc/gcal/auth/start", methods=["POST"])
@login_required
def gcal_auth_start():
    client_id = _get_setting("gcal_client_id")
    client_secret = _get_setting("gcal_client_secret")

    if not client_id or not client_secret:
        return jsonify({"ok": False, "error": "請先在 Admin 設定 gcal_client_id 與 gcal_client_secret"}), 400

    redirect_uri = _build_redirect_uri()

    try:
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }
            },
            scopes=SCOPES,
            redirect_uri=redirect_uri,
        )
        auth_url, state = flow.authorization_url(access_type="offline", prompt="consent")
        return jsonify({"ok": True, "auth_url": auth_url, "state": state})
    except Exception as exc:
        logger.exception("gcal_auth_start failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── GET /api/osc/gcal/auth/callback ──────────────────────────────────────────

@osc_gcal_bp.route("/api/osc/gcal/auth/callback", methods=["GET"])
def gcal_auth_callback():
    code = request.args.get("code", "")
    if not code:
        return _html_page("❌ 授權失敗", "未收到授權碼，請重新嘗試。")

    client_id = _get_setting("gcal_client_id")
    client_secret = _get_setting("gcal_client_secret")
    if not client_id or not client_secret:
        return _html_page("❌ 授權失敗", "Server 端尚未設定 gcal_client_id / gcal_client_secret。")

    redirect_uri = _build_redirect_uri()

    try:
        from google_auth_oauthlib.flow import Flow

        flow = Flow.from_client_config(
            {
                "web": {
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": [redirect_uri],
                }
            },
            scopes=SCOPES,
            redirect_uri=redirect_uri,
        )
        flow.fetch_token(code=code)
        creds = flow.credentials

        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())
        TOKEN_PATH.chmod(0o600)

        logger.info("GCal token saved to %s", TOKEN_PATH)
        return _html_page("✅ 授權成功", "Google Calendar 已成功連線，可關閉此分頁。", close=True)

    except Exception as exc:
        logger.exception("gcal_auth_callback failed")
        return _html_page("❌ 授權失敗", f"錯誤：{exc}")


def _html_page(title: str, body: str, close: bool = False) -> str:
    close_script = "<script>setTimeout(()=>window.close(),2000);</script>" if close else ""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font-family:sans-serif;display:flex;align-items:center;justify-content:center;
height:100vh;margin:0}}div{{text-align:center}}</style></head>
<body><div><h2>{title}</h2><p>{body}</p>{close_script}</div></body></html>"""


# ── POST /api/osc/gcal/disconnect ────────────────────────────────────────────

@osc_gcal_bp.route("/api/osc/gcal/disconnect", methods=["POST"])
@login_required
def gcal_disconnect():
    try:
        if TOKEN_PATH.exists():
            TOKEN_PATH.unlink()
        return jsonify({"ok": True})
    except Exception as exc:
        logger.exception("gcal_disconnect failed")
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── POST /api/osc/gcal/sync ──────────────────────────────────────────────────

@osc_gcal_bp.route("/api/osc/gcal/sync", methods=["POST"])
@login_required
def gcal_sync():
    creds = _load_creds()
    if creds is None or not creds.valid:
        return jsonify({"ok": False, "error": "尚未授權 Google Calendar，請先完成 OAuth 流程"}), 400

    payload = request.get_json(silent=True) or {}
    dry_run = bool(payload.get("dry_run", False))

    try:
        from skills.osc_orchestrator.gcal_sync import run_sync  # type: ignore
        stats = run_sync(dry_run=dry_run)
        return jsonify({"ok": True, **stats})
    except ImportError:
        # Fallback: import from skills directory directly
        import sys
        sys.path.insert(0, "/Users/ai/Desktop/MAGI_v2/skills/osc-orchestrator")
        try:
            from gcal_sync import run_sync  # type: ignore
            stats = run_sync(dry_run=dry_run)
            return jsonify({"ok": True, **stats})
        except Exception as exc2:
            logger.exception("gcal_sync fallback failed")
            return jsonify({"ok": False, "error": str(exc2)}), 500
    except Exception as exc:
        logger.exception("gcal_sync failed")
        return jsonify({"ok": False, "error": str(exc)}), 500
