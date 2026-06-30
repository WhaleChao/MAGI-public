#!/usr/bin/env python3
"""MAGI API/OAuth token health check and proactive refresh.

The report intentionally contains only metadata: service names, file paths,
expiry timestamps, and status. Secret values are never printed or written.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MAGI_ROOT = Path(__file__).resolve().parents[2]
if str(MAGI_ROOT) not in sys.path:
    sys.path.insert(0, str(MAGI_ROOT))

DEFAULT_REPORT_PATH = MAGI_ROOT / ".runtime" / "token_health" / "token_health_latest.json"
INVALID_GRANT_MARKERS = ("invalid_grant", "expired or revoked", "token has been revoked")
TOKEN_LOCK_TIMEOUT_SEC = float(os.environ.get("MAGI_GOOGLE_TOKEN_LOCK_TIMEOUT_SEC", "30") or "30")
UNVERIFIABLE_ACCOUNT_HINTS = {"", "primary"}


@dataclass
class GoogleTokenSpec:
    name: str
    token_path: Path
    scopes: list[str]
    credentials_path: Path | None = None
    account_hint: str = ""
    required: bool = False
    allow_missing_scope_field: bool = False
    note: str = ""


@dataclass
class ApiKeySpec:
    name: str
    env_names: tuple[str, ...]
    enabled_env: str | None = None
    default_enabled: bool = False
    required_env: str | None = None


def _load_local_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(MAGI_ROOT / ".env")
        return
    except Exception:
        pass

    env_path = MAGI_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        os.environ.setdefault(key, value.strip().strip('"').strip("'"))


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _configured_env(*names: str) -> bool:
    return any(_usable_secret(os.environ.get(name) or "") for name in names)


def _usable_secret(value: str) -> bool:
    v = str(value or "").strip()
    if not v:
        return False
    low = v.lower()
    placeholders = (
        "your_",
        "changeme",
        "change_me",
        "placeholder",
        "todo",
        "none",
        "null",
        "example",
        "dummy",
    )
    return not any(p in low for p in placeholders)


def _safe_status_ok(status: str) -> bool:
    return status in {"ok", "refreshed", "refreshable_soon", "skipped"}


def _parse_expiry(raw: Any) -> datetime | None:
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _normalize_scopes(raw: Any) -> set[str]:
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {part.strip() for part in raw.split() if part.strip()}
    if isinstance(raw, list):
        return {str(part).strip() for part in raw if str(part).strip()}
    return set()


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    padding = "=" * (-len(payload) % 4)
    try:
        data = base64.urlsafe_b64decode((payload + padding).encode("ascii"))
        decoded = json.loads(data.decode("utf-8"))
    except Exception:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _token_account_metadata(data: dict[str, Any]) -> str:
    for key in (
        "account",
        "account_email",
        "email",
        "user_email",
        "login_hint",
        "principal",
        "subject",
    ):
        value = str(data.get(key) or "").strip()
        if value:
            return value

    id_payload = _decode_jwt_payload(str(data.get("id_token") or ""))
    for key in ("email", "preferred_username", "sub"):
        value = str(id_payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _account_hint_status(account_hint: str, token_account: str) -> tuple[bool | None, str]:
    hint = str(account_hint or "").strip()
    token_value = str(token_account or "").strip()
    if hint.lower() in UNVERIFIABLE_ACCOUNT_HINTS:
        return None, "not_configured" if not hint else "not_verifiable_primary_hint"
    if not token_value:
        return None, "not_verifiable_missing_token_account"
    if hint.lower() == token_value.lower():
        return True, "match"
    return False, "mismatch"


def _reauth_next_action(spec: GoogleTokenSpec, reason: str) -> str:
    hint = f" with account {spec.account_hint}" if spec.account_hint else ""
    credentials = f" using {spec.credentials_path}" if spec.credentials_path else ""
    if reason == "missing_scope":
        return f"Re-authorize{hint}{credentials} so the saved token includes the required scopes."
    if reason == "account_mismatch":
        return f"Re-authorize{hint}{credentials}; the saved token metadata points at a different account."
    if reason == "missing_refresh_token":
        return f"Re-authorize{hint}{credentials} with offline access/consent so a refresh_token is saved."
    if reason == "invalid_grant":
        return f"Re-authorize{hint}{credentials}; Google reports the saved grant is expired or revoked."
    return f"Re-authorize{hint}{credentials} and replace the saved token file."


def _refresh_next_action(spec: GoogleTokenSpec, *, refresh_token_present: bool) -> str:
    if refresh_token_present:
        return "Run scripts/ops/token_health_check.py --refresh or let scripts/ops/run_after_token_refresh.py refresh before the job."
    return _reauth_next_action(spec, "missing_refresh_token")


def _atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent), delete=False) as tmp:
            try:
                os.fchmod(tmp.fileno(), mode)
            except Exception:
                pass
            tmp.write(text)
            tmp.flush()
            try:
                os.fsync(tmp.fileno())
            except Exception:
                pass
            tmp_path = Path(tmp.name)
        os.replace(str(tmp_path), path)
        try:
            path.chmod(mode)
        except Exception:
            pass
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def _token_lock_path(token_path: Path) -> Path:
    path = token_path.expanduser()
    safe_name = path.name.replace(os.sep, "_") or "token"
    return path.parent / f".{safe_name}.lock"


@contextlib.contextmanager
def google_token_file_lock(token_path: Path, *, timeout_sec: float | None = None):
    """Cross-process lock for a single Google OAuth token file."""
    path = token_path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = _token_lock_path(path)
    deadline = time.monotonic() + (TOKEN_LOCK_TIMEOUT_SEC if timeout_sec is None else max(0.0, timeout_sec))
    with lock_path.open("a+", encoding="utf-8") as fh:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"google_token_lock_timeout:{lock_path}")
                time.sleep(0.05)
        try:
            yield lock_path
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _safe_token_metadata(token_path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        data = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, "token file is not a JSON object"
    return data, ""


def _refresh_google_token(spec: GoogleTokenSpec) -> tuple[bool, str]:
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except Exception as exc:
        return False, f"missing_google_dependencies:{type(exc).__name__}"

    try:
        with google_token_file_lock(spec.token_path):
            creds = Credentials.from_authorized_user_file(str(spec.token_path), spec.scopes)
            if not getattr(creds, "refresh_token", None):
                return False, "missing_refresh_token"
            creds.refresh(Request())
            _atomic_write_text(spec.token_path, creds.to_json())
        return True, ""
    except Exception as exc:
        text = str(exc)
        if any(marker in text.lower() for marker in INVALID_GRANT_MARKERS):
            return False, "invalid_grant"
        return False, f"{type(exc).__name__}: {text[:180]}"


def check_google_token(
    spec: GoogleTokenSpec,
    *,
    refresh: bool = False,
    threshold_seconds: int = 7 * 24 * 3600,
) -> dict[str, Any]:
    token_path = spec.token_path.expanduser()
    credentials_path = spec.credentials_path.expanduser() if spec.credentials_path else None
    base = {
        "kind": "google_oauth",
        "name": spec.name,
        "path": str(token_path),
        "credentials_path": str(credentials_path) if credentials_path else "",
        "account_hint": spec.account_hint,
        "required": spec.required,
        "note": spec.note,
        "exists": token_path.exists(),
        "ok": False,
        "status": "",
        "message": "",
        "next_action": "",
        "expires_at": "",
        "expires_in_hours": None,
        "refresh_token_present": False,
        "scopes_ok": None,
        "missing_scopes": [],
        "account_from_token": "",
        "account_hint_ok": None,
        "account_check_status": "not_checked",
        "account_mismatch": False,
        "auth_required_reason": "",
        "refreshed": False,
    }
    if not token_path.exists():
        base["status"] = "missing_token" if spec.required else "skipped"
        base["ok"] = not spec.required
        base["message"] = "token file missing"
        if spec.required:
            base["next_action"] = _reauth_next_action(spec, "missing_token")
        return base

    try:
        token_path.chmod(0o600)
    except Exception:
        pass

    data, error = _safe_token_metadata(token_path)
    if data is None:
        base["status"] = "invalid_token_file"
        base["message"] = error
        return base

    expiry = _parse_expiry(data.get("expiry"))
    now = datetime.now(timezone.utc)
    token_scopes = _normalize_scopes(data.get("scopes") or data.get("scope"))
    requested_scopes = set(spec.scopes or [])
    missing_scopes = sorted(requested_scopes - token_scopes) if token_scopes else sorted(requested_scopes)
    if requested_scopes:
        if token_scopes:
            scopes_ok = requested_scopes.issubset(token_scopes)
        else:
            scopes_ok = bool(spec.allow_missing_scope_field)
            if scopes_ok:
                missing_scopes = []
    else:
        scopes_ok = True
        missing_scopes = []

    base["refresh_token_present"] = bool(data.get("refresh_token"))
    base["scopes_ok"] = scopes_ok
    base["missing_scopes"] = missing_scopes
    token_account = _token_account_metadata(data)
    account_hint_ok, account_check_status = _account_hint_status(spec.account_hint, token_account)
    base["account_from_token"] = token_account
    base["account_hint_ok"] = account_hint_ok
    base["account_check_status"] = account_check_status
    base["account_mismatch"] = account_hint_ok is False
    if expiry:
        expires_in = (expiry - now).total_seconds()
        base["expires_at"] = expiry.isoformat()
        base["expires_in_hours"] = round(expires_in / 3600, 2)
    else:
        expires_in = None

    if not scopes_ok:
        base["status"] = "missing_scope"
        scope_text = ", ".join(missing_scopes[:4])
        if len(missing_scopes) > 4:
            scope_text += f", ... (+{len(missing_scopes) - 4})"
        base["message"] = f"saved token is missing required scope(s): {scope_text or 'unknown'}"
        base["next_action"] = _reauth_next_action(spec, "missing_scope")
        return base

    if account_hint_ok is False:
        base["status"] = "account_mismatch"
        base["message"] = "saved token account metadata does not match the expected account hint"
        base["next_action"] = _reauth_next_action(spec, "account_mismatch")
        base["auth_required_reason"] = "account_mismatch"
        return base

    if not base["refresh_token_present"]:
        base["status"] = "auth_required"
        base["message"] = "saved token is missing refresh_token, so unattended refresh will not work"
        base["next_action"] = _reauth_next_action(spec, "missing_refresh_token")
        base["auth_required_reason"] = "missing_refresh_token"
        return base

    if expires_in is None:
        base["status"] = "expiry_unknown"
        base["message"] = "token has no expiry metadata; re-auth is safer"
        base["next_action"] = _reauth_next_action(spec, "expiry_unknown")
        return base

    needs_refresh = expires_in <= threshold_seconds
    if needs_refresh and refresh:
        ok, detail = _refresh_google_token(spec)
        if ok:
            data2, _ = _safe_token_metadata(token_path)
            expiry2 = _parse_expiry((data2 or {}).get("expiry")) if data2 else None
            if isinstance(data2, dict):
                base["refresh_token_present"] = bool(data2.get("refresh_token"))
            base["status"] = "refreshed"
            base["ok"] = True
            base["refreshed"] = True
            base["message"] = "token refreshed proactively"
            if expiry2:
                base["expires_at"] = expiry2.isoformat()
                base["expires_in_hours"] = round((expiry2 - now).total_seconds() / 3600, 2)
            return base
        if detail in {"invalid_grant", "missing_refresh_token"}:
            base["status"] = "auth_required"
            base["auth_required_reason"] = detail
            if detail == "missing_refresh_token":
                base["message"] = "saved token is missing refresh_token, so unattended refresh will not work"
                base["next_action"] = _reauth_next_action(spec, "missing_refresh_token")
            else:
                base["message"] = "Google OAuth grant must be renewed interactively"
                base["next_action"] = _reauth_next_action(spec, "invalid_grant")
            return base
        base["status"] = "refresh_failed"
        base["message"] = detail
        base["next_action"] = "Inspect Google OAuth dependency/network errors, then rerun scripts/ops/token_health_check.py --refresh."
        return base

    if expires_in <= 0:
        base["status"] = "expired"
        base["message"] = "token is expired; run with --refresh or re-authorize"
        base["next_action"] = _refresh_next_action(spec, refresh_token_present=bool(base["refresh_token_present"]))
        return base
    if needs_refresh:
        base["status"] = "refreshable_soon"
        base["ok"] = True
        base["message"] = "token is inside proactive refresh window and can refresh unattended"
        base["next_action"] = "No manual re-authorization needed; run with --refresh or let run_after_token_refresh refresh before the job."
        return base

    base["status"] = "ok"
    base["ok"] = True
    base["message"] = "token valid outside refresh window"
    return base


def _discover_google_tokens() -> list[GoogleTokenSpec]:
    specs: list[GoogleTokenSpec] = []

    try:
        from api.osc import accounting_sheet_import as acct

        token_path = acct._default_token_path()
        credentials_path = acct._default_credentials_path()
        required = (
            token_path.exists()
            or _configured_env("MAGI_ACCOUNTING_GOOGLE_SHEETS_TOKEN", "MAGI_GOOGLE_SHEETS_TOKEN")
            or _configured_env("MAGI_COLLEAGUE_SHEET_ID", "MAGI_ACCOUNTING_GOOGLE_SHEET_ID")
        )
        specs.append(
            GoogleTokenSpec(
                name="google_accounting_sheets",
                token_path=token_path,
                credentials_path=credentials_path,
                scopes=list(acct.GOOGLE_READ_SCOPES),
                account_hint=str(acct.DEFAULT_ACCOUNT_HINT),
                required=required,
                note="OSC accounting Google Sheets/Drive import",
            )
        )
    except Exception:
        pass

    try:
        from api.osc import drive_case_sync as drive

        readonly_path = drive.drive_sync_token_path(write=False)
        write_path = drive.drive_sync_token_path(write=True)
        credentials_path = drive.drive_sync_credentials_path()
        hint = drive.drive_sync_account_hint()
        specs.append(
            GoogleTokenSpec(
                name="google_drive_sync_readonly",
                token_path=readonly_path,
                credentials_path=credentials_path,
                scopes=list(drive.READONLY_SCOPES),
                account_hint=hint,
                required=readonly_path.exists() or _configured_env("MAGI_DRIVE_SYNC_TOKEN"),
                note="Drive/NAS case inventory and download sync",
            )
        )
        specs.append(
            GoogleTokenSpec(
                name="google_drive_sync_write",
                token_path=write_path,
                credentials_path=credentials_path,
                scopes=list(drive.WRITE_SCOPES),
                account_hint=hint,
                required=write_path.exists() or _configured_env("MAGI_DRIVE_SYNC_WRITE_TOKEN"),
                note="Drive/NAS upload and folder creation sync",
            )
        )
    except Exception:
        pass

    calendar_scopes = ["https://www.googleapis.com/auth/calendar"]
    paperclip_calendar = Path.home() / ".magi" / "google" / "token.json"
    configured_calendar_raw = (os.environ.get("MAGI_GOOGLE_CALENDAR_TOKEN_PATH") or "").strip()
    if configured_calendar_raw:
        calendar_token_path = Path(configured_calendar_raw).expanduser()
    elif paperclip_calendar.exists():
        calendar_token_path = paperclip_calendar
    else:
        calendar_token_path = MAGI_ROOT / "json" / "google_calendar_token.json"
    required = calendar_token_path.exists() or bool(configured_calendar_raw)
    specs.append(
        GoogleTokenSpec(
            name="google_calendar",
            token_path=calendar_token_path,
            credentials_path=Path(
                os.environ.get("MAGI_GOOGLE_CREDENTIALS_PATH", "") or MAGI_ROOT / "json" / "credentials.json"
            ).expanduser(),
            scopes=calendar_scopes,
            account_hint="primary",
            required=required,
            allow_missing_scope_field=True,
            note="OSC todos and Google Calendar sync",
        )
    )

    return specs


def _discover_api_keys() -> list[ApiKeySpec]:
    return [
        ApiKeySpec(
            name="line_messaging",
            env_names=("MAGI_LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_ACCESS_TOKEN"),
            enabled_env="MAGI_ENABLE_LINE",
            default_enabled=True,
        ),
        ApiKeySpec(
            name="line_channel_secret",
            env_names=("MAGI_LINE_CHANNEL_SECRET", "LINE_CHANNEL_SECRET"),
            enabled_env="MAGI_ENABLE_LINE",
            default_enabled=True,
        ),
        ApiKeySpec(
            name="discord_bot",
            env_names=("DISCORD_BOT_TOKEN",),
            enabled_env="MAGI_ENABLE_DISCORD",
            default_enabled=False,
        ),
        ApiKeySpec(
            name="telegram_bot",
            env_names=("OPENCLAW_TELEGRAM_BOT_TOKEN", "MAGI_TELEGRAM_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"),
            enabled_env="MAGI_ENABLE_TELEGRAM",
            default_enabled=False,
        ),
        ApiKeySpec(
            name="nvidia_nim",
            env_names=("NVIDIA_NIM_API_KEY",),
            enabled_env="NVIDIA_NIM_ENABLE",
            default_enabled=False,
        ),
        ApiKeySpec(
            name="gemini",
            env_names=("MAGI_GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"),
            required_env="MAGI_REQUIRE_GEMINI_API_KEY",
        ),
        ApiKeySpec(
            name="finnhub",
            env_names=("FINNHUB_API_KEY", "MAGI_FINNHUB_API_KEY"),
            required_env="MAGI_REQUIRE_FINNHUB_API_KEY",
        ),
        ApiKeySpec(
            name="huggingface",
            env_names=("HF_TOKEN", "HUGGINGFACE_HUB_TOKEN"),
            required_env="MAGI_REQUIRE_HUGGINGFACE_TOKEN",
        ),
        ApiKeySpec(
            name="magi_internal_api",
            env_names=("MAGI_API_KEY", "MAGI_EXTERNAL_API_KEY"),
            required_env="MAGI_REQUIRE_INTERNAL_API_KEY",
        ),
    ]


def check_api_key(spec: ApiKeySpec) -> dict[str, Any]:
    enabled = True
    if spec.enabled_env:
        enabled = _truthy(os.environ.get(spec.enabled_env), default=spec.default_enabled)
    required = _truthy(os.environ.get(spec.required_env), default=False) if spec.required_env else enabled
    configured_names = [name for name in spec.env_names if _usable_secret(os.environ.get(name) or "")]
    if not enabled and not required and not configured_names:
        return {
            "kind": "api_key",
            "name": spec.name,
            "env_names": list(spec.env_names),
            "enabled": False,
            "required": False,
            "configured": False,
            "ok": True,
            "status": "skipped",
            "message": "service disabled or optional",
        }
    ok = bool(configured_names)
    return {
        "kind": "api_key",
        "name": spec.name,
        "env_names": list(spec.env_names),
        "enabled": enabled,
        "required": required,
        "configured": ok,
        "ok": ok or not required,
        "status": "ok" if ok else ("missing_key" if required else "skipped"),
        "message": "configured" if ok else "not configured",
    }


def build_report(*, refresh: bool, threshold_days: float) -> dict[str, Any]:
    _load_local_env()
    threshold_seconds = int(max(0.1, threshold_days) * 24 * 3600)
    google = [
        check_google_token(spec, refresh=refresh, threshold_seconds=threshold_seconds)
        for spec in _discover_google_tokens()
    ]
    api_keys = [check_api_key(spec) for spec in _discover_api_keys()]
    checks = google + api_keys
    failure_fields = (
        "kind",
        "name",
        "status",
        "message",
        "next_action",
        "path",
        "refresh_token_present",
        "scopes_ok",
        "missing_scopes",
        "account_hint",
        "account_from_token",
        "account_hint_ok",
        "account_check_status",
        "account_mismatch",
        "auth_required_reason",
    )
    failures = [
        {key: item.get(key, "" if key in {"path", "next_action"} else None) for key in failure_fields if key in item}
        for item in checks
        if not bool(item.get("ok"))
    ]
    report = {
        "ok": not failures,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "threshold_days": threshold_days,
        "refresh_requested": refresh,
        "summary": {
            "total": len(checks),
            "google_oauth": len(google),
            "api_keys": len(api_keys),
            "failures": len(failures),
            "refreshed": sum(1 for item in google if item.get("refreshed")),
            "skipped": sum(1 for item in checks if item.get("status") == "skipped"),
        },
        "checks": checks,
        "failures": failures,
    }
    return report


def _notify_if_needed(report: dict[str, Any]) -> None:
    if report.get("ok"):
        return
    failures = report.get("failures") if isinstance(report.get("failures"), list) else []
    lines = ["MAGI API/OAuth token health needs attention:"]
    for item in failures[:8]:
        name = str(item.get("name") or "?")
        status = str(item.get("status") or "?")
        message = str(item.get("message") or "")[:100]
        lines.append(f"- {name}: {status} {message}".rstrip())
    if len(failures) > 8:
        lines.append(f"- ... {len(failures) - 8} more")
    try:
        from skills.ops.red_phone import send_telegram_push_with_status

        send_telegram_push_with_status(
            "\n".join(lines),
            severity="warning",
            source="token_health_check",
            topic_key="check",
        )
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check and proactively refresh MAGI API/OAuth tokens.")
    parser.add_argument("--refresh", action="store_true", help="Refresh Google OAuth tokens inside the threshold window.")
    parser.add_argument("--threshold-days", type=float, default=7.0, help="Proactive refresh window in days.")
    parser.add_argument("--json-out", default=str(DEFAULT_REPORT_PATH), help="Safe JSON report path.")
    parser.add_argument("--notify", action="store_true", help="Notify when action is required.")
    args = parser.parse_args(argv)

    report = build_report(refresh=args.refresh, threshold_days=args.threshold_days)
    out_path = Path(args.json_out).expanduser()
    _atomic_write_text(out_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n", mode=0o600)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.notify:
        _notify_if_needed(report)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    started = time.time()
    try:
        raise SystemExit(main())
    finally:
        _ = started
