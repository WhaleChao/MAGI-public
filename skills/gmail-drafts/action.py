#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gmail-drafts / action.py

Create Gmail drafts via Gmail API.
Policy: create draft only, never send, never delete.
"""

from __future__ import annotations
import logging

import argparse
import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from email.message import EmailMessage

_MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(_MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(_MAGI_ROOT))

from api.runtime_paths import (
    ensure_orch_on_sys_path,
    get_config_path,
    get_magi_root_dir,
    get_orch_dir,
    get_skill_python,
)

CODE_DIR = str(get_orch_dir())
_VENV_PY = str(get_skill_python())

DEFAULT_CREDENTIALS = os.environ.get("MAGI_GMAIL_CREDENTIALS_PATH", str(get_config_path("credentials.json")))
DEFAULT_TOKEN = os.environ.get("MAGI_GMAIL_COMPOSE_TOKEN_PATH", str(get_config_path("gmail_compose_token.json")))


def _scope_list_from_env(name: str, default: str) -> list[str]:
    raw = (os.environ.get(name) or default).strip()
    return [x.strip() for x in raw.split(",") if x.strip()]


def _maybe_reexec_venv() -> None:
    if os.environ.get("MAGI_GMAIL_DRAFTS_NO_VENV", "").strip() == "1":
        return
    try:
        if os.path.exists(_VENV_PY) and os.path.realpath(sys.executable) != os.path.realpath(_VENV_PY):
            os.execv(_VENV_PY, [_VENV_PY, __file__, *sys.argv[1:]])
    except Exception:
        logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 52, exc_info=True)


def _ok(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload.get("success") else 1


def _load_jsonish(text: str) -> dict:
    t = (text or "").strip()
    if not t:
        return {}
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else {"value": v}
    except Exception:
        return {"value": t}


def _eventlog(event: str, *, ok: Optional[bool] = None, payload: Optional[dict] = None, tags: Optional[dict] = None) -> None:
    try:
        ensure_orch_on_sys_path()
        import magi_eventlog  # type: ignore
        magi_eventlog.remember_event(event, ok=ok, payload=payload or {}, tags=tags or {}, source="gmail_drafts")
    except Exception:
        return


def _queue_local_draft(*, to: str, subject: str, body: str, thread_id: str = "", reason: str = "") -> dict:
    queue_dir = os.environ.get(
        "MAGI_LOCAL_DRAFT_QUEUE_DIR",
        str(get_magi_root_dir() / "_pending_gmail_drafts"),
    )
    os.makedirs(queue_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    draft_id = f"local_{ts}_{uuid4().hex[:8]}"
    out_path = os.path.join(queue_dir, f"{draft_id}.json")
    payload = {
        "draft_id": draft_id,
        "created_at": datetime.now().isoformat(),
        "to": to or "",
        "subject": subject or "",
        "body": body or "",
        "thread_id": thread_id or "",
        "reason": reason or "oauth_unavailable",
        "mode": "local_queue",
        "do_not_send": True,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return {"draft_id": draft_id, "path": out_path}


def _build_gmail_service(credentials_path: str, token_path: str, *, interactive: bool = False):
    """
    Returns (service, need_interactive_oauth, error_str)
    """
    credentials_path = (credentials_path or "").strip()
    token_path = (token_path or "").strip()
    if not credentials_path or not os.path.exists(credentials_path):
        return None, False, f"credentials_not_found:{credentials_path}"

    # Lazy imports (avoid hard dependency during unrelated runs)
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except Exception as e:
        return None, False, f"missing_google_deps:{type(e).__name__}"

    # Keep default scope compatible with existing long-lived modify tokens.
    # Users can override via MAGI_GMAIL_DRAFT_SCOPES.
    REQUEST_SCOPES = _scope_list_from_env(
        "MAGI_GMAIL_DRAFT_SCOPES",
        "https://www.googleapis.com/auth/gmail.modify",
    )

    creds = None
    if token_path and os.path.exists(token_path):
        try:
            token_scopes = REQUEST_SCOPES
            try:
                with open(token_path, "r", encoding="utf-8") as f:
                    token_data = json.load(f) or {}
                file_scopes = token_data.get("scopes")
                if isinstance(file_scopes, list) and file_scopes:
                    token_scopes = [str(x).strip() for x in file_scopes if str(x).strip()]
            except Exception:
                logging.getLogger(__name__).debug("silent-catch at %s:%s", __name__, 141, exc_info=True)
            creds = Credentials.from_authorized_user_file(token_path, token_scopes)
        except Exception:
            creds = None

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
        except Exception:
            creds = None

    if (not creds or not creds.valid) and interactive:
        try:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, REQUEST_SCOPES)
            creds = flow.run_local_server(port=0)
            os.makedirs(os.path.dirname(token_path), exist_ok=True)
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        except Exception:
            creds = None

    if not creds or not creds.valid:
        # Needs user interaction once.
        return None, True, "need_interactive_oauth"

    try:
        service = build("gmail", "v1", credentials=creds)
        return service, False, ""
    except Exception as e:
        return None, False, f"build_failed:{type(e).__name__}"


def create_draft(
    to: str,
    subject: str,
    body: str,
    *,
    credentials_path: str = "",
    token_path: str = "",
    thread_id: str = "",
    interactive: bool = False,
) -> dict:
    to = (to or "").strip()
    subject = (subject or "").strip()
    body = (body or "").rstrip()

    credentials_path = (credentials_path or DEFAULT_CREDENTIALS).strip()
    token_path = (token_path or DEFAULT_TOKEN).strip()

    service, need_oauth, err = _build_gmail_service(credentials_path, token_path, interactive=interactive)
    queue_on_oauth_error = (os.environ.get("MAGI_QUEUE_DRAFT_ON_OAUTH_ERROR", "1") or "").strip().lower() in {"1", "true", "yes", "on"}
    if need_oauth:
        if queue_on_oauth_error:
            queued = _queue_local_draft(to=to, subject=subject, body=body, thread_id=thread_id, reason="need_interactive_oauth")
            _eventlog("gmail_draft:create", ok=True, payload={"queued_local": True, "draft_id": queued.get("draft_id", "")})
            return {
                "success": True,
                "queued_local": True,
                "draft_id": queued.get("draft_id", ""),
                "local_path": queued.get("path", ""),
                "need_interactive_oauth": True,
                "error": "need_interactive_oauth",
                "credentials_path": credentials_path,
                "token_path": token_path,
            }
        _eventlog("gmail_draft:create", ok=False, payload={"error": "need_interactive_oauth"})
        return {"success": False, "need_interactive_oauth": True, "error": "need_interactive_oauth", "credentials_path": credentials_path, "token_path": token_path}
    if not service:
        if queue_on_oauth_error:
            queued = _queue_local_draft(to=to, subject=subject, body=body, thread_id=thread_id, reason=err or "service_unavailable")
            _eventlog("gmail_draft:create", ok=True, payload={"queued_local": True, "draft_id": queued.get("draft_id", "")})
            return {
                "success": True,
                "queued_local": True,
                "draft_id": queued.get("draft_id", ""),
                "local_path": queued.get("path", ""),
                "error": err,
            }
        _eventlog("gmail_draft:create", ok=False, payload={"error": err})
        return {"success": False, "error": err}

    msg = EmailMessage()
    if to:
        msg["To"] = to
    if subject:
        msg["Subject"] = subject
    msg.set_content(body or "")

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    payload = {"message": {"raw": raw}}
    if thread_id:
        payload["message"]["threadId"] = thread_id

    try:
        res = service.users().drafts().create(userId="me", body=payload).execute()
        draft_id = (res or {}).get("id", "")
        message_id = ((res or {}).get("message") or {}).get("id", "")
        _eventlog("gmail_draft:create", ok=True, payload={"draft_id": draft_id, "message_id": message_id})
        return {"success": True, "draft_id": draft_id, "message_id": message_id}
    except Exception as e:
        _eventlog("gmail_draft:create", ok=False, payload={"error": f"{type(e).__name__}:{str(e)[:200]}"})
        return {"success": False, "error": f"{type(e).__name__}: {str(e)[:220]}"}


def main() -> int:
    _maybe_reexec_venv()
    ap = argparse.ArgumentParser(description="gmail-drafts skill")
    ap.add_argument("--task", required=True, help="help|self_test|authorize|create {..json..}")
    args = ap.parse_args()
    task = (args.task or "").strip()

    if task in {"help", "list"}:
        return _ok({"success": True, "commands": ["help", "self_test", "create {..json..}"]})

    if task == "self_test":
        return _ok(
            {
                "success": True,
                "credentials_exists": os.path.exists(DEFAULT_CREDENTIALS),
                "token_exists": os.path.exists(DEFAULT_TOKEN),
                "credentials_path": DEFAULT_CREDENTIALS if os.path.exists(DEFAULT_CREDENTIALS) else "",
                "token_path": DEFAULT_TOKEN if os.path.exists(DEFAULT_TOKEN) else "",
            }
        )

    if task == "authorize":
        # Run interactive OAuth to create/refresh token file (daytime/admin).
        service, need_oauth, err = _build_gmail_service(DEFAULT_CREDENTIALS, DEFAULT_TOKEN, interactive=True)
        if service:
            return _ok({"success": True, "authorized": True, "token_path": DEFAULT_TOKEN})
        return _ok({"success": False, "authorized": False, "need_interactive_oauth": bool(need_oauth), "error": err, "token_path": DEFAULT_TOKEN})

    if task.startswith("create"):
        p = _load_jsonish(task[len("create") :].strip())
        return _ok(
            create_draft(
                to=(p.get("to") or ""),
                subject=(p.get("subject") or ""),
                body=(p.get("body") or p.get("text") or ""),
                credentials_path=(p.get("credentials_path") or ""),
                token_path=(p.get("token_path") or ""),
                thread_id=(p.get("thread_id") or ""),
                interactive=bool(p.get("interactive") or False),
            )
        )

    return _ok({"success": False, "error": f"unknown task: {task}"})


if __name__ == "__main__":
    raise SystemExit(main())
